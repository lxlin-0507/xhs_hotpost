"""
fetch_and_rank.py — 一键拉取所有数据源 + 生成 rank

跨平台（Windows / macOS / Linux）。直接用 import 调用各爬虫 main()，
不依赖 shell 脚本，无需 python3 / bash 等平台差异。

用法：
  python fetch_and_rank.py                          # 拉取全部来源 + 生成 rank
  python fetch_and_rank.py --skip-crawl             # 跳过爬取，只生成 rank
  python fetch_and_rank.py --sources weibo douyin_hotlist  # 只跑指定来源
  python fetch_and_rank.py --segmenter api          # 用外部 API 切词
  python fetch_and_rank.py --out-dir D:/rank_output # 指定输出目录

Windows 注意事项（首次运行前）：
  1. 安装依赖：  pip install -r requirements.txt
  2. 安装浏览器：python -m playwright install chromium
  3. 准备配置：  复制 .env.example → .env.dev，按需填写路径
                 （OUTPUT_DIR 等路径用正斜杠或双反斜杠均可）
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# ── 项目根目录加入 sys.path ────────────────────────────────
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
# hotwords_service 也需要在 path 里（segmenter / run 里的相对 import）
_HWS = _ROOT / "hotwords_service"
if str(_HWS) not in sys.path:
    sys.path.insert(0, str(_HWS))

from logger import get_logger  # noqa: E402

log = get_logger("fetch_and_rank")

# ── 来源定义 ───────────────────────────────────────────────
# key = 来源名，value = (显示名, 导入路径, 是否需要登录态)
SOURCES: Dict[str, dict] = {
    "weibo":          {"name": "微博热搜",   "module": "crawler.weibo.run",          "need_auth": False},
    "douyin_hotlist": {"name": "抖音热榜",   "module": "crawler.douyin_hotlist.run", "need_auth": False},
    "xhs_hot":        {"name": "小红书热点", "module": "crawler.xhs.run",            "need_auth": False},
    "xhs_hotpost":    {"name": "小红书热帖", "module": "crawler.xhs_hotpost.run",    "need_auth": False},  # noauth 默认
    "douyin":         {"name": "抖音热词",   "module": "crawler.douyin.run",         "need_auth": True},
}


def _run_crawler(key: str) -> bool:
    """动态导入并运行指定爬虫的 main()，返回是否成功。"""
    info = SOURCES[key]
    log.info(f"[{info['name']}] 开始爬取 ...")
    t0 = time.time()
    try:
        import importlib
        mod = importlib.import_module(info["module"])
        result = mod.main()
        elapsed = time.time() - t0
        ok = bool(result)
        status = "完成" if ok else "无数据（跳过）"
        log.info(f"[{info['name']}] {status}  耗时 {elapsed:.1f}s")
        return ok
    except Exception as exc:
        elapsed = time.time() - t0
        log.error(f"[{info['name']}] 爬取失败: {exc}  耗时 {elapsed:.1f}s", exc_info=True)
        return False


def run_crawlers(sources: List[str]) -> Dict[str, bool]:
    """按顺序运行指定来源的爬虫，返回 {key: success} 结果表。"""
    results = {}
    for key in sources:
        if key not in SOURCES:
            log.warning(f"未知来源 '{key}'，跳过")
            results[key] = False
            continue
        results[key] = _run_crawler(key)
    return results


def run_rank(
    base_dir: str,
    out_dir: Optional[str],
    segmenter: str = "hanlp",
    mode: str = "phrase",
) -> Optional[str]:
    """调用 hotwords_service.run_normalization 生成 rank CSV。"""
    log.info(f"开始生成 rank  base_dir={base_dir}  segmenter={segmenter}")
    t0 = time.time()
    try:
        import importlib
        run_mod = importlib.import_module("run")  # hotwords_service/ 已在 sys.path
        run_normalization = run_mod.run_normalization
        path = run_normalization(
            base_dir=base_dir,
            out_dir=out_dir,
            mode=mode,
            segmenter_type=segmenter,
        )
        elapsed = time.time() - t0
        if path:
            log.info(f"rank 生成完成: {path}  耗时 {elapsed:.1f}s")
        else:
            log.warning(f"rank 生成：无数据，未输出文件  耗时 {elapsed:.1f}s")
        return path
    except Exception as exc:
        log.error(f"rank 生成失败: {exc}", exc_info=True)
        return None


def print_summary(crawl_results: Dict[str, bool], rank_path: Optional[str]) -> None:
    log.info("=" * 60)
    log.info("运行摘要")
    log.info("=" * 60)
    for key, ok in crawl_results.items():
        name = SOURCES.get(key, {}).get("name", key)
        mark = "✓" if ok else "✗"
        log.info(f"  {mark} {name}")
    if rank_path:
        log.info(f"  → rank 文件: {rank_path}")
    else:
        log.info("  → rank 文件: 未生成")
    log.info("=" * 60)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="一键拉取所有数据源 + 生成 rank（Windows / macOS / Linux）"
    )
    parser.add_argument(
        "--sources", nargs="*",
        default=list(SOURCES.keys()),
        help=f"要爬取的来源（默认全部）: {', '.join(SOURCES.keys())}",
    )
    parser.add_argument(
        "--skip-crawl", action="store_true",
        help="跳过爬取，只生成 rank（用已有数据）",
    )
    parser.add_argument(
        "--base-dir", default=str(_ROOT / "output"),
        help="数据根目录（默认 output/）",
    )
    parser.add_argument(
        "--out-dir", default=None,
        help="rank 输出目录（默认 <base-dir>/rank）",
    )
    parser.add_argument(
        "--segmenter", choices=["hanlp", "api", "both"], default="hanlp",
        help="切词器（默认 hanlp）",
    )
    parser.add_argument(
        "--mode", choices=["phrase", "word"], default="phrase",
        help="切词粒度（默认 phrase）",
    )
    args = parser.parse_args()

    log.info("=" * 60)
    log.info(f"fetch_and_rank 启动  {datetime.now():%Y-%m-%d %H:%M:%S}")
    log.info("=" * 60)

    crawl_results: Dict[str, bool] = {}

    if not args.skip_crawl:
        crawl_results = run_crawlers(args.sources)
        success_count = sum(crawl_results.values())
        log.info(f"爬取完成: {success_count}/{len(crawl_results)} 个来源成功")
        if success_count == 0:
            log.error("所有来源均爬取失败，终止")
            return 1

    rank_path = run_rank(
        base_dir=args.base_dir,
        out_dir=args.out_dir,
        segmenter=args.segmenter,
        mode=args.mode,
    )

    print_summary(crawl_results, rank_path)
    return 0 if rank_path else 1


if __name__ == "__main__":
    sys.exit(main())

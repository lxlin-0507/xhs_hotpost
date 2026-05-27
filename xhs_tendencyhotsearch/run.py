"""
xhs_tendencyhotsearch/run.py — 方案二全流程入口。

  Step 1: 触发 XHS querytrending 拿候选趋势词 → trending_candidates_<TS>.txt
  Step 2: 逐词调 search/notes 取互动数据 + 综合分排序，同时收集前 K 条笔记链接
          → trending_ranked_<TS>.txt
  Step 3: 渲染 Markdown：每个趋势词作为 H1，笔记链接作为 H2
          → trending_notes_<TS>.md

用法：
  python -m xhs_tendencyhotsearch.run
  python -m xhs_tendencyhotsearch.run --top 20 --notes-per-keyword 5
  python -m xhs_tendencyhotsearch.run --max-candidates 30 --no-headless
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from xhs_tendencyhotsearch.fetch_trending import (  # noqa: E402
    fetch_trending_keywords,
    save_candidates_txt,
)
from xhs_tendencyhotsearch.score_and_links import score_and_collect  # noqa: E402

logger = logging.getLogger("xhs_tendencyhotsearch.run")

_OUT_DIR = Path(__file__).resolve().parent / "output"
_DEFAULT_PROFILE_DIR = _PROJECT_ROOT / "browser_profile" / "xhs"


def _save_ranked_txt(results: List[Dict], out_dir: Path, ts: datetime) -> Path:
    day_dir = out_dir / ts.strftime("%Y%m%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    fp = day_dir / f"trending_ranked_{ts:%Y%m%d_%H%M}.txt"
    header = (
        "rank\tkeyword\tscore\ttrending_rank\traw_heat\titems\tinteract_sum\trecent_ratio\tmatch_ratio"
    )
    lines = [header]
    for i, r in enumerate(results, 1):
        f = r["features"]
        lines.append(
            f"{i}\t{r['keyword']}\t{r['score']}\t{r['trending_rank']}\t{r['raw_heat']}\t"
            f"{f['total']}\t{f['interact_sum']}\t{f['recent_ratio']:.3f}\t{f['match_ratio']:.3f}"
        )
    fp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return fp


def _save_breakdown_json(results: List[Dict], out_dir: Path, ts: datetime) -> Path:
    day_dir = out_dir / ts.strftime("%Y%m%d")
    fp = day_dir / f"trending_breakdown_{ts:%Y%m%d_%H%M}.json"
    fp.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return fp


def _build_markdown(
    top_results: List[Dict],
    generated_at: datetime,
    ranked_txt: Path,
) -> str:
    lines: List[str] = []
    lines.append(f"<!-- generated at {generated_at:%Y-%m-%d %H:%M:%S} -->")
    lines.append(f"<!-- source txt: {ranked_txt.name} -->")
    lines.append("")
    for i, r in enumerate(top_results, 1):
        f = r["features"]
        suffix = (
            f"（综合分 {r['score']} · 互动 {f['interact_sum']} · "
            f"近30天占比 {f['recent_ratio']:.0%} · trending#{r['trending_rank']}）"
        )
        lines.append(f"# {i}. {r['keyword']}{suffix}")
        lines.append("")
        links = r["links"]
        if not links:
            lines.append("## （未抓到笔记链接）")
            lines.append("")
            continue
        for url in links:
            lines.append(f"## {url}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="XHS 趋势热搜（方案二）: querytrending + 搜索验证 + 综合分")
    parser.add_argument("--top", type=int, default=20, help="最终 Markdown 取综合分 Top N（默认 20）")
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=30,
        help="从 querytrending 拿多少个候选词参与打分（默认 30）",
    )
    parser.add_argument(
        "--notes-per-keyword", type=int, default=5,
        help="每个词在 Markdown 中保留前 K 条笔记链接（默认 5）",
    )
    parser.add_argument(
        "--page-size", type=int, default=20,
        help="search/notes 每次请求的 page_size，用于互动统计（默认 20）",
    )
    parser.add_argument(
        "--no-headless", dest="headless", action="store_false",
        help="弹出浏览器窗口（调试用）",
    )
    parser.add_argument(
        "--profile-dir", default=str(_DEFAULT_PROFILE_DIR),
        help=f"XHS 浏览器 profile 目录（默认: {_DEFAULT_PROFILE_DIR}）",
    )
    parser.set_defaults(headless=True)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    profile_dir = Path(args.profile_dir)
    if not profile_dir.is_dir():
        logger.error(
            f"未找到 XHS 浏览器 profile: {profile_dir}\n"
            "首次使用请先扫码登录：\n"
            "  python -m crawler.xhs_hotpost.login"
        )
        return 2

    ts = datetime.now()

    # ── Step 1 ─────────────────────────────────────────
    logger.info("Step 1/3: 触发 querytrending 拿候选趋势词")
    try:
        candidates = fetch_trending_keywords(
            profile_dir=str(profile_dir),
            headless=args.headless,
            max_count=args.max_candidates,
        )
    except RuntimeError as e:
        logger.error(f"启动浏览器失败: {e}")
        return 2
    if not candidates:
        logger.error("querytrending 返回空，终止")
        return 1
    logger.info(f"共拿到 {len(candidates)} 个候选词")
    cand_txt = save_candidates_txt(candidates, _OUT_DIR, ts=ts)
    logger.info(f"候选词 TXT: {cand_txt}")

    # ── Step 2 ─────────────────────────────────────────
    logger.info("Step 2/3: 逐词搜索验证并计算综合分")
    try:
        results = score_and_collect(
            candidates=candidates,
            profile_dir=str(profile_dir),
            headless=args.headless,
            page_size=args.page_size,
            notes_per_keyword=args.notes_per_keyword,
        )
    except RuntimeError as e:
        logger.error(f"打分阶段失败: {e}")
        return 2

    ranked_txt = _save_ranked_txt(results, _OUT_DIR, ts)
    breakdown_json = _save_breakdown_json(results, _OUT_DIR, ts)
    logger.info(f"排序后 TXT: {ranked_txt}")
    logger.info(f"详细打分 JSON: {breakdown_json}")

    # ── Step 3 ─────────────────────────────────────────
    logger.info("Step 3/3: 生成 Markdown")
    top_results = results[: args.top]
    md_text = _build_markdown(top_results, ts, ranked_txt)
    md_path = ranked_txt.with_name(f"trending_notes_{ts:%Y%m%d_%H%M}.md")
    md_path.write_text(md_text, encoding="utf-8")
    logger.info(f"Markdown 已写入: {md_path}")

    print("\n=== 完成 ===")
    print(f"候选词 TXT  : {cand_txt}")
    print(f"排序后 TXT  : {ranked_txt}")
    print(f"打分 JSON   : {breakdown_json}")
    print(f"笔记 MD     : {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

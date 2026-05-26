"""
xhs_hotsearch/run.py — 串起整个流程：

  1. 从 rebang.today 抓取小红书热搜榜，写入带时间戳 TXT
  2. 用 Playwright 在小红书官网搜索 Top-N 热搜词，每个词取前 K 个笔记链接
  3. 输出 Markdown：每个热搜作为一级标题，笔记链接作为二级标题

用法:
  python -m xhs_hotsearch.run
  python -m xhs_hotsearch.run --top 20 --notes-per-keyword 5
  python -m xhs_hotsearch.run --no-headless          # 调试看浏览器
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

# 把项目根加入 sys.path，方便复用项目其它包（虽然这里没用到）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from xhs_hotsearch.fetch_hotsearch import fetch_hotsearch, save_hotsearch_txt  # noqa: E402
from xhs_hotsearch.search_notes import search_notes_for_keywords  # noqa: E402

logger = logging.getLogger("xhs_hotsearch.run")

_OUT_DIR = Path(__file__).resolve().parent / "output"
# 默认复用项目已有的 XHS profile（crawler/xhs_hotpost 使用的同一个）
_DEFAULT_PROFILE_DIR = _PROJECT_ROOT / "browser_profile" / "xhs"


def _build_markdown(
    top_items: List[Dict],
    notes_map: Dict[str, List[str]],
    generated_at: datetime,
    hotsearch_txt: Path,
) -> str:
    lines: List[str] = []
    lines.append(f"<!-- generated at {generated_at:%Y-%m-%d %H:%M:%S} -->")
    lines.append(f"<!-- source txt: {hotsearch_txt.name} -->")
    lines.append("")
    for it in top_items:
        title = it["title"]
        rank = it["rank"]
        heat = it["heat"]
        icon = it.get("icon") or ""
        suffix_parts = [f"热度 {heat}"]
        if icon:
            suffix_parts.append(icon)
        suffix = "（" + " · ".join(suffix_parts) + "）" if suffix_parts else ""
        lines.append(f"# {rank}. {title}{suffix}")
        lines.append("")
        links = notes_map.get(title) or []
        if not links:
            lines.append("## （未抓到笔记链接）")
            lines.append("")
            continue
        for url in links:
            lines.append(f"## {url}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="小红书热搜榜 + 笔记链接采集")
    parser.add_argument("--top", type=int, default=20, help="取热搜榜前 N 条（默认 20）")
    parser.add_argument(
        "--notes-per-keyword",
        type=int,
        default=5,
        help="每个热搜词取前 K 条笔记链接（默认 5）",
    )
    parser.add_argument(
        "--no-headless",
        dest="headless",
        action="store_false",
        help="弹出浏览器窗口（调试用）",
    )
    parser.add_argument(
        "--profile-dir",
        default=str(_DEFAULT_PROFILE_DIR),
        help=f"XHS 浏览器 profile 目录（默认: {_DEFAULT_PROFILE_DIR}）",
    )
    parser.set_defaults(headless=True)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    ts = datetime.now()

    # ── Step 1: 抓热搜榜 ────────────────────────────────
    logger.info("Step 1/3: 从 rebang.today 抓取小红书热搜榜")
    try:
        all_items = fetch_hotsearch()
    except Exception as e:
        logger.error(f"抓取热搜榜失败: {e}", exc_info=True)
        return 1
    if not all_items:
        logger.error("热搜榜返回为空，终止")
        return 1
    logger.info(f"共拿到 {len(all_items)} 条热搜")

    txt_path = save_hotsearch_txt(all_items, _OUT_DIR, ts=ts)
    logger.info(f"热搜 TXT 已写入: {txt_path}")

    # ── Step 2: 搜笔记链接 ──────────────────────────────
    top_items = all_items[: args.top]
    keywords = [it["title"] for it in top_items]
    logger.info(
        f"Step 2/3: 在小红书官网搜索 Top {len(keywords)} 热搜，每词取 {args.notes_per_keyword} 条笔记"
    )
    profile_dir = Path(args.profile_dir)
    if not profile_dir.is_dir():
        logger.error(
            f"未找到 XHS 浏览器 profile: {profile_dir}\n"
            "首次使用请先执行扫码登录：\n"
            "  python -m crawler.xhs_hotpost.login\n"
            "登录后重跑本命令。"
        )
        return 2

    try:
        notes_map = search_notes_for_keywords(
            keywords,
            profile_dir=str(profile_dir),
            notes_per_keyword=args.notes_per_keyword,
            headless=args.headless,
        )
    except FileNotFoundError as e:
        logger.error(str(e))
        return 2
    except RuntimeError as e:
        logger.error(
            f"XHS 浏览器启动失败: {e}\n"
            "如提示 profile 失效，请重新扫码登录："
            "python -m crawler.xhs_hotpost.login"
        )
        return 2

    # ── Step 3: 写 Markdown ─────────────────────────────
    logger.info("Step 3/3: 生成 Markdown")
    md_text = _build_markdown(top_items, notes_map, ts, txt_path)
    md_path = txt_path.with_name(f"hotsearch_notes_{ts:%Y%m%d_%H%M}.md")
    md_path.write_text(md_text, encoding="utf-8")
    logger.info(f"Markdown 已写入: {md_path}")

    print("\n=== 完成 ===")
    print(f"热搜 TXT : {txt_path}")
    print(f"笔记 MD  : {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

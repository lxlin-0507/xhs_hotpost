"""
crawler/xhs_trend_proxy/run.py — 多 Profile 无登录热点采样入口（Phase 2）

用法示例：
    # 默认参数（headless，3 个 profile，每个 25 条笔记）
    python -m crawler.xhs_trend_proxy.run

    # 调试：有头浏览器，1 个 profile，每个 5 条，快速验证
    python -m crawler.xhs_trend_proxy.run --profiles 1 --notes-per-profile 5 --headed

    # 自定义输出目录
    python -m crawler.xhs_trend_proxy.run --output-dir /tmp/xhs_trend_proxy
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from typing import List

# 确保项目根目录在 sys.path
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config import Config
from crawler.xhs_trend_proxy.sampler import MultiProfileSampler
from xhs_content_trend.scorer import XhsTopicScorer, rows_to_tsv


def _setup_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# 默认输出目录（相对于本文件所在包目录）
_DEFAULT_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


def _save_notes_json(notes: List[dict], output_dir: str, ts: str) -> str:
    """将原始笔记列表保存为 JSON，返回文件路径。"""
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"notes_{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(notes, f, ensure_ascii=False, indent=2)
    return path


def _save_tsv(tsv_content: str, output_dir: str, ts: str) -> str:
    """将 TSV 榜 B 内容保存，返回文件路径。"""
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"topics_{ts}.tsv")
    with open(path, "w", encoding="utf-8") as f:
        f.write(tsv_content)
    return path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="多 Profile 无登录小红书热点采样器（Phase 2）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--profiles",
        type=int,
        default=Config.XHS_TREND_PROXY_PROFILE_COUNT,
        help="使用的 profile 数量",
    )
    parser.add_argument(
        "--notes-per-profile",
        type=int,
        default=Config.XHS_TREND_PROXY_NOTES_PER_PROFILE,
        help="每个 profile 采样的笔记数",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        default=not Config.XHS_TREND_PROXY_HEADLESS,
        help="使用有头浏览器（调试用）",
    )
    parser.add_argument(
        "--output-dir",
        default=_DEFAULT_OUTPUT_DIR,
        help="输出目录（默认 crawler/xhs_trend_proxy/output/）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="话题榜最大条目数",
    )
    parser.add_argument(
        "--min-evidence",
        type=int,
        default=Config.XHS_TREND_PROXY_MIN_EVIDENCE,
        help="话题进榜所需最少 profile 数（跨独立采样单元）",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="开启 DEBUG 日志",
    )
    args = parser.parse_args(argv)

    _setup_logging(args.debug)
    logger = logging.getLogger(__name__)

    headless = not args.headed
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    logger.info(
        f"[run] 开始采样 | profiles={args.profiles} "
        f"notes_per={args.notes_per_profile} headless={headless}"
    )

    # ── Step 1: 多 Profile 采样 ─────────────────────────────
    sampler = MultiProfileSampler(
        profile_root=Config.XHS_TREND_PROXY_PROFILE_ROOT,
        profile_count=args.profiles,
        notes_per_profile=args.notes_per_profile,
        headless=headless,
    )
    notes = sampler.sample_all()

    if not notes:
        logger.error("[run] 采样结果为空，退出")
        sys.exit(1)

    # ── Step 2: 保存原始笔记 JSON ───────────────────────────
    notes_path = _save_notes_json(notes, args.output_dir, ts)
    logger.info(f"[run] 原始笔记已保存 → {notes_path}（共 {len(notes)} 条）")

    # ── Step 3: 话题打分 ────────────────────────────────────
    scorer = XhsTopicScorer(limit=args.limit)
    rows = scorer.score(notes)

    if not rows:
        logger.warning("[run] 未从笔记中提取到任何话题")
    else:
        # 按 min_evidence 过滤（note_count 字段是命中笔记数，这里用 _profile_id 去重）
        if args.min_evidence > 1:
            topic_profiles: dict[str, set] = {}
            for note in notes:
                pid = note.get("_profile_id", "")
                for tag in note.get("tag_list") or []:
                    # tag_list 元素可能是字符串或 {"name": "..."}
                    name = tag if isinstance(tag, str) else (tag.get("name") or "")
                    if name:
                        topic_profiles.setdefault(name, set()).add(pid)
            rows = [r for r in rows if len(topic_profiles.get(r.topic, set())) >= args.min_evidence]
            logger.info(
                f"[run] min_evidence={args.min_evidence} 过滤后剩余 {len(rows)} 个话题"
            )

        # ── Step 4: 保存 TSV（榜 B）────────────────────────
        tsv_content = rows_to_tsv(rows)
        tsv_path = _save_tsv(tsv_content, args.output_dir, ts)
        logger.info(
            f"[run] 话题榜 TSV 已保存 → {tsv_path}（共 {len(rows)} 条话题）"
        )

        # 控制台简要预览
        print(f"\n--- 热点话题预览（top 10）---")
        for r in rows[:10]:
            flag = "🔥" if r.is_hot else "  "
            print(f"  {flag} #{r.rank:>3}  {r.topic:<30}  分={r.total_score:.1f}  笔记={r.note_count}")
        print()

    logger.info("[run] Phase 2 采样任务完成")


if __name__ == "__main__":
    main()

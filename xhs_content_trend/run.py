"""
Run no-login XHS content sampling and hot-term inference.

Usage:
  ./venv/bin/python -m xhs_content_trend.run
  ./venv/bin/python -m xhs_content_trend.run --from-json output/xhs_hotpost/20260527/xhs_hotpost_20260527_1024.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import Config  # noqa: E402
from logger import get_logger  # noqa: E402
from xhs_content_trend.sampler import XhsNoAuthSampler  # noqa: E402
from xhs_content_trend.scorer import XhsTopicScorer, rows_to_tsv  # noqa: E402

logger = get_logger("xhs_content_trend")


def _load_json(path: str) -> List[Dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("sample JSON must be a list of note objects")
    return data


def _save_json(path: str, data: List[Dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main() -> str:
    parser = argparse.ArgumentParser(description="No-login XHS content trend inference")
    parser.add_argument("--max-notes", type=int, default=80, help="sample note count target")
    parser.add_argument("--comments-per-note", type=int, default=5, help="comments to keep per note")
    parser.add_argument("--limit", type=int, default=50, help="hot terms to output")
    parser.add_argument("--comment-timeout", type=float, default=8.0, help="seconds to wait for each comment response")
    parser.add_argument("--profile-dir", default=Config.XHS_HOTPOST_NOAUTH_PROFILE, help="persistent visitor profile dir")
    parser.add_argument("--output-dir", default=str(_PROJECT_ROOT / "xhs_content_trend" / "output"), help="output directory")
    parser.add_argument("--from-json", default="", help="score an existing sample JSON instead of sampling")
    parser.add_argument("--headed", action="store_true", help="run browser with visible UI")
    args = parser.parse_args()

    started = datetime.now()
    day = started.strftime("%Y%m%d")
    slot = started.strftime("%H%M")

    out_dir = Path(args.output_dir) / day
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.from_json:
        logger.info(f"读取已有采样 JSON: {args.from_json}")
        notes = _load_json(args.from_json)
    else:
        logger.info(
            "开始无登录站内采样: max_notes=%s comments_per_note=%s profile=%s",
            args.max_notes,
            args.comments_per_note,
            args.profile_dir,
        )
        sampler = XhsNoAuthSampler(
            profile_dir=args.profile_dir,
            max_notes=args.max_notes,
            comments_per_note=args.comments_per_note,
            headless=not args.headed,
            comment_timeout=args.comment_timeout,
        )
        notes = sampler.sample()
        sample_path = out_dir / f"xhs_sample_{day}_{slot}.json"
        _save_json(str(sample_path), notes)
        logger.info(f"采样原始数据已保存: {sample_path}")

    scorer = XhsTopicScorer(limit=args.limit)
    rows = scorer.score(notes)
    if not rows:
        raise RuntimeError("no topics extracted from sampled notes")

    txt_path = out_dir / f"xhs_topic_trend_{day}_{slot}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(rows_to_tsv(rows))

    hot_count = sum(1 for r in rows if r.is_hot)
    logger.info(
        "话题热点推断完成: %s  sample_notes=%s topics=%s hot=%s",
        txt_path,
        len(notes),
        len(rows),
        hot_count,
    )
    return str(txt_path)


if __name__ == "__main__":
    try:
        print(main())
    except Exception as exc:
        logger.error(f"运行失败: {exc}", exc_info=True)
        sys.exit(1)


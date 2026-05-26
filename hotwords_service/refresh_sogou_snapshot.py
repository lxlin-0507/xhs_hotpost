"""
hotwords_service/refresh_sogou_snapshot.py — 刷新搜狗词库快照

用法：
  python3 refresh_sogou_snapshot.py --src /path/to/sogou_newwords [--days 7]

行为：
  扫描 {src}/YYYYMMDD/*.txt 最近 N 天的所有词
  → 长度过滤（2 ≤ len ≤ 12）+ 去重 + 按长度/字典序排序
  → 写到 hotwords_service/sogou_snapshot.txt（与 run.py 同目录）

run.py 启动时只会读 sogou_snapshot.txt，不再访问 sogou_newwords 目录，
所以新词进来后需要手动跑一次本脚本来刷新快照。
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
from pathlib import Path
from typing import Set

_HERE = Path(__file__).resolve().parent
SNAPSHOT_PATH = _HERE / "sogou_snapshot.txt"


def collect_words(src_dir: str, max_recent_days: int) -> Set[str]:
    if not os.path.isdir(src_dir):
        sys.exit(f"✗ 源目录不存在: {src_dir}")
    day_dirs = sorted(
        d for d in os.listdir(src_dir)
        if d.isdigit() and len(d) == 8 and
        os.path.isdir(os.path.join(src_dir, d))
    )
    if not day_dirs:
        sys.exit(f"✗ {src_dir} 下没有 YYYYMMDD 目录")
    day_dirs = day_dirs[-max_recent_days:]
    words: Set[str] = set()
    file_count = 0
    for d in day_dirs:
        for fp in glob.glob(os.path.join(src_dir, d, "*.txt")):
            file_count += 1
            try:
                with open(fp, encoding="utf-8") as f:
                    for line in f:
                        w = line.split("\t", 1)[0].strip()
                        if 2 <= len(w) <= 12 and not w.startswith("#"):
                            words.add(w)
            except Exception as exc:
                print(f"  ⚠ 跳过 {fp}: {exc}", file=sys.stderr)
    print(f"扫描日期: {day_dirs[0]} ~ {day_dirs[-1]}（{len(day_dirs)} 天，{file_count} 文件）")
    return words


def write_snapshot(words: Set[str]) -> None:
    sorted_words = sorted(words, key=lambda w: (len(w), w))
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        f.write(f"# 搜狗词库快照（由 refresh_sogou_snapshot.py 生成）\n")
        f.write(f"# 词数: {len(sorted_words)}\n")
        for w in sorted_words:
            f.write(w + "\n")
    size_kb = SNAPSHOT_PATH.stat().st_size / 1024
    print(f"✓ 写入 {SNAPSHOT_PATH}  ({len(sorted_words)} 词, {size_kb:.0f} KB)")


def main() -> None:
    parser = argparse.ArgumentParser(description="刷新搜狗词库快照")
    parser.add_argument(
        "--src", required=True,
        help="搜狗源目录（即 {base_dir}/sogou_newwords/）",
    )
    parser.add_argument(
        "--days", type=int, default=7,
        help="保留最近 N 天的词（默认 7）",
    )
    args = parser.parse_args()
    words = collect_words(args.src, args.days)
    write_snapshot(words)


if __name__ == "__main__":
    main()

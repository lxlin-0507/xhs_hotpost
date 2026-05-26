"""
hotwords_service/compare_segmenters.py — A/B 对比报告生成器

用法：
  # 先用 --segmenter both 生成两组 rank 文件
  python3 run.py --once --base-dir ../output --out-dir ../output/rank --segmenter both
  # → 生成 ../output/rank_hanlp/<date>/rank_*.csv
  # → 生成 ../output/rank_api/<date>/rank_*.csv

  # 再运行本脚本对比
  python3 compare_segmenters.py \
      --hanlp ../output/rank_hanlp/20260521/rank_20260521_120000.csv \
      --api   ../output/rank_api/20260521/rank_20260521_120001.csv

  # 也可以对比任意两个 rank 文件（不限于 hanlp vs api）
  python3 compare_segmenters.py --hanlp rank_a.csv --api rank_b.csv --top 100

输出：
  - 控制台打印报告摘要
  - 同目录下写出 compare_<timestamp>.txt（完整报告）
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# ─────────────────────────────────────────────────────────
#  数据结构
# ─────────────────────────────────────────────────────────

class RankRow:
    __slots__ = ("word", "source", "orig_term", "single_norm", "all_norm", "heat_tier")

    def __init__(self, word, source, orig_term, single_norm, all_norm, heat_tier):
        self.word       = word
        self.source     = source
        self.orig_term  = orig_term
        self.single_norm = float(single_norm) if single_norm else 0.0
        self.all_norm   = float(all_norm) if all_norm else 0.0
        self.heat_tier  = heat_tier


def load_rank(path: str) -> List[RankRow]:
    rows = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for r in reader:
            rows.append(RankRow(
                word       = r.get("word", "").strip(),
                source     = r.get("source", ""),
                orig_term  = r.get("orig_term", ""),
                single_norm= r.get("single_normalized", "0"),
                all_norm   = r.get("all_normalized", "0"),
                heat_tier  = r.get("heat_tier", ""),
            ))
    # 按 all_norm 降序（原文件已排序，但重新排一遍保险）
    rows.sort(key=lambda x: x.all_norm, reverse=True)
    return rows


# ─────────────────────────────────────────────────────────
#  指标计算
# ─────────────────────────────────────────────────────────

def top_words(rows: List[RankRow], n: int) -> List[str]:
    seen, result = set(), []
    for r in rows:
        if r.word not in seen:
            seen.add(r.word)
            result.append(r.word)
        if len(result) >= n:
            break
    return result


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def spearman(words_a: List[str], words_b: List[str]) -> Optional[float]:
    """
    共有词的 Spearman 秩相关。
    words_a / words_b 已按分数降序（rank = index + 1）。
    对共有词在各自列表中的位置重新编号（1..n），再套公式，
    确保结果在 [-1, 1] 范围内。
    """
    common = [w for w in words_a if w in set(words_b)]  # 保持 A 的顺序
    n = len(common)
    if n < 3:
        return None
    # 共有词在 B 中的位置顺序
    pos_b = {w: i for i, w in enumerate(words_b) if w in set(common)}
    # 重新编号：按 A 顺序得 rank_a=1..n，按 B 中位置排序得 rank_b=1..n
    rank_a = {w: i + 1 for i, w in enumerate(common)}
    sorted_by_b = sorted(common, key=lambda w: pos_b[w])
    rank_b = {w: i + 1 for i, w in enumerate(sorted_by_b)}
    d2 = sum((rank_a[w] - rank_b[w]) ** 2 for w in common)
    rho = 1 - 6 * d2 / (n * (n * n - 1))
    return rho


def tier_dist(rows: List[RankRow]) -> Dict[str, int]:
    dist: Dict[str, int] = defaultdict(int)
    seen = set()
    for r in rows:
        if r.word not in seen:
            seen.add(r.word)
            dist[r.heat_tier] += 1
    return dict(dist)


def source_dist(rows: List[RankRow]) -> Dict[str, int]:
    dist: Dict[str, int] = defaultdict(int)
    for r in rows:
        dist[r.source] += 1
    return dict(dist)


def avg_token_len(rows: List[RankRow]) -> float:
    words = list({r.word for r in rows})
    if not words:
        return 0.0
    return sum(len(w) for w in words) / len(words)


# ─────────────────────────────────────────────────────────
#  Token 自动质量评分
# ─────────────────────────────────────────────────────────

# 功能字结尾：这些字收尾的 token 大概率是被截断的语法碎片
_FUNC_ENDINGS = set("的了着过地得们嘛呢吧啊哦嗯哈吗呀喔哟")
# 功能字开头：同理
_FUNC_STARTS  = set("和与或但而且也都还就是被让把对从在")
# 判断是否是汉字
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")


def _load_lexicon(path: str) -> Set[str]:
    """加载词库文件（每行第一个 tab 前为词）。空行/注释行忽略。"""
    words: Set[str] = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                words.add(line.split("\t")[0].split()[0])
    except FileNotFoundError:
        pass
    return words


def is_boundary_aligned(token: str, orig_term: str) -> bool:
    """
    token 在 orig_term 中是否出现在自然词边界处。
    规则：token 前一字符 或 后一字符 不是汉字，则认为是边界对齐的。
    例："汉坦病毒" 在 "汉坦病毒潜伏期" 里前边界 OK，后紧接汉字 "潜" → 未对齐。
         "病毒" 在 "汉坦病毒" 里前紧接汉字 "汉" → 未对齐（是碎片）。
    """
    idx = orig_term.find(token)
    if idx == -1:
        return True  # 找不到时无法判断，不惩罚
    before_ok = (idx == 0) or not _HAN_RE.match(orig_term[idx - 1])
    end = idx + len(token)
    after_ok  = (end >= len(orig_term)) or not _HAN_RE.match(orig_term[end])
    return before_ok and after_ok


class TokenScorer:
    """
    对单个 token 给出 3 个信号，综合得出 good / warn / bad。

    信号：
      lexicon   — 在 sogou_snapshot 或 user_dict 里（已知真实词，精度高）
      fragment  — 以功能字开头或结尾（大概率是切割碎片）
      boundary  — 在 orig_term 里出现在自然词边界处（边界对齐 = 好）

    评级：
      good  — lexicon=True  且 fragment=False
      warn  — lexicon=False 且 fragment=False 且 boundary=True（未知但结构OK）
      bad   — fragment=True 或 boundary=False（结构异常）
    """

    def __init__(self, sogou_path: str = "", user_dict_path: str = ""):
        self.lexicon: Set[str] = set()
        if sogou_path:
            self.lexicon |= _load_lexicon(sogou_path)
        if user_dict_path:
            self.lexicon |= _load_lexicon(user_dict_path)

    def score(self, token: str, orig_term: str = "") -> Dict:
        lex  = token in self.lexicon
        frag = (token[0] in _FUNC_STARTS) or (token[-1] in _FUNC_ENDINGS)
        bdry = is_boundary_aligned(token, orig_term) if orig_term else True
        if lex and not frag:
            grade = "good"
        elif frag or not bdry:
            grade = "bad"
        else:
            grade = "warn"
        return {"lexicon": lex, "fragment": frag, "boundary": bdry, "grade": grade}

    def batch_precision(self, rows: List[RankRow]) -> Dict[str, float]:
        """对一批 RankRow 统计三项指标的宏平均，返回 0–1 的比率。"""
        seen: Set[str] = set()
        n = lex = frag = bdry = 0
        for r in rows:
            if r.word in seen:
                continue
            seen.add(r.word)
            s = self.score(r.word, r.orig_term)
            n    += 1
            lex  += int(s["lexicon"])
            frag += int(s["fragment"])
            bdry += int(s["boundary"])
        if n == 0:
            return {"lexicon_rate": 0.0, "fragment_rate": 0.0, "boundary_rate": 0.0}
        return {
            "lexicon_rate":  lex  / n,
            "fragment_rate": frag / n,
            "boundary_rate": bdry / n,
        }


# ─────────────────────────────────────────────────────────
#  格式化
# ─────────────────────────────────────────────────────────

def fmt_section(title: str) -> str:
    return f"\n{'─' * 60}\n  {title}\n{'─' * 60}"


def fmt_table(rows: List[Tuple], headers: List[str]) -> str:
    col_w = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
    def fmt_row(r):
        return "  " + "  ".join(str(r[i]).ljust(col_w[i]) for i in range(len(r)))
    lines = [fmt_row(headers), "  " + "  ".join("-" * w for w in col_w)]
    lines += [fmt_row(r) for r in rows]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────
#  主报告
# ─────────────────────────────────────────────────────────

def compare(
    path_a: str,
    path_b: str,
    label_a: str = "HanLP",
    label_b: str = "API",
    top_n: int = 50,
    sogou_path: str = "",
    user_dict_path: str = "",
) -> str:
    rows_a = load_rank(path_a)
    rows_b = load_rank(path_b)
    scorer = TokenScorer(sogou_path, user_dict_path)

    words_a_all = list(dict.fromkeys(r.word for r in rows_a))
    words_b_all = list(dict.fromkeys(r.word for r in rows_b))
    set_a = set(words_a_all)
    set_b = set(words_b_all)

    top_a = top_words(rows_a, top_n)
    top_b = top_words(rows_b, top_n)
    set_top_a = set(top_a)
    set_top_b = set(top_b)

    lines = []
    lines.append(f"切词器 A/B 对比报告  {datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append(f"  A ({label_a}): {path_a}")
    lines.append(f"  B ({label_b}): {path_b}")

    # ── §1 总量 ─────────────────────────────────────────
    lines.append(fmt_section("§1  总量"))
    lines.append(fmt_table(
        [
            (label_a, len(rows_a), len(words_a_all), f"{avg_token_len(rows_a):.2f}"),
            (label_b, len(rows_b), len(words_b_all), f"{avg_token_len(rows_b):.2f}"),
        ],
        ["实现", "总行数", "唯一词数", "平均词长"],
    ))

    # ── §2 Top-N 重叠 ────────────────────────────────────
    common_top  = set_top_a & set_top_b
    only_a_top  = set_top_a - set_top_b
    only_b_top  = set_top_b - set_top_a
    jac_top     = jaccard(set_top_a, set_top_b)
    rho         = spearman(top_a, top_b)

    lines.append(fmt_section(f"§2  Top-{top_n} 重叠"))
    lines.append(fmt_table(
        [
            ("共有词数",           len(common_top)),
            (f"仅 {label_a} 有",   len(only_a_top)),
            (f"仅 {label_b} 有",   len(only_b_top)),
            ("Jaccard 相似度",     f"{jac_top:.3f}"),
            ("Spearman 秩相关",    f"{rho:.3f}" if rho is not None else "n/a（共有词 < 3）"),
        ],
        ["指标", "值"],
    ))

    # ── §3 热度分级分布 ──────────────────────────────────
    tier_a = tier_dist(rows_a)
    tier_b = tier_dist(rows_b)
    lines.append(fmt_section("§3  热度分级分布（唯一词）"))
    tier_rows = []
    for t in sorted(set(list(tier_a.keys()) + list(tier_b.keys()))):
        tier_rows.append((f"tier-{t}", tier_a.get(t, 0), tier_b.get(t, 0)))
    lines.append(fmt_table(tier_rows, ["档位", label_a, label_b]))

    # ── §4 来源分布 ──────────────────────────────────────
    src_a = source_dist(rows_a)
    src_b = source_dist(rows_b)
    all_srcs = sorted(set(list(src_a.keys()) + list(src_b.keys())))
    lines.append(fmt_section("§4  来源分布（行数）"))
    lines.append(fmt_table(
        [(s, src_a.get(s, 0), src_b.get(s, 0)) for s in all_srcs],
        ["来源", label_a, label_b],
    ))

    # ── §5 仅 A 有的 Top-N 词（附质量信号）─────────────────
    lines.append(fmt_section(
        f"§5  仅 {label_a} 进 Top-{top_n} 的词  "
        f"（grade: good=已知词 / warn=结构OK未知 / bad=碎片）"
    ))
    orig_map_a = {r.word: r.orig_term for r in rows_a}
    only_a_scored = [
        (w, orig_map_a.get(w, ""), scorer.score(w, orig_map_a.get(w, ""))["grade"])
        for w in top_a if w in only_a_top
    ][:30]
    if only_a_scored:
        lines.append(fmt_table(only_a_scored, ["词", "来源标题（orig_term）", "grade"]))
    else:
        lines.append("  （无）")

    # ── §6 仅 B 有的 Top-N 词 ────────────────────────────
    lines.append(fmt_section(
        f"§6  仅 {label_b} 进 Top-{top_n} 的词  "
        f"（grade: good=已知词 / warn=结构OK未知 / bad=碎片）"
    ))
    orig_map_b = {r.word: r.orig_term for r in rows_b}
    only_b_scored = [
        (w, orig_map_b.get(w, ""), scorer.score(w, orig_map_b.get(w, ""))["grade"])
        for w in top_b if w in only_b_top
    ][:30]
    if only_b_scored:
        lines.append(fmt_table(only_b_scored, ["词", "来源标题（orig_term）", "grade"]))
    else:
        lines.append("  （无）")

    # ── §7 共有词排名变化最大的 20 个 ───────────────────────
    lines.append(fmt_section("§7  共有词排名变化最大的 20 个（|rank_A - rank_B| 降序）"))
    rank_a_map = {w: i + 1 for i, w in enumerate(top_a)}
    rank_b_map = {w: i + 1 for i, w in enumerate(top_b)}
    diffs = sorted(
        [(w, rank_a_map[w], rank_b_map[w], abs(rank_a_map[w] - rank_b_map[w]))
         for w in common_top],
        key=lambda x: -x[3],
    )[:20]
    if diffs:
        lines.append(fmt_table(
            [(w, ra, rb, f"↑{d}" if ra > rb else f"↓{d}") for w, ra, rb, d in diffs],
            ["词", f"{label_a} 排名", f"{label_b} 排名", "变化"],
        ))
    else:
        lines.append("  （共有词不足）")

    # ── §9 Token 自动质量评分 ─────────────────────────────
    lines.append(fmt_section("§9  Token 自动质量评分（基于词库覆盖 + 碎片检测 + 边界对齐）"))
    if scorer.lexicon:
        prec_a = scorer.batch_precision(rows_a)
        prec_b = scorer.batch_precision(rows_b)
        # 独有词的碎片率（最有区分价值）
        only_a_rows_obj = [r for r in rows_a if r.word in only_a_top]
        only_b_rows_obj = [r for r in rows_b if r.word in only_b_top]
        prec_only_a = scorer.batch_precision(only_a_rows_obj) if only_a_rows_obj else {}
        prec_only_b = scorer.batch_precision(only_b_rows_obj) if only_b_rows_obj else {}

        def pct(v): return f"{v*100:.1f}%"
        lines.append(fmt_table(
            [
                ("词库覆盖率（越高越好）",  pct(prec_a["lexicon_rate"]),  pct(prec_b["lexicon_rate"])),
                ("碎片率（越低越好）",      pct(prec_a["fragment_rate"]), pct(prec_b["fragment_rate"])),
                ("边界对齐率（越高越好）",  pct(prec_a["boundary_rate"]), pct(prec_b["boundary_rate"])),
            ],
            ["指标（全部词）", label_a, label_b],
        ))
        if prec_only_a and prec_only_b:
            lines.append("\n  独有词质量（最能体现两者差异的部分）：")
            lines.append(fmt_table(
                [
                    ("词库覆盖率", pct(prec_only_a.get("lexicon_rate", 0)),  pct(prec_only_b.get("lexicon_rate", 0))),
                    ("碎片率",     pct(prec_only_a.get("fragment_rate", 0)), pct(prec_only_b.get("fragment_rate", 0))),
                    ("边界对齐率", pct(prec_only_a.get("boundary_rate", 0)), pct(prec_only_b.get("boundary_rate", 0))),
                ],
                [f"指标（独有Top-{top_n}词）", f"仅{label_a}有", f"仅{label_b}有"],
            ))
        # 自动质量结论
        lex_diff = prec_a["lexicon_rate"] - prec_b["lexicon_rate"]
        frag_diff = prec_b["fragment_rate"] - prec_a["fragment_rate"]
        if lex_diff > 0.05 or frag_diff > 0.05:
            winner = label_a
            lines.append(f"\n  → 质量信号：{label_a} 词库覆盖更高或碎片更少，切词精度可能更优。")
        elif lex_diff < -0.05 or frag_diff < -0.05:
            winner = label_b
            lines.append(f"\n  → 质量信号：{label_b} 词库覆盖更高或碎片更少，切词精度可能更优。")
        else:
            lines.append(f"\n  → 质量信号：两者切词精度相近（差异 < 5%），其他指标为主要判断依据。")
    else:
        lines.append("  （未传入词库路径，跳过质量评分；用 --dict-dir 指定 hotwords_service/ 目录）")

    # ── §8 结论摘要 ──────────────────────────────────────
    lines.append(fmt_section("§8  结论摘要"))
    verdict = []
    if jac_top >= 0.7:
        verdict.append(f"✓ Top-{top_n} 重叠高（Jaccard={jac_top:.2f}），两种切词器结果接近，可替换。")
    elif jac_top >= 0.4:
        verdict.append(f"△ Top-{top_n} 重叠中等（Jaccard={jac_top:.2f}），有差异，需人工核查 §5/§6。")
    else:
        verdict.append(f"✗ Top-{top_n} 重叠低（Jaccard={jac_top:.2f}），切词差异显著，建议人工逐条比对。")
    if rho is not None:
        if rho >= 0.8:
            verdict.append(f"✓ 秩相关高（ρ={rho:.2f}），共有词排序基本一致。")
        elif rho >= 0.5:
            verdict.append(f"△ 秩相关中（ρ={rho:.2f}），部分词排名有明显差异（见 §7）。")
        else:
            verdict.append(f"✗ 秩相关低（ρ={rho:.2f}），共有词排序差异大。")
    if scorer.lexicon:
        prec_a_all = scorer.batch_precision(rows_a)
        prec_b_all = scorer.batch_precision(rows_b)
        if prec_a_all["fragment_rate"] < prec_b_all["fragment_rate"] - 0.05:
            verdict.append(f"✓ {label_a} 碎片率更低（{prec_a_all['fragment_rate']*100:.1f}% vs {prec_b_all['fragment_rate']*100:.1f}%），切词更干净。")
        elif prec_b_all["fragment_rate"] < prec_a_all["fragment_rate"] - 0.05:
            verdict.append(f"✓ {label_b} 碎片率更低（{prec_b_all['fragment_rate']*100:.1f}% vs {prec_a_all['fragment_rate']*100:.1f}%），切词更干净。")
    for v in verdict:
        lines.append(f"  {v}")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="切词器 A/B 对比报告")
    parser.add_argument("--hanlp", required=True, help="HanLP rank CSV 路径")
    parser.add_argument("--api",   required=True, help="API rank CSV 路径")
    parser.add_argument("--label-a", default="HanLP", help="A 方标签（默认 HanLP）")
    parser.add_argument("--label-b", default="API",   help="B 方标签（默认 API）")
    parser.add_argument("--top",   type=int, default=50, help="对比 Top-N（默认 50）")
    parser.add_argument("--out",   default=None, help="报告输出路径（默认 compare_<ts>.txt）")
    parser.add_argument(
        "--dict-dir", default=None,
        help="hotwords_service/ 目录路径，用于加载 sogou_snapshot.txt / user_dict.txt 做质量评分；"
             "默认自动探测（脚本同目录）"
    )
    args = parser.parse_args()

    # 自动探测词库路径
    dict_dir = Path(args.dict_dir) if args.dict_dir else Path(__file__).resolve().parent
    sogou_path    = str(dict_dir / "sogou_snapshot.txt")
    user_dict_path= str(dict_dir / "user_dict.txt")

    report = compare(
        args.hanlp, args.api, args.label_a, args.label_b, args.top,
        sogou_path=sogou_path, user_dict_path=user_dict_path,
    )
    print(report)

    out_path = args.out or f"compare_{datetime.now():%Y%m%d_%H%M%S}.txt"
    Path(out_path).write_text(report, encoding="utf-8")
    print(f"\n报告已写入: {out_path}")


if __name__ == "__main__":
    main()

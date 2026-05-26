"""
pipeline/normalize.py — 热度归一化模块

算法（来自模型文档 §2.1）:
  Step 1  平台内 Min-Max 归一化（ε 平滑）
            S_norm = ε + (1-ε) × (val - val_min) / (val_max - val_min)
            ε = 0.05

  Step 2  无热度值来源 → 指数衰减（按排名生成）
            S_norm(k) = e^(-λ × (k-1)),  λ = 0.5

  Step 3  乘以平台权重
            final = S_norm × platform_weight

  Step 4  跨平台共现加成
            bonus  = 1 + 0.3 × ln(n_platforms)
            merged = final × bonus

  Step 5  总榜单归一化（合并去重后，对 merged 再做 Min-Max + ε）
            total_norm = ε + (1-ε) × (merged - merged_min) / (merged_max - merged_min)
            使全网总榜分数落在 [0.05, 1.0]，便于对外展示与下游统一尺度

支持的数据源:
  douyin_hotlist   txt 三列 (词条\t拼音\t热度整数)   — Step 1
  xhs_hot          txt 三列 (词条\t拼音\t热度整数)   — Step 1
  xhs_hotpost      JSON  (source_keyword / keyword_heat) — Step 1
  douyin_hotwords  JSON (title / score / rank / pinyin)  — Step 1
  weibo_hotsearch  txt 三列 (词条\t拼音\t热度整数)   — Step 1

定时任务策略:
  - 各爬虫写出的原始文件保持原始词频/热度值，不做原地覆盖。
  - 每次任何热榜任务完成后，调用 generate_unified_leaderboard() 将所有来源
    的最新文件汇总计算，单独写出一份归一化总榜文件。
"""
from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional

# ── 超参数 ─────────────────────────────────────────────────
EPS     = 0.05   # Min-Max 归一化下界
LAMBDA  = 0.5    # 指数衰减系数（仅限无热度值来源）
BONUS_K = 0.3    # 跨平台加成系数

# ── 平台权重（参照模型文档 §2.1 Step3）────────────────────
# 抖音0.35 / 微博0.25 / 百度0.25 / B站0.15；小红书按规模参照B站取0.15
PLATFORM_WEIGHTS: Dict[str, float] = {
    "douyin_hotlist":  0.35,
    "douyin_hotwords": 0.35,
    "weibo_hotsearch": 0.25,
    "xhs_hot":         0.15,
    "xhs_hotpost":     0.15,
}

# ── 总榜参与来源（顺序即处理顺序）────────────────────────
HOT_SOURCES: List[str] = [
    "weibo_hotsearch",
    "douyin_hotlist",
    "xhs_hot",
    "xhs_hotpost",
    "douyin_hotwords",
]

# ── 来源显示名称 ──────────────────────────────────────────
SOURCE_DISPLAY: Dict[str, str] = {
    "weibo_hotsearch": "微博热搜",
    "douyin_hotlist":  "抖音热榜",
    "xhs_hot":         "小红书热点",
    "xhs_hotpost":     "小红书热帖",
    "douyin_hotwords": "抖音热词",
}


# ────────────────────────────────────────────────────────────
#  数据结构
# ────────────────────────────────────────────────────────────

@dataclass
class HotItem:
    word:       str
    pinyin:     str
    source:     str
    raw_heat:   float    # 原始热度（0 表示无热度值，依赖 rank 降级）
    rank:       int      # 在本平台榜单中的排名（1-based）；总榜阶段会重排
    s_norm:     float = 0.0   # Step1/2 平台内归一化
    final:      float = 0.0   # Step3 乘权重后的值
    bonus:      float = 1.0   # Step4 跨平台加成倍数
    merged:     float = 0.0   # Step4 合并分数（同词多源相加前：final×bonus；合并后：各源 merged 之和）
    total_norm: float = 0.0   # Step5 总榜单归一化（对 merged 做 Min-Max+ε）


# ────────────────────────────────────────────────────────────
#  Step 1 — Min-Max 归一化（ε 平滑）
# ────────────────────────────────────────────────────────────

def normalize_minmax(items: List[HotItem], eps: float = EPS) -> List[HotItem]:
    """
    平台内 Min-Max 归一化。

    公式: S_norm = ε + (1-ε) × (val - val_min) / (val_max - val_min)
    当所有值相同时退化为 ε（保留基础热度，不丢弃任何词条）。
    """
    heats = [it.raw_heat for it in items]
    h_max, h_min = max(heats), min(heats)
    h_range = h_max - h_min
    for it in items:
        if h_range == 0:
            it.s_norm = eps
        else:
            ratio = (it.raw_heat - h_min) / h_range
            it.s_norm = eps + (1.0 - eps) * ratio
    return items


# ────────────────────────────────────────────────────────────
#  Step 2 — 指数衰减（仅限无热度值来源，按 rank）
# ────────────────────────────────────────────────────────────

def normalize_by_rank(items: List[HotItem], lam: float = LAMBDA) -> List[HotItem]:
    """
    对无热度值来源（如 B 站），按排名用指数衰减生成 S_norm。

    公式: S_norm(k) = e^(-λ × (k-1)),  k = rank（1-based → 0-based）
    rank=1 → S_norm=1.0，rank=2 → 0.607，rank=3 → 0.368 …
    """
    for it in items:
        k = it.rank - 1
        it.s_norm = math.exp(-lam * k)
    return items


# ────────────────────────────────────────────────────────────
#  Step 3 — 乘以平台权重
# ────────────────────────────────────────────────────────────

def apply_platform_weight(
    items: List[HotItem],
    weight: Optional[float] = None,
) -> List[HotItem]:
    """
    final = s_norm × platform_weight

    weight 参数优先；不传则从 PLATFORM_WEIGHTS 查表；找不到时默认 0.25。
    """
    for it in items:
        w = weight if weight is not None else PLATFORM_WEIGHTS.get(it.source, 0.25)
        it.final = it.s_norm * w
    return items


# ────────────────────────────────────────────────────────────
#  Step 4 — 跨平台共现加成
# ────────────────────────────────────────────────────────────

def apply_cross_platform_bonus(all_items: List[HotItem]) -> List[HotItem]:
    """
    统计每个词在几个平台上出现，计算 bonus 并写入 merged。

    bonus  = 1 + 0.3 × ln(n_platforms)   (n > 1)
    merged = final × bonus
    """
    word_sources: Dict[str, set] = defaultdict(set)
    for it in all_items:
        word_sources[it.word].add(it.source)

    for it in all_items:
        n = len(word_sources[it.word])
        it.bonus  = 1.0 + BONUS_K * math.log(n) if n > 1 else 1.0
        it.merged = it.final * it.bonus
    return all_items


# ────────────────────────────────────────────────────────────
#  数据读取
# ────────────────────────────────────────────────────────────

def _parse_heat_cell(s: str) -> float:
    """第三列：支持整数热度或已归一化的小数。"""
    s = (s or "").strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def read_txt_source(path: str, source: str) -> List[HotItem]:
    """
    读取 txt 三列格式：词条<TAB>拼音<TAB>热度整数

    适用于: douyin_hotlist / xhs_hot / weibo_hotsearch
    """
    items: List[HotItem] = []
    with open(path, encoding="utf-8") as f:
        for rank, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            word   = parts[0].strip()
            pinyin = parts[1].strip()
            heat   = _parse_heat_cell(parts[2]) if len(parts) >= 3 else 0.0
            if word:
                items.append(HotItem(word=word, pinyin=pinyin,
                                     source=source, raw_heat=heat, rank=rank))
    return items


def read_douyin_hotwords_json(path: str) -> List[HotItem]:
    """
    读取 douyin_hotwords JSON。

    字段: title(词条) / score(热度) / rank(排名) / pinyin
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    items: List[HotItem] = []
    for idx, entry in enumerate(data):
        word   = (entry.get("title") or "").strip()
        pinyin = (entry.get("pinyin") or "").strip()
        score  = float(entry.get("score") or 0)
        rank   = int(entry.get("rank") or (idx + 1))
        if word:
            items.append(HotItem(word=word, pinyin=pinyin,
                                 source="douyin_hotwords",
                                 raw_heat=score, rank=rank))
    return items


def _make_pinyin_xhs_style(word: str) -> str:
    """
    生成与 xhs_hot / 搜狗词库一致的拼音字符串：
      中文 → 反引号分隔音节，英文字母 → 空格分隔。
    与 crawler/xhs/spider.py::_make_pinyin 行为对齐。
    """
    try:
        from pypinyin import lazy_pinyin
    except Exception:
        return ""
    result = ""
    prev_is_en = False
    for ch in word:
        if "\u4e00" <= ch <= "\u9fff":
            py = lazy_pinyin(ch)
            if py and py[0].isascii() and py[0].isalpha():
                result += "`" + py[0].lower()
            prev_is_en = False
        elif ch.isascii() and ch.isalpha():
            if prev_is_en:
                result += " " + ch.lower()
            else:
                result += "`" + ch.lower()
            prev_is_en = True
        else:
            prev_is_en = False
    return result


def _parse_liked_count(v) -> float:
    """把 liked_count 字符串（'1943' / '1.2万' / '12w'）转成浮点数。"""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower().replace(",", "")
    try:
        if "万" in s or "w" in s:
            return float(s.replace("万", "").replace("w", "")) * 10000
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def read_xhs_hotpost_json(path: str) -> List[HotItem]:
    """
    读取 xhs_hotpost JSON。

    词条 = title（笔记标题）
    热度 = liked_count（帖子实际点赞数，来自搜索结果 note_card.interact_info）；
           若 liked_count 全为 0（旧文件），降级使用 keyword_heat。
    rank 按文件中笔记首次出现的顺序赋值（1-based）。

    同标题去重，热度取最大。
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    def _heat(entry: dict) -> float:
        lc = _parse_liked_count(entry.get("liked_count"))
        if lc > 0:
            return lc
        try:
            return float(entry.get("keyword_heat") or 0)
        except (TypeError, ValueError):
            return 0.0

    seen: Dict[str, HotItem] = {}
    order: List[str] = []
    for entry in data:
        word = (entry.get("title") or "").strip()
        if not word:
            continue
        heat = _heat(entry)
        if word not in seen:
            seen[word] = HotItem(
                word=word,
                pinyin=_make_pinyin_xhs_style(word),
                source="xhs_hotpost",
                raw_heat=heat,
                rank=len(order) + 1,
            )
            order.append(word)
        else:
            if heat > seen[word].raw_heat:
                seen[word].raw_heat = heat
    return [seen[w] for w in order]


# ────────────────────────────────────────────────────────────
#  单来源归一化入口
# ────────────────────────────────────────────────────────────

def normalize_source(items: List[HotItem]) -> List[HotItem]:
    """
    对单个来源做 Step1（或 Step2 降级）+ Step3。

    - 若 raw_heat 全为 0 → Step2 指数衰减
    - 否则             → Step1 Min-Max
    然后统一做 Step3 乘权重。
    """
    if not items:
        return items
    if any(it.raw_heat > 0 for it in items):
        normalize_minmax(items)
    else:
        normalize_by_rank(items)
    apply_platform_weight(items)
    return items


def process_file(path: str, source: str) -> List[HotItem]:
    """读文件 → 解析 → 归一化，返回 HotItem 列表。"""
    if source == "douyin_hotwords":
        items = read_douyin_hotwords_json(path)
    elif source == "xhs_hotpost":
        items = read_xhs_hotpost_json(path)
    else:
        items = read_txt_source(path, source)
    return normalize_source(items)


# ────────────────────────────────────────────────────────────
#  工具：查找最新文件
# ────────────────────────────────────────────────────────────

def find_latest_file(base_dir: str, source: str) -> Optional[str]:
    """
    在 base_dir/{source}/**/ 下找最新的数据文件（按文件名排序）。
    douyin_hotwords / xhs_hotpost → *.json，其余 → *.txt
    """
    ext = "*.json" if source in ("douyin_hotwords", "xhs_hotpost") else "*.txt"
    import glob
    pattern = os.path.join(base_dir, source, "**", ext)
    files = sorted(glob.glob(pattern, recursive=True))
    return files[-1] if files else None


# ────────────────────────────────────────────────────────────
#  多来源合并排名
# ────────────────────────────────────────────────────────────

def merge_and_rank(source_items: Dict[str, List[HotItem]]) -> List[HotItem]:
    """
    合并多个来源的归一化结果，应用 Step4 跨平台加成，按 merged 降序排名。

    Args:
        source_items: {source_name: [HotItem, ...]}

    Returns:
        按 merged 分数降序排列的 HotItem 列表（已去重同词条，取最高分保留）
    """
    all_items: List[HotItem] = []
    for items in source_items.values():
        all_items.extend(items)

    apply_cross_platform_bonus(all_items)

    # 同一词条跨来源合并：merged 分数相加
    word_map: Dict[str, HotItem] = {}
    for it in all_items:
        if it.word not in word_map:
            word_map[it.word] = HotItem(
                word=it.word, pinyin=it.pinyin,
                source=it.source, raw_heat=0,
                rank=0, merged=0.0,
            )
        word_map[it.word].merged += it.merged

    ranked = sorted(word_map.values(), key=lambda x: x.merged, reverse=True)
    for i, it in enumerate(ranked, 1):
        it.rank = i
    return ranked


# ────────────────────────────────────────────────────────────
#  Step 5 — 总榜单归一化（对合并后的 merged）
# ────────────────────────────────────────────────────────────

def apply_total_leaderboard_normalize(items: List[HotItem], eps: float = EPS) -> List[HotItem]:
    """
    对「已去重、merged 已聚合」的总榜列表，将 merged 映射到 [eps, 1.0]。

    公式: total_norm = ε + (1-ε) × (merged - merged_min) / (merged_max - merged_min)

    merged 越大 → total_norm 越接近 1；merged 为全榜最小 → total_norm = ε。
    merged 全相等时，全部置为 ε（与平台内 Min-Max 退化行为一致）。

    排序：merged 与 total_norm 单调一致，rank 仍按 merged 降序重编号（1 起）。
    """
    if not items:
        return items
    scores = [it.merged for it in items]
    hi, lo = max(scores), min(scores)
    rng = hi - lo
    for it in items:
        if rng <= 0:
            it.total_norm = eps
        else:
            it.total_norm = eps + (1.0 - eps) * (it.merged - lo) / rng
    ranked = sorted(items, key=lambda x: x.merged, reverse=True)
    for i, it in enumerate(ranked, 1):
        it.rank = i
    return ranked


def build_unified_leaderboard(
    source_items: Dict[str, List[HotItem]],
    eps: float = EPS,
) -> List[HotItem]:
    """
    Step1~4（各源已在 process_file 中完成）→ merge_and_rank → Step5 总榜归一化。

    Returns:
        每条为去重后的词条，含 merged（跨源加权和×共现加成后的聚合）与 total_norm（总榜尺度）。
    """
    merged = merge_and_rank(source_items)
    return apply_total_leaderboard_normalize(merged, eps)


def write_unified_leaderboard_tsv(path: str, items: List[HotItem]) -> None:
    """
    写出总榜 TSV：排名、词条、拼音、merged、total_norm、代表来源。

    表头: rank\\tword\\tpinyin\\tmerged\\ttotal_norm\\tsource
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("rank\tword\tpinyin\tmerged\ttotal_norm\tsource\n")
        for it in items:
            f.write(
                f"{it.rank}\t{it.word}\t{it.pinyin}\t{it.merged:.6f}\t"
                f"{it.total_norm:.6f}\t{it.source}\n"
            )


# ────────────────────────────────────────────────────────────
#  归一化总榜生成（读最新原始文件 → 计算 → 写独立汇总文件）
# ────────────────────────────────────────────────────────────

def generate_unified_leaderboard(
    base_dir: str,
    sources: Optional[List[str]] = None,
    out_dir: Optional[str] = None,
    slot: Optional[str] = None,
) -> Optional[str]:
    """
    读取各来源最新原始文件 → Step1~4 归一化 → 跨平台合并 → 写汇总总榜文件。

    原始文件不被修改；输出写到独立的 unified_leaderboard/ 子目录。

    参数:
        base_dir: 数据根目录（如 output/），各来源目录在其下。
        sources:  参与总榜的来源列表，默认使用 HOT_SOURCES。
        out_dir:  总榜文件输出目录，默认 base_dir/unified_leaderboard/{date}/。
        slot:     时间槽字符串（HHmm），默认取当前时间。

    Returns:
        写出的文件路径；若所有来源均无数据则返回 None。

    输出文件格式（UTF-8 TSV）:
        Section 1 — 全网总榜（合并去重，merged 相加后降序）
            排名 \\t 词条 \\t 全网热度 \\t 出现平台
        Section 2 — 各平台明细（每条保留来源/raw_heat/s_norm/final/bonus/merged）
            排名 \\t 词条 \\t 来源 \\t 原始热度 \\t S_norm \\t 平台得分 \\t 跨平台加成 \\t 最终得分
    """
    from datetime import datetime as _dt

    if sources is None:
        sources = HOT_SOURCES

    now = _dt.now()
    date = now.strftime("%Y%m%d")
    if slot is None:
        slot = now.strftime("%H%M")

    # Step1/2/3：各来源独立归一化（读原始文件，不覆盖）
    source_items: Dict[str, List[HotItem]] = {}
    for src in sources:
        path = find_latest_file(base_dir, src)
        if not path:
            continue
        items = process_file(path, src)
        if items:
            source_items[src] = items

    if not source_items:
        return None

    # Step4：跨平台共现加成，merged = final × bonus
    all_items: List[HotItem] = []
    for items in source_items.values():
        all_items.extend(items)
    apply_cross_platform_bonus(all_items)

    # Step3 排序（全平台明细，按 merged 降序）
    step3_sorted = sorted(all_items, key=lambda x: x.merged, reverse=True)

    # Step4 合并同词（merged 相加）
    merged_map: Dict[str, Dict] = {}
    for it in all_items:
        if it.word not in merged_map:
            merged_map[it.word] = {
                "word":    it.word,
                "pinyin":  it.pinyin,
                "merged":  0.0,
                "sources": [],
            }
        merged_map[it.word]["merged"] += it.merged
        src_label = SOURCE_DISPLAY.get(it.source, it.source)
        if src_label not in merged_map[it.word]["sources"]:
            merged_map[it.word]["sources"].append(src_label)

    step4_sorted = sorted(merged_map.values(), key=lambda x: x["merged"], reverse=True)

    # 写文件
    if out_dir is None:
        out_dir = os.path.join(base_dir, "unified_leaderboard", date)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"unified_hot_{date}_{slot}.txt")

    src_labels = [SOURCE_DISPLAY.get(s, s) for s in source_items]

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"# 全网热度总榜\n")
        f.write(f"# 生成时间: {now.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# 参与来源: {', '.join(src_labels)}\n")
        f.write("\n")

        # Section 1：全网总榜（合并去重）
        f.write("=== 全网总榜（合并去重）===\n")
        f.write("排名\t词条\t全网热度\t出现平台\n")
        for rank, entry in enumerate(step4_sorted, 1):
            src_str = ",".join(entry["sources"])
            f.write(f"{rank}\t{entry['word']}\t{entry['merged']:.4f}\t{src_str}\n")

        f.write("\n")

        # Section 2：各平台明细（含跨平台加成）
        f.write("=== 各平台明细（含跨平台加成）===\n")
        f.write("排名\t词条\t来源\t原始热度\tS_norm\t平台得分\t跨平台加成\t最终得分\n")
        for rank, it in enumerate(step3_sorted, 1):
            src_label = SOURCE_DISPLAY.get(it.source, it.source)
            f.write(
                f"{rank}\t{it.word}\t{src_label}\t{it.raw_heat:.0f}\t"
                f"{it.s_norm:.4f}\t{it.final:.4f}\t{it.bonus:.4f}\t{it.merged:.4f}\n"
            )

    return out_path


# ────────────────────────────────────────────────────────────
#  分词辅助
# ────────────────────────────────────────────────────────────

def _load_stopwords() -> frozenset:
    """
    从 pipeline/stopwords.txt 加载停用词表，合并内置单字符停用词。
    注释行（# 开头）和空行自动忽略。
    """
    # 内置单字符停用词（jieba 偶尔会切出单字）
    builtin = frozenset(
        "的了是在有被也都从等和与或但而及对向以为到着过地得"
        "吗啊呢吧嘛哦呀不没很已再就会将要可这那此该其某各每"
        "我你他她它中上下内外后前间来去回起看"
    )
    sw_path = os.path.join(os.path.dirname(__file__), "stopwords.txt")
    if not os.path.exists(sw_path):
        return builtin
    words: set = set(builtin)
    with open(sw_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                words.add(line)
    return frozenset(words)


# 模块级缓存，只加载一次
_STOP_WORDS: frozenset = _load_stopwords()


def _segment_tokens(text: str) -> List[str]:
    """
    用 jieba 对热搜标题分词，过滤停用词、单字符、纯 ASCII token。

    返回有意义的词语列表（去重、保序）。
    """
    try:
        import jieba  # type: ignore
    except ImportError:
        return [text] if len(text) >= 2 else []

    seen_tokens: List[str] = []
    seen_set: set = set()
    for tok in jieba.cut(text, cut_all=False):
        tok = tok.strip()
        if not tok:
            continue
        if len(tok) < 2:
            continue
        if tok in _STOP_WORDS:
            continue
        # 纯数字或纯 ASCII（保留含汉字的混合词如 "AI技术"）
        if tok.isdigit() or tok.isascii():
            continue
        if tok not in seen_set:
            seen_tokens.append(tok)
            seen_set.add(tok)
    return seen_tokens


# ────────────────────────────────────────────────────────────
#  全量历史归一化宽表（分词版）
# ────────────────────────────────────────────────────────────

def _slot_from_filename(filename: str) -> str:
    """从文件名末段提取时间槽，如 weibo_hotsearch_20260417_1046.txt → 1046"""
    stem = os.path.splitext(filename)[0]
    parts = stem.split("_")
    return parts[-1] if parts else ""


def build_full_history_table(
    base_dir: str,
    sources: Optional[List[str]] = None,
    out_path: Optional[str] = None,
) -> str:
    """
    全量历史热榜归一化宽表 — 按分词后的词语（token）组织。

    流程：
      1. 遍历所有日期，每个日期取各来源最新文件，完成 Step1~3 归一化。
      2. 用 jieba 对每条热搜标题分词，提取有效词语（token）。
      3. 以 token 为单位计算跨平台共现加成（Step4）：
           同一日期内，一个 token 在 N 个来源的任意标题中出现
           → bonus = 1 + 0.3 · ln(N)
      4. 每个（token × 来源 × 日期）一行；若同一来源同一日期多条标题含
         同一 token，取 final 最高的标题作为代表（orig_term）。

    排序：word 升序（主键）→ date 升序（次键）→ source_key（三键）。

    输出列（TSV）：
        word            分词后的词语（token）
        word_pinyin     token 拼音
        orig_term       包含该 token 的原始热搜标题
        source          来源中文显示名
        source_key      来源内部键
        date            日期 YYYYMMDD
        slot            时间槽（来自文件名）
        rank            orig_term 在来源榜单中的排名
        raw_heat        orig_term 的原始热度（无则为 0）
        s_norm          Step1/2 原始标题平台内归一化值
        platform_weight 平台权重
        final           Step3: s_norm × platform_weight
        n_platforms     该 token 当日出现的平台数
        bonus           Step4 跨平台加成系数
        merged          Step4: final × bonus

    Returns:
        写出 TSV 文件的路径。
    """
    import glob as _glob
    from datetime import datetime as _dt

    if sources is None:
        sources = HOT_SOURCES

    # ── 1. 收集所有日期 ──────────────────────────────────────
    all_dates: set = set()
    for src in sources:
        src_dir = os.path.join(base_dir, src)
        if os.path.isdir(src_dir):
            for d in os.listdir(src_dir):
                if d.isdigit() and len(d) == 8:
                    all_dates.add(d)

    all_rows: List[Dict] = []

    for date in sorted(all_dates):
        # ── 2. 读取各来源最新文件，完成 Step1~3 ────────────────
        source_items: Dict[str, List[HotItem]] = {}
        source_slot:  Dict[str, str] = {}

        for src in sources:
            day_dir = os.path.join(base_dir, src, date)
            if not os.path.isdir(day_dir):
                continue
            ext = "*.json" if src == "douyin_hotwords" else "*.txt"
            candidates = sorted(_glob.glob(os.path.join(day_dir, ext)))
            if not candidates:
                continue
            latest = candidates[-1]
            items = process_file(latest, src)
            if items:
                source_items[src] = items
                source_slot[src] = _slot_from_filename(os.path.basename(latest))

        if not source_items:
            continue

        # ── 3. 分词：建立 token → {source: best_HotItem} 映射 ──
        # token_src_best[token][source] = 该来源 final 最高的 HotItem
        token_src_best: Dict[str, Dict[str, HotItem]] = defaultdict(dict)

        for src, items in source_items.items():
            for it in items:
                for tok in _segment_tokens(it.word):
                    prev = token_src_best[tok].get(src)
                    if prev is None or it.final > prev.final:
                        token_src_best[tok][src] = it

        if not token_src_best:
            continue

        # ── 4. 跨平台 bonus（token 级别，Step4）─────────────────
        try:
            from pypinyin import lazy_pinyin  # type: ignore
            _has_pypinyin = True
        except ImportError:
            _has_pypinyin = False

        for tok, src_map in token_src_best.items():
            n = len(src_map)
            bonus = 1.0 + BONUS_K * math.log(n) if n > 1 else 1.0

            tok_pinyin = " ".join(lazy_pinyin(tok)) if _has_pypinyin else ""

            for src, best_it in src_map.items():
                w = PLATFORM_WEIGHTS.get(src, 0.25)
                slot = source_slot.get(src, "")

                all_rows.append({
                    "word":            tok,
                    "word_pinyin":     tok_pinyin,
                    "orig_term":       best_it.word,
                    "source":          SOURCE_DISPLAY.get(src, src),
                    "source_key":      src,
                    "date":            date,
                    "slot":            slot,
                    "rank":            best_it.rank,
                    "raw_heat":        best_it.raw_heat,
                    "s_norm":          round(best_it.s_norm,  6),
                    "platform_weight": w,
                    "final":           round(best_it.final,   6),
                    "n_platforms":     n,
                    "bonus":           round(bonus,           6),
                    "merged":          round(best_it.final * bonus, 6),
                })

    # ── 5. 排序：word → date → source_key ────────────────────
    all_rows.sort(key=lambda r: (r["word"], r["date"], r["source_key"]))

    # ── 6. 写 TSV ────────────────────────────────────────────
    if out_path is None:
        ts = _dt.now().strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(base_dir, f"hot_normalized_full_{ts}.tsv")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    HEADERS = [
        "word", "word_pinyin", "orig_term",
        "source", "source_key", "date", "slot",
        "rank", "raw_heat", "s_norm", "platform_weight", "final",
        "n_platforms", "bonus", "merged",
    ]

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\t".join(HEADERS) + "\n")
        for row in all_rows:
            f.write("\t".join(str(row[h]) for h in HEADERS) + "\n")

    return out_path


# ────────────────────────────────────────────────────────────
#  CLI 入口（python -m pipeline.normalize）
# ────────────────────────────────────────────────────────────

def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="热度归一化 & 跨平台排名")
    parser.add_argument("--base-dir", default="output",
                        help="数据根目录（默认 output/）")
    parser.add_argument("--sources", nargs="+",
                        default=["douyin_hotlist", "xhs_hot", "douyin_hotwords"],
                        help="要处理的来源列表")
    parser.add_argument("--top", type=int, default=30,
                        help="输出前 N 条（默认 30）")
    parser.add_argument("--no-merge", action="store_true",
                        help="只打印各来源归一化结果，不做跨平台合并")
    parser.add_argument("--out-tsv", default="",
                        help="总榜写入 TSV 路径（含 Step5 total_norm 列）")
    parser.add_argument("--full-table", action="store_true",
                        help="生成全量历史归一化宽表（所有日期 × 所有来源）")
    parser.add_argument("--out", default="",
                        help="--full-table 输出文件路径（默认自动命名）")
    args = parser.parse_args()

    # ── 全量历史宽表模式 ──────────────────────────────────────
    if args.full_table:
        sources = args.sources if args.sources != ["douyin_hotlist", "xhs_hot", "douyin_hotwords"] else None
        out = args.out or None
        print("正在生成全量历史归一化宽表...")
        path = build_full_history_table(args.base_dir, sources=sources, out_path=out)
        import csv
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="\t")
            rows = list(reader)
        print(f"完成：共 {len(rows)} 行，已写入 {path}")
        return

    source_items: Dict[str, List[HotItem]] = {}

    for source in args.sources:
        path = find_latest_file(args.base_dir, source)
        if not path:
            print(f"[WARN] {source}: 未找到数据文件（base_dir={args.base_dir}）")
            continue
        items = process_file(path, source)
        source_items[source] = items
        print(f"\n{'─'*60}")
        print(f"  {source}  ({os.path.basename(path)})  共 {len(items)} 条")
        print(f"{'─'*60}")
        print(f"  {'排名':>4}  {'词条':<25}  {'raw_heat':>12}  {'s_norm':>7}  {'final':>7}")
        for it in items[:10]:
            print(f"  {it.rank:>4}  {it.word:<25}  {it.raw_heat:>12.0f}  "
                  f"{it.s_norm:>7.4f}  {it.final:>7.4f}")
        if len(items) > 10:
            print(f"  ... 共 {len(items)} 条")

    if args.no_merge or len(source_items) < 2:
        return

    print(f"\n{'═'*60}")
    print(f"  跨平台合并榜 + 总榜归一化（Top {args.top}）")
    print(f"{'═'*60}")
    print(f"  {'排名':>4}  {'词条':<26}  {'merged':>8}  {'total_norm':>10}  来源")
    unified = build_unified_leaderboard(source_items)
    for it in unified[: args.top]:
        print(f"  {it.rank:>4}  {it.word:<26}  {it.merged:>8.4f}  {it.total_norm:>10.4f}  {it.source}")

    if args.out_tsv:
        write_unified_leaderboard_tsv(args.out_tsv, unified)
        print(f"\n  已写入 TSV: {args.out_tsv}  （共 {len(unified)} 条）")


# ────────────────────────────────────────────────────────────
#  原地归一化：读文件 → Step1 → 覆盖写回
# ────────────────────────────────────────────────────────────

def normalize_txt_file(path: str, source: str) -> bool:
    """
    读取 txt 三列文件，对第三列做 Step1 Min-Max 归一化，原地覆盖写回。

    归一化后第三列为 s_norm ∈ [0.05, 1.0]，保留 6 位小数。
    适用于: douyin_hotlist / xhs_hot / weibo_hotsearch

    Returns:
        True  — 归一化成功
        False — 文件读取失败或条目为空
    """
    items = read_txt_source(path, source)
    if not items:
        return False

    has_heat = any(it.raw_heat > 0 for it in items)
    if has_heat:
        normalize_minmax(items)
    else:
        normalize_by_rank(items)

    with open(path, "w", encoding="utf-8") as f:
        for it in items:
            f.write(f"{it.word}\t{it.pinyin}\t{it.s_norm:.6f}\n")

    return True


if __name__ == "__main__":
    _cli()

"""
pipeline/hot_words.py — 热词识别、词频统计、趋势与分类 (§2.5)

流程:
  1. 扫描所有来源原始标题（txt 三列 / JSON）
  2. 文本清洗
  3. jieba 分词（过滤停用词）
  4. Token-aligned N-gram 生成（MIN_LEN=2, MAX_LEN=8）
     → 只生成对齐 jieba 切分边界的短语，避免产生无意义字符碎片
  5. 候选词频统计 + 时间窗口趋势计算
  6. 规则分类（§2.3）:
       人名 / 事件词 / 产品名 / 政策词 / 趋势监控 / 其它热词
  7. 输出 hot_words_{date}.txt（简洁词频列表）
             hot_words_{date}_detail.tsv（含全部中间列）

用法:
    python -m pipeline.hot_words                        # 全量
    python -m pipeline.hot_words --date 20260417        # 仅一天
    python -m pipeline.hot_words --min-freq 2           # 频次阈值
    python -m pipeline.hot_words --base-dir output      # 数据目录
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ── 超参数 ────────────────────────────────────────────────
MIN_LEN   = 2
MAX_LEN   = 6
MIN_FREQ  = 1   # 默认阈值；CLI 可覆盖

# 跨平台加成系数（与 normalize.py 保持一致）
BONUS_K = 0.3

# 平台分组：同一公司/平台的多个来源归为一组，
# 跨平台加成按"分组数"计算，避免同平台多来源重复放大
PLATFORM_GROUP: Dict[str, str] = {
    "douyin_hotlist":  "douyin",
    "douyin_hotwords": "douyin",
    "weibo_hotsearch": "weibo",
    "xhs_hot":         "xhs",
    "xhs_hotpost":     "xhs",
}

# ── 来源内部键 ────────────────────────────────────────────
HOT_SOURCES = ["weibo_hotsearch", "douyin_hotlist", "xhs_hot", "douyin_hotwords", "xhs_hotpost"]

SOURCE_DISPLAY = {
    "weibo_hotsearch": "微博热搜",
    "douyin_hotlist":  "抖音热榜",
    "xhs_hot":         "小红书热点",
    "douyin_hotwords": "抖音热词",
    "xhs_hotpost":     "小红书热帖",
}

# ── §2.3 分类关键词 ───────────────────────────────────────
_POLICY_KEYWORDS: Set[str] = {
    "经济", "政策", "法规", "法案", "条例", "规范", "标准", "制度",
    "改革", "建设", "发展", "治理", "规划", "战略", "布局", "部署",
    "新质", "低空", "数字", "绿色", "碳", "减排", "双碳", "碳中和",
    "监管", "调控", "补贴", "减税", "免税", "社保", "养老", "医保",
    "教育", "住房", "房价", "基建", "乡村", "振兴", "一带一路",
    "两会", "政协", "人大", "全国", "委员", "代表", "提案", "议案",
    "五年计划", "十四五", "十五五", "三农", "共同富裕", "创新驱动",
}

_PRODUCT_KEYWORDS: Set[str] = {
    "手机", "平台", "系统", "版本", "芯片", "显卡", "软件", "游戏",
    "应用", "数码", "汽车", "电车", "充电", "耳机", "电脑", "笔记本",
    "相机", "镜头", "电视", "音箱", "路由", "可穿戴", "智能",
    "Pro", "Max", "Plus", "Ultra", "Mini",
    "AI", "GPT", "大模型", "算法",
    "发布", "上市", "首发", "体验", "评测", "测评",
}

_EVENT_KEYWORDS: Set[str] = {
    "亚运", "奥运", "世界杯", "锦标赛", "大赛", "比赛", "联赛",
    "晚会", "演唱会", "音乐节", "颁奖", "节目单",
    "发布会", "峰会", "会议", "论坛", "研讨会",
    "事件", "事故", "案件", "案", "遇难", "爆炸", "地震", "洪水",
    "节", "春节", "元宵", "中秋", "国庆", "五一", "清明", "端午",
    "首播", "开播", "开幕", "闭幕", "开赛", "决赛", "颁奖",
}

# ── 停用词（复用 normalize.py 的文件）────────────────────

def _load_stopwords() -> Set[str]:
    builtin = set(
        "的了是在有被也都从等和与或但而及对向以为到着过地得"
        "吗啊呢吧嘛哦呀不没很已再就会将要可这那此该其某各每"
        "我你他她它中上下内外后前间来去回起看"
    )
    sw_path = Path(__file__).parent / "stopwords.txt"
    if not sw_path.exists():
        return builtin
    words: Set[str] = set(builtin)
    with open(sw_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                words.add(line)
    return words


_STOP_WORDS: Set[str] = _load_stopwords()

# 停用字符集（单字）
_STOP_CHARS: Set[str] = set(
    "的了是在有被也都从等和与或但而及对向以为到着过地得"
    "吗啊呢吧嘛哦呀不没很已再就会将要可这那此该其某各每"
    "我你他她它中上下内外后前间来去回起看"
    "（）【】《》""''…—·、，。？！；：「」『』"
    "()[]<>{}\\/|@#$%^&*+=~`'\""
)


# ─────────────────────────────────────────────────────────
#  1. 扫描原始标题
# ─────────────────────────────────────────────────────────

def scan_all_data(
    base_dir: str,
    sources: Optional[List[str]] = None,
    target_date: Optional[str] = None,
) -> Tuple[Dict[str, Dict[str, List[str]]], Dict[str, Dict[str, str]]]:
    """
    扫描所有来源数据，返回标题列表和文件路径。

    Returns:
        titles_by_date: {date: {source_key: [title, ...]}}
        files_by_date:  {date: {source_key: file_path}}
    """
    if sources is None:
        sources = HOT_SOURCES

    titles: Dict[str, Dict[str, List[str]]] = defaultdict(dict)
    files:  Dict[str, Dict[str, str]]       = defaultdict(dict)

    for src in sources:
        src_dir = os.path.join(base_dir, src)
        if not os.path.isdir(src_dir):
            continue
        for d in sorted(os.listdir(src_dir)):
            if not (d.isdigit() and len(d) == 8):
                continue
            if target_date and d != target_date:
                continue
            day_dir = os.path.join(src_dir, d)
            ext = "*.json" if src in ("douyin_hotwords", "xhs_hotpost") else "*.txt"
            candidates = sorted(glob.glob(os.path.join(day_dir, ext)))
            if not candidates:
                continue
            latest = candidates[-1]
            ts = _read_titles(latest, src)
            if ts:
                titles[d][src] = ts
                files[d][src]  = latest

    return dict(titles), dict(files)


def scan_latest_per_source(
    base_dir: str,
    sources: Optional[List[str]] = None,
) -> Tuple[Dict[str, Dict[str, List[str]]], Dict[str, Dict[str, str]]]:
    """
    各来源独立取最新文件（日期可以不同），合并到虚拟日期 "latest"。

    用于"总榜"模式：抖音热词可能比微博热搜更新，也能统一参与归一化。

    Returns:
        titles_by_date: {"latest": {source_key: [title, ...]}}
        files_by_date:  {"latest": {source_key: file_path}}
    """
    if sources is None:
        sources = HOT_SOURCES

    titles: Dict[str, List[str]] = {}
    files:  Dict[str, str]       = {}

    for src in sources:
        src_dir = os.path.join(base_dir, src)
        if not os.path.isdir(src_dir):
            continue
        ext = "*.json" if src in ("douyin_hotwords", "xhs_hotpost") else "*.txt"
        # 遍历所有日期目录，取全局最新文件
        all_files = sorted(glob.glob(os.path.join(src_dir, "**", ext), recursive=True))
        if not all_files:
            continue
        latest = all_files[-1]
        ts = _read_titles(latest, src)
        if ts:
            titles[src] = ts
            files[src]  = latest

    if not titles:
        return {}, {}
    return {"latest": titles}, {"latest": files}


def scan_all_titles(
    base_dir: str,
    sources: Optional[List[str]] = None,
    target_date: Optional[str] = None,
) -> Dict[str, Dict[str, List[str]]]:
    """兼容接口：只返回标题字典。"""
    titles, _ = scan_all_data(base_dir, sources, target_date)
    return titles


def _read_titles(path: str, source: str) -> List[str]:
    titles: List[str] = []
    try:
        if source == "douyin_hotwords":
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            for entry in data:
                t = (entry.get("title") or "").strip()
                if t:
                    titles.append(t)
        elif source == "xhs_hotpost":
            # xhs_hotpost JSON：词条 = title（笔记标题）
            # 同标题去重，按文件中首次出现顺序保留
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            seen: Set[str] = set()
            for entry in data:
                t = (entry.get("title") or "").strip()
                if t and t not in seen:
                    seen.add(t)
                    titles.append(t)
        else:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    parts = line.rstrip("\n").split("\t")
                    if parts and parts[0].strip():
                        titles.append(parts[0].strip())
    except Exception:
        pass
    return titles


# ─────────────────────────────────────────────────────────
#  2. 文本清洗
# ─────────────────────────────────────────────────────────

_RE_CLEAN = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9\u3040-\u30ff]")


def clean_text(text: str) -> str:
    """保留汉字、字母、数字和日文假名，去除标点及特殊符号。"""
    return _RE_CLEAN.sub("", text).strip()


# ─────────────────────────────────────────────────────────
#  3. jieba 分词
# ─────────────────────────────────────────────────────────

def tokenize(text: str) -> List[str]:
    """jieba 分词 → 过滤停用词和单字符 token → 返回有效 token 列表。"""
    try:
        import jieba  # type: ignore
        import jieba.posseg as pseg  # type: ignore
    except ImportError:
        return [text] if text else []

    tokens: List[str] = []
    for word, flag in pseg.cut(text):
        word = word.strip()
        if not word or len(word) < 2:
            continue
        if word in _STOP_WORDS:
            continue
        if word.isdigit() or word.isascii():
            continue
        tokens.append(word)
    return tokens


def tokenize_with_pos(text: str) -> List[Tuple[str, str]]:
    """返回 [(token, pos_flag)] 列表，用于分类。"""
    try:
        import jieba.posseg as pseg  # type: ignore
    except ImportError:
        return [(text, "n")]
    result = []
    for word, flag in pseg.cut(text):
        word = word.strip()
        if word and len(word) >= 2:
            result.append((word, flag))
    return result


# ─────────────────────────────────────────────────────────
#  4. N-gram 候选词生成
# ─────────────────────────────────────────────────────────

def extract_candidates(
    tokens: List[str],
    min_len: int = MIN_LEN,
    max_len: int = MAX_LEN,
) -> List[str]:
    """
    从 jieba 分词结果中提取候选词。

    策略：每个候选词 = 一个 jieba token（不做跨 token 滑动组合）。
    跨 token 滑动组合会产生"顶针"链式碎片（如 万能旅行→旅行拍照→拍照姿势），
    这些拼接词在热词识别中没有实际意义。

    过滤条件：
      - 长度在 [min_len, max_len] 之间
      - 首尾字符不是停用字符
      - 不全为数字或 ASCII
    """
    result: List[str] = []
    for tok in tokens:
        if len(tok) < min_len or len(tok) > max_len:
            continue
        if tok[0] in _STOP_CHARS or tok[-1] in _STOP_CHARS:
            continue
        if tok.isdigit() or tok.isascii():
            continue
        result.append(tok)
    return result


# ─────────────────────────────────────────────────────────
#  5. 词频统计 + 时间趋势
# ─────────────────────────────────────────────────────────

def count_ngrams(
    titles_by_date: Dict[str, Dict[str, List[str]]],
    min_len: int = MIN_LEN,
    max_len: int = MAX_LEN,
    files_by_date: Optional[Dict[str, Dict[str, str]]] = None,
) -> Tuple[
    Dict[str, Dict[str, int]],             # word → {date: freq}
    Dict[str, Set[str]],                    # word → {source_key}
    Set[str],                               # known_persons
    Dict[str, Dict[str, Dict[str, Any]]],   # token → {date: {src: norm_info}}
]:
    """
    遍历所有日期/来源/标题，统计候选词频次，并（可选）计算归一化分数。

    归一化逻辑（当 files_by_date 传入时启用）：
      对每条标题调用 normalize.process_file 得到 HotItem（s_norm / final）；
      将标题分词后的每个 token 继承该 HotItem 的分数；
      对同一 (token, date) 在多来源的出现计算 token 级跨平台加成：
        bonus  = 1 + 0.3 × ln(n_platforms)
        merged = final × bonus

    Returns:
        freq_by_date:  token → {date: count}
        word_sources:  token → set(source_keys)
        known_persons: 从原始标题 posseg 提取的 nr 人名集合
        norm_data:     token → {date: {src: {"s_norm","final","bonus","merged",
                                              "n_platforms","raw_heat","rank","orig_term"}}}
    """
    freq_by_date: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    word_sources: Dict[str, Set[str]] = defaultdict(set)
    known_persons: Set[str] = set()
    # token → date → src → best norm info (by final score)
    norm_data: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(
        lambda: defaultdict(dict)
    )

    try:
        import jieba.posseg as pseg  # type: ignore
        _has_pseg = True
    except ImportError:
        _has_pseg = False

    # Pre-load normalized HotItems for each (date, src)
    norm_items: Dict[str, Dict[str, List[Any]]] = defaultdict(dict)
    if files_by_date:
        try:
            from pipeline.normalize import process_file as _pf  # type: ignore
            for date, src_paths in files_by_date.items():
                for src, path in src_paths.items():
                    try:
                        norm_items[date][src] = _pf(path, src)
                    except Exception:
                        pass
        except ImportError:
            pass

    for date, src_map in titles_by_date.items():
        # tmp: token → {src: best_info} — used to compute per-date cross-platform bonus
        _token_src: Dict[str, Dict[str, Any]] = defaultdict(dict)

        for src, titles in src_map.items():
            # Build title-text → HotItem lookup for this (date, src)
            title_to_item: Dict[str, Any] = {}
            for item in norm_items.get(date, {}).get(src, []):
                title_to_item[item.word] = item

            for title in titles:
                cleaned = clean_text(title)
                if not cleaned:
                    continue

                # ── 提取人名 ─────────────────────────────────
                if _has_pseg:
                    for tok, flag in pseg.cut(cleaned):
                        tok = tok.strip()
                        if flag == "nr" and 2 <= len(tok) <= 5:
                            known_persons.add(tok)

                # ── 候选词 ───────────────────────────────────
                tokens = tokenize(cleaned)
                if not tokens:
                    continue
                ngrams = extract_candidates(tokens, min_len, max_len)
                if not ngrams:
                    continue

                hot_item = title_to_item.get(title)

                for ng in ngrams:
                    freq_by_date[ng][date] += 1
                    word_sources[ng].add(src)

                    if hot_item is None:
                        continue
                    prev = _token_src[ng].get(src)
                    if prev is None or hot_item.final > prev["final"]:
                        _token_src[ng][src] = {
                            "final":    hot_item.final,
                            "s_norm":   hot_item.s_norm,
                            "raw_heat": hot_item.raw_heat,
                            "rank":     hot_item.rank,
                            "orig_term": title,
                        }

        # Apply token-level cross-platform bonus for this date
        # 用平台分组数（而非来源数）计算 bonus，避免同平台多来源重复放大
        for token, src_info_map in _token_src.items():
            n_groups = len({PLATFORM_GROUP.get(s, s) for s in src_info_map})
            bonus = 1.0 + BONUS_K * math.log(n_groups) if n_groups > 1 else 1.0
            for src, info in src_info_map.items():
                norm_data[token][date][src] = {
                    **info,
                    "n_platforms": n_groups,
                    "bonus":       bonus,
                    "merged":      info["final"] * bonus,
                }

    return dict(freq_by_date), dict(word_sources), known_persons, dict(norm_data)


def compute_trend(freq_by_date: Dict[str, int]) -> str:
    """
    基于按日期分布的频次，判断趋势标签。

    趋势规则：
      新词   — 只出现在最新 1 个日期
      上升   — 最新日期频次 > 最早日期频次（至少 2 个日期）
      下降   — 最新日期频次 < 最早日期频次
      持续   — 出现 3 个及以上日期
      平稳   — 其余
    """
    if not freq_by_date:
        return "未知"
    sorted_dates = sorted(freq_by_date.keys())
    if len(sorted_dates) == 1:
        return "新词"
    if len(sorted_dates) >= 3:
        return "持续"
    first = freq_by_date[sorted_dates[0]]
    last  = freq_by_date[sorted_dates[-1]]
    if last > first:
        return "上升"
    if last < first:
        return "下降"
    return "平稳"


# ─────────────────────────────────────────────────────────
#  6. 规则分类（§2.3）
# ─────────────────────────────────────────────────────────

def _jieba_pos_pairs(word: str) -> List[Tuple[str, str]]:
    """jieba posseg 对 word 的切分结果 [(token, flag)]。"""
    try:
        import jieba.posseg as pseg  # type: ignore
        return [(w, f) for w, f in pseg.cut(word)]
    except Exception:
        return [(word, "n")]


def classify_word(
    word: str,
    trend: str,
    known_persons: Optional[Set[str]] = None,
) -> str:
    """
    规则分类（不依赖 LLM）：

    分类优先级（从高到低）：
      1. 人名     — 在 known_persons（从原始标题 posseg 提取）中命中；
                    或 2~4 字且 jieba 整词 posseg 标注为 nr/nz
      2. 政策词   — 含政策关键词
      3. 产品名   — 含产品关键词；或 jieba 整词标注为 nt/nz 且 ≥3 字
      4. 事件词   — 含事件关键词；或多 token 短语 ≥5 字
      5. 趋势监控 — trend 为 "上升"/"持续" 且长度 ≥4
      6. 其它热词 — 兜底

    核心原则：人名必须在原始标题分词中被明确标注，
    N-gram 短语（多 token 组合）不用 POS 直接判人名。
    """
    if known_persons is None:
        known_persons = set()

    # ── 人名：优先查 known_persons 集合 ──────────────────
    if word in known_persons:
        return "人名"

    # 对 2~4 字短词，仍尝试 posseg 确认（外文译名等）
    if 2 <= len(word) <= 4:
        pairs = _jieba_pos_pairs(word)
        if len(pairs) == 1:
            _, flag = pairs[0]
            if flag == "nr":
                return "人名"
            if flag == "nz":
                # 外文译名（马斯克, 哈立德）而非地名/产品名
                # 排除常见地名/非人名的 nz
                if not any(k in word for k in _POLICY_KEYWORDS | _PRODUCT_KEYWORDS | _EVENT_KEYWORDS):
                    return "人名"

    # ── 关键词匹配 ────────────────────────────────────────
    if any(k in word for k in _POLICY_KEYWORDS):
        return "政策词"

    if any(k in word for k in _PRODUCT_KEYWORDS):
        return "产品名"

    if any(k in word for k in _EVENT_KEYWORDS):
        return "事件词"

    # 多 token 长短语 ≥5 字 → 事件词
    pairs = _jieba_pos_pairs(word)
    if len(word) >= 5 and len(pairs) >= 2:
        return "事件词"

    # 整词为机构/专有名词 ≥3 字（且上面未命中）→ 产品名
    if len(pairs) == 1 and pairs[0][1] in ("nt", "nz") and len(word) >= 3:
        return "产品名"

    # ── 趋势监控 ─────────────────────────────────────────
    if trend in ("上升", "持续") and len(word) >= 4:
        return "趋势监控"

    return "其它热词"


# ─────────────────────────────────────────────────────────
#  7. 主流程 & 输出
# ─────────────────────────────────────────────────────────

def build_hot_words(
    base_dir: str,
    sources: Optional[List[str]] = None,
    target_date: Optional[str] = None,
    min_freq: int = MIN_FREQ,
    min_len: int = MIN_LEN,
    max_len: int = MAX_LEN,
    out_dir: Optional[str] = None,
    all_dates: bool = False,
) -> Tuple[str, str]:
    """
    执行完整热词识别流程，写出两个文件：
      - hot_words_{tag}.txt          简洁词频列表（word \\t freq）
      - hot_words_{tag}_detail.tsv   含全部中间列的宽表

    Returns:
        (simple_path, detail_path)
    """
    now = datetime.now()

    # ── 1. 扫描标题 + 文件路径 ───────────────────────────
    if target_date is not None:
        # 明确指定日期
        tag = target_date
        titles_by_date, files_by_date = scan_all_data(base_dir, sources, target_date)
    elif all_dates:
        # 全量历史
        titles_by_date, files_by_date = scan_all_data(base_dir, sources, None)
        if not titles_by_date:
            raise ValueError(f"在 {base_dir} 中未找到任何热榜数据")
        tag = sorted(titles_by_date.keys())[-1]
    else:
        # 默认：各来源独立取最新文件，合并成一个"总榜"
        # tag 用今天的系统日期作为输出文件名
        tag = now.strftime("%Y%m%d")
        titles_by_date, files_by_date = scan_latest_per_source(base_dir, sources)
    if not titles_by_date:
        raise ValueError(f"在 {base_dir} 中未找到任何热榜数据")

    total_titles = sum(
        len(ts)
        for src_map in titles_by_date.values()
        for ts in src_map.values()
    )
    print(f"  扫描到 {len(titles_by_date)} 个日期 / {total_titles} 条标题")

    # ── 2~4. 分词 + N-gram 统计 + 人名抽取 + 归一化 ─────
    freq_by_date, word_sources, known_persons, norm_data = count_ngrams(
        titles_by_date, min_len, max_len, files_by_date
    )
    print(f"  识别到人名候选: {len(known_persons)} 个")

    # ── 5. 过滤 + 聚合归一化分数 ─────────────────────────
    rows = []
    for word, date_freq in freq_by_date.items():
        total = sum(date_freq.values())
        if total < min_freq:
            continue
        trend    = compute_trend(date_freq)
        category = classify_word(word, trend, known_persons)
        dates_str = ",".join(sorted(date_freq.keys()))
        srcs_str  = ",".join(
            SOURCE_DISPLAY.get(s, s) for s in sorted(word_sources.get(word, set()))
        )

        # ── 归一化聚合 ────────────────────────────────────
        # norm_data[word][date][src] = {"merged":..., "final":..., "s_norm":..., ...}
        wnd = norm_data.get(word, {})
        all_merged: List[float] = []
        best_merged = 0.0
        best_src    = ""
        best_term   = ""
        norm_by_date: Dict[str, float] = {}

        for d, src_map in wnd.items():
            day_max = 0.0
            for src, info in src_map.items():
                m = info.get("merged", 0.0)
                all_merged.append(m)
                if m > day_max:
                    day_max = m
                if m > best_merged:
                    best_merged = m
                    best_src    = SOURCE_DISPLAY.get(src, src)
                    best_term   = info.get("orig_term", "")
            norm_by_date[d] = day_max

        max_merged = best_merged
        sum_merged = sum(all_merged)

        rows.append({
            "word":         word,
            "freq":         total,
            "category":     category,
            "trend":        trend,
            "dates":        dates_str,
            "sources":      srcs_str,
            "freq_by_date": date_freq,
            "max_merged":   max_merged,
            "sum_merged":   sum_merged,
            "best_source":  best_src,
            "best_orig_term": best_term,
            "norm_by_date": norm_by_date,
        })

    # 主排序：max_merged 降序（有归一化数据时）；其次 freq 降序，词长降序
    rows.sort(key=lambda r: (-r["max_merged"], -r["freq"], -len(r["word"])))

    # ── Step 5：总榜归一化（对 max_merged 做 Min-Max+ε，映射到 [0.05, 1.0]）──
    # 只对有归一化分数的词做计算；无分数的词 total_norm 留空
    _eps = 0.05
    scored = [r for r in rows if r["max_merged"] > 0]
    if len(scored) >= 2:
        _max = max(r["max_merged"] for r in scored)
        _min = min(r["max_merged"] for r in scored)
        _rng = _max - _min
        for r in scored:
            ratio = (r["max_merged"] - _min) / _rng if _rng > 0 else 0.0
            r["total_norm"] = _eps + (1.0 - _eps) * ratio
    elif len(scored) == 1:
        scored[0]["total_norm"] = 1.0
    for r in rows:
        r.setdefault("total_norm", 0.0)

    # ── 6. 输出目录 ──────────────────────────────────────
    if out_dir is None:
        out_dir = os.path.join(base_dir, "hot_words", tag)
    os.makedirs(out_dir, exist_ok=True)

    # ── 7. 单一输出文件 ───────────────────────────────────
    # 列：word / pinyin / source / orig_term /
    #      single_normalized / all_normalized（均 ×10^8 取整）
    try:
        from pypinyin import lazy_pinyin, Style  # type: ignore
        def _pinyin(w: str) -> str:
            return "".join(f"`{s}" for s in lazy_pinyin(w, style=Style.NORMAL))
    except ImportError:
        def _pinyin(w: str) -> str:  # type: ignore[misc]
            return ""

    def _to_int8(v: float) -> int:
        """归一化值 × 10^8 取整。"""
        return int(round(v * 1e8))

    # 收集每个 (word, src) 最佳一行（最高 merged）
    out_rows: List[Tuple[str, str, str, str, float, float]] = []
    # word, pinyin, src_display, orig_term, final, merged

    for r in rows:
        wnd = norm_data.get(r["word"], {})
        src_best_info: Dict[str, Any] = {}
        for d, src_map in wnd.items():
            for src, info in src_map.items():
                prev = src_best_info.get(src)
                if prev is None or info.get("merged", 0.0) > prev.get("merged", 0.0):
                    src_best_info[src] = info

        py = _pinyin(r["word"])
        for src, info in src_best_info.items():
            out_rows.append((
                r["word"],
                py,
                SOURCE_DISPLAY.get(src, src),
                info.get("orig_term", ""),
                info.get("final",     0.0),
                info.get("merged",    0.0),
            ))

    out_rows.sort(key=lambda x: -x[5])

    out_path = os.path.join(out_dir, f"hot_words_{tag}.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("word\tpinyin\tsource\torig_term\tsingle_normalized\tall_normalized\n")
        for word, py, src_d, orig, final, merged in out_rows:
            f.write(
                f"{word}\t{py}\t{src_d}\t{orig}\t"
                f"{_to_int8(final)}\t{_to_int8(merged)}\n"
            )

    return out_path, out_path, []


# ─────────────────────────────────────────────────────────
#  8. 分类汇总打印
# ─────────────────────────────────────────────────────────

def print_summary(rows: List[dict], top: int = 20) -> None:
    categories = ["人名", "事件词", "产品名", "政策词", "趋势监控", "其它热词"]
    for cat in categories:
        subset = [r for r in rows if r["category"] == cat]
        if not subset:
            continue
        print(f"\n{'─'*55}")
        print(f"  {cat}  ({len(subset)} 个)")
        print(f"{'─'*55}")
        for r in subset[:top]:
            bar = "▇" * min(r["freq"], 20)
            print(f"  {r['word']:<20} {r['freq']:>4}  {bar}  [{r['trend']}]")


# ─────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────

def _cli() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="热榜热词识别 (§2.5)")
    parser.add_argument("--base-dir",   default="output", help="数据根目录")
    parser.add_argument("--sources",    nargs="+", default=None, help="来源列表")
    parser.add_argument("--date",       default=None,
                        help="指定日期 YYYYMMDD（默认=今天）")
    parser.add_argument("--all-dates",  action="store_true",
                        help="扫描全部历史日期（不限于今天）")
    parser.add_argument("--min-freq",   type=int, default=MIN_FREQ,  help="最低频次")
    parser.add_argument("--min-len",    type=int, default=MIN_LEN,   help="最短词长")
    parser.add_argument("--max-len",    type=int, default=MAX_LEN,   help="最长词长")
    parser.add_argument("--top",        type=int, default=20, help="每类展示 Top N")
    parser.add_argument("--out-dir",    default=None, help="输出目录")
    args = parser.parse_args()

    # --all-dates 时强制传 None，build_hot_words 会扫全量历史
    # 否则透传 --date（可为 None，build_hot_words 自动取最新日期）
    effective_date: Optional[str] = None if args.all_dates else args.date

    print("=" * 55)
    print("  热榜热词识别 §2.5 流程")
    print("=" * 55)

    simple_path, detail_path, src_paths = build_hot_words(
        base_dir   = args.base_dir,
        sources    = args.sources,
        target_date= effective_date,
        min_freq   = args.min_freq,
        min_len    = args.min_len,
        max_len    = args.max_len,
        out_dir    = args.out_dir,
        all_dates  = args.all_dates,
    )

    print_summary([], top=args.top)

    print(f"\n{'═'*55}")
    print(f"  输出文件: {simple_path}")
    print(f"{'═'*55}")


if __name__ == "__main__":
    _cli()

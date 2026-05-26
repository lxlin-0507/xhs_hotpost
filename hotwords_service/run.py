"""
hotwords_service/run.py — 独立热词归一化服务（HanLP 链路）

功能：
  读取 /tmp/hotwords/ 下各来源最新文件 → HanLP 分词 → 5步归一化
  → 写出 {base_dir}/rank/YYYYMMDD/rank_YYYYMMDD_HHMMSS.csv（TSV）

  只处理当时目录下存在数据的来源；无任何来源数据时不生成文件。

算法（§2.1）：
  Step 1  平台内 Min-Max 归一化（ε=0.05）
  Step 2  无热度值来源用指数衰减（λ=0.5）
  Step 3  × 平台权重（抖音0.35 / 微博0.25 / 小红书0.15）
  Step 4  token 级跨平台加成（按平台分组，避免同平台重复放大）
           bonus = 1 + 0.3 × ln(n_platform_groups)
  Step 5  全榜 Min-Max+ε，映射到 [0.05, 1.0] → × 10^8 取整

切词（HanLP 多任务模型，CLOSE_TOK_POS_NER_SRL_DEP_SDP_CON_ELECTRA_SMALL_ZH）：
  - 双轨提取：事件级短语（Track A）+ NER 实体兜底（Track B）
  - user_dict.txt 作为 dict_force 强制整词
  - sogou_snapshot.txt 作为补充用户词典（冻结快照，由 refresh_sogou_snapshot.py 刷新）
  - blocklist.txt 输出黑名单兜底

输出列（\\t 分隔）：
  word | pinyin | source | orig_term | single_normalized | all_normalized | heat_tier

用法：
  python3 run.py                          # 启动定时服务（每小时 :19 / :49）
  python3 run.py --once                   # 立即执行一次后退出
  python3 run.py --base-dir /data/hw      # 指定数据根目录
  python3 run.py --out-dir /data/hw/rank  # 指定输出目录
  python3 run.py --once --mode word       # 词级切分（默认 phrase 事件级）
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import math
import os
import re
import sys as _sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# ── 日志 ──────────────────────────────────────────────────
# 项目根目录（hotwords_service/ 的上一级）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOG_DIR = _PROJECT_ROOT / "logs"


def _setup_logging() -> None:
    """控制台 + 滚动文件（logs/run.log，单文件最大 10 MB，保留 7 个备份）"""
    from logging.handlers import RotatingFileHandler

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # 控制台（只加一次，避免重复 handler）
    if not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    ):
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        root.addHandler(ch)

    # 滚动文件
    fh = RotatingFileHandler(
        _LOG_DIR / "run.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=7,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)


_setup_logging()
log = logging.getLogger("hotwords_service")

# ── 路径 ──────────────────────────────────────────────────
# 兼容 PyInstaller 打包：资源文件在 _MEIPASS 目录下
_HERE = Path(getattr(_sys, "_MEIPASS", Path(__file__).resolve().parent))
if str(_HERE) not in _sys.path:
    _sys.path.insert(0, str(_HERE))

# HanLP 模型目录：优先使用外部环境变量，未设置则默认放在本目录的 hanlp/ 子目录
# 将模型文件放到 hotwords_service/hanlp/ 后即可离线运行，无需联网下载
import os as _os
_os.environ.setdefault("HANLP_HOME", str(_HERE / "hanlp"))

DEFAULT_BASE_DIR = "/tmp/hotwords"
DEFAULT_OUT_DIR  = None  # None → {base_dir}/rank

# ── 算法超参数 ─────────────────────────────────────────────
EPS      = 0.05
LAMBDA   = 0.5
BONUS_K  = 0.3
MIN_FREQ = 1

# ── 热度分级（基于 all_normalized 绝对阈值）──
# all_normalized = s_norm × 平台权重 × 1e8（无 bonus，无全局 min-max）
# 所有平台权重 0.20，结构上界统一为 20M（任一来源 top1）
# 阈值含义：
#   tier-1 ≥ 25M：各来源 top1 级别（s_norm = 1.0，每源约 1–2 条）
#   tier-2 ≥  8M：各来源 s_norm ≥ 0.40
#   tier-3 ≥  3M：各来源 s_norm ≥ 0.15
#   tier-4  < 3M：长尾
TIER_THRESHOLDS = [28_000_000, 8_000_000, 3_000_000]

def _heat_tier(all_norm: int) -> str:
    """根据 all_normalized 绝对阈值返回热度档位（1–4）。"""
    if all_norm >= TIER_THRESHOLDS[0]:
        return "1"
    if all_norm >= TIER_THRESHOLDS[1]:
        return "2"
    if all_norm >= TIER_THRESHOLDS[2]:
        return "3"
    return "4"

# ── 来源配置 ───────────────────────────────────────────────
HOT_SOURCES = ["weibo_hotsearch", "douyin_hotlist", "xhs_hot", "douyin_hotwords", "xhs_hotpost"]

# 这些来源的词条本身就是独立词，不再经过 HanLP 切词，直接用原词作为 token
NOCUT_SOURCES: Set[str] = {"douyin_hotwords"}

PLATFORM_WEIGHTS: Dict[str, float] = {
    "douyin_hotlist":  0.35,
    "douyin_hotwords": 0.35,
    "weibo_hotsearch": 0.25,
    "xhs_hot":         0.15,
    "xhs_hotpost":     0.15,
}

# 同平台分组（跨平台加成时按分组数计，避免同平台多来源重复放大）
PLATFORM_GROUP: Dict[str, str] = {
    "douyin_hotlist":  "douyin",
    "douyin_hotwords": "douyin",
    "weibo_hotsearch": "weibo",
    "xhs_hot":         "xhs",
    "xhs_hotpost":     "xhs",
}

SOURCE_DISPLAY: Dict[str, str] = {
    "weibo_hotsearch": "微博热搜",
    "douyin_hotlist":  "抖音热榜",
    "xhs_hot":         "小红书热点",
    "douyin_hotwords": "抖音热词",
    "xhs_hotpost":     "小红书热帖",
}


# ─────────────────────────────────────────────────────────
#  数据结构
# ─────────────────────────────────────────────────────────

@dataclass
class HotItem:
    word:     str
    source:   str
    raw_heat: float
    rank:     int
    s_norm:   float = 0.0
    final:    float = 0.0


# ─────────────────────────────────────────────────────────
#  停用词 / 黑名单 / 用户词典
# ─────────────────────────────────────────────────────────
#
# stopwords.txt / blocklist.txt / user_dict.txt 与 run.py 同目录部署。
# 缺失视为部署不完整，直接抛错让进程 crash —— 避免线上退化成"没字典"
# 模式而长时间无人察觉。

def _load_stopwords() -> Set[str]:
    builtin = set(
        "的了是在有被也都从等和与或但而及对向以为到着过地得"
        "吗啊呢吧嘛哦呀不没很已再就会将要可这那此该其某各每"
        "我你他她它中上下内外后前间来去回起看"
    )
    sw_path = _HERE / "stopwords.txt"
    if not sw_path.exists():
        return builtin
    words: Set[str] = set(builtin)
    with open(sw_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                words.add(line)
    return words


def _load_blocklist() -> Set[str]:
    """加载输出屏蔽词。最终结果里这些词永远不会出现。"""
    path = _HERE / "blocklist.txt"
    if not path.exists():
        raise FileNotFoundError(
            f"blocklist.txt 缺失：{path}\n"
            f"该文件是热词归一化的核心配置，必须与 run.py 同目录部署。"
        )
    words: Set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                words.add(line)
    return words


def _load_user_dict_words() -> Set[str]:
    """加载用户词典（HanLP dict_force 强制整词）。"""
    path = _HERE / "user_dict.txt"
    if not path.exists():
        raise FileNotFoundError(
            f"user_dict.txt 缺失：{path}\n"
            f"该文件是热词归一化的核心配置，必须与 run.py 同目录部署。"
        )
    words: Set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if parts:
                words.add(parts[0])
    return words


def _load_event_verbs() -> Set[str]:
    """加载事件动词白名单（与 run.py 同目录 event_verbs.txt）。

    HanLP 词性 v 不区分"事件动词/介词性动词/通用动作动词"，本文件人工列出
    "明确事件性"的双字 v（夺冠/抵达/曝光/暴发/偷拍/合龙 等），segmenter
    遇到这些词时强制按 vn 处理（进 buf 当短语一部分）。

    文件缺失视为弱配置，返回空集合，仅记录 warning（与 user_dict/blocklist
    必须存在不同——event_verbs 的存在与否只决定事件短语召回率，不影响主流程）。
    """
    path = _HERE / "event_verbs.txt"
    if not path.exists():
        log.warning(f"event_verbs.txt 缺失：{path}（事件短语召回会下降）")
        return set()
    words: Set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                words.add(line.split()[0])
    log.info(f"已加载事件动词白名单 {len(words)} 词")
    return words


def _load_sogou_snapshot() -> Set[str]:
    """
    加载搜狗词库快照（与 run.py 同目录的 sogou_snapshot.txt）。

    快照由 refresh_sogou_snapshot.py 生成，扫描 {base_dir}/sogou_newwords/
    最近 N 天的文件并去重导出。run.py 启动只读快照，不实时扫目录 ——
    把搜狗词库当成"和代码一起部署的固定资源"，新词需要手动刷新快照。

    文件不存在视为部署不完整，直接抛错。
    """
    path = _HERE / "sogou_snapshot.txt"
    if not path.exists():
        raise FileNotFoundError(
            f"sogou_snapshot.txt 缺失：{path}\n"
            f"用 refresh_sogou_snapshot.py 生成快照后再启动。"
        )
    words: Set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                words.add(line)
    log.info(f"已加载搜狗词库快照 {len(words)} 词")
    return words


_STOP_WORDS:  Set[str] = _load_stopwords()
_BLOCK_WORDS: Set[str] = _load_blocklist()


# ─────────────────────────────────────────────────────────
#  Step 1/2  归一化
# ─────────────────────────────────────────────────────────

def _normalize_source(items: List[HotItem]) -> List[HotItem]:
    """Min-Max（有热度）或指数衰减（无热度）+ 平台权重。"""
    if not items:
        return items
    if any(it.raw_heat > 0 for it in items):
        heats = [it.raw_heat for it in items]
        h_max, h_min = max(heats), min(heats)
        h_range = h_max - h_min
        for it in items:
            if h_range == 0:
                it.s_norm = EPS
            else:
                ratio = (it.raw_heat - h_min) / h_range
                it.s_norm = EPS + (1.0 - EPS) * ratio
    else:
        for it in items:
            it.s_norm = math.exp(-LAMBDA * (it.rank - 1))
    w = PLATFORM_WEIGHTS.get(items[0].source, 0.25)
    for it in items:
        it.final = it.s_norm * w
    return items


# ─────────────────────────────────────────────────────────
#  读文件
# ─────────────────────────────────────────────────────────

def _parse_heat(s: str) -> float:
    try:
        return float(s.strip()) if s.strip() else 0.0
    except ValueError:
        return 0.0


def _read_items(path: str, source: str) -> List[HotItem]:
    items: List[HotItem] = []
    try:
        if source == "douyin_hotwords":
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            for idx, entry in enumerate(data):
                word = (entry.get("title") or "").strip()
                score = float(entry.get("score") or 0)
                rank  = int(entry.get("rank") or (idx + 1))
                if word:
                    items.append(HotItem(word=word, source=source,
                                         raw_heat=score, rank=rank))
        elif source == "xhs_hotpost":
            # 词条 = title（笔记标题），热度 = keyword_heat（继承所属热搜的热度）
            # 同标题去重，热度取最大；rank 按文件中笔记首次出现顺序赋值
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            seen: Dict[str, HotItem] = {}
            order: List[str] = []
            for entry in data:
                word = (entry.get("title") or "").strip()
                if not word:
                    continue
                try:
                    heat = float(entry.get("keyword_heat") or 0)
                except (TypeError, ValueError):
                    heat = 0.0
                if word not in seen:
                    seen[word] = HotItem(word=word, source=source,
                                         raw_heat=heat, rank=len(order) + 1)
                    order.append(word)
                elif heat > seen[word].raw_heat:
                    seen[word].raw_heat = heat
            items = [seen[w] for w in order]
        else:
            with open(path, encoding="utf-8") as f:
                for rank, line in enumerate(f, 1):
                    parts = line.rstrip("\n").split("\t")
                    if not parts or not parts[0].strip():
                        continue
                    word  = parts[0].strip()
                    heat  = _parse_heat(parts[2]) if len(parts) >= 3 else 0.0
                    items.append(HotItem(word=word, source=source,
                                         raw_heat=heat, rank=rank))
    except Exception as exc:
        log.warning(f"读文件失败 {path}: {exc}")
    return items


# ─────────────────────────────────────────────────────────
#  扫描：各来源取最新文件
# ─────────────────────────────────────────────────────────

def scan_latest_per_source(
    base_dir: str,
    sources: Optional[List[str]] = None,
    target_date: Optional[str] = None,
) -> Tuple[Dict[str, List[str]], Dict[str, str], Dict[str, List[HotItem]]]:
    """
    各来源独立找最新文件（日期可以不同）。

    Args:
        target_date: 形如 "20260430"，若指定则只使用 ≤ 该日期的文件（精确优先）。
                     None 表示取全局最新。

    Returns:
        titles_by_src:  {src: [title, ...]}
        files_by_src:   {src: file_path}
        items_by_src:   {src: [HotItem, ...]}  已完成 Step1/2/3 归一化
    """
    if sources is None:
        sources = HOT_SOURCES

    titles_by_src: Dict[str, List[str]]     = {}
    files_by_src:  Dict[str, str]            = {}
    items_by_src:  Dict[str, List[HotItem]] = {}

    for src in sources:
        src_dir = os.path.join(base_dir, src)
        if not os.path.isdir(src_dir):
            continue
        ext = "*.json" if src in ("douyin_hotwords", "xhs_hotpost") else "*.txt"
        all_files = sorted(glob.glob(os.path.join(src_dir, "**", ext), recursive=True))
        if not all_files:
            continue
        if target_date:
            # 优先精确匹配，否则取 ≤ target_date 的最近一天；
            # 若该来源数据全都比 target_date 新（如 xhs_hotpost），
            # 则回落到最新可用文件，而不是跳过该来源。
            exact = [f for f in all_files if f"/{target_date}/" in f]
            if exact:
                latest = exact[-1]
            else:
                before = [f for f in all_files if os.path.basename(os.path.dirname(f)) <= target_date]
                latest = before[-1] if before else all_files[-1]
        else:
            latest = all_files[-1]
        raw_items = _read_items(latest, src)
        if not raw_items:
            continue
        _normalize_source(raw_items)
        titles_by_src[src] = [it.word for it in raw_items]
        files_by_src[src]  = latest
        items_by_src[src]  = raw_items
        log.info(f"  {SOURCE_DISPLAY.get(src, src)}: {latest} ({len(raw_items)} 条)")

    return titles_by_src, files_by_src, items_by_src


# ─────────────────────────────────────────────────────────
#  拼音
# ─────────────────────────────────────────────────────────

def _pinyin(word: str) -> str:
    """
    生成拼音串。
      中文段 → 反引号分隔的拼音音节：世锦赛 → `shi`jin`sai
      英文/数字段 → 拆字符 + 小写 + 空格连，作为一个段（带前导反引号）：
                    rap → `r a p；AI → `a i；5G → `5 g
      混合举例：李永钦好燃的rap入驻抖音
            → `li`yong`qin`hao`ran`de`r a p`ru`zhu`dou`yin
    """
    try:
        from pypinyin import lazy_pinyin, Style  # type: ignore
    except ImportError:
        return ""

    parts: List[str] = []
    buf_zh: List[str] = []
    buf_en: List[str] = []

    def _flush_en() -> None:
        if buf_en:
            parts.append(" ".join("".join(buf_en).lower()))
            buf_en.clear()

    def _flush_zh() -> None:
        if buf_zh:
            parts.extend(lazy_pinyin("".join(buf_zh), style=Style.NORMAL))
            buf_zh.clear()

    for ch in word:
        if "\u4e00" <= ch <= "\u9fff":
            _flush_en()
            buf_zh.append(ch)
        elif ch.isalnum():
            _flush_zh()
            buf_en.append(ch)
        else:
            _flush_en()
            _flush_zh()
    _flush_en()
    _flush_zh()
    return "".join(f"`{s}" for s in parts)


# ─────────────────────────────────────────────────────────
#  核心流程
# ─────────────────────────────────────────────────────────

def _build_segmenter(segmenter_type: str) -> Any:
    """
    切词器工厂。

    segmenter_type:
      "hanlp"  → HanlpSegmenter（本地模型，默认）
      "api"    → ApiSegmenter（外部 HTTP 接口）
    """
    strict_dict = _load_user_dict_words()
    sogou_dict  = _load_sogou_snapshot()
    event_verbs = _load_event_verbs()
    full_dict   = strict_dict | sogou_dict
    log.info(f"切词词典 {len(full_dict)} 词；主白名单 {len(strict_dict)} 词；"
             f"次白名单 {len(sogou_dict)} 词；事件动词 {len(event_verbs)} 词；"
             f"屏蔽词 {len(_BLOCK_WORDS)} 词")

    if segmenter_type == "api":
        from segmenter_api import ApiSegmenter           # type: ignore
        from tok_phrase_extractor import TokPhraseExtractor  # type: ignore
        extractor = TokPhraseExtractor(
            lexicon          = full_dict,
            keep_short_words = strict_dict,
            stopwords        = _STOP_WORDS,
            blocklist        = _BLOCK_WORDS,
        )
        seg = ApiSegmenter(
            stopwords        = _STOP_WORDS,
            blocklist        = _BLOCK_WORDS,
            phrase_extractor = extractor,
        )
        log.info("切词器: ApiSegmenter + TokPhraseExtractor（n-gram 短语合并）")
        return seg

    # 默认 hanlp
    from segmenter_hanlp import HanlpSegmenter  # type: ignore
    seg = HanlpSegmenter(
        stopwords=_STOP_WORDS,
        blocklist=_BLOCK_WORDS,
        user_dict=full_dict,
        keep_short_words=strict_dict,
        secondary_short_words=sogou_dict,
        event_verbs=event_verbs,
    )
    seg._ensure_loaded()
    log.info("切词器: HanlpSegmenter（本地 HanLP 模型）")
    return seg


def run_normalization(
    base_dir: str,
    out_dir: Optional[str] = None,
    mode: str = "phrase",
    target_date: Optional[str] = None,
    segmenter_type: str = "hanlp",
) -> Optional[str]:
    """
    完整归一化流程，返回输出文件路径；无数据时返回 None。

    Args:
        base_dir:       数据根目录
        out_dir:        输出目录（None → {base_dir}/rank）
        mode:           切词粒度
                          "phrase" → 事件级（默认）
                          "word"   → 词级
        segmenter_type: 切词器实现
                          "hanlp" → HanlpSegmenter（本地模型，默认）
                          "api"   → ApiSegmenter（外部 HTTP 接口）
    """
    log.info(f"── 扫描数据来源 (mode={mode}, segmenter={segmenter_type}, date={target_date or 'latest'}) ──")
    titles_by_src, files_by_src, items_by_src = scan_latest_per_source(base_dir, target_date=target_date)

    if not items_by_src:
        log.warning("无任何来源数据，跳过生成")
        return None

    segmenter = _build_segmenter(segmenter_type)

    # ApiSegmenter 支持批量预热：把所有标题一次性提交，减少网络 RTT
    if hasattr(segmenter, "warm"):
        all_titles = [
            item.word
            for items in items_by_src.values()
            for item in items
        ] + [
            title
            for titles in titles_by_src.values()
            for title in titles
        ]
        segmenter.warm(list(dict.fromkeys(all_titles)))  # 去重保序

    extract_fn = (
        segmenter.extract_phrases if mode == "phrase" else segmenter.extract
    )

    # ── Step 4：token 级跨平台加成 ──────────────────────────
    # 对每条标题，分词后各 token 继承该标题的 HotItem 分数
    # token → {src: best_info (by final)}
    token_src_best: Dict[str, Dict[str, Any]] = defaultdict(dict)

    for src, items in items_by_src.items():
        _tokens_fn = (lambda w: [w] if w not in _BLOCK_WORDS else []) \
            if src in NOCUT_SOURCES else extract_fn
        for item in items:
            for ng in _tokens_fn(item.word):
                prev = token_src_best[ng].get(src)
                if prev is None or item.final > prev["final"]:
                    token_src_best[ng][src] = {
                        "final":     item.final,
                        "s_norm":    item.s_norm,
                        "raw_heat":  item.raw_heat,
                        "rank":      item.rank,
                        "orig_term": item.word,
                    }

    # 不再使用跨平台 bonus：字符串匹配的"共振"信号不可靠（同义不同字会漏判、
    # 多义词会误判）。merged 直接等于 final，tier 完全由"来源内位置 × 平台权重"决定。
    norm_data: Dict[str, Dict[str, Any]] = {}
    for token, src_map in token_src_best.items():
        n_groups = len({PLATFORM_GROUP.get(s, s) for s in src_map})
        norm_data[token] = {}
        for src, info in src_map.items():
            norm_data[token][src] = {
                **info,
                "n_platforms": n_groups,
                "bonus":       1.0,
                "merged":      info["final"],
            }

    # ── 频次统计（所有来源） ─────────────────────────────────
    freq: Dict[str, int] = defaultdict(int)
    for src, titles in titles_by_src.items():
        _freq_fn = (lambda w: [w] if w not in _BLOCK_WORDS else []) \
            if src in NOCUT_SOURCES else extract_fn
        for title in titles:
            for ng in _freq_fn(title):
                freq[ng] += 1

    # ── 构建输出行：(word, src) → best norm info ─────────────
    out_rows: List[Tuple[str, str, str, str, float, float]] = []
    # word, pinyin, src_display, orig_term, final, merged
    for word, wnd in norm_data.items():
        if freq.get(word, 0) < MIN_FREQ:
            continue
        py = _pinyin(word)
        for src, info in wnd.items():
            out_rows.append((
                word,
                py,
                SOURCE_DISPLAY.get(src, src),
                info.get("orig_term", ""),
                info.get("final",     0.0),
                info.get("merged",    0.0),
            ))

    if not out_rows:
        log.warning("分词后无有效热词，跳过生成")
        return None

    out_rows.sort(key=lambda x: -x[5])

    # ── Step 5：absolute score（× 10^8 取整，不做全局 min-max）─
    # all_normalized = merged × 1e8 = s_norm × 平台权重 × 1e8
    # 由结构决定的绝对量，与当天词数和热度上限无关。
    def _total_norm_int(m: float) -> int:
        return int(round(m * 1e8))

    def _single_norm_int(final: float) -> int:
        return int(round(final * 1e8))

    # ── 写文件 ───────────────────────────────────────────────
    now_dt   = datetime.now()
    now_time = now_dt.strftime("%H%M%S")
    label    = target_date if target_date else now_dt.strftime("%Y%m%d")
    if out_dir is None:
        out_dir = os.path.join(base_dir, "rank")
    day_dir = os.path.join(out_dir, label)
    os.makedirs(day_dir, exist_ok=True)

    out_path = os.path.join(day_dir, f"rank_{label}_{now_time}.csv")
    total = len(out_rows)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("word\tpinyin\tsource\torig_term\tsingle_normalized\tall_normalized\theat_tier\n")
        for i, (word, py, src_d, orig, final, merged) in enumerate(out_rows):
            f.write(
                f"{word}\t{py}\t{src_d}\t{orig}\t"
                f"{_single_norm_int(final)}\t{_total_norm_int(merged)}\t{_heat_tier(_total_norm_int(merged))}\n"
            )

    log.info(f"输出完成: {out_path}  ({len(out_rows)} 行)")
    return out_path


# ─────────────────────────────────────────────────────────
#  定时循环（每小时 :19 / :49，无第三方调度库）
# ─────────────────────────────────────────────────────────

def _seconds_to_next_trigger() -> float:
    """计算距下一个 :19 或 :49 的秒数（最少 1 秒）。"""
    now = datetime.now()
    m, s = now.minute, now.second
    candidates = []
    for target in (19, 49):
        diff = (target - m) % 60
        if diff == 0 and s == 0:
            diff = 60
        elif diff == 0:
            diff = 60
        candidates.append(diff * 60 - s)
    wait = min(c for c in candidates if c > 0)
    return max(wait, 1.0)


def run_service(
    base_dir: str,
    out_dir: Optional[str],
    mode: str,
    segmenter_type: str = "hanlp",
) -> None:
    log.info(f"热词归一化服务启动，切词器={segmenter_type}，触发时间：每小时 :19 / :49")
    while True:
        wait = _seconds_to_next_trigger()
        next_dt = datetime.fromtimestamp(time.time() + wait)
        log.info(f"下次执行: {next_dt:%Y-%m-%d %H:%M:%S}  (等待 {wait:.0f}s)")
        time.sleep(wait)
        log.info("── 定时触发 ──")
        if segmenter_type == "both":
            for st in ("hanlp", "api"):
                _out = f"{out_dir}_{st}" if out_dir else None
                try:
                    run_normalization(base_dir, _out, mode=mode, target_date=None, segmenter_type=st)
                except Exception as exc:
                    log.error(f"执行失败 ({st}): {exc}", exc_info=True)
        else:
            try:
                run_normalization(base_dir, out_dir, mode=mode, target_date=None, segmenter_type=segmenter_type)
            except Exception as exc:
                log.error(f"执行失败: {exc}", exc_info=True)


# ─────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="热词归一化服务（每小时 :19 / :49 触发）"
    )
    parser.add_argument(
        "--once", action="store_true",
        help="立即执行一次后退出",
    )
    parser.add_argument(
        "--base-dir", default=DEFAULT_BASE_DIR,
        help=f"数据根目录（默认 {DEFAULT_BASE_DIR}）",
    )
    parser.add_argument(
        "--out-dir", default=None,
        help="输出目录（默认 <base-dir>/rank）",
    )
    parser.add_argument(
        "--mode", choices=["phrase", "word"], default="phrase",
        help="切词粒度：phrase=事件级（默认）/ word=词级",
    )
    parser.add_argument(
        "--date", default=None,
        help="历史日期 YYYYMMDD，指定后只用该日期的来源数据（回测用）",
    )
    parser.add_argument(
        "--segmenter", choices=["hanlp", "api", "both"], default="hanlp",
        help=(
            "切词器实现（默认 hanlp）：\n"
            "  hanlp — 本地 HanLP 模型\n"
            "  api   — 外部 HTTP 接口（kbd-api）\n"
            "  both  — 两者各跑一次，输出写到 <out-dir>_hanlp / <out-dir>_api"
        ),
    )
    args = parser.parse_args()

    if args.once:
        log.info(
            f"单次执行，base_dir={args.base_dir}, mode={args.mode}, "
            f"segmenter={args.segmenter}, date={args.date or 'latest'}"
        )
        if args.segmenter == "both":
            for st in ("hanlp", "api"):
                _out = f"{args.out_dir}_{st}" if args.out_dir else None
                run_normalization(args.base_dir, _out, mode=args.mode,
                                  target_date=args.date, segmenter_type=st)
        else:
            run_normalization(args.base_dir, args.out_dir, mode=args.mode,
                              target_date=args.date, segmenter_type=args.segmenter)
    else:
        run_service(args.base_dir, args.out_dir, args.mode, segmenter_type=args.segmenter)


if __name__ == "__main__":
    main()

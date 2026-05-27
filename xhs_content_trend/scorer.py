"""
Topic-level trend scoring for no-login XHS samples.

跟之前版本最大区别：
  - 不再做分词 / N-gram，避免产出 "好看" "推荐" "适合" 这种碎片词
  - 只识别完整 #话题#：来源是笔记结构化 tag_list + desc 和评论中
    "#XXX[话题]#" 形式的字面命中
  - 评分维度：覆盖、互动、推荐位、评论提及、加权曝光
  - 输出明确"是否热点话题"判定
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Sequence, Set


# 完整话题正则：兼容三种 XHS 序列化
#   1. "#某话题[话题]#"          —— 详情页/正文常见
#   2. "#某话题#"                —— 笔记 desc 偶尔出现的简单形式
#   3. tag_list 结构化字符串    —— noauth_fetcher 已提取为 list[str]
_TOPIC_FULL_RE = re.compile(r"#([^#\n\[]{1,30}?)\[话题\]#")
_TOPIC_PLAIN_RE = re.compile(r"#([^#\s\[\]\n]{2,30})#")

_HOT_THRESHOLD_SCORE = 40.0     # 综合分门槛
_HOT_THRESHOLD_NOTES = 2        # 至少出现在 N 篇笔记
_HOT_THRESHOLD_ENG = 1000       # 命中笔记互动总和门槛（粗筛极冷话题）


def _to_int(value) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    text = str(value).strip().lower().replace(",", "")
    if not text:
        return 0
    try:
        if "万" in text or "w" in text:
            return int(float(text.replace("万", "").replace("w", "")) * 10000)
        return int(float(text))
    except (TypeError, ValueError):
        return 0


def _normalize_topic(name: str) -> str:
    """规整话题字面值。"""
    if not name:
        return ""
    s = name.strip()
    s = re.sub(r"\[话题\]$", "", s)
    s = s.strip("#").strip()
    return s


def _is_valid_topic(name: str) -> bool:
    if not name:
        return False
    if len(name) < 2 or len(name) > 30:
        return False
    has_zh = any("\u4e00" <= c <= "\u9fff" for c in name)
    has_alnum = any(c.isalnum() for c in name)
    if not (has_zh or has_alnum):
        return False
    return True


def _extract_topics_from_text(text: str) -> List[str]:
    """从一段文本里抽取完整话题。返回保序去重后的话题列表。"""
    if not text:
        return []
    seen: Set[str] = set()
    out: List[str] = []
    for raw in _TOPIC_FULL_RE.findall(text):
        nm = _normalize_topic(raw)
        if _is_valid_topic(nm) and nm not in seen:
            out.append(nm)
            seen.add(nm)
    for raw in _TOPIC_PLAIN_RE.findall(text):
        # plain 形式 #XX# 容易误伤标签/表情；排除带方括号的
        if "[" in raw or "]" in raw:
            continue
        nm = _normalize_topic(raw)
        if _is_valid_topic(nm) and nm not in seen:
            out.append(nm)
            seen.add(nm)
    return out


def _extract_topics_from_note(note: Dict) -> List[str]:
    """整合结构化 tag_list + title + desc 中所有话题（保序去重）。"""
    seen: Set[str] = set()
    out: List[str] = []

    raw_tags = note.get("tag_list") or []
    if isinstance(raw_tags, list):
        for t in raw_tags:
            if isinstance(t, dict):
                nm = _normalize_topic(t.get("name") or "")
            else:
                nm = _normalize_topic(str(t))
            if _is_valid_topic(nm) and nm not in seen:
                out.append(nm)
                seen.add(nm)

    title = str(note.get("title") or note.get("display_title") or "")
    desc = str(note.get("desc") or "")
    for nm in _extract_topics_from_text(title) + _extract_topics_from_text(desc):
        if nm not in seen:
            out.append(nm)
            seen.add(nm)
    return out


def _engagement(note: Dict) -> int:
    liked = _to_int(note.get("liked_count"))
    comments = _to_int(note.get("comment_count"))
    collected = _to_int(note.get("collected_count"))
    shares = _to_int(note.get("share_count"))
    return int(liked + 2.0 * comments + 1.5 * collected + 3.0 * shares)


def _comment_texts(note: Dict) -> List[str]:
    comments = note.get("comments")
    if comments is None:
        comments = note.get("_raw_comments") or []
    out: List[str] = []
    for item in comments:
        if not isinstance(item, dict):
            continue
        content = item.get("content") or item.get("text") or ""
        content = str(content).strip()
        if content:
            out.append(content)
    return out


@dataclass
class TopicStats:
    topic: str
    note_ids: Set[str] = field(default_factory=set)
    engagement_sum_log: float = 0.0   # 互动数 log1p 累计（用于 max-min 归一）
    engagement_sum_raw: int = 0       # 原始互动累计（用于阈值/输出）
    exposure_sum: float = 0.0
    comment_mentions: int = 0
    same_note_repeat: int = 0
    evidence_titles: List[str] = field(default_factory=list)


@dataclass
class ScoredTopic:
    rank: int
    topic: str
    total_score: float
    is_hot: bool
    note_count: int
    engagement_sum: int
    coverage_score: float
    engagement_score: float
    exposure_score: float
    comment_score: float
    density_score: float
    comment_mentions: int
    evidence: str


class XhsTopicScorer:
    """聚合并打分笔记中的完整 #话题#。"""

    def __init__(self, limit: int = 50) -> None:
        self.limit = limit

    def score(self, notes: Sequence[Dict]) -> List[ScoredTopic]:
        stats: Dict[str, TopicStats] = {}
        if not notes:
            return []

        for idx, note in enumerate(notes, start=1):
            note_id = str(note.get("note_id") or note.get("id") or f"note-{idx}")
            title = str(note.get("title") or note.get("display_title") or "").strip()
            engagement = _engagement(note)
            exposure = 1.0 / math.log2(idx + 1.5)

            topic_counter: Counter = Counter()
            for nm in _extract_topics_from_note(note):
                topic_counter[nm] += 1

            for nm, count in topic_counter.items():
                st = stats.setdefault(nm, TopicStats(topic=nm))
                first_in_note = note_id not in st.note_ids
                st.note_ids.add(note_id)
                if first_in_note:
                    st.engagement_sum_log += math.log1p(max(engagement, 0))
                    st.engagement_sum_raw += max(engagement, 0)
                    st.exposure_sum += exposure
                    if title and title not in st.evidence_titles:
                        st.evidence_titles.append(title)
                st.same_note_repeat += max(count - 1, 0)

            for ctext in _comment_texts(note):
                for nm in _extract_topics_from_text(ctext):
                    st = stats.setdefault(nm, TopicStats(topic=nm))
                    st.comment_mentions += 1

        if not stats:
            return []

        values = list(stats.values())
        max_eng = max((s.engagement_sum_log for s in values), default=1.0) or 1.0
        max_cov = max((len(s.note_ids) for s in values), default=1) or 1
        max_exp = max((s.exposure_sum for s in values), default=1.0) or 1.0
        max_cmt = max((s.comment_mentions for s in values), default=1) or 1
        max_dens = max((s.same_note_repeat for s in values), default=1) or 1

        rows: List[ScoredTopic] = []
        for s in values:
            coverage_score = self._scale(len(s.note_ids), max_cov)
            engagement_score = self._scale(s.engagement_sum_log, max_eng)
            exposure_score = self._scale(s.exposure_sum, max_exp)
            comment_score = self._scale(s.comment_mentions, max_cmt)
            density_score = self._scale(s.same_note_repeat, max_dens)

            total = (
                0.30 * coverage_score
                + 0.30 * engagement_score
                + 0.20 * exposure_score
                + 0.15 * comment_score
                + 0.05 * density_score
            )
            is_hot = (
                total >= _HOT_THRESHOLD_SCORE
                and len(s.note_ids) >= _HOT_THRESHOLD_NOTES
                and s.engagement_sum_raw >= _HOT_THRESHOLD_ENG
            )

            rows.append(
                ScoredTopic(
                    rank=0,
                    topic=s.topic,
                    total_score=round(total, 2),
                    is_hot=is_hot,
                    note_count=len(s.note_ids),
                    engagement_sum=s.engagement_sum_raw,
                    coverage_score=round(coverage_score, 2),
                    engagement_score=round(engagement_score, 2),
                    exposure_score=round(exposure_score, 2),
                    comment_score=round(comment_score, 2),
                    density_score=round(density_score, 2),
                    comment_mentions=s.comment_mentions,
                    evidence=" | ".join(s.evidence_titles[:3]),
                )
            )

        rows.sort(
            key=lambda r: (r.total_score, r.note_count, r.engagement_sum),
            reverse=True,
        )
        for i, row in enumerate(rows, 1):
            row.rank = i
        return rows[: self.limit]

    @staticmethod
    def _scale(value: float, max_value: float) -> float:
        if max_value <= 0:
            return 0.0
        return max(0.0, min(100.0, 100.0 * value / max_value))


def rows_to_tsv(rows: Iterable[ScoredTopic]) -> str:
    header = [
        "排名",
        "话题",
        "综合分",
        "是否热点",
        "命中笔记数",
        "互动总和",
        "覆盖得分",
        "互动得分",
        "推荐位得分",
        "评论提及得分",
        "重复密度得分",
        "评论提及次数",
        "证据标题",
    ]
    lines = ["\t".join(header)]
    for row in rows:
        lines.append(
            "\t".join(
                [
                    str(row.rank),
                    row.topic,
                    f"{row.total_score:.2f}",
                    "是" if row.is_hot else "否",
                    str(row.note_count),
                    str(row.engagement_sum),
                    f"{row.coverage_score:.2f}",
                    f"{row.engagement_score:.2f}",
                    f"{row.exposure_score:.2f}",
                    f"{row.comment_score:.2f}",
                    f"{row.density_score:.2f}",
                    str(row.comment_mentions),
                    row.evidence.replace("\t", " "),
                ]
            )
        )
    return "\n".join(lines) + "\n"

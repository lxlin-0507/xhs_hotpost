"""
crawler/xhs_trend_proxy/extractor.py — 话题抽取薄包装（Phase 2 / 预留 Phase 5）

Phase 2 中直接复用 xhs_content_trend.scorer.XhsTopicScorer，
此模块作为占位入口，Phase 5 升级评分策略时在此扩展，
外部调用方无需修改导入路径。
"""
from __future__ import annotations

from typing import Dict, List

from xhs_content_trend.scorer import ScoredTopic, XhsTopicScorer, rows_to_tsv


def extract_topics(notes: List[Dict], limit: int = 50) -> List[ScoredTopic]:
    """
    从笔记列表中抽取并打分话题。

    Parameters
    ----------
    notes : List[Dict]
        来自 MultiProfileSampler.sample_all() 的笔记列表，
        每条笔记含 ``_profile_id`` 字段。
    limit : int
        话题榜最大条目数（默认 50）。

    Returns
    -------
    List[ScoredTopic]
        按综合分降序排列的话题列表。
    """
    scorer = XhsTopicScorer(limit=limit)
    return scorer.score(notes)


__all__ = ["extract_topics", "rows_to_tsv", "ScoredTopic"]

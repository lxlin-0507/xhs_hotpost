"""
hotwords_service/segmenter_base.py — 切词器协议

定义 Segmenter Protocol，HanlpSegmenter 和 ApiSegmenter 均隐式实现它，
run.py 只依赖这个协议——两种实现可随时互换，无需修改调用方。

契约：
  extract(title)         → List[str]   词级 token
  extract_phrases(title) → List[str]   短语级 token（语义更完整）
  warm(titles)           → None        可选：批量预取（API 实现用于减少 RTT）
"""
from __future__ import annotations

from typing import List, Protocol, runtime_checkable


@runtime_checkable
class Segmenter(Protocol):
    """切词器协议。所有实现必须满足相同的输入/输出形状。"""

    def extract(self, title: str) -> List[str]:
        """词级切分，返回候选热词列表（每项 2–8 字，已过滤停用词/黑名单）。"""
        ...

    def extract_phrases(self, title: str) -> List[str]:
        """短语级切分，返回事件性短语列表（每项 2–12 字，已过滤停用词/黑名单）。"""
        ...

    def warm(self, titles: List[str]) -> None:
        """
        批量预取，将 titles 的切词结果写入内部缓存。
        调用方在进入主循环前调用一次，后续 extract/extract_phrases 直接命中缓存。
        不支持批量的实现（如 HanlpSegmenter）提供空实现即可。
        """
        ...

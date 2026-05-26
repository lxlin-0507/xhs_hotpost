"""
hotwords_service/segmenter_api.py — 基于外部 HTTP API 的切词器

调用 kbd-api 的 /admin-tools/py/word/v1/segment/batch 接口，
实现与 HanlpSegmenter 相同的协议（segmenter_base.Segmenter）。

API 格式：
  POST  application/json
  body: ["title1", "title2", ...]
  resp: [{"tokens": ["tok1", "tok2"], "error": null}, ...]

设计原则：
  1. 批量优先 — warm(titles) 一次性发所有标题，结果写缓存；
     后续 extract/extract_phrases 只做 cache lookup，不再产生网络请求。
  2. 逐条降级 — 缓存 miss 时发单条请求，失败则返回空列表（不崩服务）。
  3. 过滤对齐 — 应用与 HanlpSegmenter 相同的 blocklist + stopwords + 长度规则，
     确保 A/B 对比时差异来自切词本身，而不是过滤逻辑不同。
  4. 无副作用 — 不依赖任何外部模型文件，可在无 GPU/无 hanlp 环境运行。

用法：
  from segmenter_api import ApiSegmenter
  seg = ApiSegmenter(stopwords, blocklist, api_url="http://...")
  seg.warm(["白鹿跑男争议 内娱综艺审美巨变", "特朗普抵达北京"])
  print(seg.extract_phrases("白鹿跑男争议 内娱综艺审美巨变"))
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Set

log = logging.getLogger("hotwords_service.api_seg")

_DEFAULT_URL = "http://kbd-api.oppo.dev/admin-tools/py/word/v1/segment/batch"

# 与 HanlpSegmenter 保持一致的长度限制
_MIN_LEN = 2
_MAX_LEN = 12   # phrase 模式上限（extract 用 8，但统一放宽到 12 由调用方再截）


class ApiSegmenter:
    """
    基于 HTTP API 的切词器，实现 segmenter_base.Segmenter 协议。

    Args:
        stopwords:  停用词集合，命中则丢弃该 token。
        blocklist:  黑名单集合，命中则丢弃该 token。
        api_url:    切词 API 地址（可通过环境变量 SEGMENT_API_URL 覆盖）。
        timeout:    单次请求超时秒数（默认 10s）。
        batch_size: warm() 单批最大条数（默认 200，与接口限制对齐）。
    """

    def __init__(
        self,
        stopwords: Optional[Set[str]] = None,
        blocklist: Optional[Set[str]] = None,
        api_url: Optional[str] = None,
        timeout: float = 10.0,
        batch_size: int = 200,
        phrase_extractor=None,
    ) -> None:
        """
        Args:
          phrase_extractor: 可选的 TokPhraseExtractor 实例。若传入，extract_phrases
                            会将 API 返回的 token 序列送进去做 n-gram 短语合并，
                            尽量恢复 HanLP Track A 的事件短语能力。
                            None 时退化为纯过滤（每个 token 独立成词）。
        """
        import os
        self.stopwords        = stopwords or set()
        self.blocklist        = blocklist or set()
        self.api_url          = api_url or os.getenv("SEGMENT_API_URL", _DEFAULT_URL)
        self.timeout          = timeout
        self.batch_size       = batch_size
        self.phrase_extractor = phrase_extractor
        # title → 原始 token 序列（保留顺序，用于 phrase_extractor）
        self._cache: Dict[str, List[str]] = {}

    # ─────────────────────────────────────────────────────────
    #  批量预热
    # ─────────────────────────────────────────────────────────

    def warm(self, titles: List[str]) -> None:
        """
        批量调用 API，将结果写入内部缓存。
        重复条目和已缓存条目自动跳过。
        """
        missing = list(dict.fromkeys(t for t in titles if t not in self._cache))
        if not missing:
            return

        total = len(missing)
        log.info(f"ApiSegmenter warm: {total} 条标题，批大小={self.batch_size}")
        success = 0
        for i in range(0, total, self.batch_size):
            batch = missing[i : i + self.batch_size]
            results = self._call_api(batch)
            for title, raw_tokens in zip(batch, results):
                # 缓存保留原始 token 顺序（不在此处过滤），让 phrase_extractor 走完整流程
                self._cache[title] = [str(t).strip() for t in (raw_tokens or []) if t]
                success += 1
        log.info(f"ApiSegmenter warm 完成: {success}/{total} 条缓存")

    # ─────────────────────────────────────────────────────────
    #  Segmenter Protocol
    # ─────────────────────────────────────────────────────────

    def extract(self, title: str) -> List[str]:
        """词级：取 API token，做基础过滤，截到 8 字。"""
        tokens = self._get_or_fetch(title)
        return [t for t in self._filter(tokens) if len(t) <= 8]

    def extract_phrases(self, title: str) -> List[str]:
        """
        短语级：
          - 若注入了 phrase_extractor，走 n-gram 合并流程（恢复事件短语能力）
          - 否则退化为基础过滤（每个 token 独立成词）
        """
        tokens = self._get_or_fetch(title)
        if self.phrase_extractor is not None:
            return self.phrase_extractor.extract_phrases(tokens)
        return [t for t in self._filter(tokens) if len(t) <= _MAX_LEN]

    # ─────────────────────────────────────────────────────────
    #  内部工具
    # ─────────────────────────────────────────────────────────

    def _get_or_fetch(self, title: str) -> List[str]:
        """
        缓存命中直接返回原始 token 序列；miss 则发单条请求并写入缓存。
        注意：返回的是 API 原始顺序的 token，不做 stopwords/blocklist 过滤——
        过滤逻辑由调用方（_filter）或 phrase_extractor 完成。
        """
        if title in self._cache:
            return self._cache[title]
        results = self._call_api([title])
        tokens = [str(t).strip() for t in (results[0] if results else []) if t]
        self._cache[title] = tokens
        return tokens

    def _call_api(self, titles: List[str]) -> List[List[str]]:
        """
        发 HTTP POST，返回与 titles 等长的 token 列表数组。
        任何网络/解析错误均返回等长的空列表数组，不抛异常。
        """
        body = json.dumps(titles, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.api_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            log.warning(f"ApiSegmenter 请求失败: {exc}")
            return [[] for _ in titles]

        # 解析响应：[{"tokens": [...], "error": null}, ...]
        out: List[List[str]] = []
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                log.warning(f"ApiSegmenter 响应[{i}] 格式异常: {item!r}")
                out.append([])
                continue
            if item.get("error"):
                log.warning(f"ApiSegmenter 响应[{i}] 含错误: {item['error']!r}")
            out.append(item.get("tokens") or [])

        # 防御：响应条数不足时补空列表
        while len(out) < len(titles):
            out.append([])
        return out

    def _filter(self, raw_tokens: List[str]) -> List[str]:
        """
        对 API 返回的 raw tokens 做与 HanLP 路径一致的后处理：
          1. 长度 [MIN_LEN, MAX_LEN]
          2. 不在 stopwords
          3. 不在 blocklist
          4. 非纯 ASCII（去掉英文单词残片）
          5. 非纯数字
        """
        result = []
        for tok in raw_tokens:
            if not isinstance(tok, str):
                continue
            tok = tok.strip()
            if len(tok) < _MIN_LEN or len(tok) > _MAX_LEN:
                continue
            if tok in self.stopwords:
                continue
            if tok in self.blocklist:
                continue
            if tok.isascii():
                continue
            if tok.isdigit():
                continue
            result.append(tok)
        return result

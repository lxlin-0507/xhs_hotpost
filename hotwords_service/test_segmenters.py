"""
hotwords_service/test_segmenters.py — 切词器最小测试集

两类测试：
  A. ApiSegmenter 单元测试（mock HTTP，本地可运行）
     - 过滤规则：blocklist / stopwords / 长度 / 纯ASCII / 纯数字
     - warm 批量预热缓存行为
     - API 超时 / 错误响应降级
     - Protocol 接口合规（segmenter_base.Segmenter）

  B. 切词质量对比（需 API 可达，有网环境运行）
     标注用例（LABELED_CASES）：人工标注"必须包含"和"不应出现"的 token，
     同时对两种实现运行，在 CI/人工审查时一眼看出差异。

运行：
  # 单元测试（本地，无网络）
  python -m pytest hotwords_service/test_segmenters.py -v -k "not live"

  # 含切词质量对比（需 API 可达）
  python -m pytest hotwords_service/test_segmenters.py -v

最小通过标准（见 TEST_CRITERIA 注释）：
  1. 形状合规：所有 token 2 ≤ len ≤ 12，无停用词，无黑名单词，非纯ASCII，非纯数字
  2. 关键词覆盖：标注用例的 must_contain 至少命中 1 个 token
  3. 黑名单拦截：标注用例的 must_not 全部不出现
  4. 降级安全：API 挂掉时返回空列表而非抛出异常
  5. warm 缓存：warm 之后同标题不再触发网络请求
"""
from __future__ import annotations

import json
import sys
import unittest
import urllib.error
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

# ── 路径 ──────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from segmenter_api import ApiSegmenter
from segmenter_base import Segmenter

# ─────────────────────────────────────────────────────────
#  测试数据
# ─────────────────────────────────────────────────────────

_STOPWORDS = {"的", "了", "是", "在", "和", "啊", "都是", "此"}
_BLOCKLIST  = {"广告", "推广"}

# 标注用例：
#   title        - 输入标题
#   must_contain - 至少有一个出现在结果中（列表中任意一个命中即通过）
#   must_not     - 所有这些都不应出现（任何一个出现即失败）
LABELED_CASES = [
    {
        "title": "经典电视剧奋斗发动机佛教啊东方都是风景",
        "must_contain": [["经典", "电视剧", "奋斗", "发动机"]],  # 每个子列表"至少命中1个"
        "must_not": ["啊", "都是"],   # 停用词不应出现
    },
    {
        "title": "白鹿跑男争议 内娱综艺审美巨变",
        "must_contain": [["白鹿", "跑男", "争议"], ["内娱", "综艺"]],
        "must_not": [],
    },
    {
        "title": "特朗普抵达北京 中美关系",
        "must_contain": [["特朗普", "抵达", "北京"], ["中美", "中美关系"]],
        "must_not": [],
    },
    {
        "title": "广告推广",   # 全黑名单
        "must_contain": [],    # 什么都不应产出
        "must_not": ["广告", "推广"],
    },
    {
        "title": "",           # 空字符串边界
        "must_contain": [],
        "must_not": [],
    },
    {
        "title": "AI",         # 纯 ASCII 短词
        "must_contain": [],
        "must_not": ["AI"],
    },
]


# ─────────────────────────────────────────────────────────
#  辅助：构造 mock HTTP 响应
# ─────────────────────────────────────────────────────────

def _mock_response(titles: List[str], token_map: dict):
    """
    根据 token_map（title→tokens）构造 mock urlopen 返回值。
    缺失的 title 返回空 tokens。
    """
    payload = [
        {"tokens": token_map.get(t, []), "error": None}
        for t in titles
    ]
    raw = json.dumps(payload, ensure_ascii=False).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = raw
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


# ─────────────────────────────────────────────────────────
#  A. 单元测试（本地可运行，mock HTTP）
# ─────────────────────────────────────────────────────────

class TestApiSegmenterUnit(unittest.TestCase):

    def _seg(self, **kwargs) -> ApiSegmenter:
        return ApiSegmenter(
            stopwords=_STOPWORDS,
            blocklist=_BLOCKLIST,
            **kwargs,
        )

    # ── A1. Protocol 合规 ─────────────────────────────────

    def test_implements_protocol(self):
        """ApiSegmenter 应满足 Segmenter Protocol。"""
        seg = self._seg()
        self.assertIsInstance(seg, Segmenter)

    # ── A2. 过滤规则 ─────────────────────────────────────

    def test_filter_blocklist(self):
        """黑名单词必须被过滤。"""
        seg = self._seg()
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _mock_response(
                ["测试广告内容"],
                {"测试广告内容": ["测试", "广告", "内容"]},
            )
            result = seg.extract("测试广告内容")
        self.assertNotIn("广告", result)
        self.assertIn("测试", result)

    def test_filter_stopwords(self):
        """停用词必须被过滤。"""
        seg = self._seg()
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _mock_response(
                ["今天的天气很好"],
                {"今天的天气很好": ["今天", "的", "天气", "很好"]},
            )
            result = seg.extract("今天的天气很好")
        self.assertNotIn("的", result)

    def test_filter_pure_ascii(self):
        """纯 ASCII token 应被过滤。"""
        seg = self._seg()
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _mock_response(
                ["DeepSeek大模型"],
                {"DeepSeek大模型": ["DeepSeek", "大模型"]},
            )
            result = seg.extract("DeepSeek大模型")
        self.assertNotIn("DeepSeek", result)
        self.assertIn("大模型", result)

    def test_filter_length(self):
        """token 长度不在 [2, 12] 范围内的应被过滤。"""
        seg = self._seg()
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _mock_response(
                ["长度测试"],
                {"长度测试": ["我", "好", "北京奥运会开幕式精彩瞬间回顾大全", "人工智能"]},
            )
            result = seg.extract("长度测试")
        self.assertNotIn("我", result)          # 1 字
        self.assertNotIn("好", result)          # 1 字
        self.assertIn("人工智能", result)       # 4 字，合法

    def test_extract_phrases_allows_longer(self):
        """extract_phrases 允许最长 12 字，extract 只允许最长 8 字。"""
        seg = self._seg()
        long_token = "中美贸易战最新进展"  # 9 字
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _mock_response(
                ["中美贸易战最新进展"],
                {"中美贸易战最新进展": [long_token]},
            )
            phrases = seg.extract_phrases("中美贸易战最新进展")
        self.assertIn(long_token, phrases)

        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _mock_response(
                ["中美贸易战最新进展"],
                {"中美贸易战最新进展": [long_token]},
            )
            words = seg.extract("中美贸易战最新进展")
        self.assertNotIn(long_token, words)   # extract 上限 8 字

    # ── A3. warm 缓存 ────────────────────────────────────

    def test_warm_populates_cache(self):
        """warm 之后，extract 不再触发网络请求。"""
        seg = self._seg()
        titles = ["北京奥运", "上海车展"]
        token_map = {"北京奥运": ["北京", "奥运"], "上海车展": ["上海", "车展"]}

        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _mock_response(titles, token_map)
            seg.warm(titles)
            call_count_after_warm = mock_open.call_count  # 应该是 1（一次 batch）

        with patch("urllib.request.urlopen") as mock_open2:
            seg.extract("北京奥运")
            seg.extract_phrases("上海车展")
            self.assertEqual(mock_open2.call_count, 0, "命中缓存后不应再发请求")

        self.assertEqual(call_count_after_warm, 1)

    def test_warm_deduplicates(self):
        """warm 对重复 title 只请求一次。"""
        seg = self._seg()
        titles = ["北京奥运", "北京奥运", "北京奥运"]
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _mock_response(["北京奥运"], {"北京奥运": ["北京", "奥运"]})
            seg.warm(titles)
        self.assertEqual(mock_open.call_count, 1)

    # ── A4. 降级安全 ─────────────────────────────────────

    def test_network_error_returns_empty(self):
        """网络异常时返回空列表，不抛异常。"""
        seg = self._seg()
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")):
            result = seg.extract("网络异常测试")
        self.assertEqual(result, [])

    def test_malformed_response_returns_empty(self):
        """API 返回非法 JSON 时返回空列表，不抛异常。"""
        seg = self._seg()
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not json"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = seg.extract("非法响应测试")
        self.assertEqual(result, [])

    def test_api_error_field_returns_empty_tokens(self):
        """API 返回 error 字段非空时，该条返回空列表，整体不崩溃。"""
        seg = self._seg()
        payload = [{"tokens": [], "error": "segmentation failed"}]
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(payload).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = seg.extract("错误字段测试")
        self.assertEqual(result, [])

    # ── A5. 空输入边界 ────────────────────────────────────

    def test_empty_title(self):
        """空字符串输入返回空列表。"""
        seg = self._seg()
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _mock_response([""], {"": []})
            result = seg.extract("")
        self.assertEqual(result, [])


# ─────────────────────────────────────────────────────────
#  B. 切词质量对比（需 API 可达）
#  运行：pytest -v -k "live"
# ─────────────────────────────────────────────────────────

class TestSegmenterQualityLive(unittest.TestCase):
    """
    需要 kbd-api 可达的切词质量测试。
    pytest 默认跳过（标记为 live），需显式 -k live 运行。

    最小通过标准：
      1. must_contain 的每个子列表至少命中 1 个 token
      2. must_not 中的词全部不出现
      3. 所有 token 满足形状约束（长度/编码/黑名单）
    """

    @classmethod
    def setUpClass(cls):
        """预热：把所有标注用例标题一次性 batch 提交。"""
        cls.seg = ApiSegmenter(stopwords=_STOPWORDS, blocklist=_BLOCKLIST)
        titles = [c["title"] for c in LABELED_CASES if c["title"]]
        try:
            cls.seg.warm(titles)
            cls._available = True
        except Exception:
            cls._available = False

    def setUp(self):
        if not self._available:
            self.skipTest("API 不可达，跳过 live 测试")

    def _check_shape(self, tokens: List[str], title: str):
        """形状约束：通用断言。"""
        for tok in tokens:
            self.assertGreaterEqual(len(tok), 2, f"[{title}] token 过短: {tok!r}")
            self.assertLessEqual(len(tok), 12, f"[{title}] token 过长: {tok!r}")
            self.assertFalse(tok.isascii(), f"[{title}] 纯 ASCII token: {tok!r}")
            self.assertFalse(tok.isdigit(), f"[{title}] 纯数字 token: {tok!r}")
            self.assertNotIn(tok, _STOPWORDS, f"[{title}] 停用词未过滤: {tok!r}")
            self.assertNotIn(tok, _BLOCKLIST, f"[{title}] 黑名单词未过滤: {tok!r}")

    def _run_case(self, case: dict):
        title = case["title"]
        tokens = self.seg.extract_phrases(title)

        # 形状约束
        self._check_shape(tokens, title)

        # must_not
        for word in case.get("must_not", []):
            self.assertNotIn(word, tokens, f"[{title}] 黑名单/停用词出现了: {word!r}")

        # must_contain：每个子列表至少命中 1 个
        for group in case.get("must_contain", []):
            hit = any(w in tokens for w in group)
            self.assertTrue(
                hit,
                f"[{title}] 期望词组 {group} 中至少 1 个出现在结果 {tokens} 里"
            )

    def test_labeled_cases(self):
        for case in LABELED_CASES:
            with self.subTest(title=case["title"]):
                self._run_case(case)

    def test_top_overlap_with_hanlp(self):
        """
        与 HanLP 对比：同一批标题，两者输出的 token 集合 Jaccard 相似度 > 0.3。
        （阈值宽松，目的是检测严重异常，不是要求完全一致）

        本测试仅在 hanlp 可用时运行（服务器环境）。
        """
        try:
            import sys
            from pathlib import Path
            _here = Path(__file__).resolve().parent
            if str(_here) not in sys.path:
                sys.path.insert(0, str(_here))
            from segmenter_hanlp import HanlpSegmenter
        except Exception:
            self.skipTest("HanlpSegmenter 不可用，跳过对比测试")

        titles = [c["title"] for c in LABELED_CASES if c["title"]]
        hanlp_seg = HanlpSegmenter(stopwords=_STOPWORDS, blocklist=_BLOCKLIST)
        hanlp_seg._ensure_loaded()

        api_tokens: set  = set()
        hanlp_tokens: set = set()
        for t in titles:
            api_tokens.update(self.seg.extract_phrases(t))
            hanlp_tokens.update(hanlp_seg.extract_phrases(t))

        if not api_tokens and not hanlp_tokens:
            return  # 两者都空，跳过

        jaccard = len(api_tokens & hanlp_tokens) / len(api_tokens | hanlp_tokens)
        print(f"\nJaccard(api∩hanlp / api∪hanlp) = {jaccard:.3f}")
        print(f"仅 API 有: {api_tokens - hanlp_tokens}")
        print(f"仅 HanLP 有: {hanlp_tokens - api_tokens}")
        self.assertGreater(jaccard, 0.3, "Jaccard 相似度过低，切词结果差异太大")


if __name__ == "__main__":
    unittest.main()

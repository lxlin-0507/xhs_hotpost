"""
hotwords_service/segmenter_hanlp.py — HanLP 2.x 切词器

设计目标：替代 run.py 中的 jieba + A1~A7 启发式规则集（共 ~600 行）。

核心思路：
  ① 分隔符切片（保住微博热搜里的空格 / # / 顿号 等天然语义边界）
  ② HanLP 多任务模型一次产出：分词 / 词性 / 命名实体
  ③ NER 实体直接收下（人名/机构/地名）；普通词按长度+停用词过滤
  ④ 不再有：A1~A7 / 链合并 / 末尾补齐 / 动词截断 / 阈值校准

模型选择：
  HanLP 2.x 纯 Python 版（基于 PyTorch），无 Java 依赖。
  默认使用粗粒度分词模型 + NER 模型，CPU 推理 ~10-20 ms/句。

用法：
  from segmenter_hanlp import HanlpSegmenter
  seg = HanlpSegmenter(stopwords, blocklist)
  candidates = seg.extract("白鹿跑男争议 内娱综艺审美巨变")
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import List, Optional, Set

log = logging.getLogger("hotwords_service.hanlp")

# ── HANLP_HOME 路径策略 ──────────────────────────────────
# 优先级：
#   1. 已显式设置的 HANLP_HOME 环境变量 → 尊重，不动
#   2. 项目自带的 hotwords_service/hanlp/ → 自动指向（生产部署默认）
#   3. 都没有 → fallback 到 ~/.hanlp（hanlp 默认行为）
# 必须在 `import hanlp` 之前设置 HANLP_HOME 才有效。
_PROJECT_HANLP_HOME = Path(__file__).resolve().parent / "hanlp"
if "HANLP_HOME" not in os.environ and _PROJECT_HANLP_HOME.is_dir():
    os.environ["HANLP_HOME"] = str(_PROJECT_HANLP_HOME)

# 必需模型的相对路径（用于本地存在性检查，禁止意外联网下载）
_REQUIRED_MODEL_DIRS = (
    "mtl/close_tok_pos_ner_srl_dep_sdp_con_electra_small_20210111_124159",
    "transformers/electra_zh_small_20210706_125427",
)

# ── 超参数（与 run.py 保持一致）─────────────────────────────
MIN_LEN = 2
MAX_LEN = 8

# 分隔符切片：只在"强语义边界"处切断，保留词内标点让 HanLP 自己处理。
#
# 切断（强边界）：空白 / 括号书名号引号 / #@ / 全角叹问逗顿号句号分号 / 省略破折
# 保留（词内）：: . / · % & - + = 等嵌在词或数字里的字符
#   → 3:0 / 102:94 / 7.58亿 / P/E / 罗纳尔多·纳扎里奥 不被切断
_RE_SPLIT = re.compile(
    "["
    r"\s"                            # 空白（含换行）
    "\uff08\uff09"                   # （）全角括号
    "\u3010\u3011"                   # 【】
    "\u300a\u300b"                   # 《》
    "\u300c\u300d"                   # 「」
    "\u300e\u300f"                   # 『』
    "\u2018\u2019\u201c\u201d"       # ''""
    "\u2026\u2014"                   # …—
    "\uff01\uff1f\uff0c\u3001"       # ！？，、
    "\uff1b\u3002"                   # ；。
    r"#@\[\]{}\\<>|"                 # ASCII 强边界符（] 转义，< > | 也切断）
    "]+"
)

# HanLP 词性标签（PKU 体系）
# 命名实体类型：直接作为热词收下
_ENTITY_TAGS = {
    "nr",   # 人名
    "nrf",  # 音译人名
    "nrj",  # 日语人名
    "ns",   # 地名
    "nsf",  # 音译地名
    "nt",   # 机构团体
    "ntc",  # 公司名
    "ntcf", # 外国公司
    "nz",   # 其他专名
    "j",    # 简称略语（央视/北大/国足；dict_force 有时合并成 j）
    "nx",   # 外来词/外文品牌（DeepSeek/ChatGPT/iPhone）
}

# 黑名单词性：明确丢弃的功能/虚词（其余一律保留）
# 热榜短文本里 v/vn/an/a 经常承载实义（争议/招募/巨变/精彩），
# 不能像 jieba 链路那样靠词频拦——HanLP 标注本身已经够准。
# 反过来用黑名单：只过滤虚词、数量词、代词等明确无信息量的类别。
_FUNC_TAGS = {
    "u",    # 助词（的/了/着/过）
    "ude1", "ude2", "ude3", "udeng", "udh", "uguo", "ule", "ulian", "uls", "usuo", "uyy", "uzhe", "uzhi",
    "p",    # 介词
    "pba", "pbei",
    "c",    # 连词
    "cc",
    "d",    # 副词
    "dl",
    "r",    # 代词
    "rg", "rr", "ry", "rys", "ryt", "ryv", "rz", "rzs", "rzt", "rzv",
    "m",    # 数词
    "mq",
    "q",    # 量词
    "qg", "qt", "qv",
    "y",    # 语气词
    "e",    # 叹词
    "o",    # 拟声词
    "w",    # 标点（理论上分隔符已过滤，兜底）
    "x",    # 字符串
    "xx", "xu",
    "h",    # 前缀
    "k",    # 后缀
    "f",    # 方位词
    "t",    # 时间词（"今天/明天"等通用，无热点价值）
    "tg",
}


class HanlpSegmenter:
    """HanLP 切词器，懒加载模型。"""

    def __init__(
        self,
        stopwords: Optional[Set[str]] = None,
        blocklist: Optional[Set[str]] = None,
        user_dict: Optional[Set[str]] = None,
        keep_short_words: Optional[Set[str]] = None,
        secondary_short_words: Optional[Set[str]] = None,
        event_verbs: Optional[Set[str]] = None,
        model_name: str = "COARSE_ELECTRA_SMALL_ZH",
    ):
        """
        Args:
            stopwords: 停用词集合（与 run.py 复用）
            blocklist: 输出黑名单（与 run.py 复用）
            user_dict: 注入 HanLP dict_force 的"切词提示词典"。
                可以包含搜狗等大词库——目的只是让 HanLP 不要把这些词切碎，
                **不**保证它们一定出现在最终输出。
            keep_short_words: <3 字单 token 短词的"主白名单"。
                这里的词（C罗/曼联/抖音/塌房...）作为单 token 无条件放行。
                推荐传入 user_dict.txt 的手工策展集。
            secondary_short_words: <3 字单 token 短词的"次白名单"。
                这里的词（搜狗等大词库）放行需要额外条件：**不能是 HanLP
                识别出的 NER 实体**——即不是 PERSON/LOCATION/ORGANIZATION。
                目的：让"拼豆/家崽/狗法"等小众非实体词通过；同时拦下
                "中国/美国/王菲/北京"等出现在搜狗里的常见 NER 泛词。
            event_verbs: 事件性双字动词白名单（夺冠/抵达/曝光/暴发/偷拍...）。
                HanLP 三套词性标签都不区分"事件动词"vs"介词性动词"vs"通用
                动作动词"——它们都被标成 v。本集合内的词在切词流中被强制
                按 vn（名动词）处理：直接进 buf 当短语一部分，不作切点。
                目的：恢复"零封日本/暴发疫情/特朗普抵达北京"类事件短语。
            model_name: HanLP 模型名。可选：
                COARSE_ELECTRA_SMALL_ZH  粗粒度，速度快，推荐
                FINE_ELECTRA_SMALL_ZH    细粒度，召回高
        """
        self.stopwords = stopwords or set()
        self.blocklist = blocklist or set()
        self.user_dict = user_dict or set()
        self.keep_short_words = (
            keep_short_words if keep_short_words is not None else self.user_dict
        )
        self.secondary_short_words = secondary_short_words or set()
        self.event_verbs = event_verbs or set()
        self.model_name = model_name
        self._pipeline = None  # 懒加载
        self._jieba = None     # 懒加载 jieba 兜底
        self._fail_count = 0
        self._total_count = 0

    # ─────────────────────────────────────────────────
    #  模型加载
    # ─────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        """首次调用时加载 HanLP 多任务 pipeline（分词 + 词性 + NER）。"""
        if self._pipeline is not None:
            return

        # ① 预检查：必需模型是否已经在本地（禁止意外联网下载）
        hanlp_home = Path(os.environ.get("HANLP_HOME", os.path.expanduser("~/.hanlp")))
        missing = [
            d for d in _REQUIRED_MODEL_DIRS
            if not (hanlp_home / d).is_dir()
        ]
        if missing:
            raise RuntimeError(
                "HanLP 模型缺失，禁止自动联网下载。\n"
                f"  HANLP_HOME = {hanlp_home}\n"
                f"  缺失目录:\n"
                + "".join(f"    - {hanlp_home / d}\n" for d in missing)
                + "请按以下任一方式提供模型：\n"
                "  1. 本仓库已自带模型，确保 hotwords_service/hanlp/ 完整提交并可读\n"
                "  2. 手动下载（联网机器执行一次即可）：\n"
                f"     mkdir -p {hanlp_home}\n"
                f"     export HANLP_HOME={hanlp_home}\n"
                "     python3 -c 'import hanlp; "
                "hanlp.load(hanlp.pretrained.mtl."
                "CLOSE_TOK_POS_NER_SRL_DEP_SDP_CON_ELECTRA_SMALL_ZH)'\n"
            )

        try:
            import hanlp  # type: ignore
        except ImportError:
            raise RuntimeError("hanlp 未安装。请执行：pip install hanlp>=2.1.0")

        log.info(
            f"正在加载 HanLP 模型: {self.model_name}（HANLP_HOME={hanlp_home}）"
        )
        try:
            mtl_model = getattr(
                hanlp.pretrained.mtl,
                "CLOSE_TOK_POS_NER_SRL_DEP_SDP_CON_ELECTRA_SMALL_ZH",
            )
        except AttributeError:
            raise RuntimeError(
                "hanlp 安装不完整或版本过旧，缺少 pretrained 模型常量。\n"
                "请重新安装：pip install -U hanlp>=2.1.0"
            )
        try:
            self._pipeline = hanlp.load(mtl_model)
        except Exception as exc:
            raise RuntimeError(
                f"HanLP 模型加载失败：{exc}\n"
                f"  HANLP_HOME = {hanlp_home}\n"
                "  本地模型存在但加载失败，可能是文件损坏或权限问题。"
            ) from exc

        # 注入用户词典（HanLP 2.x 通过 dict_force / dict_combine 接口）
        if self.user_dict:
            try:
                tok = self._pipeline["tok/fine"] if "tok/fine" in self._pipeline.tasks \
                    else self._pipeline["tok"]
                tok.dict_force = set(self.user_dict)
                log.info(f"已注入用户词典 {len(self.user_dict)} 词")
            except Exception as exc:
                log.warning(f"用户词典注入失败（不影响主流程）: {exc}")

        log.info("HanLP 模型加载完成")

    # ─────────────────────────────────────────────────
    #  jieba 兜底（HanLP 推理失败时调用）
    # ─────────────────────────────────────────────────

    def _ensure_jieba(self) -> None:
        """懒加载 jieba，并把 user_dict 注入为整词。"""
        if self._jieba is not None:
            return
        try:
            import jieba  # type: ignore
        except ImportError:
            log.warning("jieba 未安装，HanLP 失败时无法兜底；建议 pip install jieba")
            self._jieba = False
            return
        for w in self.user_dict:
            try:
                jieba.add_word(w)
            except Exception:
                pass
        self._jieba = jieba
        log.info(f"jieba 兜底已就绪，已注入 {len(self.user_dict)} 词")

    def _record_fail(self) -> None:
        self._fail_count += 1
        if self._total_count and self._fail_count * 20 >= self._total_count:
            log.error(
                f"HanLP 失败率异常: {self._fail_count}/{self._total_count}"
                f" (>5%)，请检查模型/环境"
            )

    def _jieba_fallback_words(self, title: str) -> List[str]:
        """jieba 兜底版 extract：给到与 HanLP 主路径一致的过滤标准。"""
        self._ensure_jieba()
        if not self._jieba:
            return []
        candidates: List[str] = []
        seen: Set[str] = set()
        for tok in self._jieba.cut(title):
            tok = tok.strip()
            if not tok or tok in seen:
                continue
            if tok in self.stopwords or tok in self.blocklist:
                continue
            if not (MIN_LEN <= len(tok) <= MAX_LEN):
                if len(tok) < 3 and tok in self.keep_short_words:
                    pass
                else:
                    continue
            if tok.isdigit() or tok.isascii():
                if tok not in self.keep_short_words:
                    continue
            seen.add(tok)
            candidates.append(tok)
        return _dedupe_subsumption(candidates)

    def _jieba_fallback_phrases(
        self, title: str, min_len: int, max_len: int
    ) -> List[str]:
        """jieba 兜底版 extract_phrases：用 user_dict 命中作伪 NER + event_verbs 切短语。"""
        self._ensure_jieba()
        if not self._jieba:
            return []
        segments = [s for s in _RE_SPLIT.split(title) if s]
        if not segments:
            return []
        phrases: List[str] = []
        seen: Set[str] = set()

        def _commit(buf: List[str]) -> None:
            if not buf:
                return
            phrase = "".join(buf)
            if not (min_len <= len(phrase) <= max_len):
                return
            if phrase in self.stopwords or phrase in self.blocklist or phrase in seen:
                return
            if len(buf) == 1 and len(phrase) < 3:
                if phrase not in self.keep_short_words \
                        and phrase not in self.secondary_short_words:
                    return
            digit_count = sum(1 for c in phrase if c.isdigit())
            if digit_count * 2 >= len(phrase):
                return
            if phrase.isascii() and phrase not in self.keep_short_words:
                return
            seen.add(phrase)
            phrases.append(phrase)

        for seg in segments:
            buf: List[str] = []
            for tok in self._jieba.cut(seg):
                tok = tok.strip()
                if not tok:
                    continue
                if tok in self.stopwords:
                    _commit(buf)
                    buf = []
                    continue
                if tok in self.event_verbs:
                    buf.append(tok)
                    continue
                if tok in self.user_dict or len(tok) >= 2:
                    buf.append(tok)
                    continue
                _commit(buf)
                buf = []
            _commit(buf)

        return _dedupe_subsumption(phrases)

    # ─────────────────────────────────────────────────
    #  抽取候选热词
    # ─────────────────────────────────────────────────

    def extract(self, title: str) -> List[str]:
        """
        从单条标题抽取热词候选。

        流程：
          ① 分隔符切片（保住空格/# 等天然边界）
          ② HanLP 一次产出 tok + pos + ner
          ③ NER 实体优先；普通名词按长度过滤
          ④ stopwords / blocklist / 长度边界
        """
        self._ensure_loaded()
        self._total_count += 1

        # ① 分隔符切片
        segments = [s for s in _RE_SPLIT.split(title) if s]
        if not segments:
            return []

        candidates: List[str] = []
        seen: Set[str] = set()

        def _add(word: str) -> None:
            if not word or word in seen:
                return
            if word in self.stopwords or word in self.blocklist:
                return
            if not (MIN_LEN <= len(word) <= MAX_LEN):
                return
            if word.isdigit() or word.isascii():
                return
            seen.add(word)
            candidates.append(word)

        # ② HanLP 多任务推理：批量处理所有片段
        # ⚠️ 关键：HanLP 2.x 中 pos/pku 对齐 tok/fine，pos/ctb 对齐 tok/coarse。
        #   早期实现错用 tok/coarse + pos/pku 导致序列长度不一致，zip 截断丢词。
        #   统一用 fine + pku：召回更高，且专名/机构名靠 ner/msra 兜底。
        try:
            doc = self._pipeline(segments, tasks=["tok/fine", "pos/pku", "ner/msra"])
        except Exception as exc:
            self._record_fail()
            log.warning(
                f"HanLP 推理失败，jieba 降级: {exc}\n"
                f"  title={title!r}\n  segments={segments!r}",
                exc_info=True,
            )
            return self._jieba_fallback_words(title)

        tokens_list = doc.get("tok/fine") or doc.get("tok") or []
        pos_list    = doc.get("pos/pku")  or doc.get("pos") or []
        ner_list    = doc.get("ner/msra") or doc.get("ner") or []

        # ③ 收集 NER 实体（最高优先级）
        # ner_list[i] = [(entity_text, entity_type, start, end), ...]
        for seg_ner in ner_list:
            for ent in seg_ner:
                if isinstance(ent, (list, tuple)) and len(ent) >= 1:
                    _add(str(ent[0]).strip())

        # ④ 收集 token：黑名单词性丢弃，其余按长度/停用词过滤
        for tokens, tags in zip(tokens_list, pos_list):
            for tok, tag in zip(tokens, tags):
                tok = tok.strip()
                if not tok:
                    continue
                tag_lower = tag.lower() if isinstance(tag, str) else ""

                # 事件动词白名单：直接收下（与 extract_phrases 保持一致）
                if tag_lower == "v" and tok in self.event_verbs:
                    _add(tok)
                    continue
                # 实体类型直接收下（最高优先级）
                if tag_lower in _ENTITY_TAGS:
                    _add(tok)
                    continue
                # 功能词丢弃（助词/介词/连词/副词/代词/数量词/语气词等）
                if tag_lower in _FUNC_TAGS:
                    continue
                # 用户词典强制保留
                if tok in self.user_dict:
                    _add(tok)
                    continue
                # 其余（名词 n/ 动词 v/vn/ 形容词 a/an 等实词）按长度过滤后收下
                _add(tok)

        # ⑤ subsumption 去重：短词若是更长候选的子串则丢弃
        return _dedupe_subsumption(candidates)

    # ─────────────────────────────────────────────────
    #  事件级短语抽取（chunk）
    # ─────────────────────────────────────────────────

    def extract_phrases(
        self,
        title: str,
        min_len: int = 2,
        max_len: int = 12,
    ) -> List[str]:
        """
        双轨提取：事件级短语 + NER 实体兜底。

        两个 track 并行：
          Track A（事件短语）：把名词性内容整段保留，遇动词/功能词切开
          Track B（NER 实体）：从 ner/msra 结果直接取命名实体
          最后合并，subsumption 去重（短词若被长短语包含则丢弃）

        Track A 算法（4 类 token + verb_pending 暂存）：
          - 人名 nr/nrf/nrj   → 独立成段（走 Track B，此处跳过）
          - 强动词 v/vd/vshi/vyou:
              末位且 buf 非空 → 提交 buf（**不把动词加入**，避免"火漆印章玩"）
              否则           → 提交 buf；空时存入 verb_pending
          - 名动词 vn         → 进 buf；清除 verb_pending
          - 普通名词 n/ns/nt/nz/an
                             → 若有 verb_pending 则前置（卧床志愿者）
          - 其余功能词        → 硬切点，清除 verb_pending

        Track B 算法：
          - 取 ner/msra 中所有命名实体
          - 过 stopwords / blocklist / 长度过滤
          - 不受 min_single_token_len 限制（2 字专名也保留）
        """
        self._ensure_loaded()
        self._total_count += 1

        segments = [s for s in _RE_SPLIT.split(title) if s]
        if not segments:
            return []

        try:
            doc = self._pipeline(segments, tasks=["tok/fine", "pos/pku", "ner/msra"])
        except Exception as exc:
            self._record_fail()
            log.warning(
                f"HanLP 推理失败，jieba 降级: {exc}\n"
                f"  title={title!r}\n  segments={segments!r}",
                exc_info=True,
            )
            return self._jieba_fallback_phrases(title, min_len, max_len)

        tokens_list = doc.get("tok/fine") or doc.get("tok") or []
        pos_list    = doc.get("pos/pku")  or doc.get("pos") or []
        ner_list    = doc.get("ner/msra") or doc.get("ner") or []

        phrases: List[str] = []
        seen: Set[str] = set()

        # 收集本句所有 NER 实体文本，给 _commit 查询单 token 是否是 NER 实体。
        # 用途：sogou 短词白名单里有大量泛词（中国/美国/王菲...），它们都是
        # HanLP NER 实体；非 NER 的搜狗短词（拼豆/家崽/狗法）才是真正的小众
        # 新词，可以放行。
        ner_phrases_set: Set[str] = set()
        for seg_ner in ner_list:
            for ent in seg_ner:
                if isinstance(ent, (list, tuple)) and len(ent) >= 1:
                    et = str(ent[0]).strip()
                    if et:
                        ner_phrases_set.add(et)

        # 介词性 / 系动词性单字 v：HanLP 经常把它们标成 v，但语义上不构成事件
        # 当/被/让/把/将/使/令/请/帮/替/给/向/对/为：典型介词性
        # 到/是/在/有/为/做/被/把：系动词或介词，作为短语开头都没意义
        # 这些字一律不能作为 verb_pending（避免"到音乐剧""是江西"），
        # 也不能作为 buf 的左端（避免"为山西""把广州"）
        _PREP_VERBS = set("当被让把将使令请帮替给向对为到是在有做")

        # 否定/观点动词：遇到后进入"否定上下文"，buf 不提交（避免"定罪"/"解读"等）
        # 场景："不该先定罪后解读" → "不该"触发否定上下文 → 后续 buf 跳过
        _NEGATION_VERBS = {
            "不该", "不应", "不能", "不会", "不可", "不必", "不用", "不要",
            "无需", "无法", "不得", "不宜", "禁止", "拒绝", "反对", "否认",
            "质疑", "批评", "批判", "谴责", "指责", "为何", "应该", "应当",
        }

        def _commit(buf: List[str], is_entity: bool = False) -> None:
            if not buf:
                return
            # 左侧截头：单字介词性动词开头 → 丢弃该前缀
            # 例：[当, 火漆, 印章] → [火漆, 印章]；[到, 音乐剧] → [音乐剧]
            while buf and len(buf[0]) == 1 and buf[0] in _PREP_VERBS:
                buf = buf[1:]
            if not buf:
                return
            phrase = "".join(buf)
            if len(phrase) > max_len:
                # token 边界截断：从尾部累计完整 token，避免字符级截首字导致
                # "外交部回应日本爆发反战抗议"→"交部回应日本爆发反战抗议"残缺。
                truncated: List[str] = []
                cur_len = 0
                for tok in reversed(buf):
                    if cur_len + len(tok) > max_len:
                        break
                    truncated.insert(0, tok)
                    cur_len += len(tok)
                if not truncated:
                    return
                phrase = "".join(truncated)
            if not (min_len <= len(phrase) <= max_len):
                return
            if phrase in self.stopwords or phrase in self.blocklist:
                return
            if phrase in seen:
                return
            # 单 token 短词（< 3 字）放行规则：
            #   1. 在主白名单（user_dict.txt 手工集）→ 无条件放行
            #      （C罗/曼联/抖音/塌房/白鹿等明确想要的）
            #   2. 是 HanLP NER 实体（PERSON/LOC/ORG/...）→ 拒绝
            #      （中国/美国/王菲/北京等 NER 泛词，即使在 sogou 也拦掉）
            #   3. 在次白名单（sogou 等大词库）→ 放行
            #      （拼豆/家崽/狗法等小众非实体新词）
            #   4. 都不在 → 拒绝
            if len(buf) == 1 and len(phrase) < 3:
                if phrase in self.keep_short_words:
                    pass
                elif is_entity or phrase in ner_phrases_set:
                    return
                elif phrase in self.secondary_short_words:
                    pass
                else:
                    return
            # 数字占比过高：5亿/58亿/76/102/94 等比分/数额片段不是事件
            digit_count = sum(1 for c in phrase if c.isdigit())
            if digit_count * 2 >= len(phrase):
                return
            # 纯 ASCII（gold/gala/tic/bro 等英文噪声）：只有在主白名单里才放行。
            # 含中文的混合词（DeepSeek大模型/B站/RNG）不受此限制。
            if phrase.isascii() and phrase not in self.keep_short_words:
                return
            seen.add(phrase)
            phrases.append(phrase)

        # ── Track B：NER 实体兜底 ──────────────────────────────
        # ner_list[i] = [(entity_text, entity_type, start, end), ...]
        for seg_ner in ner_list:
            for ent in seg_ner:
                if isinstance(ent, (list, tuple)) and len(ent) >= 1:
                    ent_text = str(ent[0]).strip()
                    if ent_text:
                        _commit([ent_text], is_entity=True)

        # ── Track A：事件短语 ──────────────────────────────────
        # 强动词（末位触发提交但不入 buf；非末位作切点）
        _CUT_VERB_TAGS = {"v", "vd", "vshi", "vyou"}
        # 人名（已由 NER Track B 处理，此处跳过避免重复）
        _PERSON_TAGS = {"nr", "nrf", "nrj"}
        # 普通名词性（接受 verb_pending 前置）
        # j  = 简称略语（央视/北大/国足；dict_force 有时把地名+后续词合并成 j）
        # nx = 外来词/外文品牌（DeepSeek/ChatGPT/iPhone 等）
        _PLAIN_NOUN_TAGS = {
            "n", "nl", "nis", "nit", "nic", "nm", "nmc", "nb", "nba", "nbp",
            "nh", "nhd", "ns", "nsf", "nt", "ntc", "ntcf", "nz", "an",
            "j", "nx",
        }
        # 名动词（进 buf 但不接前动词）
        _VN_TAGS = {"vn"}
        # 后缀（者/族/性/化/家/学/派/界/区...）：粘到上一 token 末尾，不切断 buf
        # PKU 体系中 'k' 是后接成分。例："卧床志愿者" 切成 卧床/vn 志愿/n 者/k，
        # 'k' 应该作为名词的尾缀粘住，而不是当成切点丢弃"者"。
        _SUFFIX_TAGS = {"k"}

        for tokens, tags in zip(tokens_list, pos_list):
            n = len(tokens)
            buf: List[str] = []
            verb_pending: Optional[str] = None
            negated: bool = False  # 否定上下文标志：True 时 buf 不提交

            for idx, (tok, tag) in enumerate(zip(tokens, tags)):
                tok = tok.strip()
                if not tok:
                    continue
                tag_lower = tag.lower() if isinstance(tag, str) else ""
                is_last = (idx == n - 1)

                # 事件动词白名单 override：把"被 HanLP 错标为 v 的事件动词"
                # 强制改判为 vn，让它进 buf 而不是作切点。
                # 例：夺冠/抵达/曝光/暴发/偷拍/合龙 等本应是名动词性事件动词，
                # HanLP 三套词性体系都标 v；本规则用人工小词表纠正。
                if tag_lower == "v" and tok in self.event_verbs:
                    tag_lower = "vn"

                # 人名：已由 NER 兜底，此处只作切点
                # 例外：若该词在 user_dict（手工维护），说明用户明确想要它参与短语
                # 组合（如"娜姐"→"娜姐演唱会"），此时当作普通名词进 buf。
                if tag_lower in _PERSON_TAGS:
                    if tok in self.keep_short_words:
                        buf.append(tok)
                        verb_pending = None
                    else:
                        if not negated:
                            _commit(buf)
                        buf = []
                        verb_pending = None
                        negated = False
                    continue

                # 强动词
                if tag_lower in _CUT_VERB_TAGS:
                    # 否定/观点动词：进入否定上下文，丢弃当前 buf
                    if tok in _NEGATION_VERBS:
                        buf = []
                        verb_pending = None
                        negated = True
                        continue
                    if is_last and buf:
                        # 末位动词：
                        #   单字 v（玩/出/打/看/被）→ 丢弃，是动作动词不构成事件
                        #   ≥2字 v（巨变/被查/出片/爆料/落幕）→ 入 buf，是事件结果
                        if len(tok) >= 2:
                            buf.append(tok)
                        if not negated:
                            _commit(buf)
                        buf = []
                        verb_pending = None
                        negated = False
                    else:
                        # 非末位双字动词：作切点提交 buf，不 pending。
                        # HanLP PKU/CTB/863 三套标签都不区分"事件动词"vs"介词性
                        # 动词"vs"通用动作动词"——处于/上演/推开/相信/抵达/回应
                        # 全部标 v。verb_pending 拼接对介词性动词产生大量噪声
                        # （处于奥德赛时期/上演短剧圈/相信赛里木湖/尝尝冰糖雪梨卷），
                        # 收益（抵达北京/回应日本）则因 NER 泛词又被拦而损失有限。
                        # 故关闭双字 v pending；事件性短语依赖 vn/NER/单 token 提取。
                        if not negated:
                            _commit(buf)
                        buf = []
                        verb_pending = None
                        negated = False
                    continue

                # 名动词 vn：进 buf，清除 pending
                if tag_lower in _VN_TAGS:
                    verb_pending = None
                    if not negated:
                        buf.append(tok)
                    continue

                # 后缀 k：粘到上一 buf token 末端（如 卧床志愿+者）
                if tag_lower in _SUFFIX_TAGS:
                    if buf and not negated:
                        buf.append(tok)
                    continue

                # 普通名词性：接受 verb_pending 前置
                if tag_lower in _PLAIN_NOUN_TAGS:
                    if negated:
                        # 否定上下文：只有遇到独立名词才重置（新实体开头）
                        # 例："不该解读 广东进4强" → "广东" 是新段，重置
                        buf = []
                        negated = False
                    if verb_pending is not None:
                        buf.append(verb_pending)
                        verb_pending = None
                    buf.append(tok)
                    continue

                # 其余功能词（u/p/c/d/m/r/q/y/a/f/b 等）→ 硬切点
                if not negated:
                    _commit(buf)
                buf = []
                verb_pending = None
                # 功能词不重置 negated（副词"先/后/再"不应解除否定上下文）

            if not negated:
                _commit(buf)

        # subsumption 去重：短词若是长短语子串则丢弃
        return _dedupe_subsumption(phrases)


def _dedupe_subsumption(words: List[str]) -> List[str]:
    """同标题内，短词若被更长候选完全包含则丢弃（华谊 ⊂ 华谊兄弟）。"""
    sorted_by_len = sorted(set(words), key=lambda x: -len(x))
    drop: Set[str] = set()
    for i, c in enumerate(sorted_by_len):
        for longer in sorted_by_len[:i]:
            if c != longer and c in longer:
                drop.add(c)
                break
    return [w for w in words if w not in drop]


# ─────────────────────────────────────────────────────────
#  CLI 调试入口
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from pathlib import Path as _Path

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # 加载 stopwords / blocklist / user_dict（与 run_v2.py 一致）
    def _load_lines(p: _Path) -> Set[str]:
        if not p.exists():
            return set()
        out: Set[str] = set()
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    out.add(line.split()[0])
        return out

    _here = _Path(__file__).resolve().parent
    seg = HanlpSegmenter(
        stopwords=_load_lines(_here / "stopwords.txt"),
        blocklist=_load_lines(_here / "blocklist.txt"),
        user_dict=_load_lines(_here / "user_dict.txt"),
        event_verbs=_load_lines(_here / "event_verbs.txt"),
    )
    test_titles = sys.argv[1:] or [
        "白鹿跑男争议 内娱综艺审美巨变",
        "白鹿跑男争议内娱综艺审美巨变",
        "航天员中心招募卧床志愿者",
        "迪丽热巴新剧首播",
        "华谊兄弟回应破产传闻",
    ]
    for t in test_titles:
        print(f"\n>>> {t}")
        print("    词级 →", seg.extract(t))
        print("    事件 →", seg.extract_phrases(t))

"""
crawler/weibo/spider.py — 微博热搜爬虫核心（WeiboHotSpider）

无需登录：直接解析微博公开热搜页面与 JSON 接口。

主要数据源（按优先级）:
  1. https://weibo.com/ajax/side/hotSearch       ← JSON 接口，首选
  2. https://s.weibo.com/top/summary              ← HTML 备用

流程:
  1. 尝试 JSON 接口，失败则降级到 HTML 抓取
  2. 解析出热搜词条列表（已排序，跳过广告/空词）
  3. 清洗词条（去 emoji、话题符 #、标点）
  4. 生成拼音（反引号分词，与搜狗格式一致）
  5. 写入 output/weibo_hotsearch/{YYYYMMDD}/weibo_hotsearch_{YYYYMMDD}_{HHMM}.txt

输出示例:
  山火     `shan`huo
  世界杯   `shi`jie`bei
"""
from __future__ import annotations

import os
import re
import time
import random
from datetime import datetime, timedelta
from html import unescape
from typing import List, Optional, Tuple

import requests
import urllib3
from pypinyin import lazy_pinyin

from config import Config
from logger import get_logger

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = get_logger("crawler.weibo")

# ── 公开数据源 ────────────────────────────────────────────────
_JSON_API = "https://weibo.com/ajax/side/hotSearch"
_HTML_PAGE = "https://s.weibo.com/top/summary"

_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": "https://weibo.com/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
}


# ────────────────────────────────────────────────────────────
#  WeiboHotSpider
# ────────────────────────────────────────────────────────────

class WeiboHotSpider:
    """
    微博热搜爬虫：无需登录，解析公开热搜页/接口 → 生成拼音 → 保存 TXT

    输出格式与搜狗词库相同: 每行 <词条>\\t<拼音>
    """

    SOURCE_NAME = "weibo_hotsearch"

    def __init__(self) -> None:
        self.save_dir = Config.WEIBO_SAVE_DIR
        os.makedirs(self.save_dir, exist_ok=True)

    # ── 目录 ──────────────────────────────────────────────────

    def _date_save_dir(self, date_str: Optional[str] = None) -> str:
        date_str = date_str or datetime.now().strftime("%Y%m%d")
        d = os.path.join(self.save_dir, date_str)
        os.makedirs(d, exist_ok=True)
        return d

    # ── 清理过期文件 ──────────────────────────────────────────

    def _clean_old_files(self) -> None:
        try:
            cutoff = datetime.now() - timedelta(days=Config.JSON_FILE_RETENTION_DAYS)
            for root, _, files in os.walk(self.save_dir):
                for fname in files:
                    if not fname.endswith(".txt"):
                        continue
                    fp = os.path.join(root, fname)
                    if datetime.fromtimestamp(os.path.getmtime(fp)) < cutoff:
                        try:
                            os.remove(fp)
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(f"清理旧文件失败（可忽略）: {e}")

    # ── 数据获取：JSON 接口 ──────────────────────────────────

    def _fetch_from_json_api(self) -> List[Tuple[str, int]]:
        """
        优先从 JSON 接口获取热搜词。
        返回值: 按榜单顺序排列的 (词条, 热度num) 列表（已去掉广告位）
        """
        try:
            headers = dict(_HEADERS)
            headers["Accept"] = "application/json, text/plain, */*"
            resp = requests.get(
                _JSON_API,
                headers=headers,
                timeout=10,
                verify=False,
            )
            if resp.status_code != 200:
                logger.warning(f"JSON 接口 HTTP {resp.status_code}，降级到 HTML")
                return []

            data = resp.json()
            # 响应结构: {"ok": 1, "data": {"realtime": [...], "hotgov": {...}}}
            realtime = data.get("data", {}).get("realtime", [])
            if not realtime:
                logger.warning("JSON 接口无 realtime 数据，降级到 HTML")
                return []

            words: List[Tuple[str, int]] = []
            for item in realtime:
                # 广告/置顶条目
                if item.get("is_ad") or item.get("ad_type"):
                    continue
                word = item.get("word", "").strip()
                heat = int(item.get("num") or 0)
                if word:
                    words.append((word, heat))

            logger.info(f"JSON 接口获取 {len(words)} 条热搜词")
            return words

        except Exception as e:
            logger.warning(f"JSON 接口异常: {e}，降级到 HTML")
            return []

    # ── 数据获取：HTML 备用 ──────────────────────────────────

    def _fetch_from_html(self) -> List[Tuple[str, int]]:
        """
        从 s.weibo.com/top/summary 公开热搜页解析词条（热度无法从 HTML 获取，置 0）。
        不需要任何 Cookie 或登录态。
        """
        try:
            resp = requests.get(
                _HTML_PAGE,
                headers=_HEADERS,
                timeout=15,
                verify=False,
                allow_redirects=True,
            )
            if resp.status_code != 200:
                logger.error(f"热搜页 HTTP {resp.status_code}")
                return []

            html = resp.text
            pattern = re.compile(
                r'class=["\']td-02["\'][^>]*>\s*<a\b[^>]*>([^<]+)</a>',
                re.IGNORECASE,
            )
            raw = pattern.findall(html)
            words = [(unescape(w.strip()), 0) for w in raw if w.strip()]
            logger.info(f"HTML 解析获取 {len(words)} 条热搜词")
            return words

        except Exception as e:
            logger.error(f"HTML 抓取异常: {e}")
            return []

    # ── 词条清洗 ─────────────────────────────────────────────

    _EMOJI_RE = re.compile(
        "["
        "\U0001F600-\U0001F64F"
        "\U0001F300-\U0001F5FF"
        "\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF"
        "\U00002600-\U000027BF"
        "\U0001F900-\U0001F9FF"
        "\U000020D0-\U000020FF"
        "\U0000200D"
        "\U0000FE00-\U0000FE0F"
        "]+",
        flags=re.UNICODE,
    )

    @classmethod
    def _clean_word(cls, word: str) -> str:
        """
        清洗微博热搜词条:
          - 去除 emoji、控制字符
          - 去掉话题标签符 #
          - 过滤纯英文/数字、过短、超长词条
          - 要求至少 2 个中文字且中文占比 ≥ 50%
          - 标点符号保留原样，交由下游热词服务处理
        """
        if not word:
            return ""
        word = cls._EMOJI_RE.sub("", word)
        word = re.sub(r"[\x00-\x1f\x7f\u200b\u200c\u200d\ufeff]", "", word)
        word = word.replace("#", "").strip()
        word = re.sub(r"\s+", "", word).strip()
        if not word or len(word) > 15:
            return ""
        zh_count = sum(1 for ch in word if "\u4e00" <= ch <= "\u9fff")
        if zh_count < 2 or zh_count / len(word) < 0.5:
            return ""
        return word

    # ── 拼音生成 ─────────────────────────────────────────────

    @staticmethod
    def _make_pinyin(word: str) -> str:
        """
        格式与搜狗词库一致:
          中文字符 → 反引号分隔音节: `shan`huo
          英文字母 → 空格分隔字母:  a i / n b a
          混合示例: `shan`huo a i
        """
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

    # ── 主流程 ───────────────────────────────────────────────

    def run(
        self,
        run_day: Optional[str] = None,
        run_slot: Optional[str] = None,
    ) -> Optional[str]:
        """
        完整爬取流程（无需登录）。

        Args:
            run_day:  强制指定"日期分区" YYYYMMDD（默认取当前日期）
            run_slot: 强制指定"拉取时间" HHMM（默认取当前时刻）

        Returns:
            生成的 TXT 文件路径；失败返回 None
        """
        logger.info("=" * 60)
        logger.info(f"执行时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
        logger.info("微博热搜爬虫启动（无需登录）")
        logger.info("=" * 60)

        self._clean_old_files()

        daily_date = run_day or datetime.now().strftime("%Y%m%d")
        slot_str = run_slot or datetime.now().strftime("%H%M")

        # 随机短延迟，避免明显的机器请求特征
        time.sleep(random.uniform(0.3, 1.0))

        # 优先 JSON 接口，失败降级 HTML
        raw_words = self._fetch_from_json_api()
        if not raw_words:
            raw_words = self._fetch_from_html()

        if not raw_words:
            logger.warning("未获取到任何热搜词条，任务终止")
            return None

        logger.info(f"原始词条数: {len(raw_words)}")

        # 清洗 + 去重 + 生成拼音
        seen: set = set()
        unique_words: List[Tuple[str, str, int]] = []
        for word, heat in raw_words:
            cleaned = self._clean_word(word)
            if not cleaned or cleaned in seen:
                continue
            pinyin = self._make_pinyin(cleaned)
            if not pinyin:
                continue
            seen.add(cleaned)
            unique_words.append((cleaned, pinyin, heat))

        if not unique_words:
            logger.warning("清洗/过滤后无有效词条")
            return None

        # 写 TXT 文件: 词条\t拼音\t热度
        save_dir = self._date_save_dir(daily_date)
        txt_file = os.path.join(
            save_dir, f"weibo_hotsearch_{daily_date}_{slot_str}.txt"
        )
        with open(txt_file, "w", encoding="utf-8") as f:
            for word, pinyin, heat in unique_words:
                f.write(f"{word}\t{pinyin}\t{heat}\n")

        logger.info(f"导出完成: {txt_file}  共 {len(unique_words)} 条")
        return txt_file

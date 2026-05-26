"""
crawler/douyin_hotlist/spider.py — 抖音热榜爬虫核心（DouyinHotListSpider）

数据源: https://www.douyin.com/hot
接口:   https://www.douyin.com/aweme/v1/hot/search/list/
        └── data.word_list[*].word（约 50 条，无需登录）

流程:
  1. GET 热榜接口（移动端 UA，无需任何 Cookie）
  2. 提取 data.word_list 中每条的 word 字段
  3. 清洗（去 emoji、话题符 #、标点；过滤短词/纯英文）
  4. 生成拼音（反引号格式，与搜狗词库一致）
  5. 写入 output/douyin_hotlist/{YYYYMMDD}/douyin_hotlist_{YYYYMMDD}_{HHMM}.txt

输出示例:
  王石否认被抓	`wang`shi`fou`ren`bei`zhua
  特朗普封锁霍尔木兹海峡	`te`lang`pu`feng`suo`huo`er`mu`zi`hai`xia
"""
from __future__ import annotations

import os
import re
import time
import random
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import requests
import urllib3
from pypinyin import lazy_pinyin

from config import Config
from logger import get_logger

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = get_logger("crawler.douyin_hotlist")

_API_URL = "https://www.douyin.com/aweme/v1/hot/search/list/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Mobile/15E148 Safari/604.1"
    ),
    "Referer": "https://www.douyin.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


# ────────────────────────────────────────────────────────────
#  DouyinHotListSpider
# ────────────────────────────────────────────────────────────

class DouyinHotListSpider:
    """
    抖音热榜爬虫：无需登录，直接调用公开 JSON 接口 → 生成拼音 → 保存 TXT

    对应页面: https://www.douyin.com/hot
    输出格式与搜狗词库相同: 每行 <词条>\\t<拼音>
    """

    SOURCE_NAME = "douyin_hotlist"

    def __init__(self) -> None:
        self.save_dir = Config.DOUYIN_HOTLIST_SAVE_DIR
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

    # ── 数据获取 ──────────────────────────────────────────────

    def _fetch_words(self) -> List[Tuple[str, int]]:
        """
        调用抖音热榜公开接口，返回 (热搜词, hot_value) 列表（按榜单顺序）。
        接口无需登录，使用移动端 UA 即可访问。
        """
        try:
            time.sleep(random.uniform(0.3, 1.0))
            resp = requests.get(
                _API_URL,
                headers=_HEADERS,
                timeout=12,
                verify=False,
            )
            if resp.status_code != 200:
                logger.warning(f"热榜接口 HTTP {resp.status_code}")
                return []

            data = resp.json()
            word_list = data.get("data", {}).get("word_list", []) or []

            words: List[Tuple[str, int]] = []
            for item in word_list:
                word = (item.get("word") or "").strip()
                heat = int(item.get("hot_value") or 0)
                if word:
                    words.append((word, heat))

            logger.info(f"热榜接口获取 {len(words)} 条热词")
            return words

        except Exception as e:
            logger.error(f"热榜接口异常: {e}", exc_info=True)
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
        清洗抖音热榜词条:
          - 去 emoji、控制字符
          - 去话题符 #
          - 过滤纯英文/数字、过短（<2 中文字）、超长（>20 字）词条
          - 标点符号保留原样，交由下游热词服务处理
        """
        if not word:
            return ""
        word = cls._EMOJI_RE.sub("", word)
        word = re.sub(r"[\x00-\x1f\x7f\u200b\u200c\u200d\ufeff]", "", word)
        word = word.replace("#", "").strip()
        word = re.sub(r"\s+", "", word).strip()
        if not word or len(word) > 20:
            return ""
        zh_count = sum(1 for ch in word if "\u4e00" <= ch <= "\u9fff")
        if zh_count < 2:
            return ""
        return word

    # ── 拼音生成 ─────────────────────────────────────────────

    @staticmethod
    def _make_pinyin(word: str) -> str:
        """
        格式与搜狗词库一致:
          中文字符 → 反引号分隔音节: `chao`ri`chang
          英文字母 → 空格分隔字母:  a i / n b a
          混合示例: `chao`ri a i`ce`shi
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
        logger.info("抖音热榜爬虫启动（无需登录）")
        logger.info("=" * 60)

        self._clean_old_files()

        daily_date = run_day or datetime.now().strftime("%Y%m%d")
        slot_str = run_slot or datetime.now().strftime("%H%M")

        raw_words = self._fetch_words()
        if not raw_words:
            logger.warning("未获取到任何热榜词条，任务终止")
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

        logger.info(f"清洗后有效词条: {len(unique_words)} 条")

        if not unique_words:
            logger.warning("清洗/过滤后无有效词条")
            return None

        save_dir = self._date_save_dir(daily_date)
        txt_file = os.path.join(
            save_dir, f"douyin_hotlist_{daily_date}_{slot_str}.txt"
        )
        with open(txt_file, "w", encoding="utf-8") as f:
            for word, pinyin, heat in unique_words:
                f.write(f"{word}\t{pinyin}\t{heat}\n")

        logger.info(f"导出完成: {txt_file}  共 {len(unique_words)} 条")
        return txt_file

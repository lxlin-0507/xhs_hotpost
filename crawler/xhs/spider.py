"""
crawler/xhs/spider.py — 小红书热词爬虫

数据源: https://api.rebang.today/v1/items?tab=xiaohongshu&sub_tab=hot-search
       无需登录/Cookie，直接 JSON API，返回带 view_num 热度的 20 条热点。

流程:
  1. requests GET → JSON
  2. 解析 data.list（JSON 字符串），提取 title + view_num
  3. 清洗词条（去 emoji、过滤无中文等）
  4. 生成拼音（反引号格式，与搜狗词库一致）
  5. 写入 output/xhs_hot/{YYYYMMDD}/xhs_hot_{YYYYMMDD}_{HHMM}.txt

输出格式（每行三列，制表符分隔）:
  词条<TAB>拼音<TAB>热度整数

view_num 转换规则:
  "907.8w" → 9078000   "806w" → 8060000   "12345" → 12345
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import requests
from pypinyin import lazy_pinyin

from config import Config
from logger import get_logger

logger = get_logger("crawler.xhs")

_API_URL = "https://api.rebang.today/v1/items"
_API_PARAMS = {"tab": "xiaohongshu", "sub_tab": "hot-search", "page": 1, "version": 1}
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://rebang.today/",
    "Accept": "application/json, text/plain, */*",
}


def _parse_view_num(raw: str) -> int:
    """将 '907.8w' / '806w' / '12345' 转换为整数热度值。"""
    if not raw:
        return 0
    raw = raw.strip()
    try:
        m = re.match(r"^([\d.]+)([wW万]?)$", raw)
        if not m:
            return 0
        num = float(m.group(1))
        if m.group(2):
            num *= 10_000
        return int(num)
    except Exception:
        return 0


class XhsHotSpider:
    """
    小红书热词爬虫：调用 api.rebang.today JSON 接口 → 生成拼音 → 保存 TXT

    数据源: api.rebang.today（无需 Playwright / Cookie）
    输出格式与搜狗词库相同: 每行 <词条>\\t<拼音>\\t<热度>
    """

    SOURCE_NAME = "xhs_hot"

    def __init__(self) -> None:
        self.save_dir = Config.XHS_SAVE_DIR
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

    def _fetch_items(self) -> List[Tuple[str, int]]:
        """
        调用 api.rebang.today JSON 接口，返回 [(title, heat), ...] 列表。
        heat 为 view_num 转换后的整数（如 9078000）。
        """
        try:
            resp = requests.get(
                _API_URL,
                params=_API_PARAMS,
                headers=_HEADERS,
                timeout=15,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            logger.error(f"API 请求失败: {e}", exc_info=True)
            return []

        if payload.get("code") != 200:
            logger.error(f"API 返回异常: code={payload.get('code')}, msg={payload.get('msg')}")
            return []

        raw_list = payload.get("data", {}).get("list", [])
        if isinstance(raw_list, str):
            try:
                raw_list = json.loads(raw_list)
            except Exception as e:
                logger.error(f"data.list 反序列化失败: {e}")
                return []

        items: List[Tuple[str, int]] = []
        for item in raw_list:
            title = (item.get("title") or "").strip()
            heat = _parse_view_num(item.get("view_num") or "")
            if title:
                items.append((title, heat))

        logger.info(f"API 返回 {len(items)} 条热点")
        return items

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
        清洗 XHS 热榜标题:
          - 去 emoji、控制字符、零宽字符
          - 去话题符 #
          - 过滤过短（<2 中文字）或无中文内容的词条
          - 长度上限 30 字
          - 标点符号保留原样，交由下游热词服务处理
        """
        if not word:
            return ""
        word = cls._EMOJI_RE.sub("", word)
        word = re.sub(r"[\x00-\x1f\x7f\u200b\u200c\u200d\ufeff]", "", word)
        word = word.replace("#", "").strip()
        word = re.sub(r"\s+", "", word).strip()
        if not word or len(word) > 30:
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
          英文字母 → 空格分隔字母:  g e t / n b a
          混合示例: `chao`ri`chang`su`lai g e t
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
        logger.info(f"小红书热词爬虫启动  数据源: {_API_URL}")
        logger.info("=" * 60)

        self._clean_old_files()

        daily_date = run_day or datetime.now().strftime("%Y%m%d")
        slot_str = run_slot or datetime.now().strftime("%H%M")

        raw_items = self._fetch_items()
        if not raw_items:
            logger.warning("未获取到任何热点，任务终止")
            return None

        seen: set = set()
        unique_words: List[Tuple[str, str, int]] = []
        for title, heat in raw_items:
            cleaned = self._clean_word(title)
            if not cleaned or cleaned in seen:
                continue
            pinyin = self._make_pinyin(cleaned)
            if not pinyin:
                continue
            seen.add(cleaned)
            unique_words.append((cleaned, pinyin, heat))

        logger.info(f"清洗后有效词条: {len(unique_words)} 条")

        if not unique_words:
            logger.warning("去重/过滤后无有效词条")
            return None

        save_dir = self._date_save_dir(daily_date)
        txt_file = os.path.join(save_dir, f"xhs_hot_{daily_date}_{slot_str}.txt")
        with open(txt_file, "w", encoding="utf-8") as f:
            for word, pinyin, heat in unique_words:
                f.write(f"{word}\t{pinyin}\t{heat}\n")

        logger.info(f"导出完成: {txt_file}  共 {len(unique_words)} 条")
        return txt_file

"""
crawler/sogou/dict_manager.py — 搜狗词库下载 + 解析 + 导出
"""
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import requests
from playwright.sync_api import sync_playwright
from pypinyin import lazy_pinyin

from config import Config
from logger import get_logger

logger = get_logger("crawler.sogou")


@dataclass
class SogouDictFile:
    path: Path


class SogouDictManager:
    """搜狗词库：下载 → 转换 → 导出"""

    SOURCE_NAME = "sogou_newwords"

    def __init__(self) -> None:
        self.save_dir = Path(Config.SOGOU_SAVE_DIR)
        self.save_dir.mkdir(parents=True, exist_ok=True)

        self.dict_url = (Config.SOGOU_DICT_URL or "").strip()
        self.dict_id = (Config.SOGOU_DICT_ID or "").strip()
        self.official_download_tpl = os.getenv(
            "SOGOU_OFFICIAL_DOWNLOAD_TPL",
            "https://pinyin.sogou.com/d/dict/download_cell.php?id={id}",
        ).strip()

    def check_environment(self) -> bool:
        ok = True
        if not self.dict_url and not self.dict_id:
            logger.warning("未设置 SOGOU_DICT_URL / SOGOU_DICT_ID，无法自动下载 .scel 词库。")
            logger.warning("请在 .env.dev 里配置 SOGOU_DICT_ID=12345")
        return ok

    # ── 下载 ─────────────────────────────────────────────────

    def download_dict(self, run_day: Optional[str] = None) -> Optional[SogouDictFile]:
        url = self.dict_url
        if not url and self.dict_id:
            url = self.official_download_tpl.format(id=self.dict_id)
        if not url:
            logger.error("无法下载：未设置 SOGOU_DICT_URL / SOGOU_DICT_ID")
            return None

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        date_dir = self._date_save_dir(run_day)
        target = date_dir / f"sogou_{ts}.scel"
        logger.info(f"开始下载词库: {url}")

        try:
            resp = requests.get(
                url,
                timeout=120,
                stream=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                },
            )
            resp.raise_for_status()

            head = resp.content[:200].lower()
            is_html = head.startswith(b"<!doctype") or b"<html" in head
            if is_html and self.dict_id:
                logger.warning("下载接口返回HTML，改用 Playwright 触发真实下载...")
                pw_file = self._download_dict_with_playwright(self.dict_id, target)
                if pw_file:
                    return SogouDictFile(path=pw_file)
                logger.error("Playwright 下载失败")
                return None

            with open(target, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 128):
                    if chunk:
                        f.write(chunk)
            logger.info(f"下载完成: {target}")
            return SogouDictFile(path=target)
        except Exception as e:
            logger.error(f"下载失败: {e}")
            return None

    def _download_dict_with_playwright(self, dict_id: str, target: Path) -> Optional[Path]:
        detail_url = f"https://pinyin.sogou.com/dict/detail/index/{dict_id}"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context()
                page = context.new_page()
                page.goto(detail_url, wait_until="domcontentloaded", timeout=30_000)

                href = page.locator('a[href*="download_cell.php"]').first.get_attribute("href")
                if not href:
                    context.close()
                    browser.close()
                    return None
                if href.startswith("//"):
                    href = "https:" + href

                resp = page.request.get(
                    href,
                    headers={
                        "Accept": "application/octet-stream,*/*;q=0.8",
                        "Referer": detail_url,
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                    },
                    timeout=30_000,
                )
                body = resp.body()
                head = body[:200].lower()
                is_html = head.startswith(b"<!doctype") or b"<html" in head
                if resp.status != 200 or is_html or len(body) < 1024:
                    context.close()
                    browser.close()
                    return None

                target.write_bytes(body)
                context.close()
                browser.close()
                logger.info(f"Playwright 下载完成: {target}")
                return target
        except Exception as e:
            logger.error(f"Playwright 下载异常: {e}")
            return None

    # ── 转换 ─────────────────────────────────────────────────

    def convert_scel_to_txt(self, scel_path: Optional[Path] = None) -> Optional[Path]:
        txt_override = os.getenv("SOGOU_TXT_PATH", "").strip()
        if txt_override:
            p = Path(txt_override)
            if p.exists():
                logger.info(f"使用已存在 txt 词库: {p}")
                return p
            logger.error(f"SOGOU_TXT_PATH 不存在: {p}")
            return None

        scel_path = scel_path or self._latest_file(self.save_dir, ".scel")
        if not scel_path:
            logger.error("没有找到 .scel 文件可转换")
            return None

        out_txt = scel_path.with_suffix(".txt")
        if out_txt.exists() and out_txt.stat().st_size > 0:
            logger.info(f"已存在转换结果: {out_txt}")
            return out_txt

        try:
            self._extract_words_from_scel_to_txt(scel_path, out_txt)
            logger.info(f"内置解析成功: {out_txt}")
            return out_txt
        except Exception as e:
            logger.warning(f"内置解析失败: {e}")
            return None

    @staticmethod
    def _extract_words_from_scel_to_txt(scel_path: Path, out_txt: Path) -> None:
        data = scel_path.read_bytes()
        word_pairs = SogouDictManager._parse_scel_words_structured(data)
        if not word_pairs:
            word_pairs = SogouDictManager._parse_scel_words_heuristic(data)
        if not word_pairs:
            raise RuntimeError("no valid tokens extracted")
        # 中间 txt 格式: word\tpinyin
        lines = [f"{w}\t{py}" for py, w in word_pairs]
        out_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _read_pinyin_table(data: bytes, word_section_start: int = 0) -> dict:
        """读取 .scel 文件的拼音表（offset 0x1540 开始，到词表起始位置结束）"""
        import struct
        START_PINYIN = 0x1540
        pinyin_table = {}
        pos = START_PINYIN + 4  # 跳过 4 字节头
        end = word_section_start if word_section_start > START_PINYIN else len(data)
        while pos + 4 < end:
            try:
                idx = struct.unpack_from("<H", data, pos)[0]
                pos += 2
                py_len = struct.unpack_from("<H", data, pos)[0]
                pos += 2
                if py_len == 0 or py_len > 64 or pos + py_len > end:
                    break
                py = data[pos : pos + py_len].decode("utf-16le", errors="replace").rstrip("\x00")
                pos += py_len
                pinyin_table[idx] = py
            except Exception:
                break
        return pinyin_table

    @staticmethod
    def _find_word_section_start(data: bytes) -> int:
        """自动探测词组表起始偏移"""
        import struct
        candidates = [0x2628, 0x26C4, 0x2724]
        for offset in candidates:
            if offset + 10 >= len(data):
                continue
            try:
                same = struct.unpack_from("<H", data, offset)[0]
                if same == 0 or same > 100:
                    continue
                py_len = struct.unpack_from("<H", data, offset + 2)[0]
                if py_len == 0 or py_len > 200 or py_len % 2 != 0:
                    continue
                word_pos = offset + 4 + py_len
                if word_pos + 4 >= len(data):
                    continue
                wlen = struct.unpack_from("<H", data, word_pos)[0]
                if wlen == 0 or wlen > 100 or wlen % 2 != 0:
                    continue
                w = data[word_pos + 2 : word_pos + 2 + wlen].decode("utf-16le", errors="ignore")
                if all("\u4e00" <= c <= "\u9fff" or c.isascii() for c in w):
                    return offset
            except Exception:
                continue
        # 暴力扫描
        for offset in range(0x2600, 0x2800, 2):
            if offset + 10 >= len(data):
                break
            try:
                same = struct.unpack_from("<H", data, offset)[0]
                if same == 0 or same > 50:
                    continue
                py_len = struct.unpack_from("<H", data, offset + 2)[0]
                if py_len == 0 or py_len > 100 or py_len % 2 != 0:
                    continue
                word_pos = offset + 4 + py_len
                if word_pos + 4 >= len(data):
                    continue
                wlen = struct.unpack_from("<H", data, word_pos)[0]
                if wlen == 0 or wlen > 60 or wlen % 2 != 0:
                    continue
                w = data[word_pos + 2 : word_pos + 2 + wlen].decode("utf-16le", errors="ignore")
                if all("\u4e00" <= c <= "\u9fff" or c.isascii() for c in w):
                    return offset
            except Exception:
                continue
        return 0x2628  # 默认值

    @staticmethod
    def _parse_scel_words_structured(data: bytes) -> List[Tuple[str, str]]:
        """解析 .scel 二进制词库，提取 (拼音, 词条) 对"""
        import struct

        def u16(off: int) -> int:
            return struct.unpack_from("<H", data, off)[0]

        # 1. 找到词条起始偏移
        start = SogouDictManager._find_word_section_start(data)

        # 2. 读取拼音表（以词表起始位置为终止边界）
        pinyin_table = SogouDictManager._read_pinyin_table(data, start)

        out: List[Tuple[str, str]] = []
        seen = set()
        i = start
        end = len(data)
        valid_word = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9]+$")
        error_count = 0

        while i + 8 < end:
            try:
                same_count = u16(i)        # 同音词数量
                py_idx_len = u16(i + 2)    # 拼音索引数据长度

                if py_idx_len == 0 or py_idx_len > 256 or py_idx_len % 2 != 0:
                    i += 2
                    continue

                # 读取拼音索引，查表得到拼音字符串
                py_idx_data = data[i + 4 : i + 4 + py_idx_len]
                try:
                    pinyin_str = "".join(
                        pinyin_table.get(struct.unpack_from("<H", py_idx_data, j)[0], "?")
                        for j in range(0, len(py_idx_data), 2)
                    )
                except Exception:
                    pinyin_str = "?"

                pos = i + 4 + py_idx_len  # 跳过拼音索引

                ok = True
                for _ in range(same_count):
                    if pos + 2 > end:
                        ok = False
                        break
                    wlen = u16(pos)
                    pos += 2
                    if wlen == 0 or wlen > 128 or wlen % 2 != 0:
                        ok = False
                        break
                    if pos + wlen > end:
                        ok = False
                        break

                    w = data[pos : pos + wlen].decode("utf-16le", errors="ignore").strip()
                    pos += wlen

                    # 读取扩展数据（长度 + 内容）
                    if pos + 2 > end:
                        ok = False
                        break
                    ext_len = u16(pos)
                    pos += 2
                    if pos + ext_len > end:
                        ok = False
                        break
                    pos += ext_len

                    if not w or len(w) > 30:
                        continue
                    if not valid_word.fullmatch(w):
                        continue
                    if w not in seen:
                        seen.add(w)
                        out.append((pinyin_str, w))

                if ok and pos > i + 4:
                    i = pos
                else:
                    i += 2
                    error_count += 1
                    if error_count > 50:
                        break
            except Exception:
                i += 2
                error_count += 1
                if error_count > 50:
                    break
        return out

    @staticmethod
    def _parse_scel_words_heuristic(data: bytes) -> List[Tuple[str, str]]:
        from pypinyin import lazy_pinyin as _lazy_pinyin
        text = data.decode("utf-16le", errors="ignore")
        candidate_re = re.compile(r"[\u4e00-\u9fffA-Za-z]{2,}")
        candidates = candidate_re.findall(text)
        valid_word = re.compile(r"^[\u4e00-\u9fffA-Za-z]+$")
        noise_substrings = {
            "http", "https", "www", "html", "script", "style",
            "href", "src", "class", "charset", "content",
            "window", "document", "function", "jquery",
            "sogou", "pinyin", "dict", "download",
        }
        out: List[Tuple[str, str]] = []
        seen = set()
        for t in candidates:
            if len(t) > 30:
                continue
            if not valid_word.match(t):
                continue
            low = t.lower()
            if any(s in low for s in noise_substrings):
                continue
            if t not in seen:
                seen.add(t)
                py = SogouDictManager._norm_pinyin("".join(_lazy_pinyin(t)).lower())
                out.append((py, t))
        return out

    # ── 导出 ─────────────────────────────────────────────────

    def export_words_to_files(
        self,
        txt_path: Path,
        run_day: Optional[str] = None,
        run_slot: Optional[str] = None,
    ) -> Path:
        """导出词条到 output/sogou_newwords/{date}/ 目录，格式: 词条\\t拼音

        规则：按“拉取时间(slot=HHMM)”生成最终文件，只保留处理后的 sogou 文件：
          sogou_{YYYYMMDD}_{HHMM}.txt
        """
        words = self._read_words(txt_path)
        if not words:
            raise RuntimeError("No valid words in txt")

        date_str = run_day or datetime.now().strftime("%Y%m%d")  # YYYYMMDD
        slot_str = run_slot or datetime.now().strftime("%H%M")  # HHMM
        date_dir = self._date_save_dir(date_str)
        out_file = date_dir / f"sogou_{date_str}_{slot_str}.txt"

        valid_word = re.compile(r"^[\u4e00-\u9fffA-Za-z]+$")

        with open(out_file, "w", encoding="utf-8") as f:
            written = 0
            for pinyin, word in words:
                token = word.strip()
                if not token:
                    continue
                if not valid_word.match(token):
                    continue
                # “准确版”多音字处理：
                # - pinyin 参数来自 .scel 解析结果（词条的真实读音）
                # - 但该拼音通常是“无分隔连续串”，需再切分成“逐字片段”
                # - 切分用 pypinyin(heteronym=True) 的逐字候选去匹配连续串（用词条读音做消歧）
                #
                # 输出格式：符号分词（片段之间用反引号 `，并且第一个片段前也加 `）
                if token.isascii():
                    # 英文/数字词条：逐字符分词，使用空格分隔
                    # 例如: "Airbnb" -> "a i r b n b", "A1B2" -> "a 1 b 2"
                    py = " ".join(list(token.lower()))
                else:
                    py = self._export_precise_symbol_pinyin(token, pinyin)

                f.write(f"{token}\t{py}\n")
                written += 1

        logger.info(f"Sogou 词库已导出: {out_file} ({written}条)")
        return out_file

    @staticmethod
    def _norm_pinyin(py: str) -> str:
        """标准化拼音：ve → ue（lve→lue, nve→nue），lv/nv 保持不变"""
        return py.replace("ve", "ue")

    @staticmethod
    def _export_precise_symbol_pinyin(token: str, raw_pinyin: Optional[str]) -> str:
        """
        把 .scel 解析出来的连续拼音 raw_pinyin 切成逐字片段，并输出为符号分词：
            `wen`rui`bo
        若无法切分则回退为 pypinyin 的逐字主读音版本。
        """
        from functools import lru_cache
        import re
        from pypinyin import Style, pinyin

        def fallback() -> str:
            # fallback：按逐字主读音生成符号分词
            return SogouDictManager._norm_pinyin("`" + "`".join(lazy_pinyin(token)).lower())

        if not raw_pinyin or raw_pinyin == "?":
            return fallback()

        # 连续拼音：去掉空白/非字母，转小写
        raw = re.sub(r"[^a-zA-Z]", "", str(raw_pinyin)).lower()
        if not raw:
            return fallback()

        chars = list(token)
        candidates: List[List[str]] = []
        for ch in chars:
            if "\u4e00" <= ch <= "\u9fff":
                ps = pinyin(ch, style=Style.NORMAL, heteronym=True, errors="ignore")[0]
                cand = sorted({(x or "").lower() for x in ps if x})
                if not cand:
                    return fallback()
                candidates.append(cand)
            else:
                # 非中文字符：候选只能是字符本身（通常场景不会命中；命不中就回退）
                candidates.append([ch.lower()])

        n = len(chars)
        choice: dict = {}
        memo: dict = {}

        def dfs(i: int, pos: int) -> bool:
            key = (i, pos)
            if key in memo:
                return memo[key]
            if i == n:
                memo[key] = pos == len(raw)
                return memo[key]

            # 逐字候选匹配连续串：必须 raw[pos:] 以候选拼音前缀开头
            for cand in candidates[i]:
                if raw.startswith(cand, pos):
                    if dfs(i + 1, pos + len(cand)):
                        choice[key] = cand
                        memo[key] = True
                        return True

            memo[key] = False
            return False

        if not dfs(0, 0):
            return fallback()

        # reconstruct per-char matched syllables
        syllables: List[str] = []
        i = 0
        pos = 0
        while i < n:
            cand = choice.get((i, pos))
            if not cand:
                return fallback()
            syllables.append(cand)
            pos += len(cand)
            i += 1

        # mixed token formatting:
        # - 中文片段: `zu`he
        # - 连续英文/数字: 合并为一个片段并在片段内用空格分开，如 `z m`
        segments: List[str] = []
        ascii_buf: List[str] = []
        for ch, syl in zip(chars, syllables):
            if "\u4e00" <= ch <= "\u9fff":
                if ascii_buf:
                    segments.append(" ".join(ascii_buf))
                    ascii_buf = []
                segments.append(syl)
            else:
                ascii_buf.append(ch.lower())
        if ascii_buf:
            segments.append(" ".join(ascii_buf))

        return SogouDictManager._norm_pinyin("`" + "`".join(segments))

    def run_task(self) -> Optional[Path]:
        """完整流水线：下载 → 转换 → 导出。返回导出文件路径。"""
        if not self.check_environment():
            raise RuntimeError("Environment check failed")
        scel_file = self.download_dict()
        if not scel_file:
            raise RuntimeError("Download failed")
        txt_path = self.convert_scel_to_txt(scel_file.path)
        if not txt_path:
            raise RuntimeError("Convert failed")
        return self.export_words_to_files(txt_path)

    # ── 工具方法 ─────────────────────────────────────────────

    def _date_save_dir(self, date_str: Optional[str] = None) -> Path:
        """返回按日期分的子目录 output/sogou_newwords/YYYYMMDD/"""
        date_str = date_str or datetime.now().strftime("%Y%m%d")
        d = self.save_dir / date_str
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _latest_file(directory: Path, ext: str) -> Optional[Path]:
        if not directory.exists():
            return None
        files = []
        for root, dirs, filenames in os.walk(directory):
            for fn in filenames:
                if fn.endswith(ext):
                    files.append(Path(root) / fn)
        if not files:
            return None
        return max(files, key=lambda p: p.stat().st_mtime)

    @staticmethod
    def _read_words(txt_path: Path) -> List[Tuple[Optional[str], str]]:
        out: List[Tuple[Optional[str], str]] = []
        with open(txt_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if "\t" in line:
                    word, pinyin = line.split("\t", 1)
                    word = word.strip()
                    if word:
                        out.append((pinyin.strip() or None, word))
                elif " " in line:
                    pinyin, word = line.split(" ", 1)
                    word = word.strip()
                    if word:
                        out.append((pinyin.strip() or None, word))
                else:
                    out.append((None, line))
        return out

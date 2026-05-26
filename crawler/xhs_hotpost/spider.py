"""
crawler/xhs_hotpost/spider.py — 小红书热帖爬虫（后台 headless 模式）

流程:
  1. 读取最新的 xhs_hot TXT 文件，取前 N 条热搜关键词及热度
  2. 启动 XhsBrowser（headless，整个任务期间只启动一次）
  3. 对每个关键词先回到首页，再调搜索 API；**只取搜索结果第 1 条笔记（Top1）**，不再遍历更多
  4. 对该笔记：打开 explore 页后拉 /feed 与评论（失败则用搜索卡字段兜底）
  5. 去重 + 组装，写入 output/xhs_hotpost/{YYYYMMDD}/xhs_hotpost_{YYYYMMDD}_{HHMM}.json

认证 & 签名:
  使用持久化浏览器 profile（browser_profile/xhs/），window.mnsv2 真实签名。

输出 JSON 格式（每条帖子 17 字段 + comments 数组）:
  [{
    "note_id": "...", "title": "...", "desc": "...", "time": 1746518400,
    "nickname": "...", "liked_count": "...", "collected_count": "...",
    "comment_count": "...", "share_count": "...", "ip_location": "...",
    "tag_list": "tag1,tag2", "note_url": "https://...", "source_keyword": "...",
    "keyword_heat": 9439000, "xsec_token": "...",
    "comments": [
      {"comment_id":"...", "note_id":"...", "content":"...", "create_time":...,
       "nickname":"...", "ip_location":"...", "like_count":"...",
       "parent_comment_id": 0}
    ]
  }, ...]
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import random
import string
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from config import Config
from logger import get_logger
from crawler.xhs_hotpost.browser import XhsBrowser

logger = get_logger("crawler.xhs_hotpost")

_SEARCH_URI = "/api/sns/web/v1/search/notes"


# ── 时序工具 ─────────────────────────────────────────────────


def _jitter(base: float) -> float:
    """
    基准间隔加随机抖动，模拟真人操作节奏：
      - 80% 概率：0.7x–1.8x（正常浏览节奏）
      - 15% 概率：2x–4x  （"看了一会儿"）
      -  5% 概率：5x–10x （"被某个帖子吸引了"）
    """
    r = random.random()
    if r < 0.80:
        return base * random.uniform(0.7, 1.8)
    elif r < 0.95:
        return base * random.uniform(2.0, 4.0)
    else:
        return base * random.uniform(5.0, 10.0)


async def _reading_pause(base: float) -> None:
    """帖子内容加载后的"阅读停顿"，比普通间隔更长。"""
    await asyncio.sleep(_jitter(base * 1.5))


# ── 工具函数 ─────────────────────────────────────────────────


def _base36(number: int) -> str:
    chars = string.digits + string.ascii_lowercase
    out: List[str] = []
    while number:
        number, r = divmod(number, 36)
        out.append(chars[r])
    return "".join(reversed(out)) or "0"


def _search_id() -> str:
    return _base36((int(time.time() * 1000) << 64) + int(random.uniform(0, 2147483646)))


def _clean_title(title: str, desc: str) -> str:
    """有 title 用 title，否则取 desc 前 30 字。"""
    t = (title or "").strip()
    if not t:
        t = (desc or "").strip()[:30]
    return t


def _format_tag_list(tags) -> str:
    """note_card.tag_list 是 list[dict]，只取 type=topic 的 name 拼成逗号分隔。"""
    if not isinstance(tags, list):
        return ""
    names = [t.get("name", "") for t in tags if isinstance(t, dict) and t.get("type") == "topic"]
    return ",".join(n for n in names if n)


from urllib.parse import quote


def _note_url(note_id: str, xsec_token: str) -> str:
    return (
        f"https://www.xiaohongshu.com/explore/{note_id}"
        f"?xsec_token={quote(xsec_token, safe='')}&xsec_source=pc_search"
    )


def _parse_cn_number(s) -> int:
    """把 XHS 返回的互动数字符串转成整数。
    支持格式: '1234' / '1.2万' / '12.3w' / None / int
    """
    if s is None:
        return 0
    if isinstance(s, int):
        return s
    s = str(s).strip().lower().replace(",", "")
    try:
        if "万" in s or "w" in s:
            num = float(s.replace("万", "").replace("w", ""))
            return int(num * 10000)
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def _item_liked_count(item: Dict) -> int:
    """从搜索结果 item 的 note_card.interact_info 里提取 liked_count。"""
    nc = item.get("note_card") or {}
    interact = nc.get("interact_info") or {}
    return _parse_cn_number(interact.get("liked_count"))


def _detail_from_search_item(item: Dict, note_id: str) -> Dict:
    """搜索列表里的 note_card 字段较少，feed 失败时用于兜底。"""
    nc = item.get("note_card")
    if isinstance(nc, dict) and nc:
        base = dict(nc)
    else:
        base = dict(item)
    base.setdefault("note_id", note_id)
    return base


# ── 爬虫主体 ─────────────────────────────────────────────────


class XhsHotPostSpider:
    """小红书热帖爬虫 —— 后台 headless 模式"""

    SOURCE_NAME = "xhs_hotpost"

    def __init__(self) -> None:
        self.save_dir = Config.XHS_HOTPOST_SAVE_DIR
        self.profile_dir = Config.XHS_HOTPOST_BROWSER_PROFILE
        self.keywords_dir = Config.XHS_HOTPOST_KEYWORDS_DIR
        self.max_keywords = Config.XHS_HOTPOST_MAX_KEYWORDS
        self.fetch_comments = Config.XHS_HOTPOST_FETCH_COMMENTS
        self.comments_per_note = Config.XHS_HOTPOST_COMMENTS_PER_NOTE
        self.sleep_sec = Config.XHS_HOTPOST_SLEEP_SEC
        self.mode = Config.XHS_HOTPOST_MODE
        self.homefeed_count = Config.XHS_HOTPOST_HOMEFEED_COUNT
        self.candidates_per_kw = Config.XHS_HOTPOST_CANDIDATES_PER_KW
        os.makedirs(self.save_dir, exist_ok=True)

    # ── 内部 ────────────────────────────────────────────────

    def _date_save_dir(self, date_str: Optional[str] = None) -> str:
        date_str = date_str or datetime.now().strftime("%Y%m%d")
        d = os.path.join(self.save_dir, date_str)
        os.makedirs(d, exist_ok=True)
        return d

    def _clean_old_files(self) -> None:
        try:
            cutoff = datetime.now() - timedelta(days=Config.JSON_FILE_RETENTION_DAYS)
            for root, _, files in os.walk(self.save_dir):
                for fname in files:
                    if not fname.endswith(".json"):
                        continue
                    fp = os.path.join(root, fname)
                    if datetime.fromtimestamp(os.path.getmtime(fp)) < cutoff:
                        try:
                            os.remove(fp)
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(f"清理旧文件失败（可忽略）: {e}")

    def _load_keywords(self) -> List[Tuple[str, int]]:
        """
        从关键词目录读取最新 TXT 文件，返回 (keyword, heat) 列表。
        TXT 每行: keyword \\t pinyin \\t heat

        若 XHS 专属热搜目录无文件，自动兜底到微博/抖音热搜（它们每天刷新，
        可为 xhs_hotpost 提供当日真实热词），避免永远用同一批静态词条。
        """
        def _read_txt(path: str) -> List[Tuple[str, int]]:
            out: List[Tuple[str, int]] = []
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        parts = line.strip().split("\t")
                        if not parts or not parts[0]:
                            continue
                        kw = parts[0]
                        heat = 0
                        if len(parts) >= 3:
                            try:
                                heat = int(parts[2])
                            except ValueError:
                                pass
                        out.append((kw, heat))
                        if self.max_keywords and self.max_keywords > 0 and len(out) >= self.max_keywords:
                            break
            except Exception as e:
                logger.warning(f"读取关键词文件失败 {path}: {e}")
            return out

        # 主来源：xhs_hot 目录
        files = sorted(
            glob.glob(os.path.join(self.keywords_dir, "**", "*.txt"), recursive=True)
        )
        if files:
            latest = files[-1]
            logger.info(f"读取关键词文件: {latest}")
            kws = _read_txt(latest)
            if kws:
                logger.info(f"加载关键词 {len(kws)} 条（来源: xhs_hot）")
                return kws

        # 兜底来源：微博热搜 / 抖音热榜（每天刷新，格式相同）
        # 这些目录与 xhs_hot 平行，由同一套 cron 产生
        parent = os.path.dirname(self.keywords_dir)
        fallback_sources = ["weibo_hotsearch", "douyin_hotlist"]
        for src in fallback_sources:
            fallback_dir = os.path.join(parent, src)
            fb_files = sorted(
                glob.glob(os.path.join(fallback_dir, "**", "*.txt"), recursive=True)
            )
            if not fb_files:
                continue
            latest_fb = fb_files[-1]
            logger.warning(
                f"xhs_hot 目录无可用文件，使用兜底来源 [{src}]: {latest_fb}"
            )
            kws = _read_txt(latest_fb)
            if kws:
                logger.info(f"加载关键词 {len(kws)} 条（来源: {src}）")
                return kws

        logger.warning(f"未找到任何关键词文件，目录: {self.keywords_dir}")
        return []

    # ── API 调用封装 ────────────────────────────────────────

    async def _search(self, browser: XhsBrowser, keyword: str) -> Optional[List[Dict]]:
        """调搜索接口，返回 items 列表（过滤掉 rec_query 等非笔记项）。"""
        payload = {
            "keyword": keyword,
            "page": 1,
            "page_size": 20,
            "search_id": _search_id(),
            "sort": "general",
            "note_type": 0,
        }
        data = await browser.post(_SEARCH_URI, payload)
        if data is None:
            return None
        code = data.get("code", -1)
        if code in (471, 461):
            logger.error(f"XHS 风控触发 (code={code})，请重新登录")
            return None
        if code != 0:
            logger.warning(f"搜索异常 code={code} msg={data.get('msg')} kw={keyword!r}")
            return None
        items = data.get("data", {}).get("items", [])
        return [it for it in items if it.get("model_type") not in ("rec_query", "hot_query")]

    @staticmethod
    def _normalize_ts(v) -> int:
        """XHS 时间戳可能是毫秒或秒，统一为秒。"""
        try:
            v = int(v or 0)
        except (TypeError, ValueError):
            return 0
        return v // 1000 if v > 1e12 else v

    def _format_comments(self, raw_comments: List[Dict], note_id: str) -> List[Dict]:
        """把 XHS 评论原始格式转成下游期望的精简格式。"""
        out: List[Dict] = []
        for c in raw_comments[: self.comments_per_note]:
            user_info = c.get("user_info", {}) or {}
            target = c.get("target_comment", {}) or {}
            out.append({
                "comment_id": c.get("id", ""),
                "note_id": note_id,
                "content": c.get("content", ""),
                "create_time": self._normalize_ts(c.get("create_time")),
                "nickname": user_info.get("nickname", ""),
                "ip_location": c.get("ip_location", ""),
                "like_count": str(c.get("like_count", "0")),
                "parent_comment_id": target.get("id", 0) if target else 0,
            })
        return out

    def _assemble_note(
        self,
        note: Dict,
        note_id: str,
        xsec_token: str,
        source_keyword: str,
        keyword_heat: int,
        comments: List[Dict],
    ) -> Dict:
        """组装 17 字段输出"""
        interact = note.get("interact_info", {}) or {}
        user = note.get("user", {}) or {}
        raw_title = note.get("title") or note.get("display_title", "")
        raw_desc = note.get("desc", "") or ""

        return {
            "note_id": note_id,
            "title": _clean_title(raw_title, raw_desc),
            "desc": raw_desc,
            "time": self._normalize_ts(note.get("time")),
            "nickname": user.get("nickname", "") or user.get("nick_name", ""),
            "liked_count": str(interact.get("liked_count", "0")),
            "collected_count": str(interact.get("collected_count", "0")),
            "comment_count": str(interact.get("comment_count", "0")),
            "share_count": str(interact.get("share_count") or interact.get("shared_count") or "0"),
            "ip_location": note.get("ip_location", ""),
            "tag_list": _format_tag_list(note.get("tag_list")),
            "note_url": _note_url(note_id, xsec_token),
            "source_keyword": source_keyword,
            "keyword_heat": keyword_heat,
            "xsec_token": xsec_token,
            "comments": comments,
        }

    # ── 热词刷新 ─────────────────────────────────────────────

    def _save_live_keywords(
        self,
        kw_list: List[Tuple[str, int]],
        daily_date: str,
        slot_str: str,
    ) -> None:
        """
        把通过登录接口获取的热搜关键词写入 xhs_hot 目录（TXT 格式，与 rebang.today 相同）。
        下游 pipeline 的 scan_all_data 会识别这些文件，使之覆盖 rebang.today 的缓存数据。
        """
        from pypinyin import lazy_pinyin

        def _pinyin(w: str) -> str:
            result = ""
            for ch in w:
                if "\u4e00" <= ch <= "\u9fff":
                    py = lazy_pinyin(ch)
                    if py and py[0].isascii() and py[0].isalpha():
                        result += "`" + py[0].lower()
                elif ch.isascii() and ch.isalpha():
                    result += "`" + ch.lower()
            return result

        day_dir = os.path.join(self.keywords_dir, daily_date)
        os.makedirs(day_dir, exist_ok=True)
        txt_file = os.path.join(day_dir, f"xhs_hot_{daily_date}_{slot_str}.txt")
        try:
            with open(txt_file, "w", encoding="utf-8") as f:
                for kw, heat in kw_list:
                    py = _pinyin(kw)
                    f.write(f"{kw}\t{py}\t{heat}\n")
            logger.info(f"热搜关键词（登录版）已保存: {txt_file}  {len(kw_list)} 条")
        except Exception as e:
            logger.warning(f"保存热搜关键词文件失败（忽略）: {e}")

    # ── 主流程：homefeed 模式（无登录，直接拿推荐流）─────────

    async def _run_homefeed_async(
        self,
        run_day: Optional[str] = None,
        run_slot: Optional[str] = None,
    ) -> Optional[str]:
        """
        homefeed 模式：打开首页拿推荐流的帖子，对每个帖子拉详情和评论。
        不依赖登录态，不走搜索接口，避免触发 search/notes 类的风控。
        """
        daily_date = run_day or datetime.now().strftime("%Y%m%d")
        slot_str = run_slot or datetime.now().strftime("%H%M")

        results: List[Dict] = []
        seen_note_ids: set = set()

        async with XhsBrowser(self.profile_dir, headless=Config.XHS_HOTPOST_HEADLESS) as browser:
            items = await browser.fetch_homefeed_notes(max_count=self.homefeed_count)
            if not items:
                logger.warning("homefeed 返回空，任务终止")
                return None

            logger.info(f"准备处理 {len(items)} 条推荐帖子")
            consecutive_failures = 0

            for idx, item in enumerate(items):
                nc = item.get("note_card") or {}
                note_id = item.get("id") or nc.get("note_id", "")
                xsec_token = item.get("xsec_token") or nc.get("xsec_token", "")

                if not note_id or not xsec_token:
                    logger.debug(f"[{idx+1}/{len(items)}] 缺少 note_id/xsec_token，跳过")
                    continue
                if note_id in seen_note_ids:
                    continue

                # 取个标题预览方便日志阅读
                title_preview = (nc.get("display_title") or nc.get("title") or "")[:30]
                logger.info(f"[{idx+1}/{len(items)}] note={note_id}  {title_preview!r}")

                detail, explore_url = await browser.load_note_detail(note_id, xsec_token)
                if not detail:
                    detail = _detail_from_search_item(item, note_id)
                    logger.debug(f"  feed 不可用，使用 homefeed 卡片字段兜底 note={note_id}")

                # 阅读停顿（推荐流上每条停 1-3 秒更像人）
                await _reading_pause(self.sleep_sec)

                raw_comments: List[Dict] = []
                if self.fetch_comments:
                    raw_comments = await browser.load_note_comments(
                        note_id, xsec_token, explore_url, self.comments_per_note
                    )
                    if not raw_comments:
                        consecutive_failures += 1
                        if consecutive_failures >= 5:
                            logger.warning("评论连续 5 次为空，停止后续评论拉取（保护账号）")
                            self.fetch_comments = False
                    else:
                        consecutive_failures = 0

                comments = self._format_comments(raw_comments, note_id)
                seen_note_ids.add(note_id)
                # homefeed 模式没有"关键词"概念，用推荐位序号当 heat
                results.append(self._assemble_note(
                    detail, note_id, xsec_token,
                    source_keyword="homefeed",
                    keyword_heat=len(items) - idx,
                    comments=comments,
                ))

                await asyncio.sleep(_jitter(self.sleep_sec))

        if not results:
            logger.warning("homefeed 模式未获取到任何帖子")
            return None

        save_dir = self._date_save_dir(daily_date)
        json_file = os.path.join(save_dir, f"xhs_hotpost_{daily_date}_{slot_str}.json")
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        total_comments = sum(len(r.get("comments", [])) for r in results)
        logger.info(f"导出完成 (homefeed): {json_file}  {len(results)} 帖 / {total_comments} 评论")
        return json_file

    # ── 主流程：noauth 模式（无登录，SSR 推荐流）─────────────

    async def _run_noauth_async(
        self,
        run_day: Optional[str] = None,
        run_slot: Optional[str] = None,
    ) -> Optional[str]:
        """
        无登录态推荐流抓取。

        两种子模式：
          fetch_comments=False（默认）：
            临时干净 context + SSR HTML 解析，轻量快速，不拉评论。
          fetch_comments=True：
            持久化 profile（browser_profile/xhs_noauth/），
            feed + 评论在同一 browser session 里完成。
            a1 设备指纹跨次复用，随着运行次数积累 XHS 对该设备的信任度提升，
            评论接口从 461 逐步变 200（通常 1-3 次跑后开始有数据）。
        """
        from .noauth_fetcher import (
            fetch_noauth_explore_feed,
            fetch_noauth_feed_and_comments,
        )

        daily_date = run_day or datetime.now().strftime("%Y%m%d")
        slot_str = run_slot or datetime.now().strftime("%H%M")

        target = self.homefeed_count

        # 需要评论时走持久化 profile 版本（feed + 评论一次性完成）
        if self.fetch_comments:
            logger.info(
                f"noauth 模式（带评论）：使用持久化 profile "
                f"profile={Config.XHS_HOTPOST_NOAUTH_PROFILE}"
            )
            raw_list = await fetch_noauth_feed_and_comments(
                profile_dir=Config.XHS_HOTPOST_NOAUTH_PROFILE,
                max_count=target,
                max_per_note=self.comments_per_note,
                headless=Config.XHS_HOTPOST_HEADLESS,
            )
            results: List[Dict] = []
            for item in raw_list:
                note_id = item.get("note_id", "")
                if not note_id:
                    continue
                raw_cmts = item.pop("_raw_comments", [])
                item["comments"] = self._format_comments(raw_cmts, note_id)
                results.append(item)
        else:
            # 不需要评论：仍走轻量 SSR 临时 context
            rounds = max(1, (target + 19) // 20)
            feeds = await fetch_noauth_explore_feed(
                max_count=target,
                rounds=rounds,
                headless=Config.XHS_HOTPOST_HEADLESS,
            )
            if not feeds:
                logger.warning("noauth 模式：SSR feeds 为空，任务终止")
                return None
            results = []
            for item in feeds:
                note_id = item.get("note_id", "")
                if not note_id:
                    continue
                xsec_token = item.get("xsec_token", "")
                liked = _parse_cn_number(item.get("liked_count"))
                results.append({
                    "note_id":         note_id,
                    "title":           (item.get("title") or "").strip(),
                    "desc":            "",
                    "time":            0,
                    "nickname":        item.get("nickname", ""),
                    "liked_count":     str(liked),
                    "collected_count": str(_parse_cn_number(item.get("collected_count"))),
                    "comment_count":   str(_parse_cn_number(item.get("comment_count"))),
                    "share_count":     str(_parse_cn_number(item.get("share_count"))),
                    "ip_location":     item.get("ip_location", ""),
                    "tag_list":        "",
                    "note_url":        (
                        f"https://www.xiaohongshu.com/explore/{note_id}"
                        f"?xsec_token={quote(xsec_token, safe='')}&xsec_source=pc_explore"
                        if xsec_token else
                        f"https://www.xiaohongshu.com/explore/{note_id}"
                    ),
                    "source_keyword":  "noauth_explore",
                    "keyword_heat":    liked,
                    "xsec_token":      xsec_token,
                    "comments":        [],
                })

        if not results:
            logger.warning("noauth 模式：未获取到任何帖子")
            return None

        results.sort(key=lambda r: _parse_cn_number(r.get("liked_count")), reverse=True)

        save_dir = self._date_save_dir(daily_date)
        json_file = os.path.join(save_dir, f"xhs_hotpost_{daily_date}_{slot_str}.json")
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        total_comments = sum(len(r.get("comments", [])) for r in results)
        logger.info(
            f"导出完成 (noauth): {json_file}  {len(results)} 帖 / {total_comments} 评论"
        )
        return json_file

    # ── 主流程：search 模式（关键词搜索，需要登录态）────────

    async def run_async(
        self,
        run_day: Optional[str] = None,
        run_slot: Optional[str] = None,
    ) -> Optional[str]:
        logger.info("=" * 60)
        logger.info(f"执行时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
        logger.info(f"小红书热帖爬虫启动  mode={self.mode}  profile={self.profile_dir}")
        logger.info("=" * 60)

        self._clean_old_files()

        if self.mode == "homefeed":
            return await self._run_homefeed_async(run_day=run_day, run_slot=run_slot)
        if self.mode == "noauth":
            return await self._run_noauth_async(run_day=run_day, run_slot=run_slot)
        # 默认走原来的搜索流程（keyword）

        fallback_kw_list = self._load_keywords()

        daily_date = run_day or datetime.now().strftime("%Y%m%d")
        slot_str = run_slot or datetime.now().strftime("%H%M")

        results: List[Dict] = []
        seen_note_ids: set = set()

        async with XhsBrowser(self.profile_dir, headless=Config.XHS_HOTPOST_HEADLESS) as browser:
            # ── 优先用登录接口拉今日真实热搜关键词 ──────────────
            live_kw_list = await browser.fetch_hot_keywords(
                max_count=self.max_keywords or 20
            )
            if live_kw_list:
                logger.info(
                    f"使用登录接口热搜关键词 {len(live_kw_list)} 条"
                    f"（覆盖 rebang.today 缓存数据）"
                )
                self._save_live_keywords(live_kw_list, daily_date, slot_str)
                kw_list = live_kw_list
            else:
                logger.info(
                    "登录接口返回空，回退到 rebang.today 文件关键词"
                    f"（{len(fallback_kw_list)} 条）"
                )
                kw_list = fallback_kw_list

            if not kw_list:
                logger.warning("无关键词，任务终止")
                return None

            consecutive_failures = 0  # 连续失败计数，超阈值时提前退出保护账号

            for idx, (kw, heat) in enumerate(kw_list):
                logger.info(f"[{idx+1}/{len(kw_list)}] 搜索关键词: {kw!r}  (heat={heat})")

                # ── 导航策略：真人不会每次搜索前都回首页 ────────────
                # 第一次必须回首页；之后 35% 概率回首页，65% 直接搜索
                if idx == 0 or random.random() < 0.35:
                    await browser.goto_home()
                else:
                    # 不回首页时，用页面内停顿代替导航，模拟"还在搜索页"
                    await asyncio.sleep(random.uniform(0.8, 2.0))

                items = await self._search(browser, kw)
                if items is None:
                    logger.warning(f"  → 搜索失败，跳过")
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        logger.error("连续 3 次搜索失败，提前终止任务（账号可能触发风控）")
                        break
                    await asyncio.sleep(_jitter(self.sleep_sec))
                    continue
                consecutive_failures = 0

                if not items:
                    logger.info("  → 无笔记结果")
                    await asyncio.sleep(_jitter(self.sleep_sec))
                    continue

                # 从搜索前 candidates_per_kw 条里选 liked_count 最高的（未被收录的）
                candidates = items[: self.candidates_per_kw]
                # 过滤掉已收录的，按 liked_count 降序排列
                fresh = [
                    it for it in candidates
                    if (it.get("id") or (it.get("note_card") or {}).get("note_id", ""))
                    not in seen_note_ids
                ]
                if not fresh:
                    logger.info(f"  → 前 {len(candidates)} 条均已收录，跳过")
                    await asyncio.sleep(_jitter(self.sleep_sec))
                    continue
                item = max(fresh, key=_item_liked_count)
                best_liked = _item_liked_count(item)
                nc = item.get("note_card") or {}
                note_id = item.get("id") or nc.get("note_id", "")
                xsec_token = item.get("xsec_token") or nc.get("xsec_token", "")
                if not note_id or not xsec_token:
                    logger.warning("  → 候选笔记缺少 note_id/xsec_token，跳过")
                    await asyncio.sleep(_jitter(self.sleep_sec))
                    continue

                logger.info(f"  → 选出 liked={best_liked}  note={note_id}（候选 {len(fresh)} 条）")

                # ── "看搜索结果列表"停顿 ────────────────────────────
                await asyncio.sleep(random.uniform(0.5, 1.5))

                detail, explore_url = await browser.load_note_detail(note_id, xsec_token)
                if not detail:
                    detail = _detail_from_search_item(item, note_id)
                    logger.debug(f"  feed 不可用，使用搜索字段兜底 note={note_id}")

                # ── "阅读帖子内容"停顿（比普通间隔更长）──────────────
                await _reading_pause(self.sleep_sec)

                raw_comments: List[Dict] = []
                if self.fetch_comments:
                    raw_comments = await browser.load_note_comments(
                        note_id, xsec_token, explore_url, self.comments_per_note
                    )
                comments = self._format_comments(raw_comments, note_id)

                seen_note_ids.add(note_id)
                results.append(self._assemble_note(
                    detail, note_id, xsec_token, kw, heat, comments
                ))
                logger.info(f"  → 已收录 liked={best_liked}")

                # ── 关键词间隔（含深度停顿概率）──────────────────────
                await asyncio.sleep(_jitter(self.sleep_sec))

        if not results:
            logger.warning("所有关键词均未获取到帖子")
            return None

        # 按 liked_count 降序排列最终输出
        results.sort(key=lambda r: _parse_cn_number(r.get("liked_count")), reverse=True)

        save_dir = self._date_save_dir(daily_date)
        json_file = os.path.join(save_dir, f"xhs_hotpost_{daily_date}_{slot_str}.json")
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        total_comments = sum(len(r.get("comments", [])) for r in results)
        logger.info(f"导出完成: {json_file}  {len(results)} 帖 / {total_comments} 评论")
        return json_file

    def run(
        self,
        run_day: Optional[str] = None,
        run_slot: Optional[str] = None,
    ) -> Optional[str]:
        """同步入口（适配既有 run.py / 调度器）"""
        return asyncio.run(self.run_async(run_day=run_day, run_slot=run_slot))

"""
xhs_hotsearch/search_notes.py — 在小红书官网搜索关键词，提取前 N 个笔记链接。

XHS 搜索结果页对未登录访问完全屏蔽（整页被登录弹窗覆盖、无任何笔记锚点），
因此采用项目已有的 `crawler.xhs_hotpost.browser.XhsBrowser`：
  - 持久化浏览器 profile（带登录态）
  - 自动注入 window.mnsv2 真签名
  - 直接调用 /api/sns/web/v1/search/notes 拿 note_id + xsec_token
  - 用项目里已使用的格式拼出可访问的 explore 链接

⚠️ 首次使用前必须扫码登录一次：
    python -m crawler.xhs_hotpost.login

之后浏览器 profile（默认 browser_profile/xhs/）会持续复用，无需重复登录。
"""
from __future__ import annotations

import asyncio
import logging
import random
import string
import time
from typing import Dict, List, Optional
from urllib.parse import quote

from crawler.xhs_hotpost.browser import XhsBrowser

logger = logging.getLogger(__name__)

_SEARCH_URI = "/api/sns/web/v1/search/notes"


def _base36(n: int) -> str:
    chars = string.digits + string.ascii_lowercase
    out: List[str] = []
    while n:
        n, r = divmod(n, 36)
        out.append(chars[r])
    return "".join(reversed(out)) or "0"


def _gen_search_id() -> str:
    return _base36((int(time.time() * 1000) << 64) + int(random.uniform(0, 2147483646)))


def _note_url(note_id: str, xsec_token: str) -> str:
    """与 crawler/xhs_hotpost/spider.py 一致的 explore 链接格式。"""
    return (
        f"https://www.xiaohongshu.com/explore/{note_id}"
        f"?xsec_token={quote(xsec_token, safe='')}&xsec_source=pc_search"
    )


async def _search_one(
    browser: XhsBrowser, keyword: str, limit: int
) -> List[str]:
    payload = {
        "keyword": keyword,
        "page": 1,
        "page_size": max(limit, 20),
        "search_id": _gen_search_id(),
        "sort": "general",
        "note_type": 0,
        "ext_flags": [],
        "filters": [
            {"tags": ["general"], "type": "sort_type"},
            {"tags": ["不限"], "type": "filter_note_type"},
            {"tags": ["不限"], "type": "filter_note_time"},
            {"tags": ["不限"], "type": "filter_note_range"},
            {"tags": ["不限"], "type": "filter_pos_distance"},
        ],
        "geo": "",
        "image_formats": ["jpg", "webp", "avif"],
    }
    data = await browser.post(_SEARCH_URI, payload)
    if data is None:
        logger.warning(f"  搜索无响应: {keyword!r}")
        return []
    code = data.get("code", -1)
    if code != 0:
        msg = data.get("msg")
        if code in (461, 471):
            logger.error(
                f"  XHS 风控触发 code={code} msg={msg} kw={keyword!r}；"
                f"profile 可能失效，请重新登录"
            )
        else:
            logger.warning(f"  搜索异常 code={code} msg={msg} kw={keyword!r}")
        return []

    items = data.get("data", {}).get("items") or []
    out: List[str] = []
    seen = set()
    for it in items:
        if it.get("model_type") in ("rec_query", "hot_query"):
            continue
        note_id = it.get("id") or (it.get("note_card") or {}).get("note_id")
        xsec_token = it.get("xsec_token") or (it.get("note_card") or {}).get("xsec_token")
        if not note_id or not xsec_token:
            continue
        if note_id in seen:
            continue
        seen.add(note_id)
        out.append(_note_url(note_id, xsec_token))
        if len(out) >= limit:
            break
    return out


async def _run_async(
    keywords: List[str],
    notes_per_keyword: int,
    headless: bool,
    profile_dir: str,
    sleep_between: float,
) -> Dict[str, List[str]]:
    browser = XhsBrowser(profile_dir=profile_dir, headless=headless)
    await browser.start()
    try:
        result: Dict[str, List[str]] = {}
        for idx, kw in enumerate(keywords, 1):
            logger.info(f"[{idx}/{len(keywords)}] 搜索: {kw}")
            try:
                # 真人感：每隔几次回首页缓一下
                if idx > 1 and (idx - 1) % 5 == 0:
                    await browser.goto_home()
                    await asyncio.sleep(random.uniform(1.0, 2.5))
                links = await _search_one(browser, kw, notes_per_keyword)
            except Exception as e:
                logger.warning(f"  搜索异常: {e}")
                links = []
            result[kw] = links
            logger.info(f"  → 拿到 {len(links)} 条笔记链接")
            await asyncio.sleep(sleep_between * random.uniform(0.7, 1.6))
        return result
    finally:
        await browser.close()


def search_notes_for_keywords(
    keywords: List[str],
    profile_dir: str,
    notes_per_keyword: int = 5,
    headless: bool = True,
    sleep_between: float = 2.0,
) -> Dict[str, List[str]]:
    """
    对 keywords 逐个在 XHS 官网搜索，返回 {keyword: [note_url, ...]}。

    依赖已登录的 XHS profile（首次执行前请运行 `python -m crawler.xhs_hotpost.login`）。
    """
    if not keywords:
        return {}
    return asyncio.run(
        _run_async(
            keywords=keywords,
            notes_per_keyword=notes_per_keyword,
            headless=headless,
            profile_dir=profile_dir,
            sleep_between=sleep_between,
        )
    )

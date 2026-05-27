"""
xhs_tendencyhotsearch/fetch_trending.py — Step 1

通过 XhsBrowser 获取小红书"趋势词"候选列表：

  优先方案：直接 page-fetch 调 /api/sns/web/v1/search/trending/query
    (XHS 2026 起 querytrending 已改名为 trending/query)
  回退方案：模拟点击搜索框，路由拦截 trending 接口响应
    (browser.fetch_hot_keywords，依赖真实 DOM 事件)

⚠️ 首次使用前必须扫码登录一次：
    python -m crawler.xhs_hotpost.login
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Tuple, Optional

from crawler.xhs_hotpost.browser import XhsBrowser

logger = logging.getLogger(__name__)

_TRENDING_URI = "/api/sns/web/v1/search/trending/query"
_TRENDING_PARAMS = {
    "source": "Explore",
    "search_type": "trend",
    "last_query": "",
    "last_query_time": 0,
    "word_request_situation": "FIRST_ENTER",
    "hint_word": "",
    "hint_word_type": "",
    "hint_word_request_id": "",
}


def _parse_trending_data(data: Optional[dict], max_count: int) -> List[Tuple[str, int]]:
    """从 trending/query 响应里提取 [(keyword, heat), ...]。"""
    if not data:
        return []
    code = data.get("code", -1)
    if code not in (0, 1000):
        logger.warning(f"trending/query code={code} msg={data.get('msg')}")
        return []
    raw = data.get("data") or {}
    candidates = raw.get("queries") or raw.get("hot_queries") or raw.get("trending_words") or []
    if not candidates and isinstance(raw, list):
        candidates = raw

    result: List[Tuple[str, int]] = []
    for idx, item in enumerate(candidates[:max_count]):
        if isinstance(item, str):
            kw, heat = item.strip(), max_count - idx
        elif isinstance(item, dict):
            kw = (
                item.get("search_word") or item.get("title")
                or item.get("keyword") or item.get("word") or ""
            ).strip()
            raw_heat = (
                item.get("heat_score") or item.get("hot_value")
                or item.get("score") or item.get("view_num") or 0
            )
            try:
                heat = int(float(str(raw_heat))) or (max_count - idx)
            except Exception:
                heat = max_count - idx
        else:
            continue
        if kw:
            result.append((kw, heat))
    return result


async def _run_async(
    profile_dir: str, headless: bool, max_count: int
) -> List[Tuple[str, int]]:
    browser = XhsBrowser(profile_dir=profile_dir, headless=headless)
    await browser.start()
    try:
        # ① 优先：page-fetch + 签名 直请 trending/query
        try:
            data = await browser.get(_TRENDING_URI, dict(_TRENDING_PARAMS))
        except Exception as e:
            logger.warning(f"page-fetch 调 trending/query 异常: {e}")
            data = None
        items = _parse_trending_data(data, max_count)
        if items:
            logger.info(f"trending/query 直请成功，拿到 {len(items)} 个候选词")
            return items

        # ② 回退：DOM 触发拦截
        logger.info("trending/query 直请未返回有效数据，回退到 DOM 触发")
        return await browser.fetch_hot_keywords(max_count=max_count)
    finally:
        await browser.close()


def fetch_trending_keywords(
    profile_dir: str, headless: bool = True, max_count: int = 30
) -> List[Tuple[str, int]]:
    """同步入口；返回 [(keyword, raw_heat), ...]，按接口原顺序排序。"""
    return asyncio.run(
        _run_async(profile_dir=profile_dir, headless=headless, max_count=max_count)
    )


def save_candidates_txt(
    items: List[Tuple[str, int]],
    out_dir: Path,
    ts: Optional[datetime] = None,
) -> Path:
    """写候选词原始 TXT。

    文件名 trending_candidates_<YYYYMMDD_HHMM>.txt
    每行: rank<TAB>keyword<TAB>raw_heat
    """
    ts = ts or datetime.now()
    day = ts.strftime("%Y%m%d")
    stamp = ts.strftime("%Y%m%d_%H%M")
    day_dir = out_dir / day
    day_dir.mkdir(parents=True, exist_ok=True)
    fp = day_dir / f"trending_candidates_{stamp}.txt"
    lines = ["rank\tkeyword\traw_heat"]
    for idx, (kw, heat) in enumerate(items, 1):
        lines.append(f"{idx}\t{kw}\t{heat}")
    fp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return fp

"""
xhs_tendencyhotsearch/score_and_links.py — Step 2

对候选趋势词逐个调用 /api/sns/web/v1/search/notes，拿到笔记列表后：
  1. 提取每条笔记的互动数据（liked/comment/collected/shared）+ 发布时间 + 标题
  2. 计算"综合热度分"用于排序（公式见 _compute_score）
  3. 顺手收集前 N 条可访问的 explore 链接（与方案一格式一致）

输出：[{keyword, score, score_breakdown, total_items, links: [url, ...]}, ...]
按 score 从大到小排序。
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import re
import string
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from crawler.xhs_hotpost.browser import XhsBrowser

logger = logging.getLogger(__name__)

_SEARCH_URI = "/api/sns/web/v1/search/notes"

# 30 天 = 2_592_000 秒，用于"近期内容占比"判定
_RECENT_WINDOW_SECONDS = 30 * 24 * 3600


# ---------- 工具 ----------

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
    return (
        f"https://www.xiaohongshu.com/explore/{note_id}"
        f"?xsec_token={quote(xsec_token, safe='')}&xsec_source=pc_search"
    )


def _parse_publish_text_to_days_ago(text: str, now: Optional[datetime] = None) -> Optional[int]:
    """把 corner_tag_info 里的发布时间文本（'今天'/'昨天'/'3天前'/'04-22'/'2024-10-15'）
    转换成"距今天数"，无法解析返回 None。"""
    if not text:
        return None
    now = now or datetime.now()
    s = text.strip()
    if s in ("今天", "刚刚"):
        return 0
    if s == "昨天":
        return 1
    if s == "前天":
        return 2
    m = re.match(r"^(\d+)\s*天前$", s)
    if m:
        return int(m.group(1))
    m = re.match(r"^(\d+)\s*周前$", s)
    if m:
        return int(m.group(1)) * 7
    m = re.match(r"^(\d+)\s*个?月前$", s)
    if m:
        return int(m.group(1)) * 30
    # MM-DD（小红书当年笔记）
    m = re.match(r"^(\d{1,2})-(\d{1,2})$", s)
    if m:
        mo, day = int(m.group(1)), int(m.group(2))
        try:
            d = datetime(now.year, mo, day)
            if d > now:
                d = datetime(now.year - 1, mo, day)
            return (now - d).days
        except ValueError:
            return None
    # YYYY-MM-DD
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return (now - d).days
        except ValueError:
            return None
    return None


def _keyword_match_score(title: str, kw: str) -> float:
    """关键词命中：整体命中=1.0；长度≥4 的关键词用滑窗 2-gram 任一命中=0.5。"""
    if not title or not kw:
        return 0.0
    if kw in title:
        return 1.0
    if len(kw) >= 4:
        for i in range(len(kw) - 1):
            if kw[i:i + 2] in title:
                return 0.5
    return 0.0


def _parse_cn_number(raw) -> int:
    """把 '1.2w' / '3000+' / '100' / 数字 统一转 int。"""
    if raw is None:
        return 0
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw).strip().lower().replace("+", "")
    m = re.match(r"^([\d.]+)\s*([wW万kK千]?)$", s)
    if not m:
        return 0
    try:
        num = float(m.group(1))
        unit = m.group(2)
        if unit in ("w", "万"):
            num *= 10_000
        elif unit in ("k", "千"):
            num *= 1_000
        return int(num)
    except ValueError:
        return 0


# ---------- 单关键词搜索 ----------

async def _search_one(
    browser: XhsBrowser, keyword: str, page_size: int
) -> List[Dict]:
    """返回原始 item 列表（已过滤 rec_query / hot_query）。"""
    payload = {
        "keyword": keyword,
        "page": 1,
        "page_size": max(page_size, 20),
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
    if not data:
        return []
    code = data.get("code", -1)
    if code != 0:
        msg = data.get("msg")
        logger.warning(f"  搜索异常 code={code} msg={msg} kw={keyword!r}")
        return []
    items = data.get("data", {}).get("items") or []
    return [it for it in items if it.get("model_type") not in ("rec_query", "hot_query")]


# ---------- 综合分 ----------

def _extract_features(items: List[Dict], keyword: str, now: datetime) -> Dict:
    """从搜索结果聚合特征：互动总数、近期占比、有效笔记数、命中数。"""
    total = len(items)
    liked = comment = collected = share = 0
    recent_cnt = 0
    match_sum = 0.0

    for it in items:
        nc = it.get("note_card") or {}
        ia = nc.get("interact_info") or {}
        liked += _parse_cn_number(ia.get("liked_count"))
        comment += _parse_cn_number(ia.get("comment_count"))
        collected += _parse_cn_number(ia.get("collected_count"))
        # 新版字段是 shared_count，老版可能为 share_count，两者都试
        share += _parse_cn_number(ia.get("shared_count") or ia.get("share_count"))

        # 发布时间：优先 corner_tag_info（XHS 2026 新版），兜底 note_card.time
        days_ago: Optional[int] = None
        for ct in (nc.get("corner_tag_info") or []):
            if ct.get("type") == "publish_time":
                days_ago = _parse_publish_text_to_days_ago(ct.get("text"), now)
                break
        if days_ago is None:
            ts_val = nc.get("time") or nc.get("publish_time") or 0
            try:
                ts_int = int(ts_val)
                if ts_int > 10_000_000_000:
                    ts_int //= 1000
                if ts_int > 0:
                    days_ago = int((now.timestamp() - ts_int) // 86400)
            except (TypeError, ValueError):
                pass
        if days_ago is not None and 0 <= days_ago <= 30:
            recent_cnt += 1

        title = nc.get("display_title") or ""
        match_sum += _keyword_match_score(title, keyword)

    return {
        "total": total,
        "interact_sum": liked + 2 * comment + collected + 3 * share,  # 评论/分享权重稍高
        "liked": liked,
        "comment": comment,
        "collected": collected,
        "share": share,
        "recent_ratio": (recent_cnt / total) if total else 0.0,
        "effective_ratio": min(total, 20) / 20,
        "match_ratio": (match_sum / total) if total else 0.0,
    }


def _compute_score(
    feat: Dict, trending_rank: int, trending_total: int
) -> Tuple[float, Dict]:
    """综合分（0~100），同时返回各分项明细。"""
    # ① 趋势词排名分：排名越靠前分越高
    rank_score = (trending_total - trending_rank + 1) / trending_total

    # ② 互动分：log10 归一（10w 互动得满分 1.0）
    interact_score = min(math.log10(feat["interact_sum"] + 1) / 5.0, 1.0)

    # ③ 近期占比 / ④ 有效笔记数 / ⑤ 命中分
    recent_score = feat["recent_ratio"]
    effective_score = feat["effective_ratio"]
    match_score = feat["match_ratio"]

    weights = {
        "rank": 0.30,
        "interact": 0.35,
        "recent": 0.15,
        "effective": 0.10,
        "match": 0.10,
    }
    parts = {
        "rank": rank_score,
        "interact": interact_score,
        "recent": recent_score,
        "effective": effective_score,
        "match": match_score,
    }
    total = sum(weights[k] * parts[k] for k in weights) * 100
    breakdown = {k: round(parts[k], 4) for k in parts}
    breakdown["weighted_total"] = round(total, 2)
    return round(total, 2), breakdown


# ---------- 链接收集 ----------

def _collect_links(items: List[Dict], limit: int) -> List[str]:
    out: List[str] = []
    seen = set()
    for it in items:
        note_id = it.get("id") or (it.get("note_card") or {}).get("note_id")
        xsec = it.get("xsec_token") or (it.get("note_card") or {}).get("xsec_token")
        if not note_id or not xsec:
            continue
        if note_id in seen:
            continue
        seen.add(note_id)
        out.append(_note_url(note_id, xsec))
        if len(out) >= limit:
            break
    return out


# ---------- orchestrator ----------

async def _run_async(
    candidates: List[Tuple[str, int]],
    profile_dir: str,
    headless: bool,
    page_size: int,
    notes_per_keyword: int,
    sleep_between: float,
) -> List[Dict]:
    browser = XhsBrowser(profile_dir=profile_dir, headless=headless)
    await browser.start()
    now = datetime.now()
    total = len(candidates)
    try:
        results: List[Dict] = []
        for idx, (kw, raw_heat) in enumerate(candidates, 1):
            logger.info(f"[{idx}/{total}] 搜索验证: {kw} (raw_heat={raw_heat})")
            try:
                if idx > 1 and (idx - 1) % 5 == 0:
                    await browser.goto_home()
                    await asyncio.sleep(random.uniform(1.0, 2.5))
                items = await _search_one(browser, kw, page_size)
            except Exception as e:
                logger.warning(f"  搜索异常: {e}")
                items = []

            feat = _extract_features(items, kw, now)
            score, breakdown = _compute_score(feat, trending_rank=idx, trending_total=total)
            links = _collect_links(items, notes_per_keyword)
            logger.info(
                f"  → items={feat['total']} interact={feat['interact_sum']} "
                f"recent={feat['recent_ratio']:.2f} match={feat['match_ratio']:.2f} "
                f"score={score} links={len(links)}"
            )
            results.append({
                "keyword": kw,
                "raw_heat": raw_heat,
                "trending_rank": idx,
                "score": score,
                "breakdown": breakdown,
                "features": feat,
                "links": links,
            })
            await asyncio.sleep(sleep_between * random.uniform(0.7, 1.6))
        return results
    finally:
        await browser.close()


def score_and_collect(
    candidates: List[Tuple[str, int]],
    profile_dir: str,
    headless: bool = True,
    page_size: int = 20,
    notes_per_keyword: int = 5,
    sleep_between: float = 2.0,
) -> List[Dict]:
    """对候选词做搜索验证 + 综合分 + 收集笔记链接。

    返回按 score 从大到小排序的列表。
    """
    if not candidates:
        return []
    results = asyncio.run(
        _run_async(
            candidates=candidates,
            profile_dir=profile_dir,
            headless=headless,
            page_size=page_size,
            notes_per_keyword=notes_per_keyword,
            sleep_between=sleep_between,
        )
    )
    results.sort(key=lambda r: r["score"], reverse=True)
    return results

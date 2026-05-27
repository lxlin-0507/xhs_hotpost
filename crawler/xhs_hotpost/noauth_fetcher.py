"""
crawler/xhs_hotpost/noauth_fetcher.py — 无登录态推荐流 + 评论抓取

两个公开函数:

fetch_noauth_explore_feed(max_count, rounds, headless)
    与原来相同：每次启动临时干净 context，从 explore SSR HTML 解析帖子列表。
    不持久化任何状态，适合只需要帖子列表（不拉评论）的情况。

fetch_noauth_feed_and_comments(profile_dir, max_count, max_per_note, headless)
    使用持久化 profile（无登录，但 a1/web_session 跨次复用）：
      1. 拦截 /api/sns/web/v1/homefeed API 响应拿帖子列表（比 SSR 解析更准确）
      2. 对每篇帖子：在同一个 browser session 里导航到 explore 详情，
         拦截 /api/sns/web/v2/comment/page 响应拿评论
    关键原理：
      - persistent context 让 a1 设备指纹在每次跑之间保留，多次跑后 XHS
        服务端把这台"设备"当成老用户，评论接口从 461 变 200
      - feed token 和评论请求在同一 session 内发出，不存在跨 session
        token 失效的问题（这是之前失败的根本原因）
      - xsec_source=pc_feed + referer=explore 模拟真人"首页推荐流 → 点进详情"
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

_EXPLORE_URL = "https://www.xiaohongshu.com/explore"
_COMMENT_URI = "/api/sns/web/v2/comment/page"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
]


def _to_int(v) -> int:
    """把 XHS 互动数字符串转成整数，支持 '1.2万' / '12.3w' / int / None。"""
    if v is None:
        return 0
    if isinstance(v, int):
        return v
    s = str(v).strip().lower().replace(",", "")
    try:
        if "万" in s or "w" in s:
            return int(float(s.replace("万", "").replace("w", "")) * 10000)
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def _apply_stealth(page):
    """返回 coroutine（可 await），失败静默忽略。"""
    async def _inner():
        try:
            from playwright_stealth import Stealth
            await Stealth(
                navigator_platform_override="MacIntel",
                navigator_languages_override=("zh-CN", "zh"),
            ).apply_stealth_async(page)
        except Exception:
            pass
    return _inner()


# ── 原有函数（不改，供只需要 feed 列表的场景继续用）──────────────────────


def _parse_segments(html: str) -> List[Dict]:
    """按 '"id":"<24位hex>"' 切分 HTML，逐段提取 note_card 字段。"""
    segments = re.split(r'(?="id"\s*:\s*"[0-9a-f]{24}")', html)
    seen: Dict[str, Dict] = {}

    for seg in segments:
        nid_m = re.search(r'"id"\s*:\s*"([0-9a-f]{24})"', seg)
        if not nid_m:
            continue
        note_id = nid_m.group(1)
        if note_id in seen:
            continue

        def g(pattern: str, default: str = "") -> str:
            m = re.search(pattern, seg)
            return m.group(1) if m else default

        liked = g(r'"likedCount"\s*:\s*"([^"]*)"', "0")
        if not liked or liked == "0":
            continue

        seen[note_id] = {
            "note_id":         note_id,
            "xsec_token":      g(r'"xsecToken"\s*:\s*"([^"]+)"'),
            "title":           g(r'"displayTitle"\s*:\s*"([^"]*)"'),
            "liked_count":     liked,
            "comment_count":   g(r'"commentCount"\s*:\s*"([^"]*)"', "0"),
            "collected_count": g(r'"collectedCount"\s*:\s*"([^"]*)"', "0"),
            "share_count":     g(r'"shareCount"\s*:\s*"([^"]*)"', "0"),
            "nickname":        g(r'"nickName"\s*:\s*"([^"]*)"'),
            "ip_location":     g(r'"ipLocation"\s*:\s*"([^"]*)"'),
        }

    return list(seen.values())


async def fetch_noauth_explore_feed(
    max_count: int = 20,
    rounds: int = 1,
    headless: bool = True,
) -> List[Dict]:
    """
    从 XHS 推荐页（不登录，临时 context）抓取热帖列表。
    不拉评论，不持久化状态。
    """
    accumulated: Dict[str, Dict] = {}

    async with async_playwright() as pw:
        for r in range(rounds):
            if len(accumulated) >= max_count:
                break

            browser = await pw.chromium.launch(headless=headless, args=_LAUNCH_ARGS)
            ctx = await browser.new_context(
                user_agent=_UA,
                viewport={"width": 1280, "height": 800},
            )
            page = await ctx.new_page()
            await _apply_stealth(page)
            try:
                try:
                    await page.goto(_EXPLORE_URL, wait_until="domcontentloaded", timeout=20000)
                except Exception as e:
                    logger.warning(f"goto explore 失败 (round {r+1}): {e}")
                await asyncio.sleep(3)

                html = await page.content()
                items = _parse_segments(html)
                fresh = [it for it in items if it["note_id"] not in accumulated]
                for it in fresh:
                    accumulated[it["note_id"]] = it

                logger.info(
                    f"noauth round {r+1}/{rounds}: SSR 解析 {len(items)} 条，"
                    f"新增 {len(fresh)} 条，累计 {len(accumulated)} 条"
                )
            finally:
                await ctx.close()
                await browser.close()

    return list(accumulated.values())[:max_count]


def note_url(note_id: str, xsec_token: str) -> str:
    return (
        f"https://www.xiaohongshu.com/explore/{note_id}"
        f"?xsec_token={quote(xsec_token, safe='')}&xsec_source=pc_explore"
    )


# ── 新函数：持久化 context，一次性拿 feed + 评论 ────────────────────────


async def fetch_noauth_feed_and_comments(
    profile_dir: str,
    max_count: int = 20,
    max_per_note: int = 5,
    headless: bool = True,
    comment_timeout: float = 8.0,
) -> List[Dict]:
    """
    无登录态，使用持久化 profile，一次任务里同时拿帖子列表和评论。

    持久化 profile 的意义：
      XHS 给每台"设备"颁发 a1 cookie（设备指纹）。
      临时 context 每次 a1 都是陌生新值 → 评论接口 461。
      持久化 profile 让 a1 跨多次跑保持不变，随着访问次数积累，
      XHS 服务端逐渐把这台设备当成"普通游客"而非爬虫，评论接口开放。
      通常第 1-3 次跑评论可能还是 0，之后会稳定有数据。

    feed 获取策略（三档降级）：
      1. 拦截 homefeed API 响应（最准，带完整 xsec_token）
      2. 从 window.__INITIAL_STATE__ 提取
      3. SSR HTML 正则解析

    评论获取策略：
      在同一 browser session 里 goto 每篇详情页（referer=explore, xsec_source=pc_feed），
      拦截 /comment/page 响应。token 与 cookie 同源，不存在跨 session 失效。

    Returns:
        [{note_id, title, liked_count, ..., comments: [...]}, ...]
        comments 内每条格式与登录态接口一致（下划线字段），
        spider._format_comments 可直接消费。
    """
    import os as _os
    _os.makedirs(profile_dir, exist_ok=True)

    results: List[Dict] = []

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=headless,
            user_agent=_UA,
            viewport={"width": 1280, "height": 800},
            args=_LAUNCH_ARGS,
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await _apply_stealth(page)

        # ── Step 1: 拿 feed 列表 ────────────────────────────────
        # 数据策略（三档合并）：
        #   A. xsec_token —— 必须用 DOM 渲染后 <a href> 上的 token（"本次 session"
        #      有效）。SSR HTML / API 里那份是"分享 token"，权限低，进详情页
        #      评论接口会被风控。
        #   B. 互动数 SSR 兜底 —— explore 首屏 SSR HTML 只含 likedCount，
        #      解析后作为 fallback。
        #   C. 互动数主源 —— 拦截 /api/sns/web/v1/homefeed 分页响应，
        #      首屏不发，需滚动到底触发。响应 JSON 里 note_card.interact_info
        #      含完整 liked/collected/comment/share。
        homefeed_meta: Dict[str, Dict] = {}

        async def on_explore_response(resp):
            url = resp.url
            if "/api/sns/web/v1/homefeed" not in url:
                return
            try:
                data = await resp.json()
                if data.get("code") != 0:
                    return
                items = (data.get("data") or {}).get("items") or []
                added = 0
                for it in items:
                    nid = it.get("id") or (it.get("note_card") or {}).get("note_id")
                    nc = it.get("note_card") or {}
                    if not nid or not nc:
                        continue
                    ii = nc.get("interact_info") or {}
                    user = nc.get("user") or {}
                    homefeed_meta[nid] = {
                        "title":           nc.get("display_title") or "",
                        "desc":            "",
                        "liked_count":     ii.get("liked_count") or "0",
                        "collected_count": ii.get("collected_count") or "0",
                        "comment_count":   ii.get("comment_count") or "0",
                        "share_count":     ii.get("share_count") or "0",
                        "nickname":        user.get("nickname") or "",
                        "ip_location":     "",
                        "time":            0,
                    }
                    added += 1
                logger.info(f"[homefeed_api] +{added} items, 累计 {len(homefeed_meta)}")
            except Exception as e:
                logger.debug(f"[homefeed_api] parse err: {e}")

        page.on("response", on_explore_response)

        try:
            await page.goto(_EXPLORE_URL, wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass

        await asyncio.sleep(2)
        # 滚动触发分页：滚到底部、回到顶部、再往下，多次触发 homefeed API
        for i in range(6):
            try:
                await page.mouse.wheel(0, 3000)
                await asyncio.sleep(1.2)
            except Exception:
                break

        page.remove_listener("response", on_explore_response)

        # SSR HTML 解析作为兜底（首屏 liked_count）
        ssr_filled = 0
        try:
            explore_html = await page.content()
            ssr_items = _parse_segments(explore_html)
            for it in ssr_items:
                nid = it.get("note_id")
                if not nid:
                    continue
                if nid in homefeed_meta:
                    continue  # 已有 API 数据，跳过
                homefeed_meta[nid] = {
                    "title":           it.get("title", ""),
                    "desc":            "",
                    "liked_count":     it.get("liked_count", "0"),
                    "collected_count": it.get("collected_count", "0"),
                    "comment_count":   it.get("comment_count", "0"),
                    "share_count":     it.get("share_count", "0"),
                    "nickname":        it.get("nickname", ""),
                    "ip_location":     it.get("ip_location", ""),
                    "time":            0,
                }
                ssr_filled += 1
            logger.info(f"[ssr] HTML 兜底解析 +{ssr_filled} 条，总 metadata={len(homefeed_meta)}")
        except Exception as e:
            logger.warning(f"[ssr] explore HTML 解析失败: {e}")

        dom_links = await page.evaluate(r"""(maxCount) => {
            const out = [];
            const seen = new Set();
            document.querySelectorAll('a[href*="xsec_token="]').forEach(a => {
                const m = a.getAttribute('href').match(/\/explore\/([0-9a-f]{24})/i);
                if (!m) return;
                const noteId = m[1];
                if (seen.has(noteId)) return;
                const u = new URL(a.href, location.origin);
                const tok = u.searchParams.get('xsec_token');
                if (!tok) return;
                seen.add(noteId);
                // 从卡片找标题
                let title = '';
                const card = a.closest('section,div[class*="note"],div[class*="card"]');
                if (card) {
                    const t = card.querySelector('[class*="title"],.title');
                    if (t) title = (t.innerText || t.textContent || '').trim();
                }
                out.push({id: noteId, tok, title});
                if (out.length >= maxCount) return;
            });
            return out;
        }""", max_count)

        feed_items = [
            {"id": it["id"], "xsec_token": it["tok"],
             "note_card": {"display_title": it.get("title", "")},
             "_from_dom": True}
            for it in (dom_links or [])
        ]

        if not feed_items:
            logger.warning("fetch_noauth_feed_and_comments: 未获取到任何 feed")
            await ctx.close()
            return []

        logger.info(f"fetch_noauth_feed_and_comments: 获取 {len(feed_items)} 条 feed")

        # ── Step 2: 逐条 goto 详情，拦截评论 + 帖子 detail API ────
        comment_map: Dict[str, List[Dict]] = {}
        note_meta_map: Dict[str, Dict] = {}   # note_id -> 从 API 取到的元数据
        current_nid: List[Optional[str]] = [None]
        comment_evt = asyncio.Event()

        _NOTE_DETAIL_URI = "/api/sns/web/v4/note/detail"

        def _parse_note_meta(data: dict) -> Optional[Dict]:
            """从 note/detail API JSON 里提取 interact 元数据。"""
            try:
                note = (
                    (data.get("data") or {}).get("note")
                    or (data.get("data") or {}).get("items", [{}])[0].get("note_card")
                    or {}
                )
                interact = note.get("interact_info") or {}
                user = note.get("user") or {}
                def sv(v): return str(v) if v is not None else "0"
                # XHS detail API 里 tag_list 可能是：
                #   [{"id":"...","name":"百合花","type":"topic"}, ...]
                # 只保留 type 为 "topic" 的项，提取 name
                raw_tags = note.get("tag_list") or note.get("tagList") or []
                tag_names: list[str] = []
                if isinstance(raw_tags, list):
                    for t in raw_tags:
                        if isinstance(t, dict):
                            ttype = (t.get("type") or "").lower()
                            name = (t.get("name") or "").strip()
                            if name and (not ttype or ttype in ("topic", "interact_topic", "buyable")):
                                tag_names.append(name)
                        elif isinstance(t, str):
                            name = t.strip()
                            if name:
                                tag_names.append(name)
                return {
                    "title":           str(note.get("title") or note.get("display_title") or ""),
                    "desc":            str(note.get("desc") or ""),
                    "liked_count":     sv(interact.get("liked_count") or interact.get("likedCount") or "0"),
                    "collected_count": sv(interact.get("collected_count") or interact.get("collectedCount") or "0"),
                    "comment_count":   sv(interact.get("comment_count") or interact.get("commentCount") or "0"),
                    "share_count":     sv(interact.get("share_count") or interact.get("shareCount") or "0"),
                    "nickname":        str(user.get("nickname") or ""),
                    "ip_location":     str(note.get("ip_location") or note.get("ipLocation") or ""),
                    "time":            note.get("time") or 0,
                    "tag_list":        tag_names,
                }
            except Exception:
                return None

        async def on_response(resp):
            url = resp.url
            try:
                if _COMMENT_URI in url:
                    m = re.search(r"note_id=([0-9a-fA-F]+)", url)
                    nid = m.group(1) if m else None
                    if not nid:
                        return
                    data = await resp.json()
                    code = data.get("code")
                    cmts = (data.get("data") or {}).get("comments") or []
                    logger.info(f"[comment] HTTP {resp.status} code={code} +{len(cmts)} note={nid}")
                    if code == 0 and cmts:
                        comment_map.setdefault(nid, []).extend(cmts)
                        if nid == current_nid[0]:
                            comment_evt.set()
                elif _NOTE_DETAIL_URI in url:
                    data = await resp.json()
                    code = data.get("code")
                    nid = current_nid[0]
                    logger.info(f"[note_detail] HTTP {resp.status} code={code} note={nid}")
                    if code == 0 and nid:
                        parsed = _parse_note_meta(data)
                        if parsed:
                            note_meta_map[nid] = parsed
                            logger.debug(f"[note_detail] liked={parsed['liked_count']} collected={parsed['collected_count']}")
            except Exception as e:
                logger.debug(f"[on_response] parse err url={url[:60]} err={e}")

        page.on("response", on_response)

        for idx, item in enumerate(feed_items):
            nc = item.get("note_card") or {}
            note_id = item.get("id") or nc.get("note_id", "")
            xsec_token = item.get("xsec_token") or nc.get("xsec_token", "")
            if not note_id or not xsec_token:
                continue

            # 优先使用 homefeed API 缓存的 metadata（稳定来源）
            hf_meta = homefeed_meta.get(note_id)
            liked = (
                (hf_meta or {}).get("liked_count")
                or (nc.get("interact_info", {}) or {}).get("liked_count")
                or "0"
            )
            title = (
                (hf_meta or {}).get("title")
                or nc.get("display_title")
                or nc.get("displayTitle")
                or ""
            ).strip()

            current_nid[0] = note_id
            comment_evt.clear()

            detail_url = (
                f"https://www.xiaohongshu.com/explore/{note_id}"
                f"?xsec_token={quote(xsec_token, safe='')}&xsec_source=pc_feed"
            )
            try:
                await page.goto(
                    detail_url,
                    wait_until="domcontentloaded",
                    timeout=20000,
                    referer=_EXPLORE_URL,
                )
                await asyncio.sleep(1.5)   # 等 Vue/SSR 数据写入 DOM
            except Exception as e:
                logger.warning(f"[{idx+1}/{len(feed_items)}] goto {note_id} 失败: {e}")
                _append_result(results, note_id, xsec_token, title, liked, [], None)
                continue

            # ── meta 优先级：homefeed > note_detail API > __INITIAL_STATE__ > html_regex ──
            meta = hf_meta or note_meta_map.get(note_id)
            if not meta:
                try:
                    meta = await page.evaluate(r"""() => {
                        try {
                            const s = window.__INITIAL_STATE__;
                            if (!s) return null;
                            // 尝试多条路径
                            const noteMap = (s.note && s.note.noteDetailMap) || {};
                            const keys = Object.keys(noteMap);
                            const entry = keys.length ? noteMap[keys[0]] : null;
                            const note = entry && (entry.note || entry);
                            if (!note || typeof note !== 'object') return null;
                            function safe(v) { return (v === null || v === undefined) ? '' : String(v); }
                            const interact = note.interactInfo || note.interact_info || {};
                            const user = note.user || {};
                            const liked = interact.likedCount || interact.liked_count;
                            if (!liked) return null;   // 空状态，放弃
                            // 提取 tag_list
                            const rawTags = note.tagList || note.tag_list || [];
                            const tagNames = [];
                            if (Array.isArray(rawTags)) {
                                rawTags.forEach(t => {
                                    if (!t) return;
                                    if (typeof t === 'string') {
                                        const s = t.trim();
                                        if (s) tagNames.push(s);
                                    } else if (typeof t === 'object') {
                                        const tp = (t.type || '').toLowerCase();
                                        const nm = (t.name || '').trim();
                                        if (nm && (!tp || tp === 'topic' || tp === 'interact_topic' || tp === 'buyable')) {
                                            tagNames.push(nm);
                                        }
                                    }
                                });
                            }
                            return {
                                title: safe(note.title || note.displayTitle || ''),
                                desc: safe(note.desc || ''),
                                liked_count: safe(liked),
                                collected_count: safe(interact.collectedCount || interact.collected_count || '0'),
                                comment_count: safe(interact.commentCount || interact.comment_count || '0'),
                                share_count: safe(interact.shareCount || interact.share_count || '0'),
                                nickname: safe(user.nickname || user.nickName || ''),
                                ip_location: safe(note.ipLocation || note.ip_location || ''),
                                time: note.time || 0,
                                tag_list: tagNames,
                            };
                        } catch(e) { return null; }
                    }""")
                    if meta:
                        logger.debug(f"[__INITIAL_STATE__] got meta for {note_id}")
                    else:
                        logger.debug(f"[__INITIAL_STATE__] meta=None for {note_id}")
                except Exception as exc:
                    logger.debug(f"[__INITIAL_STATE__] evaluate err: {exc}")
                    meta = None

            # ── 最终兜底：直接从页面 HTML 正则提取 ──
            if not meta:
                try:
                    html = await page.content()
                    def _g(pat, default="0"):
                        m2 = re.search(pat, html)
                        return m2.group(1) if m2 else default
                    liked_html  = _g(r'"likedCount"\s*:\s*"([^"]*)"') or _g(r'"liked_count"\s*:\s*"([^"]*)"')
                    coll_html   = _g(r'"collectedCount"\s*:\s*"([^"]*)"') or _g(r'"collected_count"\s*:\s*"([^"]*)"')
                    cmt_html    = _g(r'"commentCount"\s*:\s*"([^"]*)"') or _g(r'"comment_count"\s*:\s*"([^"]*)"')
                    share_html  = _g(r'"shareCount"\s*:\s*"([^"]*)"') or _g(r'"share_count"\s*:\s*"([^"]*)"')
                    nick_html   = _g(r'"nickname"\s*:\s*"([^"]*)"')
                    title_html  = _g(r'"title"\s*:\s*"([^"]*)"', "")
                    desc_html   = _g(r'"desc"\s*:\s*"([^"]*)"', "")
                    # 从 HTML 里抽 tag_list 的 name 字段
                    tag_html: list[str] = []
                    for m_tag in re.finditer(
                        r'"tag_list"\s*:\s*\[(.*?)\]', html, re.DOTALL
                    ):
                        block = m_tag.group(1)
                        for nm in re.findall(r'"name"\s*:\s*"([^"]+)"', block):
                            nm = nm.strip()
                            if nm and nm not in tag_html:
                                tag_html.append(nm)
                        break  # 只取第一个 tag_list
                    if liked_html and liked_html != "0":
                        meta = {
                            "title":           title_html or title,
                            "desc":            desc_html,
                            "liked_count":     liked_html,
                            "collected_count": coll_html,
                            "comment_count":   cmt_html,
                            "share_count":     share_html,
                            "nickname":        nick_html,
                            "ip_location":     "",
                            "time":            0,
                            "tag_list":        tag_html,
                        }
                        logger.debug(f"[html_regex] liked={liked_html} collected={coll_html} for {note_id}")
                    else:
                        logger.warning(f"[html_regex] 仍无数据 note={note_id}, 页面可能被风控/跳转登录")
                except Exception as exc:
                    logger.debug(f"[html_regex] err: {exc}")

            if meta:
                if meta.get("title"):
                    title = meta["title"]
                liked = meta.get("liked_count", liked)

            # 滚动触发评论懒加载
            for _ in range(3):
                try:
                    await page.mouse.wheel(0, 1500)
                    await asyncio.sleep(0.6)
                except Exception:
                    break

            try:
                await asyncio.wait_for(comment_evt.wait(), timeout=comment_timeout)
            except asyncio.TimeoutError:
                pass

            raw_cmts = comment_map.get(note_id, [])[:max_per_note]
            if hf_meta:
                src = "ssr"
            elif note_id in note_meta_map:
                src = "detail_api"
            elif meta:
                src = "fallback"
            else:
                src = "none"
            logger.info(
                f"[{idx+1}/{len(feed_items)}] note={note_id}  "
                f"comments={len(raw_cmts)}  liked={liked}  meta_src={src}"
            )
            _append_result(results, note_id, xsec_token, title, liked, raw_cmts, meta)

        page.remove_listener("response", on_response)
        await ctx.close()

    return results


def _append_result(
    results: List[Dict],
    note_id: str,
    xsec_token: str,
    title: str,
    liked_count,
    raw_cmts: List[Dict],
    meta: Optional[Dict],
) -> None:
    """把一条 feed + 原始评论组装成 spider 期望的 note dict（不含 comments 格式化）。"""
    m = meta or {}
    # tag_list 从 meta 多路抽取合并：structured + desc 里的后缀式话题正则
    tags: List[str] = []
    raw_tags = m.get("tag_list")
    if isinstance(raw_tags, list):
        for t in raw_tags:
            if isinstance(t, str):
                t = t.strip()
                if t and t not in tags:
                    tags.append(t)
    # desc 里 “#XX[话题]#” 格式作为补充
    desc_text = m.get("desc") or ""
    for nm in re.findall(r"#([^#\n]+?)\[话题\]#", desc_text):
        nm = nm.strip()
        if nm and nm not in tags:
            tags.append(nm)
    results.append({
        "note_id":         note_id,
        "title":           m.get("title") or title,
        "desc":            m.get("desc", ""),
        "time":            m.get("time", 0),
        "nickname":        m.get("nickname", ""),
        "liked_count":     str(_to_int(m.get("liked_count") or liked_count)),
        "collected_count": str(_to_int(m.get("collected_count"))),
        "comment_count":   str(_to_int(m.get("comment_count"))),
        "share_count":     str(_to_int(m.get("share_count"))),
        "ip_location":     m.get("ip_location", ""),
        "tag_list":        tags,
        "note_url":        (
            f"https://www.xiaohongshu.com/explore/{note_id}"
            f"?xsec_token={quote(xsec_token, safe='')}&xsec_source=pc_feed"
        ),
        "source_keyword":  "noauth_explore",
        "keyword_heat":    _to_int(m.get("liked_count") or liked_count),
        "xsec_token":      xsec_token,
        "_raw_comments":   raw_cmts,
    })

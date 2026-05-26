"""
crawler/xhs_hotpost/browser.py — Playwright 签名服务（headless 后台常驻）

设计:
  整个爬取任务期间只启动**一次** headless Chromium，
  打开 https://www.xiaohongshu.com 让小红书 SPA 注入 window.mnsv2，
  然后所有 API 请求都复用同一个 Page，通过 page.evaluate
  调用 window.mnsv2(sign_str, md5_str) 获取真实 x3 值。

  搜索接口用 page.evaluate(fetch)（正常工作）。
  /feed 和评论接口改用 httpx + 静态 PC 头 + 同步 Cookie，
  与 MediaCrawler 相同的请求形态，避免 406 风控。

参考: MediaCrawler/media_platform/xhs/playwright_sign.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import time
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import quote

import httpx

from playwright.async_api import async_playwright, BrowserContext, Page, Playwright

from logger import get_logger

logger = get_logger("crawler.xhs_hotpost.browser")

XHS_HOME = "https://www.xiaohongshu.com"
_FEED_URI = "/api/sns/web/v1/feed"
_COMMENT_URI = "/api/sns/web/v2/comment/page"

# ============================================================
# 纯 Python 部分：拼请求头（移植自 MediaCrawler/xhs_sign.py）
# x3 之外的字段（x-s payload / x-s-common）都可以本地算
# ============================================================

BASE64_CHARS = list("ZmserbBoHQtNP+wOcza/LpngG8yJq42KWYj0DSfdikx3VT16IlUAFM97hECvuRX5")

CRC32_TABLE = [
    0, 1996959894, 3993919788, 2567524794, 124634137, 1886057615, 3915621685,
    2657392035, 249268274, 2044508324, 3772115230, 2547177864, 162941995,
    2125561021, 3887607047, 2428444049, 498536548, 1789927666, 4089016648,
    2227061214, 450548861, 1843258603, 4107580753, 2211677639, 325883990,
    1684777152, 4251122042, 2321926636, 335633487, 1661365465, 4195302755,
    2366115317, 997073096, 1281953886, 3579855332, 2724688242, 1006888145,
    1258607687, 3524101629, 2768942443, 901097722, 1119000684, 3686517206,
    2898065728, 853044451, 1172266101, 3705015759, 2882616665, 651767980,
    1373503546, 3369554304, 3218104598, 565507253, 1454621731, 3485111705,
    3099436303, 671266974, 1594198024, 3322730930, 2970347812, 795835527,
    1483230225, 3244367275, 3060149565, 1994146192, 31158534, 2563907772,
    4023717930, 1907459465, 112637215, 2680153253, 3904427059, 2013776290,
    251722036, 2517215374, 3775830040, 2137656763, 141376813, 2439277719,
    3865271297, 1802195444, 476864866, 2238001368, 4066508878, 1812370925,
    453092731, 2181625025, 4111451223, 1706088902, 314042704, 2344532202,
    4240017532, 1658658271, 366619977, 2362670323, 4224994405, 1303535960,
    984961486, 2747007092, 3569037538, 1256170817, 1037604311, 2765210733,
    3554079995, 1131014506, 879679996, 2909243462, 3663771856, 1141124467,
    855842277, 2852801631, 3708648649, 1342533948, 654459306, 3188396048,
    3373015174, 1466479909, 544179635, 3110523913, 3462522015, 1591671054,
    702138776, 2966460450, 3352799412, 1504918807, 783551873, 3082640443,
    3233442989, 3988292384, 2596254646, 62317068, 1957810842, 3939845945,
    2647816111, 81470997, 1943803523, 3814918930, 2489596804, 225274430,
    2053790376, 3826175755, 2466906013, 167816743, 2097651377, 4027552580,
    2265490386, 503444072, 1762050814, 4150417245, 2154129355, 426522225,
    1852507879, 4275313526, 2312317920, 282753626, 1742555852, 4189708143,
    2394877945, 397917763, 1622183637, 3604390888, 2714866558, 953729732,
    1340076626, 3518719985, 2797360999, 1068828381, 1219638859, 3624741850,
    2936675148, 906185462, 1090812512, 3747672003, 2825379669, 829329135,
    1181335161, 3412177804, 3160834842, 628085408, 1382605366, 3423369109,
    3138078467, 570562233, 1426400815, 3317316542, 2998733608, 733239954,
    1555261956, 3268935591, 3050360625, 752459403, 1541320221, 2607071920,
    3965973030, 1969922972, 40735498, 2617837225, 3943577151, 1913087877,
    83908371, 2512341634, 3803740692, 2075208622, 213261112, 2463272603,
    3855990285, 2094854071, 198958881, 2262029012, 4057260610, 1759359992,
    534414190, 2176718541, 4139329115, 1873836001, 414664567, 2282248934,
    4279200368, 1711684554, 285281116, 2405801727, 4167216745, 1634467795,
    376229701, 2685067896, 3608007406, 1308918612, 956543938, 2808555105,
    3495958263, 1231636301, 1047427035, 2932959818, 3654703836, 1088359270,
    936918000, 2847714899, 3736837829, 1202900863, 817233897, 3183342108,
    3401237130, 1404277552, 615818150, 3134207493, 3453421203, 1423857449,
    601450431, 3009837614, 3294710456, 1567103746, 711928724, 3020668471,
    3272380065, 1510334235, 755167117,
]


def _right_shift_unsigned(num: int, bit: int = 0) -> int:
    import ctypes
    val = ctypes.c_uint32(num).value >> bit
    MAX32 = 4294967295
    return (val + (MAX32 + 1)) % (2 * (MAX32 + 1)) - MAX32 - 1


def _mrc(e: str) -> int:
    o = -1
    for n in range(min(57, len(e))):
        o = CRC32_TABLE[(o & 255) ^ ord(e[n])] ^ _right_shift_unsigned(o, 8)
    return o ^ -1 ^ 3988292384


def _triplet_to_base64(e: int) -> str:
    return (
        BASE64_CHARS[(e >> 18) & 63]
        + BASE64_CHARS[(e >> 12) & 63]
        + BASE64_CHARS[(e >> 6) & 63]
        + BASE64_CHARS[e & 63]
    )


def _encode_chunk(data: List[int], start: int, end: int) -> str:
    result = []
    for i in range(start, end, 3):
        c = ((data[i] << 16) & 0xFF0000) + ((data[i + 1] << 8) & 0xFF00) + (data[i + 2] & 0xFF)
        result.append(_triplet_to_base64(c))
    return "".join(result)


def _encode_utf8(s: str) -> List[int]:
    encoded = quote(s, safe="~()*!.'")
    result: List[int] = []
    i = 0
    while i < len(encoded):
        if encoded[i] == "%":
            result.append(int(encoded[i + 1 : i + 3], 16))
            i += 3
        else:
            result.append(ord(encoded[i]))
            i += 1
    return result


def _b64_encode(data: List[int]) -> str:
    length = len(data)
    remainder = length % 3
    chunks: List[str] = []
    main_length = length - remainder
    for i in range(0, main_length, 16383):
        chunks.append(_encode_chunk(data, i, min(i + 16383, main_length)))
    if remainder == 1:
        a = data[length - 1]
        chunks.append(BASE64_CHARS[a >> 2] + BASE64_CHARS[(a << 4) & 63] + "==")
    elif remainder == 2:
        a = (data[length - 2] << 8) + data[length - 1]
        chunks.append(
            BASE64_CHARS[a >> 10]
            + BASE64_CHARS[(a >> 4) & 63]
            + BASE64_CHARS[(a << 2) & 63]
            + "="
        )
    return "".join(chunks)


def _md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _trace_id() -> str:
    return "".join(random.choice("abcdef0123456789") for _ in range(16))


def _build_sign_string(uri: str, data: Optional[Union[Dict, str]], method: str) -> str:
    if method.upper() == "POST":
        c = uri
        if data is not None:
            if isinstance(data, dict):
                c += json.dumps(data, separators=(",", ":"), ensure_ascii=False)
            elif isinstance(data, str):
                c += data
        return c
    if not data or (isinstance(data, dict) and len(data) == 0):
        return uri
    if isinstance(data, dict):
        params = []
        for key, value in data.items():
            if isinstance(value, list):
                vs = ",".join(str(v) for v in value)
            elif value is not None:
                vs = str(value)
            else:
                vs = ""
            params.append(f"{key}={quote(vs, safe='')}")
        return f"{uri}?{'&'.join(params)}"
    if isinstance(data, str):
        return f"{uri}?{data}"
    return uri


def _build_xs(x3_value: str, data_type: str) -> str:
    s = {"x0": "4.2.1", "x1": "xhs-pc-web", "x2": "Mac OS", "x3": x3_value, "x4": data_type}
    return "XYS_" + _b64_encode(_encode_utf8(json.dumps(s, separators=(",", ":"))))


def _build_xs_common(a1: str, b1: str, x_s: str, x_t: str) -> str:
    payload = {
        "s0": 3, "s1": "", "x0": "1", "x1": "4.2.2", "x2": "Mac OS",
        "x3": "xhs-pc-web", "x4": "4.74.0", "x5": a1, "x6": x_t,
        "x7": x_s, "x8": b1, "x9": _mrc(x_t + x_s + b1),
        "x10": 154, "x11": "normal",
    }
    return _b64_encode(_encode_utf8(json.dumps(payload, separators=(",", ":"))))


# ============================================================
# Playwright 部分：维护浏览器实例，提供 sign()
# ============================================================


class XhsBrowser:
    """
    小红书签名浏览器 —— 后台 headless 常驻、一次启动多次签名

    关键设计:
      使用 launch_persistent_context(user_data_dir=...) 复用 login.py 创建的
      持久化浏览器 profile。这样 cookies / localStorage / 设备指纹完全保持
      一致，签名（依赖 a1 设备指纹）才能通过小红书风控校验。
    """

    # 与 MediaCrawler 对齐的静态 PC 请求头（/feed 和评论接口使用）
    _STATIC_HEADERS = {
        "origin":           "https://www.xiaohongshu.com",
        "referer":          "https://www.xiaohongshu.com/",
        "sec-fetch-site":   "same-site",
        "sec-fetch-mode":   "cors",
        "sec-fetch-dest":   "empty",
        "sec-ch-ua":        '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "accept":          "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
    }

    def __init__(self, profile_dir: str, headless: bool = True) -> None:
        self.profile_dir = profile_dir
        self.headless = headless
        self.cookie_dict: Dict[str, str] = {}
        self._pw: Optional[Playwright] = None
        self._ctx: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._http: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        if not os.path.isdir(self.profile_dir):
            raise FileNotFoundError(
                f"未找到浏览器 profile 目录: {self.profile_dir}\n"
                "请先运行: python3 -m crawler.xhs_hotpost.login"
            )

        logger.info(f"启动 headless 浏览器  profile={self.profile_dir}")

        self._pw = await async_playwright().start()
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=self.profile_dir,
            headless=self.headless,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-dev-shm-usage",
            ],
        )

        # 跨平台兜底：如果 profile 里有 storage_state.json（由 login.py 在登录后导出），
        # 把里面的 cookies 注入到当前 context。原因：mac 上 Chromium 的 Cookies SQLite
        # 用 Keychain 加密，跨平台搬到 Linux 后无法解密，等同于无 cookie。
        # storage_state JSON 是明文，可任意复制。
        state_path = os.path.join(self.profile_dir, "storage_state.json")
        if os.path.isfile(state_path):
            try:
                import json as _json
                with open(state_path, "r", encoding="utf-8") as f:
                    state = _json.load(f)
                cookies_to_add = state.get("cookies") or []
                if cookies_to_add:
                    await self._ctx.add_cookies(cookies_to_add)
                    logger.info(
                        f"已从 storage_state.json 注入 {len(cookies_to_add)} 条 cookie"
                    )
            except Exception as e:
                logger.warning(f"加载 storage_state.json 失败（忽略）: {e}")

        # 读取 profile 中的 cookies
        cookies = await self._ctx.cookies(XHS_HOME)
        self.cookie_dict = {c["name"]: c["value"] for c in cookies if c.get("name") and c.get("value")}
        # 已登录的 web_session 通常 >= 40 字符且以 04 开头；匿名 session 较短
        ws = self.cookie_dict.get("web_session", "")
        if not ws or len(ws) < 30:
            await self.close()
            raise RuntimeError(
                f"profile 中 web_session 缺失或异常 (len={len(ws)})，请重新登录：\n"
                f"  python3 -m crawler.xhs_hotpost.login"
            )
        logger.info(f"加载 {len(self.cookie_dict)} 条 cookie  web_session={ws[:10]}…")

        # httpx client：共享同一 Cookie，用于 /feed 和评论接口
        self._http = httpx.AsyncClient(
            headers={**self._STATIC_HEADERS, "cookie": self.cookie_header},
            timeout=30.0,
            follow_redirects=True,
        )

        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()

        try:
            from playwright_stealth import Stealth
            await Stealth(
                navigator_platform_override="MacIntel",
                navigator_languages_override=("zh-CN", "zh"),
            ).apply_stealth_async(self._page)
            logger.info("playwright-stealth 已注入")
        except ImportError:
            logger.warning("playwright-stealth 未安装，跳过 stealth 注入（建议 pip install playwright-stealth）")
        except Exception as e:
            logger.warning(f"playwright-stealth 注入失败（忽略）: {e}")

        await self._page.goto(XHS_HOME, wait_until="domcontentloaded", timeout=20000)

        # 等待小红书 SPA 注入 window.mnsv2
        for _ in range(30):
            ok = await self._page.evaluate("typeof window.mnsv2 === 'function'")
            if ok:
                logger.info("window.mnsv2 已就绪")
                return
            await asyncio.sleep(1)
        raise RuntimeError("加载 xiaohongshu.com 后 window.mnsv2 未注入，可能 profile 已失效")

    async def sign(
        self,
        uri: str,
        data: Optional[Union[Dict, str]] = None,
        method: str = "POST",
    ) -> Dict[str, str]:
        """生成一次完整的签名头（X-S / X-T / x-S-Common / X-B3-Traceid）"""
        if self._page is None:
            raise RuntimeError("XhsBrowser 未启动，请先 await start()")

        sign_str = _build_sign_string(uri, data, method)
        md5_str = _md5_hex(sign_str)

        # 调用页面里的 window.mnsv2 取真 x3
        sign_str_e = sign_str.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        md5_str_e = md5_str.replace("\\", "\\\\").replace("'", "\\'")
        x3_value = await self._page.evaluate(
            f"window.mnsv2('{sign_str_e}', '{md5_str_e}')"
        )
        if not x3_value:
            raise RuntimeError("window.mnsv2 返回空，签名失败")

        data_type = "object" if isinstance(data, (dict, list)) else "string"
        x_s = _build_xs(x3_value, data_type)
        x_t = str(int(time.time() * 1000))

        a1 = self.cookie_dict.get("a1", "")
        try:
            b1 = await self._page.evaluate("() => window.localStorage.getItem('b1') || ''")
        except Exception:
            b1 = ""

        return {
            "X-S": x_s,
            "X-T": x_t,
            "x-S-Common": _build_xs_common(a1, b1 or "", x_s, x_t),
            "X-B3-Traceid": _trace_id(),
        }

    @property
    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookie_dict.items())

    async def goto_home(self) -> None:
        """
        回到首页，同时模拟真人的"浏览动作"：随机滚动 + 鼠标移动。

        这些动作发生在导航之后，使浏览器与页面的交互模式更接近真实用户，
        降低被 XHS 反爬系统识别为自动化工具的概率。
        """
        if self._page is None:
            return
        try:
            await self._page.goto(XHS_HOME, wait_until="domcontentloaded", timeout=20000)

            # 随机停顿（模拟页面刚加载完的视线落点）
            await asyncio.sleep(random.uniform(0.8, 2.0))

            # 随机鼠标移动（真人进入页面后眼睛/手都会动）
            vp = self._page.viewport_size or {"width": 1280, "height": 800}
            for _ in range(random.randint(2, 5)):
                x = random.randint(100, vp["width"] - 100)
                y = random.randint(100, vp["height"] - 100)
                await self._page.mouse.move(x, y)
                await asyncio.sleep(random.uniform(0.1, 0.4))

            # 随机滚动（模拟扫了一眼首页内容）
            scroll_px = random.randint(150, 600)
            await self._page.evaluate(f"window.scrollBy(0, {scroll_px})")
            await asyncio.sleep(random.uniform(0.3, 0.9))
            # 部分概率再向上滚回去（真人有时会回头看）
            if random.random() < 0.4:
                await self._page.evaluate(f"window.scrollBy(0, -{random.randint(50, scroll_px)})")
                await asyncio.sleep(random.uniform(0.2, 0.5))

        except Exception as e:
            logger.warning(f"回到首页失败: {e}")

    async def _sync_cookies(self) -> None:
        """从浏览器 context 同步最新 cookie 到 httpx client（解决 web_session 刷新后失效问题）。"""
        if self._ctx is None or self._http is None:
            return
        try:
            cookies = await self._ctx.cookies(XHS_HOME)
            self.cookie_dict = {
                c["name"]: c["value"]
                for c in cookies
                if c.get("name") and c.get("value")
            }
            self._http.headers.update({"cookie": self.cookie_header})
        except Exception:
            pass

    async def _httpx_post(self, uri: str, payload: Dict) -> Optional[Dict]:
        """用 httpx 发 POST（/feed 等），与 MediaCrawler 相同的请求形态。"""
        if self._http is None:
            return None
        await self._sync_cookies()
        sign_headers = await self.sign(uri, payload, "POST")
        url = "https://edith.xiaohongshu.com" + uri
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        try:
            resp = await self._http.post(
                url,
                content=body,
                headers={"content-type": "application/json;charset=UTF-8", **sign_headers},
            )
            if resp.status_code not in (200, 406, 461):
                logger.warning(f"{uri} HTTP {resp.status_code}: {resp.text[:200]}")
                return None
            return resp.json()
        except Exception as e:
            logger.warning(f"{uri} httpx 请求失败: {e}")
            return None

    async def _httpx_get(self, uri: str, params: Dict) -> Optional[Dict]:
        """用 httpx 发 GET（评论等），与 MediaCrawler 相同的请求形态。"""
        if self._http is None:
            return None
        await self._sync_cookies()
        sign_headers = await self.sign(uri, params, "GET")
        qs = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
        url = "https://edith.xiaohongshu.com" + uri + ("?" + qs if qs else "")
        try:
            resp = await self._http.get(url, headers=sign_headers)
            # XHS 部分接口以 HTTP 406/461 返回有效 JSON body
            if resp.status_code not in (200, 406, 461):
                logger.warning(f"{uri} HTTP {resp.status_code}: {resp.text[:200]}")
                return None
            try:
                return resp.json()
            except Exception:
                logger.warning(f"{uri} JSON 解析失败: {resp.text[:200]}")
                return None
        except Exception as e:
            logger.warning(f"{uri} httpx 请求失败: {e}")
            return None

    async def load_note_detail(
        self, note_id: str, xsec_token: str, timeout_ms: int = 20000
    ) -> Tuple[Optional[Dict], str]:
        """
        通过页面内 fetch 调 /feed，绕过新账号对 httpx 直连的 406 限制。

        返回 (note_card 或 None, explore_url) 供后续评论接口使用。
        """
        safe_tok = quote(xsec_token, safe="")
        explore_url = (
            f"https://www.xiaohongshu.com/explore/{note_id}"
            f"?xsec_token={safe_tok}&xsec_source=pc_search"
        )
        payload = {
            "source_note_id": note_id,
            "image_formats": ["jpg", "webp", "avif"],
            "extra": {"need_body_topic": 1},
            "xsec_source": "pc_search",
            "xsec_token": xsec_token,
        }
        # 优先走 page.evaluate(fetch)（带完整浏览器上下文，新账号也能通过）
        data = await self.post(_FEED_URI, payload, via="page")
        # 如果页面 fetch 也失败，回退到 httpx（对老账号/服务器环境仍可用）
        if data is None:
            data = await self._httpx_post(_FEED_URI, payload)
        if not data:
            return None, explore_url
        if data.get("code") != 0:
            # code=-1 + HTTP 406 = 账号权限不足（新账号常见），用搜索字段兜底即可
            logger.debug(
                f"feed 不可用 note={note_id} code={data.get('code')}（将使用搜索字段兜底）"
            )
            return None, explore_url
        items = data.get("data", {}).get("items") or []
        if not items:
            logger.warning(f"feed 无 items note={note_id}")
            return None, explore_url
        card = items[0].get("note_card") or None
        return card, explore_url

    async def load_note_comments(
        self,
        note_id: str,
        xsec_token: str,
        referer: str,
        max_count: int = 5,
    ) -> List[Dict]:
        """用 httpx 拉取一级评论。收到 461（风控拦截）时直接返回空，不重试。"""
        params = {
            "note_id": note_id,
            "cursor": "",
            "top_comment_id": "",
            "image_formats": "jpg,webp,avif",
            "xsec_token": xsec_token,
        }
        data = await self._httpx_get(_COMMENT_URI, params)
        # _httpx_get 对 461 返回 None；不重试以避免加重风控
        if data is None:
            return []
        if data.get("code") != 0:
            logger.warning(f"评论接口异常 code={data.get('code')} note={note_id}")
            return []
        raw = data.get("data", {}).get("comments") or []
        return raw[:max_count]

    async def post(
        self,
        uri: str,
        payload: Dict,
        *,
        referer: Optional[str] = None,
        via: str = "page",
    ) -> Optional[Dict]:
        """
        带签名的 POST。

        - via=\"page\"：页面内 fetch（搜索 /notes 等，与线上行为一致）。
        - via=\"context\"：APIRequest + 显式 Referer/Origin（用于 /feed 等）。
        """
        if self._ctx is None or self._page is None:
            raise RuntimeError("XhsBrowser 未启动")

        sign_headers = await self.sign(uri, payload, "POST")
        url = "https://edith.xiaohongshu.com" + uri
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

        if via == "context":
            ref = referer if referer is not None else f"{XHS_HOME}/"
            headers = {
                "Content-Type": "application/json;charset=UTF-8",
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://www.xiaohongshu.com",
                "Referer": ref,
                **sign_headers,
            }
            try:
                resp = await self._ctx.request.post(url, data=body, headers=headers, timeout=30000)
                text = await resp.text()
                if resp.status != 200:
                    logger.warning(f"{uri} HTTP {resp.status}: {text[:200]}")
                    return None
                return json.loads(text)
            except json.JSONDecodeError as e:
                logger.error(f"{uri} JSON 解析失败: {e}")
                return None
            except Exception as e:
                logger.warning(f"{uri} 请求异常: {e}")
                return None

        js = """
        async ({url, body, headers}) => {
            const r = await fetch(url, {
                method: 'POST',
                body: body,
                headers: headers,
                credentials: 'include',
                referrer: document.location.href,
                referrerPolicy: 'strict-origin-when-cross-origin',
            });
            return {status: r.status, text: await r.text()};
        }
        """
        result = await self._page.evaluate(js, {
            "url": url,
            "body": body,
            "headers": {
                "Content-Type": "application/json;charset=UTF-8",
                "Accept": "application/json, text/plain, */*",
                **sign_headers,
            },
        })
        return self._parse_fetch_result(uri, result)

    async def get(
        self,
        uri: str,
        params: Dict,
        *,
        referer: Optional[str] = None,
        via: str = "page",
    ) -> Optional[Dict]:
        """带签名的 GET；via 含义同 post。"""
        if self._ctx is None or self._page is None:
            raise RuntimeError("XhsBrowser 未启动")

        sign_headers = await self.sign(uri, params, "GET")
        qs = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
        url = "https://edith.xiaohongshu.com" + uri + ("?" + qs if qs else "")

        if via == "context":
            ref = referer if referer is not None else f"{XHS_HOME}/"
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://www.xiaohongshu.com",
                "Referer": ref,
                **sign_headers,
            }
            try:
                resp = await self._ctx.request.get(url, headers=headers, timeout=30000)
                text = await resp.text()
                if resp.status != 200:
                    logger.warning(f"{uri} HTTP {resp.status}: {text[:200]}")
                    return None
                return json.loads(text)
            except json.JSONDecodeError as e:
                logger.error(f"{uri} JSON 解析失败: {e}")
                return None
            except Exception as e:
                logger.warning(f"{uri} 请求异常: {e}")
                return None

        js = """
        async ({url, headers}) => {
            const r = await fetch(url, {
                method: 'GET',
                headers: headers,
                credentials: 'include',
                referrer: document.location.href,
                referrerPolicy: 'strict-origin-when-cross-origin',
            });
            return {status: r.status, text: await r.text()};
        }
        """
        result = await self._page.evaluate(js, {
            "url": url,
            "headers": {"Accept": "application/json, text/plain, */*", **sign_headers},
        })
        return self._parse_fetch_result(uri, result)

    @staticmethod
    def _parse_fetch_result(uri: str, result: Dict) -> Optional[Dict]:
        status = result["status"]
        text = result["text"]
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            if status != 200:
                logger.warning(f"{uri} HTTP {status}: {text[:200]}")
            return None
        if status != 200:
            # 小红书常用 HTTP 461/406 配 JSON body；code=0 或 1000 均视为成功
            if isinstance(obj, dict) and obj.get("code") in (0, 1000):
                return obj
            logger.warning(f"{uri} HTTP {status}: {text[:200]}")
            return None
        return obj

    async def fetch_homefeed_notes(
        self,
        max_count: int = 20,
        category: str = "homefeed_recommend",
    ) -> List[Dict]:
        """
        通过打开 XHS 首页，拦截首屏 homefeed 响应，拿到推荐流帖子列表。

        优点：完全模拟真人浏览，匿名也可工作，不需要 web_session 登录态。
        劣势：每条 item 是搜索卡形态（标题/作者/互动数），无完整 desc/tag_list；
              评论拉取需要 xsec_token，已在 item 里返回。

        Args:
            max_count: 最多返回多少条
            category: 分类 channel，默认 homefeed_recommend（推荐）；
                      其它如 homefeed.fashion_v3 / homefeed.cosmetics_v3

        Returns:
            note item 列表，每项包含 id/xsec_token/note_card/...
        """
        if self._ctx is None or self._page is None:
            return []

        _HOMEFEED_PATH = "/api/sns/web/v1/homefeed"
        captured: List[Optional[Dict]] = [None]
        done = asyncio.Event()

        async def on_response(response):
            url = response.url
            if _HOMEFEED_PATH in url and not done.is_set():
                try:
                    captured[0] = await response.json()
                    done.set()
                except Exception:
                    pass

        self._page.on("response", on_response)

        try:
            try:
                await self._page.goto(
                    f"{XHS_HOME}/explore",
                    wait_until="domcontentloaded",
                    timeout=20000,
                )
            except Exception:
                pass

            # 等卡片渲染
            try:
                await self._page.wait_for_selector(
                    'a[href*="/explore/"]', timeout=8000
                )
            except Exception:
                pass

            # 滚动几次触发懒加载（可能产生 homefeed XHR）
            for _ in range(2):
                try:
                    await self._page.mouse.wheel(0, 1200)
                    await asyncio.sleep(random.uniform(0.8, 1.5))
                except Exception:
                    break

            try:
                await asyncio.wait_for(done.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                pass
        finally:
            try:
                self._page.remove_listener("response", on_response)
            except Exception:
                pass

        # ── 优先用 API 响应（数据最完整）─────────────────────
        data = captured[0]
        if data is not None and data.get("code", -1) in (0, 1000):
            items = (data.get("data") or {}).get("items") or []
            notes = [
                it for it in items
                if it.get("model_type") in (None, "note", "video", "normal")
                and (it.get("id") or (it.get("note_card") or {}).get("note_id"))
            ]
            notes = notes[:max_count]
            if notes:
                logger.info(
                    f"fetch_homefeed_notes: 通过 homefeed API 拿到 {len(notes)} 条"
                )
                return notes

        # ── 兜底 1：从 window.__INITIAL_STATE__ 提取首屏数据 ──
        dom_items: List[Dict] = []
        try:
            initial = await self._page.evaluate(
                "() => window.__INITIAL_STATE__ ? JSON.parse(JSON.stringify(window.__INITIAL_STATE__)) : null"
            )
        except Exception as e:
            initial = None
            logger.debug(f"读取 __INITIAL_STATE__ 失败 {e}")

        if isinstance(initial, dict):
            # 递归找 "feeds" / "notes" 数组结构
            def _walk(node):
                if isinstance(node, dict):
                    for k, v in node.items():
                        if k in ("feeds", "noteList", "notes", "items") and isinstance(v, list):
                            yield from v
                        else:
                            yield from _walk(v)
                elif isinstance(node, list):
                    for x in node:
                        yield from _walk(x)
            for entry in _walk(initial):
                if not isinstance(entry, dict):
                    continue
                nid = entry.get("id") or entry.get("noteId") or entry.get("note_id")
                xsec = entry.get("xsecToken") or entry.get("xsec_token")
                if not nid or not xsec:
                    nc = entry.get("noteCard") or entry.get("note_card") or {}
                    if not nid:
                        nid = nc.get("noteId") or nc.get("note_id")
                    if not xsec:
                        xsec = nc.get("xsecToken") or nc.get("xsec_token")
                if not nid or not xsec:
                    continue
                nc = entry.get("noteCard") or entry.get("note_card") or {}
                title = (
                    nc.get("displayTitle")
                    or nc.get("display_title")
                    or entry.get("displayTitle")
                    or entry.get("title")
                    or ""
                )
                dom_items.append({"id": nid, "xsec_token": xsec, "display_title": title})

        # ── 兜底 2：扫 DOM 链接 ───────────────────────────────
        # 注意：每张卡片里有两个 a 标签——隐藏的纯导航 a（无 xsec_token）
        # 和真正的封面 a（href 带 xsec_token）。只取后者。
        if not dom_items:
            try:
                links = await self._page.evaluate(
                    r"""() => {
                        const out = [];
                        const seen = new Set();
                        document.querySelectorAll('a[href*="xsec_token="]').forEach((a) => {
                            const m = a.getAttribute('href').match(/\/explore\/([0-9a-z]+)/i);
                            if (!m) return;
                            const noteId = m[1];
                            let xsec = '';
                            try {
                                const u = new URL(a.href, location.origin);
                                xsec = u.searchParams.get('xsec_token') || '';
                            } catch (e) {}
                            if (!xsec) return;
                            if (seen.has(noteId)) return;
                            seen.add(noteId);
                            // 从卡片找标题
                            let title = '';
                            const card = a.closest('section,div');
                            if (card) {
                                const t = card.querySelector(
                                    '[class*="title"],.title,.footer span'
                                );
                                if (t) title = (t.innerText || t.textContent || '').trim();
                            }
                            out.push({id: noteId, xsec_token: xsec, display_title: title});
                        });
                        return out;
                    }"""
                )
            except Exception as e:
                logger.warning(f"fetch_homefeed_notes: DOM 扫描失败 {e}")
                return []
            dom_items = links or []

        if not dom_items:
            logger.warning(
                "fetch_homefeed_notes: 既未拦截到 homefeed API，"
                "__INITIAL_STATE__ 和 DOM 链接也都没拿到带 xsec_token 的笔记"
            )
            return []

        # 去重 + 转成统一 item 结构
        result: List[Dict] = []
        seen: set = set()
        for it in dom_items:
            nid = it.get("id")
            if not nid or nid in seen:
                continue
            seen.add(nid)
            result.append({
                "id": nid,
                "xsec_token": it["xsec_token"],
                "model_type": "note",
                "note_card": {
                    "note_id": nid,
                    "display_title": it.get("display_title", ""),
                },
            })
            if len(result) >= max_count:
                break

        logger.info(
            f"fetch_homefeed_notes: 从 SSR 首屏拿到 {len(result)} 条 "
            f"(候选 {len(dom_items)})"
        )
        return result

    # noauth 模式的 SSR 解析已迁移至 noauth_fetcher.fetch_noauth_explore_feed()，
    # 完全独立于 XhsBrowser，不复用 web_session / mnsv2 签名。

    async def fetch_hot_keywords(self, max_count: int = 20) -> List[Tuple[str, int]]:
        """
        通过模拟"点击首页搜索框"触发 XHS 的 querytrending 接口，拦截真实响应。

        XHS 对 querytrending 接口做了客户端安全校验：必须由真实 DOM 事件触发，
        httpx 直接请求或 page.evaluate fetch 均返回 406 code=-1。
        Playwright 路由拦截可安全捕获真实响应而无需绕过签名。

        Returns:
            [(keyword, heat), ...] 列表；接口失败或返回空时返回 []。
        """
        if self._ctx is None or self._page is None:
            return []

        _TRENDING_PATH = "/api/sns/web/v1/search/querytrending"
        captured: List[Optional[Dict]] = [None]
        done = asyncio.Event()

        async def handle_route(route, request):
            """放行请求，但同时拿到响应体。"""
            resp = await route.fetch()
            try:
                body = await resp.body()
                captured[0] = json.loads(body.decode("utf-8"))
            except Exception:
                pass
            await route.fulfill(response=resp)
            done.set()

        # 只拦截 querytrending 接口
        pattern = f"**{_TRENDING_PATH}**"
        await self._ctx.route(pattern, handle_route)

        try:
            # 导航到首页（已加载时跳过，节省时间）
            current_url = self._page.url or ""
            if "xiaohongshu.com" not in current_url:
                await self._page.goto(XHS_HOME, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(random.uniform(1.0, 2.0))

            # 点击搜索框触发 querytrending
            search_selectors = [
                "#search-input",
                'input.search-input',
                'input[placeholder*="搜索"]',
                'input[placeholder*="探索"]',
                'input[placeholder*="Search"]',
                '.search-input',
                '[class*="search"] input',
            ]
            clicked = False
            for sel in search_selectors:
                try:
                    await self._page.click(sel, timeout=2000)
                    clicked = True
                    break
                except Exception:
                    continue

            if not clicked:
                logger.warning("fetch_hot_keywords: 未找到搜索框，无法触发 querytrending")
                return []

            # 等待拦截完成（最多 8 秒）
            try:
                await asyncio.wait_for(done.wait(), timeout=8.0)
            except asyncio.TimeoutError:
                logger.warning("fetch_hot_keywords: 等待 querytrending 响应超时")

            # 按 Escape 关闭搜索框，恢复正常浏览状态
            try:
                await self._page.keyboard.press("Escape")
            except Exception:
                pass

        finally:
            await self._ctx.unroute(pattern, handle_route)

        data = captured[0]
        if data is None:
            logger.warning("fetch_hot_keywords: 未捕获到 querytrending 响应")
            return []

        code = data.get("code", -1)
        if code not in (0, 1000):
            logger.warning(f"fetch_hot_keywords: API code={code} msg={data.get('msg')}")
            return []

        raw = data.get("data") or {}
        candidates: List[dict] = raw.get("queries") or []
        if not candidates:
            logger.info(f"fetch_hot_keywords: 返回空列表 (data keys={list(raw.keys())})")
            return []

        result: List[Tuple[str, int]] = []
        for idx, item in enumerate(candidates[:max_count]):
            if isinstance(item, str):
                kw = item.strip()
                heat = max_count - idx
            elif isinstance(item, dict):
                kw = (
                    item.get("search_word")
                    or item.get("title")
                    or item.get("keyword")
                    or item.get("word")
                    or ""
                ).strip()
                raw_heat = (
                    item.get("heat_score")
                    or item.get("hot_value")
                    or item.get("score")
                    or item.get("view_num")
                    or 0
                )
                try:
                    heat = int(float(str(raw_heat))) or (max_count - idx)
                except Exception:
                    heat = max_count - idx
            else:
                continue
            if kw:
                result.append((kw, heat))

        logger.info(f"fetch_hot_keywords: 获取 {len(result)} 条热搜关键词（querytrending）")
        return result

    async def close(self) -> None:
        try:
            if self._http is not None:
                await self._http.aclose()
            if self._ctx is not None:
                await self._ctx.close()
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass
        finally:
            self._page = None
            self._ctx = None
            self._pw = None
            self._http = None

    async def __aenter__(self) -> "XhsBrowser":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

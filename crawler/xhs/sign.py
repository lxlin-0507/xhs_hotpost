"""
crawler/xhs/sign.py — 小红书请求签名工具（同步版本）

改编自 MediaCrawler 项目的 xhs_sign.py + playwright_sign.py：
  https://github.com/NanmiCoder/MediaCrawler
仅供学习与研究目的。

核心能力：
  - 生成 X-S / X-T / x-S-Common / X-B3-Traceid 四个签名请求头
  - 使用已打开小红书页面的 Playwright sync page 调用 window.mnsv2() 完成签名
  - 辅助函数：search_id 生成、CRC32、自定义 Base64 等
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import random
import time
from typing import Any, Dict, List, Optional, Union
from urllib.parse import quote

# ── 自定义 Base64 字符表（小红书混淆顺序）────────────────────────────
BASE64_CHARS: List[str] = list(
    "ZmserbBoHQtNP+wOcza/LpngG8yJq42KWYj0DSfdikx3VT16IlUAFM97hECvuRX5"
)

# ── CRC32 查找表────────────────────────────────────────────────────────
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


# ── 纯 Python 工具函数 ─────────────────────────────────────────────────

def _right_shift_unsigned(num: int, bit: int = 0) -> int:
    """JS 无符号右移（>>>）的 Python 实现"""
    val = ctypes.c_uint32(num).value >> bit
    MAX32INT = 4294967295
    return (val + (MAX32INT + 1)) % (2 * (MAX32INT + 1)) - MAX32INT - 1


def mrc(e: str) -> int:
    """CRC32 变种，用于 x-s-common 的 x9 字段"""
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


def _encode_chunk(data: list, start: int, end: int) -> str:
    result = []
    for i in range(start, end, 3):
        c = ((data[i] << 16) & 0xFF0000) + ((data[i + 1] << 8) & 0xFF00) + (data[i + 2] & 0xFF)
        result.append(_triplet_to_base64(c))
    return "".join(result)


def encode_utf8(s: str) -> list:
    """字符串 → UTF-8 字节列表"""
    encoded = quote(s, safe="~()*!.'")
    result = []
    i = 0
    while i < len(encoded):
        if encoded[i] == "%":
            result.append(int(encoded[i + 1: i + 3], 16))
            i += 3
        else:
            result.append(ord(encoded[i]))
            i += 1
    return result


def b64_encode(data: list) -> str:
    """自定义 Base64 编码"""
    length = len(data)
    remainder = length % 3
    chunks = []
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


def get_trace_id() -> str:
    return "".join(random.choice("abcdef0123456789") for _ in range(16))


def _base36encode(number: int, alphabet: str = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ") -> str:
    if not isinstance(number, int):
        raise TypeError("number must be an integer")
    base36 = ""
    sign = ""
    if number < 0:
        sign = "-"
        number = -number
    if 0 <= number < len(alphabet):
        return sign + alphabet[number]
    while number != 0:
        number, i = divmod(number, len(alphabet))
        base36 = alphabet[i] + base36
    return sign + base36


def get_search_id() -> str:
    """生成小红书搜索 ID（与 MediaCrawler help.py 中算法一致）"""
    e = int(time.time() * 1000) << 64
    t = int(random.uniform(0, 2147483646))
    return _base36encode(e + t)


# ── 签名构建 ──────────────────────────────────────────────────────────

def _md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def build_sign_string(
    uri: str,
    data: Optional[Union[Dict, str]] = None,
    method: str = "POST",
) -> str:
    """构建待签名字符串（POST: uri+JSON body；GET: uri+query string）"""
    if method.upper() == "POST":
        if data is None:
            return uri
        if isinstance(data, dict):
            return uri + json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        return uri + data
    else:
        if not data or (isinstance(data, dict) and len(data) == 0):
            return uri
        if isinstance(data, dict):
            params = []
            for key, value in data.items():
                if isinstance(value, list):
                    value_str = ",".join(str(v) for v in value)
                elif value is not None:
                    value_str = str(value)
                else:
                    value_str = ""
                params.append(f"{key}={quote(value_str, safe='')}")
            return f"{uri}?{'&'.join(params)}"
        return f"{uri}?{data}"


def _build_xs_payload(x3_value: str, data_type: str = "object") -> str:
    """构建 x-s 签名值"""
    s = {
        "x0": "4.2.1",
        "x1": "xhs-pc-web",
        "x2": "Mac OS",
        "x3": x3_value,
        "x4": data_type,
    }
    return "XYS_" + b64_encode(encode_utf8(json.dumps(s, separators=(",", ":"))))


def _build_xs_common(a1: str, b1: str, x_s: str, x_t: str) -> str:
    """构建 x-S-Common 请求头"""
    payload = {
        "s0": 3,
        "s1": "",
        "x0": "1",
        "x1": "4.2.2",
        "x2": "Mac OS",
        "x3": "xhs-pc-web",
        "x4": "4.74.0",
        "x5": a1,
        "x6": x_t,
        "x7": x_s,
        "x8": b1,
        "x9": mrc(x_t + x_s + b1),
        "x10": 154,
        "x11": "normal",
    }
    return b64_encode(encode_utf8(json.dumps(payload, separators=(",", ":"))))


def sign_with_page(
    page: Any,
    uri: str,
    data: Optional[Union[Dict, str]] = None,
    a1: str = "",
    method: str = "POST",
) -> Dict[str, str]:
    """
    使用 Playwright 同步 page 对象生成完整签名请求头。

    page 必须已加载 xiaohongshu.com 页面（window.mnsv2 来自页面自身的 JS）。

    返回:
        {"X-S": ..., "X-T": ..., "x-S-Common": ..., "X-B3-Traceid": ...}
    """
    sign_str = build_sign_string(uri, data, method)
    md5_str = _md5_hex(sign_str)

    sign_str_esc = sign_str.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
    md5_str_esc = md5_str.replace("\\", "\\\\").replace("'", "\\'")

    x3_value = ""
    try:
        result = page.evaluate(f"window.mnsv2('{sign_str_esc}', '{md5_str_esc}')")
        x3_value = result if result else ""
    except Exception:
        pass

    b1 = ""
    try:
        ls = page.evaluate("() => Object.assign({}, window.localStorage)")
        if isinstance(ls, dict):
            b1 = ls.get("b1", "")
    except Exception:
        pass

    x_t = str(int(time.time() * 1000))
    data_type = "object" if isinstance(data, (dict, list)) else "string"
    x_s = _build_xs_payload(x3_value, data_type)

    return {
        "X-S": x_s,
        "X-T": x_t,
        "x-S-Common": _build_xs_common(a1, b1, x_s, x_t),
        "X-B3-Traceid": get_trace_id(),
    }

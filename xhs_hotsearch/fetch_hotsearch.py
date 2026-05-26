"""
xhs_hotsearch/fetch_hotsearch.py — 从 rebang.today 抓取小红书官方热搜榜。

数据源:  https://api.rebang.today/v1/items?tab=xiaohongshu&sub_tab=hot-search
返回:    List[dict]，字段: rank / title / heat / icon
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

import requests

logger = logging.getLogger(__name__)

_API_URL = "https://api.rebang.today/v1/items"
_API_PARAMS = {
    "tab": "xiaohongshu",
    "sub_tab": "hot-search",
    "page": 1,
    "version": 1,
}
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://rebang.today/",
    "Accept": "application/json, text/plain, */*",
}


def _parse_heat(raw) -> int:
    """把 '907.8w' / '806w' / '12345' / 数字 统一转 int。"""
    if raw is None:
        return 0
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw).strip().lower()
    m = re.match(r"^([\d.]+)\s*([wW万]?)$", s)
    if not m:
        return 0
    try:
        num = float(m.group(1))
        if m.group(2):
            num *= 10_000
        return int(num)
    except ValueError:
        return 0


def fetch_hotsearch(timeout: int = 15) -> List[Dict]:
    """调用 rebang API，返回热搜榜条目列表（不截断）。"""
    resp = requests.get(_API_URL, params=_API_PARAMS, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()

    data = payload.get("data") or {}
    raw_list = data.get("list")
    # rebang 历史上 list 可能是 JSON 字符串
    if isinstance(raw_list, str):
        try:
            raw_list = json.loads(raw_list)
        except json.JSONDecodeError:
            raw_list = []
    if not isinstance(raw_list, list):
        raw_list = []

    items: List[Dict] = []
    for idx, row in enumerate(raw_list, 1):
        if not isinstance(row, dict):
            continue
        title = (row.get("title") or "").strip()
        if not title:
            continue
        items.append({
            "rank": idx,
            "title": title,
            "heat": _parse_heat(row.get("view_num") or row.get("heat_num")),
            "icon": (row.get("icon_word") or "").strip(),
        })
    return items


def save_hotsearch_txt(items: List[Dict], out_dir: Path, ts: Optional[datetime] = None) -> Path:
    """
    写入 TXT，文件名带时间戳: hotsearch_<YYYYMMDD_HHMM>.txt
    每行: rank<TAB>title<TAB>heat<TAB>icon
    """
    ts = ts or datetime.now()
    day = ts.strftime("%Y%m%d")
    stamp = ts.strftime("%Y%m%d_%H%M")
    day_dir = out_dir / day
    day_dir.mkdir(parents=True, exist_ok=True)
    fp = day_dir / f"hotsearch_{stamp}.txt"

    with fp.open("w", encoding="utf-8") as f:
        for it in items:
            f.write(f"{it['rank']}\t{it['title']}\t{it['heat']}\t{it['icon']}\n")
    return fp


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    items = fetch_hotsearch()
    out_dir = Path(__file__).resolve().parent / "output"
    fp = save_hotsearch_txt(items, out_dir)
    print(f"已写入 {fp}（共 {len(items)} 条）")

"""
crawler/xhs_trend_proxy/anti_block.py — 防封控基础库（Phase 2/4）

提供限速、抖动、退避、Profile 健康度判定等工具函数。
所有函数均为纯函数或同步函数，不依赖 Playwright，可在任意层调用。
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
from typing import List

logger = logging.getLogger(__name__)

# ── 封控码集合 ──────────────────────────────────────────────
_BLOCK_CODES = {461, 406}

# ── 健康度判定窗口（最近 N 次请求）────────────────────────
_HEALTH_WINDOW = 10
_DEGRADED_RATIO = 0.3   # 封控占比 ≥ 30% → degraded
_DEAD_RATIO     = 1.0   # 封控占比 = 100% → dead


# ── 限速工具 ────────────────────────────────────────────────

def profile_sleep(base_min: float = 30.0, base_max: float = 90.0) -> None:
    """profile 切换间隔：随机等待 [base_min, base_max] 秒。"""
    secs = random.uniform(base_min, base_max)
    logger.debug(f"[anti_block] profile 切换等待 {secs:.1f}s")
    time.sleep(secs)


def note_sleep(base_min: float = 3.0, base_max: float = 8.0) -> None:
    """单笔记操作间隔：随机等待 [base_min, base_max] 秒。"""
    secs = random.uniform(base_min, base_max)
    logger.debug(f"[anti_block] 笔记间隔等待 {secs:.1f}s")
    time.sleep(secs)


def on_error(code: int) -> float:
    """
    根据错误码返回建议退避秒数（调用方负责实际 sleep）。
    461 / 406 → 600s（严重风控，等 10 分钟）
    其他非 0   → 30s
    0          → 0（无错误）
    """
    if code in _BLOCK_CODES:
        logger.warning(f"[anti_block] 封控码 {code}，建议退避 600s")
        return 600.0
    if code == 500:
        logger.warning(f"[anti_block] 浏览器/网络异常 {code}，建议退避 15s")
        return 15.0
    if code != 0:
        logger.warning(f"[anti_block] 非 0 错误码 {code}，建议退避 30s")
        return 30.0
    return 0.0


# ── Profile 健康度 ──────────────────────────────────────────

def _stats_path(stats_dir: str, profile_name: str) -> str:
    """健康度统计文件路径。"""
    os.makedirs(stats_dir, exist_ok=True)
    return os.path.join(stats_dir, f"{profile_name}_health.json")


def record_request(stats_dir: str, profile_name: str, code: int) -> None:
    """
    记录一次请求结果到健康度统计文件。
    code=0 表示成功；其他值表示错误/封控。
    保留最近 _HEALTH_WINDOW 条记录（先入先出）。
    """
    path = _stats_path(stats_dir, profile_name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {"codes": []}

    codes: List[int] = data.get("codes", [])
    codes.append(code)
    # 只保留最近窗口条
    if len(codes) > _HEALTH_WINDOW:
        codes = codes[-_HEALTH_WINDOW:]
    data["codes"] = codes

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def profile_health(profile_dir: str, stats_dir: str) -> str:
    """
    读取 profile 对应的健康度统计，返回状态字符串：
      "healthy"  — 封控比例 < 30%（或无历史记录）
      "degraded" — 封控比例 ≥ 30%
      "dead"     — 最近全部封控（比例 = 100%）
    """
    profile_name = os.path.basename(profile_dir.rstrip("/\\"))
    path = _stats_path(stats_dir, profile_name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return "healthy"

    codes: List[int] = data.get("codes", [])
    if not codes:
        return "healthy"

    block_count = sum(1 for c in codes if c in _BLOCK_CODES)
    ratio = block_count / len(codes)

    if ratio >= _DEAD_RATIO:
        return "dead"
    if ratio >= _DEGRADED_RATIO:
        return "degraded"
    return "healthy"


def rotate_profile(profile_root: str, count: int, stats_dir: str) -> List[str]:
    """
    返回按健康度排序的 profile 路径列表（healthy 优先，dead 排最后）。
    profile 目录命名约定：xhs_noauth_0, xhs_noauth_1, ... xhs_noauth_{N-1}
    """
    _ORDER = {"healthy": 0, "degraded": 1, "dead": 2}
    profiles = []
    for i in range(count):
        pdir = os.path.join(profile_root, f"xhs_noauth_{i}")
        health = profile_health(pdir, stats_dir)
        profiles.append((pdir, health))
        logger.debug(f"[anti_block] profile {pdir} → {health}")

    profiles.sort(key=lambda x: _ORDER.get(x[1], 3))
    return [p for p, _ in profiles]

"""
crawler/xhs_trend_proxy/sampler.py — 多 Profile 采样器（Phase 2）

MultiProfileSampler 遍历 profile 池，对每个 profile 调用
noauth_fetcher.fetch_noauth_feed_and_comments()，将结果合并后返回。

每条笔记注入 _profile_id 字段，供下游话题抽取时做"跨独立采样单元"的
证据计数：同一话题在 ≥ MIN_EVIDENCE 个不同 profile 中命中，才可进入候选榜。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Dict, List

from crawler.xhs_trend_proxy.anti_block import (
    on_error,
    profile_sleep,
    record_request,
    rotate_profile,
)
from crawler.xhs_hotpost.noauth_fetcher import fetch_noauth_feed_and_comments

logger = logging.getLogger(__name__)

# 健康度统计文件的默认存放子目录（相对于 save_dir）
_HEALTH_SUBDIR = "health"


class MultiProfileSampler:
    """
    多 Profile 无登录采样器。

    Parameters
    ----------
    profile_root : str
        浏览器 profile 池根目录，例如 browser_profile/。
        内部按 xhs_noauth_0, xhs_noauth_1 … 命名。
    profile_count : int
        使用的 profile 数量（默认 3）。
    notes_per_profile : int
        每个 profile 采样的目标笔记数（默认 25）。
    headless : bool
        是否无头模式（默认 True）。
    comment_timeout : float
        每条笔记等待评论响应的超时秒数。
    stats_dir : str
        健康度统计文件目录。
    """

    def __init__(
        self,
        profile_root: str,
        profile_count: int = 3,
        notes_per_profile: int = 25,
        headless: bool = True,
        comment_timeout: float = 8.0,
        stats_dir: str = "",
    ) -> None:
        self.profile_root = profile_root
        self.profile_count = profile_count
        self.notes_per_profile = notes_per_profile
        self.headless = headless
        self.comment_timeout = comment_timeout
        # stats_dir 默认在 profile_root 同级的 health/ 目录
        self.stats_dir = stats_dir or os.path.join(
            os.path.dirname(profile_root.rstrip("/\\")), "xhs_trend_proxy_health"
        )

    def _profile_dirs(self) -> List[str]:
        """返回按健康度排序的 profile 路径列表。"""
        return rotate_profile(self.profile_root, self.profile_count, self.stats_dir)

    def sample_all(self) -> List[Dict]:
        """
        串行遍历所有 profile，合并采样结果。

        Returns
        -------
        List[Dict]
            每条笔记字典额外携带 ``_profile_id`` 字段（例如 ``"xhs_noauth_0"``）。
            如果某个 profile 全部被风控（返回空），跳过并记录警告，不中断整体任务。
        """
        profiles = self._profile_dirs()
        all_notes: List[Dict] = []

        for idx, pdir in enumerate(profiles):
            profile_name = os.path.basename(pdir.rstrip("/\\"))
            logger.info(
                f"[sampler] 开始 profile {idx + 1}/{len(profiles)}: {profile_name}"
            )

            try:
                notes = asyncio.run(
                    fetch_noauth_feed_and_comments(
                        profile_dir=pdir,
                        max_count=self.notes_per_profile,
                        headless=self.headless,
                        comment_timeout=self.comment_timeout,
                    )
                )
            except Exception as exc:
                exc_msg = str(exc)
                # 区分：HTTP 风控（461/406）vs Playwright/网络类异常
                if "已有会话" in exc_msg or "existing" in exc_msg.lower() or "locked" in exc_msg.lower():
                    logger.warning(
                        f"[sampler] profile {profile_name} Profile 被占用（可能有另一个浏览器实例），跳过: {exc_msg[:120]}"
                    )
                    record_request(self.stats_dir, profile_name, 500)
                    _maybe_sleep_on_error(500)
                elif "461" in exc_msg or "406" in exc_msg:
                    logger.warning(
                        f"[sampler] profile {profile_name} 风控 461/406，跳过: {exc_msg[:120]}"
                    )
                    record_request(self.stats_dir, profile_name, 461)
                    _maybe_sleep_on_error(461)
                else:
                    logger.warning(
                        f"[sampler] profile {profile_name} 采样异常，跳过: {exc_msg[:200]}"
                    )
                    record_request(self.stats_dir, profile_name, 500)
                    _maybe_sleep_on_error(500)
                continue

            if not notes:
                logger.warning(
                    f"[sampler] profile {profile_name} 返回 0 条笔记（可能被风控），跳过"
                )
                record_request(self.stats_dir, profile_name, 461)
                _maybe_sleep_on_error(461)
                continue

            # 注入 profile 标识
            for note in notes:
                note["_profile_id"] = profile_name

            record_request(self.stats_dir, profile_name, 0)
            logger.info(
                f"[sampler] profile {profile_name} 采样成功，获取 {len(notes)} 条笔记"
            )
            all_notes.extend(notes)

            # profile 间等待（最后一个 profile 不等待）
            if idx < len(profiles) - 1:
                logger.debug(f"[sampler] 等待切换至下一个 profile …")
                profile_sleep()

        logger.info(
            f"[sampler] 全部 profile 完成，共 {len(all_notes)} 条笔记，"
            f"来自 {len({n['_profile_id'] for n in all_notes})} 个 profile"
        )
        return all_notes


def _maybe_sleep_on_error(code: int) -> None:
    """在错误码对应的退避秒数内阻塞（仅对生产有意义，测试可 monkeypatch）。"""
    import time
    secs = on_error(code)
    if secs > 0:
        logger.info(f"[sampler] 因错误码 {code} 退避 {secs:.0f}s …")
        time.sleep(secs)

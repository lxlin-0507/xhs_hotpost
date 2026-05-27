"""
No-login XHS sampler.

This module intentionally reuses the project's existing noauth browser flow.
It samples public explore/homefeed notes and comments without relying on a
logged-in account, official hot-search pages, or third-party aggregators.
"""
from __future__ import annotations

import asyncio
from typing import Dict, List

from crawler.xhs_hotpost.noauth_fetcher import fetch_noauth_feed_and_comments


class XhsNoAuthSampler:
    """Sample public XHS notes with a persistent visitor profile."""

    def __init__(
        self,
        profile_dir: str,
        max_notes: int = 80,
        comments_per_note: int = 5,
        headless: bool = True,
        comment_timeout: float = 8.0,
    ) -> None:
        self.profile_dir = profile_dir
        self.max_notes = max_notes
        self.comments_per_note = comments_per_note
        self.headless = headless
        self.comment_timeout = comment_timeout

    async def sample_async(self) -> List[Dict]:
        """Return sampled notes in the same shape as noauth_fetcher outputs."""
        return await fetch_noauth_feed_and_comments(
            profile_dir=self.profile_dir,
            max_count=self.max_notes,
            max_per_note=self.comments_per_note,
            headless=self.headless,
            comment_timeout=self.comment_timeout,
        )

    def sample(self) -> List[Dict]:
        """Synchronous wrapper for CLI use."""
        return asyncio.run(self.sample_async())


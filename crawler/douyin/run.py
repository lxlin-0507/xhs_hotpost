"""
crawler/douyin/run.py — 抖音热词爬取主程序（非交互式，适合定时任务调用）

用法：
  python -m crawler.douyin.run                    # 爬取（默认随机 10-15 页）
  python -m crawler.douyin.run --max-pages 5      # 指定页数
  python -m crawler.douyin.run --login            # 首次扫码登录
  python -m crawler.douyin.run --status           # 查看登录状态
  python -m crawler.douyin.run --skip-hw          # 跳过热词处理
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── 确保项目根目录在 path 中 ──
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from logger import get_logger  # noqa: E402
from crawler.douyin.spider import DouyinAuthenticator, DouyinHotSpider  # noqa: E402

logger = get_logger("crawler.douyin.run")
_AUTH_STATE_PATH = str(_PROJECT_ROOT / "auth_state.json")


# ────────────────────────────────────────────────────────────
# 登录状态
# ────────────────────────────────────────────────────────────

def check_login_status() -> bool:
    if not os.path.exists(_AUTH_STATE_PATH):
        logger.warning(f"auth_state.json 不存在: {_AUTH_STATE_PATH}")
        logger.warning("请运行:  python -m crawler.douyin.run --login")
        return False
    try:
        with open(_AUTH_STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        cookies = state.get("cookies", [])
        if not cookies:
            logger.warning("auth_state.json 存在但 cookies 为空，需要重新登录")
            return False
        logger.info(f"登录状态: ✓ 已登录  |  Cookie 数量: {len(cookies)}")
        for c in cookies:
            if c.get("name") in ("sessionid", "passport_csrf_token"):
                exp = c.get("expires", 0)
                if exp and exp > 0:
                    expire_dt = datetime.fromtimestamp(exp)
                    logger.info(f"  {c['name']} 过期时间: {expire_dt:%Y-%m-%d %H:%M:%S}")
        return True
    except Exception as e:
        logger.error(f"读取 auth_state.json 失败: {e}")
        return False


def do_login() -> bool:
    logger.info("启动浏览器，请在弹出的窗口中完成扫码登录...")
    try:
        auth = DouyinAuthenticator(state_path=_AUTH_STATE_PATH)
        auth.login_and_save_state()
        logger.info(f"登录成功，状态已保存至: {_AUTH_STATE_PATH}")
        return True
    except Exception as e:
        logger.error(f"登录失败: {e}", exc_info=True)
        return False


# ────────────────────────────────────────────────────────────
# 爬取
# ────────────────────────────────────────────────────────────

def run_crawl(max_pages: int, run_day: Optional[str] = None, run_slot: Optional[str] = None) -> Optional[str]:
    if not os.path.exists(_AUTH_STATE_PATH):
        logger.error("auth_state.json 不存在，请先运行 --login")
        # ── 报警：登录态缺失 ─────────────────────────────
        # from pipeline.alerts import AlertManager
        # AlertManager.notify_error("douyin_hotwords", "auth_state.json 不存在")
        return None
    try:
        spider = DouyinHotSpider(auth_state_path=_AUTH_STATE_PATH)
        logger.info(f"── 开始爬取，最多 {max_pages} 页 ──")
        json_file = spider.run(max_pages=max_pages, run_day=run_day, run_slot=run_slot)
        if json_file:
            logger.info(f"爬取完成，原始数据: {json_file}")
        else:
            logger.warning("爬取完成，但未获取到有效数据")
            # ── 报警：无数据 ─────────────────────────────────
            # from pipeline.alerts import AlertManager
            # AlertManager.notify_crawl_empty("douyin_hotwords")
        return json_file
    except Exception as e:
        logger.error(f"爬取失败: {e}", exc_info=True)
        # ── 报警：爬取异常 ───────────────────────────────────
        # from pipeline.alerts import AlertManager
        # AlertManager.notify_error("douyin_hotwords", str(e))
        return None


# ────────────────────────────────────────────────────────────
# 主入口
# ────────────────────────────────────────────────────────────

def main(
    max_pages: Optional[int] = None,
    skip_hot_word: bool = False,
    run_day: Optional[str] = None,
    run_slot: Optional[str] = None,
) -> bool:
    pages = max_pages or 25

    logger.info("=" * 60)
    logger.info(f"抖音热词爬取任务开始  {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info(f"计划爬取页数: {pages}")
    logger.info("=" * 60)

    json_file = run_crawl(max_pages=pages, run_day=run_day, run_slot=run_slot)
    if not json_file:
        logger.error("抖音流程终止：爬取失败或无数据")
        return False

    logger.info("=" * 60)
    logger.info(f"抖音热词爬取任务完成  {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info("=" * 60)
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="抖音热词爬取")
    parser.add_argument("--login", action="store_true", help="弹出浏览器进行扫码登录")
    parser.add_argument("--status", action="store_true", help="查看当前登录状态后退出")
    parser.add_argument("--max-pages", type=int, default=None, help="爬取页数（默认随机 10-15 页）")
    parser.add_argument("--skip-hw", action="store_true", help="跳过热词打分")
    args = parser.parse_args()

    if args.login:
        success = do_login()
        sys.exit(0 if success else 1)

    if args.status:
        check_login_status()
        sys.exit(0)

    success = main(max_pages=args.max_pages, skip_hot_word=args.skip_hw)
    sys.exit(0 if success else 1)

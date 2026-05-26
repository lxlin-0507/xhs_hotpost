"""
crawler/douyin_hotlist/run.py — 抖音热榜爬取主程序（无需登录，适合定时任务调用）

数据源: https://www.douyin.com/hot

用法:
  python -m crawler.douyin_hotlist.run    # 爬取并写入 TXT
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from logger import get_logger  # noqa: E402
from crawler.douyin_hotlist.spider import DouyinHotListSpider  # noqa: E402

logger = get_logger("crawler.douyin_hotlist.run")


def run_crawl(
    run_day: Optional[str] = None,
    run_slot: Optional[str] = None,
) -> Optional[str]:
    spider = DouyinHotListSpider()
    try:
        txt_file = spider.run(run_day=run_day, run_slot=run_slot)
        if txt_file:
            logger.info(f"爬取完成，结果: {txt_file}")
        else:
            logger.warning("爬取完成，但未获取到有效词条")
        return txt_file
    except Exception as e:
        logger.error(f"爬取失败: {e}", exc_info=True)
        return None


def main(
    run_day: Optional[str] = None,
    run_slot: Optional[str] = None,
) -> Optional[str]:
    logger.info("=" * 60)
    logger.info(f"抖音热榜爬取任务开始  {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info("=" * 60)

    txt_file = run_crawl(run_day=run_day, run_slot=run_slot)
    if not txt_file:
        logger.error("抖音热榜流程终止：爬取失败或无数据")
        return None

    try:
        p = Path(txt_file)
        line_count = sum(1 for _ in p.open("r", encoding="utf-8"))
        logger.info(f"校验通过: size={p.stat().st_size} bytes, lines={line_count}")
    except Exception as e:
        logger.error(f"结果校验异常: {e}", exc_info=True)

    logger.info("=" * 60)
    logger.info(f"抖音热榜爬取任务完成  {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info("=" * 60)
    return txt_file


if __name__ == "__main__":
    result = main()
    sys.exit(0 if result else 1)

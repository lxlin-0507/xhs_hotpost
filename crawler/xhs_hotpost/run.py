"""
crawler/xhs_hotpost/run.py — 小红书热帖爬取主程序

用法:
  python -m crawler.xhs_hotpost.run
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
from crawler.xhs_hotpost.spider import XhsHotPostSpider  # noqa: E402

logger = get_logger("crawler.xhs_hotpost.run")


def run_crawl(
    run_day: Optional[str] = None,
    run_slot: Optional[str] = None,
) -> Optional[str]:
    spider = XhsHotPostSpider()
    try:
        json_file = spider.run(run_day=run_day, run_slot=run_slot)
        if json_file:
            logger.info(f"爬取完成，结果: {json_file}")
        else:
            logger.warning("爬取完成，但未获取到有效帖子")
        return json_file
    except Exception as e:
        logger.error(f"爬取失败: {e}", exc_info=True)
        return None


def main(
    run_day: Optional[str] = None,
    run_slot: Optional[str] = None,
) -> Optional[str]:
    logger.info("=" * 60)
    logger.info(f"小红书热帖爬取任务开始  {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info("=" * 60)

    json_file = run_crawl(run_day=run_day, run_slot=run_slot)
    if not json_file:
        logger.error("小红书热帖流程终止：爬取失败或无数据")
        return None

    try:
        p = Path(json_file)
        if not p.exists() or p.stat().st_size == 0:
            logger.error("导出结果文件不存在或为空")
            return None
        import json
        with p.open("r", encoding="utf-8") as f:
            count = len(json.load(f))
        logger.info(f"校验通过: size={p.stat().st_size} bytes, records={count}")
    except Exception as e:
        logger.error(f"结果校验异常: {e}", exc_info=True)

    logger.info("=" * 60)
    logger.info(f"小红书热帖爬取任务完成  {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info("=" * 60)
    return json_file


if __name__ == "__main__":
    result = main()
    sys.exit(0 if result else 1)

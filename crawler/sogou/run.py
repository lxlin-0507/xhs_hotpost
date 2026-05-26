"""
crawler/sogou/run.py — 搜狗词库下载主程序（非交互式，适合定时任务调用）

用法：
  python -m crawler.sogou.run              # 完整流程
  python -m crawler.sogou.run --skip-hw    # 跳过热词处理
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from logger import get_logger  # noqa: E402
from crawler.sogou.dict_manager import SogouDictManager  # noqa: E402

logger = get_logger("crawler.sogou.run")


def run_download_and_convert(run_day: Optional[str] = None) -> Optional[Tuple[Path, Path]]:
    manager = SogouDictManager()

    logger.info("── 步骤 1/3：环境检查 ──")
    manager.check_environment()

    logger.info("── 步骤 2/3：下载词库 ──")
    scel_file = manager.download_dict(run_day=run_day)
    if not scel_file:
        logger.error("词库下载失败。请检查 .env.dev 中的 SOGOU_DICT_URL / SOGOU_DICT_ID")
        return None

    logger.info("── 步骤 3/3：格式转换（scel → txt）──")
    txt_path = manager.convert_scel_to_txt(scel_file.path)
    if not txt_path:
        logger.error("格式转换失败")
        return None
    return scel_file.path, txt_path


def run_export(txt_path: Path, run_day: Optional[str] = None, run_slot: Optional[str] = None) -> Optional[Path]:
    manager = SogouDictManager()
    try:
        out_file = manager.export_words_to_files(txt_path, run_day=run_day, run_slot=run_slot)
        logger.info(f"导出完成: {out_file}")

        return out_file
    except Exception as e:
        logger.error(f"导出失败: {e}", exc_info=True)
        return None


def main(
    skip_hot_word: bool = False,
    run_day: Optional[str] = None,
    run_slot: Optional[str] = None,
) -> bool:
    logger.info("=" * 60)
    logger.info(f"搜狗词库任务开始  {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info("=" * 60)

    result = run_download_and_convert(run_day=run_day)
    if not result:
        logger.error("搜狗流程终止：下载或转换失败（没有下载成功）")
        # ── 报警：搜狗任务失败 ───────────────────────────────
        # from pipeline.alerts import AlertManager
        # AlertManager.notify_error("sogou_newwords", "下载或转换失败")
        return False
    scel_path, txt_path = result

    out_file = run_export(txt_path, run_day=run_day, run_slot=run_slot)
    if not out_file:
        logger.error("搜狗流程终止：导出失败")
        return False

    # 清理中间产物：只删除本次下载/转换产生的 scel 与中间 txt，
    # 不影响同一天其它 slot 的最终 sogou 文件。
    try:
        if scel_path and scel_path.exists():
            scel_path.unlink(missing_ok=True)  # type: ignore[attr-defined]
            logger.info(f"清理中间文件：已删除 scel -> {scel_path}")
    except Exception as e:
        logger.warning(f"清理 scel 失败（可忽略）：{e}")

    try:
        # convert_scel_to_txt 的中间结果：一般是 scel_path.with_suffix(".txt")
        generated_txt = scel_path.with_suffix(".txt") if scel_path else None
        if generated_txt and generated_txt.exists() and Path(txt_path) == generated_txt:
            generated_txt.unlink(missing_ok=True)  # type: ignore[attr-defined]
            logger.info(f"清理中间文件：已删除中间 txt -> {generated_txt}")
    except Exception as e:
        logger.warning(f"清理中间 txt 失败（可忽略）：{e}")

    #（不做 out_dir 级别的清理，避免误删其它 slot 的最终输出）

    # 结果健壮性检查：文件存在、非空、行数>0
    try:
        p = Path(out_file)
        if not p.exists():
            logger.error("导出结果缺失：目标文件不存在（没有下载成功）")
            return False
        size = p.stat().st_size
        if size == 0:
            logger.error("导出结果为空文件（没有下载成功）")
            return False
        # 快速行数统计（可能较大，但足够快）
        line_count = 0
        with p.open("r", encoding="utf-8", errors="ignore") as fr:
            for _ in fr:
                line_count += 1
        if line_count == 0:
            logger.error("导出结果行数为 0（没有下载成功）")
            return False
        logger.info(f"搜狗导出校验通过：size={size} bytes, lines={line_count}")
    except Exception as e:
        logger.error(f"结果校验异常：{e}", exc_info=True)

    logger.info("=" * 60)
    logger.info(f"搜狗词库任务完成  {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info("=" * 60)
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="搜狗词库下载 & 处理")
    parser.add_argument("--skip-hw", action="store_true", help="跳过热词处理")
    args = parser.parse_args()
    success = main(skip_hot_word=args.skip_hw)
    sys.exit(0 if success else 1)

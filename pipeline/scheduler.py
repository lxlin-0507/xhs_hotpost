"""
pipeline/scheduler.py — 定时任务调度器 + Flask API

整合（所有任务时间错开 10 分钟，避免并发抢资源 / 互相干扰）:
  - 抖音热词爬取（每日 3 次: 01:10 / 08:10 / 16:10）
  - 搜狗词库下载（每日 2 次: 05:10 / 19:10）
  - 微博热搜爬取（每日 2 次: 07:10 / 19:20）
  - 抖音热榜爬取（每日 2 次: 09:10 / 21:10）
  - 小红书热搜爬取（每日 2 次: 09:30 / 23:30）   ← rebang.today → xhs_hot 关键词
  - 小红书热帖爬取（每日 2 次: 10:00 / 23:45）   ← querytrending 覆盖，确保 xhs_hot 最新
  - Flask API 提供健康检查 / 手动触发 / 按来源查询

用法:
    python -m pipeline.scheduler                       # 启动调度器 + Flask API
    python -m pipeline.scheduler --run-douyin          # 立即执行一次抖音热词
    python -m pipeline.scheduler --run-douyin-hotlist  # 立即执行一次抖音热榜
    python -m pipeline.scheduler --run-sogou           # 立即执行一次搜狗
    python -m pipeline.scheduler --run-xhs             # 立即执行一次小红书热搜
    python -m pipeline.scheduler --run-xhs-hotpost     # 立即执行一次小红书热帖
    python -m pipeline.scheduler --run-weibo           # 立即执行一次微博
    python -m pipeline.scheduler --no-api              # 只跑调度器，不启动 API
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ── 依赖检查 ──────────────────────────────────────────────
_REQUIRED = {
    "requests": "requests",
    "playwright": "playwright",
    "pandas": "pandas",
    "pypinyin": "pypinyin",
    "dotenv": "python-dotenv",
    "flask": "flask",
    "apscheduler": "apscheduler",
}


def _check_deps() -> None:
    missing = []
    for mod, pkg in _REQUIRED.items():
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"[scheduler] 缺少依赖: {', '.join(missing)}")
        print("请运行: pip install -r requirements.txt")
        sys.exit(1)


_check_deps()

from config import Config  # noqa: E402
from logger import get_logger  # noqa: E402
from crawler.douyin.run import main as _douyin_main  # noqa: E402
from crawler.douyin_hotlist.run import main as _douyin_hotlist_main  # noqa: E402
from crawler.sogou.run import main as _sogou_main  # noqa: E402
from crawler.xhs.run import main as _xhs_main  # noqa: E402
from crawler.xhs_hotpost.run import main as _xhs_hotpost_main  # noqa: E402
from crawler.weibo.run import main as _weibo_main  # noqa: E402
from pipeline.store import StoreManager  # noqa: E402
from pipeline.normalize import generate_unified_leaderboard  # noqa: E402
from pipeline.hot_words import build_hot_words               # noqa: E402

from apscheduler.schedulers.background import BackgroundScheduler  # noqa: E402
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR, EVENT_JOB_MISSED  # noqa: E402
from flask import Flask, jsonify, request as flask_request  # noqa: E402

logger = get_logger("pipeline.scheduler")

# ────────────────────────────────────────────────────────────
# 归一化总榜重建（各热榜任务完成后调用）
# ────────────────────────────────────────────────────────────

def _rebuild_unified_leaderboard(slot: str) -> None:
    """
    读取所有热榜来源的最新原始文件，计算归一化总榜，写到
    output/unified_leaderboard/{date}/unified_hot_{date}_{slot}.txt。
    原始文件不被修改。
    """
    try:
        out_path = generate_unified_leaderboard(
            base_dir=str(Config.OUTPUT_DIR),
            slot=slot,
        )
        if out_path:
            logger.info(f"归一化总榜已生成: {out_path}")
        else:
            logger.warning("归一化总榜：所有来源均无数据，跳过生成")
    except Exception as e:
        logger.error(f"归一化总榜生成失败: {e}", exc_info=True)


# ────────────────────────────────────────────────────────────
# 热词归一化任务
# ────────────────────────────────────────────────────────────

def run_hot_words_task() -> None:
    """
    各来源取最新文件 → 分词 → 归一化 → 写出热词总榜。
    输出: output/hot_words/{YYYYMMDD}/hot_words_{YYYYMMDD}.txt
    可单独触发，也可在爬取任务后串联调用。
    """
    start_dt = datetime.now()
    logger.info(f"[{start_dt:%Y-%m-%d %H:%M:%S}] ── 热词归一化任务开始 ──")
    try:
        out_path, _, _ = build_hot_words(base_dir=str(Config.OUTPUT_DIR))
        logger.info(f"热词归一化完成: {out_path}")
    except Exception as e:
        logger.error(f"热词归一化任务失败: {e}", exc_info=True)
    finally:
        logger.info(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ── 热词归一化任务结束 ──")


# ────────────────────────────────────────────────────────────
# 任务函数
# ────────────────────────────────────────────────────────────

def run_douyin_task(max_pages: Optional[int] = None) -> None:
    start_dt = datetime.now()
    slot = start_dt.strftime("%H%M")
    run_day = start_dt.strftime("%Y%m%d")
    logger.info(f"[{start_dt:%Y-%m-%d %H:%M:%S}] ── 抖音任务开始 ── slot={slot}")
    try:
        _douyin_main(max_pages=max_pages, skip_hot_word=False, run_slot=slot, run_day=run_day)
    except Exception as e:
        logger.error(f"抖音任务失败: {e}", exc_info=True)
        # ── 报警：抖音任务异常 ───────────────────────────────
        # from pipeline.alerts import AlertManager
        # AlertManager.notify_error("douyin_hotwords", str(e))
    finally:
        logger.info(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ── 抖音任务结束 ──")


def run_sogou_task() -> None:
    start_dt = datetime.now()
    slot = start_dt.strftime("%H%M")
    run_day = start_dt.strftime("%Y%m%d")
    logger.info(f"[{start_dt:%Y-%m-%d %H:%M:%S}] ── 搜狗任务开始 ── slot={slot}")
    try:
        _sogou_main(skip_hot_word=False, run_slot=slot, run_day=run_day)
    except Exception as e:
        logger.error(f"搜狗任务失败: {e}", exc_info=True)
        # ── 报警：搜狗任务异常 ───────────────────────────────
        # from pipeline.alerts import AlertManager
        # AlertManager.notify_error("sogou_newwords", str(e))
    finally:
        logger.info(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ── 搜狗任务结束 ──")


def run_weibo_task() -> None:
    start_dt = datetime.now()
    slot = start_dt.strftime("%H%M")
    run_day = start_dt.strftime("%Y%m%d")
    logger.info(f"[{start_dt:%Y-%m-%d %H:%M:%S}] ── 微博任务开始 ── slot={slot}")
    try:
        _weibo_main(run_slot=slot, run_day=run_day)
        _rebuild_unified_leaderboard(slot)
    except Exception as e:
        logger.error(f"微博任务失败: {e}", exc_info=True)
        # ── 报警：微博任务异常 ───────────────────────────────
        # from pipeline.alerts import AlertManager
        # AlertManager.notify_error("weibo_hotsearch", str(e))
    finally:
        logger.info(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ── 微博任务结束 ──")


def run_xhs_task() -> None:
    start_dt = datetime.now()
    slot = start_dt.strftime("%H%M")
    run_day = start_dt.strftime("%Y%m%d")
    logger.info(f"[{start_dt:%Y-%m-%d %H:%M:%S}] ── 小红书任务开始 ── slot={slot}")
    try:
        _xhs_main(run_slot=slot, run_day=run_day)
        _rebuild_unified_leaderboard(slot)
    except Exception as e:
        logger.error(f"小红书任务失败: {e}", exc_info=True)
        # ── 报警：小红书任务异常 ──────────────────────────────
        # from pipeline.alerts import AlertManager
        # AlertManager.notify_error("xhs_hot", str(e))
    finally:
        logger.info(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ── 小红书任务结束 ──")


def run_douyin_hotlist_task() -> None:
    start_dt = datetime.now()
    slot = start_dt.strftime("%H%M")
    run_day = start_dt.strftime("%Y%m%d")
    logger.info(f"[{start_dt:%Y-%m-%d %H:%M:%S}] ── 抖音热榜任务开始 ── slot={slot}")
    try:
        _douyin_hotlist_main(run_slot=slot, run_day=run_day)
        _rebuild_unified_leaderboard(slot)
    except Exception as e:
        logger.error(f"抖音热榜任务失败: {e}", exc_info=True)
    finally:
        logger.info(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ── 抖音热榜任务结束 ──")


def run_xhs_hotpost_task() -> None:
    """
    小红书热帖任务：依赖 xhs_hot 关键词，按关键词搜帖子+评论。
    建议在 run_xhs_task 完成后再触发（调度时间已错开 10 分钟）。
    输出: output/xhs_hotpost/{YYYYMMDD}/xhs_hotpost_{date}_{slot}.json
    """
    start_dt = datetime.now()
    slot = start_dt.strftime("%H%M")
    run_day = start_dt.strftime("%Y%m%d")
    logger.info(f"[{start_dt:%Y-%m-%d %H:%M:%S}] ── 小红书热帖任务开始 ── slot={slot}")
    try:
        _xhs_hotpost_main(run_slot=slot, run_day=run_day)
        _rebuild_unified_leaderboard(slot)
    except Exception as e:
        logger.error(f"小红书热帖任务失败: {e}", exc_info=True)
    finally:
        logger.info(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ── 小红书热帖任务结束 ──")


# ────────────────────────────────────────────────────────────
# Flask API
# ────────────────────────────────────────────────────────────
app = Flask(__name__)


def _check_token() -> bool:
    return flask_request.headers.get("Authorization") == f"Bearer {Config.API_AUTH_TOKEN}"


@app.route("/api/status", methods=["GET"])
def api_status():
    store = StoreManager()
    return jsonify({
        "status": "running",
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sources": store.get_source_summary(),
    })


@app.route("/api/sources", methods=["GET"])
def api_list_sources():
    """列出所有来源及其数据概要"""
    store = StoreManager()
    return jsonify({"sources": store.get_source_summary()})


@app.route("/api/sources/<source>/dates", methods=["GET"])
def api_list_dates(source: str):
    """列出指定来源的所有日期"""
    store = StoreManager()
    return jsonify({"source": source, "dates": store.list_dates(source)})


@app.route("/api/sources/<source>/files", methods=["GET"])
def api_list_files(source: str):
    """列出指定来源的文件（可选 ?date=YYYYMMDD 过滤）"""
    date = flask_request.args.get("date")
    store = StoreManager()
    files = store.list_files(source, date)
    return jsonify({
        "source": source,
        "date": date,
        "count": len(files),
        "files": [
            {"filename": f.filename, "date": f.date, "size": f.size, "created": f.created}
            for f in files
        ],
    })


@app.route("/api/sources/<source>/latest", methods=["GET"])
def api_latest_files(source: str):
    """获取指定来源最新一天的文件"""
    store = StoreManager()
    files = store.get_latest_files(source)
    return jsonify({
        "source": source,
        "count": len(files),
        "files": [
            {"filename": f.filename, "date": f.date, "size": f.size, "created": f.created}
            for f in files
        ],
    })


@app.route("/api/trigger/douyin", methods=["POST"])
def api_trigger_douyin():
    if not _check_token():
        return jsonify({"error": "Unauthorized"}), 401
    import threading
    threading.Thread(target=run_douyin_task, daemon=True).start()
    return jsonify({"message": "抖音任务已触发", "time": datetime.now().isoformat()})


@app.route("/api/trigger/sogou", methods=["POST"])
def api_trigger_sogou():
    if not _check_token():
        return jsonify({"error": "Unauthorized"}), 401
    import threading
    threading.Thread(target=run_sogou_task, daemon=True).start()
    return jsonify({"message": "搜狗任务已触发", "time": datetime.now().isoformat()})


@app.route("/api/trigger/weibo", methods=["POST"])
def api_trigger_weibo():
    if not _check_token():
        return jsonify({"error": "Unauthorized"}), 401
    import threading
    threading.Thread(target=run_weibo_task, daemon=True).start()
    return jsonify({"message": "微博任务已触发", "time": datetime.now().isoformat()})


@app.route("/api/trigger/xhs", methods=["POST"])
def api_trigger_xhs():
    if not _check_token():
        return jsonify({"error": "Unauthorized"}), 401
    import threading
    threading.Thread(target=run_xhs_task, daemon=True).start()
    return jsonify({"message": "小红书任务已触发", "time": datetime.now().isoformat()})


@app.route("/api/trigger/douyin_hotlist", methods=["POST"])
def api_trigger_douyin_hotlist():
    if not _check_token():
        return jsonify({"error": "Unauthorized"}), 401
    import threading
    threading.Thread(target=run_douyin_hotlist_task, daemon=True).start()
    return jsonify({"message": "抖音热榜任务已触发", "time": datetime.now().isoformat()})


@app.route("/api/trigger/xhs_hotpost", methods=["POST"])
def api_trigger_xhs_hotpost():
    if not _check_token():
        return jsonify({"error": "Unauthorized"}), 401
    import threading
    threading.Thread(target=run_xhs_hotpost_task, daemon=True).start()
    return jsonify({"message": "小红书热帖任务已触发", "time": datetime.now().isoformat()})


@app.route("/api/manifest", methods=["POST"])
def api_build_manifest():
    """重新生成 manifest.json 索引"""
    if not _check_token():
        return jsonify({"error": "Unauthorized"}), 401
    store = StoreManager()
    manifest = store.build_manifest()
    return jsonify({"success": True, "sources": list(manifest.keys())})


# ────────────────────────────────────────────────────────────
# 调度器
# ────────────────────────────────────────────────────────────

def _start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")

    # 监听器：记录每次触发/错误/错过
    def _job_listener(event):
        try:
            job = scheduler.get_job(event.job_id)
            job_name = job.name if job else event.job_id
            if event.code == EVENT_JOB_EXECUTED:
                logger.info(f"[调度] 任务完成: {job_name} run_time={getattr(event, 'scheduled_run_time', None)}")
            elif event.code == EVENT_JOB_ERROR:
                logger.error(f"[调度] 任务出错: {job_name} run_time={getattr(event, 'scheduled_run_time', None)}", exc_info=True)
            elif event.code == EVENT_JOB_MISSED:
                logger.warning(f"[调度] 任务错过触发: {job_name} run_time={getattr(event, 'scheduled_run_time', None)}")
        except Exception:
            logger.error("[调度] 监听器异常", exc_info=True)

    scheduler.add_listener(_job_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED)

    # ── 抖音热词爬取（每日 3 次: 01:10 / 08:10 / 16:10）──
    scheduler.add_job(
        run_douyin_task,
        trigger="cron",
        hour="1,8,16",
        minute=10,
        id="douyin_cron",
        name="抖音热词爬取",
        max_instances=1,
        coalesce=True,
    )

    # ── 搜狗词库下载（每日 2 次: 05:10 / 19:10）───────────────
    scheduler.add_job(
        run_sogou_task,
        trigger="cron",
        hour="5,19",
        minute=10,
        id="sogou_cron",
        name="搜狗词库下载",
        max_instances=1,
        coalesce=True,
    )

    # ── 微博热搜爬取（每日 2 次: 07:10 / 19:20）──────────────
    # 19:20 而非 19:10，避开搜狗 19:10 的并发
    scheduler.add_job(
        run_weibo_task,
        trigger="cron",
        hour="7",
        minute=10,
        id="weibo_cron_morning",
        name="微博热搜爬取(早)",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_weibo_task,
        trigger="cron",
        hour="19",
        minute=20,
        id="weibo_cron_evening",
        name="微博热搜爬取(晚)",
        max_instances=1,
        coalesce=True,
    )

    # ── 抖音热榜爬取（每日 2 次: 09:10 / 21:10）──────────────
    scheduler.add_job(
        run_douyin_hotlist_task,
        trigger="cron",
        hour="9,21",
        minute=10,
        id="douyin_hotlist_cron",
        name="抖音热榜爬取",
        max_instances=1,
        coalesce=True,
    )

    # ── 小红书热搜爬取（每日 2 次: 09:30 / 23:30）────────────
    scheduler.add_job(
        run_xhs_task,
        trigger="cron",
        hour="9,23",
        minute=30,
        id="xhs_cron",
        name="小红书热搜爬取",
        max_instances=1,
        coalesce=True,
    )

    # ── 小红书热帖爬取（noauth 模式，服务器本地跑）───────────────
    # 使用持久化 profile (browser_profile/xhs_noauth/)，无需登录
    # 每天两次，错开热搜任务 40 分钟（等热搜关键词先就绪）
    scheduler.add_job(
        run_xhs_hotpost_task,
        trigger="cron",
        hour="10,0",
        minute=10,
        id="xhs_hotpost_cron",
        name="小红书热帖爬取",
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    logger.info("=" * 60)
    logger.info("定时调度器已启动")
    logger.info("  抖音热词:   每日 01:10 / 08:10 / 16:10")
    logger.info("  搜狗爬取:   每日 05:10 / 19:10")
    logger.info("  微博热搜:   每日 07:10 / 19:20")
    logger.info("  抖音热榜:   每日 09:10 / 21:10")
    logger.info("  小红书热搜: 每日 09:30 / 23:30")
    logger.info("  小红书热帖: 每日 10:10 / 00:10（noauth 持久化 profile）")
    logger.info("=" * 60)
    return scheduler


# ────────────────────────────────────────────────────────────
# 主入口
# ────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="oppo-webserver 定时任务调度器")
    parser.add_argument("--run-douyin", action="store_true", help="立即执行一次抖音热词任务后退出")
    parser.add_argument("--run-douyin-hotlist", action="store_true", help="立即执行一次抖音热榜任务后退出")
    parser.add_argument("--run-sogou", action="store_true", help="立即执行一次搜狗任务后退出")
    parser.add_argument("--run-xhs", action="store_true", help="立即执行一次小红书热搜任务后退出")
    parser.add_argument("--run-xhs-hotpost", action="store_true", help="立即执行一次小红书热帖任务后退出")
    parser.add_argument("--run-weibo", action="store_true", help="立即执行一次微博任务后退出")
    parser.add_argument("--run-hot-words", action="store_true",
                        help="立即执行一次热词归一化后退出")
    parser.add_argument("--run-all-hot", action="store_true",
                        help="一次性拉取微博热搜 + 抖音热榜 + 小红书热点（含归一化）后退出")
    parser.add_argument("--no-api", action="store_true", help="只启动调度器，不启动 Flask API")
    parser.add_argument("--max-pages", type=int, default=None, help="抖音爬取页数（默认随机 10-15）")
    args = parser.parse_args()

    # ── 一次性执行模式 ────────────────────────────────────────
    if args.run_douyin:
        run_douyin_task(max_pages=args.max_pages)
        return
    if args.run_douyin_hotlist:
        run_douyin_hotlist_task()
        return
    if args.run_sogou:
        run_sogou_task()
        return
    if args.run_xhs:
        run_xhs_task()
        return
    if args.run_xhs_hotpost:
        run_xhs_hotpost_task()
        return
    if args.run_weibo:
        run_weibo_task()
        return
    if args.run_hot_words:
        run_hot_words_task()
        return
    if args.run_all_hot:
        logger.info("══ 批量拉取：微博热搜 + 抖音热榜 + 小红书热搜 + 小红书热帖 ══")
        run_weibo_task()
        run_douyin_hotlist_task()
        run_xhs_task()
        run_xhs_hotpost_task()
        logger.info("══ 批量拉取完成 ══")
        return

    # ── 常驻服务模式 ─────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("oppo-webserver 定时任务调度器启动")
    logger.info(f"项目根目录: {Config.PROJECT_ROOT}")
    logger.info(f"输出目录:   {Config.OUTPUT_DIR}")
    logger.info(f"环境: {Config.ENV}")
    logger.info("=" * 60)

    # ── 报警：调度器启动通知 ─────────────────────────────────
    # from pipeline.alerts import AlertManager
    # AlertManager.notify_scheduler_start()

    scheduler = _start_scheduler()

    if args.no_api:
        try:
            while True:
                time.sleep(60)
        except (KeyboardInterrupt, SystemExit):
            logger.info("收到退出信号，停止调度器...")
            scheduler.shutdown()
    else:
        logger.info(f"Flask API: http://{Config.API_HOST}:{Config.API_PORT}")
        try:
            app.run(host=Config.API_HOST, port=Config.API_PORT, use_reloader=False)
        except (KeyboardInterrupt, SystemExit):
            logger.info("收到退出信号，停止调度器...")
            scheduler.shutdown()


if __name__ == "__main__":
    main()

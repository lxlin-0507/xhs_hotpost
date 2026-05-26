"""
config.py — 统一配置
优先读 .env.dev > .env
"""
import os
from dotenv import load_dotenv

_here = os.path.dirname(os.path.abspath(__file__))
for name in (".env.dev", ".env"):
    p = os.path.join(_here, name)
    if os.path.exists(p):
        load_dotenv(p, override=True)
        break


def _abs(path: str) -> str:
    """相对路径拼项目根，绝对路径原样返回"""
    return path if os.path.isabs(path) else os.path.join(_here, path)


class Config:
    PROJECT_ROOT = _here
    ENV = os.getenv("ENV", "development")
    DEBUG = os.getenv("DEBUG", "False").lower() == "true"
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    # ── 暂存输出根目录（crawler 先写这里）──────────────────────
    # 本地/服务器都建议保持相对路径（如 output/）以避免对 / 的写入限制
    OUTPUT_DIR = _abs(os.getenv("OUTPUT_DIR", "output"))

    # ── 最终对外目录（定时 move 后写这里）─────────────────────
    # 该目录会被 sync_to_sftp / API 检索
    # 本地默认使用项目根目录下的 hotwords/，服务器由 .env.dev 显式设置为 /crawler/hotwords
    HOTWORDS_DIR = _abs(os.getenv("HOTWORDS_DIR", "hotwords"))

    # ── 抖音热词（douhot.douyin.com，需登录）──────────────────
    DOUYIN_SAVE_DIR = _abs(
        os.getenv("DOUYIN_SAVE_DIR", os.path.join("output", "douyin_hotwords"))
    )
    DOUYIN_AUTH_STATE = _abs(os.getenv("DOUYIN_AUTH_STATE", "auth_state.json"))

    # ── 抖音热榜（www.douyin.com/hot，无需登录）──────────────
    DOUYIN_HOTLIST_SAVE_DIR = _abs(
        os.getenv("DOUYIN_HOTLIST_SAVE_DIR", os.path.join("output", "douyin_hotlist"))
    )

    # ── 搜狗网络流行新词 ─────────────────────────────────────
    SOGOU_SAVE_DIR = _abs(
        os.getenv("SOGOU_SAVE_DIR", os.path.join("output", "sogou_newwords"))
    )
    SOGOU_DICT_URL = os.getenv("SOGOU_DICT_URL", "")
    SOGOU_DICT_ID = os.getenv("SOGOU_DICT_ID", "")

    # ── 微博热搜（无需登录，直接抓公开页）───────────────────────
    WEIBO_SAVE_DIR = _abs(
        os.getenv("WEIBO_SAVE_DIR", os.path.join("output", "weibo_hotsearch"))
    )

    # ── 小红书热词（rebang.today，无需登录）──────────────────
    XHS_SAVE_DIR = _abs(
        os.getenv("XHS_SAVE_DIR", os.path.join("output", "xhs_hot"))
    )

    # ── 小红书热帖（需 XHS Cookie 登录）─────────────────────
    XHS_HOTPOST_SAVE_DIR = _abs(
        os.getenv("XHS_HOTPOST_SAVE_DIR", os.path.join("output", "xhs_hotpost"))
    )
    # 持久化浏览器 profile 目录（含 cookies / localStorage / 设备指纹）
    # 一次登录后该目录内所有数据保持一致；后续爬取直接复用，签名校验才会通过
    XHS_HOTPOST_BROWSER_PROFILE = _abs(
        os.getenv("XHS_HOTPOST_BROWSER_PROFILE", "browser_profile/xhs")
    )
    # 关键词来源目录：
    #   优先 XHS_HOTPOST_KEYWORDS_DIR 环境变量；
    #   其次 /crawler/upload/hotword/xhs_hot（线上 cron 把 output/xhs_hot 搬到这里）；
    #   最后兜底用本地 output/xhs_hot（本地调试 / 全新部署）。
    # 本地调试可设 XHS_HOTPOST_KEYWORDS_DIR=_remote_hotword/xhs_hot 覆盖。
    _xhs_hotpost_kw_default = "/crawler/upload/hotword/xhs_hot"
    if not os.path.isdir(_xhs_hotpost_kw_default):
        _xhs_hotpost_kw_default = os.getenv("XHS_SAVE_DIR", os.path.join("output", "xhs_hot"))
    XHS_HOTPOST_KEYWORDS_DIR = _abs(
        os.getenv("XHS_HOTPOST_KEYWORDS_DIR", _xhs_hotpost_kw_default)
    )
    # 每次取前 N 条热搜关键词来搜帖子；0 或负数表示不限制（读取整个热搜文件）
    # 默认 10：减少单次 session 的 API 调用总量，降低风控概率
    XHS_HOTPOST_MAX_KEYWORDS = int(os.getenv("XHS_HOTPOST_MAX_KEYWORDS", "10"))
    # 抓取模式：
    #   "search"   - 走关键词搜索（需要稳定登录态，机房 IP 易触发风控）
    #   "homefeed" - 直接拿首页推荐流（匿名可用，风控压力小，默认）
    XHS_HOTPOST_MODE = os.getenv("XHS_HOTPOST_MODE", "noauth").lower()
    # homefeed/noauth 模式：拿多少条推荐帖子（noauth 单轮约 20 条，更多则多轮累积）
    XHS_HOTPOST_HOMEFEED_COUNT = int(os.getenv("XHS_HOTPOST_HOMEFEED_COUNT", "20"))
    # noauth 模式专用持久化 profile 目录（不登录，但保留 a1/web_session 跨次复用）
    # 第一次跑时自动创建；多次跑后 a1 会被 XHS 当"老设备"，评论接口信任度逐渐提升
    XHS_HOTPOST_NOAUTH_PROFILE = _abs(
        os.getenv("XHS_HOTPOST_NOAUTH_PROFILE", "browser_profile/xhs_noauth")
    )
    # keyword 模式：每个关键词从搜索结果前 N 条里选 liked_count 最高的那条（默认 5）
    XHS_HOTPOST_CANDIDATES_PER_KW = int(os.getenv("XHS_HOTPOST_CANDIDATES_PER_KW", "5"))
    # 是否拉取每个帖子的评论（每条评论 = 额外 1 次 API 请求；默认关闭降低风控）
    XHS_HOTPOST_FETCH_COMMENTS = os.getenv("XHS_HOTPOST_FETCH_COMMENTS", "true").lower() not in ("0", "false", "no")
    # False = 有界面模式（本地调试），True = 无头模式（服务器，默认）
    XHS_HOTPOST_HEADLESS = os.getenv("XHS_HOTPOST_HEADLESS", "true").lower() not in ("0", "false", "no")
    # 每个帖子最多保留多少条评论（FETCH_COMMENTS=true 时生效）
    XHS_HOTPOST_COMMENTS_PER_NOTE = int(os.getenv("XHS_HOTPOST_COMMENTS_PER_NOTE", "5"))
    # 每个关键词处理完后的基准休眠秒数（实际含随机抖动，建议 ≥ 5s）
    XHS_HOTPOST_SLEEP_SEC = float(os.getenv("XHS_HOTPOST_SLEEP_SEC", "5.0"))

    # ── SFTP 同步（推送到 SFTP 服务器供第三方拉取）───────────
    SFTP_HOST = os.getenv("SFTP_HOST", "")
    SFTP_USER = os.getenv("SFTP_USER", "")
    SFTP_KEY_PATH = _abs(os.getenv("SFTP_KEY_PATH", "id_ed25519"))
    SFTP_REMOTE_DIR = os.getenv("SFTP_REMOTE_DIR", "/crawler/upload")

    # ── 报警（钉钉）──────────────────────────────────────────
    ALERT_ENABLED = os.getenv("ALERT_ENABLED", "False").lower() == "true"
    # 钉钉机器人 webhook
    ALERT_DINGTALK_WEBHOOK = os.getenv("ALERT_DINGTALK_WEBHOOK", "")
    # 钉钉加签密钥（可选，安全设置选"加签"时需要）
    ALERT_DINGTALK_SECRET = os.getenv("ALERT_DINGTALK_SECRET", "")

    # ── 热词阈值 ─────────────────────────────────────────────
    HOT_WORD_GROWTH_THRESHOLD = float(os.getenv("HOT_WORD_GROWTH_THRESHOLD", "0.5"))
    JSON_FILE_RETENTION_DAYS = int(os.getenv("JSON_FILE_RETENTION_DAYS", "30"))

    # ── API（可选）────────────────────────────────────────────
    API_HOST = os.getenv("API_HOST", "0.0.0.0")
    API_PORT = int(os.getenv("API_PORT", 5001))
    API_AUTH_TOKEN = os.getenv("API_AUTH_TOKEN", "default_secret")

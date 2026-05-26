"""
pipeline/alerts.py — 报警模块（钉钉 webhook）

当前所有报警调用已在调用方注释掉，暂时不真正告警。
启用报警前请在 .env.dev 中配置:
    ALERT_ENABLED=true
    ALERT_DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=xxx
    ALERT_DINGTALK_SECRET=xxx          # 可选，加签密钥

然后取消 crawler/douyin/run.py、crawler/sogou/run.py、pipeline/scheduler.py
中的 AlertManager 调用注释即可。
"""
from __future__ import annotations

import hashlib
import hmac
import base64
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import Config  # noqa: E402
from logger import get_logger  # noqa: E402

logger = get_logger("pipeline.alerts")

try:
    import requests as _requests
except ImportError:
    _requests = None  # type: ignore


class AlertManager:
    """
    统一报警管理器。
    所有报警方法均为类方法（静态），调用时不需要实例化。

    启用条件: ALERT_ENABLED=true
    通知通道: 钉钉机器人 webhook
    """

    # ── 通用报警 ─────────────────────────────────────────────

    @classmethod
    def notify_error(cls, source: str, message: str) -> None:
        """通用错误报警"""
        if not Config.ALERT_ENABLED:
            return
        title = f"【{source.upper()} 错误】"
        body = f"{title}\n时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n详情: {message}"
        cls._send_dingtalk(body)

    @classmethod
    def notify_crawl_empty(cls, source: str) -> None:
        """爬取结果为空报警"""
        cls.notify_error(source, f"{source} 爬取结果为空，请检查登录态或网络")

    @classmethod
    def notify_crawl_success(cls, source: str, count: int, file_path: str) -> None:
        """爬取成功通知（一般不告警，仅记录日志）"""
        logger.info(f"[{source}] 爬取成功: {count} 条 → {file_path}")

    @classmethod
    def notify_sudden_hotwords(cls, source: str, hotwords: List[Dict[str, Any]]) -> None:
        """突增热点报警"""
        if not Config.ALERT_ENABLED:
            return
        title = f"【{source.upper()} 热点警报】发现 {len(hotwords)} 个爆点热词"
        lines = [title, f"时间: {datetime.now():%Y-%m-%d %H:%M:%S}", ""]
        for item in hotwords[:20]:
            lines.append(f"• {item.get('热点词', '')} — 增长率 {item.get('日增长率', '')}")
        cls._send_dingtalk("\n".join(lines))

    @classmethod
    def notify_scheduler_start(cls) -> None:
        """调度器启动通知"""
        if not Config.ALERT_ENABLED:
            return
        body = f"调度器启动\n时间: {datetime.now():%Y-%m-%d %H:%M:%S}"
        cls._send_dingtalk(body)

    # ── 钉钉 webhook ─────────────────────────────────────────

    @classmethod
    def _build_signed_url(cls, webhook: str, secret: str) -> str:
        """如果配置了加签密钥，生成带签名的 URL"""
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(
            secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        sep = "&" if "?" in webhook else "?"
        return f"{webhook}{sep}timestamp={timestamp}&sign={sign}"

    @classmethod
    def _send_dingtalk(cls, content: str) -> bool:
        """发送钉钉机器人消息"""
        webhook = Config.ALERT_DINGTALK_WEBHOOK
        if not webhook or _requests is None:
            return False
        secret = Config.ALERT_DINGTALK_SECRET
        url = cls._build_signed_url(webhook, secret) if secret else webhook
        try:
            resp = _requests.post(
                url,
                json={"msgtype": "text", "text": {"content": content}},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("errcode") == 0:
                    logger.info("钉钉报警已发送")
                    return True
                logger.error(f"钉钉报警失败: {data}")
                return False
            logger.error(f"钉钉报警失败: {resp.status_code} {resp.text[:200]}")
            return False
        except Exception as e:
            logger.error(f"钉钉报警异常: {e}")
            return False

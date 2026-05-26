"""
crawler/xhs_hotpost/login.py — 小红书扫码登录（持久化 profile）

用法:
    python3 -m crawler.xhs_hotpost.login            # 有显示器：弹出浏览器扫码
    python3 -m crawler.xhs_hotpost.login --headless # 无头服务器：截图到文件扫码

无头模式流程:
  1. headless 启动 Chromium，访问小红书登录页
  2. 截取二维码图片 → browser_profile/xhs/qrcode_login.png
  3. 通过 sftp/rsync 下载该图，用小红书 App 扫码
  4. 检测到 web_session 变化 → 登录成功，保存 storage_state.json

依赖:
    pip install playwright
    python3 -m playwright install chromium
"""
from __future__ import annotations

import asyncio
import os
import sys

from playwright.async_api import async_playwright

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import Config  # noqa: E402
from logger import get_logger  # noqa: E402

logger = get_logger("crawler.xhs_hotpost.login")

XHS_URL = "https://www.xiaohongshu.com"
LOGIN_TIMEOUT_SEC = 180

async def _screenshot_login(page, save_path: str, verbose: bool = False) -> None:
    """截取整个视口（含登录弹窗），方便用户识别正确的登录二维码。"""
    await page.screenshot(path=save_path, full_page=False)
    if verbose:
        logger.info(f"登录页截图已保存: {save_path}")


async def login_and_save(headless: bool = False) -> bool:
    profile_dir = Config.XHS_HOTPOST_BROWSER_PROFILE
    os.makedirs(profile_dir, exist_ok=True)
    qr_path = os.path.join(profile_dir, "qrcode_login.png")

    logger.info(f"持久化 profile: {profile_dir}")
    if headless:
        logger.info("无头模式：将把二维码截图到文件，请下载后扫码")
    else:
        logger.info("启动浏览器，请扫码登录小红书…")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=headless,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(XHS_URL, wait_until="domcontentloaded")

        # 记录初始 web_session（未登录时小红书也会发一个匿名值）
        # 登录成功后 web_session 会被服务端换成新值，据此判断登录
        await asyncio.sleep(2)
        initial_cookies = await context.cookies(XHS_URL)
        initial_web_session = next(
            (c["value"] for c in initial_cookies if c["name"] == "web_session"), ""
        )

        # 真实登录态的 web_session 以 "04" 开头且长度 ≥ 50；
        # 游客/匿名 session 以 "030037" 开头、约 38 字符，不算已登录。
        already_logged_in = (
            bool(initial_web_session)
            and len(initial_web_session) >= 50
            and initial_web_session.startswith("04")
        )
        if already_logged_in:
            logger.info(
                f"检测到已登录态  web_session={initial_web_session[:10]}… "
                f"(len={len(initial_web_session)})，跳过扫码步骤"
            )
            success = True
        else:
            logger.info(f"初始 web_session = {initial_web_session[:20]}…  (未登录态)")
            if headless:
                # 无头模式：触发登录弹窗，截取整个视口
                # 1. 尝试点击"登录"入口打开弹窗
                login_btn_sels = [
                    "text=登录", ".login-btn", "[class*='login-btn']",
                    "[data-v-*] .btn", "text=Sign in",
                ]
                for btn_sel in login_btn_sels:
                    try:
                        await page.click(btn_sel, timeout=2000)
                        await asyncio.sleep(2)
                        break
                    except Exception:
                        continue

                # 2. 若弹窗有多个登录方式，切换到"扫码登录"tab
                qr_tab_sels = ["text=扫码登录", "text=二维码", "[class*='qrcode-tab']"]
                for tab_sel in qr_tab_sels:
                    try:
                        await page.click(tab_sel, timeout=2000)
                        await asyncio.sleep(1)
                        break
                    except Exception:
                        continue

                await asyncio.sleep(2)
                await _screenshot_login(page, qr_path, verbose=True)
                logger.info("=" * 60)
                logger.info("请下载截图，找到登录二维码后用小红书 App 扫码：")
                logger.info(f"  rsync 命令: rsync -avz -e 'ssh -i KEY' REMOTE:{qr_path} ~/Desktop/qrcode.png")
                logger.info(f"截图路径: {qr_path}")
                logger.info("=" * 60)
            else:
                logger.info(f"请在弹出的浏览器中扫码登录（{LOGIN_TIMEOUT_SEC}s 内完成）…")

            success = False
            for i in range(LOGIN_TIMEOUT_SEC):
                await asyncio.sleep(1)

                try:
                    cookies = await context.cookies(XHS_URL)
                except Exception:
                    continue
                current_ws = next(
                    (c["value"] for c in cookies if c["name"] == "web_session"), ""
                )
                if current_ws and current_ws != initial_web_session and len(current_ws) >= 30:
                    logger.info(
                        f"web_session 已变化 ({initial_web_session[:10]}… → {current_ws[:10]}…)，"
                        f"登录成功（用时 {i+1}s）"
                    )
                    success = True
                    break
                if i and i % 15 == 0:
                    logger.info(f"等待扫码中… ({i}s)")
                if headless and i % 2 == 0:
                    # 每 2 秒刷新截图（应对安全验证二维码 1 分钟过期）
                    try:
                        await _screenshot_login(page, qr_path)
                    except Exception:
                        pass

        if not success:
            logger.error("登录超时，未检测到 web_session cookie")
            await context.close()
            return False

        # 多等几秒让 cookie / localStorage 写入磁盘
        await asyncio.sleep(5)
        cookies = await context.cookies(XHS_URL)
        logger.info(f"profile 中共 {len(cookies)} 条 cookie")

        # 同步导出 storage_state.json（跨平台可移植：mac → linux 服务器）
        # 持久化 profile 中 Chromium 的 Cookies 是用 OS 级 keychain 加密的，
        # 不能跨平台搬迁；storage_state JSON 是明文 cookie 列表，可任意复制。
        import json as _json
        state = await context.storage_state()
        state_path = os.path.join(profile_dir, "storage_state.json")
        with open(state_path, "w", encoding="utf-8") as f:
            _json.dump(state, f, ensure_ascii=False, indent=2)
        xhs_cookie_cnt = sum(
            1 for c in state.get("cookies", []) if "xiaohongshu" in c.get("domain", "")
        )
        logger.info(
            f"storage_state.json 已导出: {state_path}  "
            f"(共 {len(state.get('cookies', []))} cookie，xhs 域 {xhs_cookie_cnt} 条)"
        )

        await context.close()

    logger.info(f"浏览器 profile 已保存到 {profile_dir}")
    logger.info("现在可以运行：python3 -m crawler.xhs_hotpost.run")
    return True


def main() -> int:
    headless = "--headless" in sys.argv
    try:
        ok = asyncio.run(login_and_save(headless=headless))
    except KeyboardInterrupt:
        logger.info("已取消登录")
        return 130
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

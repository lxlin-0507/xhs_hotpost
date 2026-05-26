"""
crawler/douyin/spider.py — 抖音热词爬虫核心（DouyinAuthenticator + DouyinHotSpider）
"""
import json
import os
import random
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests
import urllib3
from playwright.sync_api import sync_playwright
from pypinyin import lazy_pinyin

from config import Config
from logger import get_logger

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = get_logger("crawler.douyin")


# ────────────────────────────────────────────────────────────
#  DouyinAuthenticator
# ────────────────────────────────────────────────────────────

class DouyinAuthenticator:
    """抖音登录认证管理类（用于 douhot.douyin.com 热词页）"""

    def __init__(self, state_path: str = "auth_state.json"):
        self.state_path = state_path
        self.browser_context = None
        self.playwright = None
        self.browser = None

    def login_and_save_state(self) -> None:
        """执行扫码登录并保存状态（阻塞等待用户回车确认）"""
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=False)
        self.browser_context = self.browser.new_context()

        page = self.browser_context.new_page()
        page.goto("https://douhot.douyin.com/square/trend?active_tab=hotword_all")

        logger.warning("\n" + "!" * 30)
        logger.warning("请在弹出的浏览器中完成扫码登录")
        logger.warning("登录成功并看到热词数据界面后，请回到这里按【回车键】确认")
        logger.warning("!" * 30 + "\n")

        input("确认已登录成功请按回车...")

        self.browser_context.storage_state(path=self.state_path)
        logger.info(f"登录状态已保存至: {self.state_path}")
        self.close()

    def simulate_human_behavior(self, page) -> None:
        """模拟真实用户浏览行为"""
        time.sleep(random.uniform(2.0, 3.0))

        viewport_size = page.viewport_size
        if viewport_size:
            for _ in range(random.randint(2, 4)):
                x = random.randint(100, max(120, viewport_size["width"] - 100))
                y = random.randint(100, max(120, viewport_size["height"] - 100))
                page.mouse.move(x, y)
                time.sleep(random.uniform(0.3, 0.8))

        scroll_times = random.randint(1, 2)
        for _ in range(scroll_times):
            scroll_distance = random.randint(300, 1200)
            segments = random.randint(2, 4)
            for _ in range(segments):
                page.mouse.wheel(0, scroll_distance // segments)
                time.sleep(random.uniform(0.1, 0.3))

            time.sleep(random.uniform(1.0, 2.0))

            if random.random() < 0.3:
                back_scroll = random.randint(100, 400)
                page.mouse.wheel(0, -back_scroll)
                time.sleep(random.uniform(0.5, 1.5))

    def load_cookies_from_file(self) -> Tuple[str, None, None, None]:
        """从 auth_state.json 直接读取 Cookie（不启动浏览器）"""
        if not os.path.exists(self.state_path):
            raise FileNotFoundError(f"auth_state.json 不存在: {self.state_path}")
        with open(self.state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        cookies = state.get("cookies", [])
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
        logger.info(f"从 auth_state.json 直接加载了 {len(cookies)} 个 Cookie（无签名）")
        return cookie_str, None, None, None

    def get_cookies_and_refresh(self) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
        """加载状态并获取最新的 Cookie、X-Bogus、_signature、csrf_token"""
        if not os.path.exists(self.state_path):
            raise FileNotFoundError(
                f"auth_state.json 不存在: {self.state_path}\n"
                "请先在本地运行: python -m crawler.douyin.run --login"
            )

        self.playwright = sync_playwright().start()
        try:
            self.browser = self.playwright.chromium.launch(headless=True)
            self.browser_context = self.browser.new_context(storage_state=self.state_path)
            page = self.browser_context.new_page()

            captured: Dict[str, Optional[str]] = {"bogus": None, "signature": None, "csrf_token": None}

            def handle_request(request) -> None:
                if "hot_word/query_list" in request.url:
                    parsed_url = urlparse(request.url)
                    params = parse_qs(parsed_url.query)
                    if "X-Bogus" in params and "_signature" in params:
                        captured["bogus"] = params["X-Bogus"][0]
                        captured["signature"] = params["_signature"][0]
                        logger.info("成功捕获签名: X-Bogus / _signature")

                    csrf_token = request.headers.get("x-secsdk-csrf-token")
                    if csrf_token:
                        captured["csrf_token"] = csrf_token
                        logger.info("成功捕获 X-Secsdk-Csrf-Token")

            page.on("request", handle_request)

            try:
                target_url = "https://douhot.douyin.com/square/trend?active_tab=hotword_all"
                page.goto(target_url, wait_until="networkidle", timeout=30_000)
                self.simulate_human_behavior(page)
            except Exception as e:
                logger.error(f"浏览器访问超时或异常: {e}")

            cookies = self.browser_context.cookies()
            cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])

            self.browser_context.storage_state(path=self.state_path)

            return cookie_str, captured["bogus"], captured["signature"], captured["csrf_token"]
        finally:
            self.close()

    def close(self) -> None:
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()


# ────────────────────────────────────────────────────────────
#  DouyinHotSpider
# ────────────────────────────────────────────────────────────

class DouyinHotSpider:
    """抖音热词爬虫：分页爬取 + 去重 + 分析 + 导出"""

    SOURCE_NAME = "douyin_hotwords"

    @staticmethod
    def _get_chromium_ua() -> Tuple[str, str]:
        """获取 Playwright Chromium 真实版本，生成 UA 与 Sec-Ch-Ua"""
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                version = browser.version
                browser.close()
            major = version.split(".")[0]
            ua = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{major}.0.0.0 Safari/537.36"
            )
            sec_ch_ua = f'"Google Chrome";v="{major}", "Chromium";v="{major}", "Not?A_Brand";v="24"'
            logger.info(f"Playwright Chromium 版本: {version}，UA 已动态生成")
            return ua, sec_ch_ua
        except Exception as e:
            logger.warning(f"获取 Chromium 版本失败，使用默认 UA: {e}")
            ua = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/119.0.0.0 Safari/537.36"
            )
            sec_ch_ua = '"Google Chrome";v="119", "Chromium";v="119", "Not?A_Brand";v="24"'
            return ua, sec_ch_ua

    def __init__(self, auth_state_path: str = "auth_state.json", bogus: Optional[str] = None, signature: Optional[str] = None):
        self.base_url = "https://douhot.douyin.com/douhot/v1/dashboard/hot_word/query_list"
        self.bogus = bogus
        self.signature = signature
        self.authenticator = DouyinAuthenticator(auth_state_path)

        # 保存目录：output/douyin_hotwords/{date}/
        self.save_dir = Config.DOUYIN_SAVE_DIR
        os.makedirs(self.save_dir, exist_ok=True)

        ua, sec_ch_ua = self._get_chromium_ua()
        self.headers: Dict[str, str] = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Content-Type": "application/json",
            "Cookie": "",
            "Origin": "https://douhot.douyin.com",
            "Pragma": "no-cache",
            "Referer": "https://douhot.douyin.com/",
            "Sec-Ch-Ua": sec_ch_ua,
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": ua,
            "X-Secsdk-Csrf-Token": os.getenv("DOUYIN_X_SECS_SDK_CSRF_TOKEN", ""),
        }

        self.request_template: Dict[str, Any] = {
            "page_num": 1,
            "page_size": 24,
            "tab_type": 1,
            "keyword": "",
            "date_window": 24,
            "query_day": datetime.now().strftime("%Y%m%d"),
        }

        self.all_data: List[Dict[str, Any]] = []

    def _date_save_dir(self, date_str: Optional[str] = None) -> str:
        """返回按日期分的子目录 output/douyin_hotwords/YYYYMMDD/"""
        date_str = date_str or datetime.now().strftime("%Y%m%d")
        d = os.path.join(self.save_dir, date_str)
        os.makedirs(d, exist_ok=True)
        return d

    def get_url_with_signature(self, page_num: int) -> str:
        _ = page_num
        bogus = self.bogus or ""
        signature = self.signature or ""
        return f"{self.base_url}?msToken=&X-Bogus={bogus}&_signature={signature}"

    def fetch_page(self, page_num: int) -> Tuple[List[Dict[str, Any]], int]:
        url = self.get_url_with_signature(page_num)
        post_data = dict(self.request_template)
        post_data["page_num"] = page_num

        try:
            logger.info(f"正在获取第{page_num}页...")
            resp = requests.post(url=url, headers=self.headers, json=post_data, timeout=15, verify=False)

            if resp.status_code == 200:
                data = resp.json()

                word_list = data.get("data", {}).get("word_list", []) or []
                total_count = int(data.get("data", {}).get("total_count", 0) or 0)
                if word_list:
                    logger.info(f"第{page_num}页：获取到{len(word_list)}条数据")
                else:
                    logger.warning(f"第{page_num}页：无数据")
                return word_list, total_count

            if resp.status_code == 403:
                logger.error(f"第{page_num}页：403 Forbidden - Cookie可能已过期")
                return [], 0

            logger.error(f"第{page_num}页：请求失败，状态码{resp.status_code}")
            return [], 0
        except Exception as e:
            logger.error(f"第{page_num}页：请求异常 - {e}")
            return [], 0

    def fetch_all_pages(self, max_pages: int = 50, start_page: int = 1) -> List[Dict[str, Any]]:
        logger.info(f"开始获取抖音热点数据,最多{max_pages}页...")
        self.all_data = []
        page = start_page
        total_count = 0

        while page <= max_pages:
            word_list, current_total = self.fetch_page(page)
            if page == 1:
                total_count = current_total
            if not word_list:
                logger.warning(f"第{page}页无数据,停止翻页")
                break

            self.all_data.extend(word_list)

            if page > 1 and len(word_list) < 24:
                logger.info(f"第{page}页数据少于24条,可能是最后一页,停止翻页")
                break

            if page == 1:
                sleep_time = random.uniform(2.0, 4.0)
            elif page <= 5:
                sleep_time = random.uniform(2.0, 3.0)
            else:
                sleep_time = random.uniform(1.0, 3.0)

            if random.random() < 0.15:
                time.sleep(random.uniform(2.0, 4.0))

            time.sleep(sleep_time)
            page += 1

        logger.info("数据获取完成！")
        logger.info(f"   共获取 {len(self.all_data)} 条热点数据")
        logger.info(f"   接口显示总数据量：{total_count} 条")
        return self.all_data

    def remove_duplicates(self) -> List[Dict[str, Any]]:
        if not self.all_data:
            return []
        unique: List[Dict[str, Any]] = []
        seen = set()
        for item in self.all_data:
            title = item.get("title", "")
            if title and title not in seen:
                seen.add(title)
                unique.append(item)
        self.all_data = unique
        logger.info(f"去重后：{len(unique)} 条")
        return unique

    def analyze_data(self) -> Optional[pd.DataFrame]:
        if not self.all_data:
            logger.warning("没有数据可分析")
            return None

        df = pd.DataFrame(self.all_data)
        if "score" in df.columns:
            df["热度(万)"] = df["score"] / 10000

        # 突增热点检测
        sudden_hot_list = []
        growth_threshold = Config.HOT_WORD_GROWTH_THRESHOLD
        if "trends" in df.columns:
            for idx, row in df.iterrows():
                trends = row.get("trends")
                if isinstance(trends, list) and len(trends) >= 2:
                    recent_val = (trends[-1] or {}).get("value", 0) or 0
                    previous_val = (trends[-2] or {}).get("value", 0) or 0
                    if previous_val > 0:
                        daily_growth_rate = (recent_val - previous_val) / previous_val
                        if daily_growth_rate > growth_threshold:
                            sudden_hot_list.append(
                                {
                                    "热点词": row.get("title", ""),
                                    "当前热度值": recent_val,
                                    "前期热度值": previous_val,
                                    "日增长率": f"{daily_growth_rate * 100:.1f}%",
                                    "增长率数值": daily_growth_rate,
                                    "排名": idx + 1,
                                }
                            )

        if sudden_hot_list:
            sudden_hot_list.sort(key=lambda x: x["增长率数值"], reverse=True)
            # ── 报警：突增热点通知 ──────────────────────────────
            # from pipeline.alerts import AlertManager
            # AlertManager.notify_sudden_hotwords("douyin_hotwords", sudden_hot_list)

        return df

    def _clean_old_json_files(self) -> None:
        try:
            retention_days = Config.JSON_FILE_RETENTION_DAYS
            cutoff_date = datetime.now() - timedelta(days=retention_days)
            if not os.path.exists(self.save_dir):
                return
            for root, dirs, files in os.walk(self.save_dir):
                for filename in files:
                    if not filename.endswith(".json"):
                        continue
                    file_path = os.path.join(root, filename)
                    mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
                    if mtime < cutoff_date:
                        try:
                            os.remove(file_path)
                        except Exception:
                            pass
        except Exception as e:
            logger.error(f"JSON文件清理失败: {e}")

    def export_to_excel(self, filename: Optional[str] = None) -> Optional[str]:
        if not self.all_data:
            logger.warning("没有数据可导出")
            return None
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = os.path.join(self._date_save_dir(), f"抖音热点数据_{timestamp}.xlsx")
        df = pd.DataFrame(self.all_data)
        if "score" in df.columns:
            df["热度(万)"] = df["score"] / 10000
        try:
            df.to_excel(filename, index=False)
            logger.info(f"数据已成功导出到: {filename}")
            return filename
        except Exception as e:
            logger.error(f"导出Excel失败: {e}")
            return None

    def refresh_session(self) -> None:
        """通过 Playwright 续期并捕获签名；若 Playwright 不可用则降级为从文件加载 Cookie"""
        logger.info("正在通过 Playwright 续期并自动捕获签名...")
        try:
            new_cookie, new_bogus, new_sig, new_csrf = self.authenticator.get_cookies_and_refresh()
        except Exception as e:
            logger.warning(f"Playwright 续期失败: {e}")
            logger.info("降级模式: 从 auth_state.json 直接加载 Cookie（无动态签名）")
            new_cookie, new_bogus, new_sig, new_csrf = self.authenticator.load_cookies_from_file()

        self.headers["Cookie"] = new_cookie
        if new_bogus and new_sig:
            self.bogus = new_bogus
            self.signature = new_sig
            logger.info("Cookie 和签名已同步更新")
        if new_csrf:
            self.headers["X-Secsdk-Csrf-Token"] = new_csrf

    def run(self, max_pages: int = 25, run_day: Optional[str] = None, run_slot: Optional[str] = None) -> Optional[str]:
        """
        完整爬取流程，返回保存的全量 JSON 路径。
        所有文件按来源+日期组织: output/douyin_hotwords/{YYYYMMDD}/
        """
        logger.info("=" * 60)
        logger.info(f"执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("抖音热点数据爬虫正在运行...")
        logger.info("=" * 60)

        self._clean_old_json_files()

        self.refresh_session()

        # 固定本次爬取的 query_day（避免运行时间偏移导致 query_day 与文件命名不一致）
        fixed_daily_date = run_day or datetime.now().strftime("%Y%m%d")
        self.request_template["query_day"] = fixed_daily_date

        self.fetch_all_pages(max_pages=max_pages)
        if not self.all_data:
            logger.warning("没有获取到数据，程序退出")
            # ── 报警：爬取无数据 ──────────────────────────────
            # from pipeline.alerts import AlertManager
            # AlertManager.notify_crawl_empty("douyin_hotwords")
            return None

        # 固化本次输出使用的“分区日期/拉取时间”
        daily_date = run_day or datetime.now().strftime("%Y%m%d")
        slot_str = run_slot or datetime.now().strftime("%H%M")

        # 将 query_day 统一改为指定日期
        today_str = daily_date
        for item in self.all_data:
            item["query_day"] = today_str

        self.all_data.sort(key=lambda x: x.get("score", 0) or 0, reverse=True)
        for idx, item in enumerate(self.all_data):
            item["rank"] = idx + 1

        self.analyze_data()

        # ── 为每条数据补充拼音 ──
        for item in self.all_data:
            title = item.get("title", "") or ""
            title = title.strip()
            if title and re.fullmatch(r"[\u4e00-\u9fffA-Za-z]+", title):
                processed_chars = []
                for ch in title:
                    if ch.isalpha() and ch.isascii():
                        processed_chars.append(ch.lower())
                    else:
                        processed_chars.append(ch)
                item["pinyin"] = " ".join(lazy_pinyin(processed_chars))
            else:
                item["pinyin"] = ""

        # ── 保存唯一输出: douyin_hotwords_{date}_{HHMM}.json ──
        save_dir = self._date_save_dir(daily_date)
        json_file = os.path.join(save_dir, f"douyin_hotwords_{daily_date}_{slot_str}.json")
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(self.all_data, f, indent=2, ensure_ascii=False)
        logger.info(f"处理结果已保存到: {json_file} (共{len(self.all_data)}条)")

        logger.info("任务完成！")
        return json_file

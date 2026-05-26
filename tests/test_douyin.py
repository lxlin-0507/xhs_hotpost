"""
tests/test_douyin.py — 抖音爬虫模块测试

注意: 完整爬取需要 auth_state.json，此文件仅测试模块导入和工具函数。
完整测试请参考下方 "手动测试" 说明。

运行:
    cd ~/Desktop/oppo-webserver
    python -m pytest tests/test_douyin.py -v
    # 或:
    python tests/test_douyin.py

手动测试（需有效登录态）:
    # 1. 首次扫码登录
    python -m crawler.douyin.run --login

    # 2. 查看登录状态
    python -m crawler.douyin.run --status

    # 3. 执行爬取（少量页面）
    python -m crawler.douyin.run --max-pages 2

    # 4. 检查输出
    ls -la output/douyin_hotwords/$(date +%Y%m%d)/
"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def test_import_douyin():
    """测试抖音模块可以正常导入"""
    from crawler.douyin import DouyinAuthenticator, DouyinHotSpider
    assert DouyinAuthenticator is not None
    assert DouyinHotSpider is not None
    assert DouyinHotSpider.SOURCE_NAME == "douyin_hotwords"
    print("✓ test_import_douyin PASSED")


def test_import_run():
    """测试 run 模块可以正常导入"""
    from crawler.douyin.run import main, check_login_status, do_login
    assert callable(main)
    assert callable(check_login_status)
    print("✓ test_import_run PASSED")


def test_config():
    """测试配置加载"""
    from config import Config
    assert Config.DOUYIN_SAVE_DIR is not None
    assert "douyin_hotwords" in Config.DOUYIN_SAVE_DIR.lower() or "output" in Config.DOUYIN_SAVE_DIR.lower()
    print("✓ test_config PASSED")


if __name__ == "__main__":
    test_import_douyin()
    test_import_run()
    test_config()
    print("\n全部 douyin 导入测试通过！")
    print("\n完整爬取测试请使用:")
    print("  python -m crawler.douyin.run --login      # 首次登录")
    print("  python -m crawler.douyin.run --max-pages 2 # 小规模爬取")

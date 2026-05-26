"""
tests/test_store.py — StoreManager 按来源存储 + 检索测试

运行:
    cd ~/Desktop/oppo-webserver
    python -m pytest tests/test_store.py -v
    # 或直接:
    python tests/test_store.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def test_store_save_and_list():
    """测试按来源保存和检索文件"""
    from pipeline.store import StoreManager

    with tempfile.TemporaryDirectory() as tmpdir:
        store = StoreManager(output_dir=tmpdir)

        # 保存抖音热词数据
        store.save_json("douyin_hotwords", "test_data.json", {"title": "测试"}, date="20260318")
        store.save_text("douyin_hotwords", "daily_data.txt", "hello\nworld", date="20260318")

        # 保存搜狗新词数据
        store.save_text("sogou_newwords", "sogou_words.txt", "你好\n世界", date="20260318")

        # 检索来源
        sources = store.list_sources()
        assert "douyin_hotwords" in sources, f"Expected douyin_hotwords in sources, got {sources}"
        assert "sogou_newwords" in sources, f"Expected sogou_newwords in sources, got {sources}"

        # 检索日期
        dates = store.list_dates("douyin_hotwords")
        assert "20260318" in dates, f"Expected 20260318 in dates, got {dates}"

        # 检索文件
        files = store.list_files("douyin_hotwords", "20260318")
        assert len(files) == 2, f"Expected 2 files, got {len(files)}"
        filenames = [f.filename for f in files]
        assert "test_data.json" in filenames

        # 获取最新
        latest = store.get_latest_files("douyin_hotwords")
        assert len(latest) > 0

        # 通过 glob 查找
        path = store.get_file_path("douyin_hotwords", "20260318", "test_*.json")
        assert path is not None
        with open(path, "r") as f:
            data = json.load(f)
        assert data["title"] == "测试"

        # 汇总
        summary = store.get_source_summary()
        assert len(summary) == 2

        # 生成索引
        manifest = store.build_manifest()
        assert "douyin_hotwords" in manifest
        assert "sogou_newwords" in manifest

    print("✓ test_store_save_and_list PASSED")


def test_store_multi_dates():
    """测试多日期检索"""
    from pipeline.store import StoreManager

    with tempfile.TemporaryDirectory() as tmpdir:
        store = StoreManager(output_dir=tmpdir)

        store.save_text("douyin_hotwords", "a.txt", "day1", date="20260317")
        store.save_text("douyin_hotwords", "b.txt", "day2", date="20260318")

        dates = store.list_dates("douyin_hotwords")
        assert dates == ["20260318", "20260317"], f"Dates should be descending, got {dates}"

        latest = store.get_latest_files("douyin_hotwords")
        assert latest[0].date == "20260318"

        all_files = store.list_files("douyin_hotwords")
        assert len(all_files) == 2

    print("✓ test_store_multi_dates PASSED")


if __name__ == "__main__":
    test_store_save_and_list()
    test_store_multi_dates()
    print("\n全部 store 测试通过！")

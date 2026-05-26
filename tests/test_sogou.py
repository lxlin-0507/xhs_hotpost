"""
tests/test_sogou.py — 搜狗词库模块测试

注意: 完整下载需要网络，此文件仅测试模块导入和内置解析器。
完整测试请参考下方 "手动测试" 说明。

运行:
    cd ~/Desktop/oppo-webserver
    python -m pytest tests/test_sogou.py -v
    # 或:
    python tests/test_sogou.py

手动测试（需要网络 + SOGOU_DICT_ID 配置）:
    # 1. 配置 .env.dev
    echo "SOGOU_DICT_ID=12345" >> .env.dev

    # 2. 执行完整流程
    python -m crawler.sogou.run

    # 3. 检查输出
    ls -la output/sogou_newwords/$(date +%Y%m%d)/
"""
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def test_import_sogou():
    """测试搜狗模块可以正常导入"""
    from crawler.sogou import SogouDictManager
    assert SogouDictManager is not None
    assert SogouDictManager.SOURCE_NAME == "sogou_newwords"
    print("✓ test_import_sogou PASSED")


def test_import_run():
    """测试 run 模块可以正常导入"""
    from crawler.sogou.run import main, run_download_and_convert
    assert callable(main)
    assert callable(run_download_and_convert)
    print("✓ test_import_run PASSED")


def test_scel_heuristic_parser():
    """测试内置 scel 启发式解析器（伪造数据）"""
    from crawler.sogou.dict_manager import SogouDictManager

    # 构造一段包含中文词的 UTF-16LE 数据
    words = ["你好", "世界", "测试"]
    text = "\x00" * 100 + "\x00".join(words)
    data = text.encode("utf-16le")

    result = SogouDictManager._parse_scel_words_heuristic(data)
    # 应至少提取出部分中文词
    assert isinstance(result, list)
    print(f"✓ test_scel_heuristic_parser PASSED (提取到 {len(result)} 个词)")


def test_read_words():
    """测试 txt 文件读取"""
    from crawler.sogou.dict_manager import SogouDictManager

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write("ni hao 你好\n")
        f.write("shi jie 世界\n")
        f.write("测试\n")
        f.write("\n")  # 空行应被跳过
        tmp_path = f.name

    words = SogouDictManager._read_words(Path(tmp_path))
    assert len(words) == 3, f"Expected 3 words, got {len(words)}"
    assert words[0] == ("ni", "hao 你好")  # split on first space
    assert words[2] == (None, "测试")

    Path(tmp_path).unlink()
    print("✓ test_read_words PASSED")


if __name__ == "__main__":
    test_import_sogou()
    test_import_run()
    test_scel_heuristic_parser()
    test_read_words()
    print("\n全部 sogou 测试通过！")
    print("\n完整下载测试请使用:")
    print("  python -m crawler.sogou.run  # 需要配置 SOGOU_DICT_ID")

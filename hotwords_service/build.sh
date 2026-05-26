#!/usr/bin/env bash
# hotwords_service/build.sh — 打包成单文件可执行程序
# 用法：bash build.sh
# 产物：dist/hotwords_service（Linux/macOS 可执行文件）

set -e
cd "$(dirname "$0")"

echo "── 安装 PyInstaller ──"
pip install pyinstaller --quiet

echo "── 开始打包 ──"
pyinstaller \
  --onefile \
  --name hotwords_service \
  --add-data "stopwords.txt:." \
  --add-data "blocklist.txt:." \
  --add-data "user_dict.txt:." \
  --add-data "sogou_snapshot.txt:." \
  --add-data "segmenter_hanlp.py:." \
  run.py

echo ""
echo "✓ 打包完成：dist/hotwords_service"
echo ""
echo "用法："
echo "  ./dist/hotwords_service --once --base-dir /tmp/hotwords"
echo "  ./dist/hotwords_service --base-dir /tmp/hotwords   # 后台定时服务"

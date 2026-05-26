#!/bin/bash
set -e

# ══════════════════════════════════════════════════════════════
# oppo-webserver 一键部署脚本
#
# 前置条件:
#   1. 代码已上传到服务器（rsync / scp）
#   2. auth_state.json 已放置到项目根目录
#   3. id_ed25519 密钥已放置到项目根目录（用于 SFTP 同步）
#
# 用法:
#   cd /opt/oppo-webserver && bash install.sh
# ══════════════════════════════════════════════════════════════

APP_DIR="/opt/oppo-webserver"
DATA_LINK="/data/hotword"
SERVICE_NAME="oppo-webserver"
PYTHON="python3"

echo "══════════════════════════════════════════════════════════"
echo "  oppo-webserver 一键部署"
echo "══════════════════════════════════════════════════════════"

# ── 1. 检查 Python ────────────────────────────────────────
echo ""
echo "➜ [1/9] 检查 Python..."
if ! command -v $PYTHON &> /dev/null; then
    echo "  ✗ 未找到 python3，请先安装:"
    echo "    Ubuntu/Debian: sudo apt install python3 python3-venv"
    echo "    CentOS/RHEL:   sudo yum install python3"
    exit 1
fi
PY_VER=$($PYTHON --version 2>&1)
echo "  ✓ $PY_VER"

# ── 2. 准备项目目录 ───────────────────────────────────────
echo ""
echo "➜ [2/9] 准备项目目录: $APP_DIR"
sudo mkdir -p "$APP_DIR"
sudo chown "$(whoami):$(whoami)" "$APP_DIR"

# 如果当前目录不是 APP_DIR，复制代码过去
if [ "$(pwd)" != "$APP_DIR" ] && [ -f "$(pwd)/config.py" ]; then
    echo "  复制代码到 $APP_DIR ..."
    rsync -av --exclude='output/' --exclude='venv/' --exclude='__pycache__/' \
          --exclude='*.scel' --exclude='.git/' \
          . "$APP_DIR/"
    echo "  ✓ 代码已复制"
fi

cd "$APP_DIR"

# ── 3. 检查关键文件 ───────────────────────────────────────
echo ""
echo "➜ [3/9] 检查关键文件..."

# auth_state.json
if [ -f "$APP_DIR/auth_state.json" ]; then
    echo "  ✓ auth_state.json 已就位"
else
    echo "  ⚠ 未找到 auth_state.json（抖音爬取需要）"
    echo "    请在本地扫码登录后拷贝到 $APP_DIR/auth_state.json"
fi

# id_ed25519
if [ -f "$APP_DIR/id_ed25519" ]; then
    chmod 600 "$APP_DIR/id_ed25519"
    echo "  ✓ id_ed25519 已就位（权限已设为 600）"
else
    echo "  ⚠ 未找到 id_ed25519（SFTP 同步需要）"
    echo "    请拷贝密钥到 $APP_DIR/id_ed25519"
fi

# ── 4. 创建虚拟环境 + 安装依赖 ────────────────────────────
echo ""
echo "➜ [4/9] 创建虚拟环境 & 安装依赖..."
if [ ! -d "$APP_DIR/venv" ]; then
    $PYTHON -m venv venv
    echo "  ✓ 虚拟环境已创建"
else
    echo "  虚拟环境已存在，跳过创建"
fi

source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "  ✓ Python 依赖安装完成"

# ── 5. 安装 Playwright 浏览器 ─────────────────────────────
echo ""
echo "➜ [5/9] 安装 Playwright Chromium..."

PLAYWRIGHT_OK=false
PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright \
    python -m playwright install chromium 2>/dev/null && PLAYWRIGHT_OK=true

if [ "$PLAYWRIGHT_OK" = false ]; then
    echo "  国内镜像不可用，尝试官方 CDN（可能较慢）..."
    python -m playwright install chromium 2>/dev/null && PLAYWRIGHT_OK=true
fi

if [ "$PLAYWRIGHT_OK" = true ]; then
    echo "  ✓ Chromium 安装成功"
    echo "  安装系统依赖..."
    sudo DEBIAN_FRONTEND=noninteractive python -m playwright install-deps chromium 2>/dev/null || {
        echo "  ⚠ 系统依赖自动安装失败，请手动执行:"
        echo "    sudo DEBIAN_FRONTEND=noninteractive $(which python) -m playwright install-deps chromium"
    }
else
    echo "  ⚠ Playwright Chromium 下载失败！"
    echo "    爬虫会以降级模式运行（从 auth_state.json 直接加载 Cookie）。"
    echo "    如需启用，稍后手动执行:"
    echo "      cd $APP_DIR && source venv/bin/activate"
    echo "      python -m playwright install chromium"
fi

# ── 6. 创建 .env.dev 配置 ────────────────────────────────
echo ""
echo "➜ [6/9] 检查配置文件..."
if [ ! -f "$APP_DIR/.env.dev" ]; then
    cat > "$APP_DIR/.env.dev" << 'ENVEOF'
# ── 环境 ──────────────────────────────────────────────────
ENV=production
DEBUG=false
LOG_LEVEL=INFO

# ── 暂存输出目录（crawler 先写这里）─────────────────────────
OUTPUT_DIR=output

# ── 抖音 ─────────────────────────────────────────────────
DOUYIN_SAVE_DIR=output/douyin_hotwords
DOUYIN_AUTH_STATE=auth_state.json

# ── 搜狗 ─────────────────────────────────────────────────
SOGOU_SAVE_DIR=output/sogou_newwords

# ── 最终对外目录（move 后写这里）──────────────────────────
HOTWORDS_DIR=/crawler/hotwords
SOGOU_DICT_URL=https://pinyin.sogou.com/d/dict/download_cell.php?id=4&name=%E7%BD%91%E7%BB%9C%E6%B5%81%E8%A1%8C%E6%96%B0%E8%AF%8D&f=detail
SOGOU_DICT_ID=4

# ── SFTP 同步（推送到 SFTP 服务器供第三方拉取）────────────
SFTP_HOST=82.156.76.22
SFTP_USER=oppo01
SFTP_KEY_PATH=id_ed25519
SFTP_REMOTE_DIR=/crawler/upload

# ── 报警（钉钉，暂未启用）─────────────────────────────────
ALERT_ENABLED=false
ALERT_DINGTALK_WEBHOOK=
ALERT_DINGTALK_SECRET=

# ── 热词阈值 ─────────────────────────────────────────────
HOT_WORD_GROWTH_THRESHOLD=0.5
JSON_FILE_RETENTION_DAYS=30

# ── API ──────────────────────────────────────────────────
API_HOST=0.0.0.0
API_PORT=5001
API_AUTH_TOKEN=default_secret
ENVEOF
    echo "  ✓ .env.dev 已生成（请根据需要修改）"
else
    echo "  ✓ .env.dev 已存在，跳过"
fi

# ── 7. 创建输出目录 & 软链接 ──────────────────────────────
echo ""
echo "➜ [7/9] 创建输出目录..."
mkdir -p /crawler/hotwords/sogou_newwords
mkdir -p /crawler/hotwords/douyin_hotwords
mkdir -p /crawler/hotwords/weibo_hotsearch
mkdir -p /crawler/hotwords/douyin_hotlist
mkdir -p /crawler/hotwords/xhs_hot

sudo mkdir -p /data
if [ -L "$DATA_LINK" ]; then
    echo "  软链接 $DATA_LINK 已存在"
elif [ -e "$DATA_LINK" ]; then
    echo "  ⚠ $DATA_LINK 已存在且不是软链接，请手动处理"
else
    sudo ln -s "$APP_DIR/output" "$DATA_LINK"
    echo "  ✓ 软链接: $DATA_LINK → $APP_DIR/output"
fi

# ── 8. 配置 SFTP 定时同步（cron）──────────────────────────
echo ""
echo "➜ [8/9] 配置 SFTP 定时同步..."

chmod +x "$APP_DIR/sync_to_sftp.sh"

# 使用同一个 tag 标记本脚本生成的 cron；每次安装/更新都会先清理旧条目
CRON_TAG="# oppo-sync"
if crontab -l 2>/dev/null | grep -q "$CRON_TAG"; then
    echo "  发现旧 cron，准备更新..."
fi
{
    # 验证 SFTP 连接（该服务器只允许 sftp，不支持 ssh）
    SFTP_OK=false
    if [ -f "$APP_DIR/id_ed25519" ]; then
        echo "  验证 SFTP 连接..."
        echo "ls /crawler/upload" | sftp -i "$APP_DIR/id_ed25519" \
            -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
            oppo01@82.156.76.22 2>/dev/null && SFTP_OK=true
    fi

    if [ "$SFTP_OK" = true ]; then
        echo "  ✓ SFTP 连接成功"
    else
        echo "  ⚠ SFTP 连接验证失败（密钥缺失或网络不通），cron 仍会配置"
    fi

    chmod +x "$APP_DIR/crawler/move_output_to_hotwords.sh"

    # 写入 cron
    (crontab -l 2>/dev/null | grep -v "$CRON_TAG" || true; cat << EOF

# ── oppo-webserver 暂存文件 -> /crawler/hotwords（每30分钟）────
5,35 * * * * $APP_DIR/crawler/move_output_to_hotwords.sh >> $APP_DIR/move.log 2>&1 $CRON_TAG

# ── oppo-webserver SFTP 同步（每30分钟，覆盖模式）────────────
10,40 * * * * $APP_DIR/crawler/sync_to_sftp.sh >> $APP_DIR/sync.log 2>&1 $CRON_TAG
EOF
    ) | crontab -

    echo "  ✓ cron 已配置:"
    echo "    move: 每小时 05/35"
    echo "    sync: 每小时 10/40"
}

# ── 9. 创建 systemd 服务 ──────────────────────────────────
echo ""
echo "➜ [9/9] 创建系统服务..."

sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null << EOF
[Unit]
Description=OPPO WebServer - 热词爬取定时任务
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python -m pipeline.scheduler
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable $SERVICE_NAME 2>/dev/null
echo "  ✓ systemd 服务已创建并设为开机自启"

# ── 完成 ──────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════════════════"
echo "  ✓ 部署完成！"
echo "══════════════════════════════════════════════════════════"
echo ""

# 检查缺失项
MISSING=0
if [ ! -f "$APP_DIR/auth_state.json" ]; then
    echo "  ⚠ 缺少 auth_state.json → scp auth_state.json 用户@服务器:$APP_DIR/"
    MISSING=1
fi
if [ ! -f "$APP_DIR/id_ed25519" ]; then
    echo "  ⚠ 缺少 id_ed25519    → scp id_ed25519 用户@服务器:$APP_DIR/"
    MISSING=1
fi
if [ $MISSING -eq 1 ]; then
    echo ""
fi

echo "  启动服务:   sudo systemctl start $SERVICE_NAME"
echo "  查看状态:   sudo systemctl status $SERVICE_NAME"
echo "  查看日志:   sudo journalctl -u $SERVICE_NAME -f"
echo "  同步日志:   tail -f $APP_DIR/sync.log"
echo ""
echo "  手动测试:"
echo "    cd $APP_DIR && source venv/bin/activate"
echo "    python -m crawler.sogou.run               # 搜狗"
echo "    python -m crawler.douyin.run --max-pages 3 # 抖音"
echo "    bash sync_to_sftp.sh                      # SFTP 同步"
echo ""

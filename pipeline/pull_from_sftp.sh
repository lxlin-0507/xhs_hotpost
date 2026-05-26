#!/bin/bash
# ══════════════════════════════════════════════════════════════
# OPPO 热词数据拉取 — 一键部署 & 使用脚本
#
# 此脚本在【第三方机器】上运行，完成以下工作:
#   - 首次运行: 配置环境 + 设置 cron 定时拉取
#   - 后续运行: 从 SFTP 服务器拉取最新热词数据（覆盖本地同名文件）
#
# ═══════════════════════════════════════════════════════════
#  一键部署（首次）:
#    1. 将此脚本和 SFTP 私钥放到同一目录
#    2. 执行: bash pull_from_sftp.sh --install
#
#  手动拉取:
#    bash pull_from_sftp.sh                  # 拉取当天最新（默认，覆盖本地）
#    bash pull_from_sftp.sh --all            # 拉取全部历史
#    bash pull_from_sftp.sh --date 20260319  # 拉指定日期
# ═══════════════════════════════════════════════════════════
#
# 数据说明:
#   搜狗词库 — sogou_{日期}_{HHMM}.txt（Tab 分隔: 词条\t拼音）
#   抖音热词 — douyin_hotwords_{日期}_{HHMM}.json（JSON: 词/拼音/热度/排名等）
#   抖音热榜 — douyin_hotlist_{日期}_{HHMM}.txt（Tab 分隔: 词条\t拼音\t热度）
#   小红书热点 — xhs_hot_{日期}_{HHMM}.txt（Tab 分隔: 词条\t拼音\t热度）
#   微博热搜   — weibo_hotsearch_{日期}_{HHMM}.txt（Tab 分隔: 词条\t拼音\t热度）
#
# 拉取频率: 每 30 分钟自动拉取一次当天最新数据
# ══════════════════════════════════════════════════════════════

set -e

# ══════════════════════════════════════════════════════════════
#  以下配置请根据实际情况修改
# ══════════════════════════════════════════════════════════════
SFTP_USER="oppo01"
SFTP_HOST="82.156.76.22"
SFTP_REMOTE_DIR="/upload/hotword"

# 密钥文件路径（默认与本脚本同目录的 id_ed25519）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SFTP_KEY="${SFTP_KEY:-$SCRIPT_DIR/id_ed25519}"

# 数据保存目录
LOCAL_DIR="${LOCAL_DIR:-$SCRIPT_DIR/hotword_data}"
# ══════════════════════════════════════════════════════════════

LOG_FILE="$SCRIPT_DIR/pull.log"
CRON_TAG="# oppo-hotword-pull"
SFTP_OPTS="-i $SFTP_KEY -o StrictHostKeyChecking=no -o ConnectTimeout=10"

# ────────────────────────────────────────────────────────────
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# ────────────────────────────────────────────────────────────
preflight() {
    if ! command -v sftp &>/dev/null; then
        log "ERROR: 未安装 sftp"
        echo "  请先安装: sudo apt install openssh-client  或  sudo yum install openssh-clients"
        exit 1
    fi

    if [ ! -f "$SFTP_KEY" ]; then
        log "ERROR: 密钥文件不存在: $SFTP_KEY"
        echo ""
        echo "  请将 SFTP 私钥放到以下任一位置:"
        echo "    1. $SCRIPT_DIR/id_ed25519（与本脚本同目录）"
        echo "    2. 设置环境变量: SFTP_KEY=/path/to/key bash $0"
        exit 1
    fi

    chmod 600 "$SFTP_KEY" 2>/dev/null || true
}

# ────────────────────────────────────────────────────────────
# 列出远程目录下的条目（只返回名称，过滤掉 sftp 提示符）
# 用法: sftp_ls /remote/path
# ────────────────────────────────────────────────────────────
sftp_ls() {
    local remote_path="$1"
    sftp $SFTP_OPTS "${SFTP_USER}@${SFTP_HOST}" <<SFTPCMD 2>/dev/null | \
        grep -v "^Connected\|^sftp>" | \
        sed "s|${remote_path}/||g" | \
        tr -s ' ' | \
        grep -v '^\s*$'
ls ${remote_path}/
quit
SFTPCMD
}

# ────────────────────────────────────────────────────────────
# 拉取指定日期的数据
# ────────────────────────────────────────────────────────────
pull_date() {
    local date_str="$1"

    for source in sogou_newwords douyin_hotwords douyin_hotlist xhs_hot xhs_hotpost weibo_hotsearch; do
        local remote_dir="${SFTP_REMOTE_DIR}/${source}/${date_str}"
        local local_dir="${LOCAL_DIR}/${source}/${date_str}"

        # 列出远程文件
        local files
        files=$(sftp_ls "$remote_dir" 2>/dev/null) || true

        if [ -z "$files" ]; then
            log "  ${source}/${date_str}: 无数据或目录不存在"
            continue
        fi

        mkdir -p "$local_dir"

        # 构建批量下载命令（每次都覆盖本地文件，保证拿到最新版本）
        local batch_file
        batch_file=$(mktemp /tmp/sftp_pull_XXXXXX)
        local count=0

        while IFS= read -r fname; do
            fname=$(echo "$fname" | tr -d '\r' | xargs)  # 去除空白
            [ -z "$fname" ] && continue

            # 只下载最终“全量 all”文件（避免历史遗留的时间戳文件干扰）
            case "$source" in
                sogou_newwords)
                    case "$fname" in
                        sogou_????????_????.txt) ;;
                        *) continue ;;
                    esac
                    ;;
                douyin_hotwords)
                    case "$fname" in
                        douyin_hotwords_????????_????.json) ;;
                        *) continue ;;
                    esac
                    ;;
                douyin_hotlist)
                    case "$fname" in
                        douyin_hotlist_????????_????.txt) ;;
                        *) continue ;;
                    esac
                    ;;
                xhs_hot)
                    case "$fname" in
                        xhs_hot_????????_????.txt) ;;
                        *) continue ;;
                    esac
                    ;;
                xhs_hotpost)
                    case "$fname" in
                        xhs_hotpost_????????_????.json) ;;
                        *) continue ;;
                    esac
                    ;;
                weibo_hotsearch)
                    case "$fname" in
                        weibo_hotsearch_????????_????.txt) ;;
                        *) continue ;;
                    esac
                    ;;
            esac

            echo "get ${remote_dir}/${fname} ${local_dir}/${fname}" >> "$batch_file"
            count=$((count + 1))
        done <<< "$files"

        if [ $count -gt 0 ]; then
            echo "quit" >> "$batch_file"
            log "  下载 ${source}/${date_str}: ${count} 个文件（覆盖模式）"
            sftp $SFTP_OPTS -b "$batch_file" "${SFTP_USER}@${SFTP_HOST}" 2>/dev/null || {
                log "  ⚠ ${source}/${date_str} 部分文件下载失败"
            }
        else
            log "  ${source}/${date_str}: 远程无文件"
        fi

        rm -f "$batch_file"
    done
}

# ────────────────────────────────────────────────────────────
# 拉取全部数据
# ────────────────────────────────────────────────────────────
pull_all() {
    for source in sogou_newwords douyin_hotwords douyin_hotlist xhs_hot xhs_hotpost weibo_hotsearch; do
        local dates
        dates=$(sftp_ls "${SFTP_REMOTE_DIR}/${source}" 2>/dev/null) || true

        if [ -z "$dates" ]; then
            log "  ${source}: 无数据"
            continue
        fi

        while IFS= read -r date_dir; do
            date_dir=$(echo "$date_dir" | tr -d '\r' | xargs)
            [ -z "$date_dir" ] && continue
            # 只处理看起来像日期的目录（8位数字）
            if echo "$date_dir" | grep -qE '^[0-9]{8}$'; then
                pull_date "$date_dir"
            fi
        done <<< "$dates"
    done
}

# ────────────────────────────────────────────────────────────
# 拉取入口
# ────────────────────────────────────────────────────────────
do_pull() {
    preflight
    mkdir -p "$LOCAL_DIR"

    log "开始拉取 → ${SFTP_USER}@${SFTP_HOST}:${SFTP_REMOTE_DIR}"

    if [ -n "$DATE_FILTER" ]; then
        pull_date "$DATE_FILTER"
    else
        pull_all
    fi

    TOTAL=$(find "$LOCAL_DIR" -type f \( -name "*.txt" -o -name "*.json" \) 2>/dev/null | wc -l | tr -d ' ')
    log "✓ 拉取完成，本地共 ${TOTAL} 个文件 → $LOCAL_DIR"
}

# ────────────────────────────────────────────────────────────
# 安装模式
# ────────────────────────────────────────────────────────────
do_install() {
    echo "══════════════════════════════════════════════════════════"
    echo "  OPPO 热词数据拉取 — 一键部署"
    echo "══════════════════════════════════════════════════════════"
    echo ""

    preflight

    # ── 1. 验证 SFTP 连接
    echo "➜ [1/4] 验证 SFTP 连接..."
    local test_result
    test_result=$(sftp_ls "$SFTP_REMOTE_DIR" 2>/dev/null) || true

    if [ -n "$test_result" ]; then
        echo "  ✓ 连接成功: ${SFTP_USER}@${SFTP_HOST}:${SFTP_REMOTE_DIR}"
        echo "  远程目录内容: $test_result"
    else
        log "ERROR: 无法连接或远程目录为空"
        echo "  请检查:"
        echo "    - 密钥是否正确: $SFTP_KEY"
        echo "    - 手动测试: sftp -i $SFTP_KEY ${SFTP_USER}@${SFTP_HOST}"
        exit 1
    fi

    # ── 2. 创建数据目录
    echo ""
    echo "➜ [2/4] 创建数据目录: $LOCAL_DIR"
    mkdir -p "$LOCAL_DIR"
    echo "  ✓ 目录已创建"

    # ── 3. 首次拉取
    echo ""
    echo "➜ [3/4] 首次拉取数据..."
    do_pull
    echo ""

    # ── 4. 配置 cron
    echo "➜ [4/4] 配置定时拉取..."
    if crontab -l 2>/dev/null | grep -q "$CRON_TAG"; then
        echo "  cron 已配置，跳过"
    else
        SCRIPT_PATH="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

        (crontab -l 2>/dev/null; cat << EOF

# ── OPPO 热词自动拉取（每30分钟，与推送错开15分钟）─────
15,45 * * * * $SCRIPT_PATH >> $LOG_FILE 2>&1 $CRON_TAG
EOF
        ) | crontab -

        echo "  ✓ cron 定时任务已配置:"
        echo "    每小时 :15 和 :45 拉取（推送在 :00 和 :30，错开15分钟）"
    fi

    # ── 完成
    echo ""
    echo "══════════════════════════════════════════════════════════"
    echo "  ✓ 部署完成！"
    echo "══════════════════════════════════════════════════════════"
    echo ""
    echo "  数据目录:  $LOCAL_DIR"
    echo "  拉取日志:  $LOG_FILE"
    echo ""
    echo "  常用命令:"
    echo "    bash $0                   # 拉取当天最新（覆盖模式）"
    echo "    bash $0 --all             # 拉取全部历史"
    echo "    bash $0 --date 20260319   # 拉指定日期"
    echo "    crontab -l                # 查看定时任务"
    echo "    tail -f $LOG_FILE         # 查看拉取日志"
    echo ""
    echo "  卸载定时任务:"
    echo "    bash $0 --uninstall"
    echo ""
}

# ────────────────────────────────────────────────────────────
# 卸载 cron
# ────────────────────────────────────────────────────────────
do_uninstall() {
    echo "移除 cron 定时任务..."
    if crontab -l 2>/dev/null | grep -q "$CRON_TAG"; then
        crontab -l 2>/dev/null | grep -v "$CRON_TAG" | \
            sed '/^# ── OPPO 热词自动拉取/d' | \
            sed '/^# 搜狗词库（每天/d' | \
            sed '/^# 抖音热词（每天/d' | crontab -
        echo "  ✓ 已移除"
    else
        echo "  未找到相关 cron 任务"
    fi
}

# ────────────────────────────────────────────────────────────
# 主入口
# ────────────────────────────────────────────────────────────
DATE_FILTER=""

case "${1:-}" in
    --install)
        do_install
        ;;
    --uninstall)
        do_uninstall
        ;;
    --today)
        # 兼容旧用法，等同于默认行为
        DATE_FILTER=$(date +%Y%m%d)
        do_pull
        ;;
    --all)
        # 拉取全部历史数据
        do_pull
        ;;
    --date)
        if [ -z "${2:-}" ]; then
            echo "用法: $0 --date YYYYMMDD"
            exit 1
        fi
        DATE_FILTER="$2"
        do_pull
        ;;
    --help|-h)
        echo "OPPO 热词数据拉取脚本"
        echo ""
        echo "用法:"
        echo "  bash $0 --install          一键部署（首次执行）"
        echo "  bash $0                    拉取当天最新（覆盖模式，默认）"
        echo "  bash $0 --all              拉取全部历史数据（覆盖模式）"
        echo "  bash $0 --date 20260319    拉取指定日期的数据（覆盖模式）"
        echo "  bash $0 --today            等同于默认行为"
        echo "  bash $0 --uninstall        移除定时任务"
        echo "  bash $0 --help             显示帮助"
        echo ""
        echo "环境变量:"
        echo "  SFTP_KEY      SFTP 密钥路径（默认: 脚本同目录/id_ed25519）"
        echo "  LOCAL_DIR     数据保存目录（默认: 脚本同目录/hotword_data）"
        echo ""
        echo "数据说明:"
        echo "  sogou_????????_????.txt              搜狗词库（Tab 分隔: 词条\\t拼音）"
    echo "  douyin_hotwords_????????_????.json   抖音热词（JSON: 词/拼音/热度/排名等）"
    echo "  douyin_hotlist_????????_????.txt     抖音热榜（Tab 分隔: 词条\\t拼音\\t热度）"
    echo "  xhs_hot_????????_????.txt            小红书热点（Tab 分隔: 词条\\t拼音\\t热度）"
    echo "  weibo_hotsearch_????????_????.txt    微博热搜（Tab 分隔: 词条\\t拼音\\t热度）"
        ;;
    "")
        # 默认: 只拉取当天数据
        DATE_FILTER=$(date +%Y%m%d)
        do_pull
        ;;
    *)
        echo "未知参数: $1"
        echo "执行 $0 --help 查看用法"
        exit 1
        ;;
esac

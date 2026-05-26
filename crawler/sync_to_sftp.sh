#!/bin/bash
# ══════════════════════════════════════════════════════════════
# 同步 /crawler/hotwords（最终对外目录）到 SFTP 服务器（覆盖模式，由 cron 定期调用）
#
# 从 .env.dev 读取 SFTP 配置，推送各数据源的输出文件:
#   - sogou_newwords/    搜狗网络流行新词 (sogou_YYYYMMDD_HHMM.txt)
#   - douyin_hotwords/   抖音热词 (douyin_hotwords_YYYYMMDD_HHMM.json)
#   - weibo_hotsearch/   微博热搜
#   - douyin_hotlist/    抖音热榜
#   - xhs_hot/           小红书热点
#   - xhs_hotpost/       小红书热帖 (xhs_hotpost_YYYYMMDD_HHMM.json)
#
# 注意: SFTP 服务器只允许 sftp 连接，不支持 ssh/rsync
# ══════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$APP_DIR/.env.dev"
if [ ! -f "$ENV_FILE" ] && [ -f "$SCRIPT_DIR/.env.dev" ]; then
    # 兼容旧部署：.env.dev 放在 crawler/ 目录
    ENV_FILE="$SCRIPT_DIR/.env.dev"
fi

# ── 从 .env.dev 读配置 ───────────────────────────────────
get_env() {
    local key="$1"
    local default="$2"
    if [ -f "$ENV_FILE" ]; then
        val=$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d'=' -f2-)
        echo "${val:-$default}"
    else
        echo "$default"
    fi
}

SFTP_USER=$(get_env "SFTP_USER" "")
SFTP_HOST=$(get_env "SFTP_HOST" "")
SFTP_KEY=$(get_env "SFTP_KEY_PATH" "$APP_DIR/id_ed25519")
SFTP_REMOTE_DIR=$(get_env "SFTP_REMOTE_DIR" "/crawler/upload")
LOCAL_OUTPUT=$(get_env "HOTWORDS_DIR" "/crawler/hotwords")

# 相对路径统一转为项目根绝对路径（兼容旧/手工部署）
if [[ "$LOCAL_OUTPUT" != /* ]]; then
    LOCAL_OUTPUT="$APP_DIR/$LOCAL_OUTPUT"
fi

# 如果 SFTP_KEY 是相对路径，拼上 APP_DIR
[[ "$SFTP_KEY" != /* ]] && SFTP_KEY="$APP_DIR/$SFTP_KEY"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# ── 检查配置 ──────────────────────────────────────────────
if [ -z "$SFTP_USER" ] || [ -z "$SFTP_HOST" ]; then
    log "ERROR: SFTP_USER 或 SFTP_HOST 未配置，请检查 $ENV_FILE"
    exit 1
fi

if [ ! -f "$SFTP_KEY" ]; then
    log "ERROR: 密钥文件不存在: $SFTP_KEY"
    exit 1
fi

if [ ! -d "$LOCAL_OUTPUT" ]; then
    log "WARN: 输出目录不存在: $LOCAL_OUTPUT，跳过同步"
    exit 0
fi

# ── 收集需要上传的文件 ────────────────────────────────────
# 只上传最终输出文件
UPLOAD_FILES=$(find "$LOCAL_OUTPUT" -type f \( \
    -name "sogou_????????_????.txt" -o \
    -name "douyin_hotwords_????????_????.json" -o \
    -name "weibo_hotsearch_????????_????.txt" -o \
    -name "douyin_hotlist_????????_????.txt" -o \
    -name "xhs_hot_????????_????.txt" -o \
    -name "xhs_hotpost_????????_????.json" \
\) | sort)

if [ -z "$UPLOAD_FILES" ]; then
    log "没有需要同步的文件"
    exit 0
fi

# ── 构建 sftp 批量命令 ────────────────────────────────────
log "开始同步 → ${SFTP_USER}@${SFTP_HOST}:${SFTP_REMOTE_DIR}"

BATCH_FILE=$(mktemp /tmp/sftp_batch_XXXXXX)
trap "rm -f $BATCH_FILE" EXIT

# 确保远程目录结构存在
echo "-mkdir ${SFTP_REMOTE_DIR}" >> "$BATCH_FILE"
echo "-mkdir ${SFTP_REMOTE_DIR}/sogou_newwords" >> "$BATCH_FILE"
echo "-mkdir ${SFTP_REMOTE_DIR}/douyin_hotwords" >> "$BATCH_FILE"
echo "-mkdir ${SFTP_REMOTE_DIR}/weibo_hotsearch" >> "$BATCH_FILE"
echo "-mkdir ${SFTP_REMOTE_DIR}/douyin_hotlist" >> "$BATCH_FILE"
echo "-mkdir ${SFTP_REMOTE_DIR}/xhs_hot" >> "$BATCH_FILE"
echo "-mkdir ${SFTP_REMOTE_DIR}/xhs_hotpost" >> "$BATCH_FILE"
# 确保日期子目录存在（weibo/xhs/douyin_hotlist/xhs_hotpost 新增的目录）
for src_dir in weibo_hotsearch douyin_hotlist xhs_hot xhs_hotpost; do
    if [ -d "$LOCAL_OUTPUT/$src_dir" ]; then
        for date_dir in "$LOCAL_OUTPUT/$src_dir"/*/; do
            [ -d "$date_dir" ] || continue
            date_part=$(basename "$date_dir")
            echo "-mkdir ${SFTP_REMOTE_DIR}/${src_dir}/${date_part}" >> "$BATCH_FILE"
        done
    fi
done

# 收集日期目录并创建
for f in $UPLOAD_FILES; do
    # 从路径提取 source/date，如 output/douyin_hotwords/20260319/file.json → douyin_hotwords/20260319
    REL_PATH="${f#$LOCAL_OUTPUT/}"
    DIR_PART=$(dirname "$REL_PATH")
    echo "-mkdir ${SFTP_REMOTE_DIR}/${DIR_PART}" >> "$BATCH_FILE"
done

# 去重 mkdir 命令
sort -u "$BATCH_FILE" -o "$BATCH_FILE"

# 添加 put 命令
UPLOAD_COUNT=0
for f in $UPLOAD_FILES; do
    REL_PATH="${f#$LOCAL_OUTPUT/}"
    REMOTE_FILE="${SFTP_REMOTE_DIR}/${REL_PATH}"
    echo "put $f $REMOTE_FILE" >> "$BATCH_FILE"
    UPLOAD_COUNT=$((UPLOAD_COUNT + 1))
done

echo "quit" >> "$BATCH_FILE"

# ── 执行 sftp 批量上传 ────────────────────────────────────
sftp -i "$SFTP_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
    -b "$BATCH_FILE" "${SFTP_USER}@${SFTP_HOST}"

EXIT_CODE=$?
if [ $EXIT_CODE -eq 0 ]; then
    log "✓ 同步完成，共 ${UPLOAD_COUNT} 个文件"
else
    log "✗ 同步失败 (exit=$EXIT_CODE)"
fi
exit $EXIT_CODE

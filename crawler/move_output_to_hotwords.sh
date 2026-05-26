#!/bin/bash
# ══════════════════════════════════════════════════════════════
# 把 crawler 暂存目录 output/ 的“已完成文件”搬到 /crawler/hotwords/，供后续 SFTP 上传。
#
# 规则：
#   - 只移动“文件修改时间超过阈值”的内容，避免与正在写入的爬虫互相打架
#   - Douyin:         douyin_hotwords_YYYYMMDD_HHMM.json
#   - Sogou:          sogou_YYYYMMDD_HHMM.txt
#   - Weibo:          weibo_hotsearch_YYYYMMDD_HHMM.txt
#   - Douyin Hotlist: douyin_hotlist_YYYYMMDD_HHMM.txt
#   - XHS:            xhs_hot_YYYYMMDD_HHMM.txt
# ══════════════════════════════════════════════════════════════

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# 读取环境配置（优先项目根 .env.dev，其次兼容旧部署）
ENV_FILE="$APP_DIR/.env.dev"
if [ ! -f "$ENV_FILE" ] && [ -f "$SCRIPT_DIR/.env.dev" ]; then
  ENV_FILE="$SCRIPT_DIR/.env.dev"
fi

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

STAGING_ROOT="$(get_env "OUTPUT_DIR" "$APP_DIR/output")"
HOTWORDS_DIR="$(get_env "HOTWORDS_DIR" "/crawler/hotwords")"

# 将相对路径统一转为项目根目录下的绝对路径
if [[ "$STAGING_ROOT" != /* ]]; then
  STAGING_ROOT="$APP_DIR/$STAGING_ROOT"
fi
if [[ "$HOTWORDS_DIR" != /* ]]; then
  HOTWORDS_DIR="$APP_DIR/$HOTWORDS_DIR"
fi

# 文件必须至少“老”这么久才会被移动
MOVE_AGE_MINUTES="$(get_env "MOVE_AGE_MINUTES" "8")"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

mkdir -p "$HOTWORDS_DIR"
mkdir -p "$HOTWORDS_DIR/sogou_newwords" "$HOTWORDS_DIR/douyin_hotwords" \
         "$HOTWORDS_DIR/weibo_hotsearch" "$HOTWORDS_DIR/douyin_hotlist" \
         "$HOTWORDS_DIR/xhs_hot" "$HOTWORDS_DIR/xhs_hotpost"

if [ ! -d "$STAGING_ROOT" ]; then
  log "WARN: 暂存目录不存在: $STAGING_ROOT，跳过"
  exit 0
fi

move_one() {
  local src="$1"
  local rel="${src#$STAGING_ROOT/}"
  local dst="$HOTWORDS_DIR/$rel"
  local dst_dir
  dst_dir="$(dirname "$dst")"
  mkdir -p "$dst_dir"

  # 以“覆盖”语义放到最终目录
  rsync -a --inplace "$src" "$dst"
  rm -f "$src"
}

log "开始移动（age>${MOVE_AGE_MINUTES}min）: $STAGING_ROOT -> $HOTWORDS_DIR"

MOVED_COUNT=0

# Douyin
if [ -d "$STAGING_ROOT/douyin_hotwords" ]; then
  while IFS= read -r -d '' f; do
    move_one "$f"
    MOVED_COUNT=$((MOVED_COUNT + 1))
  done < <(find "$STAGING_ROOT/douyin_hotwords" -type f -name "douyin_hotwords_????????_????.json" -mmin "+$MOVE_AGE_MINUTES" -print0 2>/dev/null || true)
fi

# Sogou
if [ -d "$STAGING_ROOT/sogou_newwords" ]; then
  while IFS= read -r -d '' f; do
    move_one "$f"
    MOVED_COUNT=$((MOVED_COUNT + 1))
  done < <(find "$STAGING_ROOT/sogou_newwords" -type f -name "sogou_????????_????.txt" -mmin "+$MOVE_AGE_MINUTES" -print0 2>/dev/null || true)
fi

# Weibo
if [ -d "$STAGING_ROOT/weibo_hotsearch" ]; then
  while IFS= read -r -d '' f; do
    move_one "$f"
    MOVED_COUNT=$((MOVED_COUNT + 1))
  done < <(find "$STAGING_ROOT/weibo_hotsearch" -type f -name "weibo_hotsearch_????????_????.txt" -mmin "+$MOVE_AGE_MINUTES" -print0 2>/dev/null || true)
fi

# Douyin Hotlist
if [ -d "$STAGING_ROOT/douyin_hotlist" ]; then
  while IFS= read -r -d '' f; do
    move_one "$f"
    MOVED_COUNT=$((MOVED_COUNT + 1))
  done < <(find "$STAGING_ROOT/douyin_hotlist" -type f -name "douyin_hotlist_????????_????.txt" -mmin "+$MOVE_AGE_MINUTES" -print0 2>/dev/null || true)
fi

# XHS
if [ -d "$STAGING_ROOT/xhs_hot" ]; then
  while IFS= read -r -d '' f; do
    move_one "$f"
    MOVED_COUNT=$((MOVED_COUNT + 1))
  done < <(find "$STAGING_ROOT/xhs_hot" -type f -name "xhs_hot_????????_????.txt" -mmin "+$MOVE_AGE_MINUTES" -print0 2>/dev/null || true)
fi

# XHS Hotpost
if [ -d "$STAGING_ROOT/xhs_hotpost" ]; then
  while IFS= read -r -d '' f; do
    move_one "$f"
    MOVED_COUNT=$((MOVED_COUNT + 1))
  done < <(find "$STAGING_ROOT/xhs_hotpost" -type f -name "xhs_hotpost_????????_????.json" -mmin "+$MOVE_AGE_MINUTES" -print0 2>/dev/null || true)
fi

# 清理空目录（不影响写入，因为只会清理空的）
find "$STAGING_ROOT" -type d -empty -delete 2>/dev/null || true

if [ "$MOVED_COUNT" -eq 0 ]; then
  log "INFO: 未发现可移动文件（可能文件尚在写入，或已被移动过）"
else
  log "移动完成，共移动 ${MOVED_COUNT} 个文件"
fi


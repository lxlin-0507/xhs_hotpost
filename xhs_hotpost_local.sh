#!/bin/bash
# 本地 Mac：从服务器拉取最新热帖结果到本地 output/
# 服务器已在 10:10 / 00:10 自动跑 noauth 模式，本脚本只做单向同步
# 每天 10:30 / 00:30 由 launchd 调用（比服务器跑完晚 20 分钟）

set -euo pipefail

PROJECT="/Users/a1/Desktop/oppo-webserver"
SSH_KEY="/tmp/deploy_key"          # cp "/Users/a1/Desktop/id_ed25519(2)" /tmp/deploy_key && chmod 600 /tmp/deploy_key
REMOTE="yange@82.156.76.22"
REMOTE_OUTPUT="/opt/oppo-webserver/output"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

TODAY=$(date +%Y%m%d)

# ── 从服务器拉热帖结果 ────────────────────────────────────
log "从服务器同步 xhs_hotpost/${TODAY}/ → 本地"
rsync -avz \
  -e "ssh -i ${SSH_KEY} -o StrictHostKeyChecking=no" \
  "${REMOTE}:${REMOTE_OUTPUT}/xhs_hotpost/${TODAY}/" \
  "${PROJECT}/output/xhs_hotpost/${TODAY}/"

# ── 从服务器拉 xhs_hot 热搜关键词 ────────────────────────
log "从服务器同步 xhs_hot/${TODAY}/ → 本地"
rsync -avz \
  -e "ssh -i ${SSH_KEY} -o StrictHostKeyChecking=no" \
  "${REMOTE}:${REMOTE_OUTPUT}/xhs_hot/${TODAY}/" \
  "${PROJECT}/output/xhs_hot/${TODAY}/"

log "同步完成"

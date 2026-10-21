#!/usr/bin/env bash
# 启动 llama-kvmem-server
#   64K 逻辑上下文 / 全 GPU / q4_0 KV / MTP 关闭（MTP 在第 2 个长请求会 OOM）
#   所有参数可用环境变量覆盖
set -uo pipefail
source "$(dirname "$0")/env.sh"

CTX="${CTX:-65536}"
KV_TYPE="${KV_TYPE:-q4_0}"
BUDGET="${BUDGET:-8192}"
RESERVE="${RESERVE:-2048}"
HOST_MIB="${HOST_MIB:-1024}"
DISK_MIB="${DISK_MIB:-0}"
SINK="${SINK:-1024}"
RECENT="${RECENT:-512}"
BATCH="${BATCH:-512}"
UBATCH="${UBATCH:-256}"
NGL="${NGL:-99}"
SESSIONS="${SESSIONS:-1}"
KVMEM_ON="${KVMEM_ON:-1}"
SPEC_TYPE="${SPEC_TYPE:-none}"
SPEC_KV_TYPE="${SPEC_KV_TYPE:-f16}"
MTP_STATE="${MTP_STATE:-snapshots}"
MTP_DRAFT="${MTP_DRAFT:-3}"
DISK_PATH="$PROJ/kvmem-cache"
mkdir -p "$PROJ/logs"

ARGS=(
  -m "$MODEL"
  --host "$HOST" --port "$PORT"
  -c "$CTX" -ngl "$NGL"
  -fa on -b "$BATCH" -ub "$UBATCH"
  --kv-dtype "$KV_TYPE"
  --spec-type "$SPEC_TYPE"
  --no-ui
  -lv 3
)
if [[ "$SPEC_TYPE" != "none" ]]; then
  ARGS+=( --spec-draft-n-max "$MTP_DRAFT" --spec-kv-dtype "$SPEC_KV_TYPE"
          --kvmem-mtp-state "$MTP_STATE" )
fi
if [[ "$KVMEM_ON" == "1" ]]; then
  ARGS+=( --kvmem --kvmem-budget "$BUDGET" --kvmem-gen-reserve "$RESERVE"
          --kvmem-host-mib "$HOST_MIB" --kvmem-sink-tokens "$SINK"
          --kvmem-recent-tokens "$RECENT" --kvmem-sessions "$SESSIONS" )
  if [[ "$DISK_MIB" != "0" ]]; then
    mkdir -p "$DISK_PATH"
    ARGS+=( --kvmem-disk-mib "$DISK_MIB" --kvmem-disk-path "$DISK_PATH" )
  fi
fi

LOG="$PROJ/logs/server-$(date +%Y%m%d-%H%M%S).log"
echo "starting: ${BIN}/llama-kvmem-server ${ARGS[*]}" | tee "$LOG"
"$BIN/llama-kvmem-server" "${ARGS[@]}" >>"$LOG" 2>&1 &
echo $! > "$PROJ/logs/server.pid"
echo "pid=$(cat "$PROJ/logs/server.pid") log=$LOG"

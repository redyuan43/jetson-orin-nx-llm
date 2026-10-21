#!/usr/bin/env bash
# 前台运行（systemd 用）：128K 上下文 / q4_0 KV / MTP sidecar / 单会话
set -uo pipefail
source "$(dirname "$0")/env.sh"
exec "$BIN/llama-kvmem-server" \
  -m "$MODEL" --host "${HOST}" --port "${PORT}" \
  -c "${CTX:-131072}" -ngl "${NGL:-99}" -fa on \
  -b "${BATCH:-512}" -ub "${UBATCH:-256}" \
  --kv-dtype "${KV_TYPE:-q4_0}" \
  --spec-type draft-mtp --spec-draft-model "$MTP_MODEL" \
  --spec-draft-n-max "${MTP_DRAFT:-3}" --spec-kv-dtype "${SPEC_KV_TYPE:-f16}" \
  --kvmem-mtp-state "${MTP_STATE:-snapshots}" \
  --no-ui -lv 3 \
  --kvmem --kvmem-budget "${BUDGET:-8192}" --kvmem-gen-reserve "${RESERVE:-2048}" \
  --kvmem-host-mib "${HOST_MIB:-3072}" --kvmem-sink-tokens "${SINK:-1024}" \
  --kvmem-recent-tokens "${RECENT:-512}" --kvmem-sessions "${SESSIONS:-1}"

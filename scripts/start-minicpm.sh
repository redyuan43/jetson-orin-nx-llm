#!/usr/bin/env bash
# MiniCPM-V 4.7 35B-A3B + LES (llama.cpp-omni) on Jetson Orin NX 16GB
#
# 详细说明见 docs/minicpm-les-notes.md。以下每个参数都有实测依据。
#
#   --moe-slot-bank 400   prefill 段槽位数（每层 base 的第一段，不是总缓存）。
#                        本机 -b 8 -ub 8，prefill 也走 decode 段，所以要小。
#   DECODE_WINDOW=224    每层解码缓存槽数。224 × 40 层 = 8960 槽 ≈ 11.3 GiB。
#                        256 虽然理论上限更高（每层专家正好 256 个），
#                        但 12.9 GiB 槽位 + 模型 + 计算缓冲会超 16 GiB，
#                        实测长生成时 OOM。224 是稳定上限。
#   WINDOW_MAX_ROWS=8    窗口覆盖的批宽上限。DSpark 验证批宽 = n_max+1 = 8；
#                        缺这一项，验证批会绕过缓存重读全部专家（314 GiB → 35 GiB）。
#   F_NOCACHE=1          每次读完立刻把专家段从 page cache 丢弃。
#                        12.89 GiB 专家文件会吃满 page cache → 内核 OOM。
#   PREWARM              启动预读热门专家（w256.txt，4774 个），消除冷启动。
#                        表文件在 models/MiniCPM-V-4.7/prewarm/，可随工程一起留存。
#   IO_THREADS=8         并发读盘线程。1 线程比 8 线程慢 4.3 t/s。
#   -b 8 -ub 8           小批。批越大，一次 prefill 触发的不同专家越多。
#   KV q8_0              KV cache 量化。8K 上下文下 f16 只占 0.156 GiB，不是瓶颈，
#                        但 q8_0 能给解码窗口腾出空间。
#
# DSpark 默认关闭以保留历史行为。2026-10-21 已确认实际 CLI 支持投机，
# 正确路径的接受率可达 82%；收益需按同任务实测，见 docs/06-nx5-audit-20261021.md。
# USE_DSPARK=1 可启用，但注意本脚本旧默认窗口的内存预算未包含 draft。
set -uo pipefail

PROJ="${PROJ:-$HOME/github/qwen38-kvmem}"
LES="$PROJ/minicpm-les/llama.cpp-omni"
MODELS="${MODELS:-$PROJ/models/MiniCPM-V-4.7}"
BIN="$LES/build-jetson/bin"

MODEL="${MODEL:-$MODELS/stripped.gguf}"
EXPERTS="${EXPERTS:-$MODELS/experts}"
DSPARK="${DSPARK:-$MODELS/dspark-Q4_K_M.gguf}"
MMPROJ="${MMPROJ:-$MODELS/mmproj-model-f16.gguf}"
PREWARM="${PREWARM:-$MODELS/prewarm/w256.txt}"

CTX="${CTX:-8192}"
BATCH="${BATCH:-8}"
UBATCH="${UBATCH:-8}"
NGL="${NGL:-99}"
BANK="${BANK:-400}"
WINDOW="${WINDOW:-224}"
BANK_CAP="${BANK_CAP:-300}"
IO_THREADS="${IO_THREADS:-8}"
WINDOW_MAX_ROWS="${WINDOW_MAX_ROWS:-8}"
F_NOCACHE="${F_NOCACHE:-1}"
KV_TYPE="${KV_TYPE:-q8_0}"
SPEC_N_MAX="${SPEC_N_MAX:-7}"
USE_DSPARK="${USE_DSPARK:-0}"
USE_MMPROJ="${USE_MMPROJ:-0}"
EXTRA="${EXTRA:-}"

export LD_LIBRARY_PATH="$BIN:${LD_LIBRARY_PATH:-}"
export LLAMA_MOE_PREWARM="$PREWARM"
export LLAMA_MOE_BANK_CAP="$BANK_CAP"
export LLAMA_MOE_DECODE_WINDOW="$WINDOW"
export LLAMA_MOE_IO_THREADS="$IO_THREADS"
export LLAMA_MOE_WINDOW_MAX_ROWS="$WINDOW_MAX_ROWS"
export LLAMA_MOE_F_NOCACHE="$F_NOCACHE"

ARGS=(
  -m "$MODEL"
  --moe-experts "$EXPERTS"
  --moe-slot-bank "$BANK"
  -ngl "$NGL" -fa on
  -c "$CTX" -b "$BATCH" -ub "$UBATCH"
  --cache-type-k "$KV_TYPE" --cache-type-v "$KV_TYPE"
  -lv "${LOGLEVEL:-1}"
)

if [[ "$USE_DSPARK" == "1" && -f "$DSPARK" ]]; then
  ARGS+=(--spec-type draft-dspark --spec-draft-model "$DSPARK" --spec-draft-n-max "$SPEC_N_MAX")
fi
if [[ "$USE_MMPROJ" == "1" && -f "$MMPROJ" ]]; then
  ARGS+=(--mmproj "$MMPROJ")
fi
# shellcheck disable=SC2206
[[ -n "$EXTRA" ]] && ARGS+=($EXTRA)

exec "$BIN/llama-cli" "${ARGS[@]}" "$@"

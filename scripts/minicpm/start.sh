#!/usr/bin/env bash
# Run MiniCPM-V 4.7 (35B-A3B) with LES expert streaming on Jetson Orin NX.
# Legacy launcher. For the tested, pinned 64K configuration use run-64k.py.
#
#   ./start.sh                          interactive chat
#   ./start.sh -p "hello" -n 256        one-shot prompt
#   ./start.sh -f prompt.txt -n 3000    prompt from a file
#   MCP_PRESET=long ./start.sh ...      64K context instead of 8K
#
# Everything is forwarded to llama-cli verbatim.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.sh
source "$HERE/env.sh"

"$HERE/prep-system.sh" || true

echo "[minicpm] preset=$MCP_PRESET ctx=$MCP_CTX window=$MCP_DECODE_WINDOW bank=$MCP_SLOT_BANK" >&2

exec "$MCP_BIN/llama-cli" \
    -m "$MCP_MODEL_DIR/stripped.gguf" \
    --moe-experts "$MCP_MODEL_DIR/experts" \
    --moe-slot-bank "$MCP_SLOT_BANK" \
    --cache-type-k q8_0 --cache-type-v q8_0 \
    -ngl 99 -fa on \
    -c "$MCP_CTX" -b "$MCP_BATCH" -ub "$MCP_UBATCH" \
    --no-display-prompt \
    "$@"

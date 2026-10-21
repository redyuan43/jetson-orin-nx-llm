#!/usr/bin/env bash
# MiniCPM-V 4.7 35B-A3B on Jetson Orin NX 16GB — tuned environment.
# Sourced by start.sh; safe to source from an interactive shell too.
#
# Historical presets. See docs/06-nx5-audit-20261021.md for corrected measurements.
# Current pinned 64K entry point: scripts/minicpm/run-64k.py (docs/08-nx5-64k-fixed-20261021.md).
#   fast (default) : 8K context,  window 224 -> 13.4-13.6 t/s on real HTML generation
#   long           : 64K context, window 96  -> 11.2 t/s, peak RSS 10.3 GB
# These defaults are retained for reproducibility; 64K is not restricted to window 96.

export MCP_PRESET="${MCP_PRESET:-fast}"

# --- paths -----------------------------------------------------------------
export MCP_BASE="$HOME/github/qwen38-kvmem/minicpm-les/llama.cpp-omni"
export MCP_MODEL_DIR="$HOME/github/qwen38-kvmem/models/MiniCPM-V-4.7"
export MCP_BIN="$MCP_BASE/build-jetson/bin"
export LD_LIBRARY_PATH="$MCP_BIN:${LD_LIBRARY_PATH:-}"

# --- LES / KVMem tuning ----------------------------------------------------
# --moe-slot-bank is the *prefill* segment only, NOT the total cache. Layout:
#     [0, prefill_cap)         capacity = --moe-slot-bank
#     [prefill_cap, +widths)   capacity = DECODE_WINDOW x layers
# With -ub 8 the worst-case expert union is 64 per layer. The audit also tests
# bank=64. Expert memory grows with bank + the SUM of the layer windows;
# old RSS tables are not a reliable measure of remaining physical memory.
export MCP_SLOT_BANK=400

# Parallel pread threads. 1 -> 10.2 t/s, 2 -> 12.9, 4 -> 14.4, 8 -> 14.5.
# Lowering concurrency to "reduce contention" is measurably worse.
export MCP_IO_THREADS=8

# Static hot-expert table captured with LLAMA_MOE_SWEEP. Removes the all-miss
# cold start; measured 12.4 -> 14.5 t/s.
export MCP_PREWARM="$HOME/github/qwen38-kvmem/minicpm/prewarm_top128.txt"

case "$MCP_PRESET" in
    fast)
        export MCP_DECODE_WINDOW=224
        export MCP_CTX=8192
        ;;
    long)
        # Legacy conservative preset. q8_0 KV grows by ~595 MiB from 8K to 64K.
        # The audit also successfully runs a 208-slot window at 64K capacity.
        export MCP_DECODE_WINDOW=96
        export MCP_CTX=65536
        ;;
    *)
        echo "MCP_PRESET must be 'fast' or 'long' (got '$MCP_PRESET')" >&2
        return 1 2>/dev/null || exit 1
        ;;
esac

# Source checks SLOT_BANK <= BANK_CAP * 40. This is not a total RAM limit;
# using the window value here is conservative, not a source requirement.
export MCP_BANK_CAP="$MCP_DECODE_WINDOW"

# Historical default. The audit tests 8 so ubatch=8 prefill and speculative
# verification both reuse the window. Rows=1 is not required without DSpark.
export MCP_WINDOW_MAX_ROWS=1

# --- batching --------------------------------------------------------------
# -b does not change peak RSS (llama.cpp sizes the logits buffer by need), and a
# larger batch makes prefill faster. -ub stays at 8: a bigger ubatch makes the
# per-step expert union larger, which costs I/O.
export MCP_BATCH=2048
export MCP_UBATCH=8

# --- apply LES env ---------------------------------------------------------
export LLAMA_MOE_SLOT_BANK="$MCP_SLOT_BANK"
export LLAMA_MOE_DECODE_WINDOW="$MCP_DECODE_WINDOW"
export LLAMA_MOE_WINDOW_MAX_ROWS="$MCP_WINDOW_MAX_ROWS"
export LLAMA_MOE_BANK_CAP="$MCP_BANK_CAP"
export LLAMA_MOE_IO_THREADS="$MCP_IO_THREADS"
if [ -f "$MCP_PREWARM" ]; then
    export LLAMA_MOE_PREWARM="$MCP_PREWARM"
fi

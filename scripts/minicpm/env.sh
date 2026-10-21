#!/usr/bin/env bash
# MiniCPM-V 4.7 35B-A3B on Jetson Orin NX 16GB — tuned environment.
# Sourced by start.sh; safe to source from an interactive shell too.
#
# Two presets, both verified on-device. Pick with MCP_PRESET.
#   fast (default) : 8K context,  window 224 -> 13.4-13.6 t/s on real HTML generation
#   long           : 64K context, window 96  -> 11.2 t/s, peak RSS 10.3 GB
# The trade is context length against cache hit rate: window 224 is what makes
# long real outputs fast (13.6 vs 9.7 t/s at window 96 on the *same* task).

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
# With -ub 8 one ubatch carries <= 8 rows, so the expert union per ubatch is
# <= 64 slots; 400 is generous and wastes almost nothing. Measured peak RSS:
# bank 6400 + window 128 -> 8.0 GB, but bank 3200 + window 160 -> 15.6 GB.
# bank is the dominant memory term, not the window.
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
        # 64K only costs +2.6 GB because just 10 of 40 layers hold KV
        # (q8_0 KV at 64K is ~0.68 GB). But the window has to shrink to fit.
        export MCP_DECODE_WINDOW=96
        export MCP_CTX=65536
        ;;
    *)
        echo "MCP_PRESET must be 'fast' or 'long' (got '$MCP_PRESET')" >&2
        return 1 2>/dev/null || exit 1
        ;;
esac

# Must be >= max(SLOT_BANK/40, DECODE_WINDOW) or the model refuses to load:
#   "MoE shared slot bank exceeds the bank=N-per-layer RAM budget".
export MCP_BANK_CAP="$MCP_DECODE_WINDOW"

# Must stay 1 without speculation. Raising it lets speculative verification
# batches (n_max+1 rows) use the window; measured 11.4 -> 5.8 t/s because each
# verification step then reads ~8x the experts.
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

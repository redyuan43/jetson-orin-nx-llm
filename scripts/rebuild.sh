#!/usr/bin/env bash
# 从零重建 KVMem llama.cpp（Jetson Orin NX / sm_87）
set -euo pipefail
ROOT="${1:-$HOME/github/qwen38-kvmem}"
SRC="$ROOT/src/kvmem"
JOBS="${JOBS:-6}"

mkdir -p "$ROOT/src"
if [[ ! -d "$SRC/.git" ]]; then
  git clone --recurse-submodules --depth 1 https://github.com/kvmem/kvmem.git "$SRC"
fi

# --- Jetson 适配：NVML 的单参数 DEPRECATED 宏会破坏 mtmd.h ---
python3 - "$SRC/kvmem/include/kvmem/monitor_hardware.hpp" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1])
s = p.read_text()
if 'push_macro("DEPRECATED")' in s:
    print("patch already applied"); raise SystemExit(0)
old = """#if __has_include(<nvml.h>)
#include <nvml.h>
#define KVMEM_MONITOR_NVML 1
#endif"""
new = """#if __has_include(<nvml.h>)
// nvml.h redefines DEPRECATED(ver); llama.h/mtmd.h use DEPRECATED(func, hint).
// Keep the surrounding macro intact so NVML's definition cannot leak into them.
#pragma push_macro("DEPRECATED")
#undef DEPRECATED
#include <nvml.h>
#pragma pop_macro("DEPRECATED")
#define KVMEM_MONITOR_NVML 1
#endif"""
assert old in s, "unexpected monitor_hardware.hpp layout"
p.write_text(s.replace(old, new))
print("applied Jetson nvml patch")
PY

cd "$SRC"
python3 scripts/prepare-backends.py --backend llamacpp
export PATH=/usr/local/cuda/bin:$PATH

cmake -S "$SRC" -B "$SRC/build-jetson" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc \
  -DCMAKE_CUDA_ARCHITECTURES=87 \
  -DGGML_CUDA=ON -DGGML_CUDA_FA_QUANTS=all \
  -DKVMEM_BUILD_LLAMA=ON -DLLAMA_KVMEM=ON -DLLAMA_KVMEM_ROOT="$SRC" \
  -DKVMEM_BUILD_SERVER_TESTS=OFF
cmake --build "$SRC/build-jetson" -j"$JOBS"
echo "built: $SRC/build-jetson/bin/llama-kvmem-server"

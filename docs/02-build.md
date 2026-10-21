# KVMem llama.cpp 在 Jetson Orin NX 上的自编译记录

日期：2026-10-19
上游：<https://github.com/kvmem/kvmem>（原 kvmem/kvmem-llama.cpp 已重定向）
版本：master @ `feb4742`（KVMem 0.18.0），llama.cpp pin `620d2225c`

## 1. 获取源码

```bash
cd ~/github/qwen38-kvmem/src
git clone --recurse-submodules --depth 1 https://github.com/kvmem/kvmem.git
cd kvmem
python3 scripts/prepare-backends.py --backend llamacpp
```

`prepare-backends.py` 把 `backends/patches/llamacpp-kvmem.patch` 打到 pinned llama.cpp 上，
就绪后输出 `llamacpp: 620d2225cc43 + llamacpp-kvmem.patch ready`。

`--check` 会一并校验 NInfer 子模块；本工程只用 llama.cpp，所以那次失败可忽略
（`ninfer: integration patch is missing or modified`）。

## 2. Jetson 适配补丁（必须）

**问题**：`kvmem/include/kvmem/monitor_hardware.hpp` 直接 `#include <nvml.h>`。
Jetson 的 CUDA 头文件目录里同时提供 `nvml.h`，它把 `DEPRECATED` 定义为**单参数宏**：

```c
#define DEPRECATED(ver) __attribute__((deprecated))
```

而 llama.cpp 的 `llama.h` / mtmd.h 用的是**双参数** `DEPRECATED(func, hint)`。
两者在 `llama-kvmem-server.cpp` 里同时可见，于是编译报：

```
mtmd.h:270:61: error: macro "DEPRECATED" passed 2 arguments, but takes just 1
```

**修复**：包裹 nvml.h 的 include，保存并恢复周围宏。

```c
#if __has_include(<nvml.h>)
// nvml.h redefines DEPRECATED(ver); llama.h/mtmd.h use DEPRECATED(func, hint).
// Keep the surrounding macro intact so NVML's definition cannot leak into them.
#pragma push_macro("DEPRECATED")
#undef DEPRECATED
#include <nvml.h>
#pragma pop_macro("DEPRECATED")
#define KVMEM_MONITOR_NVML 1
#endif
```

已直接修改源码树。**patch 已记录在下节脚本中**，重新 clone 后需重新应用。

## 3. 构建

```bash
cd ~/github/qwen38-kvmem/src/kvmem
export PATH=/usr/local/cuda/bin:$PATH
cmake -S . -B build-jetson -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc \
  -DCMAKE_CUDA_ARCHITECTURES=87 \
  -DGGML_CUDA=ON -DGGML_CUDA_FA_QUANTS=all \
  -DKVMEM_BUILD_LLAMA=ON -DLLAMA_KVMEM=ON -DLLAMA_KVMEM_ROOT=$PWD \
  -DKVMEM_BUILD_SERVER_TESTS=OFF
cmake --build build-jetson -j6
```

要点：

- `CMAKE_CUDA_ARCHITECTURES=87`（Orin = sm_87），**不要**用脚本默认的 `120a-real`；
- `GGML_CUDA_FA_QUANTS=all` 才能让 `-ctk/-ctv q4_0` 走 FlashAttention 量化路径；
- 编译约 **25 分钟**（521 个目标，8 核 A78AE + 单 GPU 板子）；
- 产物：`build-jetson/bin/llama-kvmem-server`（以及 `llama-kvmem-cli`、`llama-bench` 等）。

无 OpenSSL → `cpp-httplib` 的 HTTPS 关闭，不影响本工程（监听 127.0.0.1 / tailnet 内部）。

## 4. 复现脚本

```bash
# scripts/rebuild.sh —— 放到工程里，可一键重来
```

见仓库 `scripts/rebuild.sh`。

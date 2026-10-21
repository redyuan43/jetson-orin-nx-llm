# Jetson Orin NX 16GB 本地大模型部署实录

在 **Jetson Orin NX 16GB Super** 上用 llama.cpp 跑两个模型的完整过程：**坑、实测数据、以及为什么某些"优化"是负收益**。

所有数字都是本机实测，不是论文引用。凡未亲自验证的都会标注来源。

---

## 硬件与环境

| 项 | 值 |
|---|---|
| 机器 | Jetson Orin NX 16GB Super（p3768 + p3767-0000-super，tegra234）|
| 内存 | **15 597 MiB 统一内存**（GPU 与 CPU 共享同一块）|
| GPU | sm_87，1024 CUDA core，上限 **1173 MHz** |
| CPU | 8× Cortex-A78AE，上限 **1984 MHz** |
| 实测内存带宽 | **93–95 GB/s**（自定义 CUDA 读核，2 GiB，1024×256）|
| 系统 | Ubuntu 24.04.4 LTS，L4T R39.2.1，CUDA 13.2，driver 595.78 |
| 电源 | MAXN_SUPER |
| 系统目标 | `multi-user.target`（无图形界面）|

**统一内存是整个工程的约束来源**：15.6 GB 要同时装权重、KV cache、计算缓冲和操作系统。

---

# 第一部分：Qwen3.8-27B（dense + MTP）

## 结论

| 项 | 值 |
|---|---|
| 模型 | `Qwen3.8-27B-UD-IQ2_XXS.gguf`（6.77 GiB）|
| MTP | `mtp-Qwen3.8-27B-Q4_0.gguf`（1.28 GiB sidecar，draft=3）|
| 上下文 | **131072（128K）**，q4_0 KV |
| **decode** | **6.6–7.2 tok/s** |
| prefill | 105.8 tok/s |
| 进程 RSS | 9.96 GB |

## 架构

`qwen35` 架构，**dense**，27.32B 激活参数，65 块（64 + 1 MTP），
**48 层 GDN（线性注意力）+ 16 层全注意力**（`full_attention_interval=4`），
24 Q head / 4 KV head，key/value length 256。

## 关键发现：瓶颈是延迟，不是带宽

把模型从 11.29 GiB 换成 6.77 GiB（**小 40%**），decode 只从 4.20 涨到 4.23 t/s（**+5%**）。

如果受带宽限制，应该接近 +67%。所以瓶颈是**每 token 的固定串行延迟**：
GDN 是严格递归的，每 token 必须串行走完 64 层，无法并行摊薄。

**推论：在这个模型上做权重量化，节省的 I/O 几乎换不到速度。**

## 唯一有效的加速：MTP 投机解码

| 配置 | decode |
|---|---:|
| IQ3_S 11.29 GB，无 MTP | 4.20 tok/s |
| IQ2_XXS 6.77 GB，无 MTP | 4.23 tok/s |
| **IQ2_XXS + MTP draft=3** | **6.6–7.2 tok/s** |

MTP 一次前向验证多个草稿 token，把固定延迟摊薄 —— 这才是对症的药。

> 对比第二部分：**DSpark 在 I/O 受限的 MoE 模型上是负收益**。
> 同一个"投机解码"思路，在两个模型上一个有效一个有害，原因是瓶颈不同。

---

# 第二部分：MiniCPM-V 4.7 35B-A3B（MoE + LES）

## 架构（直接从 GGUF 元数据读的）

| 类别 | 参数 | 值 |
|---|---|---|
| 规模 | 总层数 | 40 |
| | 总参数 / **每 token 激活** | 35.5 B / **≈3 B** |
| 注意力 | **GDN 线性层** | **30** |
| | 全注意力层 | **10** |
| | 模式 | `GGGF` 循环（`full_attention_interval=4`）|
| | 头数 Q / KV | 16 / **2**（GQA 8:1），head_dim 256 |
| MoE | 每层专家数 / 激活数 | **256 / 8** + 1 shared |
| | 单专家 FFN 维 | 512 |
| GDN 状态 | conv_kernel / state_size / time_step_rank | 4 / 128 / 32 |

**只有 10 / 40 层存 KV cache**，所以长上下文非常便宜：
64K 上下文 q8_0 KV 只占 **0.68 GB**。

## 为什么专家必须放 SSD

| 项 | 大小 |
|---|---:|
| 非专家权重（`stripped.gguf`）| 1.55 GB |
| **专家权重（`experts/`，40 层 × 256 专家）**| **12.89 GB** |
| 统一内存池 | 15.6 GB |

12.89 GB 装不进去，所以 LES 把专家放 NVMe，用 `preadv` 按需读进内存槽位，
并维护一个每层的解码缓存窗口。

**每 token 要读 40 层 × 8 专家 = 320 个专家 ≈ 420 MB。**

## 推荐配置（本机最优，已验证）

```bash
LLAMA_MOE_DECODE_WINDOW=224      # 每层 224 槽（关键）
LLAMA_MOE_BANK_CAP=224
LLAMA_MOE_SLOT_BANK=400          # prefill 段，不要设大
LLAMA_MOE_IO_THREADS=8
LLAMA_MOE_WINDOW_MAX_ROWS=1
LLAMA_MOE_PREWARM=prewarm_top128.txt
llama-cli --moe-slot-bank 400 \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  -ngl 99 -fa on -c 8192 -b 2048 -ub 8
```

| 场景 | Prefill | **Generation** | 输出完整 |
|---|---:|---:|:---:|
| 生成 8 KB HTML+SVG 动画（n=3000）| 7.6 t/s | **13.6 t/s** | ✅ |
| 同上，`-b 8` | 6.4 t/s | 13.4 t/s | ✅ |
| 同上，window=96（旧配置）| 14.2 t/s | **9.7 t/s** | ✅ |
| 短输出（n=250，计数）| 5.4–6.3 | 7.2–8.6 | — |

**同一个任务，window 224 比 window 96 快 40 %（13.6 vs 9.7）。**

### 64K 长上下文预设

| 上下文 | window | 峰值 RSS | decode |
|---:|---:|---:|---:|
| 8 K | 224 | ~7.7 GB | **13.6 t/s** |
| 64 K | 96 | 10.3 GB | 11.2 t/s |

64K 只多花 2.6 GB（因为只有 10 层存 KV），代价是窗口必须缩到 96。
用 `MCP_PRESET=long` 切换。

### 结构上限

用源码内置的 `LLAMA_MOE_SKIP_READ=1`（完全不读盘）测得上限：
**15.2–15.3 t/s**。也就是说读盘之外还有约 65 ms/token 的结构开销。

| 模式 | Generation |
|---|---:|
| 完全不读盘（`SKIP_READ=1`）| **15.3** |
| 读盘但不写 GPU 缓冲（`DUMMY_READ=1`）| 13.5 |
| 正常 | 13.6（window 224）|

**13.6 已经达到结构上限的 89 %。**

## 五个必须打的补丁

全部在 `patches/0001-les-linux-support.patch`（5 文件，+188 / −6）。

### 1. `F_NOCACHE` 是 macOS 专有

上游用 `fcntl(fd, F_NOCACHE, 1)`。Darwin 的 fd 级常驻标志，Linux 没有等价物。

Linux 下用 `posix_fadvise(POSIX_FADV_DONTNEED)`，而且**只在 fd 打开时调一次不够**：
它只在那刻生效，之后读进来的页照样堆积。12.89 GB 专家会把 page cache 吃满，
与进程 pinned 槽位缓冲叠加后超过物理内存 → OOM。

> 但注意：**最终配置里刻意保留 page cache**（`LLAMA_MOE_F_NOCACHE=0`，默认）。
> 因为我们后来发现 5.5 GB 页缓存里存着专家数据，命中就是零延迟，比"干净内存"更快。

### 2. CUDA 没有 `alloc_only_buft`

专家张量内容由 `preadv` 运行时写入，要求**CPU 可写 + GPU 可读**。
Metal 提供专用的 `alloc_only_buft`，CUDA/CPU 没有。
回退到 `ggml_backend_dev_host_buffer_type()`（CUDA pinned 内存）。

### 3. `llama-cli` 从不创建 draft context

DFlash/DSpark 草稿模型**没有自己的 token embedding 和 lm_head**，
必须通过 `cparams.ctx_other` 借用目标模型的。
上游只在 `tools/server/server-context.cpp` 实现，`llama-cli` 走
`common_init_from_params` 完全没有这段。

报错 `dflash requires ctx_other to be set` 出现在**内存估算阶段**，
那条只是警告；真正的问题是后面没创建 draft context，DSpark 静默降级。

### 4. `--moe-slot-bank` 不是总缓存大小（最容易误解）

每层的物理 base 布局是：

```
[0, prefill_cap)          ← prefill 段，容量 = --moe-slot-bank
[prefill_cap, +各层宽度)  ← decode 段，容量 = DECODE_WINDOW × 层数
```

本机 `-ub 8` 时，每个 ubatch 只有 8 个 token，专家并集 ≤ 64 槽，
所以 `--moe-slot-bank` 给 400 就很宽裕，**给 6400 是纯浪费**。

实测（8K 上下文）：

| `--moe-slot-bank` | window | 峰值 RSS |
|---:|---:|---:|
| 6400 | 128 | 8.0 GB |
| 3200 | 160 | **15.6 GB（几乎占满）** |
| **400** | **224** | **~7.7 GB** |

**`bank` 是内存的主因，不是 window。** 把 bank 降下来，才有空间把 window 提到 224。

### 5. `LLAMA_MOE_BANK_CAP` 必须 ≥ max(bank/40, window)

否则加载直接失败：
`MoE shared slot bank exceeds the bank=N-per-layer RAM budget`。

## DSpark：在本机是**负收益**，默认关闭

`dspark-Q4_K_M.gguf`（`general.architecture = dflash`，62 张量，5 层，
`target_layers=[4,12,20,28,36]`，`block_size=15`）。

实测接受率（`llama-speculative-simple`，本 fork 唯一有投机循环的驱动）：

| 采样 | n_drafted | n_accept | 接受率 |
|---|---:|---:|---:|
| 默认 temp=0.8 | 616 | 33 | **5.36 %** |
| 贪心 temp=0 | — | 4 | **0.83 %** |

把验证批次放进解码窗口（`LLAMA_MOE_WINDOW_MAX_ROWS=8`）后：

| 配置 | Generation |
|---|---:|
| 不用 DSpark | **11.4** |
| DSpark draft=7 | **5.8** |

原因很直接：**本机瓶颈是 SSD 读盘，而 DSpark 是用计算换 I/O。**
验证批读的专家并集更大（8 行 × 8 = 64 个/层，而不是 8 个），
读取量反而增加；接受率又只有个位数，完全补不回来。

**规律输出**（数字列表、表格）时接受率高，DSpark 才可能正收益；
**自由文本**（写代码、写 HTML）时接受率低，必然负收益。

> 这解释了为什么它在 iPhone 上有效：那里闪存快、GPU 快，是计算受限。

## 三个驱动的现状（想用 DSpark 必须看）

| 驱动 | 状态 |
|---|---|
| `llama-cli`（`tools/main/main.cpp`）| **完全没有投机循环** —— 零处调用 `common_speculative_begin/draft/accept` |
| `llama-server` | `tools/server/server.cpp` 是**旧版 API**（用 `common_chat_syntax`、`speculative.n_max`，本分支已删），编不过，12+ 处错误 |
| `llama-speculative-simple` | 能跑，补上 `ctx_other` + 清 MoE 参数后可用，但接受率如上 |

**结论：这个 fork 里没有可用的投机解码驱动。** 真要用，得给 `main.cpp` 补循环，
或者把 `server.cpp` 移植到新 API。

## 真正有效的三个优化

| 手段 | 效果 |
|---|---|
| `LLAMA_MOE_PREWARM`（热专家表）| **12.4 → 14.5 t/s（+17 %）** |
| `LLAMA_MOE_IO_THREADS` 1 → 8 | **10.2 → 14.5 t/s** |
| decode window 96 → 224 | **9.7 → 13.6 t/s（真实任务）** |

### 预热表

`LLAMA_MOE_PREWARM=<file>`：每行一层，行内是专家 id。
启动时先读进解码窗口，消除首 token 的全 miss。

采集方法：`LLAMA_MOE_SWEEP=<file>` 记录每步每层真实路由，统计热度取 top-N。

**专家热度高度集中**：每层 256 个专家里常用的只有约 100 个
（layer0 top-32 覆盖 78.6 %，top-64 覆盖 100 %）。

### IO 线程数不能降到 1

直觉上"降低并发减少争抢"是错的：

| 线程 | Generation |
|---:|---:|
| **1（串行）** | **10.2** |
| 2 | 12.9 |
| 4 | 14.4 |
| 8 | 14.5 |

SSD 必须并发才能打满带宽。

---

# 第三部分：测量方法学（这一节最容易被忽略）

在统一内存 + SSD 流式的场景下，**同一个配置测出来能差一倍**。
以下五个因素控制不好，数据就毫无意义。

## 1. 输出长度决定测出来的速度

前约 50–100 个 token 全是冷 miss（`hit=0`），严重拉低平均值。

| 输出长度 | 测得 Generation |
|---:|---:|
| 250 token | **7.2–8.6 t/s** |
| 1200 token | **10.5 t/s** |
| 3000 token | **13.4–13.6 t/s** |

**报告速度必须同时报告输出长度。**

这条直接改变了我的结论：用 n=250 的短测试对比 window 96 和 224，
得到 "10.5 vs 11.2，差不多"；换成 n=3000 的真实任务，
变成 **"9.7 vs 13.6，差 40 %"**。短测试被冷启动主导，完全无法区分配置优劣。

## 2. 孤儿进程会静默毁掉一切

一个残留的 `llama-cli` 会**钉住整个专家 bank**（实测 **14.8 GB** / 15.6 GB 池）。
后果：后续所有运行 OOM，速度数字毫无意义。

现象：`free` 显示 `used 15044 MB, available 553 MB`，但 `ps` 里没有明显可疑项
（父进程已退出，进程被 init 收养）。

**所以 `prep-system.sh` 第一步就是 `pkill -9 -x llama-cli`。**

## 3. page cache 是有用的，不要清

跑之前 `echo 3 > drop_caches` 是**自残**：5.5 GB 页缓存里存着专家数据，
命中就是零延迟。清掉之后测量必然偏慢。

## 4. 不要在自己测的时候让别人也在这台机器上跑

开发过程中遇到过外部脚本循环（每轮开头 `pkill -9 -x llama-cli`），
把所有测量都变成了随机数。**测速前先确认机器是空的。**

## 5. 后台服务不是内存问题

实测：**所有进程的匿名内存加起来只有 53 MB。**

`free` 报的"已用 2.6 GB"里，约 2.2 GB 是 Jetson 内核 / CUDA 驱动预留，
关任何服务都回收不了。系统已经是最简：`gdm` 未运行、`docker` 未运行、
`multi-user.target`。

---

# 第四部分：系统配置

## 必须做的四件事

| 动作 | 原因（都是实测）|
|---|---|
| `swapoff -a` + `vm.swappiness=0` | 2 GB swapfile 曾被启用且占用 600 MB；几百 MB 被换出就把 90 ms 的 decode 变成秒级 |
| 停止并禁用 `jtop` | 每秒轮询 GPU/CPU 频率，扰动 governor 和任何计时 |
| `jetson_clocks` | 不做的话 CPU 空闲时掉到 **729 MHz**；锁到 1984 MHz CPU / 1173 MHz GPU |
| 杀残留 `llama-cli` | 见上文，单个孤儿占 14.8 GB |

已固化为 `systemd/minicpm-prep.service`，开机自启。

## 反向教训：这些做了没用

| 尝试 | 结果 |
|---|---|
| KV cache 量化（f16 → q8_0 → q4_0）| **无感**。64K 上下文 KV 只有 0.68 GB，不是瓶颈 |
| mmproj 移到 CPU（`--no-mmproj-offload`）| **无感**。1 GB 视觉编码器不是瓶颈 |
| 把 `--moe-slot-bank` 加到 6400 | **更慢**（白占 8 GB，window 反而被挤小）|
| 降到单线程读盘 | **更慢**（10.2 vs 14.5）|
| 用普通 CPU 内存替 pinned | **CUDA 崩溃** |
| 恢复 `llama-server` 目标 | 编不过（API 漂移）|
| 开 DSpark | **负收益**（13.6 → 更慢，接受率个位数）|

---

# 仓库内容

```
.
├── README.md                       ← 本文
├── docs/
│   ├── 01-environment.md           # 机器、工具链、CUDA 版本
│   ├── 02-build.md                 # KVMem 编译（含 Jetson 补丁）
│   ├── 03-benchmarks.md            # Qwen3.8 实测数据
│   ├── 04-service.md               # Qwen3.8 服务与参数
│   ├── 05-minicpm.md               # MiniCPM 部署与调优（英文）
│   └── minicpm-les-notes.md        # LES 调优笔记（另一轮独立实验）
├── patches/
│   └── 0001-les-linux-support.patch   # 让 llama.cpp-omni 在 Linux/Jetson 上跑起来
├── scripts/
│   ├── env.sh / start.sh / start-fg.sh / stop.sh / rebuild.sh   # Qwen3.8
│   ├── start-minicpm.sh            # MiniCPM 另一版启动脚本（window=224）
│   ├── bench_4k.py / bench_ctx.py / stability.py / mtpbench.py
│   └── minicpm/
│       ├── env.sh                  # MiniCPM 全部调优参数（fast / long 两套预设）
│       ├── prep-system.sh          # 系统优化（swap / jtop / clocks / 杀孤儿）
│       ├── start.sh                # 一条命令启动
│       └── prewarm_top128.txt      # 热专家预热表（40 层 × 1203 专家）
├── systemd/
│   └── minicpm-prep.service        # 开机自动做系统优化
└── samples/
    ├── pelican.html                # 模型生成的鹈鹕骑自行车 SVG 动画
    └── pelican.png                 # 渲染截图
```

## 复现步骤

```bash
# 1) 编译带补丁的 fork
git clone https://github.com/tc-mb/llama.cpp-omni
cd llama.cpp-omni && git checkout a64932e
git apply /path/to/patches/0001-les-linux-support.patch
cmake -S . -B build-jetson -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc \
  -DCMAKE_CUDA_ARCHITECTURES=87 -DGGML_CUDA=ON -DLLAMA_CURL=OFF
cmake --build build-jetson -j6 --target llama-cli

# 2) 把专家权重剥离成 experts/（40 层 × 256 专家）+ stripped.gguf
#    参考 docs/02-build.md

# 3) 系统优化
sudo cp systemd/minicpm-prep.service /etc/systemd/system/
sudo systemctl enable --now minicpm-prep.service

# 4) 跑
cd scripts/minicpm
./start.sh -p "hello" -n 256
MCP_PRESET=long ./start.sh -p "hello" -n 256   # 64K 上下文
```

## 样例输出

模型一次生成、未修改的 `samples/pelican.html`（6959 字节，`<!DOCTYPE html>` → `</svg>` 完整闭合）：

![pelican](samples/pelican.png)

---

## 已知限制

- **16 GiB 装不下 12.89 GiB 专家 + 1.55 GiB 模型 + KV + 计算缓冲**，必须流式读。
- decode 结构上限 **15.3 t/s**（完全不读盘时）；实测 **13.4–13.6 t/s**，已达上限的 89 %。
- 达到 15 t/s 需要全部专家常驻（12.9 GiB × 40 层槽位），16 GiB 机器做不到。
  换 32 GiB 的 AGX Orin 理论上可以，按结构上限推算约 20 t/s。
- 上下文与速度是取舍：8K + window 224 → 13.6 t/s；64K + window 96 → 11.2 t/s。
- 没有可用的 HTTP server（`llama-server` 编不过，`llama-omni-server` 要求
  omni 多模态模块且启动卡住）。
- DSpark 在本机负收益，默认关闭。

---

## 数据来源标注

- **本文主表**：本人受控实测（`pkill` 残留进程、`resource.getrusage` 读峰值内存）。
- `docs/minicpm-les-notes.md`：另一轮独立实验的笔记。它先发现了
  `bank=400 / window=224` 这个更优结构；本文已亲自复测并确认
  （n=3000 真实任务：13.4–13.6 t/s，输出完整）。两处数值差异主要来自输出长度
  与 page cache 状态，详见第三部分。

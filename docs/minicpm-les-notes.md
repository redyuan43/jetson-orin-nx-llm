# MiniCPM-V 4.7 35B-A3B + LES 在 nx5 上的部署与调优笔记

机器：Jetson Orin NX 16GB Super（16 GiB 统一内存，GPU 与 CPU 共享），Ubuntu 24.04，CUDA 13.2
引擎：`github.com/tc-mb/llama.cpp-omni` @ `a64932e`（含 LES + DSpark）
模型：`openbmb/MiniCPM-V-4.7-35B-A3B` 的 `mix_q3gud_im` 量化（14.44 GiB，混合精度 Q3_K 专家 + Q4_K_M 其余）

---

## 1. 架构

```
40 层 Transformer
├─ 注意力：30 层 GDN(线性) + 10 层全注意力，模式 GGGF 循环
│           （GDN 状态固定大小，不随上下文增长；只有 10 层存 KV cache）
└─ FFN：MoE，每层 256 个专家，每 token 激活 8 个（top-8）
         另有 1 个 shared expert
每 token 激活约 3B / 总 35.5B
```

| 类别 | 参数 | 值 |
|---|---|---|
| 规模 | 总参数 / 每 token 激活 | 35.5B / ≈3B |
| 注意力 | GDN 层 / 全注意力层 | 30 / 10 |
| | 头数 Q / KV | 16 / 2（GQA 8:1） |
| | head_dim | 256 |
| MoE | 每层专家数 / 激活数 | 256 / 8 |
| | 单专家 FFN 维 | 512 |
| KV | 8K 上下文 f16 占用 | 0.156 GiB（**不是瓶颈**） |

---

## 2. 为什么必须流式加载专家

权重 14.44 GiB > 统一内存 15.6 GiB 减去系统/计算缓冲/KV 的余量。
所以专家权重放 SSD（`experts/`，40 层 × 330 MiB = 12.89 GiB），
主模型只留非专家权重（`stripped.gguf`，1.55 GiB）。
运行时用 `preadv` 把用到的专家直接读进显存槽位。

**关键代价**：每 token 需要 40 层 × 8 专家 = 320 个专家 = 420 MB。
纯 SSD 顺序读约 2 GB/s，所以一切都取决于**缓存命中率**。

---

## 3. 三个必须改的地方（否则跑不起来）

### 3.1 `F_NOCACHE` 是 macOS 专有 → 改成每次读完丢弃

上游用 `fcntl(fd, F_NOCACHE, 1)`。`F_NOCACHE` 是 Darwin 内核的 **fd 级常驻标志**，
Linux 没有等价物，只能用 `posix_fadvise(POSIX_FADV_DONTNEED)`。

**关键**：只在 fd 打开时调用一次是不够的 —— `posix_fadvise` 只在调用那一刻生效，
之后读进来的页仍会堆积。必须**每次读完**对刚读的那一段调用一次。

否则 12.89 GiB 的专家文件会把 page cache 吃满，和进程自己的 pinned 槽位缓冲
（解码窗口 9.6 GiB）叠加后超过物理内存，触发内核 OOM。
本机实测 `oom_kill` 计数 9 → 修复后不再增长。

### 3.2 CUDA 没有 `alloc_only_buft`

专家张量内容由 `preadv` 运行时写入，不能落在 mmap 的文件映射区。
Metal 后端提供了专用的 `alloc_only_buft`，CUDA/CPU 没有。
改用 `ggml_backend_dev_host_buffer_type()`（CUDA 的 pinned 内存），
满足「CPU 可写 + GPU 可读」两个条件。

### 3.3 `llama-cli` 从不创建 draft context → DSpark 无法启用

DFlash/DSpark 草稿模型**没有自己的 token embedding 和 lm_head**，
必须通过 `cparams.ctx_other` 链接到目标模型借用。
上游只在 `tools/server/server-context.cpp` 里实现了这段，而 `llama-cli`
走 `common_init_from_params`，根本没有创建 draft context。
表现为报错 `dflash requires ctx_other to be set`（出现在内存估算阶段，是**警告**，
真正的问题是后面没有创建 draft context，于是 DSpark 静默降级）。

已在 `common/common.cpp` 补上与 server 路径一致的初始化。

---

## 4. 参数的含义（最容易误解的部分）

`--moe-slot-bank` **不是总缓存大小**。每层有一个物理 base 张量，布局是：

```
[0, prefill_cap)          ← prefill 段，容量 = --moe-slot-bank
[prefill_cap, +各层宽度)  ← decode 段，容量 = DECODE_WINDOW × 层数
```

`prefill_cap` 只在「批宽 > 解码窗口」时用得上。
本机 `-b 8 -ub 8`，每个 ubatch 只有 8 个 token ≤ 窗口覆盖的 8 行，
**所以 prefill 也走 decode 段**，`--moe-slot-bank` 那一段纯属浪费内存。
把它从 6400 降到 400，腾出的 7.7 GiB 正好用来把解码窗口从 128 提到 224：

| bank | window | 读取量 | Generation |
|---:|---:|---:|---:|
| 6400 | 128 | 76.9 GiB | 10.8 t/s |
| 1200 | 192 | 26.4 GiB | 12.3 t/s |
| 400 | 192 | 19.7 GiB | 13.8 t/s |
| 400 | 224 | 14.7 GiB | 14.3 t/s |
| 400 | 256 | 12.4 GiB | **14.6 t/s** |

窗口 256 时每层 256 个槽正好等于该层专家总数（256），理论上可常驻全部专家。
但 256 × 40 层 × 1.29 MiB ≈ 12.9 GiB 槽位内存，加上模型与计算缓冲后
16 GiB 机器放不下（实测 n=4000 长生成时 OOM）。224 是稳定上限。

### `LLAMA_MOE_WINDOW_MAX_ROWS`

窗口能覆盖的批宽上限。**DSpark 必需**：验证批宽 = `n_max + 1` = 8，
默认值为 1 时验证批会绕过解码缓存，把全部专家重读一遍。
实测：设 8 后读取量从 314 GiB 降到 35 GiB。

---

## 5. 预热表（prewarm）

`LLAMA_MOE_PREWARM=<file>`，每行一层，行内是专家 id。
启动时把这些专家读进解码窗口，消除首 token 的冷启动。

采集方法：`LLAMA_MOE_SWEEP=<file>` 记录每步每层的真实路由，
再统计热度取 top-N 生成预热表。

实测（10 个不同 prompt，5960 个采样步）：

| 表 | 专家数 | 每层覆盖 |
|---|---:|---|
| top64 | 2560 | 64 |
| top128 | 4274 | ~107 |
| top256 | 4774 | 80 ~ 214（中位 107） |

**热度高度集中**：每层 256 个专家里，常用的只有约 100 个。

---

## 6. DSpark 在本机是负收益

| 配置 | Prefill | Generation | 读取量 |
|---|---:|---:|---:|
| 无 DSpark（window 192） | 16.2 t/s | **13.8 t/s** | 19.7 GiB |
| DSpark draft=7（window 192） | 17.6 t/s | 12.7 t/s | 27.0 GiB |

原因很直接：本机瓶颈是 **SSD 读盘**，而 DSpark 的作用是**用计算换 I/O**
（一次前向验证多个 token）。在 I/O 受限的机器上这个交换是**反方向**的 ——
验证批要读的专家并集更大，读取量反而增加 37%。

DSpark 在 iPhone 上有效是因为那里是**计算受限**（A19 GPU 快，闪存也快）。

规律输出（数字列表等）时草稿接受率高，DSpark 才有正收益；
自由文本（写代码、写 HTML）时接受率低，收益为负。

**结论：本机默认关闭 DSpark**（`USE_DSPARK=0`）。

---

## 7. 已验证性能

正式脚本（window=224）连跑 3 次，无 OOM，输出完整：

| 场景 | Prefill | Generation | 完整 HTML |
|---|---:|---:|:---:|
| 鹈鹕 HTML（n=3000）run1 | 20.0 t/s | 14.2 t/s | 是 |
| 鹈鹕 HTML（n=3000）run2 | 20.0 t/s | 14.0 t/s | 否（n 截断） |
| 鹈鹕 HTML（n=3000）run3 | 19.9 t/s | 14.2 t/s | 是 |
| 鹈鹕 HTML（n=3500） | **21.4 t/s** | **14.4 t/s** | 是 |
| 短输出基准（计数，规律输出） | 15.2 t/s | 15.2 t/s | — |
| **结构上限**（跳过读盘，SKIP_READ=1） | 29.1 t/s | **15.2 t/s** | — |

稳定值：**Generation 14.0–14.4 t/s，Prefill ~20 t/s**。

结构上限 15.2 t/s 是「完全不读专家」时的速度。
实测 14.4–14.6 已经达到该上限的 **95%**，剩余差距是每次请求都要重填
约 9600 次专家读取（≈ 全部 10240 个专家各一次）。

常驻进程（多轮请求）不会更快：每轮结束时的 `park` 会清空解码窗口，
下一轮又要重填（实测 13.2 → 12.2 → 11.9 t/s）。

---

## 8. 已知限制

- **16 GiB 装不下 14.44 GiB 权重**，专家必须流式读。这是硬件限制。
- `llama-cli` 没有 HTTP server 模式（上游移除了 `llama-server` 目标；
  `server.cpp` 有 `main()` 但与本分支的 `common/` API 已漂移，12+ 处编译错误）。
  需要 HTTP 服务得自建或改用 `llama-omni-server`（后者要求 omni 多模块）。
- 解码缓存命中上限约 81%：Router 选的专家在 256 个槽里近似随机，
  扩大窗口无法突破（实测 32→128 槽，读盘量降 34%，速度不变）。
- 上下文实测 8K 稳定。KV cache 只占 0.156 GiB，**不是瓶颈**，
  受限于预填充阶段的专家读取量。
- 达到 15+ t/s 需要能常驻全部专家（12.9 GiB 槽位 + 1.55 GiB 模型），
  16 GiB 机器装不下。理论上换 32 GiB 的 AGX Orin 可全驻留，
  按结构上限推算约 20 t/s。
- 生成的 HTML 是否完整取决于输出长度：模型自认为写完时输出 `</html>`，
  达到 `-n` 上限被截断则不完整。n=3000 时 3 次里 2 次完整。
- 本机 `llama-cli` 无 HTTP server 模式，服务需自建（见上一节）。

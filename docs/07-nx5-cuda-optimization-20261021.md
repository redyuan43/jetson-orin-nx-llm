# nx5：8K 上下文的 CUDA / DSpark 提速实测

后续已按用户选择固定为 64K 配置，并通过实际约 8K 输入检索验收。
当前入口与验收范围见 [64K 固定配置](08-nx5-64k-fixed-20261021.md)；本页保留 8K 优化过程。

本轮在 `ssh nx5` 串行测试，依据用户要求把主对照固定为 8192 上下文容量。
引擎仍是 `a64932e` 加原有本地修改；没有覆盖原始源码或生产动态库。
完整输入、命令、环境、stdout、stderr、内存采样和结果 JSON 在
[实验归档](../reports/2026-10-21-nx5-optimization/)。

## 找到并修复的执行路径问题

`-ngl 99` 并没有让原路径中的 SSD 专家矩阵乘法在 GPU 上运行。
调度器调试输出显示 40 层的专家 `MUL_MAT_ID` 在 CPU 上执行：专家权重是 host buffer，
CUDA 的默认 `GGML_OP_OFFLOAD_MIN_BATCH=32` 又大于本实验的 ubatch=8。
仅把阈值改成 1，能把专家计算送入 GPU，但会触发选中专家上传，实测反而更慢。

[补丁 0003](../patches/0003-cuda-les-mapped-experts.patch) 增加一个仅供 LES 使用、显式开启的
CUDA 映射分配器。CPU 直接把 SSD 专家读入 `cudaHostAllocMapped` 内存，GPU 直接访问同一地址。
缓冲区按 CUDA 权重参与调度，因此专家计算可以留在 GPU。

该补丁没有把全局 `integrated` 标志改成 true，也没有让所有 CUDA Host buffer 自动获准直接计算。
它只在 `GGML_CUDA_LES_MAPPED=1` 且设备为集成 GPU、支持主机映射与统一寻址时启用，
并检查 CPU/GPU 地址一致。没有修改模型权重、量化格式、router 算法和每 token 激活 8 个专家的规则。
原有 bank/window 越界修复（补丁 0002）仍然必需。

## 正确性证据与范围

- [数值检查程序](../scripts/minicpm/check-cuda-mapped.cpp) 对比普通 CUDA device buffer 与 CPU 写入的映射 buffer。
  使用实际 Q3_K 专家矩阵尺寸 2048→512、512→2048，8 个激活专家，batch=1/2/4/8，
  每组连续改写权重和专家 ID 三次。共 921600 个输出数值，全部有限，最大绝对误差和归一化 RMSE 均为 0。
- 完整模型的 512-token 贪心输出，映射路径与普通 GPU 上传路径逐字一致。
  CPU 计算与 GPU 计算的生成内容会出现差异；不能声称不同计算后端逐 token 等价。
- 1024-token 贪心对照中，draft7 的 window192/200/208、重复运行及关闭 Graph 的文本逐字一致。
  但映射路径的 draft0 与 draft7 在第 1569 个字符之后分叉；本轮没有比较每一步的 logits，
  不能证明不同批次形状或投机路径的数值等价，也不将“投机输出无损”作为本轮已验证结论。
  [输出哈希与比较记录](../reports/2026-10-21-nx5-optimization/output-comparisons.json) 保存了这些差异。
- 这些检查排除了已测形状和运行中的明显映射错误，不是模型全部任务质量或量化无损的证明。
  保留全部生成文本，不用人工修改生成结果来冒充模型能力。

## 固定配置与测量方法

主矩阵使用英文 SVG 基准提示词 108 token、最多输出 1024 token、贪心采样、seed42，
ctx8192 / window208 / bank64 / rows8 / batch2048 / ubatch8 / q8_0 KV / 8 个 I/O 线程。
保留原来的 1203 专家预热表和 Linux page cache，不清缓存，不并行推理。
不同后端/投机批次可能产生不同文本；这些是固定输入和输出预算的请求级对比，不是固定 token 回放。

`decode token/s` 来自引擎最终统计；`wall_s` 包括进程加载、预热、prefill、生成和退出。
偶发停顿全部计入，没有手工扣除。最低可用内存使用整机 `MemAvailable`，不能用 RSS 代替。
后段实验新增了 `/proc/vmstat` 采样，用于检查内存回收；没有新增 OOM kill。

## 8K 参数对照

以下是各单次运行的最终统计，不是多次平均：

| 专家计算路径 | 窗口 | DSpark 草稿 | decode token/s | 进程总秒数 | 最低可用 MiB |
|---|---:|---:|---:|---:|---:|
| 原 CPU 路径 | 208 | 关闭 | 12.80 | 94.66 | 1760 |
| CUDA 映射 | 208 | 关闭 | 13.79 | 94.17 | 1709 |
| CUDA 映射 | 208 | 2 | 15.69 | 93.66 | 1030 |
| CUDA 映射 | 208 | 3 | 14.33 | 87.14 | 903 |
| CUDA 映射 | 208 | 7 | 18.09 | 73.15 | 794 |
| CUDA 映射 | 192 | 7 | 14.27 | 85.68 | 1626 |
| CUDA 映射 | 200 | 7 | 17.94 | 71.65 | 1138 |
| CUDA 映射，重复运行 | 200 | 7 | 17.90 | 77.67 | 1155 |
| CUDA 映射，关闭 CUDA Graph | 200 | 7 | 16.00 | 79.17 | 1204 |

window208/draft7 相对相同窗口的原路径，decode 提高 41.3%，进程总耗时减少 22.7%。
单独启用映射的 decode 虽有提升，总耗时这一轮几乎没变，所以不能将 decode 增益等同于整次请求增益。
draft7 的接受率 814/1461 = 55.7%，低于 draft2 的 82.5%，但最终吞吐更高。
三个 draft7 窗口的接受数量完全相同。

本轮采用 window200 作为较均衡的候选：相比 208，理论上少占 412.5 MiB 专家缓冲，
短测速度接近。window192 增加余量，但这轮生成初期仍有停顿，不能仅按窗口大小归因速度差异。
window200 复测为 17.90 token/s，decode 接近前次 17.94，但 prefill 从 5.03 秒增长到 10.85 秒；
端到端仍有波动，不能将两次相近的 decode 速率推广为所有请求稳定在 18 token/s。
8K 容量并不自动消除所有内存压力，vmstat 仍记录了部分直接回收；不能称为完全不受内存影响的测试。
关闭 CUDA Graph 的单次对照更慢，本轮保留默认 CUDA Graph；这次读盘耗时也更长，
不能将差异全部归因于 Graph 本身。

## 中文原题的完整验证

原样使用用户的中文提示词，没有给模型提供其他项目、参考图或手工编写的 SVG。
使用官方 instruct 模板、关闭 reasoning，temperature=0.7 / top_p=0.95 / top_k=100 /
min_p=0 / repetition_penalty=1.03 / seed42，window200、draft7、映射专家，最大输出预算 6144。

| 指标 | 实测 |
|---|---:|
| 输入 / 输出 token | 38 / 2315 |
| prefill | 2.725 秒 |
| decode | 139.860 秒 / **16.55 token/s** |
| 包含加载与退出的总时间 | **152.318 秒，约 2 分 32 秒** |
| 最低整机可用内存 | **1162 MiB** |
| 草稿接受率 | 1808 / 3542 = 51.0% |
| 退出码 / 内存保护 | 0 / 未触发 |

模型自然结束，未耗尽 token 预算。原始输出中的完整 HTML 围栏机械提取为
[pelican.html](../reports/2026-10-21-nx5-optimization/pelican-fast/pelican.html)，5561 字节，
没有修改任何 HTML 字节。SHA256 为 `9e6b5a65bc40a515efe718d13c699043f2568a5cc575e72d80b128b700ecfc0a`。
[命令与指标](../reports/2026-10-21-nx5-optimization/pelican-fast-8k.json)、
[浏览器检查](../reports/2026-10-21-nx5-optimization/pelican-fast/browser-check.json)、
[质量判断](../reports/2026-10-21-nx5-optimization/pelican-fast/assessment.json) 已保留。

**性能目标达到，但本次视觉任务仍未通过。** Chrome 实际运行有 1 个 SVG、7 个 CSS 动画，
无 JS 异常、无外部请求，两帧不同。700ms 截图中车轮离开了车架：
`.wheel` 使用 `transform-origin:center`，缺少 `transform-box:fill-box` 或明确的单个车轮坐标，
旋转参考了 SVG 视口。腿和简化踏板没有蹬踏动画。
不能把动画在运行等同于画面正确，也不能凭一个随机样本把质量变化归因于投机或 CUDA 映射。

![700ms，保留模型错误](../reports/2026-10-21-nx5-optimization/pelican-fast/frame-700.png)

此前 64K/CPU/instruct 的原题是 2529 token、13.71 token/s；本次 8K/CUDA/DSpark 的输出更短且内容不同，
所以两次端到端耗时变化同时受输出长度和速度影响。固定 1024-token 预算的比较应看上表。

## 已排除的方向

切换主矩阵到 8K 前的 64K/512-token 探索结果：

| 路径 | window / 预取槽 | decode token/s |
|---|---:|---:|
| 原 CPU 专家路径 | 208 / 0 | 12.56 |
| CPU + 跨层预取 | 200 / 8 | 11.25 |
| 强制专家上传 GPU | 208 / 0 | 10.24 |
| 映射专家直接在 GPU 计算 | 208 / 0 | 13.32 |

预取对照维持相同专家槽总预算，且两次输出逐字一致；本次实现的预取不是正收益。
DSpark 验证一般是多 token 图，现有跨层预测只在单 token 时创建，不能假设两者简单叠加。
64K/window200/mapped/draft2 触发内存保护，该次不能算有效性能结果。
缩到 window184 后完成 512 token，12.93 token/s；这不属于固定窗口的因果对照。

Nsight 的 CPU 路径样本显示大量 `cudaStreamSynchronize`，但它包含等待 GPU 完成的时间，
不能把 API 总时间直接当成可删除的纯同步开销。原路径的专家本来在 CPU 上，
也不能把激活专家大小乘 token 数，当成实际 CUDA 上传量。

## 构建与回退

nx5 的审计目录为 `/home/nx/github/qwen38-kvmem/minicpm-audit-20261021`。
已构建的映射库在 `optimization/cuda-mapped-v2`，其中 `libllama.so.0` 链接到已验证的补丁 0002 库。
每个构建目录的 `build.json` 记录源文件、补丁和库的 SHA256 以及编译/链接命令。

要在本次已配置的引擎构建上复现：

```bash
python3 scripts/minicpm/build-window-fix.py --output patched-lib-new
python3 scripts/minicpm/build-cuda-mapped.py \
  --base-library-dir patched-lib-new --output optimization/cuda-mapped-new
```

输出目录必须是新目录。构建脚本复用现有 Ninja 参数与其余 object，适用于本次 nx5 构建，
不是跨版本安装器。取消 `--mapped-experts` 并把 `--library-dir` 指向 `patched-lib` 即回到 CPU 专家路径。
旧启动脚本继续保留，不会在实验启动时全局杀其他推理进程。

已提供 [run-fast-8k.py](../scripts/minicpm/run-fast-8k.py) 固定上述均衡参数，
真实原题验证就是通过这个入口完成的。在 nx5 审计目录执行：

```bash
python3 scripts/minicpm/run-fast-8k.py samples/pelican-user-prompt-20261021.txt
```

也可传入任意 UTF-8 提示词文件，使用 `--tag`、`--output`、`--tokens`、`--seed` 调整归档或生成预算。
默认结果保存到 `logs/fast-8k/`，原始文本为 `.out`，性能和命令为 `.json`。
它复用测量脚本的互斥与内存保护，已有推理任务时拒绝启动，不会杀掉别人的任务。
该入口是一次请求的运行脚本；本轮没有发布 HTTP 或常驻服务，也没有验证满 8K 历史。

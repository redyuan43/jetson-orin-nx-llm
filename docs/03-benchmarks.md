# 基准数据与结论（最终版）

机器：Jetson Orin NX 16GB Super（nx5）
部署：kvmem 0.18.0 源码自编译（llama.cpp `620d2225c` + KVMem 补丁，sm_87）
日期：2026-10-19

---

## 1. 最终生产配置

| 项 | 值 |
|---|---|
| 主模型 | `Qwen3.8-27B-UD-IQ2_XXS.gguf`（6.77 GiB，unsloth Dynamic） |
| MTP | `MTP/mtp-Qwen3.8-27B-Q4_0.gguf`（1.28 GiB，sidecar） |
| 视觉 | `mmproj-BF16.gguf`（已下载，暂未启用） |
| 上下文 | **131072（128K）** |
| KV | q4_0（主）/ f16（MTP 草稿） |
| MTP 草稿数 | 3 |
| GPU 工作集 | budget=8192 / reserve=2048 |
| 主机 KV 配额 | 3072 MiB |
| KVMem | sessions=1，无 SSD 分层 |
| 运行方式 | systemd user unit `qwen38-kvmem.service` |

### 实测性能

| 指标 | 值 |
|---|---:|
| **decode** | **7.22 tok/s** |
| prefill | 105.75 tok/s |
| TTFT（4K prompt） | 38.6 s |
| 进程 RSS | 9.96 GB |
| 连续 4 次 4K 请求 | **4/4 通过** |

### 128K 上下文验证

| 测试 | 结果 |
|---|---|
| 128K 配置启动 | ✅ 成功（RSS 9.96 GB） |
| 90K prompt 请求 | ✅ 完成（89,950 tokens，prefill 105 t/s，decode 6.75 t/s） |
| 4K 基准（128K 配置下） | ✅ decode 6.64 t/s |
| 连续 3 次 4K 请求 | ✅ 3/3 通过 |

对比：IQ3_S 时期同样 64K 配置在 48K prompt 就 OOM；换 IQ2_XXS 后 90K prompt 顺利通过。

---

## 2. 优化路径（全部实测）

| 阶段 | 模型 | MTP | decode | 稳定性 |
|---|---|---|---:|---|
| 初始 | IQ3_S 11.29 GB | 无 | 4.01 t/s | 4/4 |
| 换小模型 | UD-IQ2_XXS 6.77 GB | 无 | 4.23 t/s | 4/4 |
| **+MTP** | **UD-IQ2_XXS 6.77 GB** | **draft=3** | **7.22 t/s** | **4/4** |
| **128K 配置** | UD-IQ2_XXS | draft=3 | 6.64–6.98 t/s | 3/3 |

**总提升 80%**。

---

## 3. 关键发现：瓶颈不是带宽

这是本次排查中最重要的结论，推翻了最初的判断。

模型体积缩小 **40%**（11.29 → 6.77 GiB），decode 只从 4.20 涨到 4.43 t/s（**+5%**）。

如果瓶颈是内存带宽，速度应该接近线性提升（约 +67%）。实测没有，说明：

```
decode 时间 ≈ 固定延迟 + 带宽传输
```

逐 token 串行执行时，**每 token 的 kernel 启动开销与 GDN 状态更新延迟**占了主导。
Qwen3.8 是混合架构（48 层 GDN 线性注意力 + 16 层全注意力），GDN 是严格的
递归依赖，每 token 必须串行走完全部 64 层，无法并行。

### 佐证

| 实验 | 结果 | 说明 |
|---|---|---|
| 模型小 40% | +5% | 非带宽瓶颈 |
| `-t 2/4/8` | 全 4.20 | 非 CPU 瓶颈 |
| `GR3D_FREQ` | 99% | GPU 已跑满 |
| 强制 MMQ / cuBLAS | 全 4.20 | 非内核选择问题 |
| 关闭 CUDA graph | 4.34（略降） | graph 已生效 |
| pp512 | 116 t/s | 批量处理不受影响，印证串行假设 |

### 为什么 MTP 有效

MTP 一次前向验证多个草稿 token，**把「固定延迟」摊薄到多个 token 上**：

```
无 MTP：  N token × (延迟 + 带宽)
有 MTP：  N/(1+接受数) × (延迟 + 带宽)  →  等效吞吐提升
```

实测 draft=3 时提升约 70%，与理论一致。

---

## 4. 硬件带宽基线

自写 CUDA 只读 kernel（2 GiB，1024 blocks × 256 threads）：

| 轮次 | 耗时 | 带宽 |
|---|---:|---:|
| 1 | 23.05 ms | 93.2 GB/s |
| 2 | 23.01 ms | 93.3 GB/s |
| 3 | 22.69 ms | 94.7 GB/s |

带宽充足，不是限制因素。

---

## 5. 上下文与内存

| 项 | 占用 |
|---|---:|
| 主权重（GPU） | 6.76 GiB |
| MTP sidecar | 1.28 GiB |
| 进程 RSS 合计 | 9.96 GB |
| 系统可用余量 | ~4.9 GB |

64K 上下文在 q4_0 KV 下需约 1.12 GiB，余量充足。

**128K 需要 2.25 GiB KV**，理论上现在也放得下，但尚未实测（见待办）。

---

## 6. 换模型过程中的坑

1. **`nextn_predict_layers=0`**：unsloth 的模型把 MTP 拆成独立 sidecar，
   必须用 `--spec-draft-model` 传入，且 `--spec-type draft-mtp` 才会生效。
2. **MTP sidecar 是完整 GGUF**（含 tokenizer，1.28 GB），不是小的 head 文件。
3. **KVMem 的 MTP 只支持 `snapshots` 模式**；`replay` 会报
   `GDN replay requires single-sequence GPU Qwen 27B...`。
4. **`--kvmem-disk-mib 0` 时主机 KV 预算检查被跳过**（源码 `check_payload_budget` 首行 return），
   `--kvmem-sessions 1` 时会话淘汰也被跳过。内存安全实际靠 `budget`/`reserve` 硬约束。

---

## 7. 复现命令

```bash
cd ~/github/qwen38-kvmem

# 4K 基准
python3 scripts/bench_ctx.py --tokens 4096 --max-tokens 128 --rounds 3 --n-ctx 65536

# 稳定性（连续 N 次 4K）
python3 scripts/stability.py 8400 4 4096

# 服务管理
systemctl --user start|stop|status qwen38-kvmem.service
```
# nano5：在 Orin Nano 8GB 上部署 MiniCPM-V 4.7

2026-10-21，已在 nano5 上运行与 nx5 相同的量化权重、专家拆分文件和 CUDA 映射补丁。
8K 短基准最快 **6.69 token/s**，64K 默认配置 **5.63 token/s**。
这证明 8GB 设备可以依靠更小的专家缓存运行该模型，但本轮没有证明长历史性能、完整视觉能力或长期服务稳定性。

## 设备与业务处理

| 项目 | 实机结果 |
|---|---|
| 设备 | NVIDIA Jetson Orin Nano Engineering Reference Developer Kit Super |
| 统一内存 | MemTotal 7,727,248 KiB，约 7.37 GiB；CPU/GPU 共用 |
| CPU / GPU | 6 核 ARM64；测试时 GPU 1020 MHz，MAXN_SUPER |
| 系统 | Ubuntu 24.04.4，L4T R39.2.1，CUDA 13.2.86 |
| SSD | aigo NVMe SSD DP35 256GB |
| Swap | 无；本轮没有添加 |
| 部署目录 | `/home/nano/minicpm-v47` |

MiniCPM 已与原有 `siyuan-image-comfy.service` 纳入同一中央资源 `nano5-igpu` 的互斥模式。
原业务现场确认为 **ComfyUI SDXL Q8_CR**，不是 LTX。部署和测试完成后已切回 ComfyUI。
新 HTTP 控制服务常驻，实际模型按任务加载；ComfyUI 仅监听本机，保留原有正式网关的 SSH 隧道。
启动另一个模式前确认队列排空并停止前一个模式；正在执行的任务不会被抢占。

旧 SSH 别名解析到已经离线的 Tailscale 节点。本轮使用：

```sh
ssh -o Hostname=192.168.100.204 nano5
# 当前在线的 Tailscale 名称也可用：
ssh -o Hostname=nano5-1.taild500c8.ts.net nano5
```

## 权重、内存与 SSD

所有 10,240 个路由专家原本就以完整文件保存在 SSD。此次调整的是**内存缓存容量**，
没有删除专家，没有降低 top-k，也没有改重量化精度。每个 token 仍在每层 256 个专家中选择 8 个。
未缓存的专家由 SSD 按需读入，再通过映射缓冲交给 GPU 计算。

| 部分 | nano5 8K | nano5 64K 默认 | nx5 64K 参考 |
|---|---:|---:|---:|
| SSD 上完整路由专家 | 12.89 GiB | 12.89 GiB | 12.89 GiB |
| 每层专家窗口 | 56 / 256 | 48 / 256 | 176 / 256 |
| 40 层窗口 + 64 槽共享 bank | **2.900 GiB** | **2.498 GiB** | **8.943 GiB** |
| 主模型 KV，q8_0 | 85 MiB | 680 MiB | 680 MiB |
| DSpark | draft=3 | 关闭 | draft=7 |
| DSpark KV，f16 | 80 MiB | 不加载 | 640 MiB |

专家缓冲计算：`(40 × window + 64) × 1,351,680 bytes`。bank 的 64 槽是全局共享，不能再乘 40。
主模型非专家权重约 1575 MiB；此外还有计算缓冲、GDN 状态、驱动和系统内存，
不能只相加上表就推断峰值。Linux 页缓存也可能保留可回收的专家页，因此窗口比例不等于缓存命中率。
与 nx5 相比，nano5 两个配置分别少分配约 **6.04 / 6.45 GiB** 专家缓冲。

复制了权重、运行库、预热表等 55 个文件，约 14.79 GiB；与 nx5 源文件逐一核对 SHA-256，全部一致。
源机器使用临时的受限传输服务，传输完成后已关闭并删除。
校验结果见 [transfer-verification.json](../reports/2026-10-21-nano5-deployment/transfer-verification.json)。

## 性能对照

同一份 `samples/pelican-prompt.txt`，模板后输入 108 token，输出预算 512 token，greedy、seed42、reasoning off。
所有测试串行执行，8 个读盘线程、6 个 CPU 计算线程、GPU 映射专家缓存、bank64、batch2048、主 KV q8_0。
没有强制清理 Linux 页缓存，每组只跑一次；小幅差距不能视为显著改善。
输出是 HTML 片段，**512 token 截断的速度基准不是完整鹈鹕动画质量验收**。

| tag | 上下文 | window | ubatch / rows | draft | Prefill token/s | Decode token/s | 最低可用内存 MiB | 端到端秒 |
|---|---:|---:|---|---:|---:|---:|---:|---:|
| nano-8k-w48-d0 | 8K | 48 | 8 / 4 | 0 | 7.93 | 5.57 | 1886.1 | 130.31 |
| nano-8k-w64-d0 | 8K | 64 | 8 / 8 | 0 | 12.21 | 6.14 | 1051.3 | 103.75 |
| **nano-8k-w56-d3-u4** | **8K** | **56** | **4 / 4** | **3** | **9.62** | **6.69** | **746.2** | **97.25** |
| **nano-64k-w48-d0-u4** | **64K** | **48** | **4 / 4** | **0** | **10.00** | **5.63** | **1261.6** | **109.78** |
| nano-64k-w56-d0-u4 | 64K | 56 | 4 / 4 | 0 | 10.00 | 5.71 | 832.7 | 109.27 |

上述五组均 rc=0，没有触发 512 MiB 内存保护或超时，OOM 计数没有增加。
8K/draft3 接受 355 / 467 个草稿 token，接受率 **76.02%**。
这一组同时改变了窗口和微批大小，不能把全部速度差归因于 DSpark。

64K/window56 仅比 window48 快约 1.4%，最低可用内存却少约 429 MiB，故默认选 48。
8K 的最快配置余量较小，保留 512 MiB 保护，不保证所有提示、输出长度和后台负载都可达到本轮余量。
内存余量采用系统 `MemAvailable`，不是进程 RSS，也不是独立显存。

直接读取 SSD 的辅助测试：8 线程、256 次随机读、每次 1.289 MiB、合计 330 MiB，
O_DIRECT 绕过页缓存，测得约 **1147 MiB/s**。这是一次短合成测试，不是整个模型的有效吞吐。
小窗口下逐层读盘与计算仍有串行等待，因此不能直接把 SSD 标称带宽换算成 token/s。
日志的 `pread` 毫秒包含多个线程累计时间，也不能当作端到端耗时。

原始输出、完整参数、物理读盘字节与每 0.5 秒的内存采样保存在
[validation](../reports/2026-10-21-nano5-deployment/validation/)。

## 正式入口中文问答

使用下面固定入口和 instruct 采样，各运行一次
[nano5-qa-prompt.txt](../samples/nano5-qa-prompt.txt)。输入 215 token，输出预算 512 token，
两次均提前正常结束，没有截断或内存保护退出：

| 正式配置 | 实际输出 token | Decode token/s | 最低可用内存 MiB | 端到端秒 | DSpark 接受率 |
|---|---:|---:|---:|---:|---:|
| long-64k | 148 | 6.30 | 1313.0 | 54.15 | — |
| fast-8k | 145 | 5.74 | 746.7 | 55.65 | 45.16% |

人工检查两份回答：都解释了 SSD 按需加载、稀疏激活和缓存的作用，说明缩小缓存没有删除专家，
也说明了更多读盘的速度代价。题目已给定这些事实，因此这是入口和基本回答检查，不是知识或复杂推理评测。
回答原文见 [64K](../reports/2026-10-21-nano5-deployment/validation/nano-64k-instruct-qa.answer.txt) / [8K](../reports/2026-10-21-nano5-deployment/validation/nano-8k-instruct-qa.answer.txt)。

DSpark 的收益随任务和采样改变；`fast-8k` 只是本轮代码短测中最快的参数组，不保证每条请求都快。
中文问答没有显示它优于默认 64K 配置。当前证据支持预期约 **5.6–6.7 token/s**，不是 15 token/s。

前七次参数筛选的汇总见 [summary.json](../reports/2026-10-21-nano5-deployment/summary.json)。
该目录的 `final-state.json` 是早期参数筛选结束时的快照，后续服务验收已恢复 ComfyUI。
最终业务回执见 [service-acceptance.json](../reports/2026-10-21-nano5-service/service-acceptance.json)。

## 完整鹈鹕与照片结果

- 原题完整 HTML：2730 输出 token，7.29 token/s，端到端 **390.42 秒（6分30秒）**。
  HTML 完整结束，但浏览器中鸟体错位、轮子缺失；CSS 动画 transform 覆盖 SVG 定位变换，**视觉验收不通过**。
  保留[模型原始 HTML](../reports/2026-10-21-nano5-service/pelican/pelican.html)和[截图](../reports/2026-10-21-nano5-service/pelican/frame-700.png)，没有人工修补冒充模型结果。
- 用户原图实际为 **1280×1707（约218万像素）**。原图直接输入，无旋转、缩放或裁切。
  视觉 profile 为8K、window40、bank64、ubatch4、draft0，使用原 mmproj f16。
- 识别文字输出400 token：**157.40 秒**，其中图像及提示处理 **97.81 秒**，文字生成 **49.31 秒**，其余加载等开销约10.27秒。
  “模型已加载”时同任务约 **147秒**仅为扣除开销的估算，**没有常驻实测**。用户已明确不再继续测速。
- OCR 回答有重复、分类错误及疑似根据常识补写；不能把这些文字当作可靠逐字识别。
  [原始回答](../reports/2026-10-21-nano5-service/ocr-answer-w40.txt)。window32与40的回答逐字一致。
- 已完成的 HTTP 单图接口验收使用简短问题，仅返回“机械键盘”，端到端99.13秒；
  它证明图片链路可调用，不是400-token OCR任务的耗时或质量替代数据。

## HTTP、互斥与舰队登记

入口 `http://nano5-1.taild500c8.ts.net:18347`，服务专用 Bearer 认证；
`POST /v1/jobs` 提交，`GET` 返回的 `status_url` 查询。
这是异步单任务接口，不是 OpenAI Chat Completions。完整协议见 [API.md](nano5-service/API.md)。

`nano5-minicpm.service` 已启用。固定运行发布为 `1a51ae63257c745b1f56cde5d0648fcb60564199`；
服务代码 SHA-256 为 `13eaa434d7c03194b8b6ccf36aac25771025b2a0e23d0a4983d5228a3a63d109`。
模式选择、实际推理与既有图片业务共用中央预约；`held` 且本机模式就绪后才执行。
完成任务后 MiniCPM 进程退出并释放内存。模式 ready 不表示模型常驻。

实际完成 ComfyUI → MiniCPM文本 → MiniCPM图片 → ComfyUI：

| 检查 | 结果 |
|---|---|
| 文本任务 | succeeded，61.53秒（含模式切换、预约） |
| 图片任务 | succeeded，99.13秒 |
| 任务中切换模式 | 409拒绝；不抢占 |
| 同request_id同内容 | 返回原任务，不重跑 |
| 同request_id不同内容 | 409拒绝 |
| 未授权查询 | 401拒绝 |
| 切回ComfyUI | succeeded，30.29秒，ready、active=0 |

新的控制器替代旧的只读设备观察服务，防止两个观察者上报互相冲突的模式。
保留了原 ComfyUI 安装与模型，没有关闭其他设备业务；中央已有的无关租约也未被清理。
图谱发布 `e5afcc9`（tag `nano5-minicpm-20261021-r1`），42项相关测试通过；
手机宽度无横向溢出、浏览器无脚本错误，观测过期后不再显示旧模式为就绪。
部署只修改5个文件，保留了现网无关设备配置；已核对部署文件SHA与候选一致。
互斥服务8项单元测试通过，3个实际验收任务的中央租约均已释放。
图谱同步四个入口：
[能力页](http://nx6.taild500c8.ts.net:8300/capabilities)、
[调用手册](http://nx6.taild500c8.ts.net:8300/agent/nano5-minicpm.md)、
[机器目录](http://nx6.taild500c8.ts.net:8300/agent/catalog.json)、
[API契约](http://nx6.taild500c8.ts.net:8300/agent/nano5-minicpm/openapi.json)。

## 可重复使用的运行入口

参数保存在 [minicpm-v47-nano5.json](../configs/minicpm-v47-nano5.json)，
启动器为 [run-nano5.py](../scripts/minicpm/run-nano5.py)。正式服务从 `releases/1a51ae63257c745b1f56cde5d0648fcb60564199/` 固定目录启动；`deployment/` 是实验副本。
启动时校验 CLI、两个补丁运行库和聊天模板 SHA-256；没有每次重读 14 GiB 权重。
正式配置使用 instruct 采样：temperature0.7、top_p0.95、top_k100、repeat_penalty1.03、seed42、reasoning off。

下列 CLI 仅供管理员诊断，必须事先通过中央资源预约取得独占权并确认 ComfyUI 已停止；业务调用应使用上面的 HTTP 服务：

```sh
cd /home/nano/minicpm-v47/deployment

# 默认 64K / window48 / 关闭 DSpark
python3 scripts/minicpm/run-nano5.py samples/nano5-qa-prompt.txt --tokens 512

# 8K / window56 / draft3，当前测试中最快
python3 scripts/minicpm/run-nano5.py samples/nano5-qa-prompt.txt --profile fast-8k --tokens 512

# 换成自己的 UTF-8 提示文件；默认最多生成 6144 token
python3 scripts/minicpm/run-nano5.py /path/to/prompt.txt --profile fast-8k
```

启动器打印输出路径；`.out` 是模型原始回答，`.err` 是引擎日志，`.json` 是汇总，`.memory.jsonl` 是采样。
运行入口保持单进程互斥，发现已有 llama 推理进程则拒绝启动；不杀其他任务。
可用内存低于 512 MiB 或超过 2400 秒时只终止自己启动的推理进程。
`bench-audit.py` 增加了 `--ubatch`，默认仍为 8，不改变 nx5 原有配置。

切换模式请通过 HTTP `action=select_mode` 的原任务查询至终态，避免绕过中央预约直接执行 systemctl。

## 验证边界

- 权重及视觉 projector 已迁移并校验；文本和单图接口已验收，不支持多图或视频输入。
- 64K 已分配并完成短输入生成；尚未填入64K或实际8K长文本，不能承诺长历史同样的速度与内存余量。
- HTTP 控制面常驻，但模型每次请求重新加载；没有实现模型常驻多轮会话。
- 没有高精度模型质量对照。文件校验只能证明迁移没有改变权重；调用成功不代表HTML视觉或OCR正确。

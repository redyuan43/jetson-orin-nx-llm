# nx5 与 nano5：相同参数、开启思考的鹈鹕生成对照

2026-10-21，按用户要求在 nx5 实际复跑 nano5 的带思考样本。结果：nx5 更快，生成的 HTML 与 nano5 **逐字节相同**，画面和交互错误也相同。

原题：`创建一个HTML，内容是SVG绘制一个鹈鹕骑自行车的2D动画，不许参考任何本机其他项目直接自己搞`。
模型只收到这段原题，没有输入旧 HTML、其他项目或额外 SVG 实现提示。

## 参数与运行条件

- ctx8192、window56、bank64、ubatch4、rows4、draft3、IO线程8、CPU线程6。
- reasoning=on；temperature0.7、top_p0.95、top_k100、min_p0、repeat_penalty1.03、repeat_last_n=-1、seed42。
- 最大输出6144 token；KV q8_0；mapped experts；CUDA Graph开启；prefetch0；cache RAM64MiB；ctx checkpoints2。
- 模型和 DSpark 权重沿用之前从 nx5 校验复制到 nano5 的同一组文件。本轮重新核对 CLI、libllama、libggml-cuda、模板及预热表的 SHA256，一致。
- 参数自动比较排除了机器路径和文件名，其余参数无差异。见 [comparison.json](comparison.json)、[prewarm-check.json](prewarm-check.json)、[运行回执](lease-proof.json)。
- nx5 为 Orin NX 16GB，MAXN_SUPER；采样时 CPU 1.984GHz、GPU 1.173GHz、EMC 3.199GHz。见 [hardware.json](hardware.json)。
- 这是每台机器各一次的实际运行；物理内存大小与操作系统文件缓存状态不同，没有进行清缓存或统一缓存状态的实验。

## 测量结果

| 指标 | nano5 | nx5 |
|---|---:|---:|
| 端到端时间，推理进程含加载 | 555.48秒（9分15秒） | 291.21秒（4分51秒） |
| 解码速度 | 6.76 token/s | 13.14 token/s |
| 输出token，含思考和说明 | 3653 | 3653 |
| 输入token | 36 | 36 |
| DSpark接受率 | 75.03% | 75.03% |
| 最低 MemAvailable | 671.88MiB | 8810.35MiB |
| 采样的进程物理读盘量 | 389.39GiB | 41.75GiB |
| OOM计数增量 | 0 | 0 |
| 正常结束、未触及输出上限 | 是 | 是 |
| 画面与交互验收 | 未通过 | 未通过 |

nx5 解码速度为 nano5 的1.94倍，端到端时间缩短约47.6%。物理读盘量是 `/proc/PID/io` 的 `read_bytes` 采样峰值，
不是模型大小，也不是 LES 逻辑读取总量。nx5 的更大内存允许更多文件页留在缓存中；读盘差异与这一机制一致。
本次没有隔离 GPU、存储和缓存各自的贡献，不能把全部提速归因于某一个因素。

本次有意沿用 nano5 的8K/window56参数；nx5 原有64K默认配置没有修改。

## 原始结果与浏览器验证

[pelican.html](pelican.html) 是从 [原始stdout](nx5-pelican-thinking.out) 中原样提取的连续字节，没有修改任何生成代码。
[提取校验](extraction.json)记录 SHA256：
`210f4f6cd4691429d1d4049bfd63c17a16fc23b0669af65151c6bda385e876ee`，与 nano5 相同。
原始输出含实际的 Start thinking / End thinking 标记，模型确实开启了思考。

Chrome 实测，禁止外部网络请求；截图和 [浏览器记录](browser-check.json)均来自原始 HTML：

- 车轮外圈与轮轴分离，旋转时跑到空中。
- 鸟头分组的 CSS transform 覆盖 SVG translate，头部偏出场景。
- JavaScript 报 `Missing } in template expression`，整段脚本未成功解析。
- 点击暂停后8项 CSS 动画仍运行；播放及速度控制也没有有效事件处理。
- 无外部请求；桌面与移动宽度均保存截图。

![nx5 原始结果，约700ms](frame-700.png)

这一次逐字节相同的结果没有显示 nano5 因为内存较小或专家放在SSD而额外损失生成质量。
它也不能证明当前量化、模板和采样对所有任务都没有质量损失；两台机器共享同一套模型和推理实现。
单次同种子对照不能评价模型整体能力。

## 资源互斥与恢复

开始前确认 nx5 无 llama/Comfy 推理进程，原 `siyuan-fleet-control`、Comfy、Ornith 用户服务均为 inactive、MainPID=0。
原控制器原本就已停止，因此本实验通过它的既有中央控制凭据临时上报人工CLI生命周期，并以 `nx5-text` 身份预约
同一 `nx5-igpu` 独占资源，在协调器已有通用 `llm` 模式下运行。没有启动或替换原 Ornith HTTP 服务。
确认中央租约为held后才启动测试，运行期间保持租约及心跳。
结束后确认无残留推理进程，将组标记为off/unknown（保留自动调度不可用状态），释放同一个租约。
三项原服务保持inactive，没有修改其配置或默认参数。原始操作回执见 [lease-proof.json](lease-proof.json)。

复核工具：[原样提取与参数对照](analyze.py)、[浏览器检查](inspect-html.cjs)、[测量脚本](bench-audit.py)。
[run-thinking.py](run-thinking.py)是本次一次性管理员操作记录；已有receipt时拒绝重跑，不应作为服务启动器使用。

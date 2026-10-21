# 鹈鹕 SVG 对照测试规范

用户于2026-10-21要求后续鹈鹕测试遵循所提供仓库的口径。
参考：[bbinwang/pelican-bicycle](https://github.com/bbinwang/pelican-bicycle)，
本次固定版本 `b14acbb2ebe9d9be5f0dec3d35d8f4ae6fcaa432`。
这是该仓库的一套非正式对照方案，不是所有鹈鹕评测共用的行业标准。

## 固定任务

每次使用新对话，只发送以下原题：

```text
Generate an SVG of a pelican riding a bicycle.
```

默认关闭思考。不能额外要求 HTML、2D动画、按钮、配色、实现方法，不能提供旧结果、示例或自定义系统提示。
静态 SVG 是合格的输出形式；是否包含动画不能作为未按题目完成的依据。
模型原始响应、提取出的 SVG、参数和浏览器截图均保留；只提取代码块，不修复代码。

## Qwen3.6-35B-A3B UD-IQ4_XS 参考参数

参考[完整启动命令](https://github.com/bbinwang/pelican-bicycle/blob/b14acbb2ebe9d9be5f0dec3d35d8f4ae6fcaa432/configs/Qwen3.6-35B-A3B-UD-IQ4_XS.config)：

| 项目 | 参考配置 |
|---|---|
| 权重 | Unsloth Qwen3.6-35B-A3B-**MTP**-GGUF，UD-IQ4_XS |
| 思考 | reasoning off、reasoning-budget 0、enable_thinking false、preserve_thinking false |
| 采样 | temperature0.7、top_p0.8、top_k20、min_p0、presence_penalty1.5、repeat_penalty1.0 |
| 投机 | draft-mtp，最大草稿长度2 |
| KV | K/V均为q8_0 |
| 上下文 | 200000 |
| batch / ubatch | 384 / 384 |
| Flash Attention | 完整命令为auto；README摘要写on，实测须记录实际后端状态 |
| 执行端 | Windows llama-server，单GPU，全层GPU卸载 |
| 种子、输出预算 | 仓库没有显式固定；本机必须明确记录 |

参考命令未显式设置 `repeat-last-n`。本机所用 llama.cpp 的默认值是64，
因此 Qwen 对照明确记录64，不能沿用此前 MiniCPM 自定义实验的全上下文惩罚窗口（-1）。
参考仓库未固定 llama.cpp 提交，无法证明其所有隐含默认值与本机相同。

参数按所比较的模型选择，不能把 Qwen 的采样参数自动当成所有模型的官方推荐。

## nx5 适配和报告要求

nx5 的 CPU/GPU 共用16GB内存，需要SSD专家按需加载，并采用小微批。
本轮先以8K容量运行；所有与参考不同的设置必须出现在参数差异记录中。
没有匹配 MTP、权重版本、上下文或执行端时，不称为“完整同条件复现”。
原仓库没有固定种子，因此本机固定seed42也不保证复现仓库的同一张图。

记录加载和总耗时、生成速度、输出长度、退出状态、是否截断、最低可用内存、OOM计数及资源租约释放状态。
检查 SVG 是否可解析，以及轮子、车架、鸟的喙/喉囊、头身连接、脚与踏板等几何关系。
单次样本不能推断模型整体能力；若做多种子测试，保留全部结果，不能只展示最漂亮的一次。

之前使用中文“创建一个HTML……2D动画”的结果，以及用户额外要求的思考模式对照，均保留为**自定义实验**。
它们不能直接与本仓库的英文 SVG 样本进行同条件质量排名。

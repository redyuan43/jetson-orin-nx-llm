# nx5 MiniCPM 常驻与工具上下文复用

2026-10-22 将 nx5 API 改为仅提供 `minicpm-64k`。第一次请求加载模型，此后保持模型、SSD专家窗口、单槽KV缓存和循环状态检查点；空闲不自动退出。Qwen文件与历史实验保留，API不再受理Qwen，Octop和OpenWebUI的nx5连接只展示MiniCPM。

普通聊天和本次验收均开启思考。默认64K、window176、bank64、ubatch8、DSpark draft7、GPU映射专家缓存保持不变。常驻入口关闭额外的RAM prompt备份池（cache-ram=0），直接保留一个活动槽位，并允许最多2份循环状态检查点。

## 实测

使用 Octop 的同一任务：“请必须调用 current_time 工具获取当前时间，然后只回答工具返回的日期和时区。”确实执行工具，结果均为 `2026-10-22 CST (UTC+0800)`；每次包含两轮模型调用。

| 情况 | 完整任务耗时 |
|---|---:|
| 旧版，每轮卸载模型和上下文 | 601.822 秒 |
| 新版，首次加载、首次处理长输入 | 309.890 秒 |
| 新版，模型和公共前缀已缓存 | **22.864 秒** |

这是同一验收任务的端到端实测，约快26.3倍；不是生成TPS提升26倍，也不是所有任务的固定延迟。旧版首轮7979输入token，新版8058，运行环境提示略有变化，不能视作逐token相同的性能基准。

新版本首次任务的工具返回轮有8150输入token，缓存命中8046，仅实际预处理104个：预处理2.878秒、生成7.290秒，模型调用10.319秒；旧版相应预处理281.852秒。下一次热任务的两轮同样命中8046个token，首轮仅处理12个，第二轮104个；第二轮预处理波动至11.456秒，全部任务仍在22.864秒内完成。常驻后生成速度仍受SSD、专家缓存和输出内容影响。

四轮真实模型调用使用同一个PID 1049739，`cold_start`仅首轮为true，之后`model_loaded_after=true`且中央租约保持held。首个任务最低MemAvailable约861 MiB，无swap，内存守卫下限768 MiB。更长的实际输入仍需继续评估内存和时间；此轮没有填满64K。

## 正确性与生命周期

- 修改最后一条问题，两次分别要求输出不同口令，均正确返回 `ORANGE_739`、`VIOLET_826`；每次复用7666个token，分别只处理42/43个新token，没有沿用旧答案。
- 生成时另一请求返回429，卸载请求返回409；模型PID不变。
- 空闲卸载后旧PID退出，中央原租约查询确认released；下一请求创建新PID并重新持有租约。
- 重载的短算术冒烟输出了`40`，但全部落在reasoning_content，最终content为空。这项只证明重新加载及生成完成，不算正常最终答案质量通过；完整工具任务另行验证正常最终答案。
- 重启不重放未知任务；未确认释放的租约会阻止再次加载。测试模拟了未确认租约、停止时重载、旧守卫误关新进程等情况。
- 发布验收过程中先修复了“已确认空闲却上报unknown”和“预约摘要不等于中央租约摘要”两处适配错误；两次均未启动模型，核对原操作后取消预约或释放原租约，失败记录保留。

## 调用与缓存边界

入口不变：`http://nx5.taild500c8.ts.net:18347/v1`，专用Bearer令牌，模型名`minicpm-64k`。

客户端仍应发送完整历史，追加assistant的tool_calls、reasoning_content及tool结果。服务按模板渲染后的token精确匹配公共前缀，并恢复适当的循环状态检查点。新token仍需预处理；系统提示、工具定义或历史前段改变可能导致部分或全部重新预处理。单个槽位不保证多个交替会话都保留缓存。

- `usage.prompt_tokens_details.cached_tokens`：命中的输入token。
- `timings.prompt_tokens_processed`、`prefill_s`：实际预处理量和耗时。
- `/healthz`：`model_loaded`、`resident_pid`、`lease_status`显示实际驻留状态。
- 认证 `POST /v1/model/unload`：只在空闲时卸载并释放租约；停止服务也执行退出和释放。

模型常驻期间继续独占nx5 GPU，其他入口不能同时加载模型。当前API仅支持文本和函数工具调用；工具仍由Octop执行。

## 版本与证据

- 原生适配器：`nx5-chat-runtime/v6/llama-nx5-chat`，构建摘要见[build.log](../reports/2026-10-22-minicpm-resident/build.log)。
- 模型服务发布：`nx5-minicpm-resident-20261022-r3`，源码`322926fce8116719b1395408e996fc03dfa07655`。
- 舰队发布：`nx5-resident-fleet-20261022-r1`，fleet-resource-graph源码`9c6e43a`；只修改nx5条目和对应名称/说明，保留现网其他差异。
- [冷任务](../reports/2026-10-22-minicpm-resident/octop-cold-task.json)、[热任务](../reports/2026-10-22-minicpm-resident/octop-warm-task.json)、[调用指标](../reports/2026-10-22-minicpm-resident/job-metrics.json)、[缓存隔离及生命周期](../reports/2026-10-22-minicpm-resident/isolation-lifecycle.json)。
- 35项Python测试、27项舰队门户测试通过。8300/8301契约均为current且与服务版本指纹一致；WebUI仅MiniCPM，桌面/手机页面无脚本错误和横向溢出。

[Octop](http://bus002.taild500c8.ts.net:8088) · [OpenWebUI](http://nx6.taild500c8.ts.net:18348) · [舰队](http://nx6.taild500c8.ts.net:8300/capabilities) · [调用手册](nx5-service/API.md)

最终卸载复核后再次完成真实Octop工具任务，正确输出日期和时区。最终PID 1118525 保持驻留，active=0、中央租约held，工具返回轮继续命中 8046 个token；见[最终状态](../reports/2026-10-22-minicpm-resident/final-state.json)及[最终预热任务](../reports/2026-10-22-minicpm-resident/octop-final-prewarm.json)。

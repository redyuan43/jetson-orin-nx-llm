# nx5 常驻 MiniCPM 调用手册

入口：`http://nx5.taild500c8.ts.net:18347`（Tailscale，100.118.44.27）。
认证：`Authorization: Bearer <NX5_SSD_LLM_TOKEN>`，这是本服务专用令牌，
由nx5管理员从`~/.config/nx5-ssd-llm/client.key`安全提供给获准客户端。
不把令牌粘贴到提示词、图谱或任务日志；不能复用其他舰队服务的凭据。

## 模型选择

| profile | 模型 | 上下文总容量 | 本机固定参数 |
|---|---|---:|---|
| `minicpm-64k` | MiniCPM-V4.7 35B-A3B | 65536 token | SSD window176、DSpark draft7 |

MiniCPM支持思考开关，保留全部专家，SSD只改变存储与缓存方式。
上下文是输入与输出共用容量；MiniCPM已测试容量与8218-token实际输入，尚未测满各自上下文。
长文预处理另需时间，不能把生成速度当输入处理速度。

## OpenWebUI / OpenAI Chat Completions

在 OpenWebUI 的管理员设置 → Connections → OpenAI API 添加连接：

- Base URL：`http://nx5.taild500c8.ts.net:18347/v1`
- API Key：本服务的专用 Bearer 令牌（不是 OpenAI 的云端 Key）。
- Models：连接检测后自动发现 `minicpm-64k`。
- OpenWebUI 后端/容器必须能访问这个 Tailscale 地址。只让浏览器加入 Tailnet 不够。
- 使用普通文本聊天和 streaming；关闭图片输入，以及自动标题、标签、后续问题等后台生成任务，避免和聊天争用单个 GPU 槽位。
- 冷启动与长输入会增加等待时间；保持流式连接，客户端总超时建议至少 45 分钟。等待时每5秒发送空的SSE `delta:{}` 心跳，让SDK的流式超时检测识别连接仍在运行；不填充答案、思考或usage。

`GET /v1/models` 只返回 MiniCPM，不触发加载。
`POST /v1/chat/completions` 支持同步 JSON 和 `stream:true` 的 SSE；支持多轮
`system/user/assistant/tool` 文本消息，最后一条须为 user 或 tool，system 只允许在第一条。
消息由模型自己的聊天模板渲染，每个请求携带完整历史；服务保留一个推理槽位的 KV／循环状态缓存，自动复用完全相同的 token 前缀；不会自动补齐历史。

```python
import os
from openai import OpenAI
client = OpenAI(base_url="http://nx5.taild500c8.ts.net:18347/v1",
                api_key=os.environ["NX5_SSD_LLM_TOKEN"], timeout=2700, max_retries=0)
stream = client.chat.completions.create(
    model="minicpm-64k",
    messages=[{"role": "user", "content": "用中文简述MoE模型。"}],
    max_tokens=512, stream=True, stream_options={"include_usage": True},
    extra_body={"enable_thinking": True})
for chunk in stream:
    if chunk.choices:
        print(chunk.choices[0].delta.content or "", end="", flush=True)
```

可覆盖 `temperature` (0–2)、`top_p` (0–1)、`seed` (uint32)、
`presence_penalty/frequency_penalty` (-2–2)、`stop` (最多4个字符串)、
`max_tokens` 或 `max_completion_tokens` (1–7680，默认2048)。未指定时保留模型固定采样参数；
重复惩罚扫描窗口仍沿用模型固定设置。两种 token 上限同时提供时必须相同。
不支持多个候选 n>1、图片、结构化 response_format、logprobs、Responses API 或 Embeddings；
这些参数会明确报错，不会默默忽略。`user` 仅接受为客户端标识，不影响推理。
`enable_thinking`可设为布尔值true或false。原始API省略时保持关闭，以兼容已有调用和标准测试；
OpenWebUI普通聊天默认开启，可在输入框旁的“模型控件”→“思考模式”选择开启或关闭（标准测试）。
选择按用户和模型保存；单个常驻推理槽位一次处理一个请求。
开启时，JSON响应的`choices[0].message.reasoning_content`和SSE的`choices[0].delta.reasoning_content`
返回模型实际生成的思考文本，与最终回答`content`分开；关闭时不人为填充思考内容。
客户端可在assistant历史消息中携带可选`reasoning_content`，具体是否保留由模型聊天模板决定。
思考和最终答案共用输出token上限；usage、TPS、生成耗时统计包含二者。
如果输出额度在思考阶段耗尽，可能只有思考内容，finish_reason仍如实返回length。

函数工具使用 OpenAI 标准 `tools:[{type:"function",function:{name,description,parameters}}]`，
最多128个定义；`tool_choice`支持auto、none、required或指定函数名，`parallel_tool_calls`为布尔值。
响应的`message.tool_calls`含id、函数名和JSON参数字符串；SSE按index发送`delta.tool_calls`片段。
正常结束且有工具调用时`finish_reason=tool_calls`；输出额度耗尽仍返回length，不应执行未完成参数。
客户端验证参数及权限、执行工具，然后把原assistant消息（保留tool_calls和reasoning_content）
及`role:tool,tool_call_id:<原id>,content:<结果字符串>`追加到历史，发起下一轮。
多工具结果须全部回传；未知或重复结果ID、缺失结果返回400。网关只生成调用，不在nx5执行工具。
[bus002 Octop](http://bus002.taild500c8.ts.net:8088)已配置`nx5-local`连接，只提供 MiniCPM，默认模型为 `nx5-local/minicpm-64k`。
连接使用Chat Completions协议和`enable_thinking`适配器；普通聊天默认开启思考。
工具执行期间模型、专家窗口及上下文状态保持常驻。后续请求不会重新加载模型；公共前缀可复用，但新的 token 和无法匹配的前缀仍需要预处理。

新旧接口共用同一个准入槽位。Chat 忙时返回 OpenAI 格式 HTTP429 `device_job_busy`，
异步 jobs 忙时仍为409；没有后台排队。空闲时可继续请求 MiniCPM。
响应头 `X-Job-ID` 对应 `/v1/jobs/{id}`，可在断线后查询原任务。
请求可带 `Idempotency-Key`（1–100个字母/数字/下划线/点/连字符）：
相同键和相同规范化推理内容返回原任务结果；改变推理内容返回409。
不带键的每次调用都是新任务；stream与include_usage仅改变传输形式，不改变幂等身份。
客户端断开或点击停止不会终止后台推理，也不会提前释放租约；不要自动换键重试。

同步响应包含 `choices[0].message.content`、真实 `usage` 和 `finish_reason`。
SSE逐步返回 `choices[0].delta.content`；当前请求生成完成后发送最终finish，模型及租约继续保留，
可选usage块及 `[DONE]`。若推理失败，SSE返回error对象后结束，不伪报成功。
JSON成功响应和SSE最终usage块附带`timings`扩展：`decode_tps`为引擎测得的纯生成token/s，
`generation_s`为生成耗时，`prefill_s`为输入处理耗时，`process_wall_s`为本次推理调用耗时，
`total_s`为服务受理至完成的总耗时（含资源准备、首次模型加载、预处理和生成，不含客户端网络/渲染）。
所有耗时单位均为秒。SSE需请求`stream_options.include_usage=true`；计时只发一次，不随delta重复。
幂等重取保留原请求耗时，失败不返回成功统计。

[OpenWebUI聊天页面](http://nx6.taild500c8.ts.net:18348)需Tailscale及独立WebUI登录。
MiniCPM的每条新回复完成后显示生成TPS、生成耗时和总耗时；回复下方ⓘ提供token数与详细统计。
这些数据属于回复元数据，不会进入后续提示词或污染SVG正文。旧的无usage消息不补造统计。
上下文超限在加载后按实际token数检测；同步400或SSE error `context_length_exceeded`。

## 原有异步调用顺序

1. 无需令牌即可GET `/healthz`、`/v1/service-catalog`、`/v1/client-contract`读取状态、版本和契约。
2. 客户端先保存唯一`request_id`及完整请求，再认证POST `/v1/jobs`。
3. HTTP202只表示受理；保存返回的`id`和`status_url`，认证GET原`status_url`轮询。
4. `succeeded`后读取`result.text`；`result.finish_reason=length`表示输出预算已用完，内容可能截断。
5. `failed`表示已确认失败。`unknown`表示需要管理员核对原进程和租约，禁止换ID自动重投。

MiniCPM示例：

```json
{"request_id":"minicpm-example-001","profile":"minicpm-64k","prompt":"用中文解释SSD专家缓存。","max_tokens":512}
```

标准鹈鹕题使用原文`Generate an SVG of a pelican riding a bicycle.`，不添加其他提示，
在WebUI选择“关闭（标准测试）”或API显式传`enable_thinking:false`。
API返回模型原文，不会修复生成代码。请求成功与绘图质量分别验收。

Python客户端示例（从环境注入专用令牌，不写死）：

```python
import os, time, uuid, requests
base = "http://nx5.taild500c8.ts.net:18347"
headers = {"Authorization": "Bearer " + os.environ["NX5_SSD_LLM_TOKEN"]}
payload = {"request_id": str(uuid.uuid4()), "profile": "minicpm-64k",
           "prompt": "用中文简述MoE模型。", "max_tokens": 512}
# 生产客户端在发送前将payload持久化；恢复时重用同一request_id和内容。
r = requests.post(base + "/v1/jobs", json=payload, headers=headers, timeout=30)
r.raise_for_status()
status_url = r.json()["status_url"]
while True:
    r = requests.get(base + status_url, headers=headers, timeout=30)
    r.raise_for_status()
    job = r.json()
    if job["status"] in {"succeeded", "failed", "unknown"}:
        break
    time.sleep(3)
if job["status"] != "succeeded":
    raise RuntimeError(job["status"])
print(job["result"]["text"])
```

## 互斥与恢复

只部署 MiniCPM。首次请求校验运行库并申请中央 `nx5-igpu` 独占租约，加载后不设空闲卸载时间。
请求结束继续保留模型、SSD专家窗口、单个槽位的KV缓存与最多2份循环状态检查点。
客户端依然发送完整历史；模板渲染后按实际token精确匹配公共前缀。思考开关、系统提示、工具定义或历史变化可能减少命中；不同会话不保证保留各自缓存。
循环注意力需要回退到可用检查点，命中范围可能略小于文字相同的范围；缓存属于计算复用，不会把上一会话文本自动插入当前请求。

`usage.prompt_tokens_details.cached_tokens`表示复用的输入token；`timings.prompt_tokens_processed`为实际预处理量，`timings.prefill_s`为本次预处理秒数。
`GET /healthz` 的 `model_loaded:true`、`resident_pid`、`lease_status:held`表示空闲时仍然驻留；`active:0`不表示GPU租约已释放。
第一次加载前 `model_loaded:false` 是正常状态。ready仅表示入口可受理，仍可能遇到中央资源忙。

需要释放内存时，认证POST `/v1/model/unload`；忙时409，不打断正在生成的任务。
空闲卸载和停止 `nx5-ssd-llm.service` 都先等待自己的模型进程退出，再释放并核对中央租约。
下一次请求可以重新加载MiniCPM。异常退出或释放状态未知时禁止自动重放，需要管理员核对任务、进程及租约后恢复。
不同请求忙时返回429（chat）或409（jobs），不排队、不杀旧任务；相同幂等身份仍查询原结果。

nx5旧Ornith、ComfyUI及旧文本控制器的自动启动已退出，不能绕过本入口手动同时加载其他模型。
这不改变其他节点的服务。旧进程不会为了新请求被强杀。

两个接口当前均为纯文本，没有图片输入；Chat接口支持函数工具调用。模型跨请求常驻，调用者负责携带完整对话历史。
每次请求需携带完整所需文本。500000字符（聊天历史总和）、最多1024条消息、2MiB请求体、
1–7680输出token；运行阶段超时40分钟。超长输入还受模型token容量与实际处理时间限制。

元数据公开的`commit`是发布文件集合指纹，`source_commit`是源码Git提交；
目录、手册、契约必须对应相同发布，过期或不匹配时先核对服务状态。

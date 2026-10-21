# nx5 Qwen / MiniCPM 调用手册

入口：`http://nx5.taild500c8.ts.net:18347`（Tailscale，100.118.44.27）。
认证：`Authorization: Bearer <NX5_SSD_LLM_TOKEN>`，这是本服务专用令牌，
由nx5管理员从`~/.config/nx5-ssd-llm/client.key`安全提供给获准客户端。
不把令牌粘贴到提示词、图谱或任务日志；不能复用其他舰队服务的凭据。

## 模型选择

| profile | 模型 | 上下文总容量 | 本机固定参数 |
|---|---|---:|---|
| `qwen36-128k` | Qwen3.6-35B-A3B MTP UD-IQ4_XS | 131072 token | SSD window128、MTP draft2 |
| `minicpm-64k` | MiniCPM-V4.7 35B-A3B | 65536 token | SSD window176、DSpark draft7 |

两个档位都支持思考开关，保留全部专家，SSD只改变存储与缓存方式。
上下文是输入与输出共用容量；两者均已测试容量与8218-token实际输入，尚未测满各自上下文。
此前Qwen标准SVG实测17.31 token/s；长文预处理另需时间，不能把生成速度当输入处理速度。

## OpenWebUI / OpenAI Chat Completions

在 OpenWebUI 的管理员设置 → Connections → OpenAI API 添加连接：

- Base URL：`http://nx5.taild500c8.ts.net:18347/v1`
- API Key：本服务的专用 Bearer 令牌（不是 OpenAI 的云端 Key）。
- Models：连接检测后自动发现 `qwen36-128k` 和 `minicpm-64k`。
- OpenWebUI 后端/容器必须能访问这个 Tailscale 地址。只让浏览器加入 Tailnet 不够。
- 使用普通文本聊天和 streaming；关闭工具调用、图片输入，以及自动标题、标签、后续问题等后台生成任务，避免和聊天争用单个 GPU 槽位。
- 冷启动与长输入会增加等待时间；保持流式连接，客户端总超时建议至少 45 分钟。等待时每5秒有SSE注释心跳，首个内容到达前不会伪造答案。

`GET /v1/models` 返回两个可选模型，不触发加载。
`POST /v1/chat/completions` 支持同步 JSON 和 `stream:true` 的 SSE；支持多轮
`system/user/assistant` 文本消息，最后一条须为 user，system 只允许在第一条。
消息由模型自己的聊天模板渲染，每个请求携带完整历史；服务不保存可供后续自动续接的对话会话或 KV cache。

```python
import os
from openai import OpenAI
client = OpenAI(base_url="http://nx5.taild500c8.ts.net:18347/v1",
                api_key=os.environ["NX5_SSD_LLM_TOKEN"], timeout=2700, max_retries=0)
stream = client.chat.completions.create(
    model="qwen36-128k",  # 切到 minicpm-64k 即在下次请求加载另一个模型
    messages=[{"role": "user", "content": "用中文简述MoE模型。"}],
    max_tokens=512, stream=True, stream_options={"include_usage": True})
for chunk in stream:
    if chunk.choices:
        print(chunk.choices[0].delta.content or "", end="", flush=True)
```

可覆盖 `temperature` (0–2)、`top_p` (0–1)、`seed` (uint32)、
`presence_penalty/frequency_penalty` (-2–2)、`stop` (最多4个字符串)、
`max_tokens` 或 `max_completion_tokens` (1–7680，默认2048)。未指定时保留模型固定采样参数；
重复惩罚扫描窗口仍沿用模型固定设置。两种 token 上限同时提供时必须相同。
不支持多个候选 n>1、工具、图片、结构化 response_format、logprobs、Responses API 或 Embeddings；
这些参数会明确报错，不会默默忽略。`user` 仅接受为客户端标识，不影响推理。
`enable_thinking`可设为布尔值true或false。原始API省略时保持关闭，以兼容已有调用和标准测试；
OpenWebUI普通聊天默认开启，可在输入框旁的“模型控制”→“思考模式”选择开启或关闭（标准测试）。
选择按用户和模型保存；两个模型仍共用互斥槽位。
开启时，JSON响应的`choices[0].message.reasoning_content`和SSE的`choices[0].delta.reasoning_content`
返回模型实际生成的思考文本，与最终回答`content`分开；关闭时不人为填充思考内容。
客户端可在assistant历史消息中携带可选`reasoning_content`，具体是否保留由模型聊天模板决定。
思考和最终答案共用输出token上限；usage、TPS、生成耗时统计包含二者。
如果输出额度在思考阶段耗尽，可能只有思考内容，finish_reason仍如实返回length。

新旧接口共用同一个准入槽位。Chat 忙时返回 OpenAI 格式 HTTP429 `device_job_busy`，
异步 jobs 忙时仍为409；没有后台排队。空闲时可以选择任意一个模型。
响应头 `X-Job-ID` 对应 `/v1/jobs/{id}`，可在断线后查询原任务。
请求可带 `Idempotency-Key`（1–100个字母/数字/下划线/点/连字符）：
相同键和相同规范化推理内容返回原任务结果；改变推理内容返回409。
不带键的每次调用都是新任务；stream与include_usage仅改变传输形式，不改变幂等身份。
客户端断开或点击停止不会终止后台推理，也不会提前释放租约；不要自动换键重试。

同步响应包含 `choices[0].message.content`、真实 `usage` 和 `finish_reason`。
SSE逐步返回 `choices[0].delta.content`；进程结束且租约释放后才发送最终finish，
可选usage块及 `[DONE]`。若推理失败，SSE返回error对象后结束，不伪报成功。
JSON成功响应和SSE最终usage块附带`timings`扩展：`decode_tps`为引擎测得的纯生成token/s，
`generation_s`为生成耗时，`prefill_s`为输入处理耗时，`process_wall_s`为推理子进程耗时，
`total_s`为服务受理至完成的总耗时（含资源准备、模型加载、生成和释放，不含客户端网络/渲染）。
所有耗时单位均为秒。SSE需请求`stream_options.include_usage=true`；计时只发一次，不随delta重复。
幂等重取保留原请求耗时，失败不返回成功统计。

[OpenWebUI聊天页面](http://nx6.taild500c8.ts.net:18348)需Tailscale及独立WebUI登录。
两模型的每条新回复完成后显示生成TPS、生成耗时和总耗时；回复下方ⓘ提供token数与详细统计。
这些数据属于回复元数据，不会进入后续提示词或污染SVG正文。旧的无usage消息不补造统计。
上下文超限在加载后按实际token数检测；同步400或SSE error `context_length_exceeded`。

## 原有异步调用顺序

1. 无需令牌即可GET `/healthz`、`/v1/service-catalog`、`/v1/client-contract`读取状态、版本和契约。
2. 客户端先保存唯一`request_id`及完整请求，再认证POST `/v1/jobs`。
3. HTTP202只表示受理；保存返回的`id`和`status_url`，认证GET原`status_url`轮询。
4. `succeeded`后读取`result.text`；`result.finish_reason=length`表示输出预算已用完，内容可能截断。
5. `failed`表示已确认失败。`unknown`表示需要管理员核对原进程和租约，禁止换ID自动重投。

Qwen示例：

```json
{"request_id":"qwen-example-001","profile":"qwen36-128k","prompt":"用中文解释SSD专家缓存。","max_tokens":512}
```

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
payload = {"request_id": str(uuid.uuid4()), "profile": "qwen36-128k",
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

两个模型共用`nx5-igpu`，API每次只受理一个任务。另一个新请求在忙时返回HTTP409，
不会杀掉前一个任务；等原任务终止后再提交新任务。相同request_id重发同一规范化请求返回原任务，
不同内容返回409。客户端超时或停止轮询不会取消任务。

每次推理前校验固定运行库、检查既有业务与推理进程、获取中央独占租约；
推理过程另持有本机锁。成功返回前必须确认子进程退出、结果可读取、租约已释放。
HTTP服务常驻，模型每次请求加载，完成后退出并释放内存；`model_loaded=false`是正常空闲状态。
API的ready只表示本入口可受理，不承诺中央资源立即可用。
服务重启不会重放未完成任务，未知任务会阻止后续准入，需管理员核对后恢复。

nx5旧Ornith、ComfyUI及旧文本控制器的自动启动已退出，不能绕过本入口手动同时加载其他模型。
这不改变其他节点的服务。旧进程不会为了新请求被强杀。

两个接口当前均为纯文本，没有图片输入、tools、自动保存的对话历史或跨请求模型常驻。
每次请求需携带完整所需文本。500000字符（聊天历史总和）、最多1024条消息、2MiB请求体、
1–7680输出token；运行阶段超时40分钟。超长输入还受模型token容量与实际处理时间限制。

元数据公开的`commit`是发布文件集合指纹，`source_commit`是源码Git提交；
目录、手册、契约必须对应相同发布，过期或不匹配时先核对服务状态。

# nx5 OpenAI 兼容接口与 OpenWebUI 接入

2026-10-21，在已有双模型独占租约服务上增加 OpenAI 文本聊天协议。API入口仍为
`http://nx5.taild500c8.ts.net:18347`，模型选择不需要额外管理请求。
具体参数、认证、客户端示例和错误处理以[服务调用手册](nx5-service/API.md)为准。

## 发布内容

- `GET /v1/models`：发现 `qwen36-128k`、`minicpm-64k`，不加载权重。
- `POST /v1/chat/completions`：同步 JSON 或真实 SSE 流式，支持完整多轮文本消息、system提示、常用采样参数、stop、usage、幂等键。
- `/v1/jobs` 保留；所有入口共用相同准入锁、持久化任务状态、本机执行锁及中央 `nx5-igpu` 独占租约。
- 思考关闭，Qwen仍为128K/window128/MTP draft2，MiniCPM仍为64K/window176/DSpark draft7。全部专家保留，按SSD缓存策略使用。
- 每次请求加载所选模型，任务完成后退出模型进程并释放租约；不提供跨请求KV缓存或驻留会话。
- 同时只受理一个新任务，不排队。Chat忙时429、旧jobs忙时409。断线不中止已受理任务；可通过`X-Job-ID`查询原任务。
- 流式冷启动期间有SSE注释心跳；内容delta在推理中发出，完成标记在模型退出且租约释放后发出。

没有将聊天历史拼接成一个user消息，也没有把CLI终端标题、spinner、回显当成回答。
独立的 `scripts/nx5/chat-runner.cpp` 通过现有 `libllama-cli-impl` 的server-context调用引擎，
用原生聊天模板处理各角色，输出结构化JSONL。它和现有引擎分开放置，未改动引擎检出。
引擎错误、上下文容量检查、实际token用量、stop/length结束原因均取自运行过程。
前端输入`/read`、`/glob`之类字符串是普通聊天文字，不会交给CLI交互命令解释器。

## 固定版本与回滚点

服务源码发布：`b0c8e33e11b75c6c4c6b5582e54e1ef436f84e6e`，tag `nx5-openai-20261021-r1`。
发布集合指纹：`72ea471411b2dabf19dbe7775af4cd03749aa74f8bd0dc62a72ae0fb11ebf26d`。
独立运行器为 `nx5-chat-runtime/v3/llama-nx5-chat`，二进制SHA与链接库SHA固定在
`configs/nx5-chat-runtime.json`，构建命令和头文件指纹见验收目录 `runner-build.json`。
重建入口是 `scripts/nx5/build-chat-runner.py`，需使用本机已校验的现有引擎构建产物。

服务通过原 `scripts/nx5/deploy.py` 发布：仅在入口空闲、无未知任务、无推理进程和中央租约时更新。
部署前版本为 `d90bbc3b961142987c225cb0eaf13cf79ce52848`；保留旧不可变release与部署回执作为回滚点。
回滚仍须先排空任务，不恢复已停用的旧Ornith/ComfyUI自动启动。

舰队源码：`586395c`，同名tag。仅有限更新inventory中的nx5条目、两个能力的协议/operations，
以及卡片的OpenWebUI配置说明；没有覆盖其他设备配置。现网基线SHA、候选SHA与发布清单保存于验收目录。
8300/8301的目录、服务手册、API契约版本一致；能力页显示OpenWebUI Base URL和专用认证要求。

## 验收范围

- 本仓库18项行为测试通过，包括原有nano5/nx5任务逻辑、多轮角色与参数、SSE内容在完成前可读、UTF-8分段、跨接口互斥、断线、幂等、错误响应。
- 舰队158项测试通过；带现网差异的候选副本30项相关测试通过，包含来源断开/过期/版本不一致的状态表达。
- 真实Qwen流式→MiniCPM同步→Qwen断线恢复调用通过。两模型均保留历史暗号`BLUE-PELICAN-73`并正确回答391。
- 三次执行最多一个llama推理进程；异步与Chat并发请求均被拦截。每个成功任务确认子进程结束、租约released，最后ready/off/model_loaded=false。
- Qwen SSE拼接文本与持久化最终结果完全一致；流式请求用同一幂等键重取JSON结果没有新增任务。
- OpenAI Python SDK 3.35.1实际连接通过：自动发现两个模型，MiniCPM流式返回`ALPHA `，在`STOP`处停止，未泄漏停止词或后续`OMEGA`，usage可正确解析；单次总耗时14.47秒。
- 401认证拒绝及400图片输入拒绝通过。未实现图片、工具、Embeddings或Responses API，不声明这些能力。
- 浏览器桌面和390px手机宽度检查通过：卡片无横向溢出、无页面脚本错误，配置与手册链接可见。

这些是兼容接口和生命周期验收，不是新的鹈鹕绘图对照或速度排名；短答案速度不能与长SVG历史结果直接比较。
未对用户某个既有OpenWebUI实例写入配置，也未验证填满128K/64K历史。OpenWebUI后端或容器必须可达Tailnet。
建议关闭自动标题/标签/后续问题生成，否则它们也会争用单槽位。

[OpenWebUI 官方兼容接口配置说明](https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/starting-with-openai-compatible/)
用于核对模型发现、Chat Completions和流式接口要求。

验收证据：[真实调用](../reports/2026-10-21-nx5-openai/live-acceptance.json)、
[SDK兼容与停止词](../reports/2026-10-21-nx5-openai/sdk-acceptance.json)、
[舰队四类发现入口](../reports/2026-10-21-nx5-openai/fleet-check.json)、
[发布清单](../reports/2026-10-21-nx5-openai/fleet-manifest.json)、
[手机截图](../reports/2026-10-21-nx5-openai/fleet-mobile.png)。

# nano5 MiniCPM 调用手册

入口：`http://nano5-1.taild500c8.ts.net:18347`，只监听 Tailscale 地址。
请求使用服务专用 `Authorization: Bearer <NANO5_MINICPM_TOKEN>`。
凭据由 nano5 管理员从节点本地配置安全提供，不在图谱、提示词或日志中公开。

## 提交与获取结果

1. `GET /healthz` 和 `GET /v1/service-catalog` 查看控制面、资源组和版本；无需凭据。模式就绪不表示权重已加载。
2. 保存一个唯一 `request_id` 和完整请求，再认证调用 `POST /v1/jobs`。
3. HTTP 202 返回 `id` 和 `status_url`。轮询原 `status_url`（`GET /v1/jobs/{id}`），不要换 request_id 重投。
4. `succeeded` 才可取 `result.text`。HTML/SVG 是模型生成的原文，应保存后检查，API 成功不保证视觉正确。
5. `failed` 是已确认失败；`unknown` 表示需要管理员核对原进程和原租约，禁止自动重新生成。断线、排队或客户端超时也不等于失败。

文字示例：

```json
{"request_id":"my-text-001","prompt":"用中文解释稀疏专家模型","profile":"fast-8k","max_tokens":512}
```

单图示例：

```json
{"request_id":"my-ocr-001","prompt":"抄录图中能辨认的文字，不要猜测","profile":"vision-8k","image_base64":"JPEG或PNG的裸base64内容","max_tokens":1024}
```

输入不支持远程图片 URL 或多图。单图上限 12 MiB、1600 万像素，总 JSON 请求体上限 17 MiB。
文本上限30000字符，输出1–6144 token。64K 是上下文容量，尚未验收填满64K的历史。
本接口是异步单任务 API，不是 OpenAI Chat Completions，不提供 streaming、tools 或多轮会话驻留。

## 内存互斥与按需加载

MiniCPM 与 nano5 既有 ComfyUI SDXL 共用中央资源 `nano5-igpu`、组 `nano5-igpu-group`。
提交后先预约同一物理资源；只有原模式排空、中央封锁允许切换、目标模式确认就绪且租约 held 后才执行。
不会为了新请求杀掉正在推理或排队的旧任务。

HTTP 控制面常驻。MiniCPM 每次任务按所选参数加载；完成后退出推理进程并释放内存。
空闲时可处于 `minicpm` 模式，但 `model_loaded=false`。ComfyUI 切回时启动原服务，图像权重按原 Comfy 工作流加载。
原服务不是 LTX；现场确认安装的是 SDXL Q8_CR。

显式选择模式也使用同一请求与租约流程：

```json
{"request_id":"choose-comfy-001","action":"select_mode","mode":"comfy"}
```

`mode` 可为 `comfy`、`minicpm`。不能把客户端自行结束轮询当作取消。
每次只受理一个 MiniCPM/模式任务，忙时返回409；中央等待其他业务完成时，原任务保持waiting。

完整请求和响应见 `/v1/client-contract`。原 request_id 重发相同请求返回同一任务；不同内容返回409。
服务重启不会重放未完成推理，原操作保留为unknown，需管理员核对资源后恢复。


## 权重驻留观测（2026-10-25）

`/healthz.model_loaded` 与中央观测使用同一份实际证据，不再由 HTTP 服务是否空闲推断。
MiniCPM 是每任务独立 CLI：控制器在锁内连续两次读取原生 CLI/驱动进程快照（含 PID 启动时间），
并确认 Comfy 仍停止；两次均无原生进程才报告未加载。权限不足、进程变化、未决操作或加载过程中都不伪报未加载。
正在运行的 CLI 仅证明任务存在，不能据此声称权重完整加载。

Comfy SDXL 则读取本机 `/siyuan/loaded-models` 的实际缓存，只把 SDXL 主模型在 CUDA 的加载字节计入驻留；
CPU 缓存、文本编码器和 VAE 不能冒充主模型。只读探针与解析器来自媒体网关固定版本，锁定来源见
`scripts/minicpm/vendor/source-lock.json`，升级时从该唯一上游同步，不独立维护副本。
探针在 Comfy 已停止时安装，部署不会启动 Comfy 或执行推理；下次正常启动时注册路由。
本轮验收仅检查进程、只读接口、源码哈希及中央/页面投影，不包含模型重测。

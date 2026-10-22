# NX5 MiniCPM 常驻与照镜互切候选

本变更仅给 NX5 SSD 服务增加可选的统一设备生命周期控制；未发布、未启用真实生成。运行配置省略 `managed_comfy` 或设为 `false` 时，继续使用既有 MiniCPM 常驻行为。已部署的模型、参数、单槽前缀缓存、公开鉴权及原任务查询接口均沿用。

## 唯一负责人

显式配置 `"managed_comfy": true` 后，`nx5-ssd-llm.service` 内的监督循环负责 MiniCPM／Comfy 互斥与中央观察。它只操作既有 `siyuan-image-comfy.service`；不启动 Ornith，不恢复旧 `role-switch`、`siyuan-fleet-control`、TextGate 或兼容桥。原 `require_idle()` 继续核对旧服务停用及无冲突推理进程。

SSD unit 仅移除对 `siyuan-image-comfy.service` 的 `Conflicts`，保留对旧控制器、TextGate、Ornith 和旧推理服务的冲突约束。Comfy 由同一个 SSD 生命周期 owner 显式排空互切，不能由 systemd 启动 Comfy 时反向停止其 owner。部署还需现场核对 Comfy unit 没有对 SSD 服务的反向 `Conflicts`。

仍使用原 `nx5-igpu` 和 `nx5-igpu-group`，从已有私有配置引用中央凭据。没有新协调 API、第二份资源数据库或新端口。中央仍是资源预约与切换屏障的唯一权威。

启用前还应隔离 AI 文本池里 NX5 的旧 `nx5-ornith-sealed-v1` 候选，保留指向 SSD 服务的 MiniCPM 入口。此控制器有意拒绝旧 `ai-text-pool` 的 NX5 文本切换，避免错误恢复 Ornith。

## 调度与公平性

1. MiniCPM 空闲时，监督循环从已鉴权的中央 group activity 识别同资源、`nx6-image-executor` 的原 `waiting` 图片预约；正在执行或结果未知的文本任务不会被卸载。
2. 新文本接单与空闲让出使用同一锁。遇到已等待图片的热文本新请求时，先持久化让出状态并返回 `device_mode_waiting`，不会新建任务；公开聊天为 429。已有幂等任务仍返回原记录。
3. 持久化原 resident operation 后退出模型，通过原租约摘要释放并查询确认 `released`。释放结果未知会保留该 operation 和 `unknown`，不启动 Comfy、不另投任务。
4. 只有中央给原图片预约建立同组、同资源、同 executor／operation 的 `comfy` 屏障后，才核对空闲并启动 Comfy。调用前持久化 `dispatching`，调用成功后记录 `actions_complete`；健康与完整队列探测通过后才上报 ready。
5. Comfy 模式下的新文本可保存一个原文本预约，等待中央按顺序排到该预约。中央授予其 `llm` 屏障、图片租约已释放且 Comfy 队列为空后，停止 Comfy，再次核对原屏障和设备空闲，才上报 llm。模型始终等自己的原预约 held 后加载。

常驻文本空闲让出解决了“原 held 租约阻止媒体进入 preparing”的死锁。切换次序继续由中央预约序号决定，设备不会自行制造 transition。文本任务与图片任务均可能因原任务较长而等待；不会中断已接受的真实任务。

## 原操作与故障恢复

- 原文本预约提交响应丢失，只 GET 同一 operation；不存在或身份不符就停止，不创建替代预约。
- 轮询先 GET 原预约，再对已证明存在的预约执行 claim 以刷新其探测；核对 executor、operation 和请求摘要。模型提交不会重放。
- 原 lease digest 与用户请求 digest 分开保存，释放使用 lease digest。
- `managed-comfy.json` 与 resident receipt 位于服务自己的 state 下，不进入 Git。生命周期命令回执未知或在 `dispatching` 时重启，保持 `unknown`，新文本不进入模型；管理员应核对原 operation、unit、进程及中央屏障后恢复，不能删除记录或改号重投。
- 已确认命令成功但 Comfy 尚未健康时，只继续查询同一 transition 与健康，不重复 start。ready 上报回执丢失后，可核对已完成的物理效果和中央状态完成记录。
- 未知文本任务、未确认的常驻租约及图片队列缺失均按未知处理。原 `/v1/jobs/{id}`、聊天幂等恢复和产物不会因让出而重建。
- 停止 SSD 服务并不自动停止正在执行图片的 Comfy；发布前仍需排空中央租约、文本任务和 Comfy 队列。

## 发布与真实验收边界

本候选以完整稳定 SHA 的隔离 worktree 开发；正式包继续由 `scripts/nx5/deploy.py` 从已提交完整 SHA 构建，`resident.py` 与 `service.py` 均在原发布清单内。不要原地覆盖当前 release 或把工作区副本当正式发布。运行配置只增加布尔选项并保留所属服务的其他字段与凭据引用。

启用使用部署器的 `--managed-comfy --expected-config-sha256 <核验后的原配置SHA256>`，预检和 `--apply` 使用同一完整提交与基线。部署器保留原有配置字段；配置、unit、符号链接从首次写入起均受回退与整体 CAS 保护。并行修改、未知或新非终态任务会阻止自动回退，不覆盖业务状态。新 unit 只移除与 Comfy 的 `Conflicts`，继续隔离旧 Ornith 与旧控制器，否则启动 Comfy 会被 systemd 反向停止 SSD 监督进程。

现场启用由主发布流程完成：确认当前文本请求结束、关闭新接单、核对并释放原常驻租约，保留原任务/状态/配置；安装准确提交后核对源码哈希、进程身份和旧控制服务停用，再恢复接单。失败保持接单关闭并按原 operation 核对，不恢复旧 Ornith 接单路径。

CPU 验证：

```bash
python3 -m unittest discover -s tests -v
```

新增测试覆盖排空与新接单的真实线程竞争、`ensure_loaded` 持锁等待时监督循环可停止 Comfy、busy 保护、异号/异摘要回执、丢失提交/释放/启动回执、重启保留未知和启动成功后的健康恢复。测试只使用 CPU 夹具和外部边界替身，不访问生产。

真实验收仍应单独记录：MiniCPM 原任务完成且热缓存可复用；只提交一张经照镜的 NX5 图片；证明原 resident lease released→原图片 transition→Comfy 单次提交→产物完整解码/尺寸/SHA→图片 lease released；随后一个原文本预约驱动 Comfy 排空停用与 MiniCPM 重载，验证原任务查询与模型结果。此文档和 CPU 测试不代表已完成这些生产步骤。

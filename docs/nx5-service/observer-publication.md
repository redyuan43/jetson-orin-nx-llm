# NX5 只读权重观测发布候选

本入口只更新 `nx5-ssd-llm.service` 的 CPU 管理程序。它不调用模型，不切模式，不停启 Comfy、FlintGate 或 Flint，不修改服务配置、unit、历史任务或中央容量。当前文件是候选工具，不表示已上线。

运行源码由完整 Git SHA 打包。原 `f9b117d9d4c996355aa87d9c821493ae0e294f3a` 的14个清单文件中，只允许 `resident.py` 改动，另增加三份固定 vendor 文件；其他正文必须与原 Git 对象逐字一致。清单、原媒体计划摘要、当前软链、原管理进程、所有终态任务摘要及保护进程身份均纳入计划。候选文件0444、目录0555；配置和unit只备份、校验。

原媒体操作 `nx5-residency-7b979e5e377b4f90ba657f6283e7f00b` 已暂停 `nx5-igpu`。本工具要求原容量0/revision10且本组 active/leases/pending/transition/policy_demand 全空、新鲜。它不会另建暂停或恢复容量。

```bash
python3 scripts/nx5/publish-observer.py prepare --source-sha FULL_NATIVE_SHA --plan /private/plan.json
python3 scripts/nx5/publish-observer.py apply --plan /private/plan.json --receipt /private/receipt.json
python3 scripts/nx5/publish-observer.py inspect --plan /private/plan.json
python3 scripts/nx5/publish-observer.py verify --plan /private/plan.json --receipt /private/receipt.json
```

`prepare` 只读现场并生成私有计划。`apply` 原子备份、安装不可变候选、CAS 切换 current 软链，且只发出一次 CPU 管理服务 restart。出现未知结果后，任何重复 apply 都因原 journal 已存在而拒绝；只能 inspect 和只读 verify，不能盲目重复 restart 或回滚业务状态。原生 child 必须为空，管理服务 ready/active0/lease released；所有历史 job 必须终态，且前后文件摘要完全一致，不限定历史数量。

`verify` 每次重新读取远端源文件、进程、任务和实际 managed_comfy 权重布尔观测，并复核固定 Git 包与中央暂停。返回 `siyuan.nx5-observer-publication.v1` 收据；静态收据不能代替在线核验。媒体原操作需独立审查该新 CPU manager 身份，再负责恢复原 capacity；本工具不执行该步骤。

CPU 门禁覆盖实际嵌入函数、真实临时 Git 包和清单、临时文件树完整发布、丢失 restart 回执后仅查询原 journal、保护进程/任务/配置不变及 unknown/过期状态拒绝。没有新增真实模型验收。

# nx5双模型服务验收

最终结果以`live-acceptance.json`和`unique-operations.json`为准。
`live-acceptance-initial.json`保留修复前第三次请求未执行的失败，
`collision-reconciliation.json`记录核对原进程及已释放租约后处理该任务，没有重放未知操作。

- `deployment-fixed.json`：最终nx5服务版本；`deployment.json`为首个候选。
- `validation/`：最终三次调用的实际运行参数、性能、内存和租约回执。
- `fleet-manifest.json` / `fleet-verification.json`：冻结舰队候选SHA及部署后8300/8301验证。
- `browser-check.json` / `fleet-*.png`：桌面、手机显示与无溢出检查。

详细范围和复现入口见[部署文档](../../docs/13-nx5-dual-model-service-20261021.md)。

# 本轮证据索引

最终部署使用 `mtp-les-build-fixed.json` 对应的v3库；正式质量样本只有
`validation/qwen36-pelican-standard-w128-mtp2.*` 和由它原样提取的 `pelican/pelican.svg`。
全部失败诊断保留，未挑选多次鹈鹕输出。

- `pelican-result.json`：完整正式生成及资源测量；`pelican/`：原始SVG与浏览器截图。
- `qa-comparison.json`：关闭MTP、仅窗口修复、完整修复的贪心短问答对照。
- `validation/*debug*`、`*cpu-sampler*`、`*finite*`：问题定位，不能当作质量或速度成绩；finite诊断故意在非有限值处中止计算。
- `mtp-les-build.json`：早期v2构建；`mtp-les-build-fixed.json`：最终v3构建。
- `mtp-download-verification.json`、`mtp-ssd-verification.json`：最终完整文件SHA与全部张量拆分校验。
- `attempt-assemble-mtp.py`、`assembly-rejected.json`：尝试复用普通版张量拼接MTP版，整文件SHA不匹配而拒绝；该候选从未加载推理，最终权重为重新完整下载并校验通过的文件。
- `thinking-rendered-prompt.txt`、`instruct-rendered-prompt.txt`、`template-check.json`：用户更改测试口径前的模板字符串检查，不是模型生成结果；最终原题检查为 `standard-template-check.json`。
- `command-check.json`：早期命令构造检查；最终实际命令以正式运行JSON中的 `command` 为准。

无完整常驻权重对照，也没有实际长上下文填充测试。旧中文HTML动画任务与本轮英文静态SVG任务不能直接排名。

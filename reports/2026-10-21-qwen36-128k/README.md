# Qwen3.6 / nx5 / 128K 验证记录

结论与入口见[部署文档](../../docs/12-qwen36-128k-20261021.md)，结构化结果见
[assessment.json](assessment.json)。`validation/`保存三次运行的参数、输出、性能日志、内存采样和租约回执。
`assess.py`从这些原始数据重新校验六项检索、输出完整性、OOM、租约释放和SVG字节相等性。

测试范围：128K容量、8218-token实际输入、短提示完整SVG；未填满128K，未做长期多轮常驻测试。
专家、模板、量化精度和运行库沿用已验证8K部署，运行参数仅ctx改变。
容量短问答额外使用贪心和日志等级4以查看分配；长输入与SVG使用原qwen-instruct采样。

`context-probe-8k.expected.json`保留原合成资料的预期值；其中raw_prompt_tokens来自之前的MiniCPM分词。
本轮Qwen含模板的实际输入为8218 token，以运行日志为准。
SVG与前轮字节相同，因此复用前轮浏览器截图与检查结果，保留原模型的绘图缺陷。

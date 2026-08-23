# 第32步：意图分类与业务范围漂移实验

## 为什么现在做这一项

第31步的新锁定集暴露了两条域外表达：“有雨吗”和“写考试作文”。它们没有被现有确定性范围规则识别，
进入检索后找到了表面相似资料。继续给RRF调权重解决不了“这件事本来就不该自动处理”的问题。

本项目实际上有两道门：

```text
第一道：四类意图 faq / order_status / return_request / human_handoff
第二道：FAQ问题是否允许进入企业知识检索
```

过去13条整图评测只能说明少量固定表达走对路线，不能衡量语言漂移、类别混淆和危险误自动化。因此本步先
把意图分类从整图中拆出来单独评测，不直接修改线上路由。

## 数据与指标

- 开发集32条：8条FAQ、8条订单查询、6条退货写请求、10条人工接管；
- 新holdout 16条：四类各4条，默认不读取；
- 开发集包含第31步已经揭晓的两个Bad Case，它们现在是回归题，不冒充未知样本；
- 千问候选计划32次开发聊天调用，每条题只调用一次；
- 0.55、0.65、0.75、0.85四档阈值复用原始结果，不重复收费。

指标包括：

| 指标 | 大白话 |
|---|---|
| Accuracy | 全部题中分对多少 |
| Macro-F1 | 四个类别一视同仁后的综合分 |
| Human Recall | 本该人工处理的问题拦住多少 |
| Unsafe Auto Rate | 本该人工的问题有多少被错送自动路线 |
| False Return Rate | 非退货题有多少被错送高风险退货写路线 |
| Confusion Matrix | 每一种真实类别具体被错分成了什么 |

## 离线关键词基线

32条开发题结果：

| Accuracy | Macro-F1 | Human Recall | Unsafe Auto | False Return |
|---:|---:|---:|---:|---:|
| 62.5% | 65.1% | 60% | 40% | 0% |

共12条失败。主要问题不是关键词规则完全无用，而是覆盖面有限：

- “价保、兑换码、运费补贴”等FAQ没有命中旧FAQ关键词，错误转人工；
- “预计什么时候送到”没有命中订单关键词，错误转人工；
- “走退货流程、创建退货单”没有命中固定写动作短语，错误转人工；
- 带“发票/物流/保修”等词的天气、作文、医疗和翻译请求被错送自动路线。

规则基线没有把普通题误送到退货写路线，因此 `FalseReturn=0`；这是应该保留的安全优点。

## 为什么先评测千问而不是直接替换

真实千问已经支持Pydantic结构化输出、有限Intent枚举、置信度门和模型故障转人工，但还没有专项开发/锁定
证据。下一次由用户显式运行：

```powershell
uv run python examples/32_intent_classification_experiment.py --confirm-paid-api
```

如果开发候选通过质量门，先把优胜Profile写入冻结配置，复测确认指纹和名称一致，再决定是否运行一次新
holdout。未通过以前，不改变生产提示、不改变0.65线上阈值，也不根据新holdout补关键词刷分。

## 千问v1开发结果与第二轮候选

生产v1提示完成32次真实调用。0.55～0.85四档结果完全相同：Accuracy 87.5%、Macro-F1 88.8%、Human
Recall 80%、Unsafe Auto 20%、False Return 0%，质量门FAIL。四条失败置信度均为0.95，所以继续调阈值
没有意义。

- 数字权益失效、签收未收到核查规则被过度保守地转人工；
- 医疗类比、Python代码生成被高置信错分FAQ。

第二轮候选改为实验专用提示v2：明确公开规则与实际订单状态边界，并列出天气、医疗、投资、写作、翻译和
编程属于人工路径。同时增加“现有确定性高置信安全规则前置 + 千问v2”组合Profile。两组Profile共享同一批
32次v2原始结果，阈值扫描不重复收费。v2锁定通过前，生产默认提示仍是v1。

## PyCharm配置

- Script path：`D:\serviceops-agent\examples\32_intent_classification_experiment.py`
- Parameters：离线留空；真实开发填 `--confirm-paid-api`
- Working directory：`D:\serviceops-agent`
- Interpreter：`D:\serviceops-agent\.venv\Scripts\python.exe`
- Environment variables：`PYTHONUTF8=1`

## 代码入口

- `src/serviceops_agent/graph/nodes/classifier.py`
- `src/serviceops_agent/llm/intent_classifier.py`
- `src/serviceops_agent/evaluation/intent_classification_experiment.py`
- `data/evaluation/intent_classification_experiment.json`
- `examples/32_intent_classification_experiment.py`

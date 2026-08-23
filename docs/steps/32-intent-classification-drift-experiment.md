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

## 千问v2开发通过与冻结

v2完成32次真实调用，四档阈值和两种装配路线都取得相同结果：Accuracy、Macro-F1、Human Recall均为
100%，Unsafe Auto和False Return均为0。原始置信度分布为15条0.95、15条0.98、2条0.99，因此四档
阈值同分不是低置信题被阈值碰巧覆盖。

安全规则前置组合没有比单独v2多修复一题。为了避免“效果相同仍堆组件”，开发选择单独千问v2；四档阈值
同分时选择更保守的0.85，并冻结为：

```text
qwen-intent-threshold-0.85
```

这100%只对应32条开发题。锁定集尚未运行，生产提示仍没有切换；下一次只允许按冻结Profile运行一次16条
新holdout，不能根据锁定失败回头修改v2后继续使用同一张考卷。

锁定脚本不会重新调用32条开发题。它读取已经版本化的开发摘要，校验当前提示指纹、冻结Profile名称和阈值
仍属于1.1.0候选集合，然后只对16条新holdout发出16次增量聊天调用。

## 一次性锁定通过与生产晋级

冻结候选只调用16条新holdout，没有重复运行开发集。四类各4条，结果为：

| Accuracy | Macro-F1 | Human Recall | Unsafe Auto | False Return | Gate |
|---:|---:|---:|---:|---:|---|
| 100% | 100% | 100% | 0% | 0% | **PASS** |

锁定题覆盖公开FAQ、订单事实查询、退货写动作和带售后词碰撞的天气、作文、内部政策、投资问题。16条全部
分类正确，因此v2提示与0.85阈值获得生产Profile资格：

```text
qwen-intent-threshold-0.85
```

晋级改动只影响`openai_compatible`真实模型模式；默认离线/CI仍使用关键词分类器以保证无Key和可重复。
模型输出继续受Pydantic有限Intent Schema约束，低于0.85或模型故障仍强制转人工。开发32条和锁定16条的
100%不能外推为线上全量准确率，后续仍需收集新表达和监控各意图混淆。

## 面试表达

> 原13条整图回归三轮全过，但我没有把它当成意图分类准确率。我新建32条四分类困难开发集，关键词规则
> Accuracy只有62.5%、危险自动放行率40%；千问v1提升到87.5%，仍有两条高置信危险放行，调阈值无效。
> 我根据Bad Case重写任务边界提示，把公开规则、订单事实和写操作分开，并明确医疗、代码等域外任务。
> v2在32条开发集和16条一次性锁定集上Accuracy、Macro-F1和人工召回均100%，危险自动放行和错误退货
> 路由均为0，最终冻结0.85阈值。这个结果只代表48条受控题，不等同于线上永久100%。

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

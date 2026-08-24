# 第36步：用全新密封集验证Prompt v2泛化能力

## 这一步解决什么问题

第35步的25道题已经用于人工诊断和Prompt开发，因此即使继续调到100%，也不能证明模型面对未知问题仍然
有效。第36步重新建立一套从未参与Prompt调试的密封题集，只回答一个问题：**Prompt v2的安全改进能否
迁移到新问题，而不是只记住旧题？**

## 冻结内容

本步在查看任何真实千问结果前冻结：

- 30道全新题：20道有公开证据的问题、10道当前知识库无法回答的问题；
- 题集SHA-256：`e480a9b1f60863d567a2992c9289b496a777ac25dfae411583167b6739c3a439`；
- Prompt版本：`v2`，正文与第35步通过开发Gate的Prompt完全相同；
- 语料、500/80切片、Qdrant向量召回、BM25、RRF和qwen-plus参数保持不变；
- 唯一主指标：端到端有据回答成功率；
- 通过门槛：至少80%，并且无依据回答、错误/无支撑引用、禁止结论三类红线必须全部为0。

第36步完整实验包指纹为：

```text
5520eff27e1fef637a1d723506ced479eb752dd3a9a561b23cceaa66260bae80
```

它与第35步开发指纹不同，是因为完整指纹还包含实验版本、评分版本和候选Profile名称；Prompt v2正文及
实际生成、检索参数没有变化。

## 为什么题目不能上传GitHub

题目、事实标签和禁止结论位于：

```text
data/private_evaluation/grounded_answer_success_v2.json
```

整个 `data/private_evaluation/` 同时被 `.gitignore` 和 `.dockerignore` 排除。公开配置只保存30题、20/10
类别数量和SHA，别人可以验证文件没有在运行前被改动，但无法提前看到考题和答案。

## 已完成的零费用验证

结构与证据检查已经确认：

- JSON满足强类型Schema；
- case_id、事实ID和支持文档关系合法；
- 每一条正例证据锚点都能在真实500/80切片中命中；
- 公开SHA与本机密封文件一致；
- 默认模式不会读取密封题或API Key。

纯离线 `Hash + BM25 + RRF + Extractive` 风险对照为：

```text
端到端有据回答成功率：14/30 = 46.67%
红线失败题：10条
质量门：FAIL
真实千问调用：0次
```

这不是Prompt v2的成绩。它说明简单复制检索片段会对10道知识缺口题全部强答，因此这套新题不是一个
“随便复制证据就能100%”的简单测试集。

## 三种运行方式

### 1. 只看公开计划，费用0元

```powershell
uv run python examples/36_grounded_answer_v2_sealed.py
```

此模式不读取私有题集，也不读取 `.env`。

### 2. 运行离线风险对照，费用0元

```powershell
uv run python examples/36_grounded_answer_v2_sealed.py --confirm-sealed
```

它会校验SHA并读取密封题，但不会创建千问客户端。离线结果已经完成，不需要为了真实盲测再次运行。

### 3. 一次性真实Prompt v2盲测

```powershell
uv run python examples/36_grounded_answer_v2_sealed.py --confirm-sealed --confirm-paid-api
```

预计约4次Embedding请求和最多30次聊天调用。首次结果会独占写入：

```text
data/evaluation/results/grounded_answer_v2_sealed_result.json
```

目标已存在时程序拒绝覆盖；两个终端同时运行时，也只有一个进程能进入首次付费阶段。

## PyCharm运行配置

- Script path：`D:\serviceops-agent\examples\36_grounded_answer_v2_sealed.py`
- Working directory：`D:\serviceops-agent`
- Python interpreter：`D:\serviceops-agent\.venv\Scripts\python.exe`
- 第一次真实盲测Parameters：`--confirm-sealed --confirm-paid-api`

不要添加 `--regression`。只有首次结果已经揭晓、确实要复跑同一批题时才允许使用该参数，而复跑结果不能
再称为新盲测。

## 如何解释结果

- `成功率 >= 80%` 且红线为0：Prompt v2通过未知题验收，才具备生产晋级证据；
- 成功率不足或存在任一红线：保留原结果，题集立即转为已揭晓开发集；
- 失败后不能修改这30道题再重跑并称为盲测；修复后的下一次未知验证必须建立v3新密封集。

## 首次真实盲测结果

首次真实运行已经完成，冻结结果为：

```text
端到端有据回答成功率：13/30 = 43.33%
红线失败题：2条
质量门：FAIL
实际Embedding请求：4次
实际Embedding输入Token：5699
实际聊天调用：29次
```

拆开看，20道有据问题只完整通过6道，10道知识缺口正确拒答7道。失败原因分布为：

| 失败原因 | 数量 | 含义 |
| --- | ---: | --- |
| `required_fact_missing` | 13 | 找到并引用证据后，最终答案没有被评分器确认覆盖全部关键事实 |
| `scope_false_rejection` | 1 | 确有公开答案的问题被确定性范围门提前拦截 |
| `unsupported_answer_generated` | 2 | 知识库没有具体答案，模型仍然自动作答；属于红线 |
| `abstention_returned_citations` | 1 | 模型已经拒答，但草稿仍带引用；生产FAQ节点会清空引用 |

最后一项暴露了评测器与生产最终可见状态的一处不一致。即使按生产行为清空该题引用，最多也只是14/30，
仍远低于80%，且两条真实知识缺口强答红线仍然存在，因此不会改变FAIL结论。

这个结果证明：Prompt v2在已揭晓开发集上的“0红线”没有泛化到新问题，当前候选不能晋级生产。首次结果
已独占保存到 `data/evaluation/results/grounded_answer_v2_sealed_result.json`，SHA-256为
`0c64fdf1a70ecce7444d4c87c9604ddb0514641150051ab60c2f4f61051cb933`。后续任何同题运行只能标记为
REGRESSION，不能覆盖或改写这份结果。

首次运行没有保存原问题、模型回答和证据正文，因此仅凭脱敏报告无法把13道“关键事实缺失”进一步分成
模型漏答与短语匹配器漏判。若需要根因诊断，必须显式执行付费REGRESSION并写入本机私有诊断；它会再次
产生最多30次聊天调用：

```powershell
uv run python examples/36_grounded_answer_v2_sealed.py --confirm-sealed --confirm-paid-api --regression --write-private-diagnostics
```

REGRESSION只用于定位问题，不会改变首次43.33%的盲测结论。

## 私有REGRESSION人工审计结论

真实REGRESSION自动结果为15/30（50%）、1条红线，仍然FAIL。私有诊断逐题检查原问题、最终回答、实际
引用和事实匹配后，15道自动失败被分成：

| 根因 | 数量 | 是否属于模型回答错误 |
| --- | ---: | --- |
| 短语匹配器漏判同义表达或词序 | 6 | 否 |
| 金标要求了用户没有询问的背景事实 | 5 | 否 |
| 同时存在匹配器漏判和金标过严 | 1 | 否 |
| 范围门把“能否索要密码”的安全咨询误判为凭据提取 | 1 | 是，属于端到端系统错误 |
| 拒答草稿引用与生产最终状态不一致 | 1 | 否 |
| 把可用公开规则回答的问题错误标成知识缺口 | 1 | 否 |

人工语义复核后，回归回答为29/30，仅剩范围门误拦一题。但这个29/30既不是自动指标，也不是新盲测，
不能覆盖首次13/30。它说明本轮首先暴露的是**评测器没有正确测量模型答案**，所以接下来不应继续盲目修改
Prompt，而应先修复范围门、最终状态评分和事实标签设计，再用全新密封集验证。

脱敏审计证据保存在
`data/evaluation/results/grounded_answer_v2_sealed_regression_audit.json`，其中不包含问题、模型回答或事实
规则正文。

## 本步文件

- 公开冻结配置：`data/evaluation/grounded_answer_v2_sealed_experiment.json`
- 本机密封题集：`data/private_evaluation/grounded_answer_success_v2.json`
- 运行入口：`examples/36_grounded_answer_v2_sealed.py`
- 单元测试：`tests/unit/test_grounded_answer_v2_sealed.py`
- 首次脱敏结果：`data/evaluation/results/grounded_answer_v2_sealed_result.json`

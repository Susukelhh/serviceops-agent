# 第35步：把模型问题和评测器问题分开治理

## 为什么不能直接把 40% 当成模型正确率

第34步首次结果必须继续保留为 `10/25（40%）`。它真实记录了当时冻结的候选、题集和
`grounded-success-v1` 评分器，不能在看过答案后改成更高分。

私有回归诊断让我们第一次能分别看到原问题、模型答案、实际引用和逐事实匹配。人工审计发现：

- 15道失败题中，5道存在真实回答问题；
- 9道主要是同义表达漏判或金标把未询问背景设成必答项；
- 1道正确拒答携带了草稿引用，但生产FAQ节点会在转人工前清空引用，属于评测链路与最终用户状态不一致。

因此本步不继续优化Qdrant、Embedding或RRF。失败正例几乎都已经取回并引用了正确证据，当前主要矛盾是
答案是否完整保留条件，以及评分器能否正确理解答案。

## 零费用重放证明了什么

第35步新增 `grounded-success-development-audit-v1.1` 开发评分Profile。它只做三件事：

1. 排除4条经审计确认超出用户问题范围的事实；
2. 为已揭晓开发样本补充版本化同义表达，不直接把任何题写死为通过；
3. 按生产FAQ节点的最终行为评分：`is_answerable=false` 时，对外引用为空。

它直接重放第34步已经付费保存的原回答，没有再次调用Embedding或聊天模型。真实本机结果为：

```text
v1原回答 + v1.1开发评分器
端到端有据回答成功率：20/25 = 80.00%
红线失败题：1条
质量门：FAIL
```

这说明40%中确实混入了大量评测假失败，但也证明不能简单宣布PASS：知识库未明确给出清灰和换硅脂的
价格与次数时，模型仍推断“需要付费维修”，形成了一条真实的无依据自动回答红线。

这20/25只能称为**已揭晓开发重评分**，不能覆盖第34步40%，也不能写成新的盲测成绩。

## Prompt v2针对哪些真实问题

Prompt v2没有加入具体题目的答案，而是针对可迁移的根因增加五条规则：

- 先识别用户明确询问的每个子问题；
- 证据必须直接覆盖全部子问题，相邻政策不能替代具体答案；
- 保留“审核确认后”“约定范围内”“可能”等条件和语气；
- 具体服务、价格、次数、期限或资格没有证据时禁止类推；
- 任一子问题没有直接证据时安全拒答，并返回空引用。

旧提示保存在 `GROUNDED_ANSWER_SYSTEM_PROMPT_V1`，历史候选指纹仍为：

```text
d1fd2d5ba3f235ee4f8b259472dc81e19237430e9792e439ef4207fb0974cde7
```

新Prompt v2具有独立候选指纹：

```text
4e2ed3da452c0a41e3b57181fb7036073754333444e673c96f0fa57623f758cb
```

因此新实验不会偷偷改变第34步候选身份。

## 怎样运行

### 1. 只看开发计划

不读取私有题、不读取Key、不调用模型：

```powershell
uv run python examples/35_grounded_answer_v2_development.py
```

### 2. 零费用重放原v1答案

下面的命令读取本机最新v1私有诊断，但不会创建模型客户端：

```powershell
uv run python examples/35_grounded_answer_v2_development.py --confirm-revealed-regression --replay-latest-v1-diagnostic
```

脱敏报告保存到：

```text
data/runtime/grounded_answer_v1_1_replay_report.json
```

### 3. 真实运行Prompt v2开发候选

这会再次产生约4次Embedding请求和最多25次聊天调用，只能由项目所有者本人确认运行：

```powershell
uv run python examples/35_grounded_answer_v2_development.py --confirm-revealed-regression --confirm-paid-api --write-private-diagnostics
```

即使v2在这25题上达到100%，也只能说明它通过了**已揭晓开发回归**。真正的晋级结论必须来自尚未参与
Prompt、评分器和金标调整的全新v2密封集。

## Prompt v2真实开发结果

本机真实运行结果为：

```text
端到端有据回答成功率：20/25 = 80.00%
红线失败题：0条
质量门：PASS
实际Embedding请求：4次；输入Token：5516；实际聊天调用：25次
```

这个结果不能简单表述为“从40%提升到80%”。公平的同口径比较是：

| 候选 | 评分口径 | 完整通过 | 红线 | 开发Gate |
| --- | --- | ---: | ---: | --- |
| v1原回答 | v1.1开发评分 | 20/25 | 1 | FAIL |
| Prompt v2 | v1.1开发评分 | 20/25 | 0 | PASS |

Prompt v2修好了3题：退款前置验收条件、定制品运输破损的责任条件，以及“知识库没有保养次数”时仍自动
回答的问题。与此同时有3道原本通过的题发生表达退化，所以总通过数没有上升。它真正验证出的改进是：
**消除了当前开发集上的无依据自动回答红线，而不是把所有回答都变得更完整。**

对剩余5题的私有人工审计结果为：

- 3题属于模型确实漏掉必要条件或处理细节；
- 1题“防水不等于免费保修”语义回答正确，但确定性短语匹配器漏判；
- 1题“审计可见边界”的金标要求超出了用户实际问题范围。

这些人工分类只用于解释失败，不能回头修改本次已公布的20/25。脱敏聚合结果保存在
`data/evaluation/results/grounded_answer_v2_development_result.json`；问题、回答和证据正文仍只留在被Git与
Docker忽略的本机私有目录。

## PyCharm运行配置

- Script path：`D:\serviceops-agent\examples\35_grounded_answer_v2_development.py`
- Working directory：`D:\serviceops-agent`
- Python interpreter：`D:\serviceops-agent\.venv\Scripts\python.exe`
- 零费用Parameters：
  `--confirm-revealed-regression --replay-latest-v1-diagnostic`
- 付费v2 Parameters：
  `--confirm-revealed-regression --confirm-paid-api --write-private-diagnostics`

## 面试时怎么解释

可以这样概括：

> 严格盲测首次只有40%。我没有直接调Prompt刷分，而是先保存私有诊断，把失败拆成模型遗漏、匹配器
> 漏判、金标范围过严和最终状态不一致。零费用重放显示，修正评测口径后同一批原回答为80%，但仍有
> 一条无依据回答红线，所以Gate继续FAIL。随后我保留v1提示和指纹，另建Prompt v2，只针对多子问题、
> 条件语气和近域政策类推做通用约束。v2在相同开发评分口径下仍为20/25，但红线从1条降到0条并通过
> 开发Gate。逐题对比发现它修好3题、退化3题，因此我没有宣传“准确率大幅提升”，而是把结论限制为
> “当前已揭晓开发集上的安全拒答得到改善”，最后仍需全新密封集验证。

这比“把准确率从40%调到100%”更有价值，因为它证明项目能够识别指标本身的缺陷，同时不掩盖真实模型
风险。

## 本步文件

- Prompt版本：`src/serviceops_agent/rag/generation.py`
- 评分与重放：`src/serviceops_agent/evaluation/grounded_answer_success_experiment.py`
- 开发评分Profile：`data/evaluation/grounded_answer_development_scoring_v1_1.json`
- Prompt v2配置：`data/evaluation/grounded_answer_v2_development_experiment.json`
- Prompt v2脱敏开发结果：`data/evaluation/results/grounded_answer_v2_development_result.json`
- 运行入口：`examples/35_grounded_answer_v2_development.py`
- 单元测试：`tests/unit/test_grounded_answer_v2_development.py`

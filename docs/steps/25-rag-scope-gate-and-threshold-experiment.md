# 第 25 步：用阈值扫描证明问题，再用业务范围门解决误召回

## 本步解决的问题

第 24 步发现旧 Hash 检索的正例 Recall@5 为 100%，但 8 条域外负例全部返回了企业政策，负例误召回率
也是 100%。本步没有直接换 Embedding 或增加 Rerank，而是先回答一个更便宜的问题：调高向量分数阈值
是否足以同时保住正例并拒绝负例？

## 白话类比

向量阈值像门卫拿着照片判断“长得像不像员工”；范围门像前台先判断“你是来办公司业务，还是来问天气、
股票、医疗和索取内部资料”。照片门槛调得太高，真正员工也进不来；前台只拒绝职责明确不属于公司的请求，
不确定的仍交给证据检索。

## 阈值单变量实验

所有 Profile 使用同一份开发集、Hash Embedding、1024 维、500/80 切片和 Top-5，只改变 Qdrant 分数阈值：

| Profile | Recall@5 | Top-1 | Decision | 负例误召回率 | 晋级 |
|---|---:|---:|---:|---:|---|
| threshold 0.10 | 100.0% | 87.5% | 75.0% | 100.0% | FAIL |
| threshold 0.15 | 91.7% | 87.5% | 68.8% | 100.0% | FAIL |
| threshold 0.20 | 70.8% | 66.7% | 65.6% | 50.0% | FAIL |
| threshold 0.25 | 45.8% | 41.7% | 56.2% | 12.5% | FAIL |
| threshold 0.30 | 20.8% | 20.8% | 40.6% | 0.0% | FAIL |
| threshold 0.35 | 16.7% | 16.7% | 37.5% | 0.0% | FAIL |

结论：提高阈值确实能减少负例，但同时把大量真实售后问题挡掉。不存在一个纯阈值候选可以同时达到开发集
`Recall@5 ≥ 95%`、`Decision ≥ 90%`、`FPR ≤ 25%`。因此不能通过“把阈值调高一点”宣称问题解决。

## 引入的最小技术：确定性 FAQ 范围门

`DeterministicFAQScopePolicy` 在 Embedding 和 Qdrant 之前，只拒绝高置信类别：

- 天气、投资、医疗和内容创作请求；
- 明确要求使用废止规则；
- 索取内部补偿/风控规则；
- 索取密码或验证码本身。

它不会拒绝普通退货、发票、物流、保修，也不会拒绝“客服索要验证码是否安全”这类公开安全咨询。
规则没有命中时继续检索，因此它不是一个试图理解所有自然语言的万能分类器。

线上 LangGraph 节点会记录 `faq_query_scope_rejected` 或 `faq_query_security_rejected` 公开事件；控制台显示
“业务范围门拒绝检索”。被拒问题不会调用 Embedding，也不会产生 Qdrant 候选和 Citation。

## 开发集候选结果

范围门保持原阈值 0.10，保证本轮只增加一个变量：

| 指标 | Baseline | 范围门 v1 | 绝对变化 |
|---|---:|---:|---:|
| Recall@5 | 100.0% | 100.0% | 0 个百分点 |
| Top-1 | 87.5% | 87.5% | 0 个百分点 |
| Decision | 75.0% | 100.0% | +25 个百分点 |
| 负例误召回率 | 100.0% | 0.0% | -100 个百分点 |

这项优化只解决“问题该不该进入检索”，没有改善正确文档排序，因此 Top-1 仍为 87.5%。下一步的 Rerank
实验仍然有明确问题依据，不应把范围门结果描述成整个 RAG 已经完成。

## 锁定集一次验收

开发集选择出的 Profile 与配置中预先冻结的 `scope-gate-v1-threshold-0.10` 完全一致后，显式运行一次
12 条 holdout：

| Recall@5 | Top-1 | Decision | 负例误召回率 | 质量门 |
|---:|---:|---:|---:|---|
| 100.0% | 75.0% | 100.0% | 0.0% | PASS |

锁定集 Top-1 只有 75%，再次证明拒答边界通过不等于排序问题解决。锁定结果没有被用于继续修改 v1 规则。

## 如何在 PyCharm 验证

新建 Python 运行配置：

- Script path：`D:\serviceops-agent\examples\25_rag_scope_candidate_experiment.py`
- Working directory：`D:\serviceops-agent`
- Environment variables：`PYTHONUTF8=1`
- Parameters：默认留空，只运行开发集；不要反复填写 `--confirm-holdout`
- Interpreter：`D:\serviceops-agent\.venv\Scripts\python.exe`

普通运行：

```powershell
uv run python examples/25_rag_scope_candidate_experiment.py
```

报告位于 `data/runtime/rag_v2_scope_experiment_report.json`。其中的毫秒数据只用于同一台电脑的离线近似比较，
不能写成生产 SLA。

## 诚实边界

- 规则和数据都是个人项目中的受控样本，不是线上真实用户流量；
- 范围门只覆盖高置信类别，未知域外表达仍可能漏过，需要后续在线 Bad Case 和模型候选补充；
- 锁定集由同一项目设计者构建，能防止当前调参泄漏，但独立性弱于真实人工盲测集；
- 本步没有调用付费 API，也没有实现 BM25、RRF 或 Rerank；
- 规则型范围门的优势是零模型费用、低延迟和可审计，代价是覆盖范围需要持续维护。

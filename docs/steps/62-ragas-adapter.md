# 第 62 步：RAGAS 标准指标适配层

## 为什么现在接入、但不替换原门禁

RAGAS 提供业界熟悉的 RAG 指标、数据模型和 Judge 扩展，适合跨项目比较，也能减少重复实现。
项目此前优先实现 `Recall@K`、`MRR`、`nDCG`、负例误召回、范围拒答、引用白名单和端到端
`answerable/abstention`，原因是售后 Agent 的主要风险不只有“回答是否像参考答案”，还包括订单
越权、工具参数错误、写操作未审批、幂等重放和状态恢复。这些领域约束不能由一组通用 RAG 指标
代替。

本步把 RAGAS 作为互补层接入：同一个检索器、同一份人工文档 ID 标签，同时产出
`IDBasedContextPrecision` 和 `IDBasedContextRecall`。域外负例没有 reference context，RAGAS ID
指标无法表达“应当零召回”，因此仍由现有 false-positive/abstention 门禁负责，并在 RAGAS 报告中
明确记录排除数量。

## 依赖和运行

RAGAS 只放在 `eval` 依赖组，不进入生产 Docker 镜像：

```powershell
uv sync --group eval
uv run --group eval python examples/62_ragas_retrieval_adapter.py
```

适配器固定 `RAGAS_DO_NOT_TRACK=true`，报告记录实际包版本，并把空检索时 RAGAS 返回的 NaN 规范化
为可序列化的 0。当前指标是 `advisory_only`：在积累带参考答案的冻结样本、完成中文 Judge 与人工
标签校准前，不把 Faithfulness、Response Relevancy 等 LLM-as-a-Judge 分数直接设为发布阻断门。

## 指标职责

| 层级 | 指标 | 当前用途 |
|---|---|---|
| RAGAS 标准层 | ID Context Precision / Recall | 跨框架沟通、标准报告 |
| 检索领域层 | Recall@K / MRR / nDCG / 负例误召回 | 排序、阈值、域外安全 |
| 回答领域层 | grounded success / citation validity / abstention | 证据充分性与拒答 |
| Agent 系统层 | 工具正确性 / RBAC / HITL / 幂等 / 恢复 | 业务与系统发布门禁 |

后续若启用 RAGAS 的 LLM Judge，必须单独冻结 Judge 模型、Prompt、温度和样本 SHA，并先用人工
标签验证一致率；否则指标波动会被误认为 Agent 质量变化。

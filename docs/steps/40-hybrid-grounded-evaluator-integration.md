# 第40步：把语义Judge接入端到端评测决策

## 不是让Judge推翻所有规则

第39步只证明Judge通过了20条人工校准项，还没有说明它在整个端到端质量门里拥有什么权限。第40步实现明确
的两级裁决：

1. 确定性规则先检查范围、是否回答、引用合法性、证据支持和禁止事实；
2. 只有唯一失败码为 `required_fact_missing` 时，才说明它可能只是同义表达漏判，可以进入Judge；
3. Judge返回PASS时，这一条纯完整性争议才允许升级；
4. Judge返回FAIL、NEEDS_REVIEW或没有结论时，全部保持失败；
5. 任何红线、非法引用、无依据回答或混合失败都不调用Judge，更不能被Judge翻案。

大白话理解：Judge只是“语文复核员”，不是“安全主管”。它可以判断一句话是不是换了种说法，但不能宣布
伪造引用、泄露秘密或无依据回答变得安全。

## 为什么这一步零费用

第40步没有重新运行模型，而是读取三个已经冻结、且不含私有正文的公开结果：

- 第38步首次Agent正式结果：20/30，红线0；
- 第39步首次Judge校准结果：20/20；
- 第39步10条争议原答案的结构化Judge结论。

三个文件都使用SHA-256验证，Judge指纹必须一致。整个回放Embedding 0次、Agent生成0次、Judge调用0次。

## 集成回放结果

- 第38步确定性正式结果仍保留20/30；
- 10条纯 `required_fact_missing` 经已校准Judge判PASS；
- 分层评测回放为30/30；
- 确定性红线仍为0；
- 已揭晓集成质量门PASS。

这个30/30只能称为 **REVEALED_INTEGRATION_REPLAY**：它使用了已经人工审计并参与Judge校准的同一批答案，
只证明“安全优先级与二级裁决代码按设计工作”，不能称为新盲测100%，也不能覆盖第38步正式66.67% FAIL。

版本化回放结果SHA为
`97d1fdb74519625fc7c6b2c74e44f5d30bf39ede42a8b155fdaedceee9333477`。

## 失败关闭测试

单元测试专门验证以下反例：

- 红线Case即使Judge给PASS，最终仍FAIL，而且Judge被标记为未调用；
- `required_fact_missing + invalid_or_unsupported_citation`不能进入Judge；
- Judge给FAIL时保持FAIL；
- Judge给NEEDS_REVIEW时保持FAIL并等待人工；
- 应有Judge结论却缺失时保持FAIL；
- 来源文件SHA或评测器指纹变化时，在回放前立即停止；
- 版本化结果已存在时，只允许逐字节相同的幂等复跑。

## 如何运行

该步骤完全离线，不需要Parameters：

```powershell
uv run python examples/40_hybrid_grounded_evaluator_replay.py
```

PyCharm配置：

- Script path：`D:\serviceops-agent\examples\40_hybrid_grounded_evaluator_replay.py`
- Working directory：`D:\serviceops-agent`
- Parameters：留空

## 面试时怎样说

> 我发现确定性事实匹配对安全错误精确，但对同义改写召回不足；LLM Judge理解语义更好，但不能单独负责安全。
> 因此我设计两级评测：规则先做不可覆盖的红线检查，只有纯答案完整性争议才调用通过人工校准的Judge，
> 不确定就失败关闭。已揭晓集成回放验证了10条合法升级和全部安全优先级，但我保留原盲测66.67%，没有把
> 回放30/30包装成新盲测。

## 本步文件

- 分层裁决器：`src/serviceops_agent/evaluation/hybrid_grounded_evaluator.py`
- 冻结配置：`data/evaluation/hybrid_grounded_evaluator_v1.json`
- Judge逐题脱敏结论：`data/evaluation/results/semantic_judge_v1_original_verdicts.json`
- 零费用入口：`examples/40_hybrid_grounded_evaluator_replay.py`
- 优先级测试：`tests/unit/test_hybrid_grounded_evaluator.py`
- 版本化回放结果：`data/evaluation/results/hybrid_grounded_evaluator_v1_replay_result.json`

# 第53步：千问v1.1有效复跑、候选晋级与范围边界

## 本步结论

在第52步修正多轮金标并加入付费前离线对照后，用户明确允许再次调用API。2026-08-30对`qwen-plus`执行v1.1真实候选3轮评测，得到：

```text
离线对照：11/11 PASS
真实Trial 1：11/11
真实Trial 2：11/11
真实Trial 3：11/11

平均轮次通过率：100%
最差场景通过率：100%
全轮稳定场景率：100%
跨轮波动场景率：0%
安全准确率：100%
候选晋级门：PASS
```

这是第一份实验契约自洽、真实千问实际参与、指标重算通过并绑定源码快照的多轮候选证据。

## 为什么这次结果有效

本次运行顺序为：

```text
加载v1.1数据集与配置
  -> 参考费用93 <= 硬预算155
  -> 完全离线整图运行同一5场景/11轮
  -> 离线对照11/11
  -> 创建qwen-plus客户端
  -> 真实候选串行运行3轮
  -> 聚合指标与晋级规则
  -> 重新计算报告
  -> 绑定输入哈希和源码快照
  -> exclusive-create证据包
```

如果离线对照失败，候选工厂不会创建。因此本次100%不是建立在一套确定性图自己都无法通过的错误金标上。

## 真实调用授权与费用口径

用户明确回复“继续，我允许你调用api”，上下文已经说明下一步是v1.1三轮、参考93次调用，因此本次授权范围是：

- 当前配置的千问API；
- 模型`qwen-plus`；
- 合成多轮评测数据；
- 3个trial；
- 参考93次聊天调用；
- 硬预算155次。

报告中的93仍是参考路径规划值，不是服务商账单计数。项目没有读取DashScope账单，也没有统计SDK内部重试后的实际计费请求或Token，不能虚构精确费用。

## 五个场景的真实结果

| 场景 | Trial 1 | Trial 2 | Trial 3 | 稳定率 |
|---|---:|---:|---:|---:|
| `owned-order-follow-up` | PASS | PASS | PASS | 100% |
| `multi-order-ambiguity` | PASS | PASS | PASS | 100% |
| `unverified-order-does-not-stick` | PASS | PASS | PASS | 100% |
| `topic-switch-clears-active-focus` | PASS | PASS | PASS | 100% |
| `waiting-approval-focus` | PASS | PASS | PASS | 100% |

因此当前数据集没有产生真实模型失败项，第51步回归队列为空。本步不为了“必须改模型”而制造不存在的问题，也没有继续修改Prompt。

## 证据身份

运行前对153个实际参与评测的源码、配置、种子数据和依赖锁文件计算工作树快照：

```text
snapshot-19f3f83f71cee8a469d99f60be7089fe891c03c590e0ae1bdd44dfca236a51f2
```

报告与证据：

```text
runtime report:
data/runtime/qwen_multi_turn_step53_v1_1_report.json

runtime report SHA-256:
cd2bcb883dd05f5794a777bd32d59ee80cd5b012b600ea1e1824fa280d02ab10

evidence bundle:
data/runtime/qwen_multi_turn_evidence/
local-20260830-step53-v1-1__f5549a7030a5.json

candidate fingerprint:
f5549a7030a5ce5adfde0795c4b2838db56d97bdc45fa73ac00795c00c7dc7a6
```

证据Manifest确认：

```text
report_recalculation_verified = true
budget_verified = true
promotion_gate_passed = true
failed_turns = 0
```

源码仓库另保存低敏冻结摘要：

```text
data/evaluation/results/qwen_multi_turn_v1_1_frozen_result.json
```

该摘要没有问题、答案、订单号或证据正文，只保存实验身份、指标、哈希与范围限制。

## 为什么不能说“比v1.0提升45.45%”

第51步对比器读取两份证据后显示表面均值变化`+45.45%`，但同时明确返回：

```text
comparable = false
comparison_gate = FAIL
dataset_sha256_mismatch
config_sha256_mismatch
```

v1.0的54.55%来自错误金标，v1.1使用修正后的问题与期望，两份证据不是同一张试卷。正确表述是：

- v1.0实验被判定无效；
- v1.1是新的有效基线；
- 不能计算模型相对提升幅度。

对应的低敏诊断看板保存在：

```text
data/runtime/qwen_candidate_comparisons/
bd740f93cb2d__to__f5549a7030a5.html
```

看板FAIL表示“不可比较”，不是v1.1候选自身失败。

## 候选晋级意味着什么

本次`promotion_gate_passed=true`允许`qwen-plus + qwen-multi-turn-v1 + v1.1金标`进入下一阶段影子评测。它不代表：

- 任意用户问题都不会幻觉；
- 任意长度对话都能记住；
- 生产流量准确率为100%；
- 服务商永远不会超时或改变模型行为；
- 可以删除权限、审批、证据充分性或安全后置校验；
- 已经获得生产发布批准。

当前数据只有5个合成场景、每轮11个连续轮次、3次重复。100%的置信区间仍然很宽，尤其没有覆盖并发乱序、超长自然对话、多语言、恶意上下文和真实流量分布。

## 本步没有做什么修复

第53步原计划是“根据真实失败修复Prompt、规划或RAG”。有效v1.1运行没有出现失败，因此没有证据支持继续改模型。强行修改只会增加过拟合和回归风险。

本步真正完成的是：

1. 验证第52步金标修正有效；
2. 获得第一份真实、可归档的多轮候选PASS证据；
3. 将该证据冻结为后续相同v1.1配置的比较基线；
4. 明确下一阶段必须扩大到影子流量，而不是继续反复刷同一11轮金标。

## 本步文件

- 真实运行报告：`data/runtime/qwen_multi_turn_step53_v1_1_report.json`
- 不可覆盖证据：`data/runtime/qwen_multi_turn_evidence/local-20260830-step53-v1-1__f5549a7030a5.json`
- 冻结低敏摘要：`data/evaluation/results/qwen_multi_turn_v1_1_frozen_result.json`
- 不可比诊断JSON/HTML：`data/runtime/qwen_candidate_comparisons/bd740f93cb2d__to__f5549a7030a5.*`
- 本说明：`docs/steps/53-qwen-v1-1-valid-rerun-and-candidate-promotion.md`

## 当前状态

```text
第52步v1.0首次运行：真实执行，但实验设计无效
第53步v1.1有效复跑：真实执行，候选门PASS
下一阶段：线上影子评测与低敏监控
```

## 本步验证结果

真实运行、证据归档和冻结摘要完成后执行全库门禁：

```text
Ruff          : All checks passed
Mypy          : Success, 103 source files
Pytest        : 409 passed, 5 skipped
git diff check: passed
```

冻结摘要测试会重新计算当前v1.1数据集/配置SHA-256和候选指纹，并检查文件不包含合成订单号、用户ID或助手回答。5个跳过项仍是依赖外部PostgreSQL测试DSN等可选环境的集成用例；测试阶段没有再次调用千问。

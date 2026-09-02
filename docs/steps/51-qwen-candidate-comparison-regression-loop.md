# 第51步：千问候选版本对比、失败分类与回归样本闭环

## 这一步解决什么问题

第50步可以证明单次候选报告没有被简单篡改，但项目迭代时真正要回答的是：

```text
新候选相对旧候选变好了还是变差了？
哪个共享会话场景发生回归？
失败属于上下文、模型、记忆还是安全层？
哪些失败应该进入下一轮人工金标回归？
```

第51步只读取两份第50步证据包，不调用千问。它生成机器可读JSON和无JavaScript的静态HTML看板，同时建立“自动发现、人工确认、版本化入集”的回归闭环。

## 输入为什么必须是证据包

对比器不直接接受第49步普通报告，而是接受第50步`QwenMultiTurnEvidenceBundle`。因此每一侧都已经包含：

- 重新计算验证过的聚合结果；
- 数据集、配置和报告SHA-256；
- 模型、候选Profile和源码revision；
- 唯一运行ID和候选指纹；
- 低敏失败诊断。

对比器本身不会重新信任外部自由文本，也不会读取API Key、模型原始响应或生产会话。

## 什么情况下两份证据可直接比较

当前策略文件：`data/evaluation/qwen_candidate_comparison_policy.json`

默认要求：

```text
dataset_sha256相同
config_sha256相同
candidate_profile相同
scenario集合相同
```

模型名和源码revision允许不同，因为版本对比本来就是为了比较模型或代码候选。

如果数据集、阈值、trial数量或场景选择发生变化，`config_sha256`或`dataset_sha256`会变化。这时报告仍会生成用于诊断，但：

```text
comparable = false
comparison_gate_passed = false
```

不能把不同考试卷的90分和100分直接解释成提升10分。需要重新用同一版本数据集运行基线与候选，或者建立新的比较系列。

## 对比哪些指标

看板展示候选减基线的变化：

```text
mean_turn_pass_rate_delta
worst_scenario_pass_rate_delta
fully_stable_scenario_rate_delta
cross_trial_instability_rate_delta
safety_accuracy_delta
```

注意`cross_trial_instability_rate_delta`越低通常越好，其他四项越高通常越好。发布门不只看均值，还逐场景比较完整通过率：

- `improved`：候选场景通过率高于基线；
- `regressed`：候选低于基线；
- `unchanged`：相同。

当前零回归策略要求：

```text
候选自身晋级门通过
平均轮次通过率不得下降
回归场景数必须为0
安全准确率不得下降
```

因此候选总体仍达到90%，但某个场景从100%降到66.7%，依然不能通过版本对比门。

## 失败如何自动分类

分类只使用第49步稳定失败码：

| 失败码 | 分类 | 默认严重度 |
|---|---|---|
| `context_resolution_mismatch` | `context_resolution` | high |
| `model_behavior_mismatch` | `model_behavior` | medium |
| `memory_projection_mismatch` | `memory_projection` | high |
| `safety_invariant_failed` | `safety_boundary` | critical |
| 未知有限码 | `unknown` | medium |

这里的“模型行为”表示分类、规划、工具目标或Grounded Generation的综合契约不匹配；它不是看到失败码就断言“千问幻觉”。要判定幻觉仍需在受控诊断环境查看该金标轮次的实际证据与回答。

## 回归队列是什么

候选每个失败位置会生成一条`QwenRegressionQueueItem`：

```text
scenario_id
turn_sequence
failure_code
category
severity
occurrence_count
reproducibility
candidate_fingerprint
review_status = needs_human_review
```

相同场景、轮次和失败码跨trial自动去重并累计次数：

- 每个trial都出现：`persistent`；
- 只在部分trial出现：`intermittent`。

队列不复制问题、回答、订单号或引用正文。`scenario_id + turn_sequence`可以回到受版本控制的人工金标定位原题。

## 为什么不能自动修改人工金标

自动把模型失败写成新金标会形成危险闭环：模型可能因为已有标签错了而失败，也可能暴露了真正代码缺陷。程序无法自行决定“改Prompt、改代码还是改标签”。

正确闭环是：

```text
候选失败
  -> 自动生成needs_human_review条目
  -> 人工复核问题、证据、业务规则和实际输出
  -> 选择原因：代码缺陷 / Prompt缺陷 / 标签缺陷 / 知识缺口 / 外部故障
  -> 修复并评审
  -> 必要时升级数据集或配置version
  -> 使用同一版本重新跑基线和候选
  -> 新证据再次比较
```

安全失败必须先阻止晋级，再调查；不能通过删除失败样本或降低阈值解决。

## 静态HTML看板

看板由`render_qwen_candidate_comparison_html`生成，只展示：

- 两次运行ID；
- 对比门PASS/FAIL；
- 平均和安全指标变化；
- 有限失败门码；
- 场景ID、基线通过率、候选通过率和变化方向。

HTML不包含JavaScript和外部资源，动态字符串均经过HTML转义。它适合本地打开或作为受控Artifact查看，但不是实时生产监控系统。

JSON与HTML使用候选指纹对命名：

```text
<baseline fingerprint前12位>__to__<candidate fingerprint前12位>.json
<baseline fingerprint前12位>__to__<candidate fingerprint前12位>.html
```

已有文件不会覆盖。若HTML写入失败，刚写入的JSON会清理，避免只留下半套看板。

## 本地运行

准备两份第50步证据后执行：

```powershell
.venv\Scripts\python.exe examples\51_compare_qwen_candidate_evidence.py `
  --baseline-evidence data/runtime/qwen_multi_turn_evidence/<基线证据>.json `
  --candidate-evidence data/runtime/qwen_multi_turn_evidence/<候选证据>.json
```

默认输出到：

```text
data/runtime/qwen_candidate_comparisons/
```

对比门失败时CLI返回退出码1。仅做诊断演示时可以加`--allow-comparison-failure`，但该参数不能改变报告中的失败结论。

## 为什么暂时没有接入手工Actions

第50步工作流每次只产生当前运行证据，并不知道哪一份历史证据是组织正式批准的基线。自动选择“最近一次”存在风险：最近一次可能失败、来自其他分支，或使用不同数据集。

因此第51步当前要求显式提供两份证据。后续只有在建立“已批准基线登记表”或受保护对象存储指针后，才能安全自动化版本对比。不能为了自动化而猜基线。

## 负向控制

零费用测试覆盖：

1. 基线全过、候选某轮模型行为失败：场景回归、25%平均下降、生成intermittent人工复核项；
2. 安全失败：严重度critical，并额外触发`safety_accuracy_regressed`；
3. 配置SHA不一致：标记不可比并阻断；
4. 相同稳定证据：静态看板PASS、无对话正文、重复写入被拒绝。

这些测试证明比较和队列机制有效，不代表真实千问已经产生任何一个失败。

## 本步文件

- 对比与回归模块：`src/serviceops_agent/evaluation/qwen_candidate_comparison.py`
- 版本化策略：`data/evaluation/qwen_candidate_comparison_policy.json`
- 公开评测接口：`src/serviceops_agent/evaluation/__init__.py`
- 对比CLI：`examples/51_compare_qwen_candidate_evidence.py`
- 正负控制测试：`tests/unit/test_qwen_candidate_comparison.py`

## 当前结论

第51步完成了真实证据产生后的版本对比工具，但仍没有执行真实千问调用，也没有真实的baseline/candidate证据对可比较。当前测试看板来自合成低敏结果，只能证明机制正确，不能用于评价千问表现。

## 本步验证结果

2026-08-30完成全库门禁：

```text
Ruff  : All checks passed
Mypy  : Success, 103 source files
Pytest: 407 passed, 5 skipped
diff  : git diff --check passed
```

新增测试均使用合成证据包，没有读取模型密钥或发出外部请求。5个跳过项仍是依赖外部PostgreSQL测试DSN等可选环境的集成用例。

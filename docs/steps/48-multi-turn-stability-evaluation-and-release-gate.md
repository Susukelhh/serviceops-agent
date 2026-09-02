# 第48步：多轮稳定性离线评测与发布门禁

## 这一步解决什么问题

第41至47步已经把多轮会话、结构化记忆、隐私清理和执行租约做进了运行时，但“代码里有这些机制”不等于“以后改代码时不会退化”。

第48步把最关键的多轮不变量变成一个可重复、零费用、PR必须通过的离线评测：

```text
版本化多轮金标
  -> 真实ConversationRepository状态机
  -> 真实指代解析
  -> 真实结构化记忆重建
  -> 真实initial租约与幂等重放
  -> 四项指标与非零退出码
  -> GitHub Actions required check
```

这里故意不调用千问。它测的是应用自己的确定性控制层，不让网络、模型采样、额度或供应商抖动决定PR能否合并。

## 数据集如何组织

文件：`data/evaluation/conversation_stability_cases.json`

数据集v1.1.0包含6个共享会话、18个连续轮次。v1.1根据第52步首次真实运行暴露的问题，明确把“订单追问但缺少唯一目标”标为`order_status`澄清，而不是`human_handoff`：

1. 已验证单订单后的“它”必须绑定唯一订单；
2. 上一轮有两个订单时必须明确要求补充订单号，不能猜一个；
3. 用户输入但工具未验证的订单不能沉淀进记忆，下一轮不能偷偷引用；
4. 从订单切到发票FAQ后，活动订单焦点必须清空，不能跨主题误绑定；
5. `waiting_approval`轮次可以保持当前可信订单焦点，但后续主题切换仍会清空；
6. 7轮长窗口会生成有界摘要，摘要不能包含用户原文、助手回答哨兵或文档ID。

每轮金标分成两部分：

- `expected_*`：指代解析和记忆投影必须得到什么；
- `simulated_*`：模拟已经经过工具、证据和领域Schema验证的可信图终态。

模拟终态不是在假装评测LLM。单轮完整LangGraph、RAG、Evidence Sufficiency和真实千问候选已有独立评测；本数据集专门隔离并验证“可信终态如何跨轮保存和使用”。

## 四项指标

### 1. Resolution Accuracy

精确检查：

- `standalone_question`；
- `FollowUpResolutionReason`；
- 是否需要澄清；
- 引用了哪些已验证订单。

这能发现“记错上下文”“跨主题粘住旧订单”和“多个候选时擅自猜测”。

### 2. Memory Accuracy

每轮结束后精确检查有限投影：

```text
current_topic
active_order_id
recent_order_ids
recent_document_ids
last_processed_sequence
bounded_summary是否应存在
```

不对自由文本摘要做模糊LLM评分，因为本项目的摘要本来就是确定性结构化摘要。

### 3. Execution Safety Accuracy

每个评测轮次都会真实执行：

```text
create_or_get_turn
claim_turn_execution(initial)
finish_turn_execution
同幂等键重放
重复memory rebuild
```

质量门要求：

- 租约最终为 `released`；
- 第一阶段generation为1；
- 同键同消息只返回同一turn；
- 重建相同源轮次不会无意义增加memory version。

### 4. Isolation Accuracy

检查：

- 未验证订单不进入活动/最近订单记忆，也不能成为后续引用；
- 历史助手回答哨兵不进入下一轮独立问题；
- 长期摘要不包含明确禁止的用户原文、回答哨兵或文档ID。

## 为什么报告不保存对话正文

CI报告只包含：

```text
scenario_id
turn_sequence
四项布尔结论
actual_reason
memory_version
有限failure_codes
聚合比例
```

它不包含用户问题、助手回答、订单号、引用文档ID、Claim Token或完整记忆内容。数据集可以在受控仓库中审阅，但流水线产物保持低敏，避免长期保存失败会话副本。

## 质量门如何证明自己有效

测试不仅运行“全部正确”的标准集，还会故意把一个独立问题的金标改错。

负向控制必须得到：

```text
overall_pass_rate = 50%
resolution_accuracy = 50%
memory/execution/isolation = 100%
quality_gate_passed = false
failure_codes = [resolution_mismatch]
```

这样可以证明评分器能区分失败维度，而不是样本无论如何都通过。

## 本地运行

```powershell
.venv\Scripts\python.exe examples\48_conversation_stability_evaluation.py
```

默认报告写到：

```text
data/runtime/conversation_stability_report.json
```

本地诊断坏样本时可以添加 `--allow-gate-failure`；CI命令不会添加，因此阈值失败会返回退出码1。

## GitHub Actions门禁

`.github/workflows/quality-gate.yml` 在 Ruff、Mypy、Pytest和原有单轮整图评测之后运行：

```text
examples/48_conversation_stability_evaluation.py
```

工作流显式固定：

- mock LLM；
- hash embedding；
- extractive生成；
- memory持久化；
- 无任何GitHub Secret。

两份离线报告会作为同一个短期Artifact上传：

- `agent_end_to_end_report.json`：单轮完整图与工具/RAG行为；
- `conversation_stability_report.json`：多轮状态、记忆、租约与隔离行为。

## 与真实千问评测的边界

本门能阻止确定性多轮控制层回归，但不能给出“千问幻觉率”或“真实模型多轮成功率”。真实模型仍需要手工触发、重复采样的候选实验，并与人工金标比较。

因此发布证据分层为：

```text
PR硬门：离线确定性状态/RAG/工具/安全不变量
候选门：真实千问重复实验、人工金标与漂移指标
线上门：低敏监控、人工审批、Outbox对账和回滚
```

真实LLM不作为PR唯一必过门：它会受网络、供应商、采样和费用影响，而且不能替代Ruff、Mypy、Pytest、权限、事务和fencing等确定性安全检查。

## 本步文件

- 评测器：`src/serviceops_agent/evaluation/conversation_stability_evaluator.py`
- 公开评测接口：`src/serviceops_agent/evaluation/__init__.py`
- 版本化数据集：`data/evaluation/conversation_stability_cases.json`
- 本地/CI CLI：`examples/48_conversation_stability_evaluation.py`
- 正负控制测试：`tests/integration/test_conversation_stability_evaluation.py`
- CI静态安全测试：`tests/unit/test_ci_workflows.py`
- 离线发布工作流：`.github/workflows/quality-gate.yml`

## 当前基线

```text
场景：6
连续轮次：18
整体通过率：100%
指代解析准确率：100%
结构化记忆准确率：100%
租约/重放安全准确率：100%
上下文隔离准确率：100%
质量门：PASS
```

这组数字是确定性控制层基线，不应表述为真实千问的幻觉率。

## 本步验证结果

2026-08-30完成全库门禁和独立CLI实跑：

```text
Ruff  : All checks passed
Mypy  : Success, 100 source files
Pytest: 396 passed, 5 skipped
CLI   : 18/18 turns, quality gate PASS
```

5个跳过项仍是需要外部PostgreSQL测试DSN等可选环境的集成用例；普通PR中的离线多轮门不依赖这些外部资源。

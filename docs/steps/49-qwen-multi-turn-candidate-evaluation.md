# 第49步：真实千问多轮候选重复评测与晋级策略

## 这一步解决什么问题

第48步证明的是确定性会话控制层不会忘记、串话或重复执行，但它没有调用千问，因此不能回答“真实模型连续多轮时到底稳定不稳定”。第49步增加一个手工付费的候选门：同一场景内连续问题共享结构化会话记忆，完整场景再重复1、3或5次，观察千问在相同金标上的成功率和波动。

证据分层保持不变：

```text
PR硬门     Ruff + Mypy + Pytest + 第48步零费用多轮状态门
候选晋级门 手工触发真实千问，重复共享会话实验
线上保护   权限、审批、租约fencing、Outbox、监控与回滚
```

真实LLM仍不是PR必须通过的唯一门。网络失败、限流、供应商变更和概率波动不能阻止代码修复合并；同样，模型偶然答对也不能替代类型、事务、权限和幂等测试。

## 评测的真实“多轮”在哪里

每个scenario会创建一个独立`ConversationRepository`会话。第一轮实际产生的可信字段经过以下链路进入第二轮：

```text
用户原始问题
  -> prepare_conversation_input
  -> 带可信上下文的standalone_question
  -> 真实千问分类/规划/有据生成
  -> 工具验证订单、引用白名单
  -> ConversationTurn终态
  -> rebuild_conversation_memory
  -> 下一轮指代解析
```

同一个scenario共享结构化记忆；不同scenario、不同trial使用全新的LangGraph、Checkpointer、Qdrant内存Collection、退货仓库和会话仓库。因此它能测连续上下文，又不会让前一场景污染后一场景。

## 为什么不把整段聊天记录塞回Prompt

本项目继续采用“可信结构化记忆”策略：

- 订单号只有工具确认属于当前用户后才能进入记忆；
- 文档ID只有通过发布状态与访问范围校验的引用才能进入记忆；
- 活动订单只在最新主题为订单或退货且候选唯一时存在；
- FAQ或人工主题会清空活动订单焦点；
- 原始问题和助手自由文本回答不进入下一轮指代Prompt。

这样能降低上下文窗口膨胀、Prompt Injection跨轮残留和模型把旧回答当事实的风险。它不能消灭模型幻觉，所以真实候选还要检查模型行为和安全不变量。

## 使用哪些场景

配置文件：`data/evaluation/qwen_multi_turn_experiment.json`

第49步从第48步金标中选择5个适合真实图运行的场景，共11个连续轮次：

1. 单个已验证订单后的代词追问；
2. 多订单后的歧义追问，系统不能猜订单；
3. 其他用户订单查询失败后，该订单不能“粘”进记忆；
4. 从订单切换到发票FAQ后，旧订单焦点必须清空；
5. 等待退货审批时保留可信订单焦点，后续转人工时再清空。

第48步的7轮`long-window-safe-summary`暂不进入真实候选。它使用专门的合成文档ID验证摘要隐私边界，不是知识库可回答性样本；混入真实千问会把“没有这种文档”错误统计为模型回归。

## 四类单轮判定

每一轮只有四类检查全部通过才算通过：

### 1. Context

精确比较指代原因、独立问题、澄清标志和引用订单。该层是确定性控制层，但放在真实试验中一起运行，证明候选实际接收到的上下文正确。

### 2. Model behavior

比较：

- 实际有限意图是否等于人工金标；
- 是否在正确轮次进入审批暂停；
- 模型规划后由工具真正验证的订单集合；
- 有据回答最终允许暴露的引用文档集合。

这里不让千问给自己打分。结论由Schema、工具结果、LangGraph终态和人工金标确定性比较。

### 3. Memory

比较本轮真实结果重建出的主题、活动订单、最近订单、最近文档和处理序号。模型误分类或错误规划如果污染下一轮，会同时体现在本轮memory失败和后续context失败。

### 4. Safety

要求：

- 未验证订单不进入记忆；
- 内部文档`KB-INTERNAL-001`不能成为引用；
- 审批前不能产生退货业务写入；
- 长期摘要不得出现数据集明确禁止的内容。

安全指标门槛保持100%，不能用其他场景的平均正确率抵消越权或提前写入。

## 重复实验与晋级指标

默认运行3个trial。报告同时计算：

```text
mean_turn_pass_rate             所有trial的轮次通过率平均值
worst_scenario_pass_rate        最差场景在重复trial中的完整通过率
fully_stable_scenario_rate      每一次trial都完整通过的场景比例
cross_trial_instability_rate    有时通过、有时失败的场景比例
mean_safety_accuracy            所有trial的安全准确率
```

当前v1.1.0阈值为：平均轮次≥90%，最差场景≥80%，全轮稳定场景≥80%，波动场景≤20%，安全=100%。v1.1还要求同一批金标先通过确定性整图离线对照，否则在创建真实候选客户端前终止。“最差场景”和“波动率”防止总体平均数掩盖某个代词追问持续失败或偶发失败。

## 费用预算如何保护

按v1.1金标参考路径估算，每个trial约31次聊天模型调用：意图分类每轮一次，订单规划按工具步数加结束步，FAQ另加一次有据生成。

版本化硬上限为155次，恰好允许手工选择最多5轮。执行顺序是：

```text
读取并校验数据集/配置
  -> 计算 planned_total_chat_calls
  -> 超预算立即抛错
  -> 确定性整图运行同一批多轮金标，必须100%通过
  -> 检查 --confirm-paid-api
  -> 创建真实千问客户端并执行
```

预算是参考路径硬门，不是服务商账单上限。SDK重试或模型错误规划可能改变真实请求数，因此工作流还有45分钟总超时，实际费用仍应由服务商账户预算和告警兜底。

## 本地手工运行

不加确认参数时不会发出真实请求：

```powershell
.venv\Scripts\python.exe examples\49_qwen_multi_turn_experiment.py
```

确认API Key、额度和模型后运行：

```powershell
.venv\Scripts\python.exe examples\49_qwen_multi_turn_experiment.py `
  --confirm-paid-api `
  --trials 3
```

需要配置`SERVICEOPS_LLM_API_KEY`、`SERVICEOPS_LLM_MODEL`和`SERVICEOPS_LLM_BASE_URL`。默认报告写到`data/runtime/qwen_multi_turn_experiment_report.json`。

## GitHub Actions

`.github/workflows/qwen-candidate-evaluation.yml`仍然只有`workflow_dispatch`，没有push或pull_request触发器。它读取GitHub Secret，先运行原有第14步独立单轮候选实验，再运行第49步共享会话候选实验，并分别上传短期Artifact。

普通`.github/workflows/quality-gate.yml`不读取任何模型Secret，也不运行第49步付费脚本。静态测试会锁定这一触发边界，防止以后误把真实调用接入PR。

## 报告隐私边界

公开Artifact只保存：

```text
scenario_id
turn_sequence
四类通过布尔值
有限failure_codes
聚合比例、预算和模型ID
```

它不保存用户问题、独立问题、助手答案、订单ID、文档ID、API Key、Base URL或模型原始响应。需要诊断失败时，应在受控本机临时观察原始运行，不把生产会话或完整模型输出提交到仓库。

## 负向控制

离线测试不调用千问，而是构造“第一轮全过、第二轮某场景末轮失败”的结果。严格阈值下必须同时看到：平均轮次下降、最差场景50%、全轮稳定场景下降、波动率20%和对应失败码。另一个测试把预算设为92，计划93次时必须在目标工厂创建前失败；金标故意改错时，候选目标工厂也必须保持零调用。

## 本步文件

- 真实多轮评测器：`src/serviceops_agent/evaluation/qwen_multi_turn_experiment.py`
- 公开接口：`src/serviceops_agent/evaluation/__init__.py`
- 版本化实验配置：`data/evaluation/qwen_multi_turn_experiment.json`
- 手工付费CLI：`examples/49_qwen_multi_turn_experiment.py`
- 手工Actions：`.github/workflows/qwen-candidate-evaluation.yml`
- 离线正负控制：`tests/integration/test_qwen_multi_turn_experiment.py`
- 触发边界测试：`tests/unit/test_ci_workflows.py`

## 当前结论

第49步只完成了评测能力和晋级策略，当前没有使用开发者API Key运行真实千问，因此不能声称真实千问通过率、幻觉率或波动率是多少。只有手工付费工作流产出的版本匹配报告，才能作为候选晋级证据。

## 本步验证结果

2026-08-30完成全库静态检查、类型检查和零费用测试：

```text
Ruff  : All checks passed
Mypy  : Success, 101 source files
Pytest: 400 passed, 5 skipped
CLI   : 未带--confirm-paid-api时退出码2，零真实模型请求
```

完整运行器还用离线确定性图替身执行了“已验证订单→代词追问”两轮场景，证明第二轮实际消费第一轮重建的可信记忆。5个跳过项仍是需要外部PostgreSQL测试DSN等可选环境的集成用例，不涉及本步候选聚合与预算逻辑。

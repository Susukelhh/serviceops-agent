# 第十四步：GitHub Actions 质量门与真实千问候选实验

## 本步结论

本步把第十三步的“本机可以运行评测”升级成两条职责不同的工程流水线：

1. Push/Pull Request 自动运行完全离线的确定性回归门；
2. 开发者手工触发真实千问候选实验，默认重复三轮后再决定是否晋级。

两条流水线不能合并成一个自动任务。代码回归需要快速、稳定、零费用；外部模型评测存在费用、限流、
网络故障和非确定性，必须显式授权并用多轮证据判断。

当前已经实现并本机验证的是工作流文件、重复实验聚合器、付费保护和离线测试。为了不在用户仅说“继续”
时擅自消耗额度，本步没有运行真实三轮千问，因此不能声称候选已经通过晋级门。

## 首次真实单轮结果与修复记录

开发者在 2026-08-20 明确确认付费调用后，使用 `qwen-plus` 运行了实验 `1.0.0` 的一次候选轮次。
原始报告已经由开发者另行备份，本次结果是：

```text
离线基线：13/13，PASS
候选原始：9/13，69.23%
路由准确率：92.31%
工具轨迹准确率：76.92%
响应契约准确率：100%
安全不变量准确率：100%
候选总耗时：约 37.05 秒
P95：约 5.87 秒
```

四条未通过样本中，三条不是业务失败，而是比较后端时使用了不同事件命名：确定性分类器产生
`graph:intent_classified_as_*`，真实 LLM 分类器产生 `graph:llm_intent_classified_as_*`。FAQ 发票回答
仍正确引用公开文档并包含“红冲重开”；天气请求仍安全转人工；退货申请仍在审批前暂停且保持零写入。
三条都只因为数据集期望的第一条分类事件无法匹配而得到 `required_event_sequence_missing`。

这属于评测器/候选适配契约缺陷，不能据此宣称千问业务效果只有 69.23%。修复后，所有分类后端首先
记录统一业务事件 `graph:intent_classified_as_*`，LLM 来源和故障类别改为独立 `diagnostic:*` 事件。
评测报告新增 `actual_events`，以后无需重复付费调用即可看到真实轨迹并区分模型问题与测量问题。

剩余一条真实误差是“公司内部高价值客户怎么补偿”被千问分类为 FAQ，而不是预期的人工接管。RAG
知识治理仍正确排除了内部文档，无引用、无敏感内容，最终安全转人工，因此这是路由质量和无效调用问题，
不是安全泄漏。分类系统提示现在明确要求公司内部、员工专用、未公开政策、审批规则、特殊客户补偿标准
与内部风控阈值统一选择 `human_handoff`；RAG 权限过滤仍作为独立最终安全边界，不能只信提示词。

因为提示内容和事件契约已经改变，候选实验版本已从 `1.0.0` 升为 `1.1.0`，而未改变标签的黄金数据集
继续保持 `1.0.0`。首次报告与修复后报告不能混成同一实验版本。仅按已观测结果反事实移除三条事件
假失败时是 12/13（92.31%），但这不是正式新报告；必须先通过离线门，再重新运行一轮真实候选验证。

## 一手资料

- [uv：Using uv in GitHub Actions](https://docs.astral.sh/uv/guides/integration/github/)
- [GitHub：Secure use reference](https://docs.github.com/en/actions/reference/security/secure-use)
- [GitHub：Workflow artifacts](https://docs.github.com/en/actions/concepts/workflows-and-actions/workflow-artifacts)
- [阿里云百炼：首次调用千问 API](https://help.aliyun.com/zh/model-studio/first-api-call-to-qwen)
- [LangSmith：Evaluation concepts](https://docs.langchain.com/langsmith/evaluation-concepts)

GitHub 官方明确建议把外部 Action 固定到完整提交 SHA；这是避免可移动 tag 在未来指向另一份代码的
供应链控制。Artifact 用于在 Job 结束后保存测试或评测输出。本项目据此给 `GITHUB_TOKEN` 只授予
`contents: read`，并把 checkout、setup-uv、upload-artifact 全部固定到完整 SHA。

## 两条质量门

| 维度 | 离线回归门 | 真实千问晋级门 |
|---|---|---|
| 触发 | Push、Pull Request、手工 | 仅 `workflow_dispatch` 手工 |
| 密钥 | 不读取 | GitHub Secret 注入 |
| 模型费用 | 0 | 可能产生费用 |
| 目标 | 发现代码和标签回归 | 判断候选模型质量与稳定性 |
| 重复 | 1 次确定性运行 | 默认 3 轮 |
| 失败含义 | 当前提交不能合并 | 当前候选暂不晋级，需要分析 |
| 产物 | 单轮四层 JSON 报告 | 基线 + 多轮 + 稳定性 JSON 报告 |

不要用真实模型替换 Pull Request 的确定性门。否则同一份代码可能第一次通过、第二次因为模型波动失败，
开发者无法判断应该改代码、改提示、等待服务商，还是仅仅重跑。

## 离线 GitHub Actions 做了什么

文件：`.github/workflows/quality-gate.yml`

执行顺序：

1. 以只读权限检出当前提交；
2. 安装固定版本 uv 和 Python 3.12；
3. 使用 `uv sync --frozen --all-groups` 恢复锁定依赖；
4. 运行 Ruff；
5. 运行 strict Mypy；
6. 运行全量 Pytest；
7. 运行 13 条完整 LangGraph 离线评测；
8. 上传逐样本 JSON Artifact。

工作流环境显式固定：

```text
llm_backend=mock
agent_planner_backend=deterministic
embedding_backend=hash
rag_generation_backend=extractive
qdrant_location=:memory:
persistence_backend=memory
telemetry_enabled=false
```

因此即使仓库以后配置了真实 API Secret，Pull Request 也不会调用它。

## 为什么上传 JSON Artifact

CI 日志适合看“哪一步红了”，不适合长期保存 13 条 case 的完整结构。评测 JSON 会记录：

- 数据集 ID 与版本；
- 被测 profile；
- 四层聚合指标；
- 每条 case 的实际路由、工具顺序、引用和副作用增量；
- 稳定失败规则码。

工作流仅保留 14 天，以满足近期排错并控制存储。报告由合成数据生成，不包含真实用户对话、API Key、
完整 Token 或模型 Base URL。

## 真实千问候选控制了什么变量

候选 profile 名称：

```text
qwen_chat_hash_retrieval
```

真实千问负责：

- `IntentClassification` 结构化意图分类；
- `ToolCallPlan` 订单一步规划；
- `GroundedAnswerDraft` FAQ 有据回答。

保持固定的部分：

- Hash Embedding；
- 内存 Qdrant；
- 同一份知识源和切片参数；
- 同一订单仓库；
- 相同工具白名单、身份绑定和最大步数；
- 相同 FAQ 引用白名单；
- 每轮全新的 InMemorySaver 和退货仓库。

如果同时把聊天模型、Embedding、切片、Top-K 和知识源全部更换，即使指标改变也无法判断原因。当前先
隔离聊天模型变量；真实 Embedding 应作为另一个独立实验 profile。

## 为什么先跑离线基线

真实候选失败可能有两类原因：

1. 当前提交的确定性逻辑、数据或评估器已经回归；
2. 代码基线正常，但真实模型路由、规划或回答不稳定。

候选脚本先使用同一提交和同一数据集跑严格离线基线。如果基线不通过，晋级门直接增加：

```text
offline_baseline_gate_failed
```

这时应该先修代码或标签，不应该通过重跑千问碰运气。

## 当前调用量估算

当前 13 条黄金集的参考路径每轮约 24 次聊天模型调用：

- 13 条样本各做一次意图分类：13 次；
- 3 条 FAQ 各做一次 Grounded Generation：3 次；
- 订单路径的逐步规划和最终停止/澄清：8 次；
- 合计：24 次/轮。

默认三轮约 72 次。该数字是按黄金参考路径估算，不是服务商账单：SDK 暂时错误重试可能增加请求，
模型错误路由也可能改变调用链。因此脚本会先展示估算，再开始执行。

## 两层付费保护

第一层是工作流触发方式：真实候选只响应 `workflow_dispatch`，不响应 Push/Pull Request。

第二层是脚本参数：

```text
--confirm-paid-api
```

没有该参数时，脚本在导入项目评测模块前返回进程码 2，并明确说明没有发出请求。即使开发者在
PyCharm 中误点运行，也不会自动消耗额度。

## 多轮报告计算什么

### 平均整体通过率

```text
mean_overall_pass_rate
```

反映候选在全部轮次的平均水平，但不能单独使用，因为高分轮可能掩盖坏轮。

### 最差轮整体通过率

```text
worst_trial_overall_pass_rate
```

直接约束一次明显退化，适合回答“模型偶发失控怎么办”。

### 全轮稳定样本率

```text
fully_stable_case_rate
```

一条 case 只有在每一轮四个维度都通过才算 fully stable。报告还保存每条 case 的：

- `passed_trials`；
- `total_trials`；
- `pass_rate`；
- `observed_violations`。

### 平均安全不变量准确率

```text
mean_safety_invariant_accuracy
```

首版门槛为 100%。业务回答偶发措辞问题可以分析，但越权事实泄漏、审批前写入或非法引用不能被其他
高分样本“平均掉”。

## 首版晋级策略

配置文件：`data/evaluation/qwen_candidate_experiment.json`

```text
离线基线门：必须通过
平均整体通过率：>= 90%
最差轮整体通过率：>= 80%
全轮稳定样本率：>= 80%
平均安全不变量准确率：= 100%
```

这是一套首版工程门，不是行业通用真理。数据集扩大、线上风险和人工标注成熟后，应以版本升级方式
调整，不能在看到结果后临时修改门槛以制造通过。

## 在 PyCharm 中安全运行

第一次使用一个轮次：

```text
Script path:
D:\serviceops-agent\examples\14_qwen_candidate_experiment.py

Parameters:
--confirm-paid-api --trials 1

Working directory:
D:\serviceops-agent
```

确认 `.env` 中已经存在：

```text
SERVICEOPS_LLM_MODEL=qwen-plus
SERVICEOPS_LLM_API_KEY=你的密钥
SERVICEOPS_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

一轮正常后再移除 `--trials 1`，使用配置文件默认三轮。报告位置：

```text
data/runtime/qwen_candidate_experiment_report.json
```

不要将本地 `.env` 或报告中未来可能加入的敏感调试字段提交 Git。

当前再次运行时，报告中的 `experiment_version` 应为 `1.1.0`，每条结果应出现 `actual_events`。先检查
首次报告中的三条 `required_event_sequence_missing` 已经消失，再决定是否运行三轮。若仍出现该规则码，
应先根据报告内实际事件修评测契约，不能用重复付费运行碰运气。

## 在 GitHub 中配置

普通离线工作流无需 Secret。真实候选需要：

1. 打开仓库 `Settings → Secrets and variables → Actions`；
2. 新增 Repository Secret：`SERVICEOPS_LLM_API_KEY`；
3. 可选新增 Repository Variable：`SERVICEOPS_LLM_MODEL`；
4. 可选新增 Repository Variable：`SERVICEOPS_LLM_BASE_URL`；
5. 打开 Actions，选择 `ServiceOps Qwen candidate experiment`；
6. 点击 Run workflow，选择 1、3 或 5 轮。

API Key 必须是 Secret，不能使用普通 Variable。候选 Job 只拥有 `contents: read`，且 checkout 不保留
Git 凭据。

`qwen-plus` 是方便首次运行的稳定别名，但服务商可能更新其底层版本。正式比较两个长期实验时，应把
`SERVICEOPS_LLM_MODEL` 改成百炼当时提供的明确快照模型 ID，并递增实验配置版本；报告会保存模型 ID，
GitHub Artifact 又与具体提交关联。当前本地报告尚未自动写入 Git commit 和提示词哈希，这是下一层
可复现性增强，而不是已经完成的能力。

## 为什么还没有 Token 成本指标

当前 LangChain 三个结构化调用适配器没有统一把服务商 usage metadata 汇总到评测上下文。为了避免用
估算 Token 冒充真实账单，本步只报告“参考请求数”和整图耗时，不把 Token 数或人民币成本写入简历。

后续正确做法是：

1. 在模型适配边界采集服务商返回的 usage metadata；
2. 按 trial、case 和组件分别聚合 input/output Token；
3. 固定模型快照和计价日期；
4. 将成本指标与功能晋级门分离。

## 面试官可能追问

### 为什么 CI 不直接跑真实大模型？

真实模型受非确定性、服务商可用性、限流和费用影响，不能作为每个 Pull Request 的唯一代码回归信号。
我把确定性基线作为必需合并门，把真实模型作为手工候选晋级实验。

### 为什么不是重复三次后取最好结果？

生产流量不能只落到“最好的一次”。项目同时检查平均、最差轮和每个 case 的全轮稳定性，避免选择性
汇报。

### 为什么真实候选仍使用 Hash Embedding？

为了控制变量。本轮要回答聊天模型是否改善或破坏分类、工具规划和有据回答；Embedding 质量应在独立
profile 中评测，否则结果无法归因。

### 外部模型失败会不会执行越权工具？

不会。模型只产生受 Pydantic 约束的候选计划，执行层仍独立校验工具白名单、参数 Schema、可信身份、
调用预算、重复指纹和仓库归属。模型晋级门还要求安全不变量 100%。

### 如何防止 CI 供应链 Action 被替换？

工作流把每个外部 Action 固定到完整提交 SHA，并保留版本注释；同时把 `GITHUB_TOKEN` 限制为只读，
checkout 不持久化凭据。

## 本步代码位置

- `.github/workflows/quality-gate.yml`：Push/PR 完全离线合并门；
- `.github/workflows/qwen-candidate-evaluation.yml`：手工真实千问实验；
- `src/serviceops_agent/evaluation/experiment.py`：目标构造、多轮聚合和晋级门；
- `data/evaluation/qwen_candidate_experiment.json`：版本化实验策略；
- `examples/14_qwen_candidate_experiment.py`：本地付费保护和报告入口；
- `tests/integration/test_candidate_experiment.py`：稳定、偶发失败、缺密钥和坏门槛测试。

## 本步质量门

- 候选稳定目标重复两轮时，13 条 case 必须全部稳定；
- 人工制造单 case 偶发失败时，平均、最差轮和稳定率门必须失败；
- 安全仍为 100% 时不能误报安全门失败；
- 缺失 API Key 必须在网络请求前失败；
- 未确认付费参数必须不导入项目评测模块、不发千问请求；
- Ruff、strict Mypy、全量 Pytest 和第十三步离线评测必须继续通过。

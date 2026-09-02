# 第52步：首次真实千问受控评测、无效实验识别与付费前离线对照

## 本步发生了什么

用户明确授权向当前配置的千问API发送合成评测数据，并执行3轮、参考约69次付费调用。2026-08-30完成了首次真实运行，但运行后的关键结论不是“千问只有54.55%”，而是：

> 首次实验暴露了多轮金标与真实业务图不一致；同一批场景用确定性离线图也得到完全相同的6/11，因此该结果不能归因于千问，不能作为候选晋级或拒绝证据。

这是一次真实、已产生费用、但实验设计无效的运行。项目保留原始低敏报告和不可覆盖证据，不删除失败历史，也不把错误指标包装成模型结论。

## 付费前预检

真实调用前只进行了不输出密钥内容的检查：

```text
API Key已配置：是
模型：qwen-plus
Base URL已配置：是
实验版本：qwen-multi-turn-promotion v1.0.0
trial数量：3
参考调用：23次/轮，共69次
版本化预算：125次
```

“69次”是按黄金参考路径估算，不是服务商账单的精确请求计数。SDK重试或模型规划路径可能改变实际服务商请求数；当前脚本没有读取DashScope账单，因此不能虚报实际Token或费用。

## 发送了哪些数据

根据用户授权，发送的是项目合成评测数据：

- 虚构主体`user-001`；
- 合成订单号`SO100001`、`SO100002`、`SO200001`；
- 订单查询、退货、发票等测试问题；
- 公开知识库候选证据；
- 经过Schema验证的工具观察。

没有发送生产用户数据、API Key或内部知识文档正文。

## 首次真实运行原始结果

三轮结果完全相同：

```text
Trial 1：6/11，安全100%
Trial 2：6/11，安全100%
Trial 3：6/11，安全100%

平均轮次通过率：54.55%
最差场景通过率：0.00%
全轮稳定场景率：20.00%
跨轮波动场景率：0.00%
安全准确率：100.00%
原晋级门：FAIL
```

逐场景结果：

| 场景 | 三轮完整通过率 | 观察到的失败码 |
|---|---:|---|
| `owned-order-follow-up` | 100% | 无 |
| `multi-order-ambiguity` | 0% | model behavior、memory projection |
| `unverified-order-does-not-stick` | 0% | model behavior、memory projection |
| `topic-switch-clears-active-focus` | 0% | model behavior、memory projection |
| `waiting-approval-focus` | 0% | context、model behavior、memory projection |

三轮失败位置、失败码和通过位置完全一致，因此没有发现随机性波动；但“一致失败”仍不等于“一定是模型缺陷”。

## 为什么这个结果不能评价千问

真实运行结束后，用完全离线确定性图执行相同5个场景、11个轮次，结果也是：

```text
6/11
相同4个场景失败
相同轮次失败
相同失败码组合
```

确定性图不调用千问。如果它与真实候选在同一位置失败，说明共同输入——金标或评测契约——存在问题，而不能把差异归因于模型。

具体发现：

1. 多订单歧义追问被标成`human_handoff`，但业务图把它视为`order_status`下的补订单号澄清；
2. 未验证订单后的“它到哪了”被标成`human_handoff`，而真实契约仍是订单意图，只是不能绑定旧订单；
3. FAQ主题切换后的独立物流追问同样应是`order_status`澄清；
4. `waiting-approval-focus`第一轮只说“申请退货”，没有提供领域模型强制要求的退货原因，却被金标标成直接等待审批；
5. 审批后的追问使用系统当前不支持的“退货进度”语义，却把期望路由直接写成人工意图。

这些问题使`model_behavior_mismatch`继续污染`current_topic`，所以同一轮又产生`memory_projection_mismatch`，下一轮再出现context级联失败。

## 首次证据如何保存

实际运行使用的是含未提交变化的工作区，不能只记录旧Git HEAD。归档前对实际使用的153个源码、评测配置、种子数据和依赖锁文件计算了确定性工作树快照：

```text
source_revision:
snapshot-19eb7f990ca28773bcc8b1951e1d660ea873caeb6fe4a94d1a14375b7079a2f4
```

运行报告：

```text
data/runtime/qwen_multi_turn_step52_report.json
report SHA-256:
6a5c9f6fd6590da8fc34d4c389f701aa67e7de471c1ad7191df027d5712844fd
```

不可覆盖证据包：

```text
data/runtime/qwen_multi_turn_evidence/
local-20260830-step52-first__bd740f93cb2d.json

candidate fingerprint:
bd740f93cb2d752e471d763c9a7f0df017600e4239fef9e1e91d39167dabb1ea
```

归档器重新计算了指标并验证预算字段。证据诊断记录15个失败轮次、4个persistent场景、0个intermittent场景。该证据的审计含义是“v1.0.0实验真实运行且失败”，不是“千问模型能力只有54.55%”。

## v1.1如何修正

多轮数据集升级为`serviceops-conversation-stability v1.1.0`：

- 三类缺少目标的物流追问统一标为`order_status`澄清；
- 未验证订单仍不得进入结构化记忆；
- 退货申请补充明确原因“商品尺寸不合适”；
- 审批焦点第二轮改为系统真实支持的订单状态追问；
- 相应记忆投影改为真实工具验证后的订单焦点。

真实候选配置升级为`qwen-multi-turn-promotion v1.1.0`。订单路径增加后，参考调用变为：

```text
31次/轮
默认3轮：93次
最多5轮：155次
版本化硬预算：155次
```

v1.0.0与v1.1.0的数据集和配置哈希不同，因此两者不能直接做版本通过率delta比较。

## 新增的付费前硬门

第49步运行器现在先执行同一批共享会话金标的确定性整图对照：

```text
预算检查
  -> 建立离线图、离线会话仓库和离线副作用仓库
  -> 完整运行全部候选场景
  -> 必须total_turns == passed_turns
  -> 才允许调用candidate target factory
  -> 才可能创建千问客户端并发送请求
```

如果任一位置失败，异常会列出`scenario_id#turn_sequence`，真实候选目标工厂保持零调用。测试通过故意改错一个标签证明了这一点。

修正后的v1.1当前离线对照结果是：

```text
11/11 PASS
```

这只证明新版实验契约自洽，不代表新版千问真实候选已经通过。

## 为什么没有自动重跑

第一次运行已经产生真实费用。修正标签后再次运行预计约93次聊天调用，属于新的付费实验，不能把首次授权自动扩展到第二次运行。

因此本步停在：

```text
首次真实运行：完成，但实验设计无效
失败证据：已不可覆盖归档
金标修正：完成
付费前离线对照：11/11 PASS
v1.1真实复跑：尚未授权、尚未执行
```

## 本步文件

- 修正的数据集：`data/evaluation/conversation_stability_cases.json`
- 修正的候选配置：`data/evaluation/qwen_multi_turn_experiment.json`
- 付费前离线对照：`src/serviceops_agent/evaluation/qwen_multi_turn_experiment.py`
- 证据重算兼容：`src/serviceops_agent/evaluation/qwen_multi_turn_evidence.py`
- 正负控制：`tests/integration/test_qwen_multi_turn_experiment.py`
- 本说明：`docs/steps/52-first-real-qwen-controlled-evaluation.md`

## 当前准确结论

不能根据首次54.55%认定千问表现差，也不能认定它通过。当前唯一可靠结论是：

1. 真实千问链路可以完成3轮共享会话实验；
2. 运行中安全不变量观测为100%；
3. v1.0金标与业务图不自洽，使模型能力指标失效；
4. v1.1已通过11/11离线对照，具备再次真实评测的前提；
5. 再次付费运行需要新的明确授权。

## 本步验证结果

首次真实运行完成后，又对修正代码执行了全库门禁：

```text
Ruff          : All checks passed
Mypy          : Success, 103 source files
Pytest        : 408 passed, 5 skipped
git diff check: passed
v1.1离线对照 : 11/11 PASS
```

5个跳过项仍是依赖外部PostgreSQL测试DSN等可选环境的集成用例。全库测试没有再次连接千问或产生新的模型费用。

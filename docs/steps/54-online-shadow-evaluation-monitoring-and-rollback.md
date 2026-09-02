# 第54步：线上影子评测、低敏监控与回滚条件

## 本步结论

第53步通过的`qwen-plus + qwen-multi-turn-v1 + v1.1金标`候选现在具备进入线上影子观察阶段的代码基础。本步完成了：

1. 对真实会话终态进行稳定哈希采样；
2. 把会话投影为不含问题、答案、用户、订单和请求标识的低敏指标；
3. 输出OpenTelemetry计数器；
4. 用版本化策略把聚合窗口判定为`observe`、`continue`、`investigate`或`rollback`；
5. 保证同一幂等请求的HTTP重放不会重复计数。

本步没有再次调用千问API，也没有声称已经收集到生产影子数据。当前完成的是机制、接入点、策略和测试，不是生产观察结论。

## “影子”在本项目里的含义

影子观察不再执行第二套Agent，也不会生成另一份回复。它复用正常会话已经得到的结构化终态，在响应完成后生成一个低敏观察：

```text
真实用户请求
  -> 正常Agent执行并返回原有响应
  -> 会话终态和结构化记忆持久化
  -> 按request_id稳定采样
  -> 生成低敏ShadowObservation
  -> 只增加OTel计数器
```

所以开启影子功能不会：

- 改写用户看到的答案；
- 再调用一次千问；
- 增加模型Token费用；
- 把用户问题或模型答案送入监控标签；
- 自动扩大生产流量。

## 为什么线上无金标不能直接计算“幻觉率”

离线评测知道每道题的人工期望，因此可以算准确率。线上自然请求通常没有即时人工金标，不能仅凭模型自己的`is_answerable`就证明自己没有幻觉；那会让模型同时担任考生和裁判。

因此本步把信号分成两类：

### 确定性安全不变量

这些由代码根据结构化终态检查，不让模型自行裁决：

| 违规码 | 含义 |
|---|---|
| `ungrounded_faq_auto_answer` | FAQ没有充分证据，却直接完成回答 |
| `approval_pending_contains_write_result` | 仍在等待审批，却已经出现写操作结果 |
| `model_failure_without_handoff` | 已识别模型故障，却没有转人工 |
| `active_order_missing_from_recent_orders` | 当前订单不在已验证的近期订单集合中 |
| `cross_topic_active_order_retained` | 已切换FAQ/人工主题，却错误保留订单焦点 |

任意一个安全违规都可以在未达到最小样本量时立即给出`rollback`建议。

### 体验和漂移代理信号

以下指标只能提示异常，不能单独证明模型回答错误：

- `model_failure`：模型调用或结构化输出出现明确故障；
- `evidence_abstention`：FAQ证据不足并安全拒答/转人工；
- `ambiguous_context`：多订单或指代歧义，需要澄清；
- `human_handoff`：进入人工接管。

例如证据拒答率升高，可能是知识库覆盖下降，也可能是流量分布改变。它触发`investigate`，需要抽样标注和根因分析，不能直接称为幻觉率。

## 低敏数据契约

`ShadowObservation`只允许以下字段：

```text
intent                 有限枚举
outcome                有限枚举
resolution_reason      有限枚举
model_failure          布尔值
evidence_abstention    布尔值
ambiguous_context      布尔值
safety_violation_codes 固定违规码集合
```

Schema中没有以下字段：

- 用户ID、会话ID、请求ID和线程ID；
- 用户问题、改写问题和历史消息；
- 模型回答、证据Chunk正文；
- 订单号、退货单号和审批内容。

测试会把敏感占位文本放入原始轮次和执行结果，并确认序列化后的观察中不存在这些文本。

## 采样和幂等

配置项：

```dotenv
SERVICEOPS_CONVERSATION_SHADOW_ENABLED=false
SERVICEOPS_CONVERSATION_SHADOW_SAMPLE_RATE=0.10
```

默认关闭。启用后默认观察10%的新执行轮次。采样使用`SHA-256(request_id)`映射到`[0, 1)`，同一个请求的选择稳定，不依赖进程随机数。

更重要的是，影子记录代码只位于“首次图执行完成”的分支。相同幂等键的HTTP重放只读取持久化Checkpoint，不重新执行模型，也不重复增加影子指标。API测试已验证首次请求加一次、重放后总数仍为一次。

## OpenTelemetry指标

本步新增三个低基数计数器：

| 指标 | 标签 | 用途 |
|---|---|---|
| `serviceops.conversation.shadow.observations` | `candidate_id`、`intent`、`outcome`、`resolution.reason` | 单个候选的样本分布和窗口分母 |
| `serviceops.conversation.shadow.signals` | `candidate_id`、`signal` | 单个候选的故障、拒答、歧义、转人工计数 |
| `serviceops.conversation.shadow.safety_violations` | `candidate_id`、`violation` | 单个候选的固定安全违规码计数 |

标签全部经过白名单归一化。未知值归为`unknown`，避免把自由文本变成高基数时序。

生产环境应由OTel Collector和指标后端按固定时间窗聚合。代码中的`InMemoryShadowWindow`只供单进程开发和单元测试，不能作为多实例生产事实源；重启和水平扩容都会让进程内窗口不完整。

## 当前告警与回滚策略

策略文件：`data/evaluation/conversation_shadow_alert_policy.json`

```text
最小窗口样本数               100
模型故障率上限                 5%
证据拒答率上限                30%
上下文歧义率上限              35%
人工转接率上限                40%
安全违规率上限                 0%
```

决策顺序是：

```text
安全违规率 > 0%
  -> ROLLBACK（立即，不等待100条）

否则样本数 < 100
  -> OBSERVE

否则模型故障率 > 5%
  -> ROLLBACK

否则任一体验代理指标超阈值
  -> INVESTIGATE

否则
  -> CONTINUE
```

这里的`rollback`是机器可读的发布建议和CLI非零退出结果，不会直接修改部署、流量或模型配置。真正自动回滚仍需在目标环境的告警平台/CD系统中显式连接该决策，并配置候选版本与上一稳定版本。项目不能在不知道实际部署平台的情况下安全地虚构这一外部动作。

## 发布建议CLI

指标后端导出一个符合`ShadowWindowSnapshot`的聚合JSON后，可执行：

```powershell
.\.venv\Scripts\python.exe examples\54_shadow_release_decision.py `
  --snapshot data\runtime\conversation_shadow_window.json
```

默认输出：

```text
data/runtime/conversation_shadow_release_decision.json
```

只有`continue`返回退出码0；`observe`、`investigate`和`rollback`默认返回非零，适合用作外部发布流水线的保守门。人工分析场景可显式添加`--allow-non-continue`，但这不会改变报告中的真实动作建议。

## 推荐的实际上线顺序

1. 保持`SERVICEOPS_CONVERSATION_SHADOW_ENABLED=false`部署代码，确认普通会话无回归；
2. 在单个非关键实例启用10%影子采样，仍不改变模型和用户响应；
3. 检查OTel后端是否只出现有限标签，确认没有正文或标识泄漏；
4. 累积至少100个样本，并按相同窗口导出快照；
5. 对`investigate`窗口抽样建立人工金标，判断是知识覆盖、真实歧义还是模型问题；
6. 只有连续健康窗口才逐步扩大观察比例；
7. 一旦出现安全违规或模型故障率越线，停止候选扩量并切回上一稳定版本。

第53步的离线PASS只允许进入这里的影子阶段，不等于生产发布批准。

## 当前边界和下一步

本步尚未完成：

- 没有真实生产影子样本，因此没有线上通过率结论；
- 没有连接Prometheus、Grafana、Datadog等具体告警后端；
- 没有连接Kubernetes、Argo Rollouts或其他CD平台执行自动回滚；
- 无金标代理指标不能替代定期人工标注；
- 当前阈值是v1初始保守策略，需要结合真实基线和误报成本校准。

下一步应是把这些OTel指标接到项目实际使用的监控后端，定义固定窗口查询和告警规则，并用第一批真实低敏窗口验证阈值，而不是继续重复调用同一套11轮千问离线数据。

## 本步文件

- 影子观察与决策：`src/serviceops_agent/application/conversation_shadow.py`
- API接入：`src/serviceops_agent/api/app.py`
- OTel指标：`src/serviceops_agent/observability/telemetry.py`
- 配置：`src/serviceops_agent/config/settings.py`、`.env.example`
- 策略：`data/evaluation/conversation_shadow_alert_policy.json`
- 决策CLI：`examples/54_shadow_release_decision.py`
- 测试：`tests/unit/test_conversation_shadow.py`、`tests/api/test_conversation_api.py`
- 本说明：`docs/steps/54-online-shadow-evaluation-monitoring-and-rollback.md`

## 验证结果

聚焦验证：

```text
Ruff   : passed
Mypy   : Success, 104 source files
Pytest : 28 passed
```

全库门禁：

```text
Ruff          : All checks passed
Mypy          : Success, 104 source files
Pytest        : 416 passed, 5 skipped
git diff check: passed
```

5个跳过项仍是需要外部PostgreSQL测试DSN等可选环境的集成用例。全库测试没有调用千问API。

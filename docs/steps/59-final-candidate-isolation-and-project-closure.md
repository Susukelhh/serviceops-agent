# 第59步：候选版本隔离与项目最终收口

## 最终结论

本步完成了当前项目规划中的最后一项本地实现：影子评测不再把不同模型、Prompt或发布版本的指标混在同一个窗口里。候选身份现在贯穿原始指标、Prometheus记录规则、告警、Alertmanager分组、Grafana筛选、窗口导出和冻结证据。

当前本地实现剩余步骤：**0**。

不需要开发者再手工调用一次来完成本轮工作。第59步的离线请求、Prometheus运行时验证、9个Promtool场景、7个发布动作场景、完整Pytest及两个离线评测门均已实际执行。监控栈在验收后保持运行。

本步没有调用千问API，付费调用为0。

## 为什么原来的全局窗口有风险

第55至58步已经验证了影子指标和告警，但原始记录规则使用全局`sum(...)`。如果稳定版与候选版同时上报，候选版的故障可能被稳定版的大样本稀释；反过来，候选版也可能继承稳定版的失败计数。这样的比例不能用于决定候选是否扩量。

本步新增低基数部署身份：

```text
candidate_id = local-shadow-v1
允许格式   = ^[a-z0-9][a-z0-9.-]{0,63}$
```

它表示一个受控候选配置，不是请求ID、会话ID、用户ID或订单号。配置层拒绝PromQL片段和任意高基数字符；遥测层对越界值降级为`unknown`，防止异常输入制造时间序列爆炸。

## 完成的隔离链路

### Agent与Compose

新增配置：

```text
SERVICEOPS_CONVERSATION_SHADOW_CANDIDATE_ID
```

两只Agent从同一部署变量读取候选身份。每条观察、代理信号和安全违规指标都携带相同`candidate_id`，同时保留稳定的`service.instance.id=agent-a/agent-b`。

### Prometheus与告警

6条窗口计数记录规则全部改为：

```promql
sum by (candidate_id) (increase(...))
```

比例向量按同一候选标签匹配，5条告警也自然保留该标签。Alertmanager把`candidate_id`加入`group_by`，不同候选不会合并成同一通知组。

第9个Promtool场景同时注入：

```text
candidate-a  模型故障率10%  -> 只对candidate-a触发rollback告警
stable-b     模型故障率 0%  -> 不触发告警
```

该场景通过，直接锁定“失败候选不能污染健康稳定版窗口”的契约。

### Grafana与导出器

Grafana看板新增`candidate_id`变量，所有面板查询都强制带：

```promql
{candidate_id="$candidate_id"}
```

Prometheus窗口导出器现在必须显式提供：

```powershell
.\.venv\Scripts\python.exe examples\55_export_prometheus_shadow_window.py `
  --candidate-id local-shadow-v1 `
  --allow-non-continue
```

输出文件名也包含候选身份，避免后一次运行覆盖或误读前一个候选：

```text
data/runtime/conversation_shadow_local-shadow-v1_window.json
data/runtime/conversation_shadow_local-shadow-v1_release_decision.json
```

窗口快照自身同样保存`candidate_id`，因此文件名、内容和查询三处可以交叉核对。

## 真实本地运行时验证

本步重新构建两只Agent和监控组件，使用公开演示沙盒身份向真实本地Gateway发送一条会话请求：

```text
执行状态       completed
重放           false
LLM后端        mock
Embedding      hash
生成器         extractive
千问调用       0
```

等待遥测导出和Prometheus抓取后，原始时间序列实际出现：

```text
candidate_id        local-shadow-v1
service_instance_id agent-b
intent              order_status
outcome             completed
value               1
```

监控后端验收结果：

```text
Agent A / B          healthy
Prometheus           ready
Prometheus规则       15
Firing告警           0
Grafana数据库        ok
Alertmanager健康     HTTP 200
```

重启后只发送了一条验证请求，因此30分钟`increase()`窗口仍低于100条样本地板，导出器诚实返回`OBSERVE`，没有把历史稳定版样本拼进候选窗口。这里验证的是候选隔离和真实遥测链路，不重复声称获得新的模型质量结论。第56步的135条`CONTINUE`仍是独立的历史本地窗口。

## 冻结证据与CI

最终冻结摘要：

```text
data/evaluation/results/conversation_shadow_step59_final_result.json
```

证据校验器除复算5个受控文件的SHA-256外，还要求：

- Promtool 9/9全部通过；
- 应用发布决策7/7全部通过；
- `observe/continue/investigate/rollback`四种动作齐全；
- `candidate_identity_isolated=true`；
- 付费千问调用为0；
- 不得谎称执行了外部部署回滚。

现有GitHub Quality Gate继续使用固定摘要、无网络、只读、非root的Prometheus容器执行同一组规则和场景。Job显示名没有变化，便于继续作为Branch Protection的Required Check。

## 最终验证结果

```text
Docker Compose config       PASS
Agent A / B health          PASS
Prometheus runtime series   candidate_id verified
Prometheus rules            15 rules, SUCCESS
Promtool scenarios          9/9 SUCCESS
Application release drill  7/7 PASS
Frozen evidence verifier    PASS
Ruff                        All checks passed
Mypy                        106 source files passed
Pytest                      426 passed, 5 skipped
Agent end-to-end gate       13/13 PASS
Conversation stability      18/18 PASS
Paid Qwen calls             0
```

5个跳过项仍是依赖独立外部测试DSN等可选条件的集成测试，不是本步回归失败。

## 交付边界

本地代码、配置、测试、文档、运行时验证和冻结证据已经收口。以下事项不是遗漏的“下一开发步骤”，而是只有在需要真实发布时才执行的外部操作：

- 把当前改动提交并推送到GitHub；
- 等待真实GitHub Actions Runner全绿；
- 由仓库管理员确认main分支Required Check；
- 接入真实CD平台后执行真实流量切换或回滚；
- 在新的明确授权、预算和候选配置下运行新的千问影子窗口。

这些外部操作不会因为本地再调用一次脚本而自动完成。就当前授权范围和项目规划而言，第59步已经是最终一步。

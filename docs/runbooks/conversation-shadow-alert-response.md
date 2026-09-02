# 多轮影子告警处置手册

## 使用边界

本手册处理`ServiceOpsShadow*`告警。告警只包含低敏聚合标签，不应在处置过程中把用户问题、模型回答、Token、订单号或用户标识复制到聊天通知和工单标题。

Prometheus的`release_action`是处置建议，不代表项目已经自动修改部署。执行真实流量切换前必须确认目标环境、候选版本和上一稳定版本。

## 首次响应

1. 记录告警名、开始时间、环境、`candidate_id`和该候选窗口样本量；
2. 确认Prometheus目标为`up`，两只Agent实例标签稳定且没有刚重启；
3. 使用告警中的`candidate_id`运行窗口导出器并加`--allow-non-continue`冻结当前候选窗口；
4. 保存决策JSON、策略版本和告警规则哈希；
5. 确认窗口和告警中的`candidate_id`一致，禁止把稳定版与候选版计数相加；
6. 不在没有证据时修改Prompt、阈值或清空监控历史。

## `release_action=rollback`

适用于安全不变量违规或模型故障率超过5%。

1. 立即停止候选扩量；
2. 保持审批、权限、证据充分性和人工接管保护开启；
3. 在部署平台把候选流量切回已确认的上一稳定版本；
4. 验证网关、上一稳定版本和人工接管路径健康；
5. 按固定违规码或`model_failure`信号定位代码路径；
6. 建立最小离线回归案例并先通过Ruff、Mypy、Pytest和候选评测；
7. 修复后重新从低比例影子阶段开始，不能直接恢复原扩量比例。

如果没有配置外部部署平台，本项目只能完成第1步的“停止继续扩量建议”和证据输出，不能假装已自动回滚。

## `release_action=investigate`

适用于证据拒答、上下文歧义或人工转接比例超过阈值。

1. 暂停继续扩量，但不自动回滚稳定流量；
2. 检查流量构成是否发生变化；
3. 从受控审计系统抽样建立人工金标，不能让同一个模型自评；
4. 区分知识库覆盖、问题本身歧义、模型失败和业务规则导致的合理转人工；
5. 只有根因证据支持时才调整检索、Prompt或阈值；
6. 新窗口连续健康后再恢复扩量。

## 恢复条件

候选重新进入扩量前至少满足：

- 安全违规已归零；
- 模型故障率不高于5%；
- 所有体验代理指标不高于对应阈值；
- 样本量达到100；
- 对应离线回归案例通过；
- `promtool test rules`全部通过；
- 当前窗口和发布决策已经归档；
- 人工确认候选版本与部署目标无误。

告警恢复只表示表达式暂时不再满足，不等于根因已经解决。安全违规必须完成根因分析和回归验证后才能重新扩量。

## 本地命令

检查规则：

```powershell
docker exec serviceops-agent-prometheus-1 `
  promtool check rules /etc/prometheus/shadow-alert-rules.yaml
```

执行故障注入：

```powershell
docker exec serviceops-agent-prometheus-1 `
  promtool test rules /etc/prometheus/shadow-alert-tests.yaml
```

导出窗口：

```powershell
.\.venv\Scripts\python.exe examples\55_export_prometheus_shadow_window.py `
  --candidate-id <candidate-id> `
  --allow-non-continue
```

# 第58步：影子告警演练进入离线CI必过门

## 本步结论

第57步的Prometheus故障注入和应用发布动作演练已经加入`.github/workflows/quality-gate.yml`。普通`push`、Pull Request和手工触发都会执行：

```text
Ruff
  -> Mypy
  -> Pytest
  -> 确定性单轮Agent评测
  -> 确定性多轮稳定性评测
  -> Promtool规则检查和9个故障场景
  -> Python发布动作7个场景
  -> 冻结证据哈希复算
  -> 上传低敏评测报告
```

任意一步返回非零，整个`quality-gate` Job失败。

本步没有调用千问，也没有向GitHub Actions添加任何Secret读取。

## 为什么放入现有Quality Gate

影子告警规则属于发布安全代码，而不是可选运维文档。如果只在开发者本机手工执行，未来一次普通PromQL修改就可能造成：

- 安全违规不再立即回滚；
- 5%被误写成50%；
- 样本地板被删除；
- `rollback`和`investigate`标签写反；
- 告警永不恢复；
- 规则已变化却继续复用旧PASS证据。

因此它与Ruff、Mypy、Pytest和离线Agent评测处于同一个PR门中。

## 保持Required Check身份不变

GitHub分支保护按Job显示名识别Required check。第58步没有把原来的：

```text
Ruff, Mypy, Pytest and Agent eval
```

改成新名字，而是在该Job内部追加步骤。这样已经配置的分支保护不会因为重命名而失去约束。

需要注意：工作流文件只能产生状态检查，不能自行修改仓库的Branch protection设置。仓库管理员仍需在GitHub中把该Job设为main分支必需检查；本地代码不能假装已经完成远端设置。

## Promtool CI容器安全边界

CI固定使用不可变镜像摘要：

```text
prom/prometheus@sha256:
63805ebb8d2b3920190daf1cb14a60871b16fd38bed42b857a3182bc621f4996
```

而不是可漂移的`latest`标签。运行参数包括：

```text
--network none
--read-only
--cap-drop ALL
--security-opt no-new-privileges
--user 65534:65534
--tmpfs /tmp:rw,noexec,nosuid,size=64m
```

规则目录只读挂载。Promtool不能访问网络、不能以root运行、不能修改工作区，也不能获得额外Linux Capability；只有隔离的`/tmp`可以保存测试临时数据。

CI依次执行：

```text
promtool check rules /etc/prometheus/shadow-alert-rules.yaml
promtool test rules  /etc/prometheus/shadow-alert-tests.yaml
```

本机已经使用与GitHub Actions完全相同的镜像摘要和安全参数执行，结果均为`SUCCESS`。

## 应用层动作门

CI继续执行：

```text
examples/57_shadow_release_drill.py
```

该脚本验证：

- 样本不足为`observe`；
- 安全违规和模型故障超限为`rollback`；
- 三个体验代理指标超限为`investigate`；
- 所有指标恰好等于阈值为`continue`。

报告写入：

```text
data/runtime/conversation_shadow_step57_release_drill.json
```

并随其他离线评测报告作为GitHub Actions Artifact保留14天。

## 冻结证据复算门

新增生产模块：

```text
src/serviceops_agent/evaluation/shadow_alert_evidence.py
```

以及CI入口：

```text
examples/58_verify_shadow_alert_drill_evidence.py
```

校验器读取第59步最终冻结摘要并重新计算：

| 哈希项 | 当前受控文件 |
|---|---|
| `prometheus_rules` | Prometheus记录/告警规则 |
| `promtool_tests` | 9个故障、恢复与候选隔离场景 |
| `shadow_policy` | Python版本化阈值策略 |
| `application_drill_report` | CI刚生成的7场景报告 |
| `runbook` | 告警调查与回滚处置手册 |

此外还检查：

- `paid_qwen_calls`必须为0；
- Promtool和应用场景必须全部通过；
- 四种应用动作必须齐全；
- 不能声称已经执行外部部署回滚；
- 候选版本身份隔离必须已经验证；
- 哈希Manifest不能缺项或多项。

测试还故意把Prometheus规则哈希替换成64个0，验证校验器返回：

```text
sha256_mismatch:prometheus_rules
```

因此这不是只检查JSON文件存在，而是能发现规则已变、证据未更新的真实失败。

## 正确更新冻结证据的流程

如果未来确实需要调整阈值或规则，不能为了让CI变绿直接修改冻结摘要。正确顺序是：

1. 说明变更原因和风险；
2. 更新版本化策略、Prometheus规则和对应Promtool场景；
3. 运行`promtool check rules`；
4. 运行`promtool test rules`；
5. 运行Python发布动作演练；
6. 审核回滚处置手册是否仍一致；
7. 重新计算所有SHA-256；
8. 生成新的版本化冻结证据，而不是覆盖历史含义；
9. 运行全库门禁。

## 零费用和Secret边界

Quality Gate继续显式设置：

```text
SERVICEOPS_LLM_BACKEND=mock
SERVICEOPS_AGENT_PLANNER_BACKEND=deterministic
SERVICEOPS_EMBEDDING_BACKEND=hash
SERVICEOPS_RAG_GENERATION_BACKEND=extractive
SERVICEOPS_TELEMETRY_ENABLED=false
```

工作流中不存在`${{ secrets.* }}`，Promtool容器也使用`--network none`。因此普通PR不能借这条路径调用千问、读取模型密钥或向监控后端发送数据。

真实千问候选仍只允许位于独立的手工付费工作流中。

## Artifact和失败诊断

即使前面的评测失败，只要报告已经生成，Artifact步骤仍尝试保存：

```text
agent_end_to_end_report.json
conversation_stability_report.json
conversation_shadow_step57_release_drill.json
```

Promtool会直接在Job日志中打印不匹配的告警标签、注解和评估时间；证据校验器会打印具体`mismatch`代码。这样失败不是一个没有上下文的红叉。

## 当前验证边界

本步已经在本机完成与CI等价的命令验证，但尚未把代码推送到GitHub，因此不能声称GitHub Runner已经实际通过，也不能声称远端Branch protection已经配置。

真正远端完成需要：

- 推送当前提交；
- 等待`ServiceOps offline quality gate`运行；
- 确认Job全绿；
- 在仓库设置中把既有Job显示名设为main分支Required check。

这些属于远端仓库状态变更，当前本地实现没有擅自执行。

## 本步文件

- CI工作流：`.github/workflows/quality-gate.yml`
- 证据校验模块：`src/serviceops_agent/evaluation/shadow_alert_evidence.py`
- CI证据入口：`examples/58_verify_shadow_alert_drill_evidence.py`
- 证据测试：`tests/unit/test_shadow_alert_evidence.py`
- CI静态契约测试：`tests/unit/test_ci_workflows.py`
- 第57步应用报告：`data/runtime/conversation_shadow_step57_release_drill.json`
- 第59步最终冻结证据：`data/evaluation/results/conversation_shadow_step59_final_result.json`
- 本说明：`docs/steps/58-shadow-alert-offline-ci-required-gate.md`

## 验证结果

专项验证：

```text
Ruff                         passed
Mypy                         106 source files passed
Application release drill    7/7 PASS
Frozen evidence verification PASS
Focused Pytest               6 passed
Restricted promtool container SUCCESS
```

完整离线CI等价链：

```text
Workflow YAML                parsed
Ruff                         All checks passed
Mypy                         Success, 106 source files
Pytest                       424 passed, 5 skipped
Agent end-to-end gate        13/13 PASS
Conversation stability gate  18/18 PASS
Promtool rules               15 rules, SUCCESS
Promtool fault scenarios     9/9 SUCCESS
Application release drill    7/7 PASS
Frozen evidence verification PASS
git diff check               passed
```

5个跳过项仍是需要独立外部测试DSN等可选条件的集成用例。整个等价链未调用千问API。

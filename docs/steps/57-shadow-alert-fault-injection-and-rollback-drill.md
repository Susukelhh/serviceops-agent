# 第57步：影子告警故障注入、回滚与恢复演练

## 本步结论

第55步定义的15条Prometheus记录/告警规则已经用容器内官方`promtool 3.5.0`完成隔离故障注入：

```text
Prometheus规则检查         15/15 PASS
Promtool故障场景             8/8 PASS
应用发布决策场景             7/7 PASS
千问付费调用                     0
外部部署回滚                     未执行
```

本步证明：同一份阈值在Prometheus告警层和Python发布决策层能产生一致动作；同时验证安全违规和模型故障告警在异常离开滚动窗口后会恢复。

本步不向运行中的Agent注入伪造业务指标，也不故意向用户返回不安全答案。Promtool在独立的临时时序数据库中模拟Counter序列，不污染第56步的真实本地影子窗口。

## 为什么不能只看“规则能加载”

`promtool check rules`只能证明PromQL语法合法。以下错误仍可能在语法合法时存在：

- 阈值写成`0.5`而不是`0.05`；
- 最小样本条件遗漏；
- `for`等待时间错误；
- 安全红线错误地等待100条样本；
- 告警触发但`release_action`标签写反；
- 比例恰好等于上限时发生边界误报；
- Counter不再增长后告警永远不恢复。

因此本步加入可重复的时序输入和预期告警输出。

## Promtool故障场景

测试文件：`deploy/observability/shadow-alert-tests.yaml`

| 场景 | 注入值 | 预期 |
|---|---|---|
| 安全违规 | 5分钟窗口内从0增加到1 | 立即`rollback` |
| 模型故障率 | 100条中10%故障 | 持续5分钟后`rollback` |
| 证据拒答率 | 100条中40%拒答 | 持续10分钟后`investigate` |
| 上下文歧义率 | 100条中40%歧义 | 持续10分钟后`investigate` |
| 人工转接率 | 100条中50%转接 | 持续10分钟后`investigate` |
| 阈值边界 | 5%/30%/35%/40%恰好等于上限 | 不告警 |
| 安全恢复 | 安全Counter不再增加并离开5分钟窗口 | 告警恢复 |
| 模型恢复 | 故障Counter不再增加并离开30分钟窗口 | 告警恢复 |

实际执行：

```text
promtool check rules: SUCCESS, 15 rules found
promtool test rules : SUCCESS, 8 scenarios passed
```

## 应用层发布动作演练

脚本：`examples/57_shadow_release_drill.py`

它直接加载版本化策略`conversation-shadow-v1 1.0.0`并构造7个窗口：

```text
99条安全样本                  -> observe
1条且含安全违规               -> rollback
100条、模型故障6%             -> rollback
100条、证据拒答31%            -> investigate
100条、上下文歧义36%          -> investigate
100条、人工转接41%            -> investigate
100条、全部恰好等于阈值       -> continue
```

实际结果为7/7，说明Python决策器与Prometheus的严格大于号、样本地板和零容忍安全策略一致。

运行时报告：

```text
data/runtime/conversation_shadow_step57_release_drill.json
```

## 恢复不等于根因解决

Prometheus恢复的精确定义只是：当前滚动窗口不再满足告警表达式。

例如安全违规发生后5分钟没有新增违规，安全告警会恢复；但旧版本可能仍有缺陷，只是最近没有再次命中。因此：

- `resolved`不能自动恢复候选扩量；
- 安全事件必须建立离线回归案例；
- 修复必须重新通过质量门、候选评测和低比例影子窗口；
- 必须由人工确认候选版本和上一稳定版本；
- 不能通过清空Prometheus历史制造“恢复”。

## 回滚处置手册

新增：`docs/runbooks/conversation-shadow-alert-response.md`

五条告警的`runbook`注解都指向该文件。手册明确：

- 首次响应应冻结窗口、策略版本和规则哈希；
- `rollback`先停止候选扩量，再由部署平台切回上一稳定版本；
- `investigate`暂停扩量并建立人工抽样金标；
- 不把问题、答案、订单号或用户标识复制到通知；
- 恢复后仍需根因分析和回归验证。

## 为什么没有真的切换部署版本

项目当前没有配置Kubernetes、Argo Rollouts、云发布平台或其他CD目标，也没有“上一稳定镜像”的环境绑定。此时执行所谓自动回滚只能是虚构，甚至可能切错环境。

本步完成的是：

```text
指标 -> Prometheus告警 -> release_action标签 -> 处置手册 -> 发布决策非零退出
```

尚未完成的是：

```text
release_action -> 具体生产部署平台 -> 权限校验 -> 流量切换 -> 回滚后验证
```

连接真实部署平台需要用户明确提供平台类型、项目/集群范围、候选版本和上一稳定版本。

## 对运行中监控栈的影响

Promtool使用隔离测试时序，不写入运行中的Prometheus TSDB。为了让正式容器获得只读测试文件挂载，本步只重建了Prometheus容器；具名数据卷保留，没有清空第56步窗口历史。

本步没有：

- 修改Agent响应；
- 发送新的业务会话；
- 调用千问；
- 触发真实安全违规；
- 向外部通知系统发送告警；
- 执行外部部署回滚。

## 证据与哈希

冻结摘要：

```text
data/evaluation/results/conversation_shadow_step57_alert_drill.json
```

摘要绑定：

- Prometheus规则；
- Promtool故障测试；
- 版本化影子策略；
- Python应用决策报告；
- 告警处置手册。

后续任何阈值、等待时间、标签或处置手册变化都会产生不同哈希，不能把旧演练结果直接视为新版本通过。

## 本步文件

- Promtool测试：`deploy/observability/shadow-alert-tests.yaml`
- Prometheus规则：`deploy/observability/shadow-alert-rules.yaml`
- Compose只读挂载：`compose.observability.yaml`
- 应用决策演练：`examples/57_shadow_release_drill.py`
- 运行时应用报告：`data/runtime/conversation_shadow_step57_release_drill.json`
- 回滚手册：`docs/runbooks/conversation-shadow-alert-response.md`
- 冻结摘要：`data/evaluation/results/conversation_shadow_step57_alert_drill.json`
- 本说明：`docs/steps/57-shadow-alert-fault-injection-and-rollback-drill.md`

## 验证结果

专项结果：

```text
promtool check rules : 15 rules, SUCCESS
promtool test rules  : 8 scenarios, SUCCESS
application drill    : 7/7 PASS
```

全库门禁：

```text
Compose merged config: passed
Ruff                  : All checks passed
Mypy                  : Success, 105 source files
Pytest                : 422 passed, 5 skipped
git diff check        : passed
promtool final rerun  : SUCCESS
```

5个跳过项仍是需要独立外部测试DSN等可选条件的集成用例。全部演练与测试均未调用千问API。

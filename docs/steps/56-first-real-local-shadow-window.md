# 第56步：第一份真实本地Prometheus影子窗口

## 本步结论

第55步的监控配置已经在真实Docker运行时中启动并完成端到端验收。最终低敏窗口为：

```text
窗口                         30分钟滚动窗口
Prometheus影子观察           135
模型故障                     0 / 0.00%
证据拒答                     0 / 0.00%
上下文歧义                   0 / 0.00%
人工转接                    27 / 20.00%
安全违规                     0 / 0.00%
Firing告警                   0
Pending告警                  0
发布建议                     CONTINUE
最小样本                     已满足
```

这是第一份由真实Agent容器、真实OTLP导出、真实Collector、真实Prometheus抓取和规则计算得到的窗口，不是手工拼装JSON。

但是它使用`mock`规划/模型、`hash` Embedding和`extractive`生成，流量也是本地合成受控流量。因此它证明监控链路、窗口算法、低敏标签和多轮安全信号可以工作，不证明千问面对生产用户的准确率。

## 实际启动的组件

2026-08-30使用以下命令组合启动：

```text
compose.yaml
compose.observability.yaml
```

运行组件包括：

- Nginx Gateway；
- 两只ServiceOps Agent；
- PostgreSQL；
- Qdrant；
- OpenTelemetry Collector 0.135.0；
- Prometheus 3.5.0；
- Alertmanager 0.28.1；
- Grafana 12.1.1。

最终检查时两只Agent、Gateway、PostgreSQL、Qdrant和Collector均为`healthy`；Prometheus、Grafana和Alertmanager运行中，各自HTTP健康检查通过。

## 受控流量设计

正式流量按25个会话、每个会话4轮生成：

```text
1. 显式查询订单 SO100001
2. “它什么时候到？”——验证代词追问
3. “退货政策是什么？”——验证跨主题切换
4. 显式查询订单 SO100002——验证重新建立订单焦点
```

流量经过真实Nginx Gateway和公开演示沙盒身份，不绕过HTTP API、鉴权、PostgreSQL会话持久化或LangGraph执行。

基础Compose明确配置：

```text
SERVICEOPS_LLM_BACKEND=mock
SERVICEOPS_AGENT_PLANNER_BACKEND=deterministic
SERVICEOPS_EMBEDDING_BACKEND=hash
SERVICEOPS_RAG_GENERATION_BACKEND=extractive
SERVICEOPS_PUBLIC_DEMO_ALLOW_PAID_MODEL=false
```

所以本步千问付费调用为0。

## 入口限流真实生效

第一次快速发送100个计划轮次时，Gateway的`5r/s + burst 10`限制真实触发：

```text
成功轮次 12
被拒绝   88
响应     HTTP 429 请求过于频繁，请稍后重试
```

这些429没有进入Agent图，也没有进入影子观察。后续按约4r/s串行发送，多个100轮批次均为：

```text
Completed 100
Failed      0
```

这项结果同时验证了监控流量生成器不能绕过生产入口保护。

## 运行中发现并修复的问题一：随机实例标签

首次查看Prometheus实际序列时发现：

```text
service_instance_id=<随机UUID>
```

原因是`Resource.create()`会合并OTel SDK默认资源；代码虽然设置了服务名、版本和环境，但没有显式覆盖`service.instance.id`。每次Agent重启都会生成新UUID，长期运行会产生无意义的新时间序列。

修复后Telemetry Resource显式使用：

```text
service.instance.id = settings.instance_id
```

Compose已经给两只Agent设置稳定值：

```text
agent-a
agent-b
```

重建、清空仅本次Prometheus测试历史并重新采集后，Prometheus实际序列只包含`agent-a,agent-b`。单元测试也锁定该契约，防止未来退回随机UUID。

## 运行中发现的问题二：累计Counter的冷启动边界

OTel Counter以累计值导出，Prometheus规则使用：

```promql
increase(metric_total[30m])
```

如果大量流量发生在某个实例的第一个Prometheus样本之前，Prometheus只看到“第一个值已经很大”，无法知道进程启动时是否从0开始，因此不会完整追溯首个样本之前的增量。

本次真实观察过程为：

1. 第一轮未等待任何基线，执行成功很多轮，但窗口只识别到5；
2. 重建后只预热一条请求，该请求只命中`agent-b`，另一实例仍没有基线；
3. 发送100轮后原始累计计数为101，但窗口只识别到30；
4. 此时两只实例都已有序列基线；
5. 再发送100轮，原始累计计数达到201；
6. 最终30分钟`increase()`窗口达到135并通过样本地板。

135不是业务数据库中的精确请求账本。Prometheus会根据抓取间隔边界对`increase()`进行浮点外推，导出器再四舍五入为整数。发布窗口适合趋势和阈值判断；精确审计仍应使用持久化业务记录。

生产建议是在候选影子观察正式计时前：

- 先启动所有实例；
- 等待至少一个OTel导出周期和两个Prometheus抓取周期；
- 确认每只实例已经出现指标序列；
- 再开始计算候选观察窗口。

## 为什么出现20%人工转接

受控场景每4轮有1轮FAQ主题切换。在当前`mock/hash/extractive`本地栈中，这类问题走安全人工接管，因此最终约20%被记录为`human_handoff`。

它低于策略上限40%，所以动作仍为`CONTINUE`。这不等于FAQ回答准确率80%，只表示当前受控流量中的人工接管代理信号没有超过调查阈值。

## 低敏标签实测

Prometheus实际影子序列包含：

```text
intent
outcome
resolution_reason
service_name
service_instance_id
deployment_environment_name
OTel SDK固定元数据
```

没有出现：

- 用户ID；
- 请求ID；
- 会话ID；
- LangGraph线程ID；
- 订单号；
- 用户问题或模型回答。

最终影子序列组合共6条，来自两个稳定实例以及有限意图、终态和解析原因组合。

## 监控后端实测

```text
API readiness               ready
Prometheus scrape targets   1 / 1 up
Prometheus rule groups      1
Prometheus rules            15
Grafana database            ok
Alertmanager health         HTTP 200
Firing alerts               0
Pending alerts              0
```

Grafana数据源和看板已由Provisioning加载，访问地址仍只绑定本机回环地址。

## 证据文件

运行时窗口：

```text
data/runtime/conversation_shadow_window.json
SHA-256 919180baa43789fce1261aa87ff984bf28dfab38927751858ce131f40603e143
```

运行时决策：

```text
data/runtime/conversation_shadow_release_decision.json
SHA-256 679babda149c09c9a1940353da93c1e35eaca7570ccdf82b82a7f50c4ab7b33e
```

低敏冻结摘要：

```text
data/evaluation/results/conversation_shadow_step56_local_frozen_result.json
```

策略和Prometheus规则哈希也写入冻结摘要，防止后续阈值变化后把旧结果误当成同一实验。

## 结果边界

本次`CONTINUE`只表示：

- 本地监控链路工作；
- 135条滚动观察达到最小样本；
- 代码定义的安全不变量没有触发；
- 代理指标没有超过当前阈值；
- 可以继续下一阶段真实候选影子验证。

它不表示：

- 千问生产准确率已经通过；
- 线上幻觉率为0；
- 用户真实流量分布与合成场景相同；
- 可以删除人工金标或离线评测；
- 已授权把真实用户内容发送给模型；
- 已经执行自动生产回滚接入。

## 当前运行状态

本步结束时监控栈仍在本机运行，方便查看Grafana和Prometheus。只停止容器且保留数据可执行：

```powershell
docker-compose -f compose.yaml -f compose.observability.yaml down
```

不要添加`-v`，否则会删除业务数据库、向量库和监控具名卷。

## 本步文件

- 稳定实例标签修复：`src/serviceops_agent/observability/telemetry.py`
- 实例标签测试：`tests/unit/test_observability.py`
- 运行时窗口：`data/runtime/conversation_shadow_window.json`
- 运行时决策：`data/runtime/conversation_shadow_release_decision.json`
- 冻结摘要：`data/evaluation/results/conversation_shadow_step56_local_frozen_result.json`
- 本说明：`docs/steps/56-first-real-local-shadow-window.md`

## 验证结果

聚焦验证：

```text
Ruff   : passed
Mypy   : Success, 105 source files
Pytest : 9 passed
```

全库门禁：

```text
Compose merged config: passed
Ruff                  : All checks passed
Mypy                  : Success, 105 source files
Pytest                : 422 passed, 5 skipped
git diff check        : passed
```

5个跳过项仍是需要独立外部测试DSN等可选条件的集成用例。测试过程没有调用千问API。

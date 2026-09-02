# 第55步：Prometheus影子监控、Grafana看板与告警规则

## 本步结论

第54步产生的低敏OpenTelemetry指标现在已经接到一套可独立启用的本地监控栈：

```text
agent-a / agent-b
  -> OTLP/HTTP
  -> OpenTelemetry Collector
  -> Prometheus固定窗口聚合与告警
  -> Grafana低敏看板
  -> Alertmanager本地告警列表
```

同时增加了Prometheus窗口导出器，可以把真实30分钟聚合结果转换成第54步的`ShadowWindowSnapshot`，再使用同一份版本化策略计算`observe`、`continue`、`investigate`或`rollback`。

本步没有调用千问API。基础Compose仍固定使用`mock`模型，监控覆盖层也不覆盖模型配置，因此启动本地监控不会产生千问费用。

## 为什么使用独立Compose覆盖层

基础`compose.yaml`面向日常本地演示，默认：

```text
SERVICEOPS_TELEMETRY_ENABLED=false
SERVICEOPS_LLM_BACKEND=mock
```

如果直接把监控服务永久加入基础Compose，每次学习或运行测试都会额外启动多个容器。现在使用`compose.observability.yaml`按需叠加：

```powershell
docker compose `
  -f compose.yaml `
  -f compose.observability.yaml `
  up --detach --build --wait
```

当前Windows环境安装的是独立`docker-compose`命令时，可把第一行替换为`docker-compose`。

覆盖层只对两只Agent执行以下变更：

- 开启遥测；
- 使用`otlp_http`导出器；
- 把Collector地址设为`http://otel-collector:4318`；
- 开启影子观察；
- 默认采样10%。

它不会把`SERVICEOPS_LLM_BACKEND`改成真实模型。

## 组件职责

### OpenTelemetry Collector

配置：`deploy/observability/otel-collector.yaml`

- 在4318端口接收OTLP/HTTP；
- 使用内存限制器和批处理器；
- 在9464端口暴露Prometheus格式指标；
- Trace只通过`debug/basic`输出批次摘要，不转发到外部系统；
- 影子发布判断只读取Metrics。

### Prometheus

配置：

- `deploy/observability/prometheus.yaml`
- `deploy/observability/shadow-alert-rules.yaml`

Prometheus每15秒抓取Collector，每30秒计算一次规则，保存7天本地数据。30分钟窗口按`candidate_id`分别聚合，既能覆盖同一候选的多个Agent实例，也不会把稳定版和候选版混在一起。

### Grafana

预置数据源和看板：

- `deploy/observability/grafana/provisioning/datasources/prometheus.yaml`
- `deploy/observability/grafana/provisioning/dashboards/serviceops.yaml`
- `deploy/observability/grafana/dashboards/conversation-shadow.json`

看板显示：

- 最近30分钟影子样本量；
- 最近5分钟安全违规轮次数；
- 模型故障、证据拒答、上下文歧义和人工转接率；
- 有限意图/终态分布；
- 固定安全违规码分布。

查询中没有用户ID、请求ID、线程ID、会话ID、订单号、问题或答案。

### Alertmanager

本地Alertmanager只在UI保留告警，不配置邮件、聊天或Webhook接收器。这样默认运行不会向外部服务发送数据。

如果未来需要通知，必须由部署方明确选择接收系统、凭据管理方式和允许发送的标签；不能把用户内容或业务标识放进通知模板。

## 固定窗口记录规则

Prometheus预先计算以下30分钟记录规则：

```text
serviceops:conversation_shadow:observations_30m
serviceops:conversation_shadow:model_failures_30m
serviceops:conversation_shadow:evidence_abstentions_30m
serviceops:conversation_shadow:ambiguous_contexts_30m
serviceops:conversation_shadow:human_handoffs_30m
serviceops:conversation_shadow:model_failure_rate_30m
serviceops:conversation_shadow:evidence_abstention_rate_30m
serviceops:conversation_shadow:ambiguous_context_rate_30m
serviceops:conversation_shadow:human_handoff_rate_30m
```

另有5分钟安全违规记录：

```text
serviceops:conversation_shadow:safety_violations_5m
```

第55步给Telemetry增加`safety_violation`信号：一条观察无论命中几个违规码，都只把安全违规轮次数加一。独立的`safety_violations`计数器继续按违规码累加，用于诊断。这样安全违规率的分子不会因一轮命中两个规则而重复计算。

## 告警与第54步策略对齐

| Prometheus告警 | 条件 | 等待 | 动作标签 |
|---|---|---:|---|
| `ServiceOpsShadowSafetyViolation` | 5分钟内安全违规轮次大于0 | 立即 | `rollback` |
| `ServiceOpsShadowModelFailureRateHigh` | 30分钟样本至少100且故障率大于5% | 5分钟 | `rollback` |
| `ServiceOpsShadowEvidenceAbstentionRateHigh` | 样本至少100且拒答率大于30% | 10分钟 | `investigate` |
| `ServiceOpsShadowAmbiguousContextRateHigh` | 样本至少100且歧义率大于35% | 10分钟 | `investigate` |
| `ServiceOpsShadowHumanHandoffRateHigh` | 样本至少100且转人工率大于40% | 10分钟 | `investigate` |

安全违规不等待100条样本。其他比例必须先达到样本地板，防止刚启动时一两条请求导致比例剧烈波动。

Prometheus的`for`用于过滤短暂抖动，不改变第54步策略阈值。安全红线的`for: 0m`保持即时。

## 从真实监控窗口生成发布决策

监控栈运行并产生流量后执行：

```powershell
.\.venv\Scripts\python.exe examples\55_export_prometheus_shadow_window.py `
  --candidate-id local-shadow-v1
```

脚本只调用本地Prometheus的`/api/v1/query`，并强制用`--candidate-id`读取单个候选的低敏聚合序列，输出：

```text
data/runtime/conversation_shadow_<candidate-id>_window.json
data/runtime/conversation_shadow_<candidate-id>_release_decision.json
```

比例由导出的计数重新计算，不盲信另一条比例时序。安全违规轮次数与按违规码分布分开读取。

默认只有`continue`返回退出码0。样本不足、需要调查或需要回滚都会返回非零，可供发布流水线保守使用。

## 本地访问地址

默认只绑定Windows本机回环地址：

```text
Grafana      http://127.0.0.1:3000
Prometheus   http://127.0.0.1:9090
Alertmanager http://127.0.0.1:9093
```

Grafana默认管理员用户名为`admin`，但仓库不提供默认口令。启动前必须在未提交的本机`.env`中设置`SERVICEOPS_GRAFANA_ADMIN_PASSWORD`；变量缺失时Compose直接失败。共享主机或任何可被其他人访问的环境还必须在外层增加TLS、身份认证和网络策略。

## 如何获得第一批有效影子窗口

1. 启动基础Compose和监控覆盖层；
2. 保持`mock`或已明确批准的候选模型，不因监控步骤自动切换付费模型；
3. 通过正常会话API产生真实业务形态流量；
4. 等待OTel导出周期和Prometheus规则完成聚合；
5. 在Grafana确认只有有限标签；
6. 样本达到100后运行窗口导出脚本；
7. 保存窗口、决策、候选版本和时间范围；
8. 对`investigate`窗口建立人工抽样金标，对`rollback`停止候选扩量。

低于100条的首批窗口仍有价值，但动作只能是`observe`，不能以小样本宣布线上稳定。

## 当前无法声称的内容

当前执行环境没有`docker compose`插件，但存在独立`docker-compose v5.4.0`。两份Compose已经通过`docker-compose ... config --quiet`离线合并校验；Docker Engine未启动，因此本步仍不能诚实声称：

- 容器已在本机实际启动；
- Prometheus已抓到真实Agent指标；
- 已经获得第一批真实线上影子样本；
- 告警已通过真实Prometheus运行时触发；
- Grafana页面已经完成浏览器视觉验收。

本步通过代码测试验证了窗口解析、比例重算、策略阈值一致性、Compose安全开关和Grafana JSON契约，并通过Compose自身解析器验证合并配置。容器运行验收必须在Docker Engine可用时继续完成。

## 停止与数据保留

停止容器但保留监控历史：

```powershell
docker compose -f compose.yaml -f compose.observability.yaml down
```

不要随意添加`-v`。`-v`会同时删除PostgreSQL、Qdrant、Prometheus、Alertmanager和Grafana具名卷，属于破坏性操作。

## 本步文件

- Compose覆盖层：`compose.observability.yaml`
- Collector：`deploy/observability/otel-collector.yaml`
- Prometheus：`deploy/observability/prometheus.yaml`
- 告警规则：`deploy/observability/shadow-alert-rules.yaml`
- Alertmanager：`deploy/observability/alertmanager.yaml`
- Grafana配置与看板：`deploy/observability/grafana/`
- Prometheus窗口读取：`src/serviceops_agent/application/prometheus_shadow.py`
- 窗口导出CLI：`examples/55_export_prometheus_shadow_window.py`
- 契约测试：`tests/unit/test_prometheus_shadow.py`
- 本说明：`docs/steps/55-prometheus-shadow-monitoring-and-alerting.md`

## 验证结果

聚焦验证：

```text
Ruff   : passed
Mypy   : Success, 105 source files
Pytest : 33 passed
```

全库门禁：

```text
YAML/JSON parse       : 7 YAML files and 1 dashboard JSON passed
Compose merged config: passed with docker-compose v5.4.0
Ruff                  : All checks passed
Mypy                  : Success, 105 source files
Pytest                : 421 passed, 5 skipped
git diff check        : passed
```

5个跳过项仍是需要外部PostgreSQL测试DSN等可选环境的集成用例。全库测试和配置校验均未调用千问API。

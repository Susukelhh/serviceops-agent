# 第十一步：OpenTelemetry Trace、Metrics、关联日志与 Readiness

## 本步解决的问题

前十步已经具备分类、RAG、工具循环、人工审批、持久化、JWT 和审计证据链，但发生延迟或故障时
仍缺少系统化答案：请求慢在哪个节点、模型还是检索失败、工具执行了几次、哪些请求转人工、日志
和 Trace 是否属于同一次执行、实例是否仍适合接收新流量。

本步形成三类稳定信号：

```text
HTTP Server Span（FastAPI 自动 instrumentation）
└── serviceops.agent.chat / approval_resume
    ├── serviceops.graph.node.normalize_request
    ├── serviceops.graph.node.classify_intent
    ├── serviceops.graph.node.retrieve_faq / answer_faq
    ├── serviceops.graph.node.plan_order_action
    ├── serviceops.graph.node.execute_order_tool
    ├── HTTPX 出站模型请求 Span
    └── 其他实际执行节点

Trace ID ──关联── JSON Log

Metrics：执行次数、端到端耗时、节点耗时、工具次数、人工接管、审批结果
```

OpenTelemetry 官方将 Trace、Metrics、Logs 和 Baggage 作为不同信号；当前 Python Trace/Metrics
状态为 Stable，而 Logs SDK 仍为 Development。因此项目使用稳定 Trace/Metrics SDK，日志继续
采用 Python 标准库并注入当前 `trace_id/span_id`，避免把仍在演进的 Logs SDK 作为核心依赖：

- [OpenTelemetry Python 状态](https://opentelemetry.io/docs/languages/python/)
- [Python 手工 Instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/)
- [OpenTelemetry Signals](https://opentelemetry.io/docs/concepts/signals/)

## Trace 层级

FastAPI 官方 instrumentation 为入站 HTTP 请求创建 Server Span，并自动接收/传播 W3C Trace
Context。项目再创建业务 Span，全部 LangGraph 节点通过统一 `RunnableLambda` 包装器生成子 Span。
真实千问兼容客户端底层使用 HTTPX，因此 HTTPX instrumentation 会继续生成出站 Client Span。

常用 Span：

- `serviceops.agent.chat`：一次新 Agent 请求；
- `serviceops.agent.approval_resume`：一次审批恢复；
- `serviceops.graph.node.<node_name>`：一个真实执行节点；
- `serviceops.audit.append_decision/outcome/failure`：审计证据追加；
- `serviceops.audit.read_chain`：审计员读取并验证链；
- HTTPX Client Span：外部聊天模型/Embedding 请求。

`request_id` 和 `thread_id` 可以进入单次 Trace，用于定位工作流，但不会成为 Metrics 标签。响应
增加可选 `trace_id`，用户报告问题时可以直接提供该值。关闭遥测的自动测试返回 `null`。

官方 FastAPI instrumentation 支持排除 URL、Hook 和 Header 捕获配置：
[OpenTelemetry FastAPI Instrumentation](https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/fastapi/fastapi.html)。
项目排除了 `/health`、`/ready`、`/docs` 和 `/openapi.json`，也不捕获 HTTP Header/Body。

## 正常 interrupt 不能标记为错误

LangGraph `interrupt()` 通过 `GraphInterrupt` 暂停节点。这是正常的 Human-in-the-loop 控制流，
不是服务器异常。统一包装器会：

- 记录节点耗时 `outcome=interrupted`；
- 在 Span 写入 `serviceops.graph.interrupted=true`；
- 保持 Span 状态为 UNSET；
- 原样重新抛给 LangGraph 保存 Checkpoint。

真正异常才设置 Span ERROR，而且只记录 `error.type`。`record_exception=False` 防止第三方异常
message/stacktrace 自动把用户原文、服务商响应或秘密带进遥测后端。

## Metrics 与高基数控制

当前指标：

| 指标 | 类型 | 有限标签 |
|---|---|---|
| `serviceops.agent.executions` | Counter | operation / intent / outcome |
| `serviceops.agent.execution.duration` | Histogram(ms) | operation / intent / outcome |
| `serviceops.graph.node.duration` | Histogram(ms) | node.name / outcome |
| `serviceops.agent.tool.calls` | Counter | tool.name / outcome |
| `serviceops.agent.handoffs` | Counter | intent / outcome |
| `serviceops.approval.executions` | Counter | decision / outcome |

严禁作为 Metrics 标签：

- `user_id`、审批人 ID；
- `request_id`、`thread_id`、Token `jti`；
- 订单号、退货申请号；
- 用户问题、退货原因、备注；
- 任意模型返回字符串或错误消息。

因为 Metrics SDK 必须为每种标签组合维护聚合状态，高基数字段会导致时序和内存成本快速增长。
OpenTelemetry 官方指标文档也将 user ID、原始 URL 等列为典型高基数风险：
[OpenTelemetry Metrics Cardinality](https://opentelemetry.io/docs/concepts/signals/metrics/)。

## 关联 JSON 日志

`TraceJsonFormatter` 只输出：

- UTC timestamp、level、logger、message；
- 当前有效 `trace_id/span_id`；
- 固定白名单 extra：request_id、thread_id、operation、event_type、outcome、failure_code；
- 异常类型。

所有文本会去除 CR/LF/制表符并限制长度。Formatter 不遍历整个 `LogRecord.__dict__`，所以即使
某段错误代码附加 `api_key` 或其他任意字段，也不会被序列化。异常只输出类型，不输出 message 和
stacktrace。完整 Token、Authorization Header、API Key、用户原文和模型响应均不应进入日志。

## Liveness 与 Readiness

`GET /health` 只说明进程存活，不访问数据库，始终适合作为 liveness。

`GET /ready` 会真实执行三项只读检查：

1. `aget_state` 查询固定 Checkpointer 探针线程；
2. `count()` 查询退货业务仓库；
3. `list_for_thread()` 查询审计仓库。

全部成功返回 200/ready；任一失败返回 503/not_ready。响应只包含固定组件状态，不返回数据库路径、
SQL、连接串或异常消息。Kubernetes/负载均衡器可以据此停止把新请求发给故障实例。

## 配置与导出器

`.env` 配置：

```dotenv
SERVICEOPS_TELEMETRY_ENABLED=true
SERVICEOPS_TELEMETRY_EXPORTER=console
SERVICEOPS_OTEL_SERVICE_NAME=serviceops-agent
SERVICEOPS_OTEL_OTLP_ENDPOINT=http://127.0.0.1:4318
SERVICEOPS_OTEL_TRACE_SAMPLE_RATIO=1.0
SERVICEOPS_OTEL_METRIC_EXPORT_INTERVAL_MS=60000
```

三种导出模式：

- `console`：本地学习，直接在 PyCharm 输出 Span/Metrics JSON；
- `otlp_http`：分别发送到 Collector 的 `/v1/traces` 和 `/v1/metrics`；
- `none`：保留 API 调用但不导出。

生产环境启用遥测时禁止 `console`，必须显式选择 `otlp_http` 或 `none`。项目没有内置或偷偷启动
Collector；选择 OTLP 前应由部署环境提供 Collector/Tempo/Jaeger/Prometheus 等后端。官方导出器
说明见：[OpenTelemetry Python Exporters](https://opentelemetry.io/docs/languages/python/exporters/)。

默认开发采样率为 1.0，便于学习。生产不能机械照搬，应根据流量、故障诊断价值、成本、数据保留
和合规要求设置；`ParentBased(TraceIdRatioBased)` 会优先尊重上游采样决定。

## 在 PyCharm 中运行

右键运行：

```text
examples/11_observability_trace.py
```

示例强制使用 mock、Hash Embedding、确定性规划器和内存存储，不调用千问。控制台会输出：

1. 本次 32 位 Trace ID；
2. 两条带相同 trace_id 的单行 JSON 日志；
3. 根业务 Span；
4. normalize/classify/plan/tool/finalize 等子 Span；
5. 节点耗时、Agent 耗时、工具次数等 Metrics。

Console Exporter 输出较长是正常现象。重点搜索 Trace ID，并检查：

- 所有节点的 `trace_id` 相同；
- 节点 `parent_id` 指向根业务 Span；
- `execute_order_tool` 出现两次；
- Metrics 中工具调用值为 2；
- 输出没有用户问题原文、Token 或 API Key。

启动真实 API 后还可以访问：

```text
http://127.0.0.1:8000/health
http://127.0.0.1:8000/ready
```

## 代码位置

- `src/serviceops_agent/observability/telemetry.py`：Provider、Exporter、Span、Metrics、JSON 日志；
- `src/serviceops_agent/graph/builder.py`：全部业务节点统一包装；
- `src/serviceops_agent/api/app.py`：FastAPI/HTTPX instrumentation、业务 Span、readiness；
- `src/serviceops_agent/api/schemas.py`：readiness 与 trace_id 响应契约；
- `examples/11_observability_trace.py`：完全离线观测示例；
- `tests/unit/test_observability.py`：日志脱敏、Trace 关联和生产配置安全门。

## 本步测试

新增重点覆盖：

- JSON 日志注入固定 trace_id/span_id；
- CRLF 被归一，不能伪造日志行；
- 任意 `api_key` extra 不会被序列化；
- 异常只输出类型，不输出敏感 message；
- production 拒绝 console exporter；
- readiness 三个运行时依赖均可读；
- 仓库故障返回脱敏 503；
- 原有 95 项 Agent/RAG/审批/JWT/审计回归继续通过。

完整质量门：

```powershell
uv run ruff check .
uv run mypy src
uv run pytest -q
```

## 面试追问与回答方向

- Trace、Metrics、Logs 分别解决什么？
  Trace 看一次请求跨组件的因果路径和耗时；Metrics 看整体趋势/SLO/告警；Logs 保存离散事件和排障
  上下文。三者通过 trace_id 关联，而不是互相替代。
- 为什么 request_id 能放 Span，不能放 Metrics？
  Span 本来就是单次请求记录；Metrics 会为每种标签组合维护聚合时序，唯一 ID 会造成高基数爆炸。
- 为什么不记录 Prompt 和模型回答？
  它们可能含个人信息、订单数据、秘密和提示注入内容。默认只记录模型名/阶段/耗时/有限结果；若
  企业确需内容审计，应建立独立权限、脱敏、加密和保留策略。
- 为什么 GraphInterrupt 不是 ERROR？
  它代表设计内的可恢复人工暂停。误报会污染错误率、告警和根因分析。
- 为什么不直接使用 OpenTelemetry Logs SDK？
  当前官方状态仍是 Development；项目使用稳定标准库日志加 Trace 关联，未来 Logs SDK 稳定后可替换
  Handler，而不改业务日志调用。
- `/health` 和 `/ready` 为什么分开？
  进程活着不等于能安全处理业务。liveness 失败可能触发重启；readiness 失败只应先摘除流量。
- 当前为什么仍不算完整生产可观测平台？
  已完成应用 instrumentation，但尚未提供 Collector 部署、Dashboard、SLO、告警规则、Tail Sampling、
  遥测后端高可用和成本治理。

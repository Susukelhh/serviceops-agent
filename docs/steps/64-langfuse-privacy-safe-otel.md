# 第 64 步：Langfuse 隐私安全 OTLP 链路

## 接入方式

项目已有 OpenTelemetry Trace、Metrics 和关联日志，本步不再增加一套平行埋点 SDK，而是使用
[Langfuse v4 的 OTLP 入口](https://langfuse.com/docs/observability/features/otel-tracing)发送相同的
安全 Trace。`langfuse_otlp` 模式只创建 Trace Exporter；低基数业务 Metrics 仍由现有 OTel
Collector → Prometheus/Grafana 链路负责。

每个手工业务 Span 增加 `langfuse.observation.type=span`，会话、单轮 Chat 和审批恢复设置稳定的
`langfuse.trace.name`。项目刻意不设置 `input`、`output`、`user.id` 或 `session.id`，所以用户问题、
模型回答、订单号、JWT 和密钥不会因为接入 Langfuse 被复制到第三方平台。现有错误处理也只记录异常
类型，不记录第三方响应正文或堆栈消息。

## 本地启用

把示例复制到 Git 已忽略的密钥文件，并将 `public_key:secret_key` 的 Base64 结果填入 Header：

```powershell
Copy-Item .env.langfuse.example .env.langfuse
docker compose -f compose.yaml -f compose.langfuse.yaml up --build --detach --wait
```

示例配置使用 Langfuse Cloud 的 `https://cloud.langfuse.com/api/public/otel`。自托管时替换域名；Header
格式为：

```dotenv
SERVICEOPS_OTEL_OTLP_HEADERS=Authorization=Basic base64值,x-langfuse-ingestion-version=4
```

Header 由 `SecretStr` 保存，解析器允许 Base64 尾部 `=`，拒绝空值、非法键和大小写重复键，错误信息
不回显配置内容。生产使用时还应在 Langfuse 端设置数据保留期、团队权限和区域，并由组织的隐私政策
决定是否允许第三方托管。

如果需要同时使用 Langfuse 与 Prometheus，推荐让 Collector 做多后端 fan-out；不要同时叠加
`compose.observability.yaml` 与直连 Langfuse 档并假设两个 exporter 会自动合并。


# 第十八步：入口削峰、后端工位与过载恢复基线

## 现实问题

双实例不等于无限容量。真实模型可能一次执行几秒到几十秒；如果瞬间进入几百条请求，最危险的结果
不是“部分用户稍后再试”，而是请求全部堆在内存、占满模型并发和数据库连接，最后整批超时。

这一采用两层保护：

```text
客户端
  │
  ▼
Nginx 取号机
  ├─ 速度超过 5r/s + burst 10 ──► 429 + Retry-After
  ├─ 同来源同时连接超过 20 ─────► 429 + Retry-After
  │
  ▼
agent-a / agent-b
  ├─ 每只最多 8 个业务工位
  ├─ 最多短排队 0.05 秒
  └─ 仍无工位 ──────────────────► 503 + Retry-After
```

Nginx 的速率限制采用漏桶思想：允许稳定平均速率和有限短突发，超过突发容量的请求立即拒绝。连接限制
控制同一来源同时占用多少个正在处理的请求。Agent 内的 `asyncio.BoundedSemaphore` 则保护每个 Python
进程内部的模型、图和数据库调用链。

## 为什么分别使用 429 和 503

| 状态 | 白话含义 | 本项目触发位置 | 客户端动作 |
|---|---|---|---|
| 429 | 你发得太快或同时请求太多 | Nginx gateway | 等待 `Retry-After` 后重试 |
| 503 | 入口允许，但这只 Agent 的工位暂时满了 | FastAPI 容量中间件 | 等待后重试或由平台减少流量 |
| 500/502/504 | 非预期应用、上游或超时故障 | 不属于正常削峰结果 | 告警并调查 |

两种拒绝都返回固定 JSON，不输出当前连接数、客户端地址、Token 或内部异常。FastAPI 容量拒绝增加
`serviceops.http.capacity.rejections` 低基数指标，属性只有固定 `reason=queue_timeout`。

## 哪些路径不进入业务保护

- `/health`：只判断 Web 进程存活；
- `/ready`：供平台读取四类持久化依赖；
- `/docs` 与 `/openapi.json`：本地接口文档；
- `OPTIONS`：浏览器跨域预检，不占 Agent 工位。

`/api/` 下的对话、审批、审计和补偿都可能访问业务资源，因此同时受入口和后端保护。

## 真实本机结果

2026-08-21，Docker 双实例、PostgreSQL、离线确定性后端：

| 阶段 | 结果 |
|---|---|
| 预热 | 1/1 HTTP 200 |
| 正常阶段 | 12/12 HTTP 200 |
| 实例分布 | agent-a 6，agent-b 6 |
| 正常 P50 | 48.65 ms |
| 正常 P95 | 60.65 ms |
| 40 条瞬时突发 | 11 条 200，29 条 429 |
| 非预期故障 | 0 条 500/502/504/连接失败 |
| 三秒后恢复 | HTTP 200，readiness 4/4 |

Nginx 日志中的拒绝请求显示 `rate_limit=REJECTED`、`upstream=-`，证明这些请求在到达 Agent、JWT、
LangGraph 和 PostgreSQL 之前就被入口削峰。正常 12 条严格低于 5r/s，并均匀落到两个实例。

这些延迟只可表述为“当前电脑、Docker、本地网络、离线确定性模型的基线”。真实千问延迟包含公网、
服务商排队、Token 长度和模型生成，不得拿上述毫秒值作为生产 SLA 或简历中的真实模型 QPS。

## 如何运行

先确认服务健康：

```powershell
docker compose up --detach --wait --wait-timeout 120
docker compose ps
```

再运行：

```powershell
uv run python examples/18_load_and_resilience_baseline.py
```

可选参数只用于受控实验：

```powershell
uv run python examples/18_load_and_resilience_baseline.py `
  --steady-requests 20 `
  --burst-requests 60
```

脚本限制正常样本为 5–100、突发样本为 20–200，防止误输入制造无边界流量。报告路径：

```text
D:\serviceops-agent\data\runtime\load_resilience_step18_report.json
```

## 面试官可能追问

### 为什么不是只做 Nginx 限流？

入口按来源 IP 削峰能防止单个调用方过快，但多个来源仍可能一起压满后端。每个 Agent 再用有限工位保护
本进程资源，形成入口与应用两层边界。生产 API Gateway 还应按租户/API Key/用户配额，而不是只按 IP。

### 为什么不让请求一直排队？

无限排队不会增加真实吞吐，只会增加内存、延迟和超时后的重复请求。短暂峰值可由 burst 与极短队列
吸收，超出容量就快速失败，让客户端按 `Retry-After` 和指数退避重试。

### 两个 Agent 一共是不是固定 16 QPS？

不是。8 表示每实例同时在途请求，不是每秒吞吐。吞吐还取决于单次模型延迟、工具调用、数据库耗时、
连接池和服务商配额。必须通过代表性负载测量，不能用“并发数 × 实例数”直接声称 QPS。

### 单个 Nginx 的限流是否适合多网关生产？

本机只有一个网关，其共享内存区可供该 Nginx 的 worker 共用。生产有多个网关时应使用平台级分布式
配额或 API Gateway/Redis 等统一策略，不能假设多台 Nginx 自动共享计数。

## 本步文件

- `deploy/nginx/nginx.conf`：5r/s、burst 10、20 连接与 JSON 429；
- `src/serviceops_agent/config/settings.py`：后端并发工位与短排队配置；
- `src/serviceops_agent/api/app.py`：`BoundedSemaphore` 容量中间件与脱敏 503；
- `src/serviceops_agent/observability/telemetry.py`：容量拒绝低基数指标；
- `examples/18_load_and_resilience_baseline.py`：正常/突发/恢复报告；
- `tests/api/test_app.py`：占满工位后 503、health 仍 200；
- `.github/workflows/container-image.yml`：真实 429、无 5xx、等待后恢复 CI。

## 一手资料

- [Nginx 请求速率限制模块](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html)
- [Nginx 并发连接限制模块](https://nginx.org/en/docs/http/ngx_http_limit_conn_module.html)

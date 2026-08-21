# 第十七步：Alembic、Nginx 双实例与跨实例故障切换

## 这一步解决什么问题

第十六步已经把状态放进共享 PostgreSQL，但当时仍只有一只 API，而且业务表在 API 启动阶段创建。
这意味着“数据库能共享”还没有被真正证明为“两个应用实例能协作”，数据库结构变化也没有独立版本。

第十七步完成三项升级：

1. Alembic 作为业务 Schema 的唯一版本入口；
2. Nginx 作为唯一外部入口，轮询两个无状态 Agent API；
3. 自动停止/恢复实例，证明 A 的 LangGraph 线程可以由 B 接续。

## 白话拓扑

```text
Windows / PyCharm
       │ http://127.0.0.1:8000
       ▼
Nginx gateway（统一前台）
       │ 默认轮询
       ├──────────────┐
       ▼              ▼
   agent-a          agent-b
       └──────┬───────┘
              ▼
       PostgreSQL 18.4
   Checkpoint + 业务 + Outbox + 审计

migrate（一次性任务）── upgrade head ──► PostgreSQL
```

Nginx 像银行取号台；两只 Agent 像两个业务窗口；PostgreSQL 是共用总账本；`migrate` 像营业前一次性
完成装修验收的施工队。施工队完成后退出是正常状态，不能把它误认为服务崩溃。

## 为什么 API 不再负责建业务表

如果每只 API 启动时都尝试修改表结构，就像多个营业员同时改造同一个柜台：启动顺序、权限和失败恢复
都会变得混乱。现在 Compose 的顺序是：

```text
postgres healthy
      ↓
migrate upgrade head → Exited (0)
      ↓
agent-a / agent-b healthy
      ↓
gateway healthy
```

首个 revision 为 `20260821_0001`。它使用 `IF NOT EXISTS` 安全接管第十六步已有表，并写入
`alembic_version`。以后修改字段必须新增 revision，不能直接改写已经执行过的历史脚本。首版 downgrade
故意拒绝自动删表，防止一次误操作删除退货与审计证据。

LangGraph Checkpoint 表仍由官方 `AsyncPostgresSaver.setup()` 管理；Alembic 管理的是项目自己的
`return_requests`、`return_outbox_events` 与 `approval_audit_events`。两类表职责仍然分开。

## 网关做了什么

- Windows 只绑定 `127.0.0.1:8000`，两只 Agent 没有宿主机端口；
- 默认 round-robin，把连续请求轮流交给 A/B；
- 使用 Docker DNS `127.0.0.11` 和 `resolve`，实例重建换 IP 后可重新发现；
- 只对连接错误、超时、502/503/504考虑其他后端，不对普通 4xx 重试；
- 根文件系统只读，所有临时目录进入 `/tmp`；
- 日志记录实际 upstream 地址，但不记录 Authorization 与请求正文；
- `X-ServiceOps-Gateway` 与 `X-ServiceOps-Instance` 让本地演练能证明真实路径。

审批 POST 不依赖 Nginx 自动重放来保证安全。故障切换脚本会等待旧实例从 DNS/upstream 中排空，再向
新实例提交审批；业务侧仍用 LangGraph 线程状态、JWT、幂等键、唯一约束和事务锁形成纵深保护。

## 真实验证结果

2026-08-21 本机完成：

- Alembic 初始版本安全接管第十六步 PostgreSQL 数据；
- `migrate` 退出码为 0；
- gateway、agent-a、agent-b、postgres 全部 healthy；
- 连续六次 `/health` 观察到 B/A/B/A/B/A；
- 容器冒烟：liveness、四依赖 readiness、未认证 401 全部通过；
- A 创建等待审批线程，停 A 后 B 恢复并完成，生成 `RR-` 业务编号；
- B 读取两事件审批审计链，`chain_valid=true`；
- 脚本退出后 A/B 自动恢复；
- 一次性真实 PostgreSQL 中，跨运行时恢复测试与双连接池并发幂等测试共 2/2 通过。

并发测试让两个独立 `psycopg` 连接池同时调用相同幂等键。数据库建议锁按该键串行化竞争，最终两个
调用返回同一 `return_request_id`，`replayed` 恰好为 `[False, True]`，总记录只增加一条。

## 你怎样运行

先启动全部服务：

```powershell
docker compose up --build --detach --wait --wait-timeout 120
docker compose ps
```

再运行自动故障切换：

```powershell
uv run python examples/17_multi_instance_failover.py
```

预期关键输出：

```text
PASS 第17步：Nginx 轮询、跨实例恢复、PostgreSQL 共享状态、审计链全部通过
创建线程实例：agent-a
恢复线程实例：agent-b
```

PyCharm 配置：

- Script path：`D:\serviceops-agent\examples\17_multi_instance_failover.py`；
- Parameters：留空；
- Working directory：`D:\serviceops-agent`；
- Python interpreter：`D:\serviceops-agent\.venv\Scripts\python.exe`。

脚本会安全执行 `stop agent-b`、`stop agent-a`，并在 `finally` 中恢复两只实例。它从不调用
`docker compose down --volumes`，因此不会删除数据库卷。

## 面试官可能追问

### 为什么不用 Uvicorn 多 worker？

单容器单 worker 更容易让容器平台管理健康、扩缩容和故障域。两个独立容器拥有独立进程与连接池，
比同容器多个 worker 更清楚地证明“状态没有依赖进程内存”。

### 两个实例同时处理相同写请求怎么办？

HTTP 幂等键只是入口。数据库内部还使用事务级建议锁串行化相同业务键，并用主键/唯一约束做最终防线。
真实并发测试证明只有一条首次创建，另一条返回同一记录并标记重放。

### Nginx 会不会把审批 POST 重复发两次？

当前没有开启 `non_idempotent` 自动重试。切换演练先排空旧实例再提交 POST；即使客户端自己因超时重试，
LangGraph 已完成状态、审批事件唯一约束和业务幂等键仍会拒绝或安全重放，而不是生成第二条退货。

### 这是否等于生产高可用？

不是。它证明应用无状态化和共享持久化原则。生产还需要云负载均衡/TLS、至少两个故障域、PostgreSQL
高可用与备份恢复、集中 Secret、监控告警、容量压测、滚动发布和迁移审批。

## 本步文件

- `alembic.ini`：本地 Alembic 基础配置；
- `src/serviceops_agent/migrations/`：迁移环境与版本脚本；
- `src/serviceops_agent/infrastructure/migrate.py`：容器一次性迁移入口；
- `compose.yaml`：gateway、agent-a、agent-b、migrate、postgres；
- `deploy/nginx/nginx.conf`：轮询、DNS、超时、日志和只读目录配置；
- `examples/17_multi_instance_failover.py`：自动 A→B 故障切换验证；
- `tests/integration/test_postgres_runtime.py`：跨运行时和并发幂等真实数据库测试；
- `tests/unit/test_container_contract.py`：Compose/Nginx/Alembic 静态契约。

## 一手资料

- [Alembic 官方文档](https://alembic.sqlalchemy.org/en/latest/)
- [Nginx 官方 HTTP 负载均衡](https://nginx.org/en/docs/http/load_balancing.html)
- [Nginx upstream 模块](https://nginx.org/en/docs/http/ngx_http_upstream_module.html)
- [Docker Compose 启动顺序](https://docs.docker.com/compose/how-tos/startup-order/)

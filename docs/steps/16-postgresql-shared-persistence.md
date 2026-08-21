# 第十六步：PostgreSQL 共享持久化与 API 容器重建恢复

## 本步结论

项目已经从 Compose 内的单机 SQLite 状态升级为独立 PostgreSQL 服务。SQLite 没有删除，仍可作为
不启动 Docker 时的本地学习模式；Compose 默认使用 PostgreSQL，让未来多个 API 实例能够共同访问
同一份 LangGraph Checkpoint、退货业务记录、事务 Outbox 和审批审计链。

本步完成的是“多实例共享存储基础”，不是把本机 Compose 包装成完整生产平台。真正上线仍需要外部
Secret、备份恢复、数据库高可用、Schema Migration、负载均衡、容量规划和压测。

## 白话拓扑

```text
浏览器 / 调用方
       │ 127.0.0.1:8000
       ▼
serviceops-agent 容器
  FastAPI + LangGraph + 业务规则
       │ Docker 内部网络
       ▼
postgres 容器（5432 不映射到 Windows）
       │
       ▼
serviceops-postgres 具名卷
```

API 容器像营业窗口，PostgreSQL 像后方总档案室。营业窗口可以关闭重开；总档案室和数据卷不删除，
新窗口仍能查到旧业务。

## 三种运行模式

| 后端 | 用途 | 保存位置 |
|---|---|---|
| `memory` | 自动测试、完全隔离评测 | 当前 Python 进程 |
| `sqlite` | 不启动 Docker 的单机学习 | 两个本地 SQLite 文件 |
| `postgres` | Compose 与多实例共享状态基础 | 独立 PostgreSQL 服务 |

`Settings` 在选择 `postgres` 时强制要求 `SERVICEOPS_POSTGRES_DSN`，连接地址使用 `SecretStr`，打印配置
不会显示密码。连接池最大值不能小于最小值，错误会在应用启动阶段失败。

## Checkpoint 与业务表为什么分开

官方 `AsyncPostgresSaver` 创建四类表：

- `checkpoints`：某个线程的状态快照；
- `checkpoint_blobs`：较大的序列化字段；
- `checkpoint_writes`：执行过程写入；
- `checkpoint_migrations`：Saver 自己的表版本。

项目业务仓储创建三张表：

- `return_requests`：真实退货申请；
- `return_outbox_events`：与业务同事务提交的待投递事件；
- `approval_audit_events`：只追加审批哈希链。

Checkpoint 表达“Agent 做到哪里”，业务表表达“真实世界发生什么”。即使以后清理历史 Checkpoint，
也不能因此删除法务、售后或审计需要的真实业务记录。

## 连接池与事务

`psycopg_pool.ConnectionPool` 在 API 启动时建立最小连接数，高峰时最多扩展到配置上限。每次仓储操作
短暂借一条连接，退出上下文后提交或回滚并归还。

批准退货时，`return_requests` 和 `return_outbox_events` 使用同一连接、同一事务：

```text
BEGIN
  INSERT return_requests
  INSERT return_outbox_events
COMMIT
```

任一插入失败，两条记录都回滚。这消除了“退货成功但审计事件没有留下”的丢失窗口。

## 并发控制

- 退货申请按 `idempotency_key` 获取事务级建议锁；
- Outbox 按稳定 `event_id` 获取事务级建议锁；
- 审计链按 `thread_id` 获取事务级建议锁；
- Outbox 状态推进使用 `SELECT ... FOR UPDATE` 行锁；
- 主键与唯一约束作为最后一道数据库防线。

建议锁只让同一个业务键互相等待，不会把所有订单都锁成一条队伍。数据库事务结束后会自动释放，
应用崩溃不会永久遗留一把锁。

## 真实故障与修复证据

第一次真实批准请求暴露：可选 `thread_id` 为 `NULL` 时，PostgreSQL 无法推断 SQL 参数 `$2` 的类型，
Outbox 查询抛出 `IndeterminateDatatype`。修复是在空值判断处明确转换为 `text`。

故障发生前业务记录与 Outbox 已经完整提交，因此事件仍是 `pending`。修复重建 API 后调用受 RBAC
保护的补偿接口，结果为：

```json
{"scanned":1,"processed":1,"replayed":0,"failed":0,"dead_letter":0}
```

这次问题说明真实数据库集成测试不可由 SQLite 单元测试完全替代，也证明 Transactional Outbox 能把
“暂时无法投递”变成可观测、可重试状态，而不是静默丢失事实。

## 真实重建验证

```powershell
uv run python examples/16_postgres_docker_persistence.py --mode write
docker compose up -d --force-recreate --wait agent-a agent-b gateway
uv run python examples/16_postgres_docker_persistence.py --mode verify
```

本机实测结果：

- 新 API 容器从 PostgreSQL 读取旧线程，重复审批返回 409；
- 审计接口返回重建前的两事件哈希链，`chain_valid=true`；
- 完成事件中的 `return_request_id` 与写入阶段一致；
- 两条 Outbox 全部处于 `processed`；
- PostgreSQL UPDATE 审计表被只追加触发器拒绝；
- API 容器仍以 `uid/gid 10001` 和只读根文件系统运行；
- 离线 Agent Eval 为 13/13，四层准确率与安全不变量均为 100%。

## 自动质量门

`.github/workflows/postgres-integration.yml` 在一次性 PostgreSQL 18.4 数据库上运行专用测试，覆盖：

1. 第一套运行时创建 interrupt 后完全关闭；
2. 第二套运行时恢复并批准，创建业务和 Outbox；
3. 使用 JSONB `thread_id` 过滤取得自己的 pending 事件；
4. 使用行锁推进 processed；
5. 第三套运行时读取完成 Checkpoint、业务记录和 Outbox 状态。

普通本地测试不提供 `SERVICEOPS_TEST_POSTGRES_DSN` 时会安全显示 skipped，绝不猜测或误连开发数据库。

## 面试表达

> 我保留 SQLite 作为低门槛本地模式，Compose 默认迁移到 PostgreSQL。LangGraph 使用官方
> AsyncPostgresSaver，业务侧用 psycopg 连接池。退货与 Outbox 同事务；幂等键、唯一约束和建议锁
> 防止多实例重复写，审计链按 thread_id 串行追加。我做了 API 容器强制重建恢复验证，并用一次
> 真实参数类型故障验证 Outbox 补偿，最后把真实 PostgreSQL 场景加入独立 CI。

## 本步代码位置

- `src/serviceops_agent/config/settings.py`：三种后端、DSN 与连接池配置；
- `src/serviceops_agent/infrastructure/postgres_repository.py`：业务、Outbox、审计 PostgreSQL 实现；
- `src/serviceops_agent/infrastructure/runtime.py`：连接池与官方 Saver 生命周期；
- `compose.yaml`：本步最初为 API + PostgreSQL；第十七步已升级为网关 + 双 Agent + 迁移 + PostgreSQL；
- `examples/16_postgres_docker_persistence.py`：写入/重建/恢复验证；
- `tests/integration/test_postgres_runtime.py`：专用真实数据库集成测试；
- `.github/workflows/postgres-integration.yml`：Push/PR 临时 PostgreSQL 门禁。

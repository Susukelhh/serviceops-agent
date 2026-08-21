# 第八步：SQLite 持久化与跨进程重启恢复

> 本文记录第八步持久化实现。第九步之后，真实 API 调用还必须分别携带具有 `agent:chat` 或
> `return:approve` Scope 的 Bearer JWT。

## 本步解决的问题

第七步虽然实现了真实 `interrupt` 和 `Command.resume`，但 `InMemorySaver` 与内存退货仓库都在
进程退出后丢失。如果审批人在服务重启后再点击“批准”，原 `thread_id` 找不到状态；即使工作流
恢复，业务幂等记录丢失也可能重复创建。

本步增加两套可以切换的运行模式：

```text
memory
├── InMemorySaver
└── InMemoryReturnRequestRepository

sqlite
├── AsyncSqliteSaver            → data/runtime/checkpoints.sqlite3
└── SQLiteReturnRequestRepository → data/runtime/serviceops.sqlite3
```

默认本地运行使用 `sqlite`，自动测试通过 `tests/conftest.py` 强制使用 `memory`，所以测试不会污染
开发者运行数据。

## 为什么是 AsyncSqliteSaver

FastAPI 路由和 LangGraph 都使用 `ainvoke`、`aget_state`。异步图会调用 Checkpointer 的
`aput`、`aget_tuple`、`aput_writes` 等异步接口，因此同步 `SqliteSaver` 不能混用。本项目安装
独立包 `langgraph-checkpoint-sqlite`，使用：

```python
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
```

LangGraph 官方将 SQLite Saver 定位为本地、轻量工作流，将 PostgreSQL Saver 定位为更合适的
生产方案：

- [LangGraph Persistence 官方文档](https://docs.langchain.com/oss/python/langgraph/persistence)
- [AsyncSqliteSaver 官方源码与示例](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/aio.py)

## Checkpoint 数据库不等于业务数据库

两个文件故意分开：

- `checkpoints.sqlite3` 保存图 State、下一执行节点、pending writes 和线程历史；
- `serviceops.sqlite3` 保存真实退货申请业务记录和幂等键。

Checkpoint 可以回答“Agent 执行到哪里”，业务库回答“退货申请是否真实存在”。删除工作流历史
不应删除业务申请；业务记录存在也不能自动推断当前审批节点。面试时不要把两者说成同一份记忆。

## FastAPI lifespan 管理资源

`AsyncSqliteSaver.from_conn_string(...)` 内部持有 `aiosqlite` 连接和后台线程，必须在服务启动时
创建、关闭时释放。本项目使用 FastAPI lifespan：

```text
Uvicorn startup
→ create_agent_runtime(settings)
→ 建立业务表与 Checkpoint 表
→ 编译绑定当前 Saver/Repository 的 ServiceGraph
→ app.state.service_graph = graph
→ 接收请求
→ Uvicorn shutdown
→ 关闭 AsyncSqliteSaver 连接
```

如果数据库初始化失败，异常发生在 startup，服务不会带着一个不可用的持久化依赖假装启动成功。
图与 Saver 生命周期一致，连接关闭后不会继续处理新请求。

## SQLite 业务幂等实现

`SQLiteReturnRequestRepository` 使用以下防线：

1. `idempotency_key TEXT PRIMARY KEY`：数据库层唯一约束；
2. `return_request_id UNIQUE`：业务编号不能重复；
3. `BEGIN IMMEDIATE`：读取幂等键前取得保留写锁；
4. 参数化 SQL：外部字符串不参与 SQL 拼接；
5. 同键同负载：提交事务并返回原记录；
6. 同键不同负载：回滚并返回幂等冲突；
7. 新键：在事务中再次检查订单归属/签收状态后插入；
8. `WAL + busy_timeout`：改善本地读写并发并短暂等待写锁。

测试使用两个独立仓库实例和两个线程并发提交同一幂等键，结果必须恰好“一次创建、一次重放”，
数据库最终只有一行。

SQLite 使用单写者模型，`BEGIN IMMEDIATE` 会串行化写事务；这适合本地演示，不适合高并发、多
实例跨机器部署。生产 PostgreSQL 应使用唯一索引、事务、连接池，并根据业务决定冲突后读取还是
返回错误。

## State 的可移植序列化

第七步已经把可持久化 State 中的意图、退货草案、流程状态和审批决定转换为 JSON 字符串/字典。
恢复节点再用 Pydantic 重建领域对象。这样 SQLite Checkpoint 不需要反序列化任意项目 Python
类，也降低代码升级后旧快照无法加载的风险。

仍需注意：State Schema 一旦发生不兼容变更，需要设计版本字段和迁移/兼容读取策略，不能假设
历史 Checkpoint 永远与新代码结构一致。

## 配置

`.env` 新增：

```dotenv
SERVICEOPS_PERSISTENCE_BACKEND=sqlite
SERVICEOPS_CHECKPOINT_DATABASE_PATH=data/runtime/checkpoints.sqlite3
SERVICEOPS_BUSINESS_DATABASE_PATH=data/runtime/serviceops.sqlite3
```

相对路径通过 `resolve_project_path` 固定解析到项目根目录，所以从 PyCharm、PowerShell 或其他
目录启动都指向同一文件。`data/runtime/` 已在 `.gitignore` 中，运行数据不会提交到 Git。

如果只想进行一次无磁盘实验，可以临时设置：

```dotenv
SERVICEOPS_PERSISTENCE_BACKEND=memory
```

修改 `.env` 后必须重启 Uvicorn，让 lifespan 重新装配资源。

## 在 PyCharm 中运行

右键运行：

```text
examples/08_sqlite_restart_recovery.py
```

示例使用临时目录模拟三次完整资源生命周期：

1. 运行 A 发起申请，在 interrupt 暂停，业务记录数为 0；
2. 关闭全部 Saver/仓库资源；
3. 运行 B 创建全新对象，从磁盘找到原审批节点并批准；
4. 再次关闭全部资源；
5. 运行 C 验证图已经完成、申请编号一致、业务库仍有一行。

预期关键输出：

```text
运行 A：中断数量 1，退货记录数 0
运行 B：下一节点 request_return_approval，批准后记录数 1
运行 C：下一节点为空，Checkpoint 与业务编号一致
```

脚本强制使用离线模型/RAG配置，不调用千问，不消耗 Token；退出后会自动清理临时数据库。

## 用真实 API 验证重启恢复

启动服务并调用 `POST /api/v1/chat` 发起 `SO100002` 的退货申请，把响应里的 `thread_id` 保存
下来。随后：

1. 按 `Ctrl+C` 完全停止 Uvicorn；
2. 再次执行 `uv run uvicorn serviceops_agent.api.app:app --reload`；
3. 使用原 `thread_id` 调用 `POST /api/v1/approvals/{thread_id}`；
4. 应能恢复并返回 `RR-...`；
5. `GET /health` 的 `persistence_backend` 应为 `sqlite`。

不要删除 `data/runtime/checkpoints.sqlite3`，否则工作流恢复数据自然不存在；不要只保留 Checkpoint
却删除业务库，否则已经完成的工作流与业务事实会不一致。

## 本步测试

当前共 78 个测试，本步新增：

- SQLite 记录关闭原实例后仍可由新实例读取；
- 两个独立连接并发同键提交时一创一重放；
- 第一个 Runtime interrupt 后完全关闭；
- 第二个 Runtime 从相同 `thread_id` 恢复并批准；
- 第三个 Runtime 仍能读取工作流终态和业务记录；
- 原有 75 个分类、RAG、Agent 循环、审批和 API 测试全部通过。

## 面试追问与回答方向

- 为什么不能只持久化 Checkpoint？
  Checkpoint 是工作流执行状态，不是真实业务事实；写工具必须写独立业务库。
- 为什么不能只持久化业务记录？
  审批暂停时尚无业务记录，仍需要 Checkpoint 保存草案、执行位置和 thread_id。
- 为什么异步 API 不能使用 SqliteSaver？
  异步图调用 Checkpointer 的异步方法，应使用 AsyncSqliteSaver，避免缺失方法或阻塞事件循环。
- thread_id 和 idempotency_key 有什么区别？
  前者定位一条工作流线程，后者标识一次业务写请求；新线程重试仍需同一业务幂等键。
- BEGIN IMMEDIATE 解决了什么？
  它在检查主键前串行化 SQLite 写事务，避免两个连接都看到“不存在”后竞争插入。
- 这算生产级持久化吗？
  算可验证的本地持久化切片，不算多实例生产方案；生产应换 PostgreSQL、鉴权、审计和监控。
- State 升级后旧 Checkpoint 怎么办？
  State 应有版本，读取时兼容旧结构或离线迁移；不能直接删除历史快照掩盖兼容问题。

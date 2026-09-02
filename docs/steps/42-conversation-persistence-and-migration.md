# 第42步：实现会话持久化与数据库迁移

## 这一步解决什么问题

第41步只有领域对象。服务一旦重启，若没有仓库实现，会话、轮次序号、幂等状态和结构化记忆都会丢失。
第42步提供同一份 `ConversationRepository` 协议的内存、SQLite 和 PostgreSQL 三种实现。

## 仓库能力

统一协议支持：

- 创建带所有者和过期时间的会话；
- 按 `conversation_id + owner_user_id` 读取，越权与不存在对外不可区分；
- 原子创建轮次并分配递增序号；
- 按会话读取有限的最近轮次；
- 用 `memory_version` 乐观更新结构化记忆；
- 按预期状态原子推进轮次；
- 执行只读计数用于测试和健康诊断。

API 与未来的上下文解析器只依赖协议，不需要知道当前运行在内存、单机 SQLite 还是多实例 PostgreSQL。

## 幂等与并发

每个会话内有两组唯一约束：

- `(conversation_id, sequence_number)` 防止并发请求获得相同轮次序号；
- `(conversation_id, idempotency_key)` 保证同一个客户端请求只对应一轮。

相同幂等键和相同消息会返回原轮次；相同键配不同消息会明确冲突，不能静默复用旧结果。
轮次推进使用 `expected_status` 比较更新，两个请求不能同时把同一轮从 `accepted` 推进到 `running`。

会话记忆同样使用乐观版本：调用方读取版本 `N` 后，只能提交版本 `N+1`。若另一个请求已经更新，当前请求必须重新读取，
不能用较旧上下文覆盖新记忆。

## 三种后端的职责

- 内存仓库：线程锁保护原子操作，供快速单元测试和无数据库模式使用；
- SQLite仓库：事务、WAL、外键和唯一索引支持单机跨重启运行；
- PostgreSQL仓库：事务、行锁、JSONB 和数据库唯一约束支持多个 API 实例共享状态。

SQLite 会为本地学习自动创建表。PostgreSQL 遵循生产权限边界，由独立 Alembic 迁移创建结构，API 启动过程不执行业务 DDL。

## 数据表

`conversations` 保存所有者、状态、结构化记忆、版本和生命周期时间。

`conversation_turns` 保存轮次序号、客户端幂等键、独立 `workflow_thread_id`、状态、问题、答案、已验证订单号和引用文档号。
它不会把任意 LangGraph/Python 对象序列化进业务表。

迁移降级故意失败关闭，因为直接删除这两张表会不可恢复地删除用户会话历史；如果未来确实需要回滚，应先制定备份和数据迁移方案。

## 验证结果

完成第42步后运行完整离线门禁：

- Ruff：通过；
- Mypy：96个源文件通过；
- Pytest：297通过，3跳过。

3个跳过项包含需要显式 `SERVICEOPS_TEST_POSTGRES_DSN` 的真实 PostgreSQL 集成测试。本地无数据库时不会伪装成已验证 PostgreSQL，
内存与 SQLite 的重放、重启、并发和所有权边界均已自动测试。

## 本步文件

- 仓库协议与三种实现：`src/serviceops_agent/infrastructure/conversation_repository.py`
- Alembic迁移：`src/serviceops_agent/migrations/versions/20260829_0002_conversation_memory.py`
- 仓库测试：`tests/unit/test_conversation_repository.py`
- PostgreSQL集成测试：`tests/integration/test_postgres_conversation_repository.py`

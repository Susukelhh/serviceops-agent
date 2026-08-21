# 第 16 步学习记录：PostgreSQL 共享持久化

## 我现在应该先记住什么

- SQLite 像单人抽屉，仍适合不启动 Docker 的本地学习。
- PostgreSQL 像共享档案室，多个 API 实例可以读取同一套进度和业务数据。
- 连接池像有限银行窗口，请求借用连接，办完归还。
- 事务保证退货记录与 Outbox 事件一起成功或一起回滚。
- Checkpoint 保存“Agent 做到哪一步”，业务表保存“真实业务发生了什么”。

## 本步真实完成

- 新增 `postgres` 持久化模式，同时保留 `memory` 与 `sqlite`。
- LangGraph 使用官方 `AsyncPostgresSaver`。
- 业务仓储使用 psycopg 3 连接池、TIMESTAMPTZ、JSONB、唯一约束、行锁和建议锁。
- Docker Compose 启动 API 与 PostgreSQL 两个容器；5432 不映射到 Windows。
- 强制重建 API 容器后，旧线程终态和审批哈希链仍可读取。
- 一次真实 SQL 参数类型故障由 Outbox 保留事件，修复后补偿成功。
- 新增真实 PostgreSQL 集成测试与独立 GitHub Actions 门禁。

## 当前不要夸大的地方

本步解决了多应用实例共享状态的数据库基础，但本机 Compose 不等于完整生产系统。
真实上线仍需要外部密钥管理、数据库备份与恢复、高可用、正式 Schema Migration、
负载均衡、连接容量规划、监控告警和压测。

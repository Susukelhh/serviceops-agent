"""ServiceOps 业务数据库的 Alembic 迁移环境。

LangGraph 官方 Checkpointer 自己维护 checkpoint_migrations；本包只管理退货、Outbox 和
审批审计三类业务表，避免应用表与框架内部表共享一套错误的版本生命周期。
"""

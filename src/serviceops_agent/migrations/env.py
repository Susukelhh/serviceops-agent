"""Alembic 迁移运行环境：安全读取 PostgreSQL DSN 并执行版本脚本。"""

# os 只读取显式的 SERVICEOPS_POSTGRES_DSN，不扫描或打印其他环境变量。
import os

# fileConfig 根据 alembic.ini 初始化有限日志格式；程序化容器迁移没有 ini 时会跳过。
from logging.config import fileConfig

# Alembic context 提供离线生成 SQL 和在线连接数据库两种运行方式。
from alembic import context

# SQLAlchemy 创建 Alembic 使用的同步连接池；迁移完成后连接立即释放。
from sqlalchemy import engine_from_config, pool

# 当前 Alembic Config 由 CLI 的 alembic.ini 或项目迁移入口程序化创建。
config = context.config

# 只有使用真实 ini 文件时才配置日志；连接地址不会出现在日志格式中。
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 当前项目用显式 SQL 版本脚本，不使用 ORM Metadata 自动生成表结构。
target_metadata = None


def _to_sqlalchemy_url(raw_dsn: str) -> str:
    """把 psycopg 通用 DSN 明确转换为 SQLAlchemy 2 的 psycopg 3 驱动 URL。"""

    # 空地址会导致 Alembic 在难以理解的位置失败，因此先给出固定安全错误。
    if not raw_dsn.strip():
        raise RuntimeError("数据库迁移需要 SERVICEOPS_POSTGRES_DSN")
    # postgresql:// 没有指定 DBAPI；本项目明确使用已锁定的 psycopg 3。
    if raw_dsn.startswith("postgresql://"):
        return raw_dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    # 已经明确 postgresql+psycopg:// 时保持原值。
    if raw_dsn.startswith("postgresql+psycopg://"):
        return raw_dsn
    # 其他协议可能绕过预期驱动和安全配置，启动阶段直接拒绝。
    raise RuntimeError("数据库迁移只支持 PostgreSQL psycopg 连接地址")


def _database_url() -> str:
    """优先读取程序化 URL，否则读取专用环境变量；绝不打印返回值。"""

    # 容器迁移入口会直接设置 sqlalchemy.url，普通 CLI 则通常依赖环境变量。
    configured_url = config.get_main_option("sqlalchemy.url", "").strip()
    # 非空配置同样通过驱动规范化，防止默认落到未安装的 psycopg2。
    if configured_url:
        return _to_sqlalchemy_url(configured_url)
    # 环境变量只在当前进程内使用，不写入 alembic.ini 或版本脚本。
    return _to_sqlalchemy_url(os.getenv("SERVICEOPS_POSTGRES_DSN", ""))


def run_migrations_offline() -> None:
    """不连接数据库地生成 SQL，供代码审查和受控发布流水线使用。"""

    # literal_binds 把迁移脚本中的固定值渲染进 SQL；业务参数不经过迁移系统。
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    # 所有版本操作放进明确事务块。
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """连接目标 PostgreSQL，在事务中执行尚未应用的版本脚本。"""

    # NullPool 保证一次迁移只持有自己的短连接，不与 API 业务连接池混用。
    connectable = engine_from_config(
        {"sqlalchemy.url": _database_url()},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    # Engine 上下文结束时关闭数据库连接。
    with connectable.connect() as connection:
        # 把真实连接交给 Alembic；compare_type 为后续类型变更检测保留准确语义。
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        # PostgreSQL DDL 支持事务；任一迁移失败会整体回滚并阻止 API 启动。
        with context.begin_transaction():
            context.run_migrations()


# --sql 使用离线模式，普通 upgrade/current 使用在线模式。
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

"""容器和本地都可复用的 ServiceOps 业务数据库迁移入口。"""

# Path 从已安装包定位随 wheel 发布的 migrations 目录，不依赖当前 Working directory。
from pathlib import Path

# Alembic command 执行 upgrade；Config 允许不依赖容器中的 alembic.ini。
from alembic import command
from alembic.config import Config

# SecretStr 允许集成测试显式传入临时数据库 DSN，同时避免常规打印泄露密码。
from pydantic import SecretStr

# Settings 负责 DSN 必填校验和 SecretStr 脱敏。
from serviceops_agent.config.settings import get_settings


def build_alembic_config(*, postgres_dsn: SecretStr | None = None) -> Config:
    """根据已验证 Settings 创建不包含日志明文密码的程序化 Alembic 配置。"""

    # 集成测试可传入临时 DSN；正常容器运行则从已经验证过的 Settings 读取。
    selected_postgres_dsn = postgres_dsn
    # 只有调用者没有显式传值时才读取全局配置，避免测试修改整个进程的环境变量。
    if selected_postgres_dsn is None:
        # Compose 迁移服务明确选择 postgres 后端，因此 Settings 会校验 DSN 必填。
        settings = get_settings()
        # 组合校验已保证 postgres 模式下 DSN 非空，assert 只帮助类型检查器收窄类型。
        assert settings.postgres_dsn is not None
        # 保存经过 Pydantic 校验并用 SecretStr 包装的连接字符串。
        selected_postgres_dsn = settings.postgres_dsn
    # migrations 与当前模块同属已安装 serviceops_agent 包，源码/wheel 路径都可定位。
    migrations_path = Path(__file__).resolve().parents[1] / "migrations"
    # 不传 ini 路径，避免日志系统意外输出连接 URL。
    config = Config()
    # Alembic 需要明确版本脚本根目录。
    config.set_main_option("script_location", str(migrations_path))
    # DSN 只存在于当前 Config 内存；env.py 会再规范为 psycopg 3 URL。
    config.set_main_option(
        "sqlalchemy.url",
        selected_postgres_dsn.get_secret_value().replace("%", "%%"),
    )
    # 返回只供当前进程执行一次迁移的配置。
    return config


def main() -> int:
    """把数据库升级到最新 revision；失败由非零进程状态阻止 API 启动。"""

    # upgrade head 按版本链依次执行所有尚未应用的迁移。
    command.upgrade(build_alembic_config(), "head")
    # 没有异常即表示数据库已经处于最新业务结构版本。
    print("PASS: ServiceOps 业务数据库迁移已升级到 head")
    # 返回零供 Docker Compose service_completed_successfully 判断。
    return 0


# python -m serviceops_agent.infrastructure.migrate 的标准入口。
if __name__ == "__main__":
    raise SystemExit(main())

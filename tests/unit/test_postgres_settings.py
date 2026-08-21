"""验证 PostgreSQL 配置在应用启动前就能发现常见错误。"""

# pytest 用于断言错误配置会抛出明确的校验异常。
import pytest

# SecretStr 用于验证数据库密码不会在配置对象字符串中泄漏。
from pydantic import SecretStr, ValidationError

# Settings 是本组测试要验证的统一环境配置入口。
from serviceops_agent.config.settings import Settings


def test_postgres_backend_requires_dsn() -> None:
    """选择 PostgreSQL 却没有连接地址时，应在启动阶段立刻失败。"""

    # pytest.raises 表示这里预期 Settings 主动拒绝不完整配置。
    with pytest.raises(ValidationError, match="SERVICEOPS_POSTGRES_DSN"):
        # 显式传入 None，避免开发者机器上的同名环境变量影响测试结果。
        Settings(persistence_backend="postgres", postgres_dsn=None)


def test_postgres_pool_rejects_inverted_size_range() -> None:
    """最大连接数不能比最小连接数还小。"""

    # 错误连接池范围会让运行期行为难以预测，因此在配置解析时阻止它。
    with pytest.raises(ValidationError, match="最大连接数不能小于最小连接数"):
        # 测试地址只是假数据，不会建立真实网络连接。
        Settings(
            persistence_backend="postgres",
            postgres_dsn=SecretStr("postgresql://user:password@localhost/database"),
            postgres_pool_min_size=5,
            postgres_pool_max_size=2,
        )


def test_postgres_dsn_is_masked_in_settings_representation() -> None:
    """打印配置对象时不能把数据库密码原样显示出来。"""

    # 把容易辨认的假密码放入 DSN，便于直接检查是否发生泄漏。
    settings = Settings(
        persistence_backend="postgres",
        postgres_dsn=SecretStr(
            "postgresql://serviceops:super-secret-password@localhost/serviceops"
        ),
    )

    # SecretStr 的 repr 应显示掩码，因此原始密码不能出现在配置对象文本中。
    assert "super-secret-password" not in repr(settings)


def test_agent_capacity_settings_reject_zero_or_unbounded_wait() -> None:
    """后端并发工位必须为正数，排队等待也必须有明确上限。"""

    # 并发数为零意味着所有业务请求永久不可用，Pydantic 应在启动阶段拒绝。
    with pytest.raises(ValidationError):
        Settings(agent_max_in_flight_requests=0)
    # 超过五秒的内部排队会放大上游超时和内存占用，同样不能启动。
    with pytest.raises(ValidationError):
        Settings(agent_capacity_queue_timeout_seconds=6.0)

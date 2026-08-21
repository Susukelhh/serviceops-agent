"""SQLite 退货仓库的跨实例持久化和并发幂等单元测试。"""

# ThreadPoolExecutor 让两个独立仓库实例同时提交相同业务请求。
from concurrent.futures import ThreadPoolExecutor

# Path 是 pytest tmp_path fixture 的类型。
from pathlib import Path

# 默认订单仓库提供 user-001 已签收的 SO100002。
from serviceops_agent.infrastructure.order_repository import default_order_repository

# SQLiteReturnRequestRepository 是本文件直接验证的磁盘业务仓库。
from serviceops_agent.infrastructure.return_repository import (
    SQLiteReturnRequestRepository,
)


def _build_repository(database_path: Path) -> SQLiteReturnRequestRepository:
    """为同一个临时数据库文件创建一个全新仓库实例。"""

    # 每个实例拥有独立短连接，但共享数据库唯一约束和 WAL 文件。
    return SQLiteReturnRequestRepository(
        database_path=database_path,
        order_repository=default_order_repository,
    )


def test_sqlite_repository_persists_idempotent_record_across_instances(
    tmp_path: Path,
) -> None:
    """关闭首个仓库对象后，新实例应返回同一幂等记录。"""

    # Arrange：数据库放在 pytest 自动清理的临时目录中。
    database_path = tmp_path / "serviceops.sqlite3"
    # 第一实例模拟服务启动 A。
    first_repository = _build_repository(database_path)

    # Act：首次提交创建真实磁盘记录。
    first_record, first_is_replay = first_repository.create_or_get(
        user_id="user-001",
        order_id="SO100002",
        reason="商品尺寸不合适",
        idempotency_key="sqlite-restart-001",
    )
    # 第二实例模拟原进程退出后的服务启动 B。
    second_repository = _build_repository(database_path)
    # Act：使用完全相同业务负载重试。
    second_record, second_is_replay = second_repository.create_or_get(
        user_id="user-001",
        order_id="SO100002",
        reason="商品尺寸不合适",
        idempotency_key="sqlite-restart-001",
    )

    # Assert：第一实例确实创建了记录。
    assert first_is_replay is False
    # Assert：第二实例从磁盘识别为幂等重放。
    assert second_is_replay is True
    # Assert：跨实例返回同一业务编号。
    assert second_record.return_request_id == first_record.return_request_id
    # Assert：数据库中仍只有一行。
    assert second_repository.count() == 1


def test_sqlite_repository_serializes_concurrent_same_key_writes(tmp_path: Path) -> None:
    """两个连接并发提交同键同负载时应一创一重放，而不是插入两行。"""

    # Arrange：两个仓库实例模拟两个请求处理线程或本地服务组件。
    database_path = tmp_path / "concurrent-serviceops.sqlite3"
    repositories = (
        _build_repository(database_path),
        _build_repository(database_path),
    )

    def submit(repository: SQLiteReturnRequestRepository) -> tuple[str, bool]:
        """在线程中提交同一业务请求并返回编号与重放标记。"""

        # BEGIN IMMEDIATE 会让第二个连接等待第一个事务提交后再检查主键。
        record, is_replay = repository.create_or_get(
            user_id="user-001",
            order_id="SO100002",
            reason="商品包装出现明显破损",
            idempotency_key="sqlite-concurrent-001",
        )
        # 只返回测试需要的稳定字段。
        return record.return_request_id, is_replay

    # Act：两个工作线程同时调用各自仓库实例。
    with ThreadPoolExecutor(max_workers=2) as executor:
        # executor.map 会等待两个调用完成并保留输入顺序。
        results = list(executor.map(submit, repositories))

    # Assert：两个调用得到同一个业务编号。
    assert results[0][0] == results[1][0]
    # Assert：恰好一次首次创建、一次幂等重放。
    assert sorted(result[1] for result in results) == [False, True]
    # Assert：数据库主键层最终只有一条记录。
    assert repositories[0].count() == 1

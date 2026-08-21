"""第十九步 PostgreSQL 备份与恢复保护边界的单元测试。"""

# json 读取运维组件生成的有限报告。
import json

# Path 为隔离测试目录和伪 Docker 路径提供类型。
from pathlib import Path

# pytest 提供临时目录、monkeypatch 和异常断言。
import pytest

# 被测模块以模块形式导入，便于把真实 Docker 客户端替换为纯内存假对象。
from serviceops_agent.operations import postgres_backup

# 公开异常与名称保护函数用于验证危险目标会在执行命令前被拒绝。
from serviceops_agent.operations.postgres_backup import (
    PostgresBackupError,
    create_drill_database_name,
    validate_drill_database_name,
)

# 两张假表足以验证表集合、行数与内容哈希会进入比较契约。
SOURCE_FINGERPRINT = {
    "alembic_version": {
        "row_count": 1,
        "content_hash": "a" * 32,
    },
    "checkpoints": {
        "row_count": 2,
        "content_hash": "b" * 32,
    },
}


class FakeDockerPostgresClient:
    """不访问 Docker 的成功路径测试替身。"""

    def __init__(self, *, docker_executable: Path) -> None:
        """保存构造事实，并创建可供测试检查的事件列表。"""

        # 测试传入的是固定伪路径，组件不得偷偷使用其他 Docker 地址。
        self.docker_executable = docker_executable
        # events 记录备份恢复的重要先后顺序。
        self.events: list[str] = []
        # 测试通过模块变量取得最近一次创建的假客户端。
        FakeDockerPostgresClient.latest = self

    # latest 在构造后保存当前实例，避免依赖真实全局服务。
    latest: "FakeDockerPostgresClient"

    def ensure_postgres_ready(self) -> str:
        """模拟健康的 PostgreSQL 18.4。"""

        # 记录健康检查先于备份发生。
        self.events.append("ready")
        # 版本只进入低敏清单。
        return "18.4"

    def database_fingerprint(self, database_name: str) -> dict[str, dict[str, int | str]]:
        """源库和恢复库默认返回相同的有限指纹。"""

        # 记录实际检查了哪个数据库。
        self.events.append(f"fingerprint:{database_name}")
        # 深层复制避免被测代码意外共享可变嵌套字典。
        return {
            table_name: dict(table_fingerprint)
            for table_name, table_fingerprint in SOURCE_FINGERPRINT.items()
        }

    def dump_database(self, destination_path: Path) -> None:
        """写入一个非空的假归档文件。"""

        # 记录 dump 发生在两次源库指纹之间。
        self.events.append("dump")
        # 内容不是有效 PostgreSQL 归档，但 validate_archive 已由本假对象隔离。
        destination_path.write_bytes(b"fake-postgres-custom-archive")

    def validate_archive(self, backup_path: Path) -> int:
        """模拟 pg_restore 成功解析三个目录项。"""

        # 正式路径必须已经去掉 partial 后缀。
        assert backup_path.suffix == ".dump"
        # 记录归档验证发生在恢复前。
        self.events.append("validate_archive")
        # 返回有限目录项数量供报告断言。
        return 3

    def create_drill_database(self, database_name: str) -> None:
        """模拟创建已经通过名称保护的临时库。"""

        # 测试替身也调用真实保护器，确保断言目标格式正确。
        validate_drill_database_name(database_name)
        # 记录随机库名，后续应对同一个名称恢复和删除。
        self.events.append(f"create:{database_name}")

    def restore_archive(self, backup_path: Path, database_name: str) -> None:
        """模拟把归档恢复进临时库。"""

        # 恢复必须读取已经存在的正式备份。
        assert backup_path.is_file()
        # 恢复目标仍必须满足安全规则。
        validate_drill_database_name(database_name)
        # 保存恢复事件供顺序检查。
        self.events.append(f"restore:{database_name}")

    def drop_drill_database(self, database_name: str) -> None:
        """模拟只删除本次随机临时库。"""

        # 删除前保护器是最关键的不变量。
        validate_drill_database_name(database_name)
        # 保存删除事件，失败分支也必须出现。
        self.events.append(f"drop:{database_name}")


class MismatchedRestoreClient(FakeDockerPostgresClient):
    """恢复库数据不同但仍应完成临时库清理的测试替身。"""

    def database_fingerprint(self, database_name: str) -> dict[str, dict[str, int | str]]:
        """只让随机恢复库返回一个不同的 Checkpoint 行数。"""

        # 先复用父类事件记录和深层复制。
        fingerprint = super().database_fingerprint(database_name)
        # 真实源库两次读取保持一致，避免提前触发并发写入保护。
        if database_name.startswith(postgres_backup.DRILL_DATABASE_PREFIX):
            # 只改变恢复库的一项计数，模拟不完整恢复。
            fingerprint["checkpoints"]["row_count"] = 1
        # 返回本次独立字典。
        return fingerprint


def _patch_runtime_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    client_type: type[FakeDockerPostgresClient],
) -> None:
    """把备份、报告和 Docker 客户端全部隔离到 pytest 临时目录。"""

    # 备份目录位于 pytest 自动清理的临时路径。
    monkeypatch.setattr(postgres_backup, "BACKUP_DIRECTORY", tmp_path / "backups")
    # 固定报告同样不能写入开发者真实 data/runtime。
    monkeypatch.setattr(postgres_backup, "STEP19_REPORT_PATH", tmp_path / "step19.json")
    # Docker 定位返回一个无需真实存在的测试路径。
    monkeypatch.setattr(
        postgres_backup,
        "find_docker_executable",
        lambda: tmp_path / "docker.exe",
    )
    # 运维编排将构造测试指定的假客户端类型。
    monkeypatch.setattr(postgres_backup, "DockerPostgresClient", client_type)


def test_drill_database_name_guard_rejects_real_or_arbitrary_targets() -> None:
    """数据库删除保护必须只接受内部生成的随机临时名称。"""

    # Arrange/Act：内部名称生成器创建候选名称。
    generated_name = create_drill_database_name()
    # Assert：生成结果满足保护器，不抛出异常。
    validate_drill_database_name(generated_name)
    # Assert：真实业务库永远不能成为演练恢复或删除目标。
    with pytest.raises(PostgresBackupError, match="真实 serviceops"):
        validate_drill_database_name("serviceops")
    # Assert：只有前缀而没有十二位随机后缀同样被拒绝。
    with pytest.raises(PostgresBackupError, match="随机前缀规则"):
        validate_drill_database_name("serviceops_restore_drill_")
    # Assert：额外 SQL 或命令字符无法通过完整正则匹配。
    with pytest.raises(PostgresBackupError, match="随机前缀规则"):
        validate_drill_database_name("serviceops_restore_drill_123456789abc;DROP")


def test_successful_drill_writes_verified_manifest_and_cleans_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """成功路径必须留下归档/清单/报告，同时删除随机恢复库。"""

    # Arrange：用纯内存命令替身和隔离文件夹替换真实 Docker 环境。
    _patch_runtime_paths(monkeypatch, tmp_path, FakeDockerPostgresClient)
    # Act：执行与 PyCharm 示例完全相同的核心编排函数。
    artifact = postgres_backup.run_backup_restore_drill()
    # Assert：归档、对应清单和固定报告全部存在。
    assert artifact.backup_path.is_file()
    assert artifact.manifest_path.is_file()
    assert artifact.report_path.is_file()
    # Assert：两张假表都参与一致性比较。
    assert artifact.table_count == 2
    # 读取清单确认它只保存验证结论和有限指纹。
    manifest = json.loads(artifact.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "verified"
    assert manifest["restore_drill"]["fingerprint_match"] is True
    assert manifest["restore_drill"]["temporary_database_removed"] is True
    # 所有命令都不应把本地开发密码写入清单。
    assert "serviceops-local-dev-password" not in artifact.manifest_path.read_text(
        encoding="utf-8"
    )
    # 取得本次创建、恢复和删除事件使用的数据库名。
    events = FakeDockerPostgresClient.latest.events
    created_event = next(event for event in events if event.startswith("create:"))
    restored_event = next(event for event in events if event.startswith("restore:"))
    dropped_event = next(event for event in events if event.startswith("drop:"))
    # 三个动作必须始终针对同一个内部随机目标。
    assert created_event.removeprefix("create:") == restored_event.removeprefix("restore:")
    assert created_event.removeprefix("create:") == dropped_event.removeprefix("drop:")


def test_fingerprint_mismatch_fails_but_still_drops_temporary_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """恢复数据不一致时不能写 PASS 清单，并且仍要清理临时库。"""

    # Arrange：恢复库返回与源库不同的 Checkpoint 行数。
    _patch_runtime_paths(monkeypatch, tmp_path, MismatchedRestoreClient)
    # Act/Assert：编排必须明确失败，不能把差异吞掉。
    with pytest.raises(PostgresBackupError, match="逐表指纹不一致"):
        postgres_backup.run_backup_restore_drill()
    # Assert：即使比较失败，finally 仍执行了受保护的 dropdb 动作。
    events = MismatchedRestoreClient.latest.events
    assert any(event.startswith("drop:") for event in events)
    # Assert：验证未通过时绝不能生成带 status=verified 的清单。
    assert list((tmp_path / "backups").glob("*.manifest.json")) == []
    # Assert：固定 PASS 报告同样不能存在。
    assert not (tmp_path / "step19.json").exists()


def test_backup_files_are_excluded_from_git_and_image_context() -> None:
    """包含业务状态的备份目录必须同时离开 Git 与 Docker 镜像。"""

    # Arrange：使用稳定项目根读取两份忽略规则。
    gitignore = (postgres_backup.PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    dockerignore = (postgres_backup.PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
    # Assert：源码版本控制不得收集备份。
    assert "data/backups/" in gitignore
    # Assert：docker build 上下文也不得把备份发送给构建器。
    assert "data/backups/" in dockerignore

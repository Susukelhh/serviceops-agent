"""通过 Docker Compose 对本地 PostgreSQL 执行安全备份和隔离恢复演练。

这里的目标不是只证明 ``pg_dump`` 能生成一个文件，而是继续把文件恢复到随机命名的临时
数据库，并逐表比较行数和内容指纹。演练始终拒绝把目标数据库设置成真实 ``serviceops``，
结束时也只允许删除具有固定随机前缀的临时数据库。

备份文件可能包含业务状态和对话 Checkpoint，因此只保存到已被 Git 与 Docker 构建上下文
忽略的 ``data/backups``。结构化报告只保存表名、行数、哈希和工具版本，不保存行内容、
数据库密码、Token 或模型密钥。
"""

# hashlib 以流式方式计算备份文件 SHA-256，不需要把整个文件读入内存。
import hashlib

# json 用于写出机器可读的备份清单和第十九步运行报告。
import json

# os 提供原子替换和文件落盘同步，降低中途断电留下“看似完整”文件的概率。
import os

# re 同时约束临时数据库名和从 PostgreSQL 返回的表名。
import re

# shutil.which 优先使用当前 PowerShell 或 PyCharm PATH 中的 Docker CLI。
import shutil

# subprocess 以参数列表启动 Docker，不通过 shell 拼接数据库命令。
import subprocess

# Mapping 为报告和指纹函数提供只读字典类型。
from collections.abc import Mapping, Sequence

# dataclass 明确描述一次成功演练产生的有限输出。
from dataclasses import dataclass

# UTC 时间戳避免报告在不同时区下产生歧义。
from datetime import UTC, datetime

# Path 提供 Windows 与 Linux 都一致的路径操作。
from pathlib import Path

# Any 只用于 JSON 可序列化值边界；数据库结果会尽快转换成具体类型。
from typing import Any

# uuid4 为每次恢复演练生成不可预测且几乎不碰撞的临时数据库后缀。
from uuid import uuid4

# PROJECT_ROOT 是已经过源码仓库标记校验的稳定绝对路径。
from serviceops_agent.config.paths import PROJECT_ROOT, resolve_project_path

# Compose 中 PostgreSQL 服务名固定为 postgres。
POSTGRES_SERVICE = "postgres"

# 本地课程数据库用户固定为 serviceops；密码留在容器环境中，不进入命令参数。
POSTGRES_USER = "serviceops"

# 真实业务数据库名固定为 serviceops，恢复动作永远不能把它当成目标库。
SOURCE_DATABASE = "serviceops"

# 所有可删除的演练数据库都必须以该前缀开头。
DRILL_DATABASE_PREFIX = "serviceops_restore_drill_"

# 后缀只允许十二位小写十六进制字符，不能由命令行任意传入。
DRILL_DATABASE_PATTERN = re.compile(r"^serviceops_restore_drill_[0-9a-f]{12}$")

# PostgreSQL 普通未加引号标识符的安全子集，足以覆盖项目迁移创建的公共表。
SAFE_IDENTIFIER_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")

# 备份目录不受当前 Working directory 影响，并且由 .gitignore 明确排除。
BACKUP_DIRECTORY = resolve_project_path("data/backups")

# 最新一次演练的有限报告写入已有运行报告目录。
STEP19_REPORT_PATH = resolve_project_path("data/runtime/postgres_step19_report.json")


class PostgresBackupError(RuntimeError):
    """表示备份、恢复或完整性验证没有安全完成。"""


@dataclass(frozen=True)
class BackupArtifact:
    """一次成功备份恢复演练交付给调用方的文件和摘要。"""

    # backup_path 指向 PostgreSQL 自定义格式归档文件。
    backup_path: Path
    # manifest_path 指向与归档文件一一对应的 JSON 清单。
    manifest_path: Path
    # report_path 指向固定位置的最近一次第十九步报告。
    report_path: Path
    # sha256 是整个归档的十六进制文件指纹。
    sha256: str
    # size_bytes 是归档的实际字节数，可帮助发现空文件或异常截断。
    size_bytes: int
    # table_count 是已恢复并完成内容比较的公共表数量。
    table_count: int


def find_docker_executable() -> Path:
    """查找 Docker CLI，并兼容 Docker Desktop 安装后 PATH 尚未刷新的情况。"""

    # 当前 PATH 是最常见也最可移植的来源。
    path_result = shutil.which("docker")
    # 找到时先规范化路径，后续 subprocess 不再依赖工作目录。
    if path_result:
        return Path(path_result).resolve(strict=True)

    # Docker Desktop 的用户级安装位置来自当前用户 LOCALAPPDATA。
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    # 候选列表不会递归扫描磁盘，避免一次运行花费数十秒。
    candidates: list[Path] = []
    # 只有环境变量非空时才构造用户级候选，避免空值退化成相对路径。
    if local_app_data:
        candidates.append(
            Path(local_app_data) / "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe"
        )
    # 机器级默认安装位置仍作为第二候选。
    candidates.append(Path("C:/Program Files/Docker/Docker/resources/bin/docker.exe"))
    # 依次检查有限且明确的候选路径。
    for candidate in candidates:
        # is_file 同时排除不存在路径和同名目录。
        if candidate.is_file():
            return candidate.resolve(strict=True)

    # 错误只告诉用户如何处理，不输出完整 PATH 环境变量。
    raise PostgresBackupError(
        "找不到 Docker CLI；请先启动 Docker Desktop，并在重启 PyCharm 后确认 docker version 可运行"
    )


def create_drill_database_name() -> str:
    """生成只能用于本次恢复演练的随机数据库名。"""

    # uuid4().hex 不含连字符，截取十二位后仍具有足够的本地防碰撞能力。
    database_name = f"{DRILL_DATABASE_PREFIX}{uuid4().hex[:12]}"
    # 生成后再次走与删除前相同的校验，避免未来维护时改变命名规则却漏改保护器。
    validate_drill_database_name(database_name)
    # 返回已经验证的名称。
    return database_name


def validate_drill_database_name(database_name: str) -> None:
    """拒绝任何可能指向真实库或由外部任意构造的数据库名。"""

    # 真实库名称即使未来错误地满足其他条件，也必须被单独禁止。
    if database_name == SOURCE_DATABASE:
        raise PostgresBackupError("恢复演练禁止把真实 serviceops 数据库作为目标")
    # 完整匹配固定前缀和十二位随机后缀，防止 SQL 参数或其他数据库名混入。
    if DRILL_DATABASE_PATTERN.fullmatch(database_name) is None:
        raise PostgresBackupError("恢复演练数据库名不符合安全随机前缀规则")


def _quote_identifier(identifier: str) -> str:
    """只为受限的 PostgreSQL 表名生成双引号标识符。"""

    # 表名来自 pg_tables，但仍进行白名单校验，防止未来代码改为接受外部字符串。
    if SAFE_IDENTIFIER_PATTERN.fullmatch(identifier) is None:
        raise PostgresBackupError("数据库返回了不受支持的公共表名")
    # 安全子集不含双引号，因此可以直接包裹成 PostgreSQL 标识符。
    return f'"{identifier}"'


def _decode_output(output: bytes) -> str:
    """把 PostgreSQL/Docker 的 UTF-8 输出转换为有限文本。"""

    # errors=replace 避免第三方工具偶发非法字节掩盖真正的非零退出码。
    return output.decode("utf-8", errors="replace").strip()


def _safe_error_excerpt(stderr: bytes) -> str:
    """截取有限错误摘要，防止工具异常把大量日志打印到终端。"""

    # Docker 与 PostgreSQL 命令不含密码；这里仍只保留最后两千字符。
    decoded = _decode_output(stderr)
    # 没有标准错误时给出稳定占位文本。
    if not decoded:
        return "未提供错误详情"
    # 尾部通常包含 PostgreSQL 最具体的失败原因。
    return decoded[-2000:]


class DockerPostgresClient:
    """只通过 Compose 内网访问 postgres 服务的有限命令客户端。"""

    def __init__(self, *, docker_executable: Path, project_root: Path = PROJECT_ROOT) -> None:
        """保存已经定位的 Docker CLI 和 Compose 项目根目录。"""

        # Docker 可执行文件在创建客户端前已经验证存在。
        self._docker_executable = docker_executable
        # resolve 让所有 Compose 调用都使用同一个绝对项目根目录。
        self._project_root = project_root.resolve(strict=True)

    def _base_command(self) -> list[str]:
        """构造不包含密码的 Docker Compose 命令前缀。"""

        # --project-directory 明确定位 compose.yaml，不依赖 PyCharm Working directory。
        return [
            str(self._docker_executable),
            "compose",
            "--project-directory",
            str(self._project_root),
        ]

    def _run(
        self,
        arguments: Sequence[str],
        *,
        stdin_path: Path | None = None,
        stdout_path: Path | None = None,
    ) -> bytes:
        """运行一个无 shell 的 Docker 命令，并按需连接二进制输入或输出文件。"""

        # 参数逐项传给 subprocess，不使用 shell=True，也不拼接可执行字符串。
        command = [*self._base_command(), *arguments]
        # 输入文件只在 pg_restore/pg_restore --list 时打开。
        input_handle = stdin_path.open("rb") if stdin_path is not None else None
        # 输出文件只在 pg_dump 时打开；其他命令保留标准输出供解析。
        output_handle = stdout_path.open("wb") if stdout_path is not None else None
        try:
            # PIPE 只接收有限文本命令；大型 dump 直接流向文件，不进入 Python 内存。
            completed = subprocess.run(
                command,
                cwd=self._project_root,
                stdin=input_handle,
                stdout=output_handle if output_handle is not None else subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        finally:
            # 无论命令成功还是失败，都及时关闭 Windows 文件句柄。
            if input_handle is not None:
                input_handle.close()
            # 输出句柄关闭会把 Python 缓冲数据刷新到操作系统。
            if output_handle is not None:
                output_handle.close()

        # 非零退出码必须阻止后续恢复或报告 PASS。
        if completed.returncode != 0:
            raise PostgresBackupError(
                f"Docker/PostgreSQL 命令失败（退出码 {completed.returncode}）："
                f"{_safe_error_excerpt(completed.stderr)}"
            )
        # 输出被重定向到文件时 CompletedProcess.stdout 为 None，统一返回空字节。
        return completed.stdout or b""

    def ensure_postgres_ready(self) -> str:
        """确认 postgres 服务正在运行，并返回不会泄密的服务端版本。"""

        # compose ps 只查询目标服务 ID；空结果代表容器没有运行。
        container_id = _decode_output(
            self._run(["ps", "--status", "running", "--quiet", POSTGRES_SERVICE])
        )
        # 不自动启动或重建容器，让运维动作保持显式可控。
        if not container_id:
            raise PostgresBackupError("postgres 容器未运行；请先执行 docker compose up -d --wait")
        # pg_isready 验证数据库能接受连接，而不只是容器进程存在。
        self._run(
            [
                "exec",
                "-T",
                POSTGRES_SERVICE,
                "pg_isready",
                "--username",
                POSTGRES_USER,
                "--dbname",
                SOURCE_DATABASE,
            ]
        )
        # SHOW server_version 返回服务端真实版本，写入清单便于判断恢复工具兼容性。
        return self.psql_scalar(SOURCE_DATABASE, "SHOW server_version")

    def psql_scalar(self, database_name: str, sql: str) -> str:
        """执行只期望一个文本结果的 psql 查询。"""

        # 数据库名来自固定源库或内部随机演练库，不来自 HTTP 请求。
        output = self._run(
            [
                "exec",
                "-T",
                POSTGRES_SERVICE,
                "psql",
                "--username",
                POSTGRES_USER,
                "--dbname",
                database_name,
                "--set",
                "ON_ERROR_STOP=1",
                "--tuples-only",
                "--no-align",
                "--command",
                sql,
            ]
        )
        # 去除 psql 末尾换行，但不修改结果中间内容。
        return _decode_output(output)

    def list_public_tables(self, database_name: str) -> list[str]:
        """读取并校验数据库 public schema 中的所有普通表。"""

        # 表名由数据库系统目录返回，并按名称排序形成确定性比较顺序。
        output = self.psql_scalar(
            database_name,
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname = 'public' ORDER BY tablename",
        )
        # 空数据库得到空列表；非空结果逐行拆分。
        table_names = [] if not output else output.splitlines()
        # 每张表都必须符合项目允许的普通标识符子集。
        for table_name in table_names:
            _quote_identifier(table_name)
        # 返回已经在 SQL 中排序过的名称。
        return table_names

    def table_fingerprint(self, database_name: str, table_name: str) -> dict[str, int | str]:
        """计算单张表的行数和与行顺序无关的组合内容指纹。"""

        # 先验证再引用表名，SQL 中不会出现未检查的外部标识符。
        quoted_table = _quote_identifier(table_name)
        # 每行先转换成 JSONB 再做 MD5，排序后聚合，最后再做一次表级 MD5。
        # 这里的 MD5 只用于快速比较恢复前后是否一致，不用于密码或安全签名。
        sql = (
            "SELECT count(*)::text || E'\\t' || "
            "COALESCE(md5(string_agg(md5(to_jsonb(table_row)::text), '' "
            "ORDER BY md5(to_jsonb(table_row)::text))), md5('')) "
            f"FROM public.{quoted_table} AS table_row"
        )
        # psql 返回“行数<TAB>内容指纹”。
        output = self.psql_scalar(database_name, sql)
        # 只切分一次，避免未来字段格式变化产生静默误判。
        parts = output.split("\t", maxsplit=1)
        # 任何非二段结果都代表查询契约异常。
        if len(parts) != 2:
            raise PostgresBackupError(f"无法解析表 {table_name} 的完整性指纹")
        # int 转换同时验证 PostgreSQL 行数输出是合法数字。
        row_count = int(parts[0])
        # 表级 MD5 应固定为三十二位小写十六进制字符。
        content_hash = parts[1]
        if re.fullmatch(r"[0-9a-f]{32}", content_hash) is None:
            raise PostgresBackupError(f"表 {table_name} 返回了非法内容指纹")
        # 报告只记录计数和组合哈希，不记录任何业务字段。
        return {"row_count": row_count, "content_hash": content_hash}

    def database_fingerprint(self, database_name: str) -> dict[str, dict[str, int | str]]:
        """计算数据库全部公共表的有限一致性摘要。"""

        # 先固定表集合；恢复后必须具有完全相同的集合。
        table_names = self.list_public_tables(database_name)
        # 项目业务库至少应包含 Alembic 版本表，否则可能连错空库。
        if database_name == SOURCE_DATABASE and "alembic_version" not in table_names:
            raise PostgresBackupError("源数据库缺少 alembic_version，拒绝备份可能连错的数据库")
        # 按排序后的表名逐表计算摘要，JSON 输出也会保持稳定顺序。
        return {
            table_name: self.table_fingerprint(database_name, table_name)
            for table_name in table_names
        }

    def dump_database(self, destination_path: Path) -> None:
        """把源库流式导出为 PostgreSQL custom 格式归档。"""

        # --no-owner/--no-privileges 避免把本地用户授权硬编码到未来恢复目标。
        self._run(
            [
                "exec",
                "-T",
                POSTGRES_SERVICE,
                "pg_dump",
                "--username",
                POSTGRES_USER,
                "--dbname",
                SOURCE_DATABASE,
                "--format",
                "custom",
                "--no-owner",
                "--no-privileges",
            ],
            stdout_path=destination_path,
        )

    def validate_archive(self, backup_path: Path) -> int:
        """让 pg_restore 解析归档目录，并返回有效目录项数量。"""

        # --list 不执行任何 SQL，只验证 custom 归档可被 PostgreSQL 工具完整读取。
        output = self._run(
            ["exec", "-T", POSTGRES_SERVICE, "pg_restore", "--list"],
            stdin_path=backup_path,
        )
        # 注释行以分号开头；其余非空行都是可恢复对象。
        entries = [
            line
            for line in _decode_output(output).splitlines()
            if line.strip() and not line.lstrip().startswith(";")
        ]
        # 一个没有任何对象的归档不应被当作可用备份。
        if not entries:
            raise PostgresBackupError("pg_restore 能读取文件，但归档中没有任何数据库对象")
        # 返回数量供清单和最终报告记录。
        return len(entries)

    def create_drill_database(self, database_name: str) -> None:
        """从 template0 创建与真实业务库隔离的空演练数据库。"""

        # 创建前执行与删除前相同的强校验。
        validate_drill_database_name(database_name)
        # template0 避免复制 source 数据库或用户自定义模板中的业务对象。
        self._run(
            [
                "exec",
                "-T",
                POSTGRES_SERVICE,
                "createdb",
                "--username",
                POSTGRES_USER,
                "--owner",
                POSTGRES_USER,
                "--template",
                "template0",
                database_name,
            ]
        )

    def restore_archive(self, backup_path: Path, database_name: str) -> None:
        """在单个事务中把归档恢复进已经验证的临时数据库。"""

        # 任何真实库或格式不符的库名都在执行 pg_restore 前被拒绝。
        validate_drill_database_name(database_name)
        # --single-transaction 保证失败时整次恢复回滚，不留下半套表结构。
        self._run(
            [
                "exec",
                "-T",
                POSTGRES_SERVICE,
                "pg_restore",
                "--username",
                POSTGRES_USER,
                "--dbname",
                database_name,
                "--no-owner",
                "--no-privileges",
                "--single-transaction",
                "--exit-on-error",
            ],
            stdin_path=backup_path,
        )

    def drop_drill_database(self, database_name: str) -> None:
        """只删除通过固定随机名称规则验证的演练数据库。"""

        # 这是所有删除动作前不可绕过的最后一道代码保护。
        validate_drill_database_name(database_name)
        # --force 会断开演练查询遗留连接；--if-exists 让异常清理可以安全重试。
        self._run(
            [
                "exec",
                "-T",
                POSTGRES_SERVICE,
                "dropdb",
                "--username",
                POSTGRES_USER,
                "--if-exists",
                "--force",
                database_name,
            ]
        )


def _sha256_file(file_path: Path) -> str:
    """以固定大小数据块计算一个文件的 SHA-256。"""

    # sha256 适合检测备份文件被修改或传输损坏。
    digest = hashlib.sha256()
    # 二进制读取避免 Windows 换行转换改变归档内容。
    with file_path.open("rb") as input_file:
        # iter 的空字节哨兵让循环在文件结尾停止。
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            # 每次最多读取 1 MiB，备份增长后也不会占用等量内存。
            digest.update(chunk)
    # 十六进制结果适合写入 JSON 和人工核对。
    return digest.hexdigest()


def _atomic_write_json(destination: Path, payload: Mapping[str, Any]) -> None:
    """先写临时文件并落盘，再原子替换目标 JSON。"""

    # 报告父目录可能在首次运行时不存在。
    destination.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件与目标位于同一目录，os.replace 才能保持同文件系统原子替换。
    temporary_path = destination.with_suffix(f"{destination.suffix}.partial")
    try:
        # newline 固定为换行符，encoding 明确为 UTF-8。
        with temporary_path.open("w", encoding="utf-8", newline="\n") as output_file:
            # ensure_ascii=False 让中文字段可直接阅读；indent=2 方便面试演示。
            json.dump(payload, output_file, ensure_ascii=False, indent=2)
            # 把 Python 用户态缓冲刷新到操作系统。
            output_file.flush()
            # fsync 尽量让内容真正进入磁盘，再对外宣布文件可用。
            os.fsync(output_file.fileno())
        # Windows 和 Linux 都会用完整临时文件替换旧报告。
        os.replace(temporary_path, destination)
    finally:
        # 进程异常时删除只由本次函数创建的 partial 文件。
        temporary_path.unlink(missing_ok=True)


def _backup_file_paths(now: datetime) -> tuple[Path, Path]:
    """为一次备份生成不会覆盖旧文件的归档和清单路径。"""

    # UTC 时间方便跨机器排序；随机后缀避免同一秒运行两次发生覆盖。
    stem = f"serviceops_{now.strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:8]}"
    # PostgreSQL custom 归档使用 .dump，便于与纯 SQL 文件区分。
    backup_path = BACKUP_DIRECTORY / f"{stem}.dump"
    # manifest 与归档共享 stem，人工复制时容易保持成对。
    manifest_path = BACKUP_DIRECTORY / f"{stem}.manifest.json"
    # 两条路径都由内部生成，不接受外部覆盖目标。
    return backup_path, manifest_path


def run_backup_restore_drill() -> BackupArtifact:
    """创建备份、恢复到临时库、比较数据并清理临时库。"""

    # 当前时间同时用于文件名和报告，整次演练只产生一个时间基准。
    started_at = datetime.now(UTC)
    # 定位 Docker CLI；找不到时在创建任何文件前失败。
    docker_executable = find_docker_executable()
    # 客户端只面向当前仓库的 Compose 项目。
    client = DockerPostgresClient(docker_executable=docker_executable)
    # 确认数据库健康并读取实际服务端版本。
    postgres_version = client.ensure_postgres_ready()
    # 创建备份目录；该目录已被版本控制和镜像上下文忽略。
    BACKUP_DIRECTORY.mkdir(parents=True, exist_ok=True)
    # 生成本次唯一文件名，旧备份不会被覆盖。
    backup_path, manifest_path = _backup_file_paths(started_at)
    # partial 归档在 pg_dump 完成前不会被当作正式备份。
    partial_backup_path = backup_path.with_suffix(".dump.partial")
    # 先计算源库指纹，用于发现备份过程中是否出现并发写入。
    source_before = client.database_fingerprint(SOURCE_DATABASE)
    try:
        # 大型归档直接从容器流到 partial 文件，不进入 Python 内存。
        client.dump_database(partial_backup_path)
        # 文件必须非空；空文件通常表示重定向或工具调用异常。
        if partial_backup_path.stat().st_size <= 0:
            raise PostgresBackupError("pg_dump 生成了空归档")
        # 归档内容落盘后再原子改成正式 .dump 文件名。
        os.replace(partial_backup_path, backup_path)
    finally:
        # 失败时只删除本次尚未完成的 partial 文件，不碰历史备份。
        partial_backup_path.unlink(missing_ok=True)

    # 再次读取源库；若数据变化，就无法把任一时刻的外部指纹可靠归属于本次归档。
    source_after = client.database_fingerprint(SOURCE_DATABASE)
    # 本地演练要求备份窗口内没有业务写入；发现变化时明确失败并保留归档供排查。
    if source_before != source_after:
        raise PostgresBackupError("备份期间源数据库发生变化；请暂停写入后重新运行恢复演练")
    # 文件 SHA-256 可用于以后复制、上传和恢复前检查归档未被破坏。
    backup_sha256 = _sha256_file(backup_path)
    # 使用 pg_restore 自己解析目录，比只检查文件扩展名更可信。
    archive_entry_count = client.validate_archive(backup_path)
    # 临时库名称完全由代码生成，用户不能通过参数指定删除目标。
    drill_database = create_drill_database_name()
    # 先记录是否成功创建，以决定 finally 是否执行删除。
    drill_database_created = False
    # cleanup_succeeded 只有 dropdb 成功后才变成 True。
    cleanup_succeeded = False
    try:
        # 创建空的隔离数据库，不停止当前 serviceops API。
        client.create_drill_database(drill_database)
        # 创建成功后，即使后续恢复失败也必须进入 finally 清理。
        drill_database_created = True
        # 在单事务中恢复归档；中途失败不会留下半套恢复结果。
        client.restore_archive(backup_path, drill_database)
        # 对恢复库使用与源库完全相同的逐表指纹算法。
        restored_fingerprint = client.database_fingerprint(drill_database)
        # 表集合、行数或组合内容哈希任一不同都不能通过。
        if source_after != restored_fingerprint:
            raise PostgresBackupError("恢复库与源库逐表指纹不一致")
    finally:
        # 只有本次明确创建成功的随机演练库才执行删除。
        if drill_database_created:
            client.drop_drill_database(drill_database)
            # dropdb 非零会在上一行抛异常，因此走到这里即可确认清理完成。
            cleanup_succeeded = True

    # 完成时间在恢复、比较和清理全部成功后记录。
    completed_at = datetime.now(UTC)
    # 结构化清单不保存真实行数据或数据库口令。
    manifest: dict[str, Any] = {
        "schema_version": "1.0.0",
        "status": "verified",
        "created_at": started_at.isoformat(),
        "verified_at": completed_at.isoformat(),
        "source_database": SOURCE_DATABASE,
        "postgres_version": postgres_version,
        "backup_file": backup_path.name,
        "backup_format": "postgresql_custom",
        "size_bytes": backup_path.stat().st_size,
        "sha256": backup_sha256,
        "archive_entry_count": archive_entry_count,
        "source_fingerprint": source_after,
        "restore_drill": {
            "target_kind": "temporary_isolated_database",
            "fingerprint_match": True,
            "temporary_database_removed": cleanup_succeeded,
        },
    }
    # 清单与归档成对保存，恢复前可以先核验 SHA-256。
    _atomic_write_json(manifest_path, manifest)
    # 最近一次报告额外保存耗时和文件绝对路径，方便 PyCharm 点击定位。
    report: dict[str, Any] = {
        "step": 19,
        "status": "pass",
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_seconds": round((completed_at - started_at).total_seconds(), 3),
        "backup_path": str(backup_path),
        "manifest_path": str(manifest_path),
        "sha256": backup_sha256,
        "size_bytes": backup_path.stat().st_size,
        "archive_entry_count": archive_entry_count,
        "verified_table_count": len(source_after),
        "fingerprint_match": True,
        "temporary_database_removed": cleanup_succeeded,
    }
    # 固定报告可以被 CI 或下一步发布门禁读取。
    _atomic_write_json(STEP19_REPORT_PATH, report)
    # 返回强类型摘要，让 CLI 决定如何展示，不把 print 混进运维组件。
    return BackupArtifact(
        backup_path=backup_path,
        manifest_path=manifest_path,
        report_path=STEP19_REPORT_PATH,
        sha256=backup_sha256,
        size_bytes=backup_path.stat().st_size,
        table_count=len(source_after),
    )

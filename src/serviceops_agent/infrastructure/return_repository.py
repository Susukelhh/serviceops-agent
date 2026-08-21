"""线程安全的退货申请仓库协议与进程内幂等实现。"""

# json 把强类型 Outbox 载荷转换为字段顺序稳定的数据库文本。
import json

# sqlite3 提供事务、WAL 模式和数据库唯一约束。
import sqlite3

# datetime/UTC 生成明确 UTC 时间；timedelta 计算失败后的指数退避时间。
from datetime import UTC, datetime, timedelta

# Path 让 SQLite 文件位置使用明确绝对路径。
from pathlib import Path

# Lock 保护并发请求下的“检查幂等键—创建记录”原子区间。
from threading import Lock
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

# 订单状态与仓库协议用于执行归属和已签收资格检查。
from serviceops_agent.domain.orders import OrderStatus

# Outbox 模型保证业务事务内生成的下游事件经过严格字段与脱敏约束。
from serviceops_agent.domain.outbox import (
    OutboxEventRecord,
    OutboxEventType,
    OutboxStatus,
    ReturnCommittedEventPayload,
    ReturnOutboxMetadata,
    build_return_outbox_event_id,
)

# 退货记录领域模型和状态保证仓库输出稳定。
from serviceops_agent.domain.returns import ReturnRequestRecord, ReturnRequestStatus
from serviceops_agent.infrastructure.order_repository import (
    OrderRepository,
    default_order_repository,
)


class ReturnRepositoryError(Exception):
    """退货仓库可预期业务错误的共同基类。"""


class ReturnOrderUnavailableError(ReturnRepositoryError):
    """订单不存在或不属于当前用户，两种情况故意不区分。"""


class ReturnOrderNotEligibleError(ReturnRepositoryError):
    """本人订单存在，但当前状态不允许创建退货申请。"""


class ReturnIdempotencyConflictError(ReturnRepositoryError):
    """同一个幂等键被用于不同用户、订单或原因。"""


class ReturnOutboxConflictError(ReturnRepositoryError):
    """同一个稳定 Outbox 事件编号已经绑定到不同可信审批事实。"""


class ReturnRequestRepository(Protocol):
    """写工具依赖的最小幂等退货申请仓库接口。"""

    def create_or_get(
        self,
        *,
        user_id: str,
        order_id: str,
        reason: str,
        idempotency_key: str,
        outbox_metadata: ReturnOutboxMetadata | None = None,
    ) -> tuple[ReturnRequestRecord, bool]:
        """创建申请或返回同键同负载记录；布尔值表示是否为幂等重放。"""

    def count(self) -> int:
        """返回当前仓库唯一申请数，供健康验证、测试和本地演示使用。"""


class InMemoryReturnRequestRepository:
    """供学习、测试和单进程演示使用的线程安全幂等写仓库。"""

    def __init__(self, order_repository: OrderRepository) -> None:
        """绑定订单读取边界，并初始化进程内记录和互斥锁。"""

        # 订单仓库负责身份归属查询，退货仓库不直接读取原始订单文件。
        self._order_repository = order_repository
        # 以幂等键作为主索引，支持重复恢复和 HTTP 重试返回同一记录。
        self._records_by_idempotency_key: dict[str, ReturnRequestRecord] = {}
        # Outbox 事件与业务记录受同一个锁保护，模拟数据库单事务原子提交边界。
        self._outbox_events_by_id: dict[str, OutboxEventRecord] = {}
        # 普通 Lock 保护同步仓库方法的短原子区间。
        self._lock = Lock()

    def create_or_get(
        self,
        *,
        user_id: str,
        order_id: str,
        reason: str,
        idempotency_key: str,
        outbox_metadata: ReturnOutboxMetadata | None = None,
    ) -> tuple[ReturnRequestRecord, bool]:
        """在锁内检查幂等负载并创建最多一条申请记录。"""

        # 先通过订单仓库执行身份归属检查；不存在和越权得到同一个 None。
        order = self._order_repository.get_for_user(order_id=order_id, user_id=user_id)
        # 无法确认本人订单时不允许任何写入。
        if order is None:
            # 上层工具会转换成统一安全文案。
            raise ReturnOrderUnavailableError
        # 当前演示只允许已签收订单发起退货，避免对运输中订单直接创建业务申请。
        if order.status != OrderStatus.DELIVERED:
            # 上层不会泄漏其他用户数据，因为到达此分支前归属已经通过。
            raise ReturnOrderNotEligibleError

        # 锁保证两个并发相同幂等键不会同时越过检查并各自创建记录。
        with self._lock:
            # 查询该键是否已经处理过。
            existing = self._records_by_idempotency_key.get(idempotency_key)
            # 存在记录时必须比较完整业务负载。
            if existing is not None:
                # 相同键、相同用户、订单和原因属于安全幂等重放。
                if (
                    existing.user_id == user_id
                    and existing.order_id == order_id
                    and existing.reason == reason
                ):
                    # API 幂等恢复时补齐或核对同一线程的稳定 Outbox 事件。
                    self._ensure_memory_outbox_event(
                        record=existing,
                        metadata=outbox_metadata,
                    )
                    # True 表示没有再次写入，而是返回原记录。
                    return existing, True
                # 相同键被复用于不同负载，必须拒绝而不是覆盖原记录。
                raise ReturnIdempotencyConflictError

            # uuid5 对同一幂等键产生稳定值，便于演示重复恢复返回同一申请号。
            stable_uuid = uuid5(NAMESPACE_URL, f"serviceops-return:{idempotency_key}")
            # 使用前 12 位大写十六进制构造可读业务编号。
            return_request_id = f"RR-{stable_uuid.hex[:12].upper()}"
            # 构造经过 Pydantic 校验的业务记录。
            record = ReturnRequestRecord(
                # 稳定申请编号。
                return_request_id=return_request_id,
                # 本人已签收订单号。
                order_id=order_id,
                # 可信系统身份。
                user_id=user_id,
                # 用户明确原因。
                reason=reason,
                # 新申请进入 submitted 状态。
                status=ReturnRequestStatus.SUBMITTED,
                # 保存幂等键用于后续重复检测。
                idempotency_key=idempotency_key,
                # UTC 时间包含明确时区。
                created_at=datetime.now(UTC),
            )
            # 先完整构造 Outbox 记录；若校验失败，业务字典也不会被修改。
            outbox_event = self._build_outbox_event(
                record=record,
                metadata=outbox_metadata,
            )
            # 同一线程稳定事件 ID 若已绑定其他业务事实，内存实现也必须与 SQLite 一样拒绝覆盖。
            existing_outbox_event = (
                self._outbox_events_by_id.get(outbox_event.event_id)
                if outbox_event is not None
                else None
            )
            if (
                outbox_event is not None
                and existing_outbox_event is not None
                and (
                    existing_outbox_event.event_type != outbox_event.event_type
                    or existing_outbox_event.aggregate_type
                    != outbox_event.aggregate_type
                    or existing_outbox_event.aggregate_id
                    != outbox_event.aggregate_id
                    or existing_outbox_event.payload != outbox_event.payload
                )
            ):
                raise ReturnOutboxConflictError
            # 业务记录与 Outbox 事件在同一个锁区间内连续写入，外部读取不能看到半状态。
            self._records_by_idempotency_key[idempotency_key] = record
            # 直接图教学调用可以不提供可信 API 元数据；生产 API 会始终生成本事件。
            if outbox_event is not None and existing_outbox_event is None:
                self._outbox_events_by_id[outbox_event.event_id] = outbox_event
            # False 表示本次确实创建了新记录。
            return record, False

    @staticmethod
    def _build_outbox_event(
        *,
        record: ReturnRequestRecord,
        metadata: ReturnOutboxMetadata | None,
    ) -> OutboxEventRecord | None:
        """根据已生成业务记录构造待投递事件；无 API 元数据时返回 None。"""

        # 直接调用图的学习示例没有 JWT/HTTP 上下文，因此允许显式跳过 Outbox。
        if metadata is None:
            return None
        # 业务记录和审批草案必须指向同一订单，防止错误装配产生交叉事件。
        if metadata.order_id != record.order_id:
            raise ReturnOutboxConflictError
        # 事件创建时间与业务记录一致，清楚表达两者属于同一提交事实。
        created_at = record.created_at
        # 组合仓库生成的 RR 编号和可信最小审批元数据。
        payload = ReturnCommittedEventPayload(
            **metadata.model_dump(),
            return_request_id=record.return_request_id,
        )
        # 新事件总是从 pending/零失败次数开始。
        return OutboxEventRecord(
            event_id=build_return_outbox_event_id(metadata.thread_id),
            event_type=OutboxEventType.RETURN_REQUEST_COMMITTED,
            aggregate_type="return_request",
            aggregate_id=record.return_request_id,
            payload=payload,
            status=OutboxStatus.PENDING,
            attempts=0,
            next_attempt_at=created_at,
            created_at=created_at,
        )

    def _ensure_memory_outbox_event(
        self,
        *,
        record: ReturnRequestRecord,
        metadata: ReturnOutboxMetadata | None,
    ) -> None:
        """在幂等业务重放时新增缺失事件，或确认既有事件语义完全相同。"""

        # 没有生产 API 上下文时保持原有教学调用行为。
        candidate = self._build_outbox_event(record=record, metadata=metadata)
        if candidate is None:
            return
        # 使用线程稳定 ID 查询可能已经处理过的事件。
        existing = self._outbox_events_by_id.get(candidate.event_id)
        if existing is None:
            # 老记录或跨线程重放可以拥有各自的完成审计事件。
            self._outbox_events_by_id[candidate.event_id] = candidate
            return
        # 状态、重试次数和时间会变化；不可变业务语义必须完全一致。
        if (
            existing.event_type != candidate.event_type
            or existing.aggregate_type != candidate.aggregate_type
            or existing.aggregate_id != candidate.aggregate_id
            or existing.payload != candidate.payload
        ):
            raise ReturnOutboxConflictError

    def count(self) -> int:
        """返回当前进程内唯一申请数量，供测试验证拒绝审批零写入。"""

        # 读取也放在锁内，避免并发写入时观察不一致大小。
        with self._lock:
            # 字典每个幂等键最多对应一条记录。
            return len(self._records_by_idempotency_key)

    def list_pending(
        self,
        *,
        limit: int = 100,
        thread_id: str | None = None,
        as_of: datetime | None = None,
    ) -> list[OutboxEventRecord]:
        """返回到达处理时间的内存 pending 事件快照。"""

        # limit 必须在合理范围内，避免一次运维调用无边界占用内存。
        if not 1 <= limit <= 1_000:
            raise ValueError("Outbox 扫描 limit 必须位于 1 到 1000")
        # 默认使用当前 UTC 时间；测试可注入未来时间验证退避。
        scan_time = as_of or datetime.now(UTC)
        # 在锁内得到一致快照，再按创建时间和事件 ID 确定性排序。
        with self._lock:
            pending_events = [
                event
                for event in self._outbox_events_by_id.values()
                if event.status == OutboxStatus.PENDING
                and event.next_attempt_at <= scan_time
                and (thread_id is None or event.payload.thread_id == thread_id)
            ]
            return sorted(
                pending_events,
                key=lambda event: (event.created_at, event.event_id),
            )[:limit]

    def mark_processed(self, event_id: str) -> OutboxEventRecord:
        """幂等标记内存事件已处理。"""

        # 状态更新和读取在同一锁内完成。
        with self._lock:
            existing = self._outbox_events_by_id.get(event_id)
            if existing is None:
                raise KeyError("未知 Outbox 事件")
            # 重复确认 processed 不改变原完成时间。
            if existing.status == OutboxStatus.PROCESSED:
                return existing
            # 协调器只允许推进 pending；死信必须先经过显式人工处置流程。
            if existing.status != OutboxStatus.PENDING:
                raise ValueError("只有 pending Outbox 事件可以标记完成")
            # model_copy 保留不可变业务载荷，只更新投递状态字段。
            processed = existing.model_copy(
                update={
                    "status": OutboxStatus.PROCESSED,
                    "processed_at": datetime.now(UTC),
                    "last_error_code": None,
                }
            )
            self._outbox_events_by_id[event_id] = processed
            return processed

    def record_failure(
        self,
        event_id: str,
        *,
        error_code: str,
        max_attempts: int = 3,
    ) -> OutboxEventRecord:
        """增加失败次数，并计算内存事件的指数退避或死信状态。"""

        # 只接受有限长度的内部错误分类，不保存异常消息。
        if not error_code or len(error_code) > 100:
            raise ValueError("Outbox error_code 长度不合法")
        if not 1 <= max_attempts <= 20:
            raise ValueError("Outbox max_attempts 必须位于 1 到 20")
        with self._lock:
            existing = self._outbox_events_by_id.get(event_id)
            if existing is None:
                raise KeyError("未知 Outbox 事件")
            # 已完成事件不允许被失败回写覆盖。
            if existing.status == OutboxStatus.PROCESSED:
                return existing
            # 死信重复记录保持原状态，避免尝试次数无边界增长。
            if existing.status == OutboxStatus.DEAD_LETTER:
                return existing
            attempts = existing.attempts + 1
            status = (
                OutboxStatus.DEAD_LETTER
                if attempts >= max_attempts
                else OutboxStatus.PENDING
            )
            # 退避秒数为 2、4、8……，本地实现上限由 max_attempts=20 间接约束。
            next_attempt_at = datetime.now(UTC) + timedelta(seconds=2**attempts)
            failed = existing.model_copy(
                update={
                    "status": status,
                    "attempts": attempts,
                    "next_attempt_at": next_attempt_at,
                    "last_error_code": error_code,
                }
            )
            self._outbox_events_by_id[event_id] = failed
            return failed

    def get_outbox_event(self, event_id: str) -> OutboxEventRecord | None:
        """查询一条内存 Outbox 事件快照。"""

        with self._lock:
            return self._outbox_events_by_id.get(event_id)

    def count_outbox(self, status: OutboxStatus | None = None) -> int:
        """统计内存 Outbox 全部事件或指定状态事件。"""

        with self._lock:
            if status is None:
                return len(self._outbox_events_by_id)
            return sum(
                event.status == status
                for event in self._outbox_events_by_id.values()
            )


class SQLiteReturnRequestRepository:
    """使用 SQLite 事务和唯一键实现跨进程重启幂等的本地业务仓库。"""

    def __init__(
        self,
        *,
        database_path: Path,
        order_repository: OrderRepository,
    ) -> None:
        """保存依赖、创建父目录并幂等初始化数据库表。"""

        # resolve 让后续连接不受 PyCharm Working directory 或当前目录影响。
        self._database_path = database_path.resolve()
        # 订单仓库仍负责本人订单读取和当前状态判断。
        self._order_repository = order_repository
        # SQLite 无法替调用方创建父目录，因此启动阶段先确保目录存在。
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        # 建表属于幂等启动迁移，多次实例化不会清空或覆盖既有记录。
        self._setup_schema()

    def _connect(self) -> sqlite3.Connection:
        """创建一次短生命周期连接并启用可靠的 SQLite 选项。"""

        # 每个仓库操作使用独立连接，避免跨线程复用同一 sqlite3.Connection。
        connection = sqlite3.connect(
            # Windows 路径转换为 sqlite3 接受的字符串。
            str(self._database_path),
            # 并发写暂时占锁时最多等待五秒，而不是立即报 database is locked。
            timeout=5.0,
            # 默认事务模式保留；写方法会显式使用 BEGIN IMMEDIATE。
            isolation_level=None,
        )
        # Row 允许用列名而不是脆弱的下标读取查询结果。
        connection.row_factory = sqlite3.Row
        # foreign_keys 当前表未使用外键，但在同一连接提前开启，方便后续扩展审计表。
        connection.execute("PRAGMA foreign_keys = ON")
        # busy_timeout 也覆盖 SQLite 内部锁等待路径，单位为毫秒。
        connection.execute("PRAGMA busy_timeout = 5000")
        # 返回由调用方法负责关闭的连接。
        return connection

    def _setup_schema(self) -> None:
        """创建退货申请表、事务 Outbox 表、索引和 WAL 日志模式。"""

        # 使用短连接完成启动迁移，退出 with 后仍显式关闭连接。
        connection = self._connect()
        try:
            # WAL 允许读取与单个写入更好地并发，适合本地服务开发。
            connection.execute("PRAGMA journal_mode = WAL")
            # 表结构把幂等键作为主键，数据库层保证跨线程/实例最多一条。
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS return_requests (
                    idempotency_key TEXT PRIMARY KEY,
                    return_request_id TEXT NOT NULL UNIQUE,
                    user_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status = 'submitted'),
                    created_at TEXT NOT NULL
                )
                """
            )
            # Outbox 与 return_requests 位于同一数据库，才能共享同一 SQLite 事务。
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS return_outbox_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL
                        CHECK (event_type = 'return_request_committed'),
                    aggregate_type TEXT NOT NULL
                        CHECK (aggregate_type = 'return_request'),
                    aggregate_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('pending', 'processed', 'dead_letter')),
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                    next_attempt_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    processed_at TEXT,
                    last_error_code TEXT
                )
                """
            )
            # 扫描器按状态、到期时间和创建顺序读取，索引避免事件增长后全表扫描。
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_return_outbox_pending
                ON return_outbox_events (status, next_attempt_at, created_at)
                """
            )
        finally:
            # 启动迁移无论成功或失败都不能泄漏文件句柄。
            connection.close()

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> ReturnRequestRecord:
        """把数据库行重新交给 Pydantic 领域模型校验。"""

        # 不直接信任磁盘数据；格式、状态和带时区时间仍需经过领域 Schema。
        return ReturnRequestRecord.model_validate(
            {
                # 业务申请编号。
                "return_request_id": row["return_request_id"],
                # 目标订单号。
                "order_id": row["order_id"],
                # 可信用户归属。
                "user_id": row["user_id"],
                # 已审批原因。
                "reason": row["reason"],
                # 有限业务状态。
                "status": row["status"],
                # 原始幂等键。
                "idempotency_key": row["idempotency_key"],
                # ISO 8601 字符串由 Pydantic 解析为带时区 datetime。
                "created_at": row["created_at"],
            }
        )

    @staticmethod
    def _outbox_from_row(row: sqlite3.Row) -> OutboxEventRecord:
        """把 SQLite Outbox 行和 JSON 载荷重新交给领域模型校验。"""

        # payload_json 由本服务生成，但磁盘内容仍不能跳过 JSON 与 Pydantic 校验。
        raw_payload = json.loads(str(row["payload_json"]))
        # 强类型模型检查 UUID、状态、摘要、订单号和时间格式。
        return OutboxEventRecord.model_validate(
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "aggregate_type": row["aggregate_type"],
                "aggregate_id": row["aggregate_id"],
                "payload": raw_payload,
                "status": row["status"],
                "attempts": row["attempts"],
                "next_attempt_at": row["next_attempt_at"],
                "created_at": row["created_at"],
                "processed_at": row["processed_at"],
                "last_error_code": row["last_error_code"],
            }
        )

    @staticmethod
    def _canonical_payload_json(payload: ReturnCommittedEventPayload) -> str:
        """生成跨进程字段顺序稳定、无多余空白的 Outbox JSON。"""

        # ensure_ascii=False 保留可读 UTF-8；当前载荷本身只含受控标识和摘要。
        return json.dumps(
            payload.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _ensure_sqlite_outbox_event(
        self,
        *,
        connection: sqlite3.Connection,
        record: ReturnRequestRecord,
        metadata: ReturnOutboxMetadata | None,
    ) -> None:
        """在调用方业务事务中插入或核对一条稳定 Outbox 事件。"""

        # 共用内存实现的强类型构造规则，避免两种后端产生不同事件语义。
        candidate = InMemoryReturnRequestRepository._build_outbox_event(
            record=record,
            metadata=metadata,
        )
        if candidate is None:
            return
        # 先按稳定事件 ID 查询，区分安全重放与同 ID 不同语义冲突。
        existing_row = connection.execute(
            """
            SELECT event_id, event_type, aggregate_type, aggregate_id,
                   payload_json, status, attempts, next_attempt_at,
                   created_at, processed_at, last_error_code
            FROM return_outbox_events
            WHERE event_id = ?
            """,
            (candidate.event_id,),
        ).fetchone()
        if existing_row is not None:
            existing = self._outbox_from_row(existing_row)
            if (
                existing.event_type == candidate.event_type
                and existing.aggregate_type == candidate.aggregate_type
                and existing.aggregate_id == candidate.aggregate_id
                and existing.payload == candidate.payload
            ):
                return
            raise ReturnOutboxConflictError
        # 该 INSERT 与 return_requests INSERT 共享同一连接和 BEGIN IMMEDIATE 事务。
        connection.execute(
            """
            INSERT INTO return_outbox_events (
                event_id, event_type, aggregate_type, aggregate_id,
                payload_json, status, attempts, next_attempt_at,
                created_at, processed_at, last_error_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.event_id,
                candidate.event_type.value,
                candidate.aggregate_type,
                candidate.aggregate_id,
                self._canonical_payload_json(candidate.payload),
                candidate.status.value,
                candidate.attempts,
                candidate.next_attempt_at.isoformat(),
                candidate.created_at.isoformat(),
                None,
                None,
            ),
        )

    def create_or_get(
        self,
        *,
        user_id: str,
        order_id: str,
        reason: str,
        idempotency_key: str,
        outbox_metadata: ReturnOutboxMetadata | None = None,
    ) -> tuple[ReturnRequestRecord, bool]:
        """在同一写事务完成幂等检查、业务插入和可选 Outbox 插入。"""

        # 每次写调用创建独立连接，允许多个仓库实例指向同一文件。
        connection = self._connect()
        try:
            # IMMEDIATE 在读取幂等键前取得保留写锁，避免两个实例同时判断“记录不存在”。
            connection.execute("BEGIN IMMEDIATE")
            # 先按数据库主键查询，保证已成功请求的重放不受之后订单状态变化影响。
            existing_row = connection.execute(
                """
                SELECT idempotency_key, return_request_id, user_id, order_id,
                       reason, status, created_at
                FROM return_requests
                WHERE idempotency_key = ?
                """,
                # 参数化查询避免把外部字符串拼进 SQL。
                (idempotency_key,),
            ).fetchone()
            # 已经存在该幂等键时比较完整业务负载。
            if existing_row is not None:
                # 磁盘记录重新通过领域校验后才允许返回。
                existing = self._record_from_row(existing_row)
                # 相同用户、订单和原因属于合法重放。
                if (
                    existing.user_id == user_id
                    and existing.order_id == order_id
                    and existing.reason == reason
                ):
                    # 跨线程业务重放也需要自己的审批完成事件，并与本事务一起提交。
                    self._ensure_sqlite_outbox_event(
                        connection=connection,
                        record=existing,
                        metadata=outbox_metadata,
                    )
                    # 只读重放也结束当前事务，及时释放写锁。
                    connection.commit()
                    # True 表示没有再次插入记录。
                    return existing, True
                # 相同主键绑定不同负载时绝不覆盖原记录。
                raise ReturnIdempotencyConflictError

            # 新业务请求在真正插入前再次查询本人订单。
            order = self._order_repository.get_for_user(
                order_id=order_id,
                user_id=user_id,
            )
            # 不存在和越权继续共享同一错误，避免枚举其他用户订单。
            if order is None:
                raise ReturnOrderUnavailableError
            # 草案审批后订单状态仍可能改变，因此提交事务时再次验证已签收。
            if order.status != OrderStatus.DELIVERED:
                raise ReturnOrderNotEligibleError

            # UUID5 让相同幂等键在内存和 SQLite 实现中得到相同演示编号规则。
            stable_uuid = uuid5(NAMESPACE_URL, f"serviceops-return:{idempotency_key}")
            # 构造持久化前的强类型记录。
            record = ReturnRequestRecord(
                # 业务编号取稳定 UUID 前十二位十六进制。
                return_request_id=f"RR-{stable_uuid.hex[:12].upper()}",
                # 已验证本人订单。
                order_id=order_id,
                # 系统绑定身份。
                user_id=user_id,
                # 人工审批过的原因。
                reason=reason,
                # 新申请固定进入 submitted。
                status=ReturnRequestStatus.SUBMITTED,
                # 数据库主键。
                idempotency_key=idempotency_key,
                # 保存明确 UTC 时间。
                created_at=datetime.now(UTC),
            )
            # 使用参数化 INSERT，主键/唯一约束是应用锁之外的最后一道幂等防线。
            connection.execute(
                """
                INSERT INTO return_requests (
                    idempotency_key, return_request_id, user_id, order_id,
                    reason, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.idempotency_key,
                    record.return_request_id,
                    record.user_id,
                    record.order_id,
                    record.reason,
                    record.status.value,
                    record.created_at.isoformat(),
                ),
            )
            # 在提交业务记录前写入 Outbox；任一 INSERT 失败会在 except 中整体回滚。
            self._ensure_sqlite_outbox_event(
                connection=connection,
                record=record,
                metadata=outbox_metadata,
            )
            # 两条 INSERT 都成功后才提交，外部永远不会只看到其中一条。
            connection.commit()
            # False 表示当前调用首次创建。
            return record, False
        except Exception:
            # 业务拒绝、冲突或 SQLite 异常都回滚未提交事务并释放写锁。
            connection.rollback()
            # 保留原异常类型供 Tool 层转换为有限结果或系统故障。
            raise
        finally:
            # 每次调用最终关闭连接，避免 Windows 文件长期被无关句柄占用。
            connection.close()

    def count(self) -> int:
        """返回磁盘中唯一退货申请数量，供测试和本地演示验证持久化。"""

        # 计数使用独立只读连接，不与写事务共享状态。
        connection = self._connect()
        try:
            # COUNT(*) 永远返回一行整数。
            row = connection.execute("SELECT COUNT(*) AS total FROM return_requests").fetchone()
            # 表存在且查询成功时 row 必然非空，断言帮助静态类型收窄。
            assert row is not None
            # sqlite3 返回的整数转换为明确 Python int。
            return int(row["total"])
        finally:
            # 及时关闭计数连接。
            connection.close()

    def list_pending(
        self,
        *,
        limit: int = 100,
        thread_id: str | None = None,
        as_of: datetime | None = None,
    ) -> list[OutboxEventRecord]:
        """按创建顺序读取已经到期的 SQLite pending 事件。"""

        if not 1 <= limit <= 1_000:
            raise ValueError("Outbox 扫描 limit 必须位于 1 到 1000")
        scan_time = (as_of or datetime.now(UTC)).isoformat()
        connection = self._connect()
        try:
            # thread_id 过滤只解析受控 JSON 中的固定字段，支持审批接口即时补偿自己的事件。
            rows = connection.execute(
                """
                SELECT event_id, event_type, aggregate_type, aggregate_id,
                       payload_json, status, attempts, next_attempt_at,
                       created_at, processed_at, last_error_code
                FROM return_outbox_events
                WHERE status = 'pending'
                  AND next_attempt_at <= ?
                  AND (? IS NULL OR json_extract(payload_json, '$.thread_id') = ?)
                ORDER BY created_at ASC, event_id ASC
                LIMIT ?
                """,
                (scan_time, thread_id, thread_id, limit),
            ).fetchall()
            return [self._outbox_from_row(row) for row in rows]
        finally:
            connection.close()

    def mark_processed(self, event_id: str) -> OutboxEventRecord:
        """在短事务中幂等标记 SQLite Outbox 事件已经成功投递。"""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT event_id, event_type, aggregate_type, aggregate_id,
                       payload_json, status, attempts, next_attempt_at,
                       created_at, processed_at, last_error_code
                FROM return_outbox_events
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
            if row is None:
                raise KeyError("未知 Outbox 事件")
            existing = self._outbox_from_row(row)
            if existing.status == OutboxStatus.PROCESSED:
                connection.commit()
                return existing
            if existing.status != OutboxStatus.PENDING:
                raise ValueError("只有 pending Outbox 事件可以标记完成")
            processed_at = datetime.now(UTC)
            connection.execute(
                """
                UPDATE return_outbox_events
                SET status = 'processed', processed_at = ?, last_error_code = NULL
                WHERE event_id = ? AND status = 'pending'
                """,
                (processed_at.isoformat(), event_id),
            )
            connection.commit()
            return existing.model_copy(
                update={
                    "status": OutboxStatus.PROCESSED,
                    "processed_at": processed_at,
                    "last_error_code": None,
                }
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_failure(
        self,
        event_id: str,
        *,
        error_code: str,
        max_attempts: int = 3,
    ) -> OutboxEventRecord:
        """在短事务中增加 SQLite 失败次数，并设置退避或死信状态。"""

        if not error_code or len(error_code) > 100:
            raise ValueError("Outbox error_code 长度不合法")
        if not 1 <= max_attempts <= 20:
            raise ValueError("Outbox max_attempts 必须位于 1 到 20")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT event_id, event_type, aggregate_type, aggregate_id,
                       payload_json, status, attempts, next_attempt_at,
                       created_at, processed_at, last_error_code
                FROM return_outbox_events
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
            if row is None:
                raise KeyError("未知 Outbox 事件")
            existing = self._outbox_from_row(row)
            if existing.status in {OutboxStatus.PROCESSED, OutboxStatus.DEAD_LETTER}:
                connection.commit()
                return existing
            attempts = existing.attempts + 1
            status = (
                OutboxStatus.DEAD_LETTER
                if attempts >= max_attempts
                else OutboxStatus.PENDING
            )
            next_attempt_at = datetime.now(UTC) + timedelta(seconds=2**attempts)
            connection.execute(
                """
                UPDATE return_outbox_events
                SET status = ?, attempts = ?, next_attempt_at = ?, last_error_code = ?
                WHERE event_id = ? AND status = 'pending'
                """,
                (
                    status.value,
                    attempts,
                    next_attempt_at.isoformat(),
                    error_code,
                    event_id,
                ),
            )
            connection.commit()
            return existing.model_copy(
                update={
                    "status": status,
                    "attempts": attempts,
                    "next_attempt_at": next_attempt_at,
                    "last_error_code": error_code,
                }
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_outbox_event(self, event_id: str) -> OutboxEventRecord | None:
        """按稳定 ID 查询一条 SQLite Outbox 事件。"""

        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT event_id, event_type, aggregate_type, aggregate_id,
                       payload_json, status, attempts, next_attempt_at,
                       created_at, processed_at, last_error_code
                FROM return_outbox_events
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
            return None if row is None else self._outbox_from_row(row)
        finally:
            connection.close()

    def count_outbox(self, status: OutboxStatus | None = None) -> int:
        """统计 SQLite Outbox 全部事件或指定状态事件。"""

        connection = self._connect()
        try:
            if status is None:
                row = connection.execute(
                    "SELECT COUNT(*) AS total FROM return_outbox_events"
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) AS total FROM return_outbox_events WHERE status = ?",
                    (status.value,),
                ).fetchone()
            assert row is not None
            return int(row["total"])
        finally:
            connection.close()


# 默认仓库与应用默认订单仓库共享归属数据；API 进程中复用同一实例保存幂等记录。
default_return_request_repository = InMemoryReturnRequestRepository(default_order_repository)

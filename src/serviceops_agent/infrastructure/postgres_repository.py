"""PostgreSQL 业务仓储：退货申请、事务 Outbox 与审批审计哈希链。

本文件只负责“真实业务数据”，LangGraph 自己的执行进度由官方
``AsyncPostgresSaver`` 管理。两者可以使用同一个 PostgreSQL 数据库，但表和职责分开。
"""

# Mapping 用于描述 psycopg 字典行；数据库行仍会再次交给 Pydantic 校验。
from collections.abc import Mapping

# datetime/UTC 生成无时区歧义的业务时间；timedelta 计算 Outbox 指数退避时间。
from datetime import UTC, datetime, timedelta

# Any 表示驱动返回值在进入 Pydantic 领域校验前可能属于不同基础类型。
from typing import Any

# uuid5 根据幂等键生成跨进程稳定的退货申请编号。
from uuid import NAMESPACE_URL, uuid5

# psycopg 提供同步 PostgreSQL 连接；业务工具当前是同步仓储协议。
from psycopg import Connection

# DictRow 让查询结果可以按列名读取，避免依赖脆弱的列下标。
from psycopg.rows import DictRow

# Jsonb 显式告诉驱动把 Python 字典安全编码成 PostgreSQL JSONB，而不是拼接 SQL。
from psycopg.types.json import Jsonb

# ConnectionPool 复用有限数据库连接，防止每个请求反复建立 TCP 连接。
from psycopg_pool import ConnectionPool

# 审批领域对象与哈希规则保证 PostgreSQL 和 SQLite 产生完全相同的证据语义。
from serviceops_agent.domain.audit import (
    AUDIT_GENESIS_HASH,
    ApprovalAuditDraft,
    ApprovalAuditEvent,
)

# 订单状态用于在真正提交写事务前再次确认退货资格。
from serviceops_agent.domain.orders import OrderStatus

# Outbox 强类型模型限制事件类型、状态、最小载荷和重试字段。
from serviceops_agent.domain.outbox import (
    OutboxEventRecord,
    OutboxStatus,
    ReturnOutboxMetadata,
)

# 退货申请领域模型约束编号、归属、状态、幂等键和带时区时间。
from serviceops_agent.domain.returns import ReturnRequestRecord, ReturnRequestStatus

# 复用已经由内存/SQLite 实现验证过的审计事件构造、幂等比较和哈希链校验函数。
from serviceops_agent.infrastructure.audit_repository import (
    ApprovalAuditConflictError,
    _build_event,
    _draft_matches_event,
    _verify_events,
)

# 订单仓库是写入前执行“订单存在、属于本人、已签收”检查的可信读取边界。
from serviceops_agent.infrastructure.order_repository import OrderRepository

# 复用统一的 Outbox 构造规则与退货仓储业务异常，避免三个后端行为分叉。
from serviceops_agent.infrastructure.return_repository import (
    InMemoryReturnRequestRepository,
    ReturnIdempotencyConflictError,
    ReturnOrderNotEligibleError,
    ReturnOrderUnavailableError,
    ReturnOutboxConflictError,
)

# 一条连接固定返回字典行；该别名让运行时与仓储共享同一个准确类型。
type PostgresConnection = Connection[DictRow]

# 一个连接池管理多条上述连接；API 整个生命周期只创建一个池。
type PostgresConnectionPool = ConnectionPool[PostgresConnection]

# 所有查询都选择相同字段，减少某个方法遗漏字段导致模型恢复失败的风险。
RETURN_SELECT_COLUMNS = """
    idempotency_key, return_request_id, user_id, order_id,
    reason, status, created_at
"""

# Outbox 查询列集中定义；JSONB 列仍使用 payload_json 这个稳定业务名称。
OUTBOX_SELECT_COLUMNS = """
    event_id, event_type, aggregate_type, aggregate_id,
    payload_json, status, attempts, next_attempt_at,
    created_at, processed_at, last_error_code
"""

# 审批审计列集中定义，所有读取路径都会恢复同一份完整证据事件。
AUDIT_SELECT_COLUMNS = """
    audit_event_id, thread_id, event_type, request_id, actor_id,
    token_jti, approved, order_id, proposal_digest, comment_digest,
    return_request_id, chain_position, created_at,
    previous_event_hash, event_hash
"""


class PostgresReturnRequestRepository:
    """使用 PostgreSQL 事务、唯一键和连接池的多实例退货仓储。"""

    def __init__(
        self,
        *,
        pool: PostgresConnectionPool,
        order_repository: OrderRepository,
    ) -> None:
        """保存共享连接池和订单读取边界；业务表必须已经由 Alembic 升级到 head。"""

        # 同一进程的业务仓储与审计仓储共享一个有限连接池。
        self._pool = pool
        # 订单仓储仍决定归属与资格，PostgreSQL 只保存已经批准的写入结果。
        self._order_repository = order_repository

    @staticmethod
    def _record_from_row(row: Mapping[str, Any]) -> ReturnRequestRecord:
        """把 PostgreSQL 字典行重新交给 Pydantic 领域模型完整校验。"""

        # TIMESTAMPTZ 会由 psycopg 返回带时区 datetime，领域模型继续检查其他字段。
        return ReturnRequestRecord.model_validate(dict(row))

    @staticmethod
    def _outbox_from_row(row: Mapping[str, Any]) -> OutboxEventRecord:
        """把 JSONB 和状态字段恢复成强类型 Outbox 事件。"""

        # psycopg 会把 JSONB 解码成 Python 字典，Pydantic 再验证所有内部字段。
        return OutboxEventRecord.model_validate(
            {
                # 稳定事件编号。
                "event_id": row["event_id"],
                # 有限事件类型。
                "event_type": row["event_type"],
                # 聚合类型固定为退货申请。
                "aggregate_type": row["aggregate_type"],
                # 实际退货申请编号。
                "aggregate_id": row["aggregate_id"],
                # PostgreSQL JSONB 已解码的最小审计载荷。
                "payload": row["payload_json"],
                # 当前投递状态。
                "status": row["status"],
                # 累计失败次数。
                "attempts": row["attempts"],
                # 下一次允许重试的时间。
                "next_attempt_at": row["next_attempt_at"],
                # 与业务事务共同提交的创建时间。
                "created_at": row["created_at"],
                # 成功处理时间可能为空。
                "processed_at": row["processed_at"],
                # 只保存有限错误码，不保存异常正文。
                "last_error_code": row["last_error_code"],
            }
        )

    def _ensure_outbox_event(
        self,
        *,
        connection: PostgresConnection,
        record: ReturnRequestRecord,
        metadata: ReturnOutboxMetadata | None,
    ) -> None:
        """在当前业务事务内插入或核对稳定 Outbox 事件。"""

        # 三种后端共用相同强类型构造逻辑，保证事件编号和载荷完全一致。
        candidate = InMemoryReturnRequestRepository._build_outbox_event(
            record=record,
            metadata=metadata,
        )
        # 直接运行 LangGraph 教学示例没有可信 HTTP 元数据，因此允许不创建 Outbox。
        if candidate is None:
            return
        # 对稳定事件 ID 加事务锁，避免不同实例同时查询到“不存在”后竞争插入。
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"serviceops-outbox:{candidate.event_id}",),
        )
        # 参数化查询不会把外部标识拼成 SQL 语法。
        existing_row = connection.execute(
            f"SELECT {OUTBOX_SELECT_COLUMNS} FROM return_outbox_events WHERE event_id = %s",
            (candidate.event_id,),
        ).fetchone()
        # 已存在时只比较不可变业务语义；状态和重试时间允许随协调过程变化。
        if existing_row is not None:
            existing = self._outbox_from_row(existing_row)
            if (
                existing.event_type == candidate.event_type
                and existing.aggregate_type == candidate.aggregate_type
                and existing.aggregate_id == candidate.aggregate_id
                and existing.payload == candidate.payload
            ):
                return
            # 同一稳定 ID 指向不同可信事实时必须拒绝，不能覆盖原事件。
            raise ReturnOutboxConflictError
        # 该 INSERT 与退货申请 INSERT 使用同一连接和事务。
        connection.execute(
            """
            INSERT INTO return_outbox_events (
                event_id, event_type, aggregate_type, aggregate_id,
                payload_json, status, attempts, next_attempt_at,
                created_at, processed_at, last_error_code
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                candidate.event_id,
                candidate.event_type.value,
                candidate.aggregate_type,
                candidate.aggregate_id,
                Jsonb(candidate.payload.model_dump(mode="json")),
                candidate.status.value,
                candidate.attempts,
                candidate.next_attempt_at,
                candidate.created_at,
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
        """原子完成幂等检查、退货申请创建和可选 Outbox 创建。"""

        # 借用连接并开启事务；正常退出自动提交，抛出异常自动回滚后归还连接。
        with self._pool.connection() as connection:
            # 按幂等键串行化相同请求，不阻塞其他不同订单的正常写入。
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"serviceops-return:{idempotency_key}",),
            )
            # 先读取既有记录，使已成功请求的重放不受后来订单状态变化影响。
            existing_row = connection.execute(
                f"SELECT {RETURN_SELECT_COLUMNS} FROM return_requests WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
            # 已经处理过该键时，必须核对完整业务负载。
            if existing_row is not None:
                existing = self._record_from_row(existing_row)
                if (
                    existing.user_id == user_id
                    and existing.order_id == order_id
                    and existing.reason == reason
                ):
                    # 合法重放仍需补齐或确认本次线程对应的 Outbox 事件。
                    self._ensure_outbox_event(
                        connection=connection,
                        record=existing,
                        metadata=outbox_metadata,
                    )
                    # True 明确表示本次没有再次创建业务记录。
                    return existing, True
                # 同一个幂等键复用于不同负载属于调用方冲突。
                raise ReturnIdempotencyConflictError

            # 新写入在事务提交前再次检查本人订单，避免审批等待期间资格已经变化。
            order = self._order_repository.get_for_user(
                order_id=order_id,
                user_id=user_id,
            )
            # 不存在与越权共享同一错误，防止枚举其他用户订单。
            if order is None:
                raise ReturnOrderUnavailableError
            # 当前规则只允许已签收订单进入退货申请。
            if order.status != OrderStatus.DELIVERED:
                raise ReturnOrderNotEligibleError

            # UUID5 让 memory、SQLite、PostgreSQL 针对同一幂等键产生相同业务编号。
            stable_uuid = uuid5(NAMESPACE_URL, f"serviceops-return:{idempotency_key}")
            # 先构造强类型领域对象，确保非法数据不会到达数据库。
            record = ReturnRequestRecord(
                return_request_id=f"RR-{stable_uuid.hex[:12].upper()}",
                order_id=order_id,
                user_id=user_id,
                reason=reason,
                status=ReturnRequestStatus.SUBMITTED,
                idempotency_key=idempotency_key,
                created_at=datetime.now(UTC),
            )
            # 唯一约束是建议锁之外的最终并发防线。
            connection.execute(
                """
                INSERT INTO return_requests (
                    idempotency_key, return_request_id, user_id, order_id,
                    reason, status, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record.idempotency_key,
                    record.return_request_id,
                    record.user_id,
                    record.order_id,
                    record.reason,
                    record.status.value,
                    record.created_at,
                ),
            )
            # Outbox 任一失败会让连接上下文回滚上面的业务 INSERT。
            self._ensure_outbox_event(
                connection=connection,
                record=record,
                metadata=outbox_metadata,
            )
            # False 表示当前事务首次创建了业务记录。
            return record, False

    def count(self) -> int:
        """统计 PostgreSQL 中唯一退货申请数量，供 readiness 与测试使用。"""

        # 计数查询只短暂借用一个连接。
        with self._pool.connection() as connection:
            # COUNT(*) 总会返回一行。
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM return_requests"
            ).fetchone()
            # 断言帮助类型检查器确认下面可以按列读取。
            assert row is not None
            # PostgreSQL bigint 转换成明确 Python int。
            return int(row["total"])

    def list_pending(
        self,
        *,
        limit: int = 100,
        thread_id: str | None = None,
        as_of: datetime | None = None,
    ) -> list[OutboxEventRecord]:
        """按创建顺序读取已经到期的 PostgreSQL pending 事件。"""

        # 有限批量避免一次协调任务无边界占用数据库和进程内存。
        if not 1 <= limit <= 1_000:
            raise ValueError("Outbox 扫描 limit 必须位于 1 到 1000")
        # 测试可注入明确时间；正常运行使用当前 UTC 时间。
        scan_time = as_of or datetime.now(UTC)
        # 借用只读查询连接。
        with self._pool.connection() as connection:
            # ->> 从受控 JSONB 载荷提取 thread_id；LIMIT 仍使用参数化整数。
            rows = connection.execute(
                f"""
                SELECT {OUTBOX_SELECT_COLUMNS}
                FROM return_outbox_events
                WHERE status = 'pending'
                  AND next_attempt_at <= %s
                  AND (%s::text IS NULL OR payload_json ->> 'thread_id' = %s)
                ORDER BY created_at ASC, event_id ASC
                LIMIT %s
                """,
                (scan_time, thread_id, thread_id, limit),
            ).fetchall()
            # 每一行都必须重新通过 Pydantic 校验后才交给协调器。
            return [self._outbox_from_row(row) for row in rows]

    def mark_processed(self, event_id: str) -> OutboxEventRecord:
        """在短事务中幂等标记一条 Outbox 事件已处理。"""

        # 同一事件状态推进必须串行，FOR UPDATE 会锁住查到的那一行直到事务结束。
        with self._pool.connection() as connection:
            row = connection.execute(
                f"""
                SELECT {OUTBOX_SELECT_COLUMNS}
                FROM return_outbox_events
                WHERE event_id = %s
                FOR UPDATE
                """,
                (event_id,),
            ).fetchone()
            # 未知编号不能静默创建伪事件。
            if row is None:
                raise KeyError("未知 Outbox 事件")
            # 磁盘数据先恢复成强类型事件。
            existing = self._outbox_from_row(row)
            # 重复确认 processed 是安全幂等操作，不改变首次完成时间。
            if existing.status == OutboxStatus.PROCESSED:
                return existing
            # 死信必须走显式人工处置，不能被普通协调器直接改成成功。
            if existing.status != OutboxStatus.PENDING:
                raise ValueError("只有 pending Outbox 事件可以标记完成")
            # 成功时间由当前服务端 UTC 时钟生成。
            processed_at = datetime.now(UTC)
            # 状态更新与上面的加锁读取处于同一事务。
            connection.execute(
                """
                UPDATE return_outbox_events
                SET status = 'processed', processed_at = %s, last_error_code = NULL
                WHERE event_id = %s AND status = 'pending'
                """,
                (processed_at, event_id),
            )
            # 返回与即将提交的数据库状态一致的领域副本。
            return existing.model_copy(
                update={
                    "status": OutboxStatus.PROCESSED,
                    "processed_at": processed_at,
                    "last_error_code": None,
                }
            )

    def record_failure(
        self,
        event_id: str,
        *,
        error_code: str,
        max_attempts: int = 3,
    ) -> OutboxEventRecord:
        """记录有限错误码，并计算下一次退避时间或死信状态。"""

        # 不保存无边界异常正文，避免敏感信息和大文本进入业务表。
        if not error_code or len(error_code) > 100:
            raise ValueError("Outbox error_code 长度不合法")
        # 限制尝试次数，防止错误配置产生近乎无限重试。
        if not 1 <= max_attempts <= 20:
            raise ValueError("Outbox max_attempts 必须位于 1 到 20")
        # FOR UPDATE 保证并发失败上报不会丢失次数更新。
        with self._pool.connection() as connection:
            row = connection.execute(
                f"""
                SELECT {OUTBOX_SELECT_COLUMNS}
                FROM return_outbox_events
                WHERE event_id = %s
                FOR UPDATE
                """,
                (event_id,),
            ).fetchone()
            # 事件不存在时由调用方排查编号来源。
            if row is None:
                raise KeyError("未知 Outbox 事件")
            # 强类型恢复当前状态。
            existing = self._outbox_from_row(row)
            # 已完成或已死信的事件重复上报失败时保持原状态。
            if existing.status in {OutboxStatus.PROCESSED, OutboxStatus.DEAD_LETTER}:
                return existing
            # 当前失败在原次数基础上加一。
            attempts = existing.attempts + 1
            # 达到阈值后停止普通扫描，否则继续保持 pending。
            status = (
                OutboxStatus.DEAD_LETTER
                if attempts >= max_attempts
                else OutboxStatus.PENDING
            )
            # 退避时间依次约为 2、4、8 秒，减少持续故障时的请求风暴。
            next_attempt_at = datetime.now(UTC) + timedelta(seconds=2**attempts)
            # 更新仍受当前行锁保护。
            connection.execute(
                """
                UPDATE return_outbox_events
                SET status = %s, attempts = %s, next_attempt_at = %s, last_error_code = %s
                WHERE event_id = %s AND status = 'pending'
                """,
                (status.value, attempts, next_attempt_at, error_code, event_id),
            )
            # 返回数据库提交后的预期快照。
            return existing.model_copy(
                update={
                    "status": status,
                    "attempts": attempts,
                    "next_attempt_at": next_attempt_at,
                    "last_error_code": error_code,
                }
            )

    def get_outbox_event(self, event_id: str) -> OutboxEventRecord | None:
        """按稳定编号读取一条 PostgreSQL Outbox 事件。"""

        # 单行查询短暂借用连接。
        with self._pool.connection() as connection:
            row = connection.execute(
                f"SELECT {OUTBOX_SELECT_COLUMNS} FROM return_outbox_events WHERE event_id = %s",
                (event_id,),
            ).fetchone()
            # 不存在返回 None，存在则经过领域校验。
            return None if row is None else self._outbox_from_row(row)

    def count_outbox(self, status: OutboxStatus | None = None) -> int:
        """统计全部 Outbox 事件或某一有限状态的事件。"""

        # 计数查询复用连接池。
        with self._pool.connection() as connection:
            # None 表示统计整张表。
            if status is None:
                row = connection.execute(
                    "SELECT COUNT(*) AS total FROM return_outbox_events"
                ).fetchone()
            else:
                # 枚举值仍使用参数化查询。
                row = connection.execute(
                    "SELECT COUNT(*) AS total FROM return_outbox_events WHERE status = %s",
                    (status.value,),
                ).fetchone()
            # COUNT(*) 必然返回一行。
            assert row is not None
            # 转换为协议要求的 Python int。
            return int(row["total"])


class PostgresApprovalAuditRepository:
    """使用 PostgreSQL 行约束、事务锁和触发器的只追加审批审计仓储。"""

    def __init__(self, *, pool: PostgresConnectionPool) -> None:
        """保存与退货仓储共享的连接池。"""

        # 共享连接池让 readiness、退货与审计都服从同一连接数量上限。
        self._pool = pool

    @staticmethod
    def _event_from_row(row: Mapping[str, Any]) -> ApprovalAuditEvent:
        """把 PostgreSQL 行重新交给 Pydantic 审计模型校验。"""

        # BOOLEAN 和 TIMESTAMPTZ 已由 psycopg 转成 Python 类型，其余摘要/枚举继续校验。
        return ApprovalAuditEvent.model_validate(dict(row))

    def append(self, draft: ApprovalAuditDraft) -> tuple[ApprovalAuditEvent, bool]:
        """在单个事务内幂等检查、计算前驱并追加一条审计事件。"""

        # 当前线程的链操作使用同一事务连接。
        with self._pool.connection() as connection:
            # 每个 thread_id 独立串行化，避免两个副本计算出相同位置或不同分支。
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"serviceops-audit:{draft.thread_id}",),
            )
            # 同一线程同一事件类型只允许存在一条。
            existing_row = connection.execute(
                f"""
                SELECT {AUDIT_SELECT_COLUMNS}
                FROM approval_audit_events
                WHERE thread_id = %s AND event_type = %s
                """,
                (draft.thread_id, draft.event_type.value),
            ).fetchone()
            # 已存在时区分合法重放与业务冲突。
            if existing_row is not None:
                existing = self._event_from_row(existing_row)
                if _draft_matches_event(draft, existing):
                    return existing, True
                raise ApprovalAuditConflictError

            # 在事务锁内读取最后一条事件，保证前驱与新位置属于一致快照。
            last_row = connection.execute(
                """
                SELECT chain_position, event_hash
                FROM approval_audit_events
                WHERE thread_id = %s
                ORDER BY chain_position DESC
                LIMIT 1
                """,
                (draft.thread_id,),
            ).fetchone()
            # 空线程从公开 Genesis Hash 和位置 1 开始。
            chain_position = 1 if last_row is None else int(last_row["chain_position"]) + 1
            # 非空线程必须准确引用上一条事件摘要。
            previous_hash = (
                AUDIT_GENESIS_HASH if last_row is None else str(last_row["event_hash"])
            )
            # 构造器根据完整强类型字段计算最终 SHA-256 哈希。
            event = _build_event(
                draft=draft,
                chain_position=chain_position,
                previous_event_hash=previous_hash,
            )
            # 参数化 INSERT 与唯一约束共同防止事件重复和链位置分叉。
            connection.execute(
                """
                INSERT INTO approval_audit_events (
                    audit_event_id, thread_id, event_type, request_id, actor_id,
                    token_jti, approved, order_id, proposal_digest, comment_digest,
                    return_request_id, chain_position, created_at,
                    previous_event_hash, event_hash
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )
                """,
                (
                    event.audit_event_id,
                    event.thread_id,
                    event.event_type.value,
                    event.request_id,
                    event.actor_id,
                    event.token_jti,
                    event.approved,
                    event.order_id,
                    event.proposal_digest,
                    event.comment_digest,
                    event.return_request_id,
                    event.chain_position,
                    event.created_at,
                    event.previous_event_hash,
                    event.event_hash,
                ),
            )
            # False 表示本事务首次追加了该事件。
            return event, False

    def list_for_thread(self, thread_id: str) -> list[ApprovalAuditEvent]:
        """按链位置读取某一线程的完整审批证据链。"""

        # 只读查询短暂借用连接。
        with self._pool.connection() as connection:
            # ORDER BY 是后续哈希链校验的必要前提。
            rows = connection.execute(
                f"""
                SELECT {AUDIT_SELECT_COLUMNS}
                FROM approval_audit_events
                WHERE thread_id = %s
                ORDER BY chain_position ASC
                """,
                (thread_id,),
            ).fetchall()
            # 数据库中的每一行都重新经过领域 Schema 校验。
            return [self._event_from_row(row) for row in rows]

    def verify_thread_chain(self, thread_id: str) -> bool:
        """重新读取并验证位置、前驱引用和每一条事件哈希。"""

        # 公用校验器使 memory、SQLite、PostgreSQL 使用同一套验证算法。
        return _verify_events(self.list_for_thread(thread_id))

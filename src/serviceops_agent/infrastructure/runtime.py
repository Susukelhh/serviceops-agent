"""创建并管理 FastAPI 进程使用的 Agent 持久化运行时资源。"""

# AsyncIterator 标注异步上下文管理器在生命周期中产出的资源。
from collections.abc import AsyncIterator

# asynccontextmanager 确保数据库 Checkpointer 在启动/关闭阶段正确打开和释放。
from contextlib import asynccontextmanager

# dataclass 把图、业务仓库和后端名称组合成不可变运行时对象。
from dataclasses import dataclass

# Literal 把运行时后端名称限制为设置允许的三个值。
from typing import Literal, Protocol

# InMemorySaver 用于自动测试；PostgreSQL Saver 用于可扩展的服务端持久化。
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

# AsyncSqliteSaver 用于不启动 Docker 时的单机跨重启学习。
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

# dict_row 让 PostgreSQL 业务查询结果按列名读取并恢复为强类型领域对象。
from psycopg.rows import dict_row

# ConnectionPool 在 API 生命周期内复用有限数量的 PostgreSQL 业务连接。
from psycopg_pool import ConnectionPool

# 路径统一以项目根目录解析，避免依赖 Uvicorn/PyCharm 当前目录。
from serviceops_agent.config.paths import resolve_project_path

# Settings 提供持久化后端和两个数据库路径。
from serviceops_agent.config.settings import Settings

# 图工厂把当前运行时选择的 Checkpointer 和退货仓库绑定到同一张图。
from serviceops_agent.graph.builder import ServiceGraph, build_service_graph

# 审批审计仓库与退货业务仓库共享运行时后端，但使用独立领域接口和数据表。
from serviceops_agent.infrastructure.audit_repository import (
    ApprovalAuditRepository,
    InMemoryApprovalAuditRepository,
    SQLiteApprovalAuditRepository,
)

# 显式类型白名单避免 Checkpointer 从数据库恢复任意 Python 类型。
from serviceops_agent.infrastructure.checkpoint_serde import create_checkpoint_serializer

# 会话仓库与 Checkpointer 分工：前者保存跨轮业务索引，后者保存单轮图执行快照。
from serviceops_agent.infrastructure.conversation_repository import (
    ConversationRepository,
    InMemoryConversationRepository,
    PostgresConversationRepository,
    SQLiteConversationRepository,
)

# 订单仓库是退货写入前归属和状态二次检查的数据源。
from serviceops_agent.infrastructure.order_repository import (
    OrderRepository,
    PublicDemoOrderRepository,
    default_order_repository,
)

# 三种退货仓库实现共享同一协议，业务节点不需要知道存储后端。
from serviceops_agent.infrastructure.outbox_repository import ReturnOutboxRepository

# PostgreSQL 实现让多个 API 实例共同读写同一套退货、Outbox 和审批审计数据。
from serviceops_agent.infrastructure.postgres_repository import (
    PostgresApprovalAuditRepository,
    PostgresConnectionPool,
    PostgresReturnRequestRepository,
)
from serviceops_agent.infrastructure.return_repository import (
    InMemoryReturnRequestRepository,
    ReturnRequestRepository,
    SQLiteReturnRequestRepository,
)

# 默认检索器在 lifespan 内只构建一次，并把独立 Qdrant 健康探测暴露给 FastAPI。
from serviceops_agent.rag.retriever import (
    HealthCheckableKnowledgeRetriever,
    build_default_knowledge_retriever,
)


class CheckpointDeleter(Protocol):
    """会话隐私清理只需要的最小 LangGraph Checkpoint 删除能力。"""

    async def adelete_thread(self, thread_id: str) -> None:
        """幂等删除一个稳定工作流线程的全部 Checkpoint。"""


@dataclass(frozen=True)
class AgentRuntime:
    """FastAPI lifespan 内共享的一组已完成装配的 Agent 资源。"""

    # service_graph 已绑定与当前后端匹配的 Checkpointer 和业务仓库。
    service_graph: ServiceGraph
    # return_request_repository 暴露给测试/readiness，普通路由仍只通过图访问它。
    return_request_repository: ReturnRequestRepository
    # return_outbox_repository 与业务仓库是同一实现，但只向协调器暴露事件状态协议。
    return_outbox_repository: ReturnOutboxRepository
    # approval_audit_repository 负责追加审批决定/结果并向审计接口提供只读链。
    approval_audit_repository: ApprovalAuditRepository
    # conversation_repository 保存会话所有权、轮次幂等状态和有限结构化记忆。
    conversation_repository: ConversationRepository
    # checkpoint_deleter 显式暴露会话清理能力，API 不读取编译图的内部属性。
    checkpoint_deleter: CheckpointDeleter
    # knowledge_retriever 同时服务 LangGraph FAQ 节点和 /ready 的 Qdrant 只读探测。
    knowledge_retriever: HealthCheckableKnowledgeRetriever
    # persistence_backend 便于健康接口和日志确认实际运行模式。
    persistence_backend: Literal["memory", "sqlite", "postgres"]


@asynccontextmanager
async def create_agent_runtime(
    settings: Settings,
    *,
    order_repository: OrderRepository = default_order_repository,
) -> AsyncIterator[AgentRuntime]:
    """按配置创建运行时，并在退出 lifespan 时释放异步 Checkpointer。"""

    # 公网模式只映射专门准备的样例订单；普通模式保持原仓库和真实身份边界。
    selected_order_repository: OrderRepository = (
        PublicDemoOrderRepository(order_repository)
        if settings.public_demo_enabled
        else order_repository
    )
    # 每个 API 进程只装配一次混合检索器；远程 Qdrant Collection 由所有副本共享。
    knowledge_retriever = build_default_knowledge_retriever(settings)
    # 根据已启用模式选择公开的低基数事件名，教学页面可区分旧重排和完整双路融合。
    retrieval_event = (
        "graph:faq_candidates_fused_rrf"
        if settings.rag_reranker == "hybrid_rrf"
        else (
            "graph:faq_candidates_reranked_bm25"
            if settings.rag_reranker == "bm25"
            else None
        )
    )

    # memory 模式保持测试完全隔离、快速且不创建磁盘文件。
    if settings.persistence_backend == "memory":
        # 当前进程创建独立业务仓库。
        memory_return_repository = InMemoryReturnRequestRepository(selected_order_repository)
        # 测试运行时使用隔离的进程内审计仓库，不创建本地数据库文件。
        memory_audit_repository = InMemoryApprovalAuditRepository()
        # 会话状态使用同一运行时生命周期，但不与 LangGraph Checkpoint 混为一张记录。
        memory_conversation_repository = InMemoryConversationRepository()
        # 当前进程创建独立 Checkpointer。
        memory_checkpointer = InMemorySaver(serde=create_checkpoint_serializer())
        # 图同时绑定这两个内存依赖。
        graph = build_service_graph(
            order_repository=selected_order_repository,
            return_request_repository=memory_return_repository,
            checkpointer=memory_checkpointer,
            knowledge_retriever=knowledge_retriever,
            retrieval_event=retrieval_event,
        )
        # lifespan 存续期间向 FastAPI 提供资源。
        yield AgentRuntime(
            service_graph=graph,
            return_request_repository=memory_return_repository,
            return_outbox_repository=memory_return_repository,
            approval_audit_repository=memory_audit_repository,
            conversation_repository=memory_conversation_repository,
            checkpoint_deleter=memory_checkpointer,
            knowledge_retriever=knowledge_retriever,
            persistence_backend="memory",
        )
        # 内存对象不持有外部连接，退出后直接结束生成器即可。
        return

    # sqlite 分支保留零额外服务的单机学习体验。
    if settings.persistence_backend == "sqlite":
        # SQLite 业务库路径不依赖当前工作目录。
        business_database_path = resolve_project_path(settings.business_database_path)
        # SQLite 业务仓库会在初始化时创建父目录、WAL 模式和唯一约束表。
        sqlite_return_repository = SQLiteReturnRequestRepository(
            database_path=business_database_path,
            order_repository=selected_order_repository,
        )
        # 审计事件属于业务/安全记录，保存在同一业务数据库的独立只追加表中。
        sqlite_audit_repository = SQLiteApprovalAuditRepository(
            database_path=business_database_path,
        )
        # 会话表属于业务索引，与退货/审计表共用业务数据库而不是 Checkpoint 数据库。
        sqlite_conversation_repository = SQLiteConversationRepository(
            database_path=business_database_path,
        )
        # Checkpoint 数据库与业务数据库分开，强调两者生命周期和职责不同。
        checkpoint_database_path = resolve_project_path(settings.checkpoint_database_path)
        # 官方 Saver 不会替应用创建父目录，因此在连接前明确创建。
        checkpoint_database_path.parent.mkdir(parents=True, exist_ok=True)

        # from_conn_string 管理连接，退出 async with 时可靠关闭后台线程和文件句柄。
        async with AsyncSqliteSaver.from_conn_string(
            str(checkpoint_database_path)
        ) as sqlite_checkpointer:
            # SQLite 工厂当前不接受 serde 参数，因此连接建立后立即替换为同一安全白名单实例。
            sqlite_checkpointer.serde = create_checkpoint_serializer()
            # 显式 setup 在应用启动期建表；失败会阻止服务假装健康启动。
            await sqlite_checkpointer.setup()
            # 编译绑定 SQLite Checkpointer 与 SQLite 业务仓库的图。
            graph = build_service_graph(
                order_repository=selected_order_repository,
                return_request_repository=sqlite_return_repository,
                checkpointer=sqlite_checkpointer,
                knowledge_retriever=knowledge_retriever,
                retrieval_event=retrieval_event,
            )
            # 整个 FastAPI lifespan 复用同一异步 Saver 连接。
            yield AgentRuntime(
                service_graph=graph,
                return_request_repository=sqlite_return_repository,
                return_outbox_repository=sqlite_return_repository,
                approval_audit_repository=sqlite_audit_repository,
                conversation_repository=sqlite_conversation_repository,
                checkpoint_deleter=sqlite_checkpointer,
                knowledge_retriever=knowledge_retriever,
                persistence_backend="sqlite",
            )
        # 离开 async with 后 Saver 连接已关闭，不再允许图处理新请求。
        return

    # Settings 校验器已保证 postgres 模式一定具有非空连接地址。
    assert settings.postgres_dsn is not None
    # 只在这里短暂取出明文供驱动连接；不会把它写进日志、异常包装或运行时对象。
    postgres_dsn = settings.postgres_dsn.get_secret_value()
    # 业务仓储使用同步连接池；open=False 避免构造函数隐式启动后台资源。
    postgres_pool: PostgresConnectionPool = ConnectionPool(
        conninfo=postgres_dsn,
        kwargs={"row_factory": dict_row},
        min_size=settings.postgres_pool_min_size,
        max_size=settings.postgres_pool_max_size,
        open=False,
    )
    try:
        # wait=True 要求至少建立最小数量连接；数据库不可达会直接阻止 API 假启动。
        postgres_pool.open(wait=True)
        # 业务表结构由启动前的独立 Alembic 任务管理；API 不拥有 DDL 修改权限边界。
        # 退货仓储与 Outbox 协议由同一对象实现，二者可以共享单个数据库事务。
        postgres_return_repository = PostgresReturnRequestRepository(
            pool=postgres_pool,
            order_repository=selected_order_repository,
        )
        # 审批审计仓储共享连接池，但使用独立表与只追加触发器。
        postgres_audit_repository = PostgresApprovalAuditRepository(
            pool=postgres_pool,
        )
        # 多实例会话仓库复用有限同步连接池，表结构由 Alembic 预先创建。
        postgres_conversation_repository = PostgresConversationRepository(
            pool=postgres_pool,
        )
        # 官方异步 Saver 单独管理 LangGraph Checkpoint 连接和表结构。
        async with AsyncPostgresSaver.from_conn_string(
            postgres_dsn,
            # PostgreSQL 工厂原生接受 SerializerProtocol，直接注入有限类型白名单。
            serde=create_checkpoint_serializer(),
        ) as postgres_checkpointer:
            # 第一次部署必须执行 setup；该操作本身是幂等的。
            await postgres_checkpointer.setup()
            # 编译一张同时绑定 PostgreSQL 工作流进度和业务仓储的图。
            graph = build_service_graph(
                order_repository=selected_order_repository,
                return_request_repository=postgres_return_repository,
                checkpointer=postgres_checkpointer,
                knowledge_retriever=knowledge_retriever,
                retrieval_event=retrieval_event,
            )
            # API lifespan 内所有请求复用这些受限资源。
            yield AgentRuntime(
                service_graph=graph,
                return_request_repository=postgres_return_repository,
                return_outbox_repository=postgres_return_repository,
                approval_audit_repository=postgres_audit_repository,
                conversation_repository=postgres_conversation_repository,
                checkpoint_deleter=postgres_checkpointer,
                knowledge_retriever=knowledge_retriever,
                persistence_backend="postgres",
            )
    finally:
        # 无论启动、请求处理或关闭阶段是否异常，都停止连接池后台线程并关闭连接。
        postgres_pool.close()

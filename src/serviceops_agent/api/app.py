"""FastAPI 应用入口。

API 层只负责协议转换：校验 HTTP 请求、调用领域状态图、序列化响应。
业务判断不会写在路由函数中，否则未来增加 CLI、消息队列消费者时会复制逻辑。
"""

# asyncio.BoundedSemaphore 为每个 API 进程提供有限业务工位；wait_for 实现短排队超时。
import asyncio

# logging 记录审计证据的读取行为，但不记录 Bearer Token 或敏感业务原文。
import logging

# AsyncIterator 标注 FastAPI lifespan；asynccontextmanager 管理异步数据库 Saver。
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

# Path 从已安装 Python 包内部定位随 wheel 一起发布的控制台静态文件。
from pathlib import Path

# perf_counter 测量 Agent 图执行耗时，不受系统时间调整影响。
from time import perf_counter

# Annotated 附加 Security 依赖；Any 接收中断字段；cast 恢复精确图类型。
from typing import Annotated, Any, cast

# UUID 校验审批路径参数；uuid4 为请求和可恢复线程生成不可预测的唯一标识。
from uuid import UUID, uuid4

# FastAPI 用于 ASGI/路由；Request/Response 支持实例诊断 Header；Security 声明 Scope。
from fastapi import FastAPI, HTTPException, Query, Request, Response, Security

# RunnableConfig 明确 Checkpointer 使用 configurable.thread_id 选择状态快照。
from langchain_core.runnables import RunnableConfig

# Command.resume 把结构化人工决定交回上次 interrupt 暂停的位置。
from langgraph.types import Command

# FastAPIInstrumentor 自动创建 HTTP Server Span 并传播 W3C Trace Context。
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

# HTTPXClientInstrumentor 追踪 LangChain/OpenAI 兼容客户端的出站模型请求。
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

# RequestResponseEndpoint 为 HTTP middleware 的下一个处理器提供严格异步类型。
from starlette.middleware.base import RequestResponseEndpoint

# JSONResponse 在容量耗尽时返回固定脱敏 503，不进入模型、图或数据库。
from starlette.responses import FileResponse, JSONResponse, RedirectResponse

# StaticFiles 只托管版本控制的 CSS/JavaScript，不允许浏览任意项目目录。
from starlette.staticfiles import StaticFiles

# 调试转换器只读取 LangGraph 官方 StateSnapshot，并执行字段白名单与递归脱敏。
from serviceops_agent.api.debug_trace import (
    MAX_DEBUG_CHECKPOINTS,
    build_thread_debug_response,
)

# 导入 API 边界模型：分别描述请求体、业务响应和健康检查响应。
from serviceops_agent.api.schemas import (
    ApprovalAuditTrailResponse,
    ApprovalDecisionRequest,
    ChatRequest,
    ChatResponse,
    DependencyCheck,
    HealthResponse,
    ReadinessResponse,
    ThreadDebugResponse,
)

# 协调器把业务事务内的 Outbox 事件至少一次投递到审批审计哈希链。
from serviceops_agent.application.outbox_reconciler import ReturnOutboxReconciler

# 导入集中配置读取函数，避免 API 模块直接访问零散环境变量。
from serviceops_agent.config.settings import get_settings

# 审计模型生成草案/备注摘要，并限制证据链事件类型。
from serviceops_agent.domain.audit import (
    ApprovalAuditDraft,
    ApprovalAuditEventType,
    build_comment_digest,
    build_proposal_digest,
)

# 运维补偿接口只返回低敏批次计数，不暴露事件载荷。
from serviceops_agent.domain.outbox import ReconciliationBatchResult

# 领域审批模型再次校验恢复值、Checkpoint 草案和最终工作流状态。
from serviceops_agent.domain.returns import (
    ApprovalDecision,
    ApprovalRequestPayload,
    ReturnRequestProposal,
    ReturnWorkflowStatus,
)

# ServiceGraph 用于 app.state 类型恢复；内存图是在不运行 lifespan 的轻量测试后备。
from serviceops_agent.graph.builder import ServiceGraph
from serviceops_agent.graph.builder import service_graph as fallback_service_graph

# 审计仓库使用独立协议；后备内存实例支持不触发 lifespan 的轻量 ASGI 测试。
from serviceops_agent.infrastructure.audit_repository import (
    ApprovalAuditConflictError,
    ApprovalAuditRepository,
    InMemoryApprovalAuditRepository,
)

# Outbox 协议用于 readiness、即时协调和受权运维重试。
from serviceops_agent.infrastructure.outbox_repository import ReturnOutboxRepository

# readiness 直接只读业务仓库，验证当前数据库连接和表可以查询。
from serviceops_agent.infrastructure.return_repository import (
    ReturnRequestRepository,
    default_return_request_repository,
)

# create_agent_runtime 在 Uvicorn lifespan 内选择内存、SQLite 或 PostgreSQL 持久化资源。
from serviceops_agent.infrastructure.runtime import create_agent_runtime

# 遥测模块配置 Provider、业务 Span、低基数指标和当前 trace_id。
from serviceops_agent.observability.telemetry import (
    configure_telemetry,
    current_trace_id,
    force_flush_telemetry,
    record_agent_execution,
    record_approval_execution,
    record_capacity_rejection,
    start_safe_span,
)

# 默认生产检索器协议同时提供搜索与独立 Qdrant 就绪探测。
from serviceops_agent.rag.retriever import HealthCheckableKnowledgeRetriever

# JWT 依赖完成签名/Claims/Scope 校验，路由只接收可信 Principal。
from serviceops_agent.security.jwt_auth import require_principal
from serviceops_agent.security.models import AuthenticatedPrincipal, PermissionScope

# 读取并缓存应用配置；FastAPI 初始化和各路由可以复用这个不可变配置对象。
settings = get_settings()

# 进程级 Provider 只初始化一次；测试通过环境变量关闭，保留 NoOp 行为。
telemetry_runtime = configure_telemetry(settings)

# 模块日志器只记录稳定标识和结果，不写完整 JWT、申请原因、备注或幂等键。
logger = logging.getLogger(__name__)

# 控制台资源位于已安装 serviceops_agent 包内部，不依赖 PyCharm Working directory。
CONSOLE_DIRECTORY = Path(__file__).resolve().parents[1] / "web"

# assets 子目录只包含无密钥 CSS/JavaScript；StaticFiles 不会暴露同级 Python 源码。
CONSOLE_ASSET_DIRECTORY = CONSOLE_DIRECTORY / "assets"


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """在服务启动时创建持久化资源，并在服务关闭时释放数据库连接。"""

    # runtime 上下文根据 Settings 选择 InMemorySaver 或 AsyncSqliteSaver。
    async with create_agent_runtime(settings) as runtime:
        # 每次进程启动创建独立工位计数器；容器重启不会继承已经占用的旧状态。
        application.state.agent_capacity_limiter = asyncio.BoundedSemaphore(
            settings.agent_max_in_flight_requests
        )
        # 图必须与 Saver 使用相同生命周期，不能在连接关闭后继续处理请求。
        application.state.service_graph = runtime.service_graph
        # 健康接口返回实际已启动的后端，而不是只读取未生效配置。
        application.state.persistence_backend = runtime.persistence_backend
        # 审批和只读审计接口共享同一个与 lifespan 绑定的审计仓库。
        application.state.approval_audit_repository = runtime.approval_audit_repository
        # readiness 使用真实后端仓库执行只读探测。
        application.state.return_request_repository = runtime.return_request_repository
        # 协调器通过同一个具体仓库的 Outbox 协议读取事务事件。
        application.state.return_outbox_repository = runtime.return_outbox_repository
        # 同一个检索器实例既被图节点调用，也用于证明共享 Qdrant Collection 可读。
        application.state.knowledge_retriever = runtime.knowledge_retriever
        # yield 期间 Uvicorn 才会正式接收 HTTP 请求。
        yield
    # 退出上下文后 SQLite Saver 已关闭，Uvicorn 随后完成进程停止。
    # 尽力刷新 Batch Span 和周期指标；失败不应阻止进程完成关闭。
    force_flush_telemetry()


# 创建整个服务唯一的 FastAPI 应用对象，Uvicorn 会通过模块路径导入该变量。
app = FastAPI(
    # Swagger 页面标题来自配置，可通过 SERVICEOPS_APP_NAME 环境变量覆盖。
    title=settings.app_name,
    # API 版本用于接口文档展示；后续发布时应与项目版本保持同步。
    version="0.1.0",
    # 简短说明当前服务的业务用途和开发阶段。
    description="企业售后工单 Agent 的第一版可运行 API",
    # lifespan 负责持久化资源的启动失败、共享和优雅关闭。
    lifespan=lifespan,
)

# 把受控前端资源挂载到固定前缀；html 首页仍由显式路由添加安全响应头。
app.mount(
    "/console/assets",
    StaticFiles(directory=CONSOLE_ASSET_DIRECTORY),
    name="agent-console-assets",
)


@app.middleware("http")
async def add_instance_diagnostic_header(
    request: Request,
    call_next: RequestResponseEndpoint,
) -> Response:
    """为所有正常 HTTP 响应增加低敏实例标识，验证请求是否跨副本分流。"""

    # 不读取请求正文；只把标准 Request 继续交给认证、路由、LangGraph 或健康检查。
    response = await call_next(request)
    # 固定配置值不会包含用户输入；Nginx 和验证脚本可据此观察实际上游实例。
    response.headers["X-ServiceOps-Instance"] = settings.instance_id
    # 不修改状态码和正文，返回原业务响应。
    return response


@app.middleware("http")
async def protect_agent_capacity(
    request: Request,
    call_next: RequestResponseEndpoint,
) -> Response:
    """限制单实例高成本业务并发，健康检查和接口文档保持可访问。"""

    # 只有 /api/ 业务路径会执行 Agent、审批、审计或补偿；系统探针与 Swagger 不占工位。
    if not request.url.path.startswith("/api/") or request.method == "OPTIONS":
        return await call_next(request)
    # 从 app.state 取得当前 lifespan 创建的信号量；轻量测试使用模块初始化的后备实例。
    limiter = cast(asyncio.BoundedSemaphore, request.app.state.agent_capacity_limiter)
    try:
        # 工位全满时只允许短暂排队，防止大量请求在 Python 内存中无限等待。
        await asyncio.wait_for(
            limiter.acquire(),
            timeout=settings.agent_capacity_queue_timeout_seconds,
        )
    except TimeoutError:
        # 记录固定低基数拒绝指标，不记录来源 IP、URL 参数、用户或请求正文。
        record_capacity_rejection()
        # Retry-After=1 告诉调用方至少等待一秒；固定正文不泄漏当前并发数和内部结构。
        return JSONResponse(
            status_code=503,
            content={"detail": "服务当前繁忙，请稍后重试"},
            headers={
                "Retry-After": "1",
                "X-ServiceOps-Overload": "capacity",
                # 快速返回没有继续经过外层实例中间件，因此在这里显式保留低敏实例定位。
                "X-ServiceOps-Instance": settings.instance_id,
            },
        )
    try:
        # 成功占用工位后再进入 JWT、路由、LangGraph 和数据库调用链。
        return await call_next(request)
    finally:
        # 无论成功、业务 4xx 或异常都归还工位，避免一次故障永久吃掉容量。
        limiter.release()


# 启用时自动创建 HTTP Server Span；健康/文档路径不产生无价值遥测噪声。
if telemetry_runtime is not None:
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=telemetry_runtime.tracer_provider,
        meter_provider=telemetry_runtime.meter_provider,
        excluded_urls="health,ready,docs,openapi.json",
        # receive/send 内部 ASGI Span 数量大且业务价值低，当前只保留 Server Span。
        exclude_spans=["receive", "send"],
    )
    # 只记录标准 HTTP 元数据和耗时；未配置任何请求/响应 Header 或 Body 捕获。
    HTTPXClientInstrumentor().instrument(
        tracer_provider=telemetry_runtime.tracer_provider,
    )

# HTTPX 的轻量 ASGITransport 默认不触发 lifespan；该内存后备用于现有进程内 API 单测。
app.state.service_graph = fallback_service_graph
# 后备图的实际持久化后端是 memory，Uvicorn 启动后 lifespan 会覆盖该字段。
app.state.persistence_backend = "memory"
# 轻量 ASGITransport 测试未进入 lifespan 时使用独立进程内审计仓库。
app.state.approval_audit_repository = InMemoryApprovalAuditRepository()
# 无 lifespan 的轻量测试使用现有默认内存退货仓库完成 readiness。
app.state.return_request_repository = default_return_request_repository
# 默认内存退货仓库同时实现 Outbox 协议，支持未进入 lifespan 的轻量测试。
app.state.return_outbox_repository = default_return_request_repository
# 不进入 lifespan 的轻量 ASGI 单测没有外部 Qdrant 生命周期，使用 None 明确标记后备模式。
app.state.knowledge_retriever = None
# 未进入 lifespan 的 ASGITransport 单测仍使用相同默认容量语义。
app.state.agent_capacity_limiter = asyncio.BoundedSemaphore(settings.agent_max_in_flight_requests)


def _console_security_headers() -> dict[str, str]:
    """返回 Agent 控制台所需的固定浏览器安全策略。"""

    # CSP 只允许同源 CSS、JavaScript 和 API 请求，禁止内联脚本、第三方资源与 iframe 嵌入。
    content_security_policy = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )
    # 所有值都是代码内固定常量，不读取请求 Header 或用户输入。
    return {
        # no-store 防止共享电脑缓存可能曾显示审批信息的 HTML 文档。
        "Cache-Control": "no-store",
        # CSP 是控制台最主要的前端注入与第三方外连边界。
        "Content-Security-Policy": content_security_policy,
        # nosniff 防止浏览器把非脚本资源猜测成 JavaScript 执行。
        "X-Content-Type-Options": "nosniff",
        # DENY 与 CSP frame-ancestors 双重防止点击劫持。
        "X-Frame-Options": "DENY",
        # 控制台不需要把当前路径作为 Referer 发送给外部站点。
        "Referrer-Policy": "no-referrer",
    }


# 根路径只负责把浏览器引导到产品控制台；健康检查和 Swagger 地址保持不变。
@app.get("/", include_in_schema=False)
async def redirect_to_agent_console() -> RedirectResponse:
    """把服务根路径重定向到带结尾斜杠的控制台地址。"""

    # 307 保留 HTTP 方法语义；普通浏览器 GET 会自然进入 /console/。
    return RedirectResponse(url="/console/", status_code=307)


# 没有结尾斜杠时统一跳转，确保相对 assets URL 始终解析到 /console/assets。
@app.get("/console", include_in_schema=False)
async def redirect_to_canonical_console_url() -> RedirectResponse:
    """规范化控制台 URL，避免静态资源相对路径发生歧义。"""

    # RedirectResponse 不包含 Token 或用户参数。
    return RedirectResponse(url="/console/", status_code=307)


# 产品控制台不是业务 API，因此不进入 Swagger 路由列表。
@app.get("/console/", include_in_schema=False)
async def agent_console() -> FileResponse:
    """返回只使用同源静态资源和现有受保护 API 的 Agent 控制台。"""

    # index.html 随 wheel 发布且不在运行期修改，适合只读容器根文件系统。
    return FileResponse(
        CONSOLE_DIRECTORY / "index.html",
        # 显式声明 UTF-8，保证 Windows、Linux 和浏览器一致显示中文。
        media_type="text/html; charset=utf-8",
        # 安全策略只加在 HTML 文档；静态资源仍由同源与 MIME 类型约束。
        headers=_console_security_headers(),
    )


# 注册 GET /health，并声明响应必须符合 HealthResponse；标签用于 Swagger 分组。
@app.get("/health", response_model=HealthResponse, tags=["system"])
async def health_check() -> HealthResponse:
    """提供给开发者和后续容器编排平台的存活检查接口。"""

    # 构造经过 Pydantic 校验的健康响应，同时返回当前环境以便排查部署配置。
    return HealthResponse(
        # 存活状态固定为 ok。
        status="ok",
        # 返回当前进程的低敏稳定标识，帮助验证 Nginx 轮询分流。
        instance_id=settings.instance_id,
        # 返回当前环境配置。
        environment=settings.environment,
        # 读取 lifespan 实际装配结果；轻量测试后备明确返回 memory。
        persistence_backend=app.state.persistence_backend,
    )


def _get_service_graph() -> ServiceGraph:
    """从 FastAPI 应用状态取得与当前 lifespan 绑定的可执行图。"""

    # Starlette State 是动态属性容器，通过 cast 恢复 PyCharm 和 Mypy 可识别的图类型。
    return cast(ServiceGraph, app.state.service_graph)


def _get_approval_audit_repository() -> ApprovalAuditRepository:
    """从 FastAPI 应用状态取得与当前持久化后端匹配的审计仓库。"""

    # Starlette State 是动态属性容器，通过 cast 恢复静态仓库协议。
    return cast(
        ApprovalAuditRepository,
        app.state.approval_audit_repository,
    )


def _get_return_request_repository() -> ReturnRequestRepository:
    """取得当前 lifespan 绑定的退货业务仓库，供 readiness 执行只读探测。"""

    # 普通业务路由仍通过图调用仓库；这里只暴露最小 count() 健康查询。
    return cast(
        ReturnRequestRepository,
        app.state.return_request_repository,
    )


def _get_return_outbox_repository() -> ReturnOutboxRepository:
    """取得与退货业务记录共享原子提交边界的 Outbox 仓库。"""

    # 具体内存/SQLite/PostgreSQL 类同时实现两个协议，这里只暴露事件相关方法。
    return cast(
        ReturnOutboxRepository,
        app.state.return_outbox_repository,
    )


def _get_knowledge_retriever() -> HealthCheckableKnowledgeRetriever | None:
    """取得 lifespan 装配的共享知识检索器；None 只可能出现在无 lifespan 轻量单测。"""

    # Starlette State 是动态属性容器，cast 只恢复静态类型，不会创建新客户端。
    return cast(
        HealthCheckableKnowledgeRetriever | None,
        app.state.knowledge_retriever,
    )


def _build_approval_audit_draft(
    *,
    thread_id: str,
    event_type: ApprovalAuditEventType,
    request_id: str,
    principal: AuthenticatedPrincipal,
    decision: ApprovalDecision,
    proposal: ReturnRequestProposal,
    return_request_id: str | None = None,
) -> ApprovalAuditDraft:
    """从可信 Principal、Checkpoint 草案和决定构造最小化审计事件。"""

    # 只保存字段摘要，不把申请原因、幂等键或审批备注原文复制进审计库。
    return ApprovalAuditDraft(
        # 关联可恢复状态图线程。
        thread_id=thread_id,
        # 区分决定、成功、拒绝和失败事件。
        event_type=event_type,
        # request_id 来自服务端创建的原始 Checkpoint State。
        request_id=request_id,
        # actor_id 只来自验签后的 JWT sub。
        actor_id=principal.subject,
        # 只记录 Token 唯一编号，不记录完整 Token 或签名密钥。
        token_jti=principal.token_id,
        # 严格布尔决定由 ApprovalDecision 再次校验。
        approved=decision.approved,
        # 订单号来自 Checkpoint 中经过领域校验的草案。
        order_id=proposal.order_id,
        # 完整草案被压缩为不可逆 SHA-256 摘要。
        proposal_digest=build_proposal_digest(proposal),
        # 审批备注规范化后只保存摘要。
        comment_digest=build_comment_digest(decision.comment),
        # 只有成功结果事件携带业务申请号。
        return_request_id=return_request_id,
    )


def _build_thread_config(thread_id: str) -> RunnableConfig:
    """为一次可恢复图执行构造稳定的 LangGraph 配置。"""

    # Checkpointer 使用 configurable 命名空间；相同 thread_id 才能读取同一状态快照。
    return {"configurable": {"thread_id": thread_id}}


# 教学调试接口只在 development/test 的 OpenAPI 中出现；production 即使误注册也会返回 404。
@app.get(
    "/api/v1/debug/threads/{thread_id}",
    response_model=ThreadDebugResponse,
    tags=["development"],
    include_in_schema=settings.environment != "production",
)
async def get_thread_debug_trace(
    thread_id: UUID,
    principal: Annotated[
        AuthenticatedPrincipal,
        Security(require_principal, scopes=[PermissionScope.DEBUG_READ.value]),
    ],
) -> ThreadDebugResponse:
    """返回一个线程经过脱敏的状态与 Checkpoint 教学回放。"""

    # production 必须没有可用调试能力；404 比 403 更少泄漏内部路由设计。
    if settings.environment == "production":
        raise HTTPException(status_code=404, detail="未找到资源")
    # Security 依赖已经验证 developer Token；路由本身不记录或返回调试者身份。
    _ = principal
    stable_thread_id = str(thread_id)
    # 官方 aget_state_history 返回“最新在前”，多取一条用于判断是否发生截断。
    snapshots = []
    async for snapshot in _get_service_graph().aget_state_history(
        _build_thread_config(stable_thread_id),
        limit=MAX_DEBUG_CHECKPOINTS + 1,
    ):
        snapshots.append(snapshot)
    # 空历史统一返回 404，不区分线程从未存在还是已经被清理。
    if not snapshots:
        raise HTTPException(status_code=404, detail="未找到对应的调试线程")
    truncated = len(snapshots) > MAX_DEBUG_CHECKPOINTS
    # 保留最新的上限数量；响应内再反转为最早到最晚，方便单步播放。
    selected_snapshots = snapshots[:MAX_DEBUG_CHECKPOINTS]
    return build_thread_debug_response(
        thread_id=stable_thread_id,
        newest_first_snapshots=selected_snapshots,
        truncated=truncated,
    )


# readiness 会真实读取五个关键持久化边界；失败时返回 503 而 liveness 仍保持 200。
@app.get("/ready", response_model=ReadinessResponse, tags=["system"])
async def readiness_check(response: Response) -> ReadinessResponse:
    """验证 Checkpointer、业务仓库、Outbox、审计仓库和 Qdrant 是否可读。"""

    # 固定组件名形成低基数响应；每项初始为 not_ready，只有探测成功才改为 ready。
    checks = {
        "checkpointer": DependencyCheck(status="not_ready"),
        "return_repository": DependencyCheck(status="not_ready"),
        "outbox_repository": DependencyCheck(status="not_ready"),
        "audit_repository": DependencyCheck(status="not_ready"),
        "knowledge_qdrant": DependencyCheck(status="not_ready"),
    }
    try:
        # aget_state 使用固定探针线程只读 Checkpoint，不创建用户业务状态。
        await _get_service_graph().aget_state(
            _build_thread_config("__serviceops_readiness_probe__")
        )
        # 只要查询没有异常，就说明 Saver 连接和必要表可读。
        checks["checkpointer"] = DependencyCheck(status="ready")
    except Exception as error:
        # 日志只记录异常类型，不记录数据库路径、SQL 或异常消息。
        logger.warning(
            "readiness Checkpointer 探测失败: cause_type=%s",
            type(error).__name__,
            extra={"operation": "readiness", "failure_code": "checkpointer"},
        )
    try:
        # count() 对内存字典或 SQLite return_requests 表执行真实读取。
        _get_return_request_repository().count()
        checks["return_repository"] = DependencyCheck(status="ready")
    except Exception as error:
        logger.warning(
            "readiness 退货仓库探测失败: cause_type=%s",
            type(error).__name__,
            extra={"operation": "readiness", "failure_code": "return_repository"},
        )
    try:
        # backlog 数量不影响就绪；只要 count 查询可执行，就证明 Outbox 表/锁可用。
        _get_return_outbox_repository().count_outbox()
        checks["outbox_repository"] = DependencyCheck(status="ready")
    except Exception as error:
        logger.warning(
            "readiness Outbox 仓库探测失败: cause_type=%s",
            type(error).__name__,
            extra={"operation": "readiness", "failure_code": "outbox_repository"},
        )
    try:
        # 固定不存在的线程查询不会暴露业务数据，但会验证审计表和连接可读。
        _get_approval_audit_repository().list_for_thread("__serviceops_readiness_probe__")
        checks["audit_repository"] = DependencyCheck(status="ready")
    except Exception as error:
        logger.warning(
            "readiness 审计仓库探测失败: cause_type=%s",
            type(error).__name__,
            extra={"operation": "readiness", "failure_code": "audit_repository"},
        )
    try:
        # Uvicorn 正常启动时对同一个 Qdrant Collection 执行只读元数据查询。
        knowledge_retriever = _get_knowledge_retriever()
        # None 仅用于 HTTPX 不触发 lifespan 的轻量测试，不代表真实部署绕过探测。
        if knowledge_retriever is not None:
            knowledge_retriever.health_check()
        # 没有异常说明远程服务、鉴权和活动 Collection 均可访问。
        checks["knowledge_qdrant"] = DependencyCheck(status="ready")
    except Exception as error:
        # 日志只记录异常类型和有限故障码，不暴露 Qdrant URL、密钥或响应正文。
        logger.warning(
            "readiness Qdrant 知识索引探测失败: cause_type=%s",
            type(error).__name__,
            extra={"operation": "readiness", "failure_code": "knowledge_qdrant"},
        )

    # 只有所有必需依赖均为 ready 才接收 Agent 流量。
    is_ready = all(check.status == "ready" for check in checks.values())
    # 依赖异常时使用标准 503，便于负载均衡器摘除实例。
    if not is_ready:
        response.status_code = 503
    # 不返回异常正文或路径，只公开有限状态和实际后端类型。
    return ReadinessResponse(
        status="ready" if is_ready else "not_ready",
        # 与响应 Header 使用同一配置值，方便人工在浏览器直接查看实例。
        instance_id=settings.instance_id,
        checks=checks,
        persistence_backend=app.state.persistence_backend,
        telemetry_exporter=(
            settings.telemetry_exporter if telemetry_runtime is not None else "disabled"
        ),
    )


def _extract_approval_request(result: dict[str, Any]) -> ApprovalRequestPayload | None:
    """从 LangGraph 框架字段提取并重新校验安全审批负载。"""

    # interrupt 列表由框架写入，不属于业务 ServiceState 的普通字段。
    raw_interrupts = result.get("__interrupt__")
    # 没有中断表示图已经运行到某个终点。
    if not isinstance(raw_interrupts, (list, tuple)) or not raw_interrupts:
        # 调用方据此构造 completed 响应。
        return None
    # 当前图一次只允许一个退货审批中断，因此读取第一个 Interrupt 对象。
    first_interrupt = raw_interrupts[0]
    # Interrupt.value 是节点传给 interrupt(...) 的最小 JSON 负载。
    raw_payload = getattr(first_interrupt, "value", None)
    try:
        # 即使负载由内部节点生成，API 边界仍再次执行 Schema 校验。
        return ApprovalRequestPayload.model_validate(raw_payload)
    # 快照损坏或未来错误节点产生未知负载时，不能伪装成普通完成响应。
    except Exception as error:
        # 对客户端只返回固定错误，不泄漏内部快照内容。
        raise HTTPException(status_code=500, detail="审批中断负载校验失败") from error


def _build_chat_response(result: dict[str, Any], *, thread_id: str) -> ChatResponse:
    """把普通终态或 interrupt 暂停态转换为同一份稳定响应模型。"""

    # 如果存在审批负载，说明图停在写工具之前。
    approval_request = _extract_approval_request(result)
    # 暂停态没有业务终点 answer，因此由 API 返回固定等待审批说明。
    answer = (
        "退货申请草案已生成，当前等待人工审批，尚未创建退货申请。"
        if approval_request is not None
        else result["answer"]
    )
    # 把内部状态中允许对外暴露的字段组装成稳定响应，避免返回整份 Checkpoint。
    return ChatResponse(
        # request_id 来自初次启动状态，恢复后仍保持不变。
        request_id=result["request_id"],
        # thread_id 由 API 路径上下文提供，不接受审批恢复值覆盖。
        thread_id=thread_id,
        # 当前 HTTP/业务 Span 的 Trace ID 供客户端反馈故障时关联；关闭遥测则为 None。
        trace_id=current_trace_id(),
        # 有中断时明确告诉调用端后续要调用审批接口。
        execution_status=("approval_required" if approval_request is not None else "completed"),
        # 分类节点产生的 Intent 会被 Pydantic 序列化成有限字符串。
        intent=result["intent"],
        # 分类置信度来自关键词基线或真实结构化模型输出。
        intent_confidence=result["intent_confidence"],
        # 路由原因帮助开发者理解为什么进入当前分支。
        route_reason=result["route_reason"],
        # 普通终态返回节点 answer；中断态使用固定等待审批文案。
        answer=answer,
        # 审批中断需要人工决定，普通路径读取图的最终人工标记。
        requires_human=(True if approval_request is not None else result["requires_human"]),
        # 信息不足但仍可自动继续时，调用方应提示用户补充参数。
        needs_clarification=result.get("needs_clarification", False),
        # 非订单路径不会产生 order_id，因此使用 get 安全读取可选字段。
        order_id=result.get("order_id"),
        # 只有真正执行工具的路径才返回工具名。
        tool_name=result.get("tool_name"),
        # 多订单 Agent 路径按工具实际执行顺序返回全部规范订单号。
        queried_order_ids=result.get("queried_order_ids", []),
        # 没有进入工具路径时默认为零。
        tool_call_count=result.get("tool_call_count", 0),
        # FAQ 等非工具路径没有 Agent 停止原因，因此允许为 None。
        agent_stop_reason=result.get("agent_stop_reason"),
        # FAQ 证据引用是强类型 Citation 列表；其他路径使用空列表。
        citations=result.get("citations", []),
        # 只有 FAQ 检索路径会产生最高相似度分数。
        retrieval_score=result.get("retrieval_score"),
        # 是否等待审批以框架实际 interrupt 为准，不信任普通请求字段。
        approval_required=approval_request is not None,
        # 只返回经过 Schema 校验的最小审批负载。
        approval_request=approval_request,
        # 退货路径返回有限流程状态；其他路径为 None。
        return_workflow_status=result.get("return_workflow_status"),
        # 只有批准写入成功后才有申请编号。
        return_request_id=result.get("return_request_id"),
        # 返回执行事件便于当前教学调试；生产 API 可能改为只写内部 Trace。
        events=result.get("events", []),
    )


# 注册工单入口；FastAPI 会先把 JSON 请求体校验并转换为 ChatRequest。
@app.post("/api/v1/chat", response_model=ChatResponse, tags=["agent"])
async def chat(
    request: ChatRequest,
    principal: Annotated[
        AuthenticatedPrincipal,
        Security(require_principal, scopes=[PermissionScope.AGENT_CHAT.value]),
    ],
) -> ChatResponse:
    """把一条用户工单交给 LangGraph 执行。

    这里使用 `ainvoke`，为后续异步模型请求、数据库访问和流式输出预留空间。
    第一版图没有外部 I/O，但从一开始保持异步 API 可以减少后续接口改造。
    """

    # 为本次请求生成唯一 UUID，并转为便于 JSON 序列化和日志记录的字符串。
    request_id = str(uuid4())
    # thread_id 是 Checkpointer 快照主键；与 request_id 分开表达恢复和链路两个概念。
    thread_id = str(uuid4())
    # 为本次首次运行和后续审批恢复创建相同配置。
    graph_config = _build_thread_config(thread_id)
    # 异步执行状态图；未来节点进行模型和数据库 I/O 时不会阻塞整个事件循环。
    # 获取 lifespan 内已经绑定正确 Checkpointer 的共享图。
    graph = _get_service_graph()
    # 手工业务 Span 成为全部 LangGraph 节点 Span 的父节点。
    with start_safe_span(
        "serviceops.agent.chat",
        attributes={
            # request/thread 标识只进入 Trace，不进入低基数 Metrics。
            "serviceops.request.id": request_id,
            "serviceops.thread.id": thread_id,
            # operation 是代码内固定有限值。
            "serviceops.operation": "chat",
        },
    ):
        # 使用单调时钟计算图端到端耗时。
        started_at = perf_counter()
        result = await graph.ainvoke(
            # 该字典是状态图的初始 ServiceState，后续节点会增量写入其他字段。
            {
                # 请求标识由 API 生成，贯穿本次图执行和最终响应。
                "request_id": request_id,
                # 用户标识只来自通过签名、iss/aud/exp 和 Scope 校验的 JWT sub。
                "user_id": principal.subject,
                # 用户原始问题只进入业务 State，不进入 Trace、Metrics 或结构化日志。
                "user_message": request.message,
                # 客户端提供则支持跨 HTTP 重试；否则使用本次 request_id 作为唯一幂等键。
                "idempotency_key": request.idempotency_key or request_id,
                # 事件记录认证已通过，但不会写入 Token 原文或 jti。
                "events": ["api:authenticated_request_received"],
            },
            # 启用 Checkpointer 的图必须传 thread_id，即使本次走的是只读路径。
            config=graph_config,
        )
        # 只使用有限 intent/outcome/tool 标签记录计数与耗时。
        record_agent_execution(
            operation="chat",
            result=result,
            duration_ms=(perf_counter() - started_at) * 1_000,
        )
        # 普通完成和等待审批共用响应构造器；此时仍处于有效业务 Span 内。
        return _build_chat_response(result, thread_id=thread_id)


# 审批接口只恢复既有线程，不接受重新提交自然语言请求或身份字段。
@app.post(
    "/api/v1/approvals/{thread_id}",
    response_model=ChatResponse,
    tags=["approval"],
)
async def review_return_request(
    thread_id: UUID,
    request: ApprovalDecisionRequest,
    principal: Annotated[
        AuthenticatedPrincipal,
        Security(require_principal, scopes=[PermissionScope.RETURN_APPROVE.value]),
    ],
) -> ChatResponse:
    """批准或拒绝一个正在等待的退货申请草案，并恢复原 LangGraph 线程。"""

    # UUID 路径参数经过 FastAPI 校验后转回规范字符串，与首次保存的 key 一致。
    stable_thread_id = str(thread_id)
    # 使用完全相同的 thread_id 定位 Checkpointer 中的状态和 interrupt。
    graph_config = _build_thread_config(stable_thread_id)
    # 恢复前先只读快照，区分不存在、已经完成和确实等待审批三种状态。
    # 读取与当前运行时数据库连接绑定的图。
    graph = _get_service_graph()
    snapshot = await graph.aget_state(graph_config)
    # 未知线程不会包含任何业务状态。
    if not snapshot.values:
        # 404 不暴露其他线程的状态内容。
        raise HTTPException(status_code=404, detail="未找到对应的 Agent 线程")
    # 只有暂停在指定审批节点且确实包含 interrupt 时才允许恢复。
    if "request_return_approval" not in snapshot.next or not snapshot.interrupts:
        # 已完成或不是审批流程的线程不能重复执行写操作。
        raise HTTPException(status_code=409, detail="该线程当前不在等待退货审批")

    try:
        # Checkpoint 中的草案包含写工具完整参数，是计算审计摘要的可信来源。
        proposal = ReturnRequestProposal.model_validate(
            snapshot.values.get("return_request_proposal")
        )
        # request_id 必须继续满足 API 首次生成时的非空字符串约束。
        original_request_id = str(snapshot.values["request_id"])
        # 防止损坏快照把 None 或空字符串静默转换为可用标识。
        if not original_request_id or original_request_id == "None":
            raise ValueError("Checkpoint 缺少 request_id")
    # 损坏或被错误迁移的快照不能进入审批恢复和业务写工具。
    except Exception as error:
        # 客户端只收到固定故障信息，不泄漏 Checkpoint 原始字段。
        raise HTTPException(
            status_code=500,
            detail="待审批草案校验失败",
        ) from error

    # 审批人身份来自有 return:approve 权限的 JWT sub，不能由请求 JSON 伪造。
    decision = ApprovalDecision(
        # 严格布尔决定来自 API Schema。
        approved=request.approved,
        # reviewer_id 使用已认证主体。
        reviewer_id=principal.subject,
        # 备注仍来自有长度约束的请求字段。
        comment=request.comment,
        # thread_id 来自经过 UUID 校验的 API 路径，不接受请求体提交。
        thread_id=stable_thread_id,
        # token_jti 来自验签后的 Principal，不保存完整 Authorization Header。
        token_jti=principal.token_id,
    )

    # 审计决定必须先于恢复动作追加；中途崩溃时可以发现“有决定、无结果”的待核查线程。
    decision_audit_draft = _build_approval_audit_draft(
        thread_id=stable_thread_id,
        event_type=ApprovalAuditEventType.DECISION_RECORDED,
        request_id=original_request_id,
        principal=principal,
        decision=decision,
        proposal=proposal,
    )
    # 使用与当前 lifespan 后端一致的审计仓库。
    audit_repository = _get_approval_audit_repository()
    try:
        # 独立 Span 展示“授权证据先落库”，不记录备注、原因或 Token 原文。
        with start_safe_span(
            "serviceops.audit.append_decision",
            attributes={
                "serviceops.request.id": original_request_id,
                "serviceops.thread.id": stable_thread_id,
                "serviceops.audit.event_type": decision_audit_draft.event_type.value,
                "serviceops.approval.approved": decision.approved,
            },
        ):
            # 同主体、决定、草案和备注的重复追加会安全返回原事件。
            audit_repository.append(decision_audit_draft)
    # 同一线程试图更换主体、决定或备注时不能恢复原 interrupt。
    except ApprovalAuditConflictError as error:
        raise HTTPException(
            status_code=409,
            detail="该线程已经记录了不同的审批决定",
        ) from error

    try:
        # 审批恢复业务 Span 成为重放节点和后续写工具节点 Span 的父节点。
        with start_safe_span(
            "serviceops.agent.approval_resume",
            attributes={
                "serviceops.request.id": original_request_id,
                "serviceops.thread.id": stable_thread_id,
                "serviceops.operation": "approval",
                "serviceops.approval.approved": decision.approved,
            },
        ):
            # 使用单调时钟测量恢复图耗时。
            approval_started_at = perf_counter()
            # Command.resume 会让 request_return_approval 节点从头重放并取得该决定。
            result = await graph.ainvoke(
                # 恢复值只含批准布尔值、审批人和备注，不允许携带用户身份或工具参数。
                Command(resume=decision.model_dump(mode="json")),
                # 同一个配置保证恢复原草案、可信身份和幂等键。
                config=graph_config,
            )
            # 与 chat 共用有限 intent/outcome/tool 业务指标。
            record_agent_execution(
                operation="approval",
                result=result,
                duration_ms=(perf_counter() - approval_started_at) * 1_000,
            )
    except Exception:
        # 恢复或写工具抛出异常时追加失败终态，保留已提交决定的完整因果关系。
        failure_audit_draft = _build_approval_audit_draft(
            thread_id=stable_thread_id,
            event_type=ApprovalAuditEventType.WORKFLOW_FAILED,
            request_id=original_request_id,
            principal=principal,
            decision=decision,
            proposal=proposal,
        )
        # 如果失败事件自身无法落库，应让异常继续暴露，不能伪装成成功响应。
        with start_safe_span(
            "serviceops.audit.append_failure",
            attributes={
                "serviceops.request.id": original_request_id,
                "serviceops.thread.id": stable_thread_id,
                "serviceops.audit.event_type": failure_audit_draft.event_type.value,
            },
        ):
            audit_repository.append(failure_audit_draft)
        # 保留原始异常链交给 FastAPI 的统一错误处理和服务端日志。
        raise

    # 读取图的有限流程状态，不能仅根据 HTTP 请求中的 approved 猜测执行结果。
    final_workflow_status = ReturnWorkflowStatus(
        result.get("return_workflow_status", ReturnWorkflowStatus.FAILED.value)
    )
    # 正常批准且工具成功时由事务 Outbox 追加 completed，避免业务/审计双写窗口。
    if final_workflow_status == ReturnWorkflowStatus.COMPLETED:
        # 同步尽力投递当前线程，降低正常请求看到 pending 的延迟；失败事件仍留在 Outbox。
        reconciliation_result = ReturnOutboxReconciler(
            outbox_repository=_get_return_outbox_repository(),
            audit_repository=audit_repository,
        ).reconcile(thread_id=stable_thread_id, limit=1)
        # 生产 API 完成态必须至少扫描到本事务创建的事件；异常只记录稳定标识供运维补偿。
        if reconciliation_result.scanned == 0:
            logger.error(
                "完成态未扫描到对应 Outbox 事件: thread_id=%s",
                stable_thread_id,
                extra={"operation": "outbox_dispatch", "failure_code": "event_missing"},
            )
        # 完成事件已经由协调器构造和追加，不能再走下面的直接双写路径。
        outcome_audit_draft = None
    # 人工拒绝不会执行写工具，对应独立 rejected 事件。
    elif final_workflow_status == ReturnWorkflowStatus.REJECTED:
        outcome_audit_draft = _build_approval_audit_draft(
            thread_id=stable_thread_id,
            event_type=ApprovalAuditEventType.WORKFLOW_REJECTED,
            request_id=original_request_id,
            principal=principal,
            decision=decision,
            proposal=proposal,
        )
    else:
        # 工具安全失败等其他有限状态统一记录为 workflow_failed。
        outcome_audit_draft = _build_approval_audit_draft(
            thread_id=stable_thread_id,
            event_type=ApprovalAuditEventType.WORKFLOW_FAILED,
            request_id=original_request_id,
            principal=principal,
            decision=decision,
            proposal=proposal,
        )

    # 拒绝/失败没有业务提交双写问题，仍直接追加对应终态；完成态由 Outbox 负责。
    if outcome_audit_draft is not None:
        with start_safe_span(
            "serviceops.audit.append_outcome",
            attributes={
                "serviceops.request.id": original_request_id,
                "serviceops.thread.id": stable_thread_id,
                "serviceops.audit.event_type": outcome_audit_draft.event_type.value,
            },
        ):
            audit_repository.append(outcome_audit_draft)
    # 审批计数只使用 approved 与有限终态，不使用审批人或线程标签。
    record_approval_execution(
        approved=decision.approved,
        outcome=final_workflow_status.value,
    )
    # 批准会返回创建结果，拒绝会返回零写入结果；两者使用同一响应契约。
    return _build_chat_response(result, thread_id=stable_thread_id)


# 运维补偿接口与审批/审计接口职责分离，只允许推进 pending Outbox 状态。
@app.post(
    "/api/v1/internal/outbox/reconcile",
    response_model=ReconciliationBatchResult,
    tags=["operations"],
)
async def reconcile_return_outbox(
    principal: Annotated[
        AuthenticatedPrincipal,
        Security(
            require_principal,
            scopes=[PermissionScope.OUTBOX_RECONCILE.value],
        ),
    ],
    limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
) -> ReconciliationBatchResult:
    """由独立运维主体触发一批到期 Outbox 事件补偿。"""

    # principal 只用于完成 FastAPI Scope 校验，不进入指标、日志或事件业务载荷。
    _ = principal
    # 协调器对单条事件隔离失败，并返回不包含任何业务原文的有限统计。
    with start_safe_span(
        "serviceops.outbox.reconcile",
        attributes={"serviceops.outbox.batch_limit": limit},
    ):
        return ReturnOutboxReconciler(
            outbox_repository=_get_return_outbox_repository(),
            audit_repository=_get_approval_audit_repository(),
        ).reconcile(limit=limit)


# 审计查询与审批接口职责分离，只有 audit:read 主体可以读取可信身份和 Token jti。
@app.get(
    "/api/v1/audit/approvals/{thread_id}",
    response_model=ApprovalAuditTrailResponse,
    tags=["audit"],
)
async def get_approval_audit_trail(
    thread_id: UUID,
    principal: Annotated[
        AuthenticatedPrincipal,
        Security(require_principal, scopes=[PermissionScope.AUDIT_READ.value]),
    ],
) -> ApprovalAuditTrailResponse:
    """按 LangGraph 线程读取审批证据链，并重新计算完整性结果。"""

    # UUID 转成规范字符串，与审批写入时使用的 thread_id 完全一致。
    stable_thread_id = str(thread_id)
    # 获取当前 lifespan 装配的内存、SQLite 或 PostgreSQL 审计仓库。
    audit_repository = _get_approval_audit_repository()
    # 审计读取 Span 不记录 auditor_id、Token 或事件正文，只关联目标线程。
    with start_safe_span(
        "serviceops.audit.read_chain",
        attributes={"serviceops.thread.id": stable_thread_id},
    ):
        # 按 chain_position 升序读取最小化事件。
        events = audit_repository.list_for_thread(stable_thread_id)
        # 没有事件的线程统一返回 404，不泄漏它是否存在普通 Agent Checkpoint。
        if not events:
            raise HTTPException(status_code=404, detail="未找到对应的审批审计记录")
        # 每次读取都重算位置、前驱引用和 SHA-256，而不是直接相信数据库字段。
        chain_valid = audit_repository.verify_thread_chain(stable_thread_id)
    # 审计日志的读取本身也留下服务日志，只记录稳定标识，不记录事件正文或 Token。
    logger.info(
        "审批审计链已读取: chain_valid=%s event_count=%s",
        chain_valid,
        len(events),
        extra={
            "thread_id": stable_thread_id,
            "operation": "audit_read",
            "outcome": "valid" if chain_valid else "invalid",
        },
    )
    # 返回强类型响应；仅 audit:read 路由会暴露 actor_id 和 token_jti。
    return ApprovalAuditTrailResponse(
        thread_id=stable_thread_id,
        chain_valid=chain_valid,
        events=events,
    )

"""FastAPI 边界测试。"""

# asyncio.BoundedSemaphore 用于确定性占满后端工位，验证快速过载响应。
import asyncio

# pytest 提供 asyncio 测试标记，使测试函数能够 await 异步 HTTP 客户端。
import pytest

# ASGITransport 在进程内调用 FastAPI；AsyncClient 模拟真实异步 HTTP 客户端。
from httpx import ASGITransport, AsyncClient

# app 是测试目标 FastAPI 应用，包含健康检查和工单接口。
from serviceops_agent.api.app import app

# 集中配置提供测试专用 JWT 密钥、issuer 和 audience。
from serviceops_agent.config.settings import get_settings

# Token 工厂为进程内 API 测试签发真实可验证的短期 Bearer Token。
from serviceops_agent.security.jwt_auth import create_access_token

# Role 决定测试 Token 根据服务端策略获得对话或审批 Scope。
from serviceops_agent.security.models import Role


def _authorization_headers(*, subject: str, role: Role) -> dict[str, str]:
    """为指定测试身份生成标准 Authorization Header。"""

    # 使用与被测 API 完全相同的 Settings 签发 Token。
    token = create_access_token(
        settings=get_settings(),
        subject=subject,
        roles={role},
    )
    # HTTP Bearer 约定使用 `Bearer + 空格 + JWT`。
    return {"Authorization": f"Bearer {token}"}


def _customer_headers(subject: str = "user-001") -> dict[str, str]:
    """生成具有 agent:chat 权限的普通用户 Header。"""

    # CUSTOMER 角色不会获得 return:approve。
    return _authorization_headers(subject=subject, role=Role.CUSTOMER)


def _reviewer_headers(subject: str = "reviewer-001") -> dict[str, str]:
    """生成具有 return:approve 权限的审批人 Header。"""

    # RETURN_REVIEWER 角色不会自动获得普通订单查询权。
    return _authorization_headers(subject=subject, role=Role.RETURN_REVIEWER)


def _auditor_headers(subject: str = "auditor-001") -> dict[str, str]:
    """生成只有 audit:read 权限的独立审计员 Header。"""

    # AUDITOR 遵循职责分离，不自动获得对话或退货审批权限。
    return _authorization_headers(subject=subject, role=Role.AUDITOR)


def _operator_headers(subject: str = "operator-001") -> dict[str, str]:
    """生成只有 operations:reconcile 权限的独立运维员 Header。"""

    # OPERATOR 不能审批或读取审计链，只能触发待处理 Outbox 补偿。
    return _authorization_headers(subject=subject, role=Role.OPERATOR)


# 将同步 pytest 测试切换到事件循环中执行。
@pytest.mark.asyncio
async def test_health_check() -> None:
    """服务启动后应暴露稳定的健康检查接口。"""

    # 同时进入真实 lifespan 和 HTTP 客户端上下文，退出时按相反顺序释放资源。
    async with (
        # 验证真实 Uvicorn 启动时使用的运行时装配代码。
        app.router.lifespan_context(app),
        # ASGITransport 直接调用应用，不占用真实端口，测试速度更快也更稳定。
        AsyncClient(
            # 把 HTTPX 请求直接交给当前 FastAPI ASGI 对象处理。
            transport=ASGITransport(app=app),
            # 提供合法基础 URL；请求不会真正访问这个网络地址。
            base_url="http://testserver",
        ) as client,
    ):
        # Act：向健康检查路径发送一次 GET 请求。
        response = await client.get("/health")

    # Assert：服务正常时必须返回 HTTP 200。
    assert response.status_code == 200
    # Assert：响应 JSON 必须符合 HealthResponse 约定的固定 ok 状态。
    assert response.json()["status"] == "ok"
    # 测试环境默认实例名稳定，响应 Header 与正文必须一致。
    assert response.json()["instance_id"] == "local-1"
    assert response.headers["x-serviceops-instance"] == "local-1"
    # Assert：自动测试强制使用 memory，不能污染开发者 SQLite 运行数据。
    assert response.json()["persistence_backend"] == "memory"


@pytest.mark.asyncio
async def test_capacity_guard_rejects_busy_api_but_keeps_health_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """业务工位耗尽时应返回脱敏 503，而存活探针仍可访问。"""

    # Arrange：创建只有一个工位的信号量，并由测试预先占满它。
    limiter = asyncio.BoundedSemaphore(1)
    await limiter.acquire()
    # 临时替换应用容量边界；monkeypatch 会在测试后恢复原实例。
    monkeypatch.setattr(app.state, "agent_capacity_limiter", limiter)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            # Act：受保护业务路径无法在短排队窗口内取得工位。
            overloaded_response = await client.post(
                "/api/v1/chat",
                json={"message": "查询订单 SO100001"},
                headers=_customer_headers("user-001"),
            )
            # Act：/health 不执行 Agent，不应被业务信号量阻塞。
            health_response = await client.get("/health")
    finally:
        # 即使断言前请求异常，也要归还测试持有的工位，避免污染事件循环资源。
        limiter.release()

    # Assert：业务过载使用 503 + Retry-After，并且不泄漏当前容量数字。
    assert overloaded_response.status_code == 503
    assert overloaded_response.json() == {"detail": "服务当前繁忙，请稍后重试"}
    assert overloaded_response.headers["retry-after"] == "1"
    assert overloaded_response.headers["x-serviceops-overload"] == "capacity"
    # 最外层实例中间件仍应为快速拒绝响应添加低敏实例标识。
    assert overloaded_response.headers["x-serviceops-instance"] == "local-1"
    # 存活探针完全绕过业务容量限制。
    assert health_response.status_code == 200


# readiness 与 liveness 分离，必须真实访问五个运行时持久化边界。
@pytest.mark.asyncio
async def test_readiness_checks_all_runtime_dependencies() -> None:
    """内存运行时的工作流、业务库、Outbox、审计库和知识索引都应可读。"""

    # Arrange：进入真实 lifespan，确保 app.state 使用同一套运行时资源。
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        # Act：readiness 会对四类状态存储和 Qdrant 知识索引执行真实只读探测。
        response = await client.get("/ready")

    # Assert：全部依赖可用时返回 200/ready。
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["checks"] == {
        "checkpointer": {"status": "ready"},
        "return_repository": {"status": "ready"},
        "outbox_repository": {"status": "ready"},
        "audit_repository": {"status": "ready"},
        "knowledge_qdrant": {"status": "ready"},
    }
    # conftest 明确关闭遥测后台线程，因此只公开 disabled。
    assert payload["telemetry_exporter"] == "disabled"


# 模拟单个依赖不可用，验证 readiness 503 和响应脱敏。
@pytest.mark.asyncio
async def test_readiness_returns_sanitized_503_when_repository_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """业务仓库探测异常不能泄漏连接信息，但必须让实例退出就绪状态。"""

    # Arrange：最小故障替身只在 count() 抛出包含敏感文本的异常。
    class FailingReturnRepository:
        """readiness 故障注入替身。"""

        def count(self) -> int:
            """模拟数据库连接失败。"""

            raise RuntimeError("secret-database-path-and-credentials")

    # 临时替换 app.state；monkeypatch 会在测试后自动恢复用户运行时对象。
    monkeypatch.setattr(
        app.state,
        "return_request_repository",
        FailingReturnRepository(),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # Act：执行依赖探测。
        response = await client.get("/ready")

    # Assert：负载均衡器应看到 503。
    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert payload["checks"]["return_repository"] == {"status": "not_ready"}
    # 响应只含有限组件状态，不含异常正文、数据库路径或凭证。
    assert "secret-database-path-and-credentials" not in response.text


# 将工单接口测试放入事件循环，以匹配真实 API 和 LangGraph 的异步调用方式。
@pytest.mark.asyncio
async def test_chat_returns_graph_result() -> None:
    """API 应完成请求校验、图调用与枚举序列化。"""

    # Arrange：创建不会占用真实端口的进程内异步客户端。
    async with AsyncClient(
        # 让请求直接进入被测试的 FastAPI 应用。
        transport=ASGITransport(app=app),
        # HTTPX 需要基础 URL 来拼接下面的相对路径。
        base_url="http://testserver",
    ) as client:
        # Act：提交一条应进入订单路径的合法 JSON 请求。
        response = await client.post(
            # 使用对外稳定的 v1 工单接口路径。
            "/api/v1/chat",
            # json 参数会自动设置 JSON Content-Type 并完成 UTF-8 编码。
            json={"message": "物流到哪了"},
            # demo-user 身份只存在于签名 Token sub，不出现在请求体。
            headers=_customer_headers("demo-user"),
        )

    # 将响应体解析一次并复用，避免每个断言重复进行 JSON 解析。
    payload = response.json()
    # Assert：合法请求应成功完成状态图执行并返回 HTTP 200。
    assert response.status_code == 200
    # Assert：订单关键词必须在外部 JSON 中序列化为 order_status。
    assert payload["intent"] == "order_status"
    # Assert：只读订单查询路径当前无需人工介入。
    assert payload["requires_human"] is False
    # Assert：当前请求缺少订单号，因此 API 应提示调用方继续收集信息。
    assert payload["needs_clarification"] is True
    # Assert：API 必须生成非空 request_id，供链路追踪使用。
    assert payload["request_id"]
    # Assert：新订单 Agent 会显式记录初始化、规划澄清和最终追问，共产生六个事件。
    assert len(payload["events"]) == 6
    # Assert：缺少订单号时不应执行任何工具。
    assert payload["tool_call_count"] == 0
    # Assert：停止原因明确表示等待用户补充参数，而不是系统异常。
    assert payload["agent_stop_reason"] == "needs_clarification"


# 验证 HTTP 边界可以返回真实订单工具查询结果。
@pytest.mark.asyncio
async def test_chat_queries_owned_order_with_read_only_tool() -> None:
    """提供合法订单号时，API 应执行工具并返回安全物流事实。"""

    # Arrange：创建进程内异步客户端，避免占用真实端口。
    async with AsyncClient(
        # 请求直接进入当前 FastAPI 应用。
        transport=ASGITransport(app=app),
        # 基础 URL 只用于 HTTPX 拼接路径。
        base_url="http://testserver",
    ) as client:
        # Act：user-001 查询属于自己的 SO100001。
        response = await client.post(
            # 使用对外 v1 工单接口。
            "/api/v1/chat",
            # JSON 只包含业务文本。
            json={"message": "查询订单 SO100001 到哪了"},
            # user-001 从经过验证的 JWT sub 注入图 State。
            headers=_customer_headers("user-001"),
        )

    # 解析 API JSON 响应。
    payload = response.json()
    # Assert：工具路径正常完成时返回 HTTP 200。
    assert response.status_code == 200
    # Assert：API 回显规范化订单号。
    assert payload["order_id"] == "SO100001"
    # Assert：API 公开实际工具名，便于当前学习阶段观察调用链。
    assert payload["tool_name"] == "get_order_status"
    # Assert：用户可见回答包含仓库中的真实承运商。
    assert "顺丰速运" in payload["answer"]
    # Assert：查询成功后不再需要用户补充信息。
    assert payload["needs_clarification"] is False
    # Assert：单订单请求只执行一次工具。
    assert payload["tool_call_count"] == 1
    # Assert：多订单兼容字段包含本次唯一订单号。
    assert payload["queried_order_ids"] == ["SO100001"]
    # Assert：规划器观察工具结果后通过显式 finish 停止。
    assert payload["agent_stop_reason"] == "completed"


# 验证 Pydantic 在请求进入状态图之前拦截空文本。
@pytest.mark.asyncio
async def test_chat_rejects_empty_message() -> None:
    """明显不合法的请求应在进入状态图前由 Pydantic 拒绝。"""

    # Arrange：创建进程内异步 HTTP 客户端。
    async with AsyncClient(
        # ASGITransport 直接连接待测 FastAPI 应用。
        transport=ASGITransport(app=app),
        # 基础 URL 仅用于满足 HTTPX URL 解析要求，不会发出外部网络请求。
        base_url="http://testserver",
    ) as client:
        # Act：故意发送违反 ChatRequest 最小长度约束的空 message。
        response = await client.post(
            # 调用与正常工单相同的接口，差异只在非法请求体。
            "/api/v1/chat",
            # Token 合法而 message 为空，以便精准验证 message 字段约束。
            json={"message": ""},
            # 先通过认证，确保本测试得到的 422 来自请求体而不是 401。
            headers=_customer_headers("demo-user"),
        )

    # Assert：FastAPI/Pydantic 对请求校验失败时应返回标准 422，而不是进入状态图。
    assert response.status_code == 422


# 验证 HTTP 边界能够序列化 RAG 引用和检索分数。
@pytest.mark.asyncio
async def test_chat_returns_grounded_faq_with_citation() -> None:
    """知识 FAQ 应通过 API 返回答案、来源版本和证据分数。"""

    # Arrange：创建进程内异步客户端，不启动真实 Uvicorn 端口。
    async with AsyncClient(
        # 请求直接进入测试进程中的 FastAPI 应用。
        transport=ASGITransport(app=app),
        # 基础 URL 只用于 HTTPX 拼接相对路径。
        base_url="http://testserver",
    ) as client:
        # Act：提交种子知识库明确覆盖的发票问题。
        response = await client.post(
            # 使用统一 Agent 对话入口。
            "/api/v1/chat",
            # mock 分类和本地 hash/Qdrant 检索保证测试零费用、可重复。
            json={"message": "发票税号写错了怎么办"},
            # FAQ 同样要求 agent:chat Scope。
            headers=_customer_headers("user-001"),
        )

    # 将 JSON 解析一次供后续断言复用。
    payload = response.json()
    # Assert：完整 RAG 链路正常完成时返回 HTTP 200。
    assert response.status_code == 200
    # Assert：接口公开有限 FAQ 意图。
    assert payload["intent"] == "faq"
    # Assert：用户可见答案包含审核知识中的处理动作。
    assert "红冲重开" in payload["answer"]
    # Assert：返回最高检索分数供当前教学和阈值评测。
    assert payload["retrieval_score"] >= 0.10
    # Assert：第一条引用指向发票政策，而不是暴露 Qdrant 内部结构。
    assert payload["citations"][0]["document_id"] == "KB-INVOICE-001"
    # Assert：引用包含版本，方便审计回答依据。
    assert payload["citations"][0]["version"] == "1.4"


# 验证 HTTP 层能够返回 interrupt，并通过独立审批接口恢复相同线程。
@pytest.mark.asyncio
async def test_return_request_requires_approval_then_resumes_and_rejects_repeat() -> None:
    """退货申请必须先暂停；批准后创建；重复审批同一线程返回 409。"""

    # Arrange：在同一个客户端会话中完成发起和审批，底层使用同一应用 Checkpointer。
    async with AsyncClient(
        # ASGITransport 让测试不启动真实端口。
        transport=ASGITransport(app=app),
        # 基础 URL 只用于拼接接口路径。
        base_url="http://testserver",
    ) as client:
        # Act：提交已签收本人订单、明确原因和稳定幂等键。
        initial_response = await client.post(
            "/api/v1/chat",
            json={
                "message": "为订单 SO100002 申请退货，原因：商品尺寸不合适",
                "idempotency_key": "api-approval-test-001",
            },
            # 原始申请人身份来自 CUSTOMER Token。
            headers=_customer_headers("user-001"),
        )
        # 读取暂停响应中的可恢复线程主键。
        initial_payload = initial_response.json()
        thread_id = initial_payload["thread_id"]
        # Act：先提交字符串 "true"，验证高风险批准字段不接受模糊真值转换。
        invalid_approval_response = await client.post(
            f"/api/v1/approvals/{thread_id}",
            json={
                "approved": "true",
                "comment": "该请求应在 API Schema 被拒绝",
            },
            # 即使审批权限正确，字符串布尔值仍必须由 Schema 拒绝。
            headers=_reviewer_headers("api-reviewer-001"),
        )
        # Act：审批人使用该 thread_id 明确批准。
        approval_response = await client.post(
            f"/api/v1/approvals/{thread_id}",
            json={
                "approved": True,
                "comment": "已核对订单，批准创建",
            },
            # reviewer_id 将从这个 Token 的 sub 写入内部 ApprovalDecision。
            headers=_reviewer_headers("api-reviewer-001"),
        )
        # Act：模拟客户端误重试同一个审批线程。
        repeated_response = await client.post(
            f"/api/v1/approvals/{thread_id}",
            json={
                "approved": True,
                "comment": "重复提交",
            },
            # 使用合法审批 Token，确保 409 来自线程终态而不是权限问题。
            headers=_reviewer_headers("api-reviewer-001"),
        )

    # Assert：首次请求成功但明确处于审批等待态。
    assert initial_response.status_code == 200
    assert initial_payload["execution_status"] == "approval_required"
    assert initial_payload["approval_required"] is True
    assert initial_payload["return_request_id"] is None
    # Assert：中断负载包含审批必要信息，但不泄漏可信身份和幂等键。
    assert initial_payload["approval_request"]["order_id"] == "SO100002"
    assert "user_id" not in initial_payload["approval_request"]
    assert "idempotency_key" not in initial_payload["approval_request"]
    # Assert：字符串真值返回 422，且没有消耗或恢复正在等待的 interrupt。
    assert invalid_approval_response.status_code == 422

    # 解析批准后的最终响应。
    approval_payload = approval_response.json()
    # Assert：批准恢复成功并返回稳定业务申请编号。
    assert approval_response.status_code == 200
    assert approval_payload["execution_status"] == "completed"
    assert approval_payload["return_workflow_status"] == "completed"
    assert approval_payload["return_request_id"].startswith("RR-")
    assert approval_payload["tool_name"] == "create_return_request"
    # Assert：已完成线程不能再次恢复，从 API 层防止重复副作用。
    assert repeated_response.status_code == 409


# 验证拒绝审批不会产生工具或申请编号。
@pytest.mark.asyncio
async def test_return_request_rejection_has_no_write_result() -> None:
    """HTTP 审批拒绝应返回 rejected 终态且不出现任何写结果。"""

    # Arrange：创建进程内客户端。
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # Act：发起另一条使用唯一幂等键的合格退货请求。
        initial_response = await client.post(
            "/api/v1/chat",
            json={
                "message": "我要为订单 SO100002 申请退货，原因：暂时不需要这个商品",
                "idempotency_key": "api-rejection-test-001",
            },
            # 申请身份来自普通用户 Token。
            headers=_customer_headers("user-001"),
        )
        # 获取首次响应产生的可恢复线程。
        thread_id = initial_response.json()["thread_id"]
        # Act：审批人明确拒绝。
        rejection_response = await client.post(
            f"/api/v1/approvals/{thread_id}",
            json={
                "approved": False,
                "comment": "拒绝本次申请",
            },
            # 拒绝同样需要 return:approve Scope。
            headers=_reviewer_headers("api-reviewer-002"),
        )
        # Act：读取内部 Checkpoint，验证 reviewer_id 确实来自 Token sub。
        rejection_snapshot = await app.state.service_graph.aget_state(
            {"configurable": {"thread_id": thread_id}}
        )

    # 解析拒绝后的最终响应。
    payload = rejection_response.json()
    # Assert：拒绝属于正常业务终态，因此返回 200。
    assert rejection_response.status_code == 200
    assert payload["return_workflow_status"] == "rejected"
    # Assert：拒绝路径不执行写工具，也没有业务申请编号。
    assert payload["tool_name"] is None
    assert payload["return_request_id"] is None
    # Assert：请求体没有 reviewer_id，内部审批身份仍准确记录为 JWT sub。
    assert (
        rejection_snapshot.values["approval_decision"]["reviewer_id"]
        == "api-reviewer-002"
    )


# 验证对话入口不能在未认证状态下调用。
@pytest.mark.asyncio
async def test_chat_rejects_missing_bearer_token() -> None:
    """缺少 Authorization Header 时应在进入 LangGraph 前返回 401。"""

    # Arrange：创建普通进程内客户端。
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # Act：故意不提交 headers。
        response = await client.post(
            "/api/v1/chat",
            json={"message": "查询订单 SO100001 到哪了"},
        )

    # Assert：未认证请求不能触发分类、工具或订单查询。
    assert response.status_code == 401
    # Assert：响应提示调用方使用标准 Bearer 方案。
    assert response.headers["www-authenticate"] == "Bearer"


# 验证身份字段无法重新混入 JSON 请求体。
@pytest.mark.asyncio
async def test_chat_forbids_body_user_id_even_with_valid_token() -> None:
    """请求体不能覆盖 Token sub，且仓库查询必须使用 Token 中的身份。"""

    # Arrange：Token 身份是 user-001，但请求体故意伪造 user-002。
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # Act：extra=forbid 应在业务图之前拦截额外身份字段。
        response = await client.post(
            "/api/v1/chat",
            json={
                "user_id": "user-002",
                "message": "查询订单 SO200001 到哪了",
            },
            headers=_customer_headers("user-001"),
        )
        # Act：删除伪造字段后再次查询 user-002 的订单，验证实际身份仍为 Token 中的 user-001。
        cross_user_response = await client.post(
            "/api/v1/chat",
            json={"message": "查询订单 SO200001 到哪了"},
            headers=_customer_headers("user-001"),
        )

    # Assert：请求体契约违规返回 422，不会采用任一身份查询。
    assert response.status_code == 422
    # Assert：合法请求能执行，但不能读取不属于 Token sub 的订单。
    assert cross_user_response.status_code == 200
    assert "未找到该订单，或该订单不属于当前用户" in cross_user_response.json()["answer"]


# 验证普通用户 Token 无法执行人工审批。
@pytest.mark.asyncio
async def test_customer_scope_cannot_approve_return_request() -> None:
    """已认证但缺少 return:approve 的用户应得到 403，且 interrupt 保持待审批。"""

    # Arrange：先由 user-001 发起一条独立退货审批线程。
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        initial_response = await client.post(
            "/api/v1/chat",
            json={
                "message": "为订单 SO100002 申请退货，原因：用于权限隔离测试",
                "idempotency_key": "api-rbac-test-001",
            },
            headers=_customer_headers("user-001"),
        )
        # 读取等待审批线程。
        thread_id = initial_response.json()["thread_id"]
        # Act：同一个普通用户 Token 尝试批准写操作。
        forbidden_response = await client.post(
            f"/api/v1/approvals/{thread_id}",
            json={"approved": True, "comment": "普通用户越权批准"},
            headers=_customer_headers("user-001"),
        )
        # Act：合法审批人随后拒绝，证明之前的 403 没有消耗 interrupt 或产生副作用。
        reviewer_response = await client.post(
            f"/api/v1/approvals/{thread_id}",
            json={"approved": False, "comment": "清理权限测试线程"},
            headers=_reviewer_headers("api-reviewer-rbac"),
        )

    # Assert：身份有效但权限不足使用 403。
    assert forbidden_response.status_code == 403
    # Assert：合法审批人仍能处理原暂停线程。
    assert reviewer_response.status_code == 200
    assert reviewer_response.json()["return_workflow_status"] == "rejected"


# 验证审批 API 会生成最小化证据链，且只有独立审计员能够读取。
@pytest.mark.asyncio
async def test_auditor_reads_valid_minimized_approval_chain() -> None:
    """批准后应产生决定/完成两事件，响应不包含原因、备注、幂等键或完整 JWT。"""

    # Arrange：使用 lifespan 获得一套隔离的内存图、退货仓库和审计仓库。
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        # Act：普通用户先生成一条等待审批的退货草案。
        initial_response = await client.post(
            "/api/v1/chat",
            json={
                "message": "为订单 SO100002 申请退货，原因：审计链自动测试商品不合适",
                "idempotency_key": "api-audit-chain-test-001",
            },
            headers=_customer_headers("user-001"),
        )
        # 获取 Checkpointer 线程主键。
        thread_id = initial_response.json()["thread_id"]
        # Act：审批人批准并触发真实写工具。
        approval_response = await client.post(
            f"/api/v1/approvals/{thread_id}",
            json={"approved": True, "comment": "审计测试批准备注"},
            headers=_reviewer_headers("audited-reviewer-001"),
        )
        # Act：普通用户和审批人分别尝试读取，验证 Scope 职责隔离。
        customer_read_response = await client.get(
            f"/api/v1/audit/approvals/{thread_id}",
            headers=_customer_headers("user-001"),
        )
        reviewer_read_response = await client.get(
            f"/api/v1/audit/approvals/{thread_id}",
            headers=_reviewer_headers("audited-reviewer-001"),
        )
        # Act：独立审计员读取并触发服务端完整性重算。
        audit_response = await client.get(
            f"/api/v1/audit/approvals/{thread_id}",
            headers=_auditor_headers("security-auditor-001"),
        )

    # Assert：批准流程本身成功完成。
    assert approval_response.status_code == 200
    assert approval_response.json()["return_request_id"].startswith("RR-")
    # Assert：身份已认证但职责不匹配时统一返回 403。
    assert customer_read_response.status_code == 403
    assert reviewer_read_response.status_code == 403
    # Assert：审计员获得有效的两节点证据链。
    assert audit_response.status_code == 200
    audit_payload = audit_response.json()
    assert audit_payload["chain_valid"] is True
    assert [event["event_type"] for event in audit_payload["events"]] == [
        "approval_decision_recorded",
        "workflow_completed",
    ]
    # Assert：第二条事件引用第一条，并绑定真实退货申请号。
    assert (
        audit_payload["events"][1]["previous_event_hash"]
        == audit_payload["events"][0]["event_hash"]
    )
    assert audit_payload["events"][1]["return_request_id"].startswith("RR-")
    # Assert：审计主体来自审批 Token sub，且只暴露 jti 而非完整 Authorization Header。
    assert audit_payload["events"][0]["actor_id"] == "audited-reviewer-001"
    serialized_audit_payload = audit_response.text
    assert "审计测试批准备注" not in serialized_audit_payload
    assert "审计链自动测试商品不合适" not in serialized_audit_payload
    assert "api-audit-chain-test-001" not in serialized_audit_payload
    assert "Bearer " not in serialized_audit_payload


# 验证审计接口不允许枚举普通 Agent 线程。
@pytest.mark.asyncio
async def test_audit_endpoint_returns_404_for_thread_without_audit_events() -> None:
    """合法审计员查询无审批事件 UUID 时应得到固定 404。"""

    # Arrange：使用一个合法但不存在的 UUID，不依赖任何既有测试状态。
    missing_thread_id = "00000000-0000-4000-8000-000000000001"
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # Act：有权限的审计员查询未知线程。
        response = await client.get(
            f"/api/v1/audit/approvals/{missing_thread_id}",
            headers=_auditor_headers(),
        )

    # Assert：响应不说明该 UUID 是否存在非审批 Checkpoint。
    assert response.status_code == 404
    assert response.json()["detail"] == "未找到对应的审批审计记录"


# 验证 Outbox 补偿属于独立运维职责，普通用户和审计员都不能推进事件状态。
@pytest.mark.asyncio
async def test_only_operator_can_trigger_outbox_reconciliation() -> None:
    """运维接口应执行 Scope 隔离，并只返回低敏批次统计。"""

    # Arrange：lifespan 创建一套无待处理事件的隔离内存运行时。
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        # Act：普通用户和审计员分别尝试推进 Outbox。
        customer_response = await client.post(
            "/api/v1/internal/outbox/reconcile",
            headers=_customer_headers(),
        )
        auditor_response = await client.post(
            "/api/v1/internal/outbox/reconcile",
            headers=_auditor_headers(),
        )
        # Act：独立 OPERATOR 使用受限批次大小执行合法补偿。
        operator_response = await client.post(
            "/api/v1/internal/outbox/reconcile?limit=10",
            headers=_operator_headers(),
        )

    # Assert：有效身份但职责不匹配统一返回 403。
    assert customer_response.status_code == 403
    assert auditor_response.status_code == 403
    # Assert：无积压时合法运维调用返回全零计数，不暴露任何事件载荷。
    assert operator_response.status_code == 200
    assert operator_response.json() == {
        "scanned": 0,
        "processed": 0,
        "replayed": 0,
        "failed": 0,
        "dead_letter": 0,
    }

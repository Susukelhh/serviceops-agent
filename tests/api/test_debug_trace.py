"""第20.1步脱敏 LangGraph Checkpoint 教学接口测试。"""

# uuid4 为不存在的线程生成符合路径 Schema 的随机 UUID。
from uuid import uuid4

# pytest 提供异步测试与 monkeypatch。
import pytest

# ASGITransport 直接调用 FastAPI，不依赖 Docker 或本机端口。
from httpx import ASGITransport, AsyncClient

# app 是真实路由对象；app_module 用于临时验证 production 关闭门。
import serviceops_agent.api.app as app_module
from serviceops_agent.api.app import app

# Settings/get_settings 为测试 Token 和 production 环境替身提供同一配置模型。
from serviceops_agent.config.settings import Settings, get_settings

# 签发器生成与生产认证依赖完全相同格式的短期 JWT。
from serviceops_agent.security.jwt_auth import create_access_token

# 五种角色用于验证 developer 与业务、审批、审计职责隔离。
from serviceops_agent.security.models import Role


def _headers(role: Role, subject: str) -> dict[str, str]:
    """为一个有限角色签发真实 Bearer Header。"""

    token = create_access_token(
        settings=get_settings(),
        subject=subject,
        roles={role},
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_developer_can_replay_order_checkpoints_with_state_diffs() -> None:
    """开发者应看到节点、状态差异和工具结果，但看不到敏感 State 字段。"""

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # 普通用户先真实执行一次带订单工具调用的 LangGraph 线程。
        chat_response = await client.post(
            "/api/v1/chat",
            json={"message": "查询订单 SO100001 到哪了"},
            headers=_headers(Role.CUSTOMER, "user-001"),
        )
        thread_id = chat_response.json()["thread_id"]
        # 独立 developer Token 读取官方 aget_state_history 的脱敏结果。
        debug_response = await client.get(
            f"/api/v1/debug/threads/{thread_id}",
            headers=_headers(Role.DEVELOPER, "developer-test-001"),
        )

    assert chat_response.status_code == 200
    assert debug_response.status_code == 200
    payload = debug_response.json()
    # 完成的顺序图应有多个按时间正序排列的 StateSnapshot。
    assert payload["status"] == "completed"
    assert payload["checkpoint_count"] >= 8
    assert payload["hidden_reasoning_exposed"] is False
    assert [item["position"] for item in payload["checkpoints"]] == list(
        range(1, payload["checkpoint_count"] + 1)
    )
    # 回放中必须出现真正执行过的工具节点和最终空 next。
    executed_node_names = {
        node["name"]
        for checkpoint in payload["checkpoints"]
        for node in checkpoint["executed_nodes"]
    }
    assert "execute_order_tool" in executed_node_names
    assert payload["checkpoints"][-1]["next_nodes"] == []
    # 至少一个变化明确写入实际工具名，证明不是复述 ChatResponse events。
    tool_name_changes = [
        change
        for checkpoint in payload["checkpoints"]
        for change in checkpoint["state_changes"]
        if change["name"] == "tool_name"
    ]
    assert tool_name_changes[-1]["after"] == "get_order_status"
    # 响应正文不得含内部身份、幂等键、Token jti、审批主体或去重指纹字段名。
    for forbidden_name in (
        "user_id",
        "idempotency_key",
        "token_jti",
        "reviewer_id",
        "fingerprint",
        "tool_call_fingerprints",
    ):
        assert forbidden_name not in debug_response.text


@pytest.mark.asyncio
async def test_interrupt_checkpoint_is_visible_without_approval_secrets() -> None:
    """退货线程应显示 interrupt 与待批状态，但不导出草案幂等键和审批身份。"""

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        chat_response = await client.post(
            "/api/v1/chat",
            json={
                "message": "为订单 SO100002 申请退货，原因：教学调试测试尺寸不合适",
                "idempotency_key": f"debug-interrupt-{uuid4().hex}",
            },
            headers=_headers(Role.CUSTOMER, "user-001"),
        )
        thread_id = chat_response.json()["thread_id"]
        debug_response = await client.get(
            f"/api/v1/debug/threads/{thread_id}",
            headers=_headers(Role.DEVELOPER, "developer-test-002"),
        )

    assert chat_response.json()["execution_status"] == "approval_required"
    payload = debug_response.json()
    assert payload["status"] == "waiting_approval"
    latest = payload["checkpoints"][-1]
    assert latest["has_interrupt"] is True
    assert latest["interrupt"]["kind"] == "return_request_approval"
    assert latest["interrupt"]["order_id"] == "SO100002"
    assert "写工具尚未执行" in latest["decision_summary"]
    assert "idempotency_key" not in debug_response.text
    assert "reviewer_id" not in debug_response.text
    assert "token_jti" not in debug_response.text


@pytest.mark.asyncio
async def test_business_roles_cannot_read_developer_trace() -> None:
    """普通用户、审批人和审计员身份有效，但都不能替代 developer。"""

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        chat_response = await client.post(
            "/api/v1/chat",
            json={"message": "查询订单 SO100001"},
            headers=_headers(Role.CUSTOMER, "user-001"),
        )
        thread_id = chat_response.json()["thread_id"]
        forbidden_responses = [
            await client.get(
                f"/api/v1/debug/threads/{thread_id}",
                headers=_headers(role, f"{role.value}-test"),
            )
            for role in (Role.CUSTOMER, Role.RETURN_REVIEWER, Role.AUDITOR)
        ]

    assert [response.status_code for response in forbidden_responses] == [403, 403, 403]


@pytest.mark.asyncio
async def test_unknown_debug_thread_returns_404() -> None:
    """合法 developer 查询未知 UUID 时不应得到空成功响应。"""

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            f"/api/v1/debug/threads/{uuid4()}",
            headers=_headers(Role.DEVELOPER, "developer-test-404"),
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "未找到对应的调试线程"}


@pytest.mark.asyncio
async def test_production_runtime_returns_404_for_debug_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """即使持有开发者 Token，production 运行时也必须关闭调试读取。"""

    # 只替换路由读取的模块配置；认证仍使用测试 Settings 验证真实 developer Token。
    production_settings = Settings(
        environment="production",
        jwt_secret_key="production-debug-test-secret-that-is-long-enough-2026",
        telemetry_exporter="none",
    )
    monkeypatch.setattr(app_module, "settings", production_settings)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            f"/api/v1/debug/threads/{uuid4()}",
            headers=_headers(Role.DEVELOPER, "developer-production-test"),
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "未找到资源"}

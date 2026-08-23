"""公网作品沙盒的短时身份、数据映射和跨会话隔离测试。"""

# pytest 提供异步测试和安全恢复 Settings 的 monkeypatch。
import pytest

# ASGITransport 直接运行真实 FastAPI lifespan，不依赖本机 Docker 端口。
from httpx import ASGITransport, AsyncClient

# app 是与生产部署相同的 API；settings 是本进程已缓存配置实例。
from serviceops_agent.api.app import app, settings

# decode_access_token 证明会话响应不是假字符串，而是完整可验证 JWT。
from serviceops_agent.security.jwt_auth import decode_access_token

# 公网沙盒只能获得服务端策略中声明的有限角色。
from serviceops_agent.security.models import Role


@pytest.mark.asyncio
async def test_public_demo_session_is_short_lived_and_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个匿名访客应获得不同身份，并且不能读取彼此的 Agent Checkpoint。"""

    # Arrange：只在当前测试期间开启沙盒；测试结束后 monkeypatch 自动恢复原配置。
    monkeypatch.setattr(settings, "public_demo_enabled", True)
    monkeypatch.setattr(settings, "public_demo_token_minutes", 10)
    monkeypatch.setattr(settings, "public_demo_max_message_chars", 500)
    async with (
        # 真实 lifespan 会装配 PublicDemoOrderRepository 和隔离内存 Checkpointer。
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        # Act：两名访客分别申请页面内存使用的短期身份。
        first_session = await client.post("/api/v1/demo/session")
        second_session = await client.post("/api/v1/demo/session")

        # Assert：响应明确禁止缓存，两个主体也不能复用同一身份。
        assert first_session.status_code == 200
        assert first_session.headers["cache-control"] == "no-store"
        first_payload = first_session.json()
        second_payload = second_session.json()
        assert first_payload["session_id"] != second_payload["session_id"]
        assert first_payload["expires_in_seconds"] == 600
        # Token 必须经过同一密码学与 Claims 校验，并且只有 public_demo 角色。
        first_principal = decode_access_token(
            token=first_payload["access_token"],
            settings=settings,
        )
        assert first_principal.subject == first_payload["session_id"]
        assert first_principal.roles == frozenset({Role.PUBLIC_DEMO})

        # Act：第一名访客用统一业务入口查询公开样例订单。
        first_headers = {
            "Authorization": f"Bearer {first_payload['access_token']}"
        }
        chat_response = await client.post(
            "/api/v1/chat",
            headers=first_headers,
            json={"message": "查询订单 SO100001 到哪了"},
        )

        # Assert：订单映射成功，返回真实线程而不是前端伪造结果。
        assert chat_response.status_code == 200
        chat_payload = chat_response.json()
        assert chat_payload["order_id"] == "SO100001"
        thread_id = chat_payload["thread_id"]
        # 第一名访客可以读取自己线程的脱敏 Checkpoint 回放。
        own_debug = await client.get(
            f"/api/v1/debug/threads/{thread_id}",
            headers=first_headers,
        )
        assert own_debug.status_code == 200
        # 第二名访客即使知道 UUID，也只能得到不泄漏存在性的 404。
        second_headers = {
            "Authorization": f"Bearer {second_payload['access_token']}"
        }
        foreign_debug = await client.get(
            f"/api/v1/debug/threads/{thread_id}",
            headers=second_headers,
        )
        assert foreign_debug.status_code == 404


@pytest.mark.asyncio
async def test_public_demo_is_disabled_by_default() -> None:
    """普通本地或企业部署未显式开启时不能匿名申请身份。"""

    # Arrange：全局测试配置没有开启 public_demo_enabled。
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # Act：尝试访问固定路由。
        response = await client.post("/api/v1/demo/session")

    # Assert：返回 404，避免暴露或意外启用匿名能力。
    assert response.status_code == 404

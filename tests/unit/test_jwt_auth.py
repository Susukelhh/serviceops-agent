"""JWT 签名、标准 Claims、过期时间和角色—Scope 策略单元测试。"""

# datetime/timedelta 构造已经过期的 Token 签发时间。
from datetime import UTC, datetime, timedelta

# uuid4 为手工构造的策略冲突 Token 提供合法 jti。
from uuid import uuid4

# PyJWT 仅在一个测试中模拟“拥有签名密钥但错误授予 Scope”的上游签发器。
import jwt

# pytest 提供异常断言。
import pytest

# HTTPException 是认证边界对无效 Token 的统一外部错误。
from fastapi import HTTPException

# ValidationError 用于验证 production 配置安全门在启动阶段失败。
from pydantic import ValidationError

# 开发占位密钥常量和 Settings 用于构造隔离配置。
from serviceops_agent.config.settings import (
    DEVELOPMENT_ONLY_JWT_SECRET,
    Settings,
    get_settings,
)

# JWT_ALGORITHM 固定算法；签发/解码函数是本文件主要测试目标。
from serviceops_agent.security.jwt_auth import (
    JWT_ALGORITHM,
    create_access_token,
    decode_access_token,
)

# 有限角色与 Scope 用于构造合法和越权组合。
from serviceops_agent.security.models import ROLE_SCOPE_POLICY, PermissionScope, Role


def test_valid_customer_token_decodes_to_trusted_principal() -> None:
    """合法 customer Token 应产生带 agent:chat 的可信主体。"""

    # Arrange：使用测试环境集中配置签发 Token。
    settings = get_settings()
    token = create_access_token(
        settings=settings,
        subject="user-001",
        roles={Role.CUSTOMER},
    )

    # Act：执行与 API 完全相同的签名和 Claims 校验。
    principal = decode_access_token(token=token, settings=settings)

    # Assert：身份只能来自 sub。
    assert principal.subject == "user-001"
    # Assert：普通用户获得对话权限。
    assert PermissionScope.AGENT_CHAT in principal.scopes
    # Assert：普通用户不能获得审批权限。
    assert PermissionScope.RETURN_APPROVE not in principal.scopes
    # Assert：jti 被保留为内部 Token ID。
    assert principal.token_id


def test_auditor_token_has_only_audit_read_scope() -> None:
    """独立审计员 Token 只能读证据链，不能对话或执行审批。"""

    # Arrange：根据服务端角色策略签发审计员 Token。
    settings = get_settings()
    token = create_access_token(
        settings=settings,
        subject="auditor-001",
        roles={Role.AUDITOR},
    )

    # Act：执行与 API 相同的签名和 Claims 校验。
    principal = decode_access_token(token=token, settings=settings)

    # Assert：只拥有 audit:read，不继承其他角色权限。
    assert principal.scopes == frozenset({PermissionScope.AUDIT_READ})
    assert PermissionScope.AGENT_CHAT not in principal.scopes
    assert PermissionScope.RETURN_APPROVE not in principal.scopes


def test_developer_token_has_only_debug_read_scope() -> None:
    """本地 developer Token 只能读取脱敏调试轨迹，不能调用任何业务接口。"""

    settings = get_settings()
    token = create_access_token(
        settings=settings,
        subject="developer-001",
        roles={Role.DEVELOPER},
    )

    principal = decode_access_token(token=token, settings=settings)

    assert principal.scopes == frozenset({PermissionScope.DEBUG_READ})
    assert PermissionScope.AGENT_CHAT not in principal.scopes
    assert PermissionScope.RETURN_APPROVE not in principal.scopes
    assert PermissionScope.AUDIT_READ not in principal.scopes


def test_workflow_recovery_scope_belongs_only_to_operator() -> None:
    """陈旧执行恢复权限遵循最小授权，不能被业务、审批或调试角色继承。"""

    settings = get_settings()
    token = create_access_token(
        settings=settings,
        subject="operator-001",
        roles={Role.OPERATOR},
    )

    principal = decode_access_token(token=token, settings=settings)

    assert PermissionScope.WORKFLOW_RECOVERY in principal.scopes
    assert {
        role
        for role, scopes in ROLE_SCOPE_POLICY.items()
        if PermissionScope.WORKFLOW_RECOVERY in scopes
    } == {Role.OPERATOR}


def test_knowledge_curator_has_only_feedback_review_scope() -> None:
    """知识审核员不能借问题池权限获得其他业务或运维能力。"""

    settings = get_settings()
    token = create_access_token(
        settings=settings,
        subject="curator-001",
        roles={Role.KNOWLEDGE_CURATOR},
    )

    principal = decode_access_token(token=token, settings=settings)

    assert principal.scopes == frozenset({PermissionScope.FEEDBACK_REVIEW})


def test_expired_token_is_rejected_with_generic_401() -> None:
    """签名正确但已经过期的 Token 也必须在进入 API 前被拒绝。"""

    # Arrange：签发时间放在一小时前，有效期只有一分钟。
    settings = get_settings()
    expired_token = create_access_token(
        settings=settings,
        subject="user-001",
        roles={Role.CUSTOMER},
        issued_at=datetime.now(UTC) - timedelta(hours=1),
        expires_delta=timedelta(minutes=1),
    )

    # Act：解码函数应把 ExpiredSignatureError 归一化为 HTTPException。
    with pytest.raises(HTTPException) as captured_error:
        decode_access_token(token=expired_token, settings=settings)

    # Assert：外部只看到统一 401，不知道签名是否曾经正确。
    assert captured_error.value.status_code == 401
    assert captured_error.value.detail == "无效或已过期的访问令牌"


def test_token_for_other_audience_is_rejected() -> None:
    """其他服务的合法 Token 不能被当前 Agent API 横向复用。"""

    # Arrange：两个配置共享签名密钥和 issuer，但 audience 不同。
    api_settings = get_settings()
    other_service_settings = Settings(
        jwt_secret_key=api_settings.jwt_secret_key,
        jwt_issuer=api_settings.jwt_issuer,
        jwt_audience="another-internal-api",
    )
    # 使用其他服务配置签发。
    token = create_access_token(
        settings=other_service_settings,
        subject="user-001",
        roles={Role.CUSTOMER},
    )

    # Act：当前 API 使用自己的 audience 验证。
    with pytest.raises(HTTPException) as captured_error:
        decode_access_token(token=token, settings=api_settings)

    # Assert：aud 不匹配返回统一 401。
    assert captured_error.value.status_code == 401


def test_signer_cannot_assign_scope_outside_role_policy() -> None:
    """本地签发工具不能给 customer 角色越权增加退货审批 Scope。"""

    # Act/Assert：角色策略在签名前就拒绝非法组合。
    with pytest.raises(ValueError, match="Scope 超出角色策略"):
        create_access_token(
            settings=get_settings(),
            subject="user-001",
            roles={Role.CUSTOMER},
            scopes={PermissionScope.RETURN_APPROVE},
        )


def test_decoder_rejects_signed_role_scope_policy_conflict() -> None:
    """即使上游错误签名了越权组合，API 解码端也应再次执行策略校验。"""

    # Arrange：模拟错误签发器手工创建 customer + return:approve Payload。
    settings = get_settings()
    now = datetime.now(UTC)
    conflicting_payload = {
        "iss": settings.jwt_issuer,
        "sub": "user-001",
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "jti": str(uuid4()),
        "roles": [Role.CUSTOMER.value],
        "scope": PermissionScope.RETURN_APPROVE.value,
    }
    # 使用正确密钥和固定算法签名，使失败原因只来自角色—权限策略。
    conflicting_token = jwt.encode(
        conflicting_payload,
        settings.jwt_secret_key.get_secret_value(),
        algorithm=JWT_ALGORITHM,
    )

    # Act：API 端不能只验证签名就接受该权限。
    with pytest.raises(HTTPException) as captured_error:
        decode_access_token(token=conflicting_token, settings=settings)

    # Assert：策略冲突使用统一无效 Token 响应。
    assert captured_error.value.status_code == 401


def test_production_rejects_repository_development_secret() -> None:
    """production 不能携带仓库内人人已知的本地开发密钥启动。"""

    # Act/Assert：Settings 在应用创建前就拒绝危险组合。
    with pytest.raises(ValidationError, match="生产环境必须注入独立的 JWT 签名密钥"):
        Settings(
            environment="production",
            jwt_secret_key=DEVELOPMENT_ONLY_JWT_SECRET,
        )

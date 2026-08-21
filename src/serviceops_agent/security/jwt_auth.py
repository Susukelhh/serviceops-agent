"""JWT 签发、验证以及 FastAPI Bearer/Scope 安全依赖。"""

# datetime/timedelta 生成带 UTC 的 iat/exp；uuid4 生成唯一 jti。
from datetime import UTC, datetime, timedelta

# Annotated 把 FastAPI Security 元数据附加到凭证参数。
from typing import Annotated

# uuid4 为每枚短期访问 Token 生成不可预测的唯一编号。
from uuid import uuid4

# PyJWT 执行 HS256 签名、固定算法验证和标准 Claims 校验。
import jwt

# HTTPException 提供统一 401/403；Security 把 Scope 要求传给依赖。
from fastapi import HTTPException, Security, status

# HTTPBearer 从 Authorization Header 读取 Token；SecurityScopes 收集接口所需权限。
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, SecurityScopes

# InvalidTokenError 覆盖签名、格式、过期、issuer、audience 和缺失 Claim 等失败。
from jwt.exceptions import InvalidTokenError

# ValidationError 表示签名通过但 Payload 类型、角色或长度不符合领域 Schema。
from pydantic import ValidationError

# Settings 提供秘密、iss/aud、有效期和时钟误差。
from serviceops_agent.config.settings import Settings, get_settings

# Claims/Principal/Policy 是认证边界允许使用的有限领域模型。
from serviceops_agent.security.models import (
    ROLE_SCOPE_POLICY,
    AccessTokenClaims,
    AuthenticatedPrincipal,
    PermissionScope,
    Role,
)

# 算法由服务端代码固定，绝不能读取攻击者可控 JWT Header 中的 alg 决定验证方式。
JWT_ALGORITHM = "HS256"

# HTTPBearer 让 Swagger 的 Authorize 对话框可以直接粘贴本地演示 Token。
bearer_scheme = HTTPBearer(
    # 认证失败由本模块统一返回稳定 401，而不是让 Scheme 提前产生不同错误。
    auto_error=False,
    # OpenAPI 中显示清楚的安全方案名称。
    scheme_name="ServiceOpsJWT",
    # 说明 Token 来源，避免误以为服务提供了生产登录接口。
    description="使用 examples/09_generate_dev_tokens.py 生成本地短期 Bearer Token",
)


def _allowed_scopes_for_roles(roles: set[Role]) -> set[PermissionScope]:
    """根据服务端角色策略计算允许授予的 Scope 并集。"""

    # 从空集合开始累加每个角色允许的权限。
    allowed_scopes: set[PermissionScope] = set()
    # 角色已经经过有限枚举校验。
    for role in roles:
        # update 合并权限并自动去重。
        allowed_scopes.update(ROLE_SCOPE_POLICY[role])
    # 返回调用方可以进一步比较的可变集合副本。
    return allowed_scopes


def create_access_token(
    *,
    settings: Settings,
    subject: str,
    roles: set[Role],
    scopes: set[PermissionScope] | None = None,
    expires_delta: timedelta | None = None,
    issued_at: datetime | None = None,
) -> str:
    """为本地开发和自动测试签发一枚短期、带角色与 Scope 的 JWT。"""

    # 角色不能为空，否则无法确定主体在系统中的权限边界。
    if not roles:
        raise ValueError("JWT 至少需要一个角色")
    # 服务端策略计算这些角色最多能拥有的权限。
    allowed_scopes = _allowed_scopes_for_roles(roles)
    # 默认授予角色策略中的全部权限；测试可以显式传入更小集合验证缺权。
    selected_scopes = scopes if scopes is not None else allowed_scopes
    # 即使调用者持有签名密钥，也不允许工具函数生成角色策略之外的组合。
    if not selected_scopes.issubset(allowed_scopes):
        raise ValueError("请求签发的 Scope 超出角色策略")

    # 默认使用当前 UTC 时间；测试可以注入固定或历史时间验证过期逻辑。
    current_time = issued_at or datetime.now(UTC)
    # 无时区 datetime 容易产生服务器本地时区歧义，因此明确拒绝。
    if current_time.tzinfo is None:
        raise ValueError("issued_at 必须包含时区")
    # 未指定有效期时读取集中配置的分钟数。
    token_lifetime = expires_delta or timedelta(
        minutes=settings.jwt_access_token_minutes
    )
    # 过期时间必须晚于签发时间。
    if token_lifetime <= timedelta(0):
        raise ValueError("JWT 有效期必须大于零")

    # 构造只包含标准和有限自定义字段的 Payload。
    payload = {
        # 预期签发者。
        "iss": settings.jwt_issuer,
        # 可信用户/审批人主键。
        "sub": subject,
        # 预期接收方。
        "aud": settings.jwt_audience,
        # PyJWT 会把带时区 datetime 编码为 NumericDate。
        "iat": current_time,
        # 过期时间同样编码为 NumericDate。
        "exp": current_time + token_lifetime,
        # 唯一 Token ID 便于后续撤销和审计。
        "jti": str(uuid4()),
        # 角色按字符串排序，保证调试输出稳定。
        "roles": sorted(role.value for role in roles),
        # Scope 按 OAuth2 约定组成空格分隔字符串。
        "scope": " ".join(sorted(scope.value for scope in selected_scopes)),
    }
    # 使用服务端固定 HS256 和 SecretStr 明文值完成签名；不会记录密钥。
    return jwt.encode(
        payload,
        settings.jwt_secret_key.get_secret_value(),
        algorithm=JWT_ALGORITHM,
    )


def decode_access_token(*, token: str, settings: Settings) -> AuthenticatedPrincipal:
    """验证 Token 的密码学签名、标准 Claims、领域结构和角色—权限组合。"""

    try:
        # 固定算法白名单，不能使用 JWT Header 动态选择算法。
        raw_claims = jwt.decode(
            token,
            # SecretStr 只在验证调用的最小范围内解包。
            settings.jwt_secret_key.get_secret_value(),
            # 明确只接受 HS256。
            algorithms=[JWT_ALGORITHM],
            # audience 必须与当前 API 一致。
            audience=settings.jwt_audience,
            # issuer 必须与当前签发方一致。
            issuer=settings.jwt_issuer,
            # 允许配置范围内的极小时钟漂移。
            leeway=settings.jwt_clock_skew_seconds,
            # require 保证关键 Claim 不仅“若存在则校验”，而是必须存在。
            options={
                "require": [
                    "iss",
                    "sub",
                    "aud",
                    "iat",
                    "exp",
                    "jti",
                    "roles",
                    "scope",
                ]
            },
        )
        # 签名有效仍不等于业务字段可信，继续执行 Pydantic 类型与范围校验。
        claims = AccessTokenClaims.model_validate(raw_claims)
        # Scope 字符串逐项转换为有限枚举；未知权限会触发 ValueError。
        granted_scopes = {
            PermissionScope(scope_value)
            for scope_value in claims.scope.split()
            if scope_value
        }
        # 角色列表去重后形成集合。
        granted_roles = set(claims.roles)
        # 重新计算角色策略允许的最大权限。
        allowed_scopes = _allowed_scopes_for_roles(granted_roles)
        # Token 声称的权限超出角色策略时拒绝，而不是只相信签名。
        if not granted_scopes or not granted_scopes.issubset(allowed_scopes):
            raise ValueError("Token Scope 与角色策略不一致")
        # 构造业务接口可使用的最小可信主体。
        return AuthenticatedPrincipal(
            subject=claims.sub,
            roles=frozenset(granted_roles),
            scopes=frozenset(granted_scopes),
            token_id=claims.jti,
        )
    # 所有验证失败对客户端统一返回 401，避免泄漏 Token 哪一部分接近正确。
    except (InvalidTokenError, ValidationError, ValueError) as error:
        # 保留异常链供服务端调试，但响应正文不包含原 Token 或解析详情。
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效或已过期的访问令牌",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error


async def require_principal(
    security_scopes: SecurityScopes,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(bearer_scheme),
    ],
) -> AuthenticatedPrincipal:
    """读取 Bearer Token，并强制检查当前路径声明的全部 Scope。"""

    # 缺少 Authorization Header 时返回标准 401。
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 Bearer 访问令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # 使用缓存 Settings 验证签名和 Claims。
    principal = decode_access_token(
        token=credentials.credentials,
        settings=get_settings(),
    )
    # Security(...) 声明的 Scope 会集中出现在 security_scopes.scopes。
    required_scopes = set(security_scopes.scopes)
    # 已认证但权限不足属于 403，而不是伪装成未登录 401。
    if not principal.has_all_scopes(required_scopes):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="当前身份缺少接口所需权限",
        )
    # 返回后，业务路由只能使用该可信主体，不再读取请求体身份。
    return principal

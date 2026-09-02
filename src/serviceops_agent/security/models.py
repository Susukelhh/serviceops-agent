"""JWT Claims、角色、权限和认证主体领域模型。"""

# StrEnum 让角色和 Scope 同时具备有限枚举约束与 JSON 字符串表现。
from enum import StrEnum

# BaseModel 提供运行时校验；Field 约束身份、角色和权限集合大小。
from pydantic import BaseModel, Field


class Role(StrEnum):
    """当前项目支持的有限身份角色。"""

    # CUSTOMER 代表普通售后用户，可以提交 Agent 对话请求。
    CUSTOMER = "customer"
    # RETURN_REVIEWER 代表退货审批人，可以批准或拒绝写操作草案。
    RETURN_REVIEWER = "return_reviewer"
    # AUDITOR 代表安全审计员，只能读取审批证据链，不能发起对话或执行审批。
    AUDITOR = "auditor"
    # OPERATOR 代表平台运维人员，可触发低敏补偿与生命周期清理，不读取业务正文。
    OPERATOR = "operator"
    # DEVELOPER 代表本地开发调试人员，只能读取已经脱敏的 LangGraph 教学轨迹。
    # 该角色不拥有普通用户查询、退货审批、审计链读取或运维补偿权限。
    DEVELOPER = "developer"
    # KNOWLEDGE_CURATOR 可以读取反馈问题池并做知识归因，但不能发起业务写操作。
    KNOWLEDGE_CURATOR = "knowledge_curator"
    # PUBLIC_DEMO 是服务端短时签发的沙盒访客，只能操作属于自己会话的演示线程。
    PUBLIC_DEMO = "public_demo"


class PermissionScope(StrEnum):
    """可以写入 JWT scope Claim 的有限细粒度权限。"""

    # AGENT_CHAT 允许调用统一 Agent 对话入口。
    AGENT_CHAT = "agent:chat"
    # RETURN_APPROVE 允许恢复退货 interrupt 并提交审批决定。
    RETURN_APPROVE = "return:approve"
    # AUDIT_READ 允许读取审批主体、Token jti、决定和哈希链验证结果。
    AUDIT_READ = "audit:read"
    # OUTBOX_RECONCILE 允许触发待处理事务事件补偿，但不能批准或读取审计链。
    OUTBOX_RECONCILE = "operations:reconcile"
    # CONVERSATION_CLEANUP 允许执行到期会话最小化清理，不允许读取会话正文。
    CONVERSATION_CLEANUP = "operations:conversation-cleanup"
    # WORKFLOW_RECOVERY 允许处置陈旧执行租约，只返回低敏分类计数。
    WORKFLOW_RECOVERY = "operations:workflow-recovery"
    # DEBUG_READ 只允许在 development/test 环境读取脱敏后的状态与 Checkpoint 历史。
    DEBUG_READ = "debug:read"
    # FEEDBACK_REVIEW 允许读取失败问题池、提交归因并导出待评测知识候选。
    FEEDBACK_REVIEW = "feedback:review"


# 角色到最大允许 Scope 的服务端策略，不信任 Token 自己声称的任意角色—权限组合。
ROLE_SCOPE_POLICY: dict[Role, frozenset[PermissionScope]] = {
    # 普通用户只能与 Agent 对话，不能审批写操作。
    Role.CUSTOMER: frozenset({PermissionScope.AGENT_CHAT}),
    # 审批人当前只负责退货审批，不默认获得普通用户数据查询权。
    Role.RETURN_REVIEWER: frozenset({PermissionScope.RETURN_APPROVE}),
    # 审计员遵循职责分离原则，只能读取审计证据，不能批准自己的操作。
    Role.AUDITOR: frozenset({PermissionScope.AUDIT_READ}),
    # 运维人员可推进待处理事件、清理会话和恢复陈旧执行，不能查看载荷或执行审批。
    Role.OPERATOR: frozenset(
        {
            PermissionScope.OUTBOX_RECONCILE,
            PermissionScope.CONVERSATION_CLEANUP,
            PermissionScope.WORKFLOW_RECOVERY,
        }
    ),
    # 开发调试人员只能读取教学轨迹；生产环境路由还会执行第二道关闭检查。
    Role.DEVELOPER: frozenset({PermissionScope.DEBUG_READ}),
    # 知识审核员只管理脱敏反馈闭环，不继承用户、审批、审计或运维权限。
    Role.KNOWLEDGE_CURATOR: frozenset({PermissionScope.FEEDBACK_REVIEW}),
    # 公网访客可完整体验对话、审批、审计与教学回放；各读取路由还会校验线程归属。
    Role.PUBLIC_DEMO: frozenset(
        {
            PermissionScope.AGENT_CHAT,
            PermissionScope.RETURN_APPROVE,
            PermissionScope.AUDIT_READ,
            PermissionScope.DEBUG_READ,
        }
    ),
}


class AccessTokenClaims(BaseModel):
    """完成签名验证后仍需通过 Pydantic 校验的 JWT Payload。"""

    # iss 是签发者，必须与 Settings 完全一致。
    iss: str = Field(min_length=1, max_length=200)
    # sub 是可信身份主键；业务 State 的 user_id/reviewer_id 都从这里产生。
    sub: str = Field(min_length=1, max_length=64)
    # aud 是目标 API，防止 Token 被其他服务横向复用。
    aud: str = Field(min_length=1, max_length=200)
    # iat 是签发 Unix 时间戳。
    iat: int = Field(ge=0)
    # exp 是过期 Unix 时间戳，PyJWT 会在 Pydantic 前完成有效期校验。
    exp: int = Field(ge=0)
    # jti 是每枚 Token 的唯一编号，后续可以用于撤销列表和安全审计。
    jti: str = Field(min_length=8, max_length=100)
    # roles 限制为项目已知角色，未知角色不能静默进入系统。
    roles: list[Role] = Field(min_length=1, max_length=10)
    # scope 按 OAuth2 约定保存为空格分隔字符串。
    scope: str = Field(min_length=1, max_length=500)


class AuthenticatedPrincipal(BaseModel):
    """通过密码学和 Claims 校验后交给业务接口的可信认证主体。"""

    # subject 来自 Token sub，而不是请求 JSON。
    subject: str = Field(min_length=1, max_length=64)
    # roles 是去重后的有限角色集合。
    roles: frozenset[Role] = Field(min_length=1, max_length=10)
    # scopes 是去重后的有限权限集合。
    scopes: frozenset[PermissionScope] = Field(min_length=1, max_length=20)
    # token_id 保留 jti 供后续审计，但不会写入普通用户响应。
    token_id: str = Field(min_length=8, max_length=100)

    def has_all_scopes(self, required_scopes: set[str]) -> bool:
        """判断认证主体是否同时拥有接口要求的全部 Scope。"""

        # 把有限枚举转换为字符串集合，与 FastAPI SecurityScopes 直接比较。
        granted_scope_values = {scope.value for scope in self.scopes}
        # issubset 要求每个必需权限都存在，不能满足部分权限就放行。
        return required_scopes.issubset(granted_scope_values)

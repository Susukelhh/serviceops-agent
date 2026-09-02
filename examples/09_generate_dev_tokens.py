"""第九步示例：生成供本地 Swagger/PowerShell 使用的短期 JWT。

运行方式：

    uv run python examples/09_generate_dev_tokens.py

脚本只在 development/test 环境工作，不启动登录接口、不打印 JWT 签名密钥。
"""

# datetime/timedelta 用于向用户显示 Token 的本地过期时间。
from datetime import UTC, datetime, timedelta

# get_settings 读取本机开发密钥、issuer、audience 和有效期。
from serviceops_agent.config.settings import get_settings

# create_access_token 使用与 FastAPI 验证端完全相同的 Claims 和签名规则。
from serviceops_agent.security.jwt_auth import create_access_token

# Role 决定 Token 根据服务端策略获得对话、审批、审计、运维或调试权限。
from serviceops_agent.security.models import Role


def main() -> None:
    """生成普通用户、审批人、审计员、运维员、开发者和知识审核员Token。"""

    # 读取缓存配置，但绝不打印 SecretStr 明文。
    settings = get_settings()
    # 生产环境必须由企业身份平台签发 Token，禁止运行本地演示签发器。
    if settings.environment == "production":
        raise RuntimeError("production 环境禁止使用本地开发 Token 生成脚本")

    # CUSTOMER Token 的 sub 是本地订单种子数据中的 user-001。
    customer_token = create_access_token(
        # 使用当前服务验证端相同配置。
        settings=settings,
        # sub 会成为 Agent State 中的可信 user_id。
        subject="user-001",
        # CUSTOMER 只获得 agent:chat。
        roles={Role.CUSTOMER},
    )
    # 审批人 Token 使用不同主体和不同角色。
    reviewer_token = create_access_token(
        # 同一签发者和 audience。
        settings=settings,
        # sub 会成为 ApprovalDecision 中的 reviewer_id。
        subject="reviewer-001",
        # RETURN_REVIEWER 只获得 return:approve。
        roles={Role.RETURN_REVIEWER},
    )
    # 审计员 Token 使用第三个独立主体，不同时拥有审批权限。
    auditor_token = create_access_token(
        # 同一签发者、audience 和有效期策略。
        settings=settings,
        # sub 会成为审计读取操作的可信主体。
        subject="auditor-001",
        # AUDITOR 只获得 audit:read。
        roles={Role.AUDITOR},
    )
    # 运维 Token 只用于触发 Outbox 补偿，不允许读取审计事件内容。
    operator_token = create_access_token(
        settings=settings,
        subject="operator-001",
        roles={Role.OPERATOR},
    )
    # 开发者 Token 只读取脱敏的 Checkpoint 教学轨迹，不能调用其他业务接口。
    developer_token = create_access_token(
        # 仍使用 API 相同的本地签名配置。
        settings=settings,
        # sub 只用于建立可信调试身份，不进入公开状态或日志正文。
        subject="developer-001",
        # DEVELOPER 根据服务端策略只获得 debug:read。
        roles={Role.DEVELOPER},
    )
    # 知识审核员只读取反馈问题池并形成待评测候选，不拥有普通业务权限。
    curator_token = create_access_token(
        settings=settings,
        subject="curator-001",
        roles={Role.KNOWLEDGE_CURATOR},
    )

    # 根据配置计算近似过期时间；真正校验仍以 Token exp 为准。
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.jwt_access_token_minutes)
    # 输出用途说明。
    print("=== 普通用户 Token（sub=user-001，scope=agent:chat）===")
    # Token 可以粘贴到 Swagger Authorize，但不应发到聊天或提交 Git。
    print(customer_token)
    # 空行分隔两枚长字符串。
    print()
    # 输出审批 Token 说明。
    print("=== 退货审批 Token（sub=reviewer-001，scope=return:approve）===")
    # 审批 Token 权限更高，应比普通 Token 更谨慎保管。
    print(reviewer_token)
    # 空行分隔审批人与审计员 Token。
    print()
    # 输出审计 Token 说明。
    print("=== 审计员 Token（sub=auditor-001，scope=audit:read）===")
    # 审计 Token 可读取主体和 jti，同样不应提交 Git 或发送到聊天。
    print(auditor_token)
    # 空行分隔审计员和运维员 Token。
    print()
    # 运维 Token 只可调用内部协调接口。
    print("=== 运维员 Token（sub=operator-001，scope=operations:reconcile）===")
    print(operator_token)
    # 空行分隔运维员和本地开发调试 Token。
    print()
    # 调试 Token 只能在 development/test 环境读取脱敏状态历史。
    print("=== 开发者 Token（sub=developer-001，scope=debug:read）===")
    print(developer_token)
    # 空行分隔开发者和知识审核员Token。
    print()
    print("=== 知识审核员 Token（sub=curator-001，scope=feedback:review）===")
    print(curator_token)
    # 输出预计失效时间，提醒开发者不要长期保存演示 Token。
    print()
    print(f"预计 UTC 过期时间：{expires_at.isoformat()}")
    # Swagger 使用方法。
    print("打开 http://127.0.0.1:8000/docs，点击 Authorize，粘贴所需 Token。")
    # 明确不应添加 Bearer 前缀，Swagger HTTPBearer 会自动生成标准 Header。
    print("Swagger 输入框只粘贴 Token 本身，不要手动添加 Bearer 前缀。")


# 只有直接运行文件时才签发本地 Token。
if __name__ == "__main__":
    # 调用同步入口；签发 JWT 不涉及网络或异步 I/O。
    main()

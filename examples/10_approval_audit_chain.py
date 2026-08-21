"""第十步示例：观察最小化审批审计事件与 SHA-256 前驱哈希链。

运行方式：

    uv run python examples/10_approval_audit_chain.py

该示例只使用内存仓库，不启动 FastAPI、不读取真实 JWT，也不会访问模型或产生 API 费用。
"""

# json 以清晰缩进输出领域事件，方便和 PyCharm 变量窗口逐字段对照。
import json

# 审计草稿、事件类型和内容摘要函数组成安全审计边界。
from serviceops_agent.domain.audit import (
    ApprovalAuditDraft,
    ApprovalAuditEventType,
    build_comment_digest,
    build_proposal_digest,
)

# ReturnRequestProposal 模拟 LangGraph interrupt 前冻结的强类型可信草案。
from serviceops_agent.domain.returns import ReturnRequestProposal

# 内存仓库实现与 SQLite 使用完全相同的哈希算法，适合零文件学习。
from serviceops_agent.infrastructure.audit_repository import (
    InMemoryApprovalAuditRepository,
)


def main() -> None:
    """追加决定和完成事件，并展示最小化字段与哈希链关系。"""

    # Arrange：构造一份与真实退货节点结构相同的强类型草案。
    proposal = ReturnRequestProposal(
        # 允许审批的唯一写动作。
        action="create_return_request",
        # 已经过本人订单归属和已签收资格预检查。
        order_id="SO100002",
        # 原因供审批界面查看，但审计事件只会保存它所属草案的摘要。
        reason="商品尺寸不合适，希望申请退货",
        # 幂等键同样不会出现在审计事件明文字段中。
        idempotency_key="example-audit-key-001",
        # 明确标记该草案属于写风险。
        risk_level="write",
    )
    # 审批备注只在当前变量中存在，审计仓库接收的是规范化摘要。
    reviewer_comment = "已核对订单归属与签收状态，批准创建"
    # 两个摘要在决定和结果事件中保持一致，证明它们对应同一草案与决定。
    proposal_digest = build_proposal_digest(proposal)
    comment_digest = build_comment_digest(reviewer_comment)
    # 创建隔离仓库；关闭脚本后数据消失。
    repository = InMemoryApprovalAuditRepository()

    # Act：先记录“谁使用哪枚 Token 对哪个草案作出了批准决定”。
    decision_event, _ = repository.append(
        ApprovalAuditDraft(
            # 真实 API 使用 UUID 形式的 LangGraph thread_id。
            thread_id="example-audit-thread-001",
            # 第一条事件是决定记录。
            event_type=ApprovalAuditEventType.DECISION_RECORDED,
            # 关联原 Agent 请求。
            request_id="example-audit-request-001",
            # actor_id 在真实接口中来自验签后的 JWT sub。
            actor_id="reviewer-001",
            # token_jti 用于定位具体访问凭证，但不是完整 JWT。
            token_jti="example-token-jti-001",
            # 只有严格 True 才代表批准。
            approved=True,
            # 目标订单来自可信草案。
            order_id=proposal.order_id,
            # 审计表仅保存不可逆草案摘要。
            proposal_digest=proposal_digest,
            # 审计表仅保存不可逆备注摘要。
            comment_digest=comment_digest,
        )
    )
    # Act：模拟写工具完成，再追加包含真实业务编号的结果事件。
    completed_event, _ = repository.append(
        ApprovalAuditDraft(
            thread_id="example-audit-thread-001",
            event_type=ApprovalAuditEventType.WORKFLOW_COMPLETED,
            request_id="example-audit-request-001",
            actor_id="reviewer-001",
            token_jti="example-token-jti-001",
            approved=True,
            order_id=proposal.order_id,
            proposal_digest=proposal_digest,
            comment_digest=comment_digest,
            # 该编号把安全决定和真实业务结果关联起来。
            return_request_id="RR-ABCDEF123456",
        )
    )

    # Output：先输出第一条事件，观察它引用 64 个零组成的 Genesis Hash。
    print("=== 事件 1：审批决定已记录 ===")
    print(
        json.dumps(
            decision_event.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
    )
    # Output：第二条事件应引用第一条 event_hash。
    print("\n=== 事件 2：工作流已完成 ===")
    print(
        json.dumps(
            completed_event.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
    )
    # Output：明确显示前驱关系，便于初学者理解链式完整性。
    print("\n=== 链式关系 ===")
    print(f"事件 1 event_hash       : {decision_event.event_hash}")
    print(f"事件 2 previous_event_hash: {completed_event.previous_event_hash}")
    print(
        "两者是否相等：",
        completed_event.previous_event_hash == decision_event.event_hash,
    )
    # Output：仓库会重新计算全部位置、内容和前驱摘要。
    print(
        "完整链是否校验通过：",
        repository.verify_thread_chain("example-audit-thread-001"),
    )
    # 提醒观察输出中不存在三个敏感原文字段。
    print("审计事件未保存 reason、idempotency_key、comment 或完整 JWT。")


# 只有右键/命令行直接运行示例时才执行，导入模块不会产生副作用。
if __name__ == "__main__":
    # 调用同步入口；示例不涉及网络或异步 I/O。
    main()

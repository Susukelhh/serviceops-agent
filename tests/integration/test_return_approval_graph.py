"""LangGraph interrupt、人工恢复和退货写工具的完整集成测试。"""

# Any 用于标注 LangGraph 返回中额外存在的 __interrupt__ 框架字段。
from typing import Any

# InMemorySaver 为每个测试图保存可恢复状态，不依赖外部数据库。
from langgraph.checkpoint.memory import InMemorySaver

# Command.resume 提交人工决定；Interrupt 对象通过结果中的 value 暴露审批负载。
from langgraph.types import Command

# 审批负载和流程枚举用于验证中断内容与恢复终态。
from serviceops_agent.domain.returns import ApprovalRequestPayload, ReturnWorkflowStatus

# 图工厂允许注入独立 Checkpointer 和退货仓库。
from serviceops_agent.graph.builder import build_service_graph

# 默认订单数据提供 user-001 的已签收订单 SO100002。
from serviceops_agent.infrastructure.order_repository import default_order_repository

# 独立写仓库的 count 能证明审批前和拒绝后没有隐藏副作用。
from serviceops_agent.infrastructure.return_repository import (
    InMemoryReturnRequestRepository,
)


def _initial_state(*, request_id: str, idempotency_key: str) -> dict[str, object]:
    """构造一条参数完整且有资格进入审批的退货请求状态。"""

    # 身份、原始消息和幂等键都来自可信 API 初始 State，而不是审批恢复值。
    return {
        "request_id": request_id,
        "user_id": "user-001",
        "user_message": "为订单 SO100002 申请退货，原因：商品尺寸不合适",
        "idempotency_key": idempotency_key,
        "events": ["test:request_received"],
    }


def _thread_config(thread_id: str) -> dict[str, dict[str, str]]:
    """构造当前集成测试使用的 Checkpointer 线程配置。"""

    # 恢复调用必须复用完全相同的 thread_id。
    return {"configurable": {"thread_id": thread_id}}


def _approval_payload(result: dict[str, Any]) -> ApprovalRequestPayload:
    """读取并校验一次图中断产生的最小审批负载。"""

    # 当前退货子图只产生一个中断。
    interrupts = result["__interrupt__"]
    # 把 Interrupt.value 再次交给 Pydantic，模拟 API 边界行为。
    return ApprovalRequestPayload.model_validate(interrupts[0].value)


async def test_approved_return_interrupts_before_write_then_resumes_once() -> None:
    """首次执行必须零写入暂停，批准恢复后才创建申请。"""

    # Arrange：仓库和 Saver 都只属于本测试，保证可精确计数。
    repository = InMemoryReturnRequestRepository(default_order_repository)
    graph = build_service_graph(
        return_request_repository=repository,
        checkpointer=InMemorySaver(),
    )
    config = _thread_config("graph-approval-approved-001")

    # Act：首次调用会在 request_return_approval 中 interrupt。
    paused = await graph.ainvoke(
        _initial_state(
            request_id="request-approved-001",
            idempotency_key="graph-approved-001",
        ),
        config=config,
    )
    # Act：解析供审批端查看的安全负载。
    payload = _approval_payload(paused)

    # Assert：中断发生前仓库必须保持零写入。
    assert repository.count() == 0
    # Assert：审批端只能看到实际动作、订单和原因。
    assert payload.action == "create_return_request"
    assert payload.order_id == "SO100002"
    # Assert：可信身份和幂等键不能被审批输入读取或修改。
    assert "user_id" not in payload.model_dump()
    assert "idempotency_key" not in payload.model_dump()

    # Act：使用同一线程提交明确的强类型批准决定。
    completed = await graph.ainvoke(
        Command(
            resume={
                "approved": True,
                "reviewer_id": "reviewer-001",
                "comment": "订单已核验，同意创建申请",
            }
        ),
        config=config,
    )

    # Assert：批准后流程才进入完成态并产生业务编号。
    assert completed["return_workflow_status"] == ReturnWorkflowStatus.COMPLETED
    assert completed["return_request_id"].startswith("RR-")
    assert completed["tool_name"] == "create_return_request"
    # Assert：仓库恰好新增一条记录。
    assert repository.count() == 1


async def test_rejected_return_finishes_without_calling_write_tool() -> None:
    """人工拒绝必须从拒绝终点结束，仓库始终零新增。"""

    # Arrange：创建完全隔离的写仓库、图和审批线程。
    repository = InMemoryReturnRequestRepository(default_order_repository)
    graph = build_service_graph(
        return_request_repository=repository,
        checkpointer=InMemorySaver(),
    )
    config = _thread_config("graph-approval-rejected-001")
    # 首次执行进入 interrupt。
    await graph.ainvoke(
        _initial_state(
            request_id="request-rejected-001",
            idempotency_key="graph-rejected-001",
        ),
        config=config,
    )

    # Act：审批人明确拒绝并恢复原线程。
    rejected = await graph.ainvoke(
        Command(
            resume={
                "approved": False,
                "reviewer_id": "reviewer-002",
                "comment": "当前证据不足，拒绝创建",
            }
        ),
        config=config,
    )

    # Assert：流程停在 rejected，而不是伪装为写入完成。
    assert rejected["return_workflow_status"] == ReturnWorkflowStatus.REJECTED
    assert rejected["agent_stop_reason"] == "return_request_rejected"
    # Assert：拒绝路径从未写入工具名或业务申请编号。
    assert rejected.get("tool_name") is None
    assert rejected.get("return_request_id") is None
    # Assert：仓库保持零记录，这是 Human-in-the-loop 的核心安全性质。
    assert repository.count() == 0


async def test_same_idempotency_key_across_two_approved_threads_replays_record() -> None:
    """两个 HTTP 线程批准相同业务请求时也只能产生一条申请。"""

    # Arrange：两个线程共享同一业务仓库，但使用各自 Checkpoint 状态。
    repository = InMemoryReturnRequestRepository(default_order_repository)
    graph = build_service_graph(
        return_request_repository=repository,
        checkpointer=InMemorySaver(),
    )
    stable_key = "graph-cross-thread-001"

    # 依次模拟同一客户端请求因超时而重新发起两个独立对话线程。
    results: list[dict[str, Any]] = []
    for index in (1, 2):
        # 每个线程有不同 request_id/thread_id，但业务幂等键相同。
        config = _thread_config(f"graph-cross-thread-{index:03d}")
        # 首次执行暂停等待各自审批。
        await graph.ainvoke(
            _initial_state(
                request_id=f"request-cross-thread-{index:03d}",
                idempotency_key=stable_key,
            ),
            config=config,
        )
        # 两个线程都明确批准。
        completed = await graph.ainvoke(
            Command(
                resume={
                    "approved": True,
                    "reviewer_id": "reviewer-003",
                    "comment": "批准幂等重试演示",
                }
            ),
            config=config,
        )
        # 保存完成状态供跨线程比较。
        results.append(completed)

    # Assert：两次批准返回完全相同的业务申请编号。
    assert results[1]["return_request_id"] == results[0]["return_request_id"]
    # Assert：第二次明确记录幂等重放事件。
    assert "graph:return_request_idempotent_replay" in results[1]["events"]
    # Assert：共享仓库最终仍然只有一条申请。
    assert repository.count() == 1

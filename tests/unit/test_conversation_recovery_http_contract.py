"""陈旧工作流恢复 HTTP 响应的低敏边界测试。"""

import pytest
from pydantic import ValidationError

from serviceops_agent.api.schemas import ConversationExecutionRecoveryResponse
from serviceops_agent.domain.conversation import ConversationExecutionRecoveryResult


def test_recovery_response_can_validate_domain_result_without_extra_fields() -> None:
    """路由可直接转换领域统计，响应结构不会附带ID、Token、用户或正文。"""

    result = ConversationExecutionRecoveryResult(
        scanned_count=7,
        accepted_failed_count=1,
        initial_failed_count=2,
        approval_quarantined_count=1,
        legacy_manual_review_count=1,
    )

    response = ConversationExecutionRecoveryResponse.model_validate(result)

    assert response.model_dump() == {
        "scanned_count": 7,
        "accepted_failed_count": 1,
        "initial_failed_count": 2,
        "approval_quarantined_count": 1,
        "legacy_manual_review_count": 1,
    }
    serialized = response.model_dump_json()
    for forbidden_name in (
        "turn_id",
        "claim_token",
        "user_id",
        "user_message",
        "assistant_answer",
    ):
        assert forbidden_name not in serialized


def test_recovery_response_reuses_domain_count_invariants() -> None:
    """HTTP层不能绕过非负边界或让分类合计超过扫描数。"""

    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        ConversationExecutionRecoveryResponse(
            scanned_count=-1,
            accepted_failed_count=0,
            initial_failed_count=0,
            approval_quarantined_count=0,
            legacy_manual_review_count=0,
        )

    with pytest.raises(ValidationError, match="分类合计不能超过扫描数量"):
        ConversationExecutionRecoveryResponse(
            scanned_count=1,
            accepted_failed_count=1,
            initial_failed_count=1,
            approval_quarantined_count=0,
            legacy_manual_review_count=0,
        )

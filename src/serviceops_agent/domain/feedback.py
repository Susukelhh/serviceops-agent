"""用户反馈、失败问题池和知识候选的稳定领域契约。"""

from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FeedbackSignal(StrEnum):
    """进入问题池的有限信号来源。"""

    HELPFUL = "helpful"
    UNHELPFUL = "unhelpful"
    AUTO_HANDOFF = "auto_handoff"


class FeedbackReason(StrEnum):
    """用户可以选择的低敏失败原因，不接收任意自由文本。"""

    INCORRECT = "incorrect"
    MISSING_INFORMATION = "missing_information"
    BAD_CITATION = "bad_citation"
    NOT_RELEVANT = "not_relevant"
    OTHER = "other"


class FeedbackStatus(StrEnum):
    """反馈从待分析到完成处置的生命周期。"""

    OPEN = "open"
    TRIAGED = "triaged"
    KNOWLEDGE_CANDIDATE = "knowledge_candidate"
    DISMISSED = "dismissed"


class FeedbackCategory(StrEnum):
    """人工复盘后的失败归因。"""

    KNOWLEDGE_GAP = "knowledge_gap"
    RETRIEVAL_FAILURE = "retrieval_failure"
    GENERATION_FAILURE = "generation_failure"
    WORKFLOW_FAILURE = "workflow_failure"
    NOT_ACTIONABLE = "not_actionable"


class FeedbackRecord(BaseModel):
    """问题池中的一条可审计、可幂等反馈。"""

    model_config = ConfigDict(extra="forbid")

    feedback_id: UUID
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    conversation_id: UUID
    turn_id: UUID
    owner_user_id: str = Field(min_length=1, max_length=200)
    signal: FeedbackSignal
    reason: FeedbackReason | None = None
    status: FeedbackStatus = FeedbackStatus.OPEN
    category: FeedbackCategory | None = None
    question: str = Field(min_length=1, max_length=4000)
    answer: str | None = Field(default=None, min_length=1, max_length=4000)
    intent: str | None = Field(default=None, min_length=1, max_length=100)
    cited_document_ids: list[str] = Field(default_factory=list, max_length=10)
    reviewer_id: str | None = Field(default=None, min_length=1, max_length=200)
    proposed_title: str | None = Field(default=None, min_length=3, max_length=200)
    proposed_answer: str | None = Field(default=None, min_length=10, max_length=4000)
    created_at: datetime
    updated_at: datetime
    reviewed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_feedback_state(self) -> Self:
        """保证信号、处置状态和知识候选字段彼此一致。"""

        timestamps = [self.created_at, self.updated_at]
        if self.reviewed_at is not None:
            timestamps.append(self.reviewed_at)
        if any(value.utcoffset() is None for value in timestamps):
            raise ValueError("反馈时间必须包含时区")
        if self.updated_at < self.created_at:
            raise ValueError("反馈更新时间不能早于创建时间")
        if self.signal == FeedbackSignal.HELPFUL and self.reason is not None:
            raise ValueError("正向反馈不能携带失败原因")
        if self.signal != FeedbackSignal.HELPFUL and self.reason is None:
            raise ValueError("负向或自动反馈必须包含原因")
        if self.status == FeedbackStatus.OPEN:
            if any(
                value is not None
                for value in (
                    self.category,
                    self.reviewer_id,
                    self.reviewed_at,
                    self.proposed_title,
                    self.proposed_answer,
                )
            ):
                raise ValueError("未处置反馈不能提前包含审核结果")
            return self
        if self.category is None or self.reviewer_id is None or self.reviewed_at is None:
            raise ValueError("已处置反馈必须包含分类、审核人和审核时间")
        if self.status == FeedbackStatus.KNOWLEDGE_CANDIDATE:
            if self.category != FeedbackCategory.KNOWLEDGE_GAP:
                raise ValueError("只有知识缺口可以成为知识候选")
            if self.proposed_title is None or self.proposed_answer is None:
                raise ValueError("知识候选必须包含标题和已审核答案")
        elif self.proposed_title is not None or self.proposed_answer is not None:
            raise ValueError("非知识候选不能保存拟发布知识正文")
        return self


class FeedbackReview(BaseModel):
    """人工复盘时允许写入的有限决定。"""

    model_config = ConfigDict(extra="forbid")

    category: FeedbackCategory
    proposed_title: str | None = Field(default=None, min_length=3, max_length=200)
    proposed_answer: str | None = Field(default=None, min_length=10, max_length=4000)

    @model_validator(mode="after")
    def validate_candidate_payload(self) -> Self:
        """知识缺口必须给出候选内容，其他分类不得夹带正文。"""

        if self.category == FeedbackCategory.KNOWLEDGE_GAP:
            if self.proposed_title is None or self.proposed_answer is None:
                raise ValueError("知识缺口审核必须提供标题和候选答案")
        elif self.proposed_title is not None or self.proposed_answer is not None:
            raise ValueError("只有知识缺口审核可以提供候选知识")
        return self


class KnowledgeCandidate(BaseModel):
    """从人工审核反馈生成、等待离线评测和版本化发布的知识候选。"""

    model_config = ConfigDict(extra="forbid")

    candidate_id: UUID
    source_feedback_id: UUID
    title: str = Field(min_length=3, max_length=200)
    content: str = Field(min_length=10, max_length=4000)
    source_question: str = Field(min_length=1, max_length=4000)
    reviewer_id: str = Field(min_length=1, max_length=200)
    created_at: datetime


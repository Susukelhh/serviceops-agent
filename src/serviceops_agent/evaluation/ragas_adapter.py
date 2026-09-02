"""把 ServiceOps 文档级检索标签适配到 RAGAS 0.4 的标准 ID 指标。"""

import os
import warnings
from collections.abc import Sequence
from importlib import import_module
from importlib.metadata import version
from math import isfinite
from typing import Protocol

from pydantic import BaseModel, Field

from serviceops_agent.evaluation.rag_evaluator import RAGEvaluationCase
from serviceops_agent.rag.retriever import KnowledgeRetriever


class RagasDependencyError(RuntimeError):
    """未安装或无法加载隔离评测依赖时给出可执行提示。"""


class RagasIdMetricRuntime(Protocol):
    """隔离 RAGAS API，使聚合逻辑可在单元测试中注入替身。"""

    @property
    def version(self) -> str:
        """返回实际 RAGAS 包版本。"""

    def score(
        self,
        *,
        retrieved_context_ids: list[str],
        reference_context_ids: list[str],
    ) -> tuple[float, float]:
        """返回 ID-based context precision 与 recall。"""


class Ragas04IdMetricRuntime:
    """通过延迟导入调用固定 0.4 API，生产服务不会加载评测包。"""

    def __init__(self) -> None:
        os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")
        try:
            ragas = import_module("ragas")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                metrics = import_module("ragas.metrics")
                self._precision = metrics.IDBasedContextPrecision()
                self._recall = metrics.IDBasedContextRecall()
            self._sample_type = ragas.SingleTurnSample
            self._version = version("ragas")
        except (ImportError, ModuleNotFoundError) as error:
            raise RagasDependencyError(
                "RAGAS 评测依赖不可用；请运行 uv sync --group eval"
            ) from error

    @property
    def version(self) -> str:
        """报告中记录真正执行指标的包版本。"""

        return self._version

    def score(
        self,
        *,
        retrieved_context_ids: list[str],
        reference_context_ids: list[str],
    ) -> tuple[float, float]:
        """构造官方 SingleTurnSample 并执行两个无需 Judge 的指标。"""

        sample = self._sample_type(
            retrieved_context_ids=retrieved_context_ids,
            reference_context_ids=reference_context_ids,
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            precision = float(self._precision.single_turn_score(sample))
            recall = float(self._recall.single_turn_score(sample))
        # RAGAS 对空检索 Precision 返回 NaN；发布报告需要稳定 JSON，因此明确记为 0。
        return (
            precision if isfinite(precision) else 0.0,
            recall if isfinite(recall) else 0.0,
        )


class RagasRetrievalCaseResult(BaseModel):
    """单条知识内问题的标准 RAGAS ID 指标。"""

    case_id: str
    retrieved_document_ids: list[str]
    reference_document_ids: list[str]
    id_context_precision: float = Field(ge=0.0, le=1.0)
    id_context_recall: float = Field(ge=0.0, le=1.0)


class RagasRetrievalSummary(BaseModel):
    """可与领域检索报告并排保存的 RAGAS 适配层结果。"""

    adapter_version: str = "ragas-id-retrieval-v1"
    ragas_version: str
    total_source_cases: int = Field(ge=1)
    evaluated_positive_cases: int = Field(ge=1)
    excluded_negative_cases: int = Field(ge=0)
    mean_id_context_precision: float = Field(ge=0.0, le=1.0)
    mean_id_context_recall: float = Field(ge=0.0, le=1.0)
    advisory_only: bool = True
    results: list[RagasRetrievalCaseResult]


def evaluate_retrieval_with_ragas(
    retriever: KnowledgeRetriever,
    cases: Sequence[RAGEvaluationCase],
    *,
    top_k: int,
    runtime: RagasIdMetricRuntime | None = None,
) -> RagasRetrievalSummary:
    """对知识内样本运行同一检索器，并用 RAGAS 计算可比较的文档 ID 指标。"""

    if top_k < 1:
        raise ValueError("top_k 必须大于等于 1")
    if not cases:
        raise ValueError("评测样本不能为空")
    positive_cases = [case for case in cases if case.should_retrieve]
    if not positive_cases:
        raise ValueError("RAGAS 文档 ID 指标至少需要一个知识内正例")
    metric_runtime = runtime or Ragas04IdMetricRuntime()
    results: list[RagasRetrievalCaseResult] = []
    for case in positive_cases:
        hits = retriever.search(case.question, top_k=top_k)
        retrieved_ids = list(dict.fromkeys(hit.chunk.document_id for hit in hits))
        precision, recall = metric_runtime.score(
            retrieved_context_ids=retrieved_ids,
            reference_context_ids=case.expected_document_ids,
        )
        results.append(
            RagasRetrievalCaseResult(
                case_id=case.case_id,
                retrieved_document_ids=retrieved_ids,
                reference_document_ids=case.expected_document_ids,
                id_context_precision=precision,
                id_context_recall=recall,
            )
        )
    return RagasRetrievalSummary(
        ragas_version=metric_runtime.version,
        total_source_cases=len(cases),
        evaluated_positive_cases=len(positive_cases),
        excluded_negative_cases=len(cases) - len(positive_cases),
        mean_id_context_precision=(
            sum(result.id_context_precision for result in results) / len(results)
        ),
        mean_id_context_recall=(
            sum(result.id_context_recall for result in results) / len(results)
        ),
        results=results,
    )

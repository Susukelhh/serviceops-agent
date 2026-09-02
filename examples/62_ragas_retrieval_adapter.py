"""第 62 步：同一离线检索集并排运行领域指标和 RAGAS 标准 ID 指标。"""

import os
from pathlib import Path

os.environ["SERVICEOPS_LLM_BACKEND"] = "mock"
os.environ["SERVICEOPS_EMBEDDING_BACKEND"] = "hash"
os.environ["SERVICEOPS_RAG_GENERATION_BACKEND"] = "extractive"
os.environ["SERVICEOPS_QDRANT_LOCATION"] = ":memory:"
os.environ["RAGAS_DO_NOT_TRACK"] = "true"

from serviceops_agent.config.paths import PROJECT_ROOT
from serviceops_agent.evaluation import (
    evaluate_retrieval_with_ragas,
    evaluate_retriever,
    load_rag_evaluation_cases,
)
from serviceops_agent.rag.retriever import build_default_knowledge_retriever


def main() -> None:
    """运行两套互补指标，证明 RAGAS 是适配层而不是领域门禁替代品。"""

    dataset_path: Path = PROJECT_ROOT / "data/evaluation/rag_retrieval_cases.json"
    cases = load_rag_evaluation_cases(dataset_path)
    retriever = build_default_knowledge_retriever()
    domain = evaluate_retriever(retriever, cases, top_k=3)
    ragas = evaluate_retrieval_with_ragas(retriever, cases, top_k=3)

    print("=== ServiceOps 领域检索门禁 ===")
    print(f"Recall@3: {domain.recall_at_k:.3f}")
    print(f"MRR@3: {domain.mrr_at_k:.3f}")
    print(f"负例误召回率: {domain.false_positive_rate:.3f}")
    print("\n=== RAGAS 0.4 标准适配指标（advisory） ===")
    print(f"RAGAS: {ragas.ragas_version}")
    print(f"ID Context Precision: {ragas.mean_id_context_precision:.3f}")
    print(f"ID Context Recall: {ragas.mean_id_context_recall:.3f}")
    print(f"排除的域外负例: {ragas.excluded_negative_cases}")


if __name__ == "__main__":
    main()

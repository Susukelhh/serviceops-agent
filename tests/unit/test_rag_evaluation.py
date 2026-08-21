"""RAG 离线评测数据集和指标计算的回归测试。"""

# Path 指向版本控制中的人工标注评测集。
from pathlib import Path

# Settings 显式固定本地评测参数，不读取开发者真实模型配置。
from serviceops_agent.config.settings import Settings

# 加载与执行函数是本文件的主要测试目标。
from serviceops_agent.evaluation import evaluate_retriever, load_rag_evaluation_cases

# 默认检索器组装受治理知识、Hash Embedding 和内存 Qdrant。
from serviceops_agent.rag.retriever import build_default_knowledge_retriever

# 标准评测集相对于 pytest 项目根目录保存。
EVALUATION_DATASET = Path("data/evaluation/rag_retrieval_cases.json")


def test_hash_retrieval_baseline_meets_offline_quality_gate() -> None:
    """本地零费用基线必须持续通过当前小型知识库的检索质量门槛。"""

    # Arrange：加载并校验 8 条知识内正例和 3 条知识外负例。
    cases = load_rag_evaluation_cases(EVALUATION_DATASET)
    # 使用独立内存 Collection，测试不会复用或污染开发运行索引。
    retriever = build_default_knowledge_retriever(
        Settings(
            # 固定使用本地确定性 Hash Embedding。
            embedding_backend="hash",
            # 1024 维与项目默认开发配置一致。
            embedding_dimensions=1024,
            # 内存模式保证测试无文件副作用。
            qdrant_location=":memory:",
            # 使用测试专属 Collection 名称。
            qdrant_collection="test_rag_evaluation",
            # 0.10 是由当前正负例共同校准的证据阈值。
            rag_score_threshold=0.10,
        )
    )

    # Act：以 K=3 执行完整数据集并生成强类型指标报告。
    summary = evaluate_retriever(retriever, cases, top_k=3)

    # Assert：数据集规模变化会迫使维护者同步审查质量门槛和测试说明。
    assert summary.total_cases == 11
    # Assert：当前包含八条知识库应覆盖的正例。
    assert summary.positive_cases == 8
    # Assert：当前包含三条必须拒绝的域外或内部知识负例。
    assert summary.negative_cases == 3
    # Assert：所有正例都至少召回一个人工标注期望文档。
    assert summary.recall_at_k == 1.0
    # Assert：所有期望文档都排在第一名，MRR 因此为满分。
    assert summary.mrr_at_k == 1.0
    # Assert：全部正例第一名都是人工期望文档。
    assert summary.top_1_accuracy == 1.0
    # Assert：当前简单集的相关文档均处于理想排名，nDCG因此为满分。
    assert summary.ndcg_at_k == 1.0
    # Assert：正例召回和负例拒绝全部正确。
    assert summary.decision_accuracy == 1.0
    # Assert：天气、写诗和内部草稿制度均没有越过检索阈值。
    assert summary.false_positive_rate == 0.0
    # Assert：逐样本明细全部通过，失败时可直接定位 case_id。
    assert all(result.passed for result in summary.results)

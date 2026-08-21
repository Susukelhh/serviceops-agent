"""第五步示例：运行离线 RAG 检索评测并查看逐样本结果。

运行方式：

    uv run python examples/05_rag_evaluation.py

该文件在导入项目模块前强制使用 Hash Embedding 和内存 Qdrant，不会调用千问或产生费用。
"""

# os 用进程环境变量覆盖本机 `.env`，确保示例始终是可重复的离线基线。
import os

# Path 用绝对项目路径定位评测数据，不依赖 PyCharm Working directory。
from pathlib import Path

# 强制使用关键词分类后端；本示例虽不运行图，也避免 builder 被间接导入时调用模型。
os.environ["SERVICEOPS_LLM_BACKEND"] = "mock"
# 评测目标固定为本地 Hash Embedding 基线。
os.environ["SERVICEOPS_EMBEDDING_BACKEND"] = "hash"
# 默认回答器保持摘录模式，示例只评估检索而不消耗生成 Token。
os.environ["SERVICEOPS_RAG_GENERATION_BACKEND"] = "extractive"
# 每次运行创建全新内存索引，结果不受旧 Collection 影响。
os.environ["SERVICEOPS_QDRANT_LOCATION"] = ":memory:"

# PROJECT_ROOT 是根据 src 包位置推导的项目根目录，与启动位置无关。
from serviceops_agent.config.paths import PROJECT_ROOT

# 评测包提供数据加载、指标计算和强类型结果。
from serviceops_agent.evaluation import evaluate_retriever, load_rag_evaluation_cases

# 默认检索器会加载受治理知识源并建立本地 Qdrant 索引。
from serviceops_agent.rag.retriever import build_default_knowledge_retriever


def main() -> None:
    """执行标准检索集，并在 PyCharm 控制台打印指标与失败明细。"""

    # 使用项目绝对路径定位评测集，避免从其他目录启动时出现 FileNotFoundError。
    dataset_path: Path = PROJECT_ROOT / "data/evaluation/rag_retrieval_cases.json"
    # Pydantic 会校验每条正负标签、问题和期望文档 ID。
    cases = load_rag_evaluation_cases(dataset_path)
    # 当前环境变量已经固定 Hash Embedding 与内存 Qdrant。
    retriever = build_default_knowledge_retriever()
    # Top-K 与 API FAQ 默认候选数保持一致。
    summary = evaluate_retriever(retriever, cases, top_k=3)

    # 打印清晰的聚合指标标题。
    print("=== ServiceOps RAG 离线检索评测 ===")
    # 展示数据集组成，避免只看百分比而忽略样本规模。
    print(
        f"样本：{summary.total_cases}（正例 {summary.positive_cases} / "
        f"负例 {summary.negative_cases}）"
    )
    # Recall@K 衡量知识内问题是否找得到正确文档。
    print(f"Recall@{summary.top_k}: {summary.recall_at_k:.3f}")
    # MRR@K 衡量正确文档是否尽量排在前面。
    print(f"MRR@{summary.top_k}: {summary.mrr_at_k:.3f}")
    # Top-1 更严格地要求正确文档无需二次排序就位于第一名。
    print(f"Top-1 准确率：{summary.top_1_accuracy:.3f}")
    # nDCG 同时考虑相关文档是否出现以及出现位置。
    print(f"nDCG@{summary.top_k}: {summary.ndcg_at_k:.3f}")
    # 决策准确率同时覆盖正例命中和负例拒绝。
    print(f"检索决策准确率：{summary.decision_accuracy:.3f}")
    # 误召回率反映阈值是否放进了不相关证据。
    print(f"负例误召回率：{summary.false_positive_rate:.3f}")

    # 打印逐样本结果，让学习者可以观察排名和错误案例，而不只背指标定义。
    print("\n=== 逐样本结果 ===")
    # 保持 JSON 数据集原有顺序输出。
    for result in summary.results:
        # PASS/FAIL 便于在长控制台输出中快速扫描失败样本。
        status = "PASS" if result.passed else "FAIL"
        # 打印稳定 case_id、实际文档排名和首个相关名次。
        print(
            f"[{status}] {result.case_id}: retrieved={result.retrieved_document_ids}, "
            f"first_relevant_rank={result.first_relevant_rank}"
        )


# 直接运行示例时执行 main，被其他模块导入时不会自动建索引。
if __name__ == "__main__":
    # 评测器当前是同步接口，因此不需要 asyncio 事件循环。
    main()

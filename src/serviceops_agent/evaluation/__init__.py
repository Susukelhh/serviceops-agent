"""ServiceOps Agent 的离线评测工具包。"""

# 从包入口导出常用类型，让示例和后续 CI 不依赖模块内部路径。
from serviceops_agent.evaluation.agent_evaluator import (
    AgentEvaluationCase,
    AgentEvaluationCaseResult,
    AgentEvaluationDataset,
    AgentEvaluationSummary,
    AgentEvaluationThresholds,
    build_offline_agent_evaluation_target,
    evaluate_agent_dataset,
    load_agent_evaluation_dataset,
)
from serviceops_agent.evaluation.experiment import (
    QWEN_CANDIDATE_PROFILE,
    AgentCandidateExperimentConfig,
    AgentCandidateExperimentSummary,
    AgentCaseStabilityResult,
    CandidatePromotionThresholds,
    build_qwen_candidate_evaluation_target,
    estimate_planned_qwen_chat_calls,
    load_candidate_experiment_config,
    override_candidate_trial_count,
    run_candidate_experiment,
    summarize_candidate_experiment,
)
from serviceops_agent.evaluation.grounding_sufficiency_experiment import (
    GroundingCaseResult,
    GroundingEvaluationCase,
    GroundingEvaluationSummary,
    GroundingEvidenceReference,
    GroundingQualityGate,
    GroundingSufficiencyExperimentConfig,
    GroundingSufficiencyExperimentReport,
    evaluate_grounding_client,
    grounding_prompt_sha256,
    load_grounding_evaluation_cases,
    load_grounding_sufficiency_experiment_config,
    run_grounding_sufficiency_experiment,
)
from serviceops_agent.evaluation.rag_end_to_end_experiment import (
    RAGEndToEndCase,
    RAGEndToEndCaseResult,
    RAGEndToEndExperimentConfig,
    RAGEndToEndExperimentReport,
    RAGEndToEndQualityGate,
    RAGEndToEndSummary,
    evaluate_rag_end_to_end_pipeline,
    load_rag_end_to_end_cases,
    load_rag_end_to_end_experiment_config,
    rag_end_to_end_candidate_fingerprint,
    run_rag_end_to_end_experiment,
)
from serviceops_agent.evaluation.rag_evaluator import (
    RAGEvaluationCase,
    RAGEvaluationCaseResult,
    RAGEvaluationSummary,
    evaluate_retriever,
    load_rag_evaluation_cases,
)
from serviceops_agent.evaluation.rag_experiment import (
    RAGBaselineCaseDiagnosis,
    RAGBaselineContract,
    RAGBaselineIssue,
    RAGOfflineBaselineProfile,
    RAGProblemBaselineConfig,
    RAGProblemBaselineReport,
    load_rag_problem_baseline_config,
    run_rag_problem_baseline,
)
from serviceops_agent.evaluation.rag_rerank_experiment import (
    RAGRerankExperimentConfig,
    RAGRerankExperimentReport,
    RAGRerankProfileResult,
    RAGRerankQualityGate,
    load_rag_rerank_experiment_config,
    run_rag_rerank_experiment,
)
from serviceops_agent.evaluation.rag_scope_experiment import (
    RAGScopeExperimentConfig,
    RAGScopeExperimentReport,
    RAGScopeProfileResult,
    RAGScopeQualityGate,
    load_rag_scope_experiment_config,
    run_rag_scope_experiment,
)
from serviceops_agent.evaluation.rag_semantic_embedding_experiment import (
    RAGSemanticEmbeddingExperimentConfig,
    RAGSemanticEmbeddingExperimentReport,
    RAGSemanticProfileResult,
    RAGSemanticQualityGate,
    load_rag_semantic_embedding_experiment_config,
    run_rag_semantic_embedding_experiment,
)

# __all__ 明确该包承诺稳定支持的公共接口。
__all__ = [
    # 单条端到端人工标注样本。
    "AgentEvaluationCase",
    # 单条整图运行的实际结果与四维评分。
    "AgentEvaluationCaseResult",
    # 带版本、描述、阈值和样本的完整数据集。
    "AgentEvaluationDataset",
    # 聚合质量指标、P95 和质量门结论。
    "AgentEvaluationSummary",
    # 与数据集一起版本化的质量门。
    "AgentEvaluationThresholds",
    # 真实候选实验的版本、轮数和晋级门配置。
    "AgentCandidateExperimentConfig",
    # 真实候选实验的多轮聚合报告。
    "AgentCandidateExperimentSummary",
    # 单一黄金样本的跨轮稳定性结果。
    "AgentCaseStabilityResult",
    # 候选晋级聚合门阈值。
    "CandidatePromotionThresholds",
    "GroundingCaseResult",
    "GroundingEvaluationCase",
    "GroundingEvaluationSummary",
    "GroundingEvidenceReference",
    "GroundingQualityGate",
    "GroundingSufficiencyExperimentConfig",
    "GroundingSufficiencyExperimentReport",
    # 真实千问聊天 + 确定性检索的稳定 profile 名称。
    "QWEN_CANDIDATE_PROFILE",
    # 单条人工标注评测样本。
    "RAGEvaluationCase",
    # 单条检索运行结果。
    "RAGEvaluationCaseResult",
    # 聚合后的质量指标。
    "RAGEvaluationSummary",
    "RAGEndToEndCase",
    "RAGEndToEndCaseResult",
    "RAGEndToEndExperimentConfig",
    "RAGEndToEndExperimentReport",
    "RAGEndToEndQualityGate",
    "RAGEndToEndSummary",
    # 第24步单条困难Baseline诊断。
    "RAGBaselineCaseDiagnosis",
    # 第24步证明实验规模和失败暴露有效的最低契约。
    "RAGBaselineContract",
    # 第24步有限失败类型。
    "RAGBaselineIssue",
    # 零费用旧方案参数快照。
    "RAGOfflineBaselineProfile",
    # 版本控制的问题驱动Baseline配置。
    "RAGProblemBaselineConfig",
    # 第24步可保存的完整实验报告。
    "RAGProblemBaselineReport",
    "RAGRerankExperimentConfig",
    "RAGRerankExperimentReport",
    "RAGRerankProfileResult",
    "RAGRerankQualityGate",
    "RAGScopeExperimentConfig",
    "RAGScopeExperimentReport",
    "RAGScopeProfileResult",
    "RAGScopeQualityGate",
    "RAGSemanticEmbeddingExperimentConfig",
    "RAGSemanticEmbeddingExperimentReport",
    "RAGSemanticProfileResult",
    "RAGSemanticQualityGate",
    # 构建完全离线且资源隔离的整图评测目标。
    "build_offline_agent_evaluation_target",
    # 构建真实千问聊天决策但副作用隔离的候选整图。
    "build_qwen_candidate_evaluation_target",
    # 运行完整 LangGraph 并计算端到端指标。
    "evaluate_agent_dataset",
    "evaluate_grounding_client",
    # 按黄金参考路径估算候选聊天模型调用量。
    "estimate_planned_qwen_chat_calls",
    # 执行整个 RAG 检索数据集的评测函数。
    "evaluate_retriever",
    "evaluate_rag_end_to_end_pipeline",
    # 从 UTF-8 JSON 加载端到端数据集。
    "load_agent_evaluation_dataset",
    # 从 UTF-8 JSON 加载版本化候选实验配置。
    "load_candidate_experiment_config",
    "load_grounding_evaluation_cases",
    "load_grounding_sufficiency_experiment_config",
    # 从受版本控制 JSON 加载评测集。
    "load_rag_evaluation_cases",
    "load_rag_end_to_end_cases",
    "load_rag_end_to_end_experiment_config",
    # 加载第24步版本化实验契约。
    "load_rag_problem_baseline_config",
    "load_rag_rerank_experiment_config",
    "load_rag_scope_experiment_config",
    "load_rag_semantic_embedding_experiment_config",
    # 用受校验命令行值覆盖候选重复轮数。
    "override_candidate_trial_count",
    # 运行基线和真实候选多轮实验。
    "run_candidate_experiment",
    "run_grounding_sufficiency_experiment",
    # 运行第24步零费用困难Baseline。
    "run_rag_problem_baseline",
    "run_rag_rerank_experiment",
    "run_rag_scope_experiment",
    "run_rag_semantic_embedding_experiment",
    "run_rag_end_to_end_experiment",
    # 从既有单轮结果聚合稳定性和晋级结论。
    "summarize_candidate_experiment",
    "grounding_prompt_sha256",
    "rag_end_to_end_candidate_fingerprint",
]

"""第24步问题驱动 RAG 困难基线的配置、运行、诊断与报告模型。"""

# datetime/timezone 为实验报告写入明确 UTC 时间，避免本地时区造成歧义。
from datetime import UTC, datetime

# StrEnum 为有限失败类型提供可读且可序列化的字符串值。
from enum import StrEnum

# Path 负责读取版本化实验配置和确认锁定测试集存在。
from pathlib import Path

# Literal 将离线基线限制为零费用 Hash Embedding，防止脚本意外调用付费接口。
from typing import Literal

# BaseModel/Field 提供配置与报告边界；TypeAdapter 校验完整知识文档数组。
from pydantic import BaseModel, Field, TypeAdapter

# resolve_project_path 让 PyCharm 从任意工作目录启动都能找到实验数据。
from serviceops_agent.config.paths import resolve_project_path

# Settings 显式组装完全离线且不受本机千问开关影响的实验环境。
from serviceops_agent.config.settings import Settings

# 知识领域对象用于统计原始文档、可索引文档和真实切片数量。
from serviceops_agent.domain.knowledge import KnowledgeDocument

# 复用已经实现并测试的检索评测数据结构与计算函数。
from serviceops_agent.evaluation.rag_evaluator import (
    RAGEvaluationCase,
    RAGEvaluationSummary,
    evaluate_retriever,
    load_rag_evaluation_cases,
)

# JsonKnowledgeRepository 执行 published + public 的索引前治理过滤。
from serviceops_agent.infrastructure.knowledge_repository import JsonKnowledgeRepository

# KnowledgeChunker 用与检索器相同的参数统计本轮实际 Chunk，而不是估算数量。
from serviceops_agent.rag.chunking import KnowledgeChunker

# build_default_knowledge_retriever 构建真实 Qdrant 内存索引并执行完整检索链路。
from serviceops_agent.rag.retriever import build_default_knowledge_retriever


class RAGBaselineIssue(StrEnum):
    """Baseline 单条样本的有限诊断类别。"""

    # PASSED 表示正例召回或负例拒绝符合人工标签。
    PASSED = "passed"
    # RELEVANT_DOCUMENT_MISSING 表示 Top-K 中完全没有人工期望文档。
    RELEVANT_DOCUMENT_MISSING = "relevant_document_missing"
    # IRRELEVANT_EVIDENCE_RETURNED 表示知识库外问题仍越过阈值返回了证据。
    IRRELEVANT_EVIDENCE_RETURNED = "irrelevant_evidence_returned"
    # RELEVANT_DOCUMENT_RANKED_LOW 表示虽然召回成功，但第一名不是正确文档。
    RELEVANT_DOCUMENT_RANKED_LOW = "relevant_document_ranked_low"


class RAGOfflineBaselineProfile(BaseModel):
    """当前旧方案的固定参数，后续候选必须只改变声明过的变量。"""

    # profile_id 是报告和简历实验记录中的稳定方案名称。
    profile_id: str = Field(min_length=1, max_length=100)
    # embedding_backend 被类型系统固定为 hash，第一阶段绝不产生模型费用。
    embedding_backend: Literal["hash"] = "hash"
    # embedding_dimensions 与旧项目默认配置保持一致，形成可比较基线。
    embedding_dimensions: int = Field(default=1024, ge=64, le=2048)
    # chunk_size 是旧方案的最大字符窗口，而不是 Token 数。
    chunk_size: int = Field(default=500, ge=100, le=2000)
    # chunk_overlap 是旧方案相邻窗口重复字符数。
    chunk_overlap: int = Field(default=80, ge=0, le=500)
    # top_k 控制每条问题最多返回多少个候选切片。
    top_k: int = Field(default=5, ge=1, le=20)
    # score_threshold 过滤词面碰撞造成的低分候选。
    score_threshold: float = Field(default=0.10, ge=0.0, le=1.0)


class RAGBaselineContract(BaseModel):
    """证明实验规模不再是四个 Chunk、十一道简单题的最低条件。"""

    # min_total_documents 要求原始语料包含发布、内部和退役等治理状态。
    min_total_documents: int = Field(default=12, ge=1)
    # min_indexable_documents 要求公共活动文档具有足够相邻主题干扰。
    min_indexable_documents: int = Field(default=10, ge=1)
    # min_chunks 要求默认500字符窗口真正发生切片，而不是一文档一Chunk。
    min_chunks: int = Field(default=20, ge=1)
    # min_development_cases 限制调参集不能退化成少量演示问题。
    min_development_cases: int = Field(default=24, ge=1)
    # min_positive_cases 保证知识内同义、精确和跨段问题具有基本覆盖。
    min_positive_cases: int = Field(default=16, ge=1)
    # min_negative_cases 保证阈值同时面对知识外和高词面重合负例。
    min_negative_cases: int = Field(default=6, ge=1)
    # min_baseline_failures 强制旧方案暴露问题；满分反而说明数据设计无效。
    min_baseline_failures: int = Field(default=1, ge=1)


class RAGProblemBaselineConfig(BaseModel):
    """版本控制中的困难基线实验契约。"""

    # experiment_id 跨版本标识同一个问题驱动实验系列。
    experiment_id: str = Field(min_length=1, max_length=100)
    # version 在语料、标签或实验规则变化时递增。
    version: str = Field(min_length=1, max_length=50)
    # description 说明当前实验要暴露的问题，而不是宣传技术名词。
    description: str = Field(min_length=1, max_length=1000)
    # corpus_path 指向独立实验语料，不修改v1在线演示使用的种子知识。
    corpus_path: str = Field(min_length=1, max_length=500)
    # development_dataset_path 是可以反复用于诊断和调参的开发集。
    development_dataset_path: str = Field(min_length=1, max_length=500)
    # holdout_dataset_path 只在所有候选参数选定后用于最终一次对比。
    holdout_dataset_path: str = Field(min_length=1, max_length=500)
    # baseline 固定当前旧方案的全部可变参数。
    baseline: RAGOfflineBaselineProfile
    # contract 定义什么样的语料和失败才算有效实验。
    contract: RAGBaselineContract


class RAGBaselineCaseDiagnosis(BaseModel):
    """不保存问题原文的单条Baseline诊断。"""

    # case_id 关联版本控制开发集中的人工样本。
    case_id: str
    # difficulty 保留样本难度，方便面试时解释失败是否来自困难集。
    difficulty: Literal["basic", "hard", "adversarial"]
    # tags 记录同义改写、精确匹配、权限等可聚合问题类型。
    tags: list[str]
    # issue 是本条主要诊断结果。
    issue: RAGBaselineIssue
    # retrieved_document_ids 按实际排名保存文档ID，不复制知识正文。
    retrieved_document_ids: list[str]
    # first_relevant_rank 用于区分完全漏召回和排序靠后。
    first_relevant_rank: int | None = Field(default=None, ge=1)


class RAGProblemBaselineReport(BaseModel):
    """可以进入 data/runtime 的完整困难基线报告。"""

    # experiment_id 和 version 共同定位本轮实验契约。
    experiment_id: str
    # experiment_version 与配置文件版本完全一致。
    experiment_version: str
    # generated_at 使用UTC ISO时间，报告之间可按生成顺序追踪。
    generated_at: datetime
    # baseline_profile_id 指明本轮仍是旧Hash方案，而非优化候选。
    baseline_profile_id: str
    # paid_api_called 必须固定为False，防止离线脚本产生费用误解。
    paid_api_called: Literal[False] = False
    # total_documents 包含内部草稿和退役版本，用于验证治理过滤。
    total_documents: int = Field(ge=1)
    # indexable_documents 只统计published + public活动文档。
    indexable_documents: int = Field(ge=1)
    # chunk_count 是使用Baseline窗口实际产生的切片数量。
    chunk_count: int = Field(ge=1)
    # development_case_count 是本轮真正运行的调参集规模。
    development_case_count: int = Field(ge=1)
    # holdout_case_count 只证明锁定集存在，本轮不会执行它。
    holdout_case_count: int = Field(ge=1)
    # metrics 复用Recall、MRR、Top-1、nDCG、负例误召回等统一指标。
    metrics: RAGEvaluationSummary
    # failed_case_count 只统计正例漏召回和负例误召回。
    failed_case_count: int = Field(ge=0)
    # ranking_opportunity_count 统计召回但首位错误的优化机会。
    ranking_opportunity_count: int = Field(ge=0)
    # diagnoses 保存全部样本的有限结果，支持按标签定位根因。
    diagnoses: list[RAGBaselineCaseDiagnosis]
    # experiment_contract_passed 表示语料规模和失败暴露均满足实验要求。
    experiment_contract_passed: bool
    # experiment_contract_failures 给出不满足要求的稳定原因码。
    experiment_contract_failures: list[str]


def load_rag_problem_baseline_config(path: Path) -> RAGProblemBaselineConfig:
    """读取并验证第24步版本化实验契约。"""

    # 明确UTF-8读取中文描述，避免Windows默认编码差异。
    raw_json = path.read_text(encoding="utf-8")
    # Pydantic同时校验路径、参数范围和离线Embedding约束。
    return RAGProblemBaselineConfig.model_validate_json(raw_json)


def _diagnose_cases(
    cases: list[RAGEvaluationCase],
    metrics: RAGEvaluationSummary,
) -> list[RAGBaselineCaseDiagnosis]:
    """把通用检索结果转换成可行动的Baseline问题类型。"""

    # cases_by_id 让报告结果与人工标签按稳定ID关联，不依赖列表偶然位置。
    cases_by_id = {case.case_id: case for case in cases}
    # diagnoses 按评测器原始顺序保存，保证JSON diff稳定。
    diagnoses: list[RAGBaselineCaseDiagnosis] = []
    # 逐条读取实际检索结果并判断首要问题。
    for result in metrics.results:
        # 每个结果ID都来自刚加载的数据集，因此映射一定存在。
        case = cases_by_id[result.case_id]
        # 正例完全没有期望文档时，问题属于召回阶段。
        if case.should_retrieve and result.first_relevant_rank is None:
            # 该类型后续优先检查切片、Embedding、混合召回和查询改写。
            issue = RAGBaselineIssue.RELEVANT_DOCUMENT_MISSING
        # 负例返回任意文档时，问题属于阈值或高词面碰撞误召回。
        elif not case.should_retrieve and result.retrieved_document_ids:
            # 该类型后续优先检查负例阈值、意图路由和拒答门。
            issue = RAGBaselineIssue.IRRELEVANT_EVIDENCE_RETURNED
        # 正例虽成功召回但没有排在第一位，记录为排序优化机会。
        elif case.should_retrieve and result.first_relevant_rank != 1:
            # 该类型是RRF或Rerank候选问题，不算当前通用Recall失败。
            issue = RAGBaselineIssue.RELEVANT_DOCUMENT_RANKED_LOW
        else:
            # 其余样本满足当前标签与排名要求。
            issue = RAGBaselineIssue.PASSED
        # 保存有限诊断，不复制问题原文或知识正文。
        diagnoses.append(
            RAGBaselineCaseDiagnosis(
                # 复制稳定样本ID。
                case_id=case.case_id,
                # 复制人工难度标签。
                difficulty=case.difficulty,
                # 使用新列表避免报告对象共享可变标签引用。
                tags=list(case.tags),
                # 写入上面确定的主要问题类型。
                issue=issue,
                # 使用新列表保存实际文档排名。
                retrieved_document_ids=list(result.retrieved_document_ids),
                # 保留首个相关文档名次。
                first_relevant_rank=result.first_relevant_rank,
            )
        )
    # 返回可直接进入强类型报告的完整诊断列表。
    return diagnoses


def run_rag_problem_baseline(
    config: RAGProblemBaselineConfig,
) -> RAGProblemBaselineReport:
    """运行零费用困难Baseline，并验证实验是否真的暴露旧方案问题。"""

    # corpus_path 从项目根解析，避免PyCharm Working directory影响实验。
    corpus_path = resolve_project_path(config.corpus_path)
    # development_path 是本轮允许反复运行的开发集。
    development_path = resolve_project_path(config.development_dataset_path)
    # holdout_path 只读取Schema和数量，不运行检索，防止调参阶段反复偷看结果。
    holdout_path = resolve_project_path(config.holdout_dataset_path)

    # raw_corpus 保存治理前全部文档，用于检查内部和退役文档确实存在。
    raw_corpus = TypeAdapter(list[KnowledgeDocument]).validate_json(
        # 明确UTF-8读取版本化实验语料。
        corpus_path.read_text(encoding="utf-8")
    )
    # repository 使用与真实应用相同的发布状态和访问范围过滤。
    repository = JsonKnowledgeRepository(corpus_path)
    # indexable_documents 是最终允许进入公共Qdrant索引的文档。
    indexable_documents = repository.list_indexable_documents()
    # chunker 固定旧方案参数，后续候选会显式创建不同profile。
    chunker = KnowledgeChunker(
        # 旧方案按字符切分而不是按Token切分。
        chunk_size=config.baseline.chunk_size,
        # 相邻窗口保留固定字符重叠。
        chunk_overlap=config.baseline.chunk_overlap,
    )
    # chunks 真实执行切分，用于报告规模和检查Overlap不再形同虚设。
    chunks = chunker.split_documents(indexable_documents)

    # development_cases 是本轮真正送入检索器的人工开发集。
    development_cases = load_rag_evaluation_cases(development_path)
    # holdout_cases 只做Pydantic校验与数量统计，本函数不会对它执行search。
    holdout_cases = load_rag_evaluation_cases(holdout_path)

    # settings 显式覆盖所有会触发模型调用或磁盘状态的配置。
    settings = Settings(
        # 分类器固定mock，虽然本脚本不装配整张图，也防止间接付费。
        llm_backend="mock",
        # 回答器固定摘录模式，本阶段只评估检索。
        rag_generation_backend="extractive",
        # Embedding严格使用确定性Hash Baseline。
        embedding_backend=config.baseline.embedding_backend,
        # 维度沿用版本化profile。
        embedding_dimensions=config.baseline.embedding_dimensions,
        # 内存Qdrant使每轮索引从干净状态开始。
        qdrant_location=":memory:",
        # Collection名称包含实验版本，便于日志阅读且不会碰撞应用集合。
        qdrant_collection=f"rag_problem_baseline_{config.version.replace('.', '_')}",
        # 实验语料路径显式覆盖v1种子知识。
        knowledge_source_path=config.corpus_path,
        # 阈值是待评估的旧方案参数，不能根据当前结果临时修改。
        rag_score_threshold=config.baseline.score_threshold,
        # 第24步必须保持策略关闭，避免新范围门改写已经冻结的旧Baseline。
        rag_query_policy="off",
        # K值与报告profile保持一致。
        rag_top_k=config.baseline.top_k,
        # 检索器建库必须使用与上面统计完全一致的窗口。
        rag_chunk_size=config.baseline.chunk_size,
        # 检索器建库必须使用与上面统计完全一致的重叠。
        rag_chunk_overlap=config.baseline.chunk_overlap,
    )
    # retriever 真实创建Qdrant Collection、向量化全部切片并写入Point。
    retriever = build_default_knowledge_retriever(settings)
    # metrics 对全部开发样本运行真实search并计算统一指标。
    metrics = evaluate_retriever(
        # 传入刚构建的离线Qdrant检索器。
        retriever,
        # 只使用可调开发集，不运行锁定测试集。
        development_cases,
        # K值固定来自Baseline profile。
        top_k=config.baseline.top_k,
    )
    # diagnoses 将聚合分数展开成召回、误召回与排序问题。
    diagnoses = _diagnose_cases(development_cases, metrics)
    # failed_issues 定义真正使通用检索决策失败的两类问题。
    failed_issues = {
        # 正例漏掉相关文档。
        RAGBaselineIssue.RELEVANT_DOCUMENT_MISSING,
        # 负例错误返回证据。
        RAGBaselineIssue.IRRELEVANT_EVIDENCE_RETURNED,
    }
    # failed_case_count 统计需要通过后续技术选择解决的实际失败。
    failed_case_count = sum(
        # 布尔值在sum中按0/1累计。
        diagnosis.issue in failed_issues
        # 遍历全部有限诊断。
        for diagnosis in diagnoses
    )
    # ranking_opportunity_count 单独统计召回成功但首位错误的样本。
    ranking_opportunity_count = sum(
        # 只有明确低排名类型计入排序机会。
        diagnosis.issue == RAGBaselineIssue.RELEVANT_DOCUMENT_RANKED_LOW
        # 遍历同一诊断列表。
        for diagnosis in diagnoses
    )

    # positive_case_count 是开发集内应当召回证据的样本数量。
    positive_case_count = sum(case.should_retrieve for case in development_cases)
    # negative_case_count 是开发集内应当拒绝检索证据的样本数量。
    negative_case_count = len(development_cases) - positive_case_count
    # contract_failures 收集稳定原因码，不依赖中文输出文本解析。
    contract_failures: list[str] = []
    # 原始文档规模不足时，实验无法覆盖治理状态和相邻主题。
    if len(raw_corpus) < config.contract.min_total_documents:
        # 追加机器可读原因码供测试和CI定位。
        contract_failures.append("total_documents_below_minimum")
    # 可索引活动文档太少时，检索任务仍可能过于简单。
    if len(indexable_documents) < config.contract.min_indexable_documents:
        # 记录公共语料规模失败。
        contract_failures.append("indexable_documents_below_minimum")
    # 实际Chunk太少说明长文切分没有被真正测试。
    if len(chunks) < config.contract.min_chunks:
        # 记录切片规模失败。
        contract_failures.append("chunks_below_minimum")
    # 开发集规模不足会产生新的虚假满分风险。
    if len(development_cases) < config.contract.min_development_cases:
        # 记录开发集总数失败。
        contract_failures.append("development_cases_below_minimum")
    # 正例不足时无法覆盖多种召回困难。
    if positive_case_count < config.contract.min_positive_cases:
        # 记录正例数量失败。
        contract_failures.append("positive_cases_below_minimum")
    # 负例不足时阈值可能通过降低门槛取得虚高Recall。
    if negative_case_count < config.contract.min_negative_cases:
        # 记录负例数量失败。
        contract_failures.append("negative_cases_below_minimum")
    # 旧方案没有暴露任何失败时，说明问题集仍然不足以驱动技术选择。
    if failed_case_count < config.contract.min_baseline_failures:
        # 满分在本阶段是实验设计失败，而不是晋级成功。
        contract_failures.append("baseline_failures_below_minimum")

    # 使用全部真实统计构造不可变更的实验报告。
    return RAGProblemBaselineReport(
        # 复制实验系列ID。
        experiment_id=config.experiment_id,
        # 复制实验契约版本。
        experiment_version=config.version,
        # 记录当前UTC时间。
        generated_at=datetime.now(UTC),
        # 复制Baseline profile名称。
        baseline_profile_id=config.baseline.profile_id,
        # 类型固定为False，再次明确无付费调用。
        paid_api_called=False,
        # 报告治理前原始文档总数。
        total_documents=len(raw_corpus),
        # 报告治理后活动公共文档数。
        indexable_documents=len(indexable_documents),
        # 报告真实切片数。
        chunk_count=len(chunks),
        # 报告本轮开发集规模。
        development_case_count=len(development_cases),
        # 只报告锁定集数量，不包含其运行结果。
        holdout_case_count=len(holdout_cases),
        # 嵌入通用检索聚合指标与逐样本结果。
        metrics=metrics,
        # 保存真实决策失败数量。
        failed_case_count=failed_case_count,
        # 保存召回成功但仍可优化排序的数量。
        ranking_opportunity_count=ranking_opportunity_count,
        # 保存有限失败诊断。
        diagnoses=diagnoses,
        # 没有任何原因码时，实验规模与失败暴露契约通过。
        experiment_contract_passed=not contract_failures,
        # 保存稳定原因码列表。
        experiment_contract_failures=contract_failures,
    )

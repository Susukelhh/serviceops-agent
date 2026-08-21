"""RAG 检索离线评测数据结构、数据加载与指标计算。"""

# log2 用于计算按排名位置折损的 nDCG；Path/Sequence 负责文件与输入集合类型。
from collections.abc import Sequence
from math import log2
from pathlib import Path
from typing import Literal

# BaseModel/Field 校验评测数据和指标边界；TypeAdapter 校验顶层 JSON 数组。
from pydantic import BaseModel, Field, TypeAdapter, model_validator

# KnowledgeRetriever 是被评测对象的最小协议，不绑定 Qdrant 或特定 Embedding。
from serviceops_agent.rag.retriever import KnowledgeRetriever


class RAGEvaluationCase(BaseModel):
    """一条经过人工标注的检索问题与期望知识来源。"""

    # case_id 是报告与 CI 失败日志中的稳定样本标识。
    case_id: str = Field(min_length=1, max_length=100)
    # question 是送入检索器的真实自然语言问题。
    question: str = Field(min_length=1, max_length=500)
    # expected_document_ids 保存任意一个命中即可视为召回成功的文档 ID 集合。
    expected_document_ids: list[str] = Field(default_factory=list, max_length=10)
    # should_retrieve 区分知识库应覆盖的正例与必须拒绝的域外负例。
    should_retrieve: bool
    # tags 描述同义改写、精确词、权限等困难来源，便于按失败类型复盘。
    tags: list[str] = Field(default_factory=list, max_length=10)
    # difficulty 让报告区分基础回归与真正用于调参的困难样本。
    difficulty: Literal["basic", "hard", "adversarial"] = "basic"

    @model_validator(mode="after")
    def validate_label_consistency(self) -> "RAGEvaluationCase":
        """确保正例有期望文档，负例没有互相矛盾的期望来源。"""

        # 正例没有期望文档时无法计算 Recall 和 MRR，应在运行评测前直接报错。
        if self.should_retrieve and not self.expected_document_ids:
            # ValueError 会被 Pydantic 转换为包含 case 位置的校验错误。
            raise ValueError("正例必须至少配置一个 expected_document_id")
        # 负例携带期望文档会让“有命中还是无命中”的标签语义矛盾。
        if not self.should_retrieve and self.expected_document_ids:
            # 明确要求清空负例的期望来源。
            raise ValueError("负例不能配置 expected_document_ids")
        # 返回自身表示跨字段校验完成。
        return self


class RAGEvaluationCaseResult(BaseModel):
    """单条样本的可审计检索结果。"""

    # case_id 关联回输入评测样本。
    case_id: str
    # should_retrieve 保留正负例标签，便于报告分组。
    should_retrieve: bool
    # retrieved_document_ids 按实际检索排名保存去重后的文档 ID。
    retrieved_document_ids: list[str]
    # first_relevant_rank 是首个期望文档的一基排名；未召回或负例使用 None。
    first_relevant_rank: int | None = Field(default=None, ge=1)
    # passed 表示正例成功召回期望文档，或负例成功保持无检索结果。
    passed: bool


class RAGEvaluationSummary(BaseModel):
    """一轮检索评测的聚合指标与逐样本明细。"""

    # top_k 记录评测使用的候选数，防止脱离参数比较指标。
    top_k: int = Field(ge=1)
    # total_cases 是正例与负例总数。
    total_cases: int = Field(ge=1)
    # positive_cases 是用于 Recall@K 和 MRR@K 分母的知识内问题数。
    positive_cases: int = Field(ge=1)
    # negative_cases 是用于误召回率分母的知识外问题数。
    negative_cases: int = Field(ge=0)
    # recall_at_k 表示正例中至少召回一个期望文档的比例。
    recall_at_k: float = Field(ge=0.0, le=1.0)
    # mrr_at_k 对首个相关文档排名取倒数并在全部正例上求平均。
    mrr_at_k: float = Field(ge=0.0, le=1.0)
    # top_1_accuracy 表示正例中期望文档直接排在第一位的比例。
    top_1_accuracy: float = Field(ge=0.0, le=1.0)
    # ndcg_at_k 同时考虑全部相关文档及其排名位置，越靠前贡献越大。
    ndcg_at_k: float = Field(ge=0.0, le=1.0)
    # decision_accuracy 同时衡量正例正确命中和负例正确拒绝。
    decision_accuracy: float = Field(ge=0.0, le=1.0)
    # false_positive_rate 表示负例中仍返回任意证据的比例；无负例时固定为 0。
    false_positive_rate: float = Field(ge=0.0, le=1.0)
    # results 保存逐样本细节，便于定位聚合分数下降的具体问题。
    results: list[RAGEvaluationCaseResult]


def load_rag_evaluation_cases(path: Path) -> list[RAGEvaluationCase]:
    """从 UTF-8 JSON 数组读取并校验全部 RAG 评测样本。"""

    # read_text 明确 UTF-8，避免 Windows 默认编码破坏中文问题。
    raw_json = path.read_text(encoding="utf-8")
    # TypeAdapter 让 Pydantic 校验顶层 list 中的每一条 Case，而不是手写循环解析。
    cases = TypeAdapter(list[RAGEvaluationCase]).validate_json(raw_json)
    # 空评测集会产生没有意义的除法和虚假通过，因此启动阶段直接拒绝。
    if not cases:
        # 错误只描述数据集状态，不包含任何敏感业务内容。
        raise ValueError("RAG 评测集不能为空")
    # 至少需要一个正例才能定义 Recall@K 和 MRR@K。
    if not any(case.should_retrieve for case in cases):
        # 调用方需要补充知识库内问题后重新运行。
        raise ValueError("RAG 评测集必须至少包含一个正例")
    # 返回完成 Schema 与跨样本前置条件校验的数据。
    return cases


def evaluate_retriever(
    retriever: KnowledgeRetriever,
    cases: Sequence[RAGEvaluationCase],
    *,
    top_k: int,
) -> RAGEvaluationSummary:
    """执行检索评测，并计算 Recall@K、MRR@K、准确率和误召回率。"""

    # top_k 非法时不要依赖某个具体检索器报出难以理解的错误。
    if top_k < 1:
        # 与 Settings 的最小值约束保持一致。
        raise ValueError("top_k 必须大于等于 1")
    # 空数据无法生成有意义指标，直接拒绝而不是返回全部为零的误导报告。
    if not cases:
        # 调用方应先通过加载函数或显式传入非空序列。
        raise ValueError("评测样本不能为空")

    # results 按评测集原始顺序保存，确保报告和 Git diff 稳定。
    results: list[RAGEvaluationCaseResult] = []
    # recalled_positive_count 累计成功召回期望文档的正例数。
    recalled_positive_count = 0
    # reciprocal_rank_sum 累计每个正例首个相关结果排名的倒数。
    reciprocal_rank_sum = 0.0
    # top_1_positive_count 累计期望文档直接排在第一位的正例数量。
    top_1_positive_count = 0
    # normalized_dcg_sum 累计每个正例的归一化折损累计增益。
    normalized_dcg_sum = 0.0
    # passed_count 同时累计正例正确召回和负例正确拒绝。
    passed_count = 0
    # false_positive_count 只统计负例返回任意命中的次数。
    false_positive_count = 0

    # 每条样本独立检索，便于后续并行化或记录单条耗时。
    for case in cases:
        # search 内部负责向量化、阈值过滤和按分数降序返回 Top-K。
        hits = retriever.search(case.question, top_k=top_k)
        # 文档可能产生多个切片，指标按文档相关性评估，因此保序去重 document_id。
        retrieved_document_ids = list(
            # dict.fromkeys 在 Python 中保留首次出现顺序。
            dict.fromkeys(hit.chunk.document_id for hit in hits)
        )
        # expected_id_set 让逐排名成员判断保持清晰且高效。
        expected_id_set = set(case.expected_document_ids)
        # first_relevant_rank 初始为 None，表示尚未召回期望文档。
        first_relevant_rank: int | None = None
        # 只为正例寻找首个相关文档，负例没有期望集合。
        if case.should_retrieve:
            # enumerate 从 1 开始，直接得到信息检索指标使用的一基排名。
            for rank, document_id in enumerate(retrieved_document_ids, start=1):
                # 任一人工标注期望文档出现即可视为相关结果。
                if document_id in expected_id_set:
                    # 保存首个相关排名后即可停止，后续结果不影响 MRR。
                    first_relevant_rank = rank
                    # break 保证不会被排名更后的相关文档覆盖。
                    break
            # relevance_by_rank 把实际文档排名转换为二元相关性序列。
            relevance_by_rank = [
                # 人工标注的期望文档记为 1，其他候选记为 0。
                1 if document_id in expected_id_set else 0
                # 文档列表已经保序去重，排名位置可以直接用于 nDCG。
                for document_id in retrieved_document_ids
            ]
            # dcg 对越靠后的相关文档施加对数折损，第一名仍贡献完整 1 分。
            dcg = sum(
                # rank 从 1 开始时，log2(rank + 1) 生成标准折损分母。
                relevance / log2(rank + 1)
                # 同时遍历每个实际排名及其二元相关性。
                for rank, relevance in enumerate(relevance_by_rank, start=1)
            )
            # 理想列表最多包含 K 个期望文档，并把它们全部排在最前面。
            ideal_relevant_count = min(len(expected_id_set), top_k)
            # idcg 是当前人工标签在同一 K 值下能够取得的最高 DCG。
            idcg = sum(
                # 理想相关文档在第一、第二等位置依次折损。
                1.0 / log2(rank + 1)
                # range 的终点加一，使理想相关文档数量完整进入求和。
                for rank in range(1, ideal_relevant_count + 1)
            )
            # 正例至少有一个期望文档，因此 idcg 必定大于零。
            normalized_dcg_sum += dcg / idcg

        # 正例必须召回期望文档；负例必须完全没有通过证据阈值的命中。
        passed = (
            # 正例通过条件。
            first_relevant_rank is not None
            # 负例通过条件。
            if case.should_retrieve
            else not retrieved_document_ids
        )
        # 所有通过样本共同进入决策准确率分子。
        if passed:
            # 累加一个正确检索决策。
            passed_count += 1
        # 正例的相关排名同时贡献 Recall 和 MRR。
        if case.should_retrieve and first_relevant_rank is not None:
            # 至少召回一个期望文档，因此 Recall 计数加一。
            recalled_positive_count += 1
            # 排名越靠前倒数越大；第一名贡献 1，第二名贡献 0.5。
            reciprocal_rank_sum += 1.0 / first_relevant_rank
            # 第一名就是期望文档时，Top-1 正确计数增加一次。
            if first_relevant_rank == 1:
                # 该计数最终除以全部正例，而不是只除以已召回正例。
                top_1_positive_count += 1
        # 负例只要有任意命中就形成一次误召回。
        if not case.should_retrieve and retrieved_document_ids:
            # 累加误召回次数，用于评估阈值是否过低。
            false_positive_count += 1

        # 保存本条完整、可审计结果。
        results.append(
            RAGEvaluationCaseResult(
                # 复制稳定样本 ID。
                case_id=case.case_id,
                # 保留正负标签。
                should_retrieve=case.should_retrieve,
                # 保存实际文档排名。
                retrieved_document_ids=retrieved_document_ids,
                # 保存相关文档首位排名。
                first_relevant_rank=first_relevant_rank,
                # 保存本条最终通过状态。
                passed=passed,
            )
        )

    # positive_cases 是 Recall 和 MRR 的固定分母。
    positive_cases = sum(1 for case in cases if case.should_retrieve)
    # negative_cases 是误召回率的固定分母。
    negative_cases = len(cases) - positive_cases
    # 没有正例时指标没有定义，明确报错避免 ZeroDivisionError 或虚假零分。
    if positive_cases == 0:
        # 该检查也覆盖直接调用函数而没有经过文件加载器的场景。
        raise ValueError("评测样本必须至少包含一个正例")

    # 由经过校验的计数构造最终强类型报告。
    return RAGEvaluationSummary(
        # 记录本轮 K 值。
        top_k=top_k,
        # 总样本数用于解释准确率分母。
        total_cases=len(cases),
        # 正例数量用于解释召回指标。
        positive_cases=positive_cases,
        # 负例数量用于解释误召回率。
        negative_cases=negative_cases,
        # Recall@K 是成功召回正例占全部正例的比例。
        recall_at_k=recalled_positive_count / positive_cases,
        # MRR@K 是正例倒数排名和除以全部正例，未召回正例自然贡献 0。
        mrr_at_k=reciprocal_rank_sum / positive_cases,
        # Top-1 准确率比 Recall@K 更严格，要求相关文档无需二次排序就位于首位。
        top_1_accuracy=top_1_positive_count / positive_cases,
        # 未召回正例在 normalized_dcg_sum 中自然贡献 0。
        ndcg_at_k=normalized_dcg_sum / positive_cases,
        # 决策准确率覆盖所有正负样本。
        decision_accuracy=passed_count / len(cases),
        # 没有负例时固定为零；当前标准数据集包含负例，因此通常执行前一分支。
        false_positive_rate=(false_positive_count / negative_cases if negative_cases else 0.0),
        # 附带逐样本结果用于失败分析。
        results=results,
    )

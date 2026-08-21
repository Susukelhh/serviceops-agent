"""第28步：固定检索证据后评测回答器的证据充分性判断。"""

# sha256冻结系统提示，防止开发实验后悄悄改提示再运行holdout。
from hashlib import sha256

# Path用于读取版本化配置和数据集。
from pathlib import Path

# Literal限制报告中的数据集和回答器类型。
# Pydantic校验评测集、质量门和报告结构；TypeAdapter校验顶层JSON数组。
from pydantic import BaseModel, Field, TypeAdapter, model_validator

# resolve_project_path让PyCharm工作目录不影响数据定位。
from serviceops_agent.config.paths import resolve_project_path

# Settings提供真实千问聊天模型配置。
from serviceops_agent.config.settings import Settings

# GroundedAnswerDraft与RetrievalHit是线上回答节点使用的同一领域契约。
from serviceops_agent.domain.knowledge import GroundedAnswerDraft, RetrievalHit

# 知识仓库只加载已发布公共文档。
from serviceops_agent.infrastructure.knowledge_repository import JsonKnowledgeRepository

# 固定切片器把数据集引用解析为真实证据Chunk。
from serviceops_agent.rag.chunking import KnowledgeChunker

# 复用线上确定性回答器、千问结构化回答器和系统提示。
from serviceops_agent.rag.generation import (
    GROUNDED_ANSWER_SYSTEM_PROMPT,
    ExtractiveGroundedAnswerClient,
    GroundedAnswerClient,
    create_grounded_answer_client,
)


class GroundingEvidenceReference(BaseModel):
    """一条评测样本允许提供给回答器的固定文档与Chunk序号。"""

    # document_id指向治理语料中的稳定业务文档ID。
    document_id: str = Field(min_length=1, max_length=100)
    # chunk_indexes明确哪些切片是本次模拟检索已经召回的证据。
    chunk_indexes: list[int] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_chunk_indexes(self) -> "GroundingEvidenceReference":
        """确保Chunk序号非负、去重且升序。"""

        # 负数不是合法文档内序号。
        if any(index < 0 for index in self.chunk_indexes):
            # 在加载数据时立即暴露标注错误。
            raise ValueError("chunk_indexes不能包含负数")
        # 稳定数据要求按升序去重，避免同一证据重复发送给模型。
        if self.chunk_indexes != sorted(set(self.chunk_indexes)):
            # 标注者应整理后再运行。
            raise ValueError("chunk_indexes必须去重并按升序排列")
        # 返回通过校验的引用。
        return self


class GroundingEvaluationCase(BaseModel):
    """一条问题、固定候选证据和人工可回答标签。"""

    # case_id是报告定位Bad Case的稳定标识。
    case_id: str = Field(min_length=1, max_length=100)
    # question是发送给回答器的用户问题。
    question: str = Field(min_length=1, max_length=500)
    # evidence_refs固定检索层输出，隔离生成层变量。
    evidence_refs: list[GroundingEvidenceReference] = Field(min_length=1, max_length=5)
    # should_answer表示这些证据是否真正蕴含问题答案。
    should_answer: bool
    # tags用于按业务域和知识缺口类型复盘。
    tags: list[str] = Field(default_factory=list, max_length=10)


class GroundingQualityGate(BaseModel):
    """同时约束知识内回答能力和知识缺口安全拒答。"""

    # min_answerable_recall防止模型为了安全而一律拒答。
    min_answerable_recall: float = Field(ge=0.0, le=1.0)
    # min_abstention_accuracy要求无答案证据大多数被正确拒绝。
    min_abstention_accuracy: float = Field(ge=0.0, le=1.0)
    # min_decision_accuracy综合全部正负例。
    min_decision_accuracy: float = Field(ge=0.0, le=1.0)
    # max_unsupported_answer_rate是知识缺口中仍自动回答的上限。
    max_unsupported_answer_rate: float = Field(ge=0.0, le=1.0)
    # min_citation_validity要求放行答案只引用本次候选白名单。
    min_citation_validity: float = Field(ge=0.0, le=1.0)


class GroundingSufficiencyExperimentConfig(BaseModel):
    """第28步开发、提示冻结和一次性holdout契约。"""

    # experiment_id和version定位实验系列及语义版本。
    experiment_id: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=50)
    # 三个路径分别定位治理语料、开发集和未运行锁定集。
    corpus_path: str = Field(min_length=1, max_length=500)
    development_dataset_path: str = Field(min_length=1, max_length=500)
    holdout_dataset_path: str = Field(min_length=1, max_length=500)
    # holdout_case_count只用于调用量计划，默认阶段不读取锁定内容。
    holdout_case_count: int = Field(ge=1, le=1000)
    # 切片参数必须与产生固定证据标注时一致。
    chunk_size: int = Field(ge=100, le=2000)
    chunk_overlap: int = Field(ge=0, le=500)
    # candidate_profile_id稳定命名千问回答策略。
    candidate_profile_id: str = Field(min_length=1, max_length=100)
    # frozen_prompt_sha256开发通过后写入，holdout前校验提示未变化。
    frozen_prompt_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    # 开发与锁定集使用预先声明的不同质量门。
    development_gate: GroundingQualityGate
    holdout_gate: GroundingQualityGate

    @model_validator(mode="after")
    def validate_chunk_window(self) -> "GroundingSufficiencyExperimentConfig":
        """确保切片窗口能够前进。"""

        # overlap覆盖整个窗口会导致切片循环无法正常前进。
        if self.chunk_overlap >= self.chunk_size:
            # 读取配置时快速失败。
            raise ValueError("chunk_overlap必须小于chunk_size")
        # 返回通过组合校验的配置。
        return self


class GroundingCaseResult(BaseModel):
    """单条问题的回答决策、引用校验和失败原因。"""

    # case_id关联回人工标注。
    case_id: str
    # should_answer保留人工标签。
    should_answer: bool
    # predicted_answerable是回答器结构化决策。
    predicted_answerable: bool
    # citation_ids保存模型或基线实际返回的引用ID。
    citation_ids: list[str]
    # citation_valid表示放行答案至少引用一条且全部属于证据白名单。
    citation_valid: bool
    # passed表示回答/拒答决策与引用契约同时正确。
    passed: bool
    # failure_codes是稳定、脱敏的失败类型。
    failure_codes: list[str]


class GroundingEvaluationSummary(BaseModel):
    """一轮证据充分性评测的安全和可用性指标。"""

    # 数据规模明确正负例分母。
    total_cases: int = Field(ge=1)
    answerable_cases: int = Field(ge=1)
    unanswerable_cases: int = Field(ge=1)
    # answerable_recall衡量有答案时真正放行的比例。
    answerable_recall: float = Field(ge=0.0, le=1.0)
    # abstention_accuracy衡量知识缺口正确拒答比例。
    abstention_accuracy: float = Field(ge=0.0, le=1.0)
    # decision_accuracy衡量全部样本最终决策正确率。
    decision_accuracy: float = Field(ge=0.0, le=1.0)
    # unsupported_answer_rate与拒答率互补，直接对应幻觉风险。
    unsupported_answer_rate: float = Field(ge=0.0, le=1.0)
    # citation_validity只在实际放行的答案中计算引用白名单合规率。
    citation_validity: float = Field(ge=0.0, le=1.0)
    # quality_gate_passed与失败原因给出候选晋级结论。
    quality_gate_passed: bool
    quality_gate_failures: list[str]
    # results保留逐样本证据。
    results: list[GroundingCaseResult]


class GroundingSufficiencyExperimentReport(BaseModel):
    """离线Extractive基线与可选千问候选的完整报告。"""

    # 实验身份、模型和提示指纹用于复现。
    experiment_id: str
    experiment_version: str
    candidate_profile_id: str
    candidate_model: str
    prompt_sha256: str
    # planned_development_chat_calls等于开发样本数，每题一次结构化调用。
    planned_development_chat_calls: int = Field(ge=1)
    # planned_holdout_extra_chat_calls只来自未运行锁定题。
    planned_holdout_extra_chat_calls: int = Field(ge=1)
    # paid_api_called和actual_chat_calls明确费用边界。
    paid_api_called: bool
    actual_chat_calls: int = Field(ge=0)
    # extractive_development始终存在且完全离线。
    extractive_development: GroundingEvaluationSummary
    # qwen_development只有显式确认后出现。
    qwen_development: GroundingEvaluationSummary | None = None
    # frozen_prompt_matches控制holdout权限。
    frozen_prompt_matches: bool = False
    # 两个holdout结果只在双确认后出现。
    extractive_holdout: GroundingEvaluationSummary | None = None
    qwen_holdout: GroundingEvaluationSummary | None = None


def load_grounding_sufficiency_experiment_config(
    path: Path,
) -> GroundingSufficiencyExperimentConfig:
    """读取并校验第28步JSON实验配置。"""

    # UTF-8保证中文路径和未来说明字段可读。
    raw_json = path.read_text(encoding="utf-8")
    # Pydantic执行字段与窗口组合校验。
    return GroundingSufficiencyExperimentConfig.model_validate_json(raw_json)


def load_grounding_evaluation_cases(path: Path) -> list[GroundingEvaluationCase]:
    """读取固定证据评测集并检查正负例和ID唯一性。"""

    # 一次性校验顶层JSON数组和每个Case。
    cases = TypeAdapter(list[GroundingEvaluationCase]).validate_json(
        path.read_text(encoding="utf-8")
    )
    # 空数据集不能产生有效指标。
    if not cases:
        # 提示补充样本。
        raise ValueError("证据充分性评测集不能为空")
    # 正例和负例都必须存在，才能同时评测可用性与安全性。
    if not any(case.should_answer for case in cases) or not any(
        not case.should_answer for case in cases
    ):
        # 明确数据平衡前置条件。
        raise ValueError("证据充分性评测集必须同时包含可回答和不可回答样本")
    # case_ids用于发现重复标识。
    case_ids = [case.case_id for case in cases]
    # 重复ID会让报告无法稳定定位样本。
    if len(case_ids) != len(set(case_ids)):
        # 要求数据作者修复。
        raise ValueError("证据充分性case_id不能重复")
    # 返回经过完整校验的样本。
    return cases


def grounding_prompt_sha256() -> str:
    """计算线上Grounded系统提示的稳定SHA-256指纹。"""

    # UTF-8编码后计算十六进制摘要，不保存任何密钥或用户数据。
    return sha256(GROUNDED_ANSWER_SYSTEM_PROMPT.encode("utf-8")).hexdigest()


def _build_evidence_by_case(
    config: GroundingSufficiencyExperimentConfig,
    cases: list[GroundingEvaluationCase],
) -> dict[str, list[RetrievalHit]]:
    """把人工文档/Chunk引用解析为线上同类型RetrievalHit。"""

    # 加载治理后的公共文档，内部和退役文档不会成为实验候选。
    documents = JsonKnowledgeRepository(
        resolve_project_path(config.corpus_path)
    ).list_indexable_documents()
    # 使用版本化切片窗口产生稳定Chunk。
    chunks = KnowledgeChunker(
        chunk_size=config.chunk_size,
        chunk_overlap=config.chunk_overlap,
    ).split_documents(documents)
    # chunk_by_key支持按父文档ID和文档内序号精确查找。
    chunk_by_key = {
        # 二元组是稳定人工标注键。
        (chunk.document_id, chunk.chunk_index): chunk
        # 遍历全部活动公共Chunk。
        for chunk in chunks
    }
    # evidence_by_case保存每题按标注顺序排列的固定证据。
    evidence_by_case: dict[str, list[RetrievalHit]] = {}
    # 逐样本解析引用。
    for case in cases:
        # hits收集当前题允许提供的证据。
        hits: list[RetrievalHit] = []
        # 每份文档引用保持数据集顺序。
        for evidence_ref in case.evidence_refs:
            # 每个Chunk索引保持升序。
            for chunk_index in evidence_ref.chunk_indexes:
                # key精确定位实际Chunk。
                key = (evidence_ref.document_id, chunk_index)
                # 文档更新导致Chunk消失时必须停止，不能静默换证据。
                if key not in chunk_by_key:
                    # 错误只显示治理ID与序号，不包含正文。
                    raise ValueError(f"固定证据不存在：{key[0]} chunk {key[1]}")
                # 分数只用于保持证据顺序，不参与本步决策。
                score = max(0.50, 0.95 - len(hits) * 0.02)
                # 构造线上回答器使用的同一RetrievalHit类型。
                hits.append(RetrievalHit(chunk=chunk_by_key[key], score=score))
        # 每题至少有一条固定候选，Pydantic数据已限制evidence_refs非空。
        evidence_by_case[case.case_id] = hits
    # 返回可供多种回答器复用的固定输入。
    return evidence_by_case


def _quality_gate_failures(
    *,
    answerable_recall: float,
    abstention_accuracy: float,
    decision_accuracy: float,
    unsupported_answer_rate: float,
    citation_validity: float,
    gate: GroundingQualityGate,
) -> list[str]:
    """把五项指标转换为稳定失败原因。"""

    # failures按固定顺序保存。
    failures: list[str] = []
    # 有答案问题放行不足会影响业务可用性。
    if answerable_recall < gate.min_answerable_recall:
        # 添加有限原因码。
        failures.append("answerable_recall_below_threshold")
    # 知识缺口拒答不足是核心幻觉风险。
    if abstention_accuracy < gate.min_abstention_accuracy:
        # 添加有限原因码。
        failures.append("abstention_accuracy_below_threshold")
    # 综合决策不足不能晋级。
    if decision_accuracy < gate.min_decision_accuracy:
        # 添加有限原因码。
        failures.append("decision_accuracy_below_threshold")
    # 无依据回答超过上限时拒绝候选。
    if unsupported_answer_rate > gate.max_unsupported_answer_rate:
        # 添加有限原因码。
        failures.append("unsupported_answer_rate_above_threshold")
    # 引用白名单合规不足时拒绝候选。
    if citation_validity < gate.min_citation_validity:
        # 添加有限原因码。
        failures.append("citation_validity_below_threshold")
    # 返回可能为空的失败原因。
    return failures


async def evaluate_grounding_client(
    client: GroundedAnswerClient,
    cases: list[GroundingEvaluationCase],
    evidence_by_case: dict[str, list[RetrievalHit]],
    *,
    gate: GroundingQualityGate,
) -> GroundingEvaluationSummary:
    """运行回答器并计算回答、拒答、无依据回答和引用指标。"""

    # results按数据集顺序保存，方便报告diff。
    results: list[GroundingCaseResult] = []
    # 正负例及正确决策计数器。
    answerable_cases = sum(1 for case in cases if case.should_answer)
    unanswerable_cases = len(cases) - answerable_cases
    answered_answerable = 0
    abstained_unanswerable = 0
    correct_decisions = 0
    unsupported_answers = 0
    predicted_answer_count = 0
    valid_citation_answer_count = 0

    # 每题独立调用，真实模型调用量等于样本数。
    for case in cases:
        # 读取当前题固定证据，不允许回答器改变检索结果。
        evidence = evidence_by_case[case.case_id]
        # 调用线上同协议回答器。
        draft: GroundedAnswerDraft = await client.generate(
            question=case.question,
            evidence=evidence,
        )
        # allowed_ids是本题唯一合法引用白名单。
        allowed_ids = {hit.chunk.chunk_id for hit in evidence}
        # 去重但保持返回顺序。
        citation_ids = list(dict.fromkeys(draft.citation_ids))
        # 放行答案必须至少一条引用且全部来自白名单。
        citation_valid = bool(citation_ids) and set(citation_ids).issubset(allowed_ids)
        # failures保存当前题错误类型。
        failures: list[str] = []

        # 模型选择自动回答时统计引用质量。
        if draft.is_answerable:
            # 累加实际放行答案数。
            predicted_answer_count += 1
            # 合法引用的放行答案计入分子。
            if citation_valid:
                # 累加引用有效答案。
                valid_citation_answer_count += 1
            else:
                # 对应生产节点会被引用白名单拦截。
                failures.append("invalid_or_missing_citation")

        # 正例必须声明可回答且引用合法才通过。
        if case.should_answer:
            # answerable_pass同时保护可用性和引用边界。
            answerable_pass = draft.is_answerable and citation_valid
            # 成功放行正例时累计。
            if answerable_pass:
                # 正例召回加一。
                answered_answerable += 1
                # 综合正确决策加一。
                correct_decisions += 1
            else:
                # 区分错误拒答与引用问题。
                if not draft.is_answerable:
                    # 模型过度保守。
                    failures.append("answerable_case_declined")
            # 当前题通过状态。
            passed = answerable_pass
        else:
            # 负例只要模型拒答，生产节点就会清空可能的引用并转人工。
            unanswerable_pass = not draft.is_answerable
            # 正确拒答时累计安全指标。
            if unanswerable_pass:
                # 拒答准确数加一。
                abstained_unanswerable += 1
                # 综合正确决策加一。
                correct_decisions += 1
            else:
                # 自动回答无充分证据问题就是unsupported answer。
                unsupported_answers += 1
                # 添加核心安全失败码。
                failures.append("unsupported_answer_generated")
            # 当前负例通过状态。
            passed = unanswerable_pass

        # 保存逐题结果，不保存模型答案正文，避免报告积累用户/知识内容。
        results.append(
            GroundingCaseResult(
                case_id=case.case_id,
                should_answer=case.should_answer,
                predicted_answerable=draft.is_answerable,
                citation_ids=citation_ids,
                citation_valid=citation_valid,
                passed=passed,
                failure_codes=list(dict.fromkeys(failures)),
            )
        )

    # 计算五项聚合指标。
    answerable_recall = answered_answerable / answerable_cases
    abstention_accuracy = abstained_unanswerable / unanswerable_cases
    decision_accuracy = correct_decisions / len(cases)
    unsupported_answer_rate = unsupported_answers / unanswerable_cases
    # 没有任何放行答案时引用率约定为1，但answerable_recall会阻止全拒候选通过。
    citation_validity = (
        valid_citation_answer_count / predicted_answer_count if predicted_answer_count else 1.0
    )
    # 计算质量门原因。
    gate_failures = _quality_gate_failures(
        answerable_recall=answerable_recall,
        abstention_accuracy=abstention_accuracy,
        decision_accuracy=decision_accuracy,
        unsupported_answer_rate=unsupported_answer_rate,
        citation_validity=citation_validity,
        gate=gate,
    )
    # 返回完整摘要。
    return GroundingEvaluationSummary(
        total_cases=len(cases),
        answerable_cases=answerable_cases,
        unanswerable_cases=unanswerable_cases,
        answerable_recall=answerable_recall,
        abstention_accuracy=abstention_accuracy,
        decision_accuracy=decision_accuracy,
        unsupported_answer_rate=unsupported_answer_rate,
        citation_validity=citation_validity,
        quality_gate_passed=not gate_failures,
        quality_gate_failures=gate_failures,
        results=results,
    )


async def run_grounding_sufficiency_experiment(
    config: GroundingSufficiencyExperimentConfig,
    *,
    runtime_settings: Settings,
    confirm_paid_api: bool = False,
    include_holdout: bool = False,
) -> GroundingSufficiencyExperimentReport:
    """默认运行离线基线；双确认后运行千问开发或锁定候选。"""

    # 计算当前线上Grounded系统提示指纹。
    prompt_hash = grounding_prompt_sha256()
    # 加载开发集并解析固定证据。
    development_cases = load_grounding_evaluation_cases(
        resolve_project_path(config.development_dataset_path)
    )
    development_evidence = _build_evidence_by_case(config, development_cases)
    # Extractive基线只要证据非空就回答，预期暴露知识缺口风险。
    extractive_development = await evaluate_grounding_client(
        ExtractiveGroundedAnswerClient(),
        development_cases,
        development_evidence,
        gate=config.development_gate,
    )
    # 未确认付费时不构建真实聊天模型客户端。
    if not confirm_paid_api:
        # 返回完全离线报告。
        return GroundingSufficiencyExperimentReport(
            experiment_id=config.experiment_id,
            experiment_version=config.version,
            candidate_profile_id=config.candidate_profile_id,
            candidate_model=runtime_settings.llm_model,
            prompt_sha256=prompt_hash,
            planned_development_chat_calls=len(development_cases),
            planned_holdout_extra_chat_calls=config.holdout_case_count,
            paid_api_called=False,
            actual_chat_calls=0,
            extractive_development=extractive_development,
        )

    # 真实Grounded回答要求OpenAI兼容后端和.env中的Key/Base URL。
    candidate_settings = runtime_settings.model_copy(
        update={
            # 明确选择真实聊天后端。
            "llm_backend": "openai_compatible",
            # 明确使用结构化Grounded生成而非Extractive。
            "rag_generation_backend": "llm",
        }
    )
    # 工厂复用线上模型、超时、重试、结构化输出和上下文预算。
    qwen_client = create_grounded_answer_client(candidate_settings)
    # 运行开发集，每题一次真实结构化聊天调用。
    qwen_development = await evaluate_grounding_client(
        qwen_client,
        development_cases,
        development_evidence,
        gate=config.development_gate,
    )
    # 提示必须已冻结、与当前一致且开发门通过，才允许holdout。
    frozen_prompt_matches = (
        config.frozen_prompt_sha256 == prompt_hash and qwen_development.quality_gate_passed
    )
    # 默认不读取或运行任何锁定结果。
    extractive_holdout: GroundingEvaluationSummary | None = None
    qwen_holdout: GroundingEvaluationSummary | None = None
    # 实际调用数先计入完整开发集。
    actual_chat_calls = len(development_cases)

    # 第二把钥匙出现时进入锁定路径。
    if include_holdout:
        # 未冻结或开发未通过时禁止读取holdout。
        if not frozen_prompt_matches:
            # 错误提醒先审查开发结果并冻结提示指纹。
            raise ValueError("Grounded提示尚未冻结或开发质量门未通过，禁止运行holdout")
        # 此时才加载全新锁定样本。
        holdout_cases = load_grounding_evaluation_cases(
            resolve_project_path(config.holdout_dataset_path)
        )
        # 文件实际数量必须与预先记录计划一致。
        if len(holdout_cases) != config.holdout_case_count:
            # 防止确认费用后数据规模悄悄扩大。
            raise ValueError("Grounding holdout实际数量与配置计划不一致")
        # 解析固定锁定证据。
        holdout_evidence = _build_evidence_by_case(config, holdout_cases)
        # 离线基线仅用于同数据集公平对照。
        extractive_holdout = await evaluate_grounding_client(
            ExtractiveGroundedAnswerClient(),
            holdout_cases,
            holdout_evidence,
            gate=config.holdout_gate,
        )
        # 千问只运行已冻结提示。
        qwen_holdout = await evaluate_grounding_client(
            qwen_client,
            holdout_cases,
            holdout_evidence,
            gate=config.holdout_gate,
        )
        # 增加实际完成的锁定聊天调用。
        actual_chat_calls += len(holdout_cases)

    # 返回完整真实候选报告。
    return GroundingSufficiencyExperimentReport(
        experiment_id=config.experiment_id,
        experiment_version=config.version,
        candidate_profile_id=config.candidate_profile_id,
        candidate_model=candidate_settings.llm_model,
        prompt_sha256=prompt_hash,
        planned_development_chat_calls=len(development_cases),
        planned_holdout_extra_chat_calls=config.holdout_case_count,
        paid_api_called=True,
        actual_chat_calls=actual_chat_calls,
        extractive_development=extractive_development,
        qwen_development=qwen_development,
        frozen_prompt_matches=frozen_prompt_matches,
        extractive_holdout=extractive_holdout,
        qwen_holdout=qwen_holdout,
    )

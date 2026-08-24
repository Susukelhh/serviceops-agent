"""验证第34步事实级端到端成功率、盲测隔离、费用边界和报告脱敏。"""

# argparse用于直接构造第34步CLI的四个布尔开关组合。
import argparse

# importlib.util用于安全加载文件名以数字开头、无法普通import的example脚本。
import importlib.util

# json用于检查送入回答器的公开输入和最终报告中是否混入事实金标。
import json

# date构造通过真实领域Schema校验的固定知识切片。
from datetime import date

# Path用于拦截私有盲测文件读取，并声明稳定配置位置。
from pathlib import Path

# ModuleType为动态加载的第34步CLI模块提供明确类型。
from types import ModuleType

# pytest提供异步测试、异常断言和临时方法替换。
import pytest

# PROJECT_ROOT保证测试从PyCharm和Linux CI启动时都定位同一配置。
from serviceops_agent.config.paths import PROJECT_ROOT

# Settings构造显式无Key、无真实模型的默认运行环境。
from serviceops_agent.config.settings import Settings

# 真实领域模型约束测试证据和结构化回答，不绕过生产Schema。
from serviceops_agent.domain.knowledge import (
    GroundedAnswerDraft,
    KnowledgeChunk,
    RetrievalHit,
)

# 第34步公共类型和函数是本文件的直接验证对象。
from serviceops_agent.evaluation import (
    ForbiddenClaimRule,
    GroundedAnswerSuccessCase,
    GroundedAnswerSuccessExperimentConfig,
    GroundedAnswerSuccessExperimentReport,
    GroundedAnswerSuccessSummary,
    PrivateGroundedAnswerDiagnosticCollector,
    RequiredFactRule,
    evaluate_grounded_answer_success,
    grounded_answer_candidate_fingerprint,
    load_grounded_answer_success_config,
    load_private_grounded_answer_cases,
    run_grounded_answer_success_experiment,
    validate_grounded_answer_evidence_labels,
    write_private_grounded_answer_diagnostics,
)

# 导入实现模块只用于把固定私有目录重定向到pytest临时目录，绝不触碰真实盲测目录。
from serviceops_agent.evaluation import (
    grounded_answer_success_experiment as grounded_success_module,
)

# 完全放行策略让单元测试只关注事实与引用评分，不受范围关键词影响。
from serviceops_agent.rag.query_policy import AllowAllKnowledgeQueryPolicy

# CONFIG_PATH与第34步PyCharm脚本读取同一份公开冻结契约。
CONFIG_PATH: Path = (
    PROJECT_ROOT / "data/evaluation/grounded_answer_success_experiment.json"
)


def _load_step34_cli_module() -> ModuleType:
    """通过文件路径加载第34步脚本，避免数字文件名破坏普通import语法。"""

    # example脚本只在导入阶段声明函数；__main__保护保证不会真正执行实验。
    script_path = PROJECT_ROOT / "examples/34_grounded_answer_success_experiment.py"
    # 使用稳定但独立的测试模块名，不污染业务包命名空间。
    spec = importlib.util.spec_from_file_location(
        "serviceops_step34_cli_for_unit_tests",
        script_path,
    )
    # 缺少spec或loader说明仓库文件损坏，应给出清楚的测试失败而不是AttributeError。
    if spec is None or spec.loader is None:
        raise AssertionError("无法加载第34步CLI脚本")
    # 按Python标准导入流程创建空模块对象。
    module = importlib.util.module_from_spec(spec)
    # 只执行函数和常量定义，不会进入脚本末尾的main分支。
    spec.loader.exec_module(module)
    # 返回模块后，测试可以直接验证内部安全边界函数。
    return module


def _minimal_summary(*, profile_id: str) -> GroundedAnswerSuccessSummary:
    """创建只用于首次冻结写盘测试的最小脱敏汇总。"""

    # 首次冻结writer只读取聚合字段；空results避免引入任何问题或答案正文。
    return GroundedAnswerSuccessSummary(
        profile_id=profile_id,
        total_cases=2,
        answerable_cases=1,
        unanswerable_cases=1,
        passed_cases=1,
        grounded_answer_success_rate=0.5,
        red_line_case_ids=[],
        quality_gate_passed=False,
        quality_gate_failures=["grounded_answer_success_rate_below_threshold"],
        grounding_chat_calls=2,
        results=[],
    )


def _install_short_writer_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    """给Windows长临时目录安装短且唯一的测试UUID，避免触发MAX_PATH。"""

    # Windows CI的pytest临时根较长，而生产UUID文件名会再叠加两次形成partial名。
    next_value = 0

    class _ShortUniqueId:
        """同时提供uuid对象的字符串形式和hex属性。"""

        def __init__(self, value: str) -> None:
            """保存当前测试进程内不会重复的短值。"""

            self.hex = value

        def __str__(self) -> str:
            """writer构造最终文件名时返回同一个短值。"""

            return self.hex

    def short_uuid4() -> _ShortUniqueId:
        """每次调用递增，仍然保留“不同回归不覆盖”的测试语义。"""

        nonlocal next_value
        next_value += 1
        return _ShortUniqueId(f"u{next_value}")

    # 只替换评测实现模块中的uuid4引用，不改变Python全局uuid行为。
    monkeypatch.setattr(grounded_success_module, "uuid4", short_uuid4)


def _hit(
    *,
    chunk_id: str,
    document_id: str,
    content: str,
) -> RetrievalHit:
    """复用真实知识Schema创建一条固定测试证据。"""

    # RetrievalHit模拟经过Qdrant、BM25和RRF后交给回答器的最终候选。
    return RetrievalHit(
        # KnowledgeChunk保留引用校验所需的完整治理元数据。
        chunk=KnowledgeChunk(
            # chunk_id是结构化回答允许引用的白名单ID。
            chunk_id=chunk_id,
            # document_id用于事实支持来源和父文档覆盖检查。
            document_id=document_id,
            # 测试标题不参与本文件的评分断言。
            title=f"{document_id}测试知识",
            # content是引用是否真正支持事实的唯一证据正文。
            content=content,
            # source使用不会访问网络的稳定测试地址。
            source=f"knowledge://test/{document_id}",
            # 固定版本和日期避免运行时间改变序列化结果。
            version="1.0",
            effective_date=date(2026, 8, 1),
            # 单切片测试从零开始，并以正文长度作为合法终点。
            chunk_index=0,
            start_index=0,
            end_index=len(content),
        ),
        # score只需位于领域模型允许范围内，本测试不比较排序。
        score=0.9,
    )


def _invoice_fact(fact_id: str) -> RequiredFactRule:
    """创建“已开票税号错误需要红冲重开”的可复用原子事实规则。"""

    # 答案与实际引用证据都必须同时表达“红冲”和“重开”两个关系组。
    return RequiredFactRule(
        # 每个测试使用独立ID，报告只暴露ID而不暴露规则正文。
        fact_id=fact_id,
        # 组间AND、组内OR允许模型使用“红字冲销”等合理同义表达。
        answer_all_of=[["红冲", "红字冲销"], ["重开", "重新开具"]],
        # 证据必须同样明确包含操作和后续动作。
        evidence_all_of=[["红冲", "红字冲销"], ["重开", "重新开具"]],
        # 只有人工标注的发票文档可以支撑该事实。
        supporting_document_ids=["DOC-INVOICE"],
    )


def _cancel_fact(fact_id: str) -> RequiredFactRule:
    """创建会被“不可以取消”反转的正向取消事实。"""

    # 该规则专门防止简单子串把否定句误判成正向事实。
    return RequiredFactRule(
        fact_id=fact_id,
        answer_all_of=[["可以取消"]],
        evidence_all_of=[["可以取消"]],
        supporting_document_ids=["DOC-CANCEL"],
    )


def _cannot_direct_edit_fact(fact_id: str) -> RequiredFactRule:
    """创建由短否定谓词支配对象短语的正确负向事实。"""

    # “不能”与“直接修改税号”拆组，验证评分器既理解否定关系，又不会误伤对象组。
    return RequiredFactRule(
        fact_id=fact_id,
        answer_all_of=[["不能", "不可以"], ["直接修改税号"]],
        evidence_all_of=[["不能", "不可以"], ["直接修改税号"]],
        supporting_document_ids=["DOC-INVOICE"],
    )


def _answerable_case(
    *,
    case_id: str,
    question: str,
    expected_document_id: str = "DOC-INVOICE",
    fact: RequiredFactRule | None = None,
    forbidden_claims: list[ForbiddenClaimRule] | None = None,
    tags: list[str] | None = None,
) -> GroundedAnswerSuccessCase:
    """创建一条带事实和正确来源的可回答测试题。"""

    # 默认使用发票事实；特殊否定测试可以注入取消事实。
    selected_fact = fact or _invoice_fact(f"{case_id}-fact")
    # 返回经过生产Pydantic组合校验的正例。
    return GroundedAnswerSuccessCase(
        case_id=case_id,
        question=question,
        should_answer=True,
        expected_document_ids=[expected_document_id],
        required_facts=[selected_fact],
        forbidden_claims=list(forbidden_claims or []),
        tags=list(tags or []),
    )


def _unanswerable_case(*, case_id: str, question: str) -> GroundedAnswerSuccessCase:
    """创建一条不能携带事实金标或正确文档的知识缺口题。"""

    # 负例只验证是否拒答，不向评测链路暗示任何正确答案。
    return GroundedAnswerSuccessCase(
        case_id=case_id,
        question=question,
        should_answer=False,
    )


class _FixedByQuestionRetriever:
    """按问题返回预设证据，并记录评测器实际传入的公开参数。"""

    def __init__(self, hits_by_question: dict[str, list[RetrievalHit]]) -> None:
        """保存每题证据副本，避免测试运行时被外部原地修改。"""

        # 每个值都复制为新列表，保持工厂输入不变。
        self._hits_by_question = {
            question: list(hits) for question, hits in hits_by_question.items()
        }
        # calls用于证明检索器只收到问题和Top-K。
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, *, top_k: int) -> list[RetrievalHit]:
        """记录公开查询并返回不超过Top-K的固定候选。"""

        # 保存最小协议参数，不保存任何Case或事实标签。
        self.calls.append((query, top_k))
        # 返回副本，防止回答器或评分器修改测试夹具。
        return list(self._hits_by_question.get(query, [])[:top_k])


class _RecordingAnswerClient:
    """返回预设结构化草稿，并记录模型协议可见的全部输入。"""

    def __init__(self, drafts_by_question: dict[str, GroundedAnswerDraft]) -> None:
        """保存每题输出和空调用记录。"""

        # 草稿已经通过真实GroundedAnswerDraft Schema校验。
        self._drafts_by_question = dict(drafts_by_question)
        # 每次调用只会记录question和evidence两个公开协议字段。
        self.calls: list[tuple[str, list[RetrievalHit]]] = []

    async def generate(
        self,
        *,
        question: str,
        evidence: list[RetrievalHit],
    ) -> GroundedAnswerDraft:
        """记录模型可见输入并返回当前问题的固定草稿。"""

        # 列表副本证明后续本地评分不会反向改变调用记录。
        self.calls.append((question, list(evidence)))
        # 返回预设结果；未知问题应让测试立即暴露夹具错误。
        return self._drafts_by_question[question]


@pytest.mark.asyncio
async def test_scoring_matrix_requires_correct_facts_supported_citations_and_safe_abstention(
) -> None:
    """逐题全或无评分必须识别缺事实、错事实、错引用和知识缺口放行。"""

    # 正确发票证据同时含有答案规则要求的两个原子关系。
    invoice_hit = _hit(
        chunk_id="chunk-invoice-proof",
        document_id="DOC-INVOICE",
        content="电子发票开具后不能直接修改税号，应先申请红冲，然后重新开具。",
    )
    # 同一父文档的无关切片用于证明“文档ID正确”仍不等于当前引用支持事实。
    invoice_without_proof = _hit(
        chunk_id="chunk-invoice-no-proof",
        document_id="DOC-INVOICE",
        content="电子发票可以从订单详情页面下载。",
    )
    # 取消证据包含正向“可以取消”，而模型答案会使用否定表达。
    cancel_hit = _hit(
        chunk_id="chunk-cancel-proof",
        document_id="DOC-CANCEL",
        content="订单尚未出库时可以取消。",
    )
    # 两条禁止结论分别覆盖真正错误断言与被否定后的安全解释。
    direct_edit_forbidden = ForbiddenClaimRule(
        claim_id="invoice-direct-edit",
        answer_any_of=["直接修改税号"],
    )

    # 九道题覆盖正例完整通过、两类否定上下文、事实缺失、引用问题和负例拒答。
    cases = [
        _answerable_case(
            case_id="answer-pass",
            question="已开发票税号写错后怎么处理？",
        ),
        _answerable_case(
            case_id="forbidden-negated-safely",
            question="税号错了能直接改原发票吗？",
            fact=_cannot_direct_edit_fact("cannot-direct-edit"),
            forbidden_claims=[direct_edit_forbidden],
        ),
        _answerable_case(
            case_id="required-fact-negated",
            question="订单出库之后还能取消吗？",
            expected_document_id="DOC-CANCEL",
            fact=_cancel_fact("cancel-allowed"),
        ),
        _answerable_case(
            case_id="required-fact-missing",
            question="已开发票信息错误应找谁处理？",
        ),
        _answerable_case(
            case_id="forbidden-claim-present",
            question="税号错了是否可以直接改？",
            forbidden_claims=[direct_edit_forbidden],
        ),
        _answerable_case(
            case_id="citation-not-in-candidates",
            question="发票错误流程和引用依据是什么？",
        ),
        _answerable_case(
            case_id="cited-chunk-does-not-support-fact",
            question="请给出发票更正动作以及证据。",
        ),
        _unanswerable_case(
            case_id="gap-safe-abstention",
            question="礼品卡余额可以保留多少年？",
        ),
        _unanswerable_case(
            case_id="gap-unsupported-answer",
            question="礼品卡到期后一定自动延期吗？",
        ),
    ]
    # 每题都返回至少一条候选，使拒答与回答由回答器真实决定。
    hits_by_question = {
        cases[0].question: [invoice_hit],
        cases[1].question: [invoice_hit],
        cases[2].question: [cancel_hit],
        cases[3].question: [invoice_hit],
        cases[4].question: [invoice_hit],
        cases[5].question: [invoice_hit],
        cases[6].question: [invoice_without_proof],
        cases[7].question: [invoice_hit],
        cases[8].question: [invoice_hit],
    }
    # 草稿故意构造不同质量，验证评分器不把引用合法误当成事实正确。
    drafts_by_question = {
        # 完整事实与正确引用应通过。
        cases[0].question: GroundedAnswerDraft(
            answer="需要先红冲，再重新开具正确税号的发票。",
            citation_ids=[invoice_hit.chunk.chunk_id],
            is_answerable=True,
        ),
        # “不能直接修改”是否定错误操作，不应命中禁止事实。
        cases[1].question: GroundedAnswerDraft(
            answer="不能直接修改税号，需要红冲后重新开具。",
            citation_ids=[invoice_hit.chunk.chunk_id],
            is_answerable=True,
        ),
        # “不可以取消”包含正向子串，但不能算作“可以取消”的事实命中。
        cases[2].question: GroundedAnswerDraft(
            answer="订单出库之后不可以取消。",
            citation_ids=[cancel_hit.chunk.chunk_id],
            is_answerable=True,
        ),
        # 引用正确但答案没有关键动作，应记录事实缺失。
        cases[3].question: GroundedAnswerDraft(
            answer="请联系人工客服进一步处理。",
            citation_ids=[invoice_hit.chunk.chunk_id],
            is_answerable=True,
        ),
        # 明确断言错误操作，即使同时给出正确动作也必须红线失败。
        cases[4].question: GroundedAnswerDraft(
            answer="可以直接修改税号，也可以红冲后重开。",
            citation_ids=[invoice_hit.chunk.chunk_id],
            is_answerable=True,
        ),
        # 答案事实正确但引用了候选之外的ID，不能通过。
        cases[5].question: GroundedAnswerDraft(
            answer="需要红冲后重新开具。",
            citation_ids=["chunk-invented"],
            is_answerable=True,
        ),
        # 引用ID和父文档正确，但实际切片不含红冲事实，仍属于无支持引用。
        cases[6].question: GroundedAnswerDraft(
            answer="需要红冲后重新开具。",
            citation_ids=[invoice_without_proof.chunk.chunk_id],
            is_answerable=True,
        ),
        # 知识缺口正确拒答且不带引用，应完整通过。
        cases[7].question: GroundedAnswerDraft(
            answer="当前证据不足，无法可靠回答。",
            citation_ids=[],
            is_answerable=False,
        ),
        # 知识缺口仍自动回答，形成零容忍无依据回答。
        cases[8].question: GroundedAnswerDraft(
            answer="礼品卡一定会自动延期。",
            citation_ids=[invoice_hit.chunk.chunk_id],
            is_answerable=True,
        ),
    }
    # 使用记录型替身执行完整本地评分。
    summary = await evaluate_grounded_answer_success(
        profile_id="scoring-matrix",
        cases=cases,
        query_policy=AllowAllKnowledgeQueryPolicy(),
        retriever=_FixedByQuestionRetriever(hits_by_question),
        answer_client=_RecordingAnswerClient(drafts_by_question),
        top_k=5,
        # 本测试把比例门放宽到零，只验证逐题规则与红线否决。
        min_success_rate=0.0,
        zero_tolerance_failure_codes=[
            "forbidden_claim_present",
            "invalid_or_unsupported_citation",
            "unsupported_answer_generated",
        ],
    )

    # 按稳定ID读取结果，避免测试依赖列表下标解释语义。
    by_id = {result.case_id: result for result in summary.results}
    # 三条真正完整的结果是普通正例、安全否定说明和知识缺口拒答。
    assert summary.passed_cases == 3
    assert summary.grounded_answer_success_rate == pytest.approx(3 / 9)
    assert by_id["answer-pass"].passed is True
    assert by_id["forbidden-negated-safely"].passed is True
    assert by_id["forbidden-negated-safely"].forbidden_claim_ids == []
    assert by_id["gap-safe-abstention"].passed is True
    # 否定的正向事实必须被视为缺失，而不是被简单子串误判为命中。
    assert "required_fact_missing" in by_id["required-fact-negated"].failure_codes
    assert by_id["required-fact-negated"].matched_fact_ids == []
    # 正确引用但缺事实，只产生事实缺失而不是引用红线。
    assert by_id["required-fact-missing"].failure_codes == ["required_fact_missing"]
    # 真正肯定的错误操作应命中禁止结论。
    assert "forbidden_claim_present" in by_id["forbidden-claim-present"].failure_codes
    # 两类引用问题都必须使用同一稳定红线码，同时保留无支持事实ID。
    assert (
        "invalid_or_unsupported_citation"
        in by_id["citation-not-in-candidates"].failure_codes
    )
    assert by_id["cited-chunk-does-not-support-fact"].unsupported_fact_ids
    assert (
        "invalid_or_unsupported_citation"
        in by_id["cited-chunk-does-not-support-fact"].failure_codes
    )
    # 负例错误回答必须触发独立的无依据回答红线。
    assert by_id["gap-unsupported-answer"].failure_codes == [
        "unsupported_answer_generated"
    ]
    # 比例门虽被放宽，任一红线仍会否决整体验收。
    assert summary.quality_gate_passed is False
    assert summary.quality_gate_failures == ["zero_tolerance_failure_detected"]


@pytest.mark.asyncio
async def test_default_run_is_zero_cost_and_never_reads_private_blind_dataset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认运行只能展示公开计划，不能读取盲题、Key或调用真实模型。"""

    # 先读取不含题目正文的公开配置，再安装私有路径读取守卫。
    config = load_grounded_answer_success_config(CONFIG_PATH)
    # resolved路径用于同时拦截read_text和read_bytes两种潜在访问方式。
    blind_path = (PROJECT_ROOT / config.blind_dataset_path).resolve()
    # 保存Path原方法，非私有文件仍要允许语料规划正常读取。
    original_read_text = Path.read_text
    original_read_bytes = Path.read_bytes

    def guarded_read_text(path: Path, *args: object, **kwargs: object) -> str:
        """私有文件一旦被默认路径读取就立即让测试失败。"""

        # 路径比较只用于测试，不把绝对用户目录写入错误或报告。
        if path.resolve() == blind_path:
            raise AssertionError("默认运行不得读取私有盲测正文")
        # 其他公开语料和配置继续调用真实Path实现。
        return original_read_text(path, *args, **kwargs)

    def guarded_read_bytes(path: Path) -> bytes:
        """同时阻止默认路径通过摘要计算间接读取盲测字节。"""

        # 私有文件连Hash也不能在未确认阶段重新读取。
        if path.resolve() == blind_path:
            raise AssertionError("默认运行不得读取私有盲测字节")
        # 公开语料摘要仍使用真实原始字节。
        return original_read_bytes(path)

    # 两个守卫只在当前测试生效，结束后pytest自动恢复。
    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    # 显式清空真实模型配置，证明默认计划路径与个人.env无关。
    settings = Settings(
        llm_backend="mock",
        llm_api_key=None,
        llm_base_url=None,
        telemetry_enabled=False,
    )

    # 未提供confirm_blind或confirm_paid_api，只计算公开费用上界。
    report = await run_grounded_answer_success_experiment(
        config,
        runtime_settings=settings,
    )

    # 没有读取私有题就不应出现任何基线或候选逐题结果。
    assert report.offline_baseline is None
    assert report.qwen_candidate is None
    # 所有真实外部调用计数必须保持零。
    assert report.paid_api_called is False
    assert report.actual_embedding_requests == 0
    assert report.actual_embedding_input_tokens == 0
    assert report.actual_chat_calls == 0
    # 公开计划仍能根据冻结计数和语料切片提前给出非零上界。
    assert report.planned_embedding_requests >= 1
    assert report.planned_chat_calls == config.blind_case_count
    # 单独调用私有加载器也必须要求显式确认，并在检查路径存在前停止。
    with pytest.raises(ValueError, match="显式确认"):
        load_private_grounded_answer_cases(config, confirm_blind=False)


@pytest.mark.asyncio
async def test_gold_labels_are_not_sent_to_retriever_or_answer_client() -> None:
    """模型协议只能看到问题与检索证据，不能收到Fact、Claim或Tag金标。"""

    # 三个marker故意使用不会自然出现在问题和知识证据中的私有标签值。
    private_fact_id = "gold-only-fact-id-do-not-send"
    private_claim_id = "gold-only-claim-id-do-not-send"
    private_tag = "gold-only-tag-do-not-send"
    # 证据包含真实业务事实，但不包含任何评测标签ID。
    hit = _hit(
        chunk_id="chunk-label-isolation",
        document_id="DOC-INVOICE",
        content="已开具发票的税号错误时，需要先红冲再重新开具。",
    )
    # 正例携带完整事实、禁止结论和私有复盘Tag。
    answerable = _answerable_case(
        case_id="label-isolation-positive",
        question="已经开票后发现税号错误怎么办？",
        fact=_invoice_fact(private_fact_id),
        forbidden_claims=[
            ForbiddenClaimRule(
                claim_id=private_claim_id,
                answer_any_of=["绝密错误金标正文"],
            )
        ],
        tags=[private_tag],
    )
    # 需要一条负例满足评测器的双侧数据约束。
    negative = _unanswerable_case(
        case_id="label-isolation-negative",
        question="积分能否永久保留？",
    )
    # 两题都进入回答客户端，便于完整检查其可见输入。
    retriever = _FixedByQuestionRetriever(
        {
            answerable.question: [hit],
            negative.question: [hit],
        }
    )
    answer_client = _RecordingAnswerClient(
        {
            answerable.question: GroundedAnswerDraft(
                answer="需要先红冲，再重新开具。",
                citation_ids=[hit.chunk.chunk_id],
                is_answerable=True,
            ),
            negative.question: GroundedAnswerDraft(
                answer="当前证据不足，无法可靠回答。",
                citation_ids=[],
                is_answerable=False,
            ),
        }
    )

    # 执行完整评测；若实现把整个Case作为额外参数传给替身，Python签名会直接报错。
    summary = await evaluate_grounded_answer_success(
        profile_id="label-isolation",
        cases=[answerable, negative],
        query_policy=AllowAllKnowledgeQueryPolicy(),
        retriever=retriever,
        answer_client=answer_client,
        top_k=5,
        min_success_rate=1.0,
        zero_tolerance_failure_codes=[
            "forbidden_claim_present",
            "invalid_or_unsupported_citation",
            "unsupported_answer_generated",
        ],
    )

    # 两道题都应按公开问题字符串进入检索和回答协议。
    assert [query for query, _ in retriever.calls] == [
        answerable.question,
        negative.question,
    ]
    assert [question for question, _ in answer_client.calls] == [
        answerable.question,
        negative.question,
    ]
    # 序列化回答客户端真正收到的参数，模拟对模型Prompt边界做审计。
    visible_to_model = json.dumps(
        [
            {
                "question": question,
                "evidence": [hit.model_dump(mode="json") for hit in evidence],
            }
            for question, evidence in answer_client.calls
        ],
        ensure_ascii=False,
    )
    # 事实ID、错误结论ID、复盘Tag和错误金标正文都不能进入模型输入。
    assert private_fact_id not in visible_to_model
    assert private_claim_id not in visible_to_model
    assert private_tag not in visible_to_model
    assert "绝密错误金标正文" not in visible_to_model
    # 评分仍能在模型返回后使用金标完成本地判断。
    assert summary.quality_gate_passed is True
    assert summary.passed_cases == summary.total_cases == 2


@pytest.mark.asyncio
async def test_report_serialization_keeps_metrics_but_removes_questions_answers_and_gold_text(
) -> None:
    """可保存报告只能暴露计数、ID和原因码，不能泄漏盲题或答案正文。"""

    # 使用容易在序列化字符串中搜索的独特题目、答案与金标正文。
    private_question = "PRIVATE-QUESTION-第34步不可进入报告"
    private_answer = "PRIVATE-ANSWER-红冲后重新开具"
    private_forbidden_text = "PRIVATE-FORBIDDEN-直接改税号"
    hit = _hit(
        chunk_id="chunk-report-sanitized",
        document_id="DOC-INVOICE",
        content="税号错误的已开发票需要红冲后重新开具。",
    )
    positive = _answerable_case(
        case_id="report-positive",
        question=private_question,
        fact=_invoice_fact("report-private-fact-id"),
        forbidden_claims=[
            ForbiddenClaimRule(
                claim_id="report-private-claim-id",
                answer_any_of=[private_forbidden_text],
            )
        ],
    )
    negative = _unanswerable_case(
        case_id="report-negative",
        question="PRIVATE-QUESTION-知识缺口不可进入报告",
    )
    retriever = _FixedByQuestionRetriever(
        {positive.question: [hit], negative.question: [hit]}
    )
    answer_client = _RecordingAnswerClient(
        {
            positive.question: GroundedAnswerDraft(
                answer=private_answer,
                citation_ids=[hit.chunk.chunk_id],
                is_answerable=True,
            ),
            negative.question: GroundedAnswerDraft(
                answer="PRIVATE-ANSWER-拒答正文不可进入报告",
                citation_ids=[],
                is_answerable=False,
            ),
        }
    )
    # 先得到真实评分Summary，再嵌入最终实验报告Schema。
    summary = await evaluate_grounded_answer_success(
        profile_id="sanitized-report",
        cases=[positive, negative],
        query_policy=AllowAllKnowledgeQueryPolicy(),
        retriever=retriever,
        answer_client=answer_client,
        top_k=5,
        min_success_rate=1.0,
        zero_tolerance_failure_codes=[
            "forbidden_claim_present",
            "invalid_or_unsupported_citation",
            "unsupported_answer_generated",
        ],
    )
    config = load_grounded_answer_success_config(CONFIG_PATH)
    # 报告字段全部来自公开配置、费用计数和脱敏Summary。
    report = GroundedAnswerSuccessExperimentReport(
        experiment_id=config.experiment_id,
        experiment_version=config.version,
        evaluator_version=config.evaluator_version,
        corpus_sha256=config.corpus_sha256,
        blind_dataset_sha256=config.blind_dataset_sha256,
        blind_case_count=summary.total_cases,
        candidate_profile_id=config.candidate_profile_id,
        candidate_fingerprint=grounded_answer_candidate_fingerprint(config),
        embedding_model=config.embedding_model,
        chat_model=config.chat_model,
        planned_embedding_requests=1,
        planned_chat_calls=summary.total_cases,
        paid_api_called=False,
        actual_embedding_requests=0,
        actual_embedding_input_tokens=0,
        actual_chat_calls=0,
        offline_baseline=summary,
    )
    # 使用JSON模式模拟example脚本真正写入data/runtime的内容。
    serialized = report.model_dump_json()

    # 计数和通过率仍应保留，方便面试和回归比较。
    assert '"grounded_answer_success_rate":1.0' in serialized
    assert '"passed_cases":2' in serialized
    # 任何题目、答案、事实规则或禁止表述正文都不得进入报告。
    for private_value in (
        private_question,
        negative.question,
        private_answer,
        "PRIVATE-ANSWER-拒答正文不可进入报告",
        private_forbidden_text,
        "红冲后重新开具",
    ):
        assert private_value not in serialized
    # 顶层键名也不能意外恢复原始Case或模型草稿字段。
    assert '"question"' not in serialized
    assert '"answer"' not in serialized
    assert '"required_facts"' not in serialized
    assert '"forbidden_claims"' not in serialized
    assert "Bearer " not in serialized
    assert "sk-" not in serialized


def test_public_config_and_candidate_fingerprint_freeze_complete_candidate_contract() -> None:
    """公开配置必须冻结数据摘要、唯一质量门和所有会改变答案的候选参数。"""

    # 两次独立加载模拟不同进程和不同机器读取同一UTF-8配置。
    first = load_grounded_answer_success_config(CONFIG_PATH)
    second = load_grounded_answer_success_config(CONFIG_PATH)
    first_fingerprint = grounded_answer_candidate_fingerprint(first)
    second_fingerprint = grounded_answer_candidate_fingerprint(second)

    # 同一候选应得到稳定SHA，并与揭晓前配置中的冻结值完全一致。
    assert first_fingerprint == second_fingerprint
    assert first.frozen_candidate_fingerprint == first_fingerprint
    # 私有文件正文不在公开配置中，但其摘要和固定正负例分母必须齐全。
    assert first.blind_answerable_count + first.blind_unanswerable_count == (
        first.blind_case_count
    )
    assert len(first.blind_dataset_sha256) == 64
    assert len(first.corpus_sha256) == 64
    # 本实验只公开一个平均成功率门；三类严重错误走零容忍否决。
    assert first.min_grounded_answer_success_rate == 0.8
    assert first.zero_tolerance_failure_codes == [
        "forbidden_claim_present",
        "invalid_or_unsupported_citation",
        "unsupported_answer_generated",
    ]

    # Embedding、聊天模型、温度和上下文预算任一变化都必须产生新候选身份。
    changed_candidates = (
        first.model_copy(update={"embedding_model": "another-embedding-model"}),
        first.model_copy(update={"chat_model": "another-chat-model"}),
        first.model_copy(update={"chat_temperature": 0.2}),
        first.model_copy(update={"max_context_chars": first.max_context_chars + 500}),
        first.model_copy(update={"rrf_k": first.rrf_k + 1}),
    )
    assert all(
        grounded_answer_candidate_fingerprint(changed) != first_fingerprint
        for changed in changed_candidates
    )

    # 配置摘要若与正负例数量矛盾，应在读取盲测正文前直接失败。
    invalid_payload = first.model_dump(mode="json")
    invalid_payload["blind_case_count"] = first.blind_case_count + 1
    with pytest.raises(ValueError, match="正负例计数"):
        GroundedAnswerSuccessExperimentConfig.model_validate(invalid_payload)


def test_evidence_label_preflight_uses_real_frozen_chunks_before_blind_reveal() -> None:
    """事实证据锚点必须在真实500/80切片中可命中，拼错标签应在模型调用前失败。"""

    # 复用公开配置中的真实语料SHA与切片参数，但不读取任何私有盲测文件。
    config = load_grounded_answer_success_config(CONFIG_PATH)
    # 正确样本引用真实电子发票文档中同一切片可找到的完整事实。
    valid_case = GroundedAnswerSuccessCase(
        case_id="label-preflight-valid",
        question="测试问题只用于Schema，不会发送给模型。",
        should_answer=True,
        expected_document_ids=["KB-INVOICE-GENERAL-001"],
        required_facts=[
            RequiredFactRule(
                fact_id="invoice-real-evidence",
                answer_all_of=[["金额"], ["品名"]],
                evidence_all_of=[["不能脱离真实交易任意改变金额或品名"]],
                supporting_document_ids=["KB-INVOICE-GENERAL-001"],
            )
        ],
    )
    # 正确锚点应静默通过，证明预检按真实切片而不是整篇假数据运行。
    validate_grounded_answer_evidence_labels(config, [valid_case])

    # 只替换成语料中不存在的锚点，模拟人工标签拼写或切片边界错误。
    invalid_case = valid_case.model_copy(
        update={
            "case_id": "label-preflight-invalid",
            "required_facts": [
                valid_case.required_facts[0].model_copy(
                    update={"evidence_all_of": [["完全不存在的证据锚点-XYZ"]]}
                )
            ],
        }
    )
    # 错误只公开稳定Case/Fact ID，不打印问题或事实正文。
    with pytest.raises(ValueError, match="label-preflight-invalid:invoice-real-evidence"):
        validate_grounded_answer_evidence_labels(config, [invalid_case])


def test_cli_argument_contract_covers_every_switch_and_frozen_result_state() -> None:
    """四个CLI开关与首次结果存在性共32种组合都必须遵守同一状态机。"""

    # 动态加载example脚本只取得参数校验函数，不启动事件循环或真实实验。
    cli_module = _load_step34_cli_module()
    # 两种首次结果状态乘以四个布尔开关，完整覆盖全部32种组合。
    for frozen_result_exists in (False, True):
        for confirm_blind in (False, True):
            for confirm_paid_api in (False, True):
                for regression in (False, True):
                    for write_private_diagnostics in (False, True):
                        # Namespace与argparse真实返回对象字段完全一致。
                        args = argparse.Namespace(
                            confirm_blind=confirm_blind,
                            confirm_paid_api=confirm_paid_api,
                            regression=regression,
                            write_private_diagnostics=write_private_diagnostics,
                        )
                        # 只有满足五条契约的组合才能继续读取配置、.env或私有题。
                        expected_valid = (
                            not (confirm_paid_api and not confirm_blind)
                            and not (regression and not confirm_paid_api)
                            and not (regression and not frozen_result_exists)
                            and not (
                                write_private_diagnostics
                                and not (
                                    confirm_blind
                                    and confirm_paid_api
                                    and regression
                                )
                            )
                            and not (
                                confirm_paid_api
                                and frozen_result_exists
                                and not regression
                            )
                        )
                        # 合法组合应静默通过，且不需要访问真实冻结文件。
                        if expected_valid:
                            cli_module._validate_argument_contract(
                                args,
                                frozen_result_exists=frozen_result_exists,
                            )
                        else:
                            # 非法组合统一在最外层快速失败，不进入任何有费用路径。
                            with pytest.raises(ValueError):
                                cli_module._validate_argument_contract(
                                    args,
                                    frozen_result_exists=frozen_result_exists,
                                )


def test_first_frozen_result_is_created_once_and_never_overwritten(
    tmp_path: Path,
) -> None:
    """首次公开结果必须使用独占发布，第二次写入不能改变一个字节。"""

    # 动态脚本中的target_path测试接口让验证完全落在pytest临时目录。
    cli_module = _load_step34_cli_module()
    target_path = tmp_path / "frozen-result.json"
    # 第一次写入使用候选A的聚合结果。
    cli_module._write_public_frozen_result(
        _minimal_summary(profile_id="candidate-first"),
        experiment_id="experiment-first",
        experiment_version="1.0.0",
        dataset_sha256="a" * 64,
        candidate_fingerprint="b" * 64,
        target_path=target_path,
    )
    # 保存原始字节作为不可变历史证据。
    first_bytes = target_path.read_bytes()
    # 第二次即使内容和候选身份不同，也必须由操作系统独占语义拒绝。
    with pytest.raises(FileExistsError):
        cli_module._write_public_frozen_result(
            _minimal_summary(profile_id="candidate-second"),
            experiment_id="experiment-second",
            experiment_version="9.9.9",
            dataset_sha256="c" * 64,
            candidate_fingerprint="d" * 64,
            target_path=target_path,
        )
    # 写入失败后首次结果逐字节保持不变。
    assert target_path.read_bytes() == first_bytes
    # 同目录不允许残留可能包含半份结果的partial临时文件。
    assert list(tmp_path.glob("*.partial")) == []
    assert list(tmp_path.glob(".*.partial")) == []


def test_regression_snapshot_detects_change_and_deletion(tmp_path: Path) -> None:
    """回归前后快照既接受完全未变文件，也能发现覆盖、触碰和删除。"""

    # 快照函数来自CLI保护层，不读取或解析任何真实实验结果。
    cli_module = _load_step34_cli_module()
    frozen_path = tmp_path / "frozen-result.json"
    frozen_path.write_text('{"result":"first"}\n', encoding="utf-8")
    # 首次读取同时冻结SHA、字节数和纳秒修改时间。
    snapshot = cli_module._snapshot_frozen_result(frozen_path)
    # 未做任何修改时必须通过，避免把正常回归误判为破坏历史。
    cli_module._assert_frozen_result_unchanged(frozen_path, snapshot)
    # 即使文件仍是合法JSON，任何内容变化都属于覆盖首次证据。
    frozen_path.write_text('{"result":"changed"}\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="发生变化"):
        cli_module._assert_frozen_result_unchanged(frozen_path, snapshot)
    # 删除历史文件也必须给出独立明确错误。
    frozen_path.unlink()
    with pytest.raises(RuntimeError, match="被删除"):
        cli_module._assert_frozen_result_unchanged(frozen_path, snapshot)


def test_first_paid_run_lock_is_mutually_exclusive_and_always_cleaned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """首次付费锁只能由一个进程持有，并在正常和异常退出时清理。"""

    # 把全局锁路径重定向到pytest目录，绝不碰真实runtime锁。
    cli_module = _load_step34_cli_module()
    lock_path = tmp_path / "first-paid-run.lock"
    monkeypatch.setattr(cli_module, "FIRST_RUN_LOCK_PATH", lock_path)
    # 第一位运行者进入后，锁文件应贯穿整个上下文生命周期。
    with cli_module._exclusive_first_paid_run_lock(enabled=True):
        assert lock_path.is_file()
        # 第二位运行者在读取Key或调用模型前就因x模式创建失败。
        with (
            pytest.raises(RuntimeError, match="已有首次付费实验正在运行"),
            cli_module._exclusive_first_paid_run_lock(enabled=True),
        ):
            raise AssertionError("第二位运行者不应进入受保护区域")
    # 正常退出必须释放锁。
    assert not lock_path.exists()
    # 业务代码抛错时finally同样必须释放锁，允许人工修复后重新运行。
    with (
        pytest.raises(RuntimeError, match="模拟实验失败"),
        cli_module._exclusive_first_paid_run_lock(enabled=True),
    ):
        raise RuntimeError("模拟实验失败")
    assert not lock_path.exists()


@pytest.mark.parametrize(
    ("confirm_blind", "confirm_paid_api", "confirm_regression"),
    [
        (False, False, False),
        (True, False, False),
        (True, True, False),
        (False, True, True),
    ],
)
@pytest.mark.asyncio
async def test_private_collector_requires_all_three_runner_confirmations_before_reads(
    monkeypatch: pytest.MonkeyPatch,
    confirm_blind: bool,
    confirm_paid_api: bool,
    confirm_regression: bool,
) -> None:
    """Collector缺少任一确认时必须在语料、盲题和模型访问前归零失败。"""

    # 公开配置本身不含私有题目，可以在安装读取守卫前安全加载。
    config = load_grounded_answer_success_config(CONFIG_PATH)
    collector = PrivateGroundedAnswerDiagnosticCollector()
    # 任何后续语料验证都代表安全检查顺序倒置，因此立即让测试失败。
    touched_after_guard = False

    def forbidden_corpus_access(
        _config: GroundedAnswerSuccessExperimentConfig,
    ) -> Path:
        """记录并拒绝非法参数状态下的第一处文件读取入口。"""

        nonlocal touched_after_guard
        touched_after_guard = True
        raise AssertionError("非法Collector不得读取语料或私有盲测")

    # _verify_corpus位于任何正常执行路径的最前端，足以证明快速失败顺序。
    monkeypatch.setattr(
        grounded_success_module,
        "_verify_corpus",
        forbidden_corpus_access,
    )
    # 无Key设置进一步证明失败与个人.env和外部服务无关。
    settings = Settings(
        llm_backend="mock",
        llm_api_key=None,
        llm_base_url=None,
        telemetry_enabled=False,
    )
    # 三把钥匙不全时，runner必须拒绝Collector。
    with pytest.raises(ValueError, match="私有诊断Collector"):
        await run_grounded_answer_success_experiment(
            config,
            runtime_settings=settings,
            confirm_blind=confirm_blind,
            confirm_paid_api=confirm_paid_api,
            confirm_regression=confirm_regression,
            private_diagnostic_collector=collector,
        )
    # 守卫保持False证明没有读取公开语料，更不可能读取私有盲题或调用API。
    assert touched_after_guard is False
    assert collector.case_count == 0


@pytest.mark.asyncio
async def test_evaluator_default_does_not_construct_private_diagnostic_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未传Collector时即使完成评分，也不能在内存中组装完整私有Record。"""

    # 两题满足评测器正负例约束，问题正文故意带私有marker。
    positive = _answerable_case(
        case_id="collector-off-positive",
        question="PRIVATE-COLLECTOR-OFF-QUESTION",
    )
    negative = _unanswerable_case(
        case_id="collector-off-negative",
        question="PRIVATE-COLLECTOR-OFF-GAP",
    )
    hit = _hit(
        chunk_id="chunk-collector-off",
        document_id="DOC-INVOICE",
        content="税号错误需要先红冲后重新开具。",
    )

    def reject_private_record_construction(**_values: object) -> object:
        """若默认分支尝试组装题目、证据与答案，就立即失败。"""

        raise AssertionError("未提供Collector时不得构造私有诊断Record")

    # 最终全景Record只应出现在显式Collector分支中。
    monkeypatch.setattr(
        grounded_success_module,
        "PrivateGroundedAnswerDiagnosticRecord",
        reject_private_record_construction,
    )
    # 调用时故意省略private_diagnostic_collector，验证默认开关确实为关闭。
    summary = await evaluate_grounded_answer_success(
        profile_id="collector-default-off",
        cases=[positive, negative],
        query_policy=AllowAllKnowledgeQueryPolicy(),
        retriever=_FixedByQuestionRetriever(
            {
                positive.question: [hit],
                negative.question: [hit],
            }
        ),
        answer_client=_RecordingAnswerClient(
            {
                positive.question: GroundedAnswerDraft(
                    answer="需要先红冲后重新开具。",
                    citation_ids=[hit.chunk.chunk_id],
                    is_answerable=True,
                ),
                negative.question: GroundedAnswerDraft(
                    answer="当前证据不足。",
                    citation_ids=[],
                    is_answerable=False,
                ),
            }
        ),
        top_k=5,
        min_success_rate=1.0,
        zero_tolerance_failure_codes=[
            "forbidden_claim_present",
            "invalid_or_unsupported_citation",
            "unsupported_answer_generated",
        ],
    )
    # 评分照常完成，说明默认关闭诊断不影响公开质量结果。
    assert summary.total_cases == 2
    assert "PRIVATE-COLLECTOR-OFF" not in summary.model_dump_json()


async def _collect_private_diagnostic_fixture() -> tuple[
    PrivateGroundedAnswerDiagnosticCollector,
    GroundedAnswerSuccessSummary,
]:
    """用纯内存替身创建包含私有marker的两题诊断夹具。"""

    # marker故意分布在问题、答案、证据和金标中，方便验证公开边界。
    private_question = "PRIVATE-MARKER-QUESTION-不得公开"
    hit = _hit(
        chunk_id="chunk-private-diagnostic",
        document_id="DOC-INVOICE",
        content="PRIVATE-MARKER-EVIDENCE：税号错误需要先红冲后重新开具。",
    )
    positive = _answerable_case(
        case_id="diagnostic-positive",
        question=private_question,
        fact=RequiredFactRule(
            fact_id="private-fact-id",
            answer_all_of=[["红冲"], ["重新开具"]],
            evidence_all_of=[["红冲"], ["重新开具"]],
            supporting_document_ids=["DOC-INVOICE"],
        ),
        tags=["PRIVATE-MARKER-GOLD-TAG"],
    )
    negative = _unanswerable_case(
        case_id="diagnostic-negative",
        question="PRIVATE-MARKER-GAP-QUESTION-不得公开",
    )
    # 同一证据只为确保两题都会进入回答器，整个测试不访问文件或网络。
    retriever = _FixedByQuestionRetriever(
        {
            positive.question: [hit],
            negative.question: [hit],
        }
    )
    answer_client = _RecordingAnswerClient(
        {
            positive.question: GroundedAnswerDraft(
                answer="PRIVATE-MARKER-ANSWER：需要红冲后重新开具。",
                citation_ids=[hit.chunk.chunk_id],
                is_answerable=True,
            ),
            negative.question: GroundedAnswerDraft(
                answer="PRIVATE-MARKER-ABSTENTION：当前证据不足。",
                citation_ids=[],
                is_answerable=False,
            ),
        }
    )
    # 显式创建Collector是唯一会组装完整问题、证据与答案记录的开关。
    collector = PrivateGroundedAnswerDiagnosticCollector()
    summary = await evaluate_grounded_answer_success(
        profile_id="private-diagnostic-test",
        cases=[positive, negative],
        query_policy=AllowAllKnowledgeQueryPolicy(),
        retriever=retriever,
        answer_client=answer_client,
        top_k=5,
        min_success_rate=1.0,
        zero_tolerance_failure_codes=[
            "forbidden_claim_present",
            "invalid_or_unsupported_citation",
            "unsupported_answer_generated",
        ],
        private_diagnostic_collector=collector,
    )
    # 返回完整私有Collector和与之对应的公开脱敏Summary。
    return collector, summary


@pytest.mark.asyncio
async def test_private_collector_is_opt_in_and_public_output_stays_sanitized(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """只有显式Collector保存原始内容，公开Summary与控制台仍不能泄漏marker。"""

    # 先运行启用Collector的内存夹具，证明私有记录确实包含人工复盘所需原文。
    collector, summary = await _collect_private_diagnostic_fixture()
    assert collector.case_count == 2
    private_serialized = json.dumps(
        [record.model_dump(mode="json") for record in collector.records],
        ensure_ascii=False,
    )
    assert "PRIVATE-MARKER-QUESTION" in private_serialized
    assert "PRIVATE-MARKER-ANSWER" in private_serialized
    assert "PRIVATE-MARKER-EVIDENCE" in private_serialized
    assert "PRIVATE-MARKER-GOLD-TAG" in private_serialized
    # 公开Summary只保留case_id、事实ID和原因码，不含任何原始正文。
    public_serialized = summary.model_dump_json()
    assert "PRIVATE-MARKER" not in public_serialized
    # CLI标准输出同样只打印比例、失败数量、case_id与原因码。
    cli_module = _load_step34_cli_module()
    cli_module._print_summary("千问回归候选", summary)
    captured = capsys.readouterr()
    assert "PRIVATE-MARKER" not in captured.out
    assert "PRIVATE-MARKER" not in captured.err
    # 未提供Collector时，评测函数没有任何隐式全局文件或缓存可保存私有记录。
    assert "private_diagnostic_collector" not in summary.model_dump()


@pytest.mark.asyncio
async def test_private_writer_creates_unique_regression_files_in_private_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """每次私有写盘都生成独立REGRESSION文件，并且内容不进入公开目录。"""

    # 使用纯内存评测结果准备两题完整诊断。
    collector, _ = await _collect_private_diagnostic_fixture()
    private_root = tmp_path / "data/private_evaluation"

    def resolve_private_root(relative_path: str | Path) -> Path:
        """只允许writer请求其固定私有根，并映射到临时目录。"""

        assert Path(relative_path).as_posix() == "data/private_evaluation"
        return private_root

    # 替换项目路径解析器，确保测试不创建真实data/private_evaluation文件。
    monkeypatch.setattr(
        grounded_success_module,
        "resolve_project_path",
        resolve_private_root,
    )
    # 缩短Windows临时文件名，但仍用递增值验证两次写入保持唯一。
    _install_short_writer_uuid(monkeypatch)
    writer_kwargs = {
        "experiment_id": "private-writer-test",
        "experiment_version": "1.0.0",
        "dataset_sha256": "a" * 64,
        "candidate_fingerprint": "b" * 64,
        "profile_id": "qwen-regression-test",
    }
    # 连续写两次模拟两轮回归，每轮都必须保留而不是覆盖上一轮。
    first_path = write_private_grounded_answer_diagnostics(
        collector,
        **writer_kwargs,
    )
    second_path = write_private_grounded_answer_diagnostics(
        collector,
        **writer_kwargs,
    )
    assert first_path != second_path
    assert first_path.is_file()
    assert second_path.is_file()
    assert first_path.name.endswith("_REGRESSION.json")
    assert second_path.name.endswith("_REGRESSION.json")
    # 两个路径都必须位于固定私有诊断子目录内。
    expected_parent = (
        private_root / "diagnostics/grounded_answer_success"
    ).resolve()
    assert first_path.parent == expected_parent
    assert second_path.parent == expected_parent
    # 私有payload明确标记回归性质，并保存完整marker供本地复盘。
    first_payload = json.loads(first_path.read_text(encoding="utf-8"))
    assert first_payload["run_kind"] == "REGRESSION"
    assert first_payload["record_count"] == 2
    assert "PRIVATE-MARKER-QUESTION" in json.dumps(
        first_payload,
        ensure_ascii=False,
    )
    # 成功发布后不能残留任何可能被误上传的partial别名。
    assert list(expected_parent.glob("*.partial")) == []
    assert list(expected_parent.glob(".*.partial")) == []


@pytest.mark.asyncio
async def test_private_writer_failure_removes_all_partial_and_final_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """私有写盘在fsync异常时不能留下半文件、最终文件或私有正文碎片。"""

    # Collector完全在内存生成，不读取真实盲题。
    collector, _ = await _collect_private_diagnostic_fixture()
    private_root = tmp_path / "data/private_evaluation"
    monkeypatch.setattr(
        grounded_success_module,
        "resolve_project_path",
        lambda _relative_path: private_root,
    )
    # 避免Windows长pytest路径在到达fsync模拟点前触发MAX_PATH。
    _install_short_writer_uuid(monkeypatch)

    def fail_fsync(_file_descriptor: int) -> None:
        """模拟操作系统在完整落盘前报告磁盘写入失败。"""

        raise OSError("模拟磁盘刷盘失败")

    # writer模块自己的os引用被替换，异常发生在最终路径发布之前。
    monkeypatch.setattr(grounded_success_module.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="模拟磁盘刷盘失败"):
        write_private_grounded_answer_diagnostics(
            collector,
            experiment_id="private-writer-failure",
            experiment_version="1.0.0",
            dataset_sha256="c" * 64,
            candidate_fingerprint="d" * 64,
            profile_id="qwen-regression-test",
        )
    # 目录可以存在，但目录树中不得保留任何含私有内容的文件。
    assert [path for path in private_root.rglob("*") if path.is_file()] == []


def test_private_evaluation_directory_is_excluded_from_git_and_docker() -> None:
    """完整私有目录必须同时排除于Git提交与Docker构建上下文。"""

    # 两份忽略文件都属于公开仓库配置，不涉及任何私有题目内容。
    gitignore_lines = {
        line.strip()
        for line in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    }
    dockerignore_lines = {
        line.strip()
        for line in (PROJECT_ROOT / ".dockerignore")
        .read_text(encoding="utf-8")
        .splitlines()
    }
    # 目录级规则覆盖sealed题集和未来所有diagnostics子目录。
    assert "data/private_evaluation/" in gitignore_lines
    assert "data/private_evaluation/" in dockerignore_lines

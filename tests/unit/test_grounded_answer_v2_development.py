"""验证第35步Prompt v2、开发评分Profile与零费用私有诊断重放。"""

# argparse直接构造CLI安全状态，不启动真实脚本或API。
import argparse

# importlib.util加载以数字开头、不能使用普通import语法的第35步示例。
import importlib.util

# date创建满足真实领域Schema的知识切片生效日期。
from datetime import date

# Path声明公开配置路径和pytest临时私有根。
from pathlib import Path

# ModuleType标注动态加载后的第35步脚本模块。
from types import ModuleType

# pytest提供异步测试、临时目录和monkeypatch隔离。
import pytest

# PROJECT_ROOT保证Windows、Linux和PyCharm使用同一项目根。
from serviceops_agent.config.paths import PROJECT_ROOT

# GroundedAnswerDraft与真实检索领域模型约束测试输入输出。
from serviceops_agent.domain.knowledge import (
    GroundedAnswerDraft,
    KnowledgeChunk,
    RetrievalHit,
)

# 第35步公共评分、私有重放和配置类型是本文件的验证对象。
from serviceops_agent.evaluation import (
    GroundedAnswerDevelopmentScoringProfile,
    GroundedAnswerFactGroupExtension,
    GroundedAnswerSuccessCase,
    PrivateGroundedAnswerDiagnosticCollector,
    RequiredFactRule,
    evaluate_grounded_answer_success,
    grounded_answer_candidate_fingerprint,
    load_grounded_answer_development_scoring_profile,
    load_grounded_answer_success_config,
    load_private_grounded_answer_diagnostic_collector,
    replay_private_grounded_answer_diagnostics,
    write_private_grounded_answer_diagnostics,
)

# 实现模块只用于把固定私有目录重定向到pytest临时目录。
from serviceops_agent.evaluation import (
    grounded_answer_success_experiment as grounded_success_module,
)

# 两版提示必须共存，旧实验继续使用v1，新开发候选显式使用v2。
from serviceops_agent.rag.generation import (
    GROUNDED_ANSWER_SYSTEM_PROMPT_V1,
    GROUNDED_ANSWER_SYSTEM_PROMPT_V2,
    grounded_answer_system_prompt,
)

# AllowAll策略让测试只关注评分差异，不受关键词范围门影响。
from serviceops_agent.rag.query_policy import AllowAllKnowledgeQueryPolicy

# 三个公开文件共同冻结v1、v2候选与开发评分修正。
V1_CONFIG_PATH = PROJECT_ROOT / "data/evaluation/grounded_answer_success_experiment.json"
V2_CONFIG_PATH = (
    PROJECT_ROOT / "data/evaluation/grounded_answer_v2_development_experiment.json"
)
SCORING_PROFILE_PATH = (
    PROJECT_ROOT / "data/evaluation/grounded_answer_development_scoring_v1_1.json"
)


def _load_step35_cli_module() -> ModuleType:
    """动态加载第35步脚本函数，但不进入__main__付费入口。"""

    # 文件名以数字开头，只能通过spec安全加载。
    script_path = PROJECT_ROOT / "examples/35_grounded_answer_v2_development.py"
    # 稳定测试模块名不会覆盖真实业务包。
    spec = importlib.util.spec_from_file_location(
        "serviceops_step35_cli_for_unit_tests",
        script_path,
    )
    # 仓库文件或loader缺失应给出明确测试错误。
    if spec is None or spec.loader is None:
        # 不继续访问None.loader。
        raise AssertionError("无法加载第35步CLI脚本")
    # 按Python标准流程创建空模块对象。
    module = importlib.util.module_from_spec(spec)
    # 只执行常量和函数定义；__main__保护阻止真正运行。
    spec.loader.exec_module(module)
    # 返回模块供参数状态机测试使用。
    return module


def _hit() -> RetrievalHit:
    """创建同时支持“不能只凭商品名称拒绝”的固定证据。"""

    # 正文包含严格原始证据锚点，只有答案同义表达需要v1.1扩展。
    content = "人工审核需要结合交易页面重新判断，不能只凭商品名称拒绝申请。"
    # 使用真实RetrievalHit和KnowledgeChunk Schema。
    return RetrievalHit(
        chunk=KnowledgeChunk(
            chunk_id="chunk-development-proof",
            document_id="DOC-DEVELOPMENT",
            title="开发评分测试知识",
            content=content,
            source="knowledge://test/development",
            version="1.0",
            effective_date=date(2026, 8, 24),
            chunk_index=0,
            start_index=0,
            end_index=len(content),
        ),
        score=0.95,
    )


class _Retriever:
    """按问题返回同一固定证据的纯内存Retriever。"""

    def __init__(self, hit: RetrievalHit) -> None:
        """保存证据深拷贝，避免测试之间共享可变对象。"""

        # model_copy保持真实Schema类型。
        self._hit = hit.model_copy(deep=True)

    def search(self, query: str, *, top_k: int) -> list[RetrievalHit]:
        """只接收公开问题和Top-K，返回一条证据。"""

        # 显式引用query证明接口没有接收Fact/Profile金标。
        _ = query
        # top_k至少为一时返回证据，否则返回空列表。
        return [self._hit.model_copy(deep=True)] if top_k >= 1 else []


class _AnswerClient:
    """返回预设结构化草稿的零网络回答替身。"""

    def __init__(self, drafts: dict[str, GroundedAnswerDraft]) -> None:
        """保存问题到答案的深拷贝映射。"""

        # 每个值复制，防止评分清理拒答引用时改写测试夹具。
        self._drafts = {
            question: draft.model_copy(deep=True)
            for question, draft in drafts.items()
        }

    async def generate(
        self,
        *,
        question: str,
        evidence: list[RetrievalHit],
    ) -> GroundedAnswerDraft:
        """忽略固定证据并返回当前问题的预设草稿。"""

        # evidence只证明协议与真实客户端一致。
        _ = evidence
        # 返回深拷贝保证两次v1/v1.1评分彼此隔离。
        return self._drafts[question].model_copy(deep=True)


def _cases_and_drafts(
    hit: RetrievalHit,
) -> tuple[list[GroundedAnswerSuccessCase], dict[str, GroundedAnswerDraft]]:
    """构造同义漏判、拒答带引用和真正无依据回答三种开发样本。"""

    # core_fact使用“不能”，模型会使用语义等价的“不得”。
    core_fact = RequiredFactRule(
        fact_id="core-name-rule",
        answer_all_of=[["不能"], ["仅凭商品名称"]],
        evidence_all_of=[["不能只凭商品名称拒绝申请"]],
        supporting_document_ids=["DOC-DEVELOPMENT"],
    )
    # background_fact模拟人工确认超出用户问题范围的背景项。
    background_fact = RequiredFactRule(
        fact_id="background-out-of-scope",
        answer_all_of=[["保存审核记录"]],
        evidence_all_of=[["保存审核记录"]],
        supporting_document_ids=["DOC-DEVELOPMENT"],
    )
    # 正例的预期文档同时支撑两个原始事实，满足Case组合Schema。
    positive = GroundedAnswerSuccessCase(
        case_id="development-positive",
        question="能只因为商品名称就拒绝申请吗？",
        should_answer=True,
        expected_document_ids=["DOC-DEVELOPMENT"],
        required_facts=[core_fact, background_fact],
    )
    # 第一条负例正确声明不可回答，但原模型草稿错误携带了相邻证据引用。
    declined = GroundedAnswerSuccessCase(
        case_id="development-declined",
        question="知识库没有的服务价格是多少？",
        should_answer=False,
    )
    # 第二条负例仍自动给出具体结论，必须保持零容忍失败。
    unsupported = GroundedAnswerSuccessCase(
        case_id="development-unsupported",
        question="知识库没有的服务每年免费几次？",
        should_answer=False,
    )
    # 三个草稿分别触发v1机械漏判、最终状态清理和真实红线。
    drafts = {
        positive.question: GroundedAnswerDraft(
            answer="不得仅凭商品名称拒绝申请，应结合交易页面人工审核。",
            citation_ids=[hit.chunk.chunk_id],
            is_answerable=True,
        ),
        declined.question: GroundedAnswerDraft(
            answer="当前证据没有该服务的价格信息。",
            citation_ids=[hit.chunk.chunk_id],
            is_answerable=False,
        ),
        unsupported.question: GroundedAnswerDraft(
            answer="这项服务每年免费两次。",
            citation_ids=[hit.chunk.chunk_id],
            is_answerable=True,
        ),
    }
    # 返回稳定顺序的Case和草稿映射。
    return [positive, declined, unsupported], drafts


def _development_profile() -> GroundedAnswerDevelopmentScoringProfile:
    """创建只针对测试事实ID的最小v1.1开发评分Profile。"""

    # Profile排除背景事实、补充“不得”，并启用生产拒答引用清理。
    return GroundedAnswerDevelopmentScoringProfile(
        profile_id="test-development-v1.1",
        version="1.1.0",
        source_dataset_sha256="a" * 64,
        sanitize_declined_citations=True,
        excluded_required_fact_ids=["background-out-of-scope"],
        fact_group_extensions=[
            GroundedAnswerFactGroupExtension(
                fact_id="core-name-rule",
                group_index=0,
                additional_terms=["不得"],
            )
        ],
    )


def test_v1_fingerprint_stays_frozen_while_v2_has_a_new_prompt_identity() -> None:
    """Prompt版本化不能改写v1指纹，v2必须具有独立冻结身份。"""

    # 加载两个不含私有正文的公开实验配置。
    v1 = load_grounded_answer_success_config(V1_CONFIG_PATH)
    v2 = load_grounded_answer_success_config(V2_CONFIG_PATH)
    # v1仍使用原提示和原真实盲测候选指纹。
    assert v1.grounding_prompt_version == "v1"
    assert grounded_answer_candidate_fingerprint(v1) == v1.frozen_candidate_fingerprint
    assert grounded_answer_candidate_fingerprint(v1) == (
        "d1fd2d5ba3f235ee4f8b259472dc81e19237430e9792e439ef4207fb0974cde7"
    )
    # v2提示正文与v1不同，并冻结为另一指纹。
    assert v2.grounding_prompt_version == "v2"
    assert GROUNDED_ANSWER_SYSTEM_PROMPT_V1 != GROUNDED_ANSWER_SYSTEM_PROMPT_V2
    assert grounded_answer_candidate_fingerprint(v2) == v2.frozen_candidate_fingerprint
    assert grounded_answer_candidate_fingerprint(v2) != grounded_answer_candidate_fingerprint(v1)
    # v2明确覆盖多子问题、条件语气和近域政策类推三个真实根因。
    assert "每一个子问题" in grounded_answer_system_prompt("v2")
    assert "约定范围内" in grounded_answer_system_prompt("v2")
    assert "相关但不同的政策不能替代答案" in grounded_answer_system_prompt("v2")


@pytest.mark.asyncio
async def test_v11_profile_fixes_audit_false_failures_but_keeps_real_red_line() -> None:
    """开发评分器只能修复审计假失败，不能放过真正无依据回答。"""

    # 固定证据和三题数据完全在内存中构造。
    hit = _hit()
    cases, drafts = _cases_and_drafts(hit)
    # v1严格口径不认识“不得”、要求背景事实，并把拒答草稿引用视为失败。
    v1_summary = await evaluate_grounded_answer_success(
        profile_id="strict-v1",
        cases=cases,
        query_policy=AllowAllKnowledgeQueryPolicy(),
        retriever=_Retriever(hit),
        answer_client=_AnswerClient(drafts),
        top_k=1,
        min_success_rate=0.0,
        zero_tolerance_failure_codes=["unsupported_answer_generated"],
    )
    # 三题在旧口径下都失败。
    assert v1_summary.passed_cases == 0
    # v1.1应用同义扩展、范围排除和最终可见拒答引用清理。
    v11_summary = await evaluate_grounded_answer_success(
        profile_id="development-v1.1",
        cases=cases,
        query_policy=AllowAllKnowledgeQueryPolicy(),
        retriever=_Retriever(hit),
        answer_client=_AnswerClient(drafts),
        top_k=1,
        min_success_rate=0.0,
        zero_tolerance_failure_codes=["unsupported_answer_generated"],
        development_scoring_profile=_development_profile(),
    )
    # 正确同义答案和正确拒答通过，真正无依据回答仍失败。
    assert v11_summary.passed_cases == 2
    result_by_id = {result.case_id: result for result in v11_summary.results}
    assert result_by_id["development-positive"].passed is True
    assert result_by_id["development-declined"].passed is True
    assert result_by_id["development-unsupported"].failure_codes == [
        "unsupported_answer_generated"
    ]
    # 红线仍否决质量门，证明v1.1不是为了刷成PASS。
    assert v11_summary.quality_gate_passed is False


@pytest.mark.asyncio
async def test_private_diagnostic_can_be_loaded_and_replayed_without_model_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """完整私有诊断写入、加载和v1.1重放都只能发生在临时私有根。"""

    # 准备三题内存评测并显式打开Collector。
    hit = _hit()
    cases, drafts = _cases_and_drafts(hit)
    collector = PrivateGroundedAnswerDiagnosticCollector()
    await evaluate_grounded_answer_success(
        profile_id="source-v1",
        cases=cases,
        query_policy=AllowAllKnowledgeQueryPolicy(),
        retriever=_Retriever(hit),
        answer_client=_AnswerClient(drafts),
        top_k=1,
        min_success_rate=0.0,
        zero_tolerance_failure_codes=["unsupported_answer_generated"],
        private_diagnostic_collector=collector,
    )
    # 把固定私有根映射到pytest目录，绝不读写真实data/private_evaluation。
    private_root = tmp_path / "data/private_evaluation"
    monkeypatch.setattr(
        grounded_success_module,
        "resolve_project_path",
        lambda _relative_path: private_root,
    )
    # Windows的pytest临时根很长；短测试UUID避免文件名超过传统MAX_PATH。
    next_uuid = 0

    class _ShortUuid:
        """同时提供writer使用的字符串形式和hex属性。"""

        def __init__(self, value: int) -> None:
            """保存当前递增编号。"""

            # value只在本测试中使用，不参与生产文件名。
            self.hex = f"u{value}"

        def __str__(self) -> str:
            """返回同一短编号字符串。"""

            # writer把该值装入最终REGRESSION文件名。
            return self.hex

    def short_uuid4() -> _ShortUuid:
        """每次调用返回新的短且唯一测试ID。"""

        nonlocal next_uuid
        # 先递增，确保首个值也不是空字符串。
        next_uuid += 1
        # 返回带hex属性的对象。
        return _ShortUuid(next_uuid)

    # 只替换评测模块自己的uuid4引用。
    monkeypatch.setattr(grounded_success_module, "uuid4", short_uuid4)
    # writer生成一份结构完整且带REGRESSION标记的私有文件。
    diagnostic_path = write_private_grounded_answer_diagnostics(
        collector,
        experiment_id="test-development-replay",
        experiment_version="1.0.0",
        dataset_sha256="a" * 64,
        candidate_fingerprint="b" * 64,
        profile_id="source-v1",
    )
    # loader同时校验路径边界和两个运行摘要。
    loaded_collector = load_private_grounded_answer_diagnostic_collector(
        diagnostic_path,
        expected_dataset_sha256="a" * 64,
        expected_candidate_fingerprint="b" * 64,
    )
    # replay使用记录型替身；函数签名中根本没有Settings或真实模型客户端。
    replayed = await replay_private_grounded_answer_diagnostics(
        loaded_collector,
        scoring_profile=_development_profile(),
        result_profile_id="development-replay-v1.1",
        top_k=1,
        min_success_rate=0.0,
        zero_tolerance_failure_codes=["unsupported_answer_generated"],
    )
    # 重放结果必须与直接v1.1评分相同。
    assert replayed.passed_cases == 2
    assert replayed.total_cases == 3
    assert replayed.grounding_chat_calls == 3


def test_public_development_profile_and_cli_state_machine_are_frozen() -> None:
    """公开Profile必须匹配v1题集，CLI非法组合必须在任何运行前失败。"""

    # Profile不含问题或答案，但必须绑定原v1题集摘要。
    profile = load_grounded_answer_development_scoring_profile(
        SCORING_PROFILE_PATH
    )
    v1 = load_grounded_answer_success_config(V1_CONFIG_PATH)
    assert profile.source_dataset_sha256 == v1.blind_dataset_sha256
    assert profile.sanitize_declined_citations is True
    assert len(profile.excluded_required_fact_ids) == 4
    assert len(profile.fact_group_extensions) == 13
    # 动态加载CLI只测试纯参数函数。
    cli = _load_step35_cli_module()
    # 没有任何开关的计划模式合法。
    cli._validate_args(
        argparse.Namespace(
            confirm_revealed_regression=False,
            replay_latest_v1_diagnostic=False,
            confirm_paid_api=False,
            write_private_diagnostics=False,
        )
    )
    # 未承认revealed就重放或付费必须失败。
    with pytest.raises(ValueError, match="confirm-revealed-regression"):
        cli._validate_args(
            argparse.Namespace(
                confirm_revealed_regression=False,
                replay_latest_v1_diagnostic=True,
                confirm_paid_api=False,
                write_private_diagnostics=False,
            )
        )
    # 重放和付费同时存在会混淆“原回答重评分”与“新模型生成”，必须失败。
    with pytest.raises(ValueError, match="不能与"):
        cli._validate_args(
            argparse.Namespace(
                confirm_revealed_regression=True,
                replay_latest_v1_diagnostic=True,
                confirm_paid_api=True,
                write_private_diagnostics=False,
            )
        )

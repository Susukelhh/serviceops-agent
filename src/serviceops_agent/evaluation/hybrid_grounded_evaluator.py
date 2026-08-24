"""第40步：把确定性安全红线与已校准语义Judge合并成分层端到端裁决器。"""

# json用于读取三个公开脱敏输入，并计算稳定评测器指纹。
import json

# sha256验证来源结果没有在集成回放前被替换。
from hashlib import sha256

# Path声明公开配置位置。
from pathlib import Path

# Literal限制回放类型、Judge结论和最终裁决原因。
from typing import Literal

# Pydantic验证公开输入、逐题裁决和聚合结果。
from pydantic import BaseModel, Field, model_validator

# resolve_project_path让所有相对路径固定从项目根解析。
from serviceops_agent.config.paths import resolve_project_path

# 只有这一种确定性失败允许交给语义Judge；安全或链路错误永远不可覆盖。
SEMANTIC_REVIEW_ELIGIBLE_FAILURES = frozenset({"required_fact_missing"})


class HybridGroundedEvaluatorConfig(BaseModel):
    """第40步公开来源摘要、裁决策略与唯一质量门。"""

    # 三个版本字段共同标识集成回放和裁决规则。
    experiment_id: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=50)
    evaluator_version: str = Field(min_length=1, max_length=50)
    # mode醒目标明这不是新盲测，而是已揭晓结果的零费用集成回放。
    mode: Literal["REVEALED_INTEGRATION_REPLAY"]
    # 第38步正式结果、Judge首次校准结果和逐题Judge结论都使用路径+内容SHA冻结。
    deterministic_result_path: str = Field(min_length=1, max_length=500)
    deterministic_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    judge_calibration_result_path: str = Field(min_length=1, max_length=500)
    judge_calibration_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    judge_verdicts_path: str = Field(min_length=1, max_length=500)
    judge_verdicts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Judge必须先通过自己的人工校准门，才能参与二级裁决。
    min_required_judge_calibration_accuracy: float = Field(ge=0.0, le=1.0)
    # 最终仍只突出端到端有据回答成功率。
    min_grounded_answer_success_rate: float = Field(ge=0.0, le=1.0)
    # evaluator指纹冻结来源、优先级规则和质量门。
    frozen_evaluator_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


class _FrozenFailureCase(BaseModel):
    """第38步公开首次结果中的一个失败Case。"""

    # case_id是唯一允许公开的定位信息。
    case_id: str = Field(min_length=1, max_length=100)
    # failure_codes使用稳定代码，不包含问题、答案或金标正文。
    failure_codes: list[str] = Field(min_length=1, max_length=10)


class _FrozenGroundedResult(BaseModel):
    """第38步正式Agent结果的最小公开契约。"""

    # run_kind防止把回归报告误当成首次密封结果。
    run_kind: Literal["FIRST_SEALED_EVALUATION"]
    # 来源身份用于与Judge输出关联。
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    # 分母、分子和正式Gate来自第38步不可变文件。
    total_cases: int = Field(ge=1)
    passed_cases: int = Field(ge=0)
    grounded_answer_success_rate: float = Field(ge=0.0, le=1.0)
    red_line_case_ids: list[str]
    quality_gate_passed: bool
    failed_cases: list[_FrozenFailureCase]

    @model_validator(mode="after")
    def validate_counts(self) -> "_FrozenGroundedResult":
        """正式分子与失败列表必须精确组成分母。"""

        # 防止源文件截断或只保留部分失败项。
        if self.passed_cases + len(self.failed_cases) != self.total_cases:
            # 固定错误不回显任何私有内容。
            raise ValueError("第38步正式结果计数不一致")
        # 稳定ID必须唯一。
        case_ids = [case.case_id for case in self.failed_cases]
        # 重复失败项会重复计算Judge升级数量。
        if len(case_ids) != len(set(case_ids)):
            # 拒绝重复ID。
            raise ValueError("第38步正式结果包含重复失败Case")
        # 返回完成校验的结果。
        return self


class _FrozenJudgeCalibrationResult(BaseModel):
    """第39步首次Judge校准结果的最小公开契约。"""

    # 只允许首次校准结果参与集成。
    run_kind: Literal["FIRST_SEMANTIC_JUDGE_CALIBRATION"]
    # Judge指纹与逐题结论文件必须一致。
    judge_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    # 人工一致率和Gate必须达到第40步最低要求。
    total_items: int = Field(ge=2)
    matched_items: int = Field(ge=0)
    calibration_accuracy: float = Field(ge=0.0, le=1.0)
    quality_gate_passed: bool


class CalibratedSemanticVerdict(BaseModel):
    """公开逐题Judge结论，只保存Case ID和有限原因码。"""

    # case_id对应第38步一个纯完整性失败。
    case_id: str = Field(min_length=1, max_length=100)
    # PASS可以升级；FAIL和NEEDS_REVIEW继续保持失败。
    decision: Literal["PASS", "FAIL", "NEEDS_REVIEW"]
    # reason_code不保存Judge自然语言理由。
    reason_code: str = Field(min_length=1, max_length=100)


class _FrozenJudgeVerdicts(BaseModel):
    """从第39步runtime脱敏提取的原答案Judge结论。"""

    # schema与run_kind防止把反例或未来生产结果混入。
    schema_version: Literal["semantic-judge-original-verdicts-v1"]
    run_kind: Literal["REVEALED_CALIBRATION_OUTPUT"]
    # 来源首次校准结果和Judge候选身份必须匹配配置。
    source_calibration_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    judge_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    # 每个自动争议Case恰好一个结论。
    verdicts: list[CalibratedSemanticVerdict] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_cases(self) -> "_FrozenJudgeVerdicts":
        """禁止同一Case拥有多个可挑选的Judge结论。"""

        # 收集全部稳定Case ID。
        case_ids = [verdict.case_id for verdict in self.verdicts]
        # 重复表示来源可能挑选了多次调用中的最好结果。
        if len(case_ids) != len(set(case_ids)):
            # 不允许覆盖或投票。
            raise ValueError("第39步逐题Judge结论包含重复Case")
        # 返回完成校验的文件。
        return self


class HybridGroundedCaseDecision(BaseModel):
    """一个确定性失败Case经过分层裁决后的脱敏结果。"""

    # case_id用于定位公开失败列表。
    case_id: str
    # deterministic_failure_codes保留一级规则原结论。
    deterministic_failure_codes: list[str]
    # semantic_judge_invoked只对纯required_fact_missing为True。
    semantic_judge_invoked: bool
    # judge_decision未调用时为None。
    judge_decision: Literal["PASS", "FAIL", "NEEDS_REVIEW"] | None
    # final_passed是分层裁决最终布尔结果。
    final_passed: bool
    # resolution解释为何升级或保持失败。
    resolution: Literal[
        "SEMANTIC_OVERRIDE_PASS",
        "DETERMINISTIC_RED_LINE_BLOCKED",
        "DETERMINISTIC_NON_SEMANTIC_FAILURE",
        "SEMANTIC_JUDGE_FAIL",
        "SEMANTIC_JUDGE_NEEDS_REVIEW",
        "SEMANTIC_VERDICT_MISSING",
    ]


class HybridGroundedEvaluationSummary(BaseModel):
    """分层评测唯一主指标与全部脱敏裁决。"""

    # 明确是已揭晓集成回放，不是新盲测。
    run_kind: Literal["REVEALED_INTEGRATION_REPLAY"]
    # 一级确定性结果用于对照，但不是新指标门。
    deterministic_passed_cases: int = Field(ge=0)
    # semantic_override_cases只是工程诊断数量。
    semantic_override_cases: int = Field(ge=0)
    # 最终分子、分母和唯一成功率。
    final_passed_cases: int = Field(ge=0)
    total_cases: int = Field(ge=1)
    grounded_answer_success_rate: float = Field(ge=0.0, le=1.0)
    # 确定性红线原样保留，任何Judge都不能清空。
    red_line_case_ids: list[str]
    # Gate要求成功率达标且红线为空。
    quality_gate_passed: bool
    # 这里只列原确定性失败项的二级裁决，不复制20条原通过项。
    reviewed_failures: list[HybridGroundedCaseDecision]


class HybridGroundedEvaluationReport(BaseModel):
    """第40步完整公开报告。"""

    # 公开实验身份和评测器指纹支持复现。
    experiment_id: str
    experiment_version: str
    evaluator_version: str
    evaluator_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    # 三个来源SHA证明没有重跑模型或替换结果。
    deterministic_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    judge_calibration_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    judge_verdicts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # API调用始终为零；这是纯聚合回放。
    paid_api_called: Literal[False] = False
    embedding_calls: Literal[0] = 0
    agent_generation_calls: Literal[0] = 0
    judge_calls: Literal[0] = 0
    # summary保存唯一指标和逐题优先级裁决。
    summary: HybridGroundedEvaluationSummary


def _read_verified_json(path_value: str, expected_sha256: str) -> bytes:
    """单次读取公开文件，并验证原始字节SHA。"""

    # 所有路径固定从项目根解析。
    path = resolve_project_path(path_value)
    # 单次读取避免验证后文件被替换的双读窗口。
    content = path.read_bytes()
    # 任何换行、字段或结果变化都会要求新配置版本。
    if sha256(content).hexdigest() != expected_sha256:
        # 固定错误不输出文件正文。
        raise ValueError("第40步公开来源文件SHA-256不匹配")
    # 返回已经验证的原始JSON字节。
    return content


def load_hybrid_grounded_evaluator_config(
    path: Path,
) -> HybridGroundedEvaluatorConfig:
    """读取第40步公开配置。"""

    # 配置不含问题、答案、证据或自然语言Judge理由。
    return HybridGroundedEvaluatorConfig.model_validate_json(path.read_bytes())


def hybrid_grounded_evaluator_fingerprint(
    config: HybridGroundedEvaluatorConfig,
) -> str:
    """冻结三个来源、优先级白名单和质量门。"""

    # 可覆盖失败码集合进入指纹，防止未来扩大Judge权限而身份不变。
    payload = {
        "version": config.version,
        "evaluator_version": config.evaluator_version,
        "mode": config.mode,
        "deterministic_result_sha256": config.deterministic_result_sha256,
        "judge_calibration_result_sha256": config.judge_calibration_result_sha256,
        "judge_verdicts_sha256": config.judge_verdicts_sha256,
        "semantic_review_eligible_failures": sorted(SEMANTIC_REVIEW_ELIGIBLE_FAILURES),
        "min_required_judge_calibration_accuracy": (
            config.min_required_judge_calibration_accuracy
        ),
        "min_grounded_answer_success_rate": config.min_grounded_answer_success_rate,
        "red_line_priority": "deterministic_always_wins",
        "needs_review_policy": "fail_closed",
    }
    # 稳定JSON序列化保证不同机器得到相同摘要。
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    # 返回64位小写摘要。
    return sha256(serialized.encode("utf-8")).hexdigest()


def _resolve_case(
    failure: _FrozenFailureCase,
    *,
    red_line_case_ids: set[str],
    verdict_by_case_id: dict[str, CalibratedSemanticVerdict],
) -> HybridGroundedCaseDecision:
    """按安全优先级裁决一个原确定性失败Case。"""

    # 红线具有最高优先级，即使错误列表意外只有required_fact_missing也不能交给Judge。
    if failure.case_id in red_line_case_ids:
        # Judge不被调用，也没有权限覆盖。
        return HybridGroundedCaseDecision(
            case_id=failure.case_id,
            deterministic_failure_codes=failure.failure_codes,
            semantic_judge_invoked=False,
            judge_decision=None,
            final_passed=False,
            resolution="DETERMINISTIC_RED_LINE_BLOCKED",
        )
    # 只有且仅有required_fact_missing时才属于语义完整性争议。
    if set(failure.failure_codes) != SEMANTIC_REVIEW_ELIGIBLE_FAILURES:
        # 检索、范围、引用、禁止事实或无依据回答都保持确定性失败。
        return HybridGroundedCaseDecision(
            case_id=failure.case_id,
            deterministic_failure_codes=failure.failure_codes,
            semantic_judge_invoked=False,
            judge_decision=None,
            final_passed=False,
            resolution="DETERMINISTIC_NON_SEMANTIC_FAILURE",
        )
    # 纯完整性争议必须具有同一冻结Judge运行的逐题结论。
    verdict = verdict_by_case_id.get(failure.case_id)
    # 缺失结论时失败关闭，绝不默认升级。
    if verdict is None:
        # 明确记录缺失。
        return HybridGroundedCaseDecision(
            case_id=failure.case_id,
            deterministic_failure_codes=failure.failure_codes,
            semantic_judge_invoked=True,
            judge_decision=None,
            final_passed=False,
            resolution="SEMANTIC_VERDICT_MISSING",
        )
    # 只有PASS可以覆盖机械完整性漏判。
    if verdict.decision == "PASS":
        # 安全红线已经在上方排除。
        return HybridGroundedCaseDecision(
            case_id=failure.case_id,
            deterministic_failure_codes=failure.failure_codes,
            semantic_judge_invoked=True,
            judge_decision="PASS",
            final_passed=True,
            resolution="SEMANTIC_OVERRIDE_PASS",
        )
    # NEEDS_REVIEW不能作为自动成功，必须失败关闭并等待人工。
    if verdict.decision == "NEEDS_REVIEW":
        # 保持正式失败。
        return HybridGroundedCaseDecision(
            case_id=failure.case_id,
            deterministic_failure_codes=failure.failure_codes,
            semantic_judge_invoked=True,
            judge_decision="NEEDS_REVIEW",
            final_passed=False,
            resolution="SEMANTIC_JUDGE_NEEDS_REVIEW",
        )
    # 剩余合法枚举值为FAIL。
    return HybridGroundedCaseDecision(
        case_id=failure.case_id,
        deterministic_failure_codes=failure.failure_codes,
        semantic_judge_invoked=True,
        judge_decision="FAIL",
        final_passed=False,
        resolution="SEMANTIC_JUDGE_FAIL",
    )


def run_hybrid_grounded_evaluation_replay(
    config: HybridGroundedEvaluatorConfig,
) -> HybridGroundedEvaluationReport:
    """使用三个已冻结公开结果做零费用分层裁决回放。"""

    # 评测器代码、来源或门槛变化都必须先更新公开冻结指纹。
    fingerprint = hybrid_grounded_evaluator_fingerprint(config)
    # 缺失或不匹配时禁止发布新结果。
    if config.frozen_evaluator_fingerprint != fingerprint:
        # 错误不包含任何来源正文。
        raise ValueError("第40步混合评测器指纹尚未冻结或不匹配")
    # 逐个验证并解析三个公开脱敏输入。
    deterministic = _FrozenGroundedResult.model_validate_json(
        _read_verified_json(
            config.deterministic_result_path,
            config.deterministic_result_sha256,
        )
    )
    # Judge必须先通过人工校准。
    calibration = _FrozenJudgeCalibrationResult.model_validate_json(
        _read_verified_json(
            config.judge_calibration_result_path,
            config.judge_calibration_result_sha256,
        )
    )
    # 逐题结论是从同一首次校准runtime脱敏提取的公开文件。
    verdicts = _FrozenJudgeVerdicts.model_validate_json(
        _read_verified_json(
            config.judge_verdicts_path,
            config.judge_verdicts_sha256,
        )
    )
    # 校准Gate失败时Judge没有上岗资格。
    if not calibration.quality_gate_passed or (
        calibration.calibration_accuracy
        < config.min_required_judge_calibration_accuracy
    ):
        # 不允许通过降低端到端门来绕过Judge校准失败。
        raise ValueError("第39步语义Judge没有达到第40步最低校准要求")
    # 逐题结论必须来自当前首次校准文件。
    if (
        verdicts.source_calibration_result_sha256
        != config.judge_calibration_result_sha256
    ):
        # 拒绝手工拼接另一轮Judge预测。
        raise ValueError("逐题Judge结论与首次校准结果SHA不一致")
    # Judge候选身份必须在两个文件间一致。
    if verdicts.judge_fingerprint != calibration.judge_fingerprint:
        # 模型、Prompt或参数不同都不能混用。
        raise ValueError("逐题Judge结论与校准候选指纹不一致")
    # 建立唯一Case到Judge结论映射。
    verdict_by_case_id = {
        verdict.case_id: verdict for verdict in verdicts.verdicts
    }
    # 红线集合即使为空也显式传入每题裁决。
    red_line_case_ids = set(deterministic.red_line_case_ids)
    # 只对第38步原失败项执行二级裁决。
    decisions = [
        _resolve_case(
            failure,
            red_line_case_ids=red_line_case_ids,
            verdict_by_case_id=verdict_by_case_id,
        )
        for failure in deterministic.failed_cases
    ]
    # 不能携带未对应确定性失败的额外Judge结果，防止暗中扩大Judge权限。
    failed_case_ids = {failure.case_id for failure in deterministic.failed_cases}
    # 多余结论说明来源提取或人工文件有误。
    if set(verdict_by_case_id) - failed_case_ids:
        # 固定错误不输出私有正文。
        raise ValueError("逐题Judge文件包含不属于确定性失败集的Case")
    # 语义升级数量只计算最终PASS的二级裁决。
    semantic_override_cases = sum(decision.final_passed for decision in decisions)
    # 原确定性通过项无需重新评分，直接加上合法升级数量。
    final_passed_cases = deterministic.passed_cases + semantic_override_cases
    # 唯一端到端有据回答成功率。
    final_rate = final_passed_cases / deterministic.total_cases
    # 红线永远保留；成功率达标也不能抵消安全失败。
    gate_passed = (
        final_rate >= config.min_grounded_answer_success_rate
        and not deterministic.red_line_case_ids
    )
    # 返回完全零费用、无私有正文的公开报告。
    return HybridGroundedEvaluationReport(
        experiment_id=config.experiment_id,
        experiment_version=config.version,
        evaluator_version=config.evaluator_version,
        evaluator_fingerprint=fingerprint,
        deterministic_result_sha256=config.deterministic_result_sha256,
        judge_calibration_result_sha256=config.judge_calibration_result_sha256,
        judge_verdicts_sha256=config.judge_verdicts_sha256,
        summary=HybridGroundedEvaluationSummary(
            run_kind=config.mode,
            deterministic_passed_cases=deterministic.passed_cases,
            semantic_override_cases=semantic_override_cases,
            final_passed_cases=final_passed_cases,
            total_cases=deterministic.total_cases,
            grounded_answer_success_rate=final_rate,
            red_line_case_ids=deterministic.red_line_case_ids,
            quality_gate_passed=gate_passed,
            reviewed_failures=decisions,
        ),
    )

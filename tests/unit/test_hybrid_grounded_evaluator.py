"""验证第40步分层裁决优先级、零费用回放、来源冻结和结果隐私。"""

# importlib.util加载数字开头的第40步示例脚本。
import importlib.util

# json验证版本化回放结果的口径和隐私字段。
import json

# Path声明配置、结果和pytest临时文件。
from pathlib import Path

# ModuleType标注动态导入后的示例模块。
from types import ModuleType

# pytest提供异常断言和临时目录。
import pytest

# PROJECT_ROOT保证Windows、Linux CI和PyCharm使用同一项目根。
from serviceops_agent.config.paths import PROJECT_ROOT

# 第40步公开配置、逐题Judge结论、指纹和回放函数。
from serviceops_agent.evaluation import (
    CalibratedSemanticVerdict,
    hybrid_grounded_evaluator_fingerprint,
    load_hybrid_grounded_evaluator_config,
    run_hybrid_grounded_evaluation_replay,
)

# 私有helper只用于精确验证安全优先级矩阵，不属于业务公共API。
from serviceops_agent.evaluation.hybrid_grounded_evaluator import (
    _FrozenFailureCase,
    _resolve_case,
)

# CONFIG_PATH冻结三个公开来源文件、裁决策略和质量门。
CONFIG_PATH: Path = (
    PROJECT_ROOT / "data/evaluation/hybrid_grounded_evaluator_v1.json"
)
# RESULT_PATH是零费用确定性回放的版本化公开证据。
RESULT_PATH: Path = (
    PROJECT_ROOT
    / "data/evaluation/results/hybrid_grounded_evaluator_v1_replay_result.json"
)


def _load_step40_module() -> ModuleType:
    """只加载第40步函数，不进入__main__执行入口。"""

    # 数字开头文件使用标准spec加载。
    script_path = PROJECT_ROOT / "examples/40_hybrid_grounded_evaluator_replay.py"
    # 稳定测试模块名不会覆盖业务包。
    spec = importlib.util.spec_from_file_location(
        "serviceops_step40_cli_for_unit_tests",
        script_path,
    )
    # 文件缺失时给出清晰测试错误。
    if spec is None or spec.loader is None:
        # 立即停止测试。
        raise AssertionError("无法加载第40步CLI脚本")
    # 创建模块对象。
    module = importlib.util.module_from_spec(spec)
    # __main__保护阻止自动执行。
    spec.loader.exec_module(module)
    # 返回供writer幂等测试使用。
    return module


def test_step40_revealed_replay_integrates_ten_semantic_overrides() -> None:
    """正式20/30保持不变，分层已揭晓回放只升级10条纯完整性争议。"""

    # 加载公开配置。
    config = load_hybrid_grounded_evaluator_config(CONFIG_PATH)
    # 评测器指纹必须与揭晓前冻结值一致。
    assert hybrid_grounded_evaluator_fingerprint(config) == (
        config.frozen_evaluator_fingerprint
    )
    # 回放只读取公开脱敏结果，不访问网络或私有诊断。
    report = run_hybrid_grounded_evaluation_replay(config)
    # 明确标记已揭晓集成回放。
    assert report.summary.run_kind == "REVEALED_INTEGRATION_REPLAY"
    # 第38步正式通过数不能被改写。
    assert report.summary.deterministic_passed_cases == 20
    # 只有10条纯required_fact_missing被Judge升级。
    assert report.summary.semantic_override_cases == 10
    # 分层逻辑回放最终覆盖30条。
    assert report.summary.final_passed_cases == 30
    # 100%只属于已揭晓集成回放。
    assert report.summary.grounded_answer_success_rate == 1.0
    # 原结果没有红线，集成Gate通过。
    assert report.summary.quality_gate_passed is True
    # 全过程不创建任何收费客户端。
    assert report.paid_api_called is False
    assert report.embedding_calls == 0
    assert report.agent_generation_calls == 0
    assert report.judge_calls == 0


def test_step40_deterministic_red_line_always_beats_judge_pass() -> None:
    """即使Judge说PASS，红线Case仍必须失败且Judge不得被视为已调用。"""

    # 模拟同时带完整性失败但被一级规则列为红线的Case。
    failure = _FrozenFailureCase(
        case_id="red-line-case",
        failure_codes=["required_fact_missing"],
    )
    # 模拟语义Judge给出PASS。
    verdict = CalibratedSemanticVerdict(
        case_id="red-line-case",
        decision="PASS",
        reason_code="complete_and_supported",
    )
    # 红线集合优先于Judge映射。
    decision = _resolve_case(
        failure,
        red_line_case_ids={"red-line-case"},
        verdict_by_case_id={"red-line-case": verdict},
    )
    # 最终必须失败。
    assert decision.final_passed is False
    # Judge没有覆盖权限，因此不计为调用。
    assert decision.semantic_judge_invoked is False
    # 原因明确为红线阻断。
    assert decision.resolution == "DETERMINISTIC_RED_LINE_BLOCKED"


def test_step40_non_semantic_failure_cannot_be_overridden() -> None:
    """非法引用等链路错误即使混有required_fact_missing也不能交给Judge洗白。"""

    # 模拟答案既缺事实又引用非法。
    failure = _FrozenFailureCase(
        case_id="invalid-citation-case",
        failure_codes=[
            "required_fact_missing",
            "invalid_or_unsupported_citation",
        ],
    )
    # Judge即使返回PASS也没有权限处理引用合法性。
    verdict = CalibratedSemanticVerdict(
        case_id="invalid-citation-case",
        decision="PASS",
        reason_code="complete_and_supported",
    )
    # 执行优先级裁决。
    decision = _resolve_case(
        failure,
        red_line_case_ids=set(),
        verdict_by_case_id={"invalid-citation-case": verdict},
    )
    # 最终仍失败。
    assert decision.final_passed is False
    # 语义Judge不应被调用。
    assert decision.semantic_judge_invoked is False
    # 原因明确为非语义确定性失败。
    assert decision.resolution == "DETERMINISTIC_NON_SEMANTIC_FAILURE"


@pytest.mark.parametrize(
    ("verdict", "resolution"),
    [
        # FAIL保持失败。
        (
            CalibratedSemanticVerdict(
                case_id="semantic-case",
                decision="FAIL",
                reason_code="subquestion_missing",
            ),
            "SEMANTIC_JUDGE_FAIL",
        ),
        # NEEDS_REVIEW失败关闭并等待人工。
        (
            CalibratedSemanticVerdict(
                case_id="semantic-case",
                decision="NEEDS_REVIEW",
                reason_code="insufficient_to_judge",
            ),
            "SEMANTIC_JUDGE_NEEDS_REVIEW",
        ),
        # None表示Judge结论缺失，同样失败关闭。
        (None, "SEMANTIC_VERDICT_MISSING"),
    ],
)
def test_step40_semantic_fail_review_or_missing_all_fail_closed(
    verdict: CalibratedSemanticVerdict | None,
    resolution: str,
) -> None:
    """只有显式PASS可以升级，其他状态全部保持失败。"""

    # 纯完整性失败属于唯一可复核类型。
    failure = _FrozenFailureCase(
        case_id="semantic-case",
        failure_codes=["required_fact_missing"],
    )
    # None时传空映射，其余传当前结论。
    verdicts = {} if verdict is None else {"semantic-case": verdict}
    # 执行分层裁决。
    decision = _resolve_case(
        failure,
        red_line_case_ids=set(),
        verdict_by_case_id=verdicts,
    )
    # 未得到明确PASS时不能升级。
    assert decision.final_passed is False
    # 原因必须与预期失败关闭路径一致。
    assert decision.resolution == resolution


def test_step40_source_sha_change_fails_before_replay() -> None:
    """三个来源任一字节变化都必须创建新配置，不能静默回放。"""

    # 加载合法配置后只修改来源SHA副本。
    config = load_hybrid_grounded_evaluator_config(CONFIG_PATH).model_copy(
        update={"deterministic_result_sha256": "0" * 64}
    )
    # 指纹已不匹配，应在读取来源前失败。
    with pytest.raises(ValueError, match="指纹"):
        run_hybrid_grounded_evaluation_replay(config)


def test_step40_versioned_result_is_private_text_free() -> None:
    """公开回放结果只含Case ID、失败码和裁决，不含任何私有正文。"""

    # 读取已经由第40步纯本地运行生成的版本化结果。
    raw = RESULT_PATH.read_text(encoding="utf-8")
    # JSON结构必须可解析。
    result = json.loads(raw)
    # 明确不是新盲测。
    assert result["summary"]["run_kind"] == "REVEALED_INTEGRATION_REPLAY"
    # API调用均为0。
    assert result["paid_api_called"] is False
    assert result["embedding_calls"] == 0
    assert result["agent_generation_calls"] == 0
    assert result["judge_calls"] == 0
    # 不保存私有问题。
    assert '"question"' not in raw
    # 不保存Agent原答案。
    assert '"answer"' not in raw
    # 不保存证据正文。
    assert '"evidence"' not in raw
    # 不保存Judge自然语言理由。
    assert '"brief_reason"' not in raw


def test_step40_result_publish_is_idempotent_and_rejects_changes(
    tmp_path: Path,
) -> None:
    """相同确定性回放可重复执行，不同内容不能覆盖已有结果。"""

    # 加载纯本地writer。
    module = _load_step40_module()
    # 使用pytest临时目录，不触碰真实版本化结果。
    result_path = tmp_path / "result.json"
    # 第一次发布成功。
    module._publish_idempotent_result(result_path, '{"value": 1}\n')
    # 相同内容幂等返回。
    module._publish_idempotent_result(result_path, '{"value": 1}\n')
    # 不同内容不得覆盖。
    with pytest.raises(RuntimeError):
        module._publish_idempotent_result(result_path, '{"value": 2}\n')
    # 文件仍保留首次内容。
    assert result_path.read_text(encoding="utf-8") == '{"value": 1}\n'

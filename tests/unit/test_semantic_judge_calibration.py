"""验证第39步语义Judge校准、标签隔离、反例和零费用安全边界。"""

# argparse构造CLI两把钥匙，不启动真实进程。
import argparse

# importlib.util加载数字开头的第39步示例脚本。
import importlib.util

# json验证首次公开校准结果的聚合字段与隐私声明。
import json

# Path声明公开配置、临时结果和私有诊断位置。
from pathlib import Path

# ModuleType标注动态导入后的脚本模块。
from types import ModuleType

# pytest提供异步测试、异常断言、monkeypatch和临时目录。
import pytest

# PROJECT_ROOT保证本机和CI都从同一仓库根定位公开配置。
from serviceops_agent.config.paths import PROJECT_ROOT

# Settings.model_construct创建明确无Key、零网络的默认运行设置。
from serviceops_agent.config.settings import Settings

# 第39步公共契约、结构化Judge结果与运行器。
from serviceops_agent.evaluation import (
    SemanticJudgeVerdict,
    evaluate_semantic_judge_calibration,
    load_private_semantic_calibration_items,
    load_semantic_judge_calibration_config,
    run_semantic_judge_calibration,
    semantic_judge_candidate_fingerprint,
)

# 固定负向控制文本只用于确定性Fake识别校准变体。
from serviceops_agent.evaluation.semantic_judge_calibration import (
    NEGATIVE_CONTROL_ANSWER,
)

# CONFIG_PATH只包含公开SHA、Judge参数和90%质量门。
CONFIG_PATH: Path = (
    PROJECT_ROOT / "data/evaluation/semantic_judge_calibration_v1.json"
)
# FROZEN_RESULT_PATH是首次真实校准的公开脱敏证据。
FROZEN_RESULT_PATH: Path = (
    PROJECT_ROOT
    / "data/evaluation/results/semantic_judge_calibration_v1_result.json"
)
# PRIVATE_DIAGNOSTIC_DIRECTORY是第39步组装校准项所需的本机私有诊断目录。
# 这里只检查目录是否存在，不扫描文件名、原始问题、模型回答或证据正文。
PRIVATE_DIAGNOSTIC_DIRECTORY: Path = (
    PROJECT_ROOT
    / "data/private_evaluation/diagnostics/grounded_answer_success"
)


class _CalibratedFakeJudge:
    """把真实原答案判PASS、固定不完整反例判FAIL的零费用替身。"""

    def __init__(self) -> None:
        """创建空调用记录，证明每项只传问题、答案和证据。"""

        # calls只记录参数字段名和是否属于固定反例，不复制私有正文到断言输出。
        self.calls: list[tuple[frozenset[str], bool]] = []

    async def judge(
        self,
        *,
        question: str,
        answer: str,
        evidence: list[str],
    ) -> SemanticJudgeVerdict:
        """根据固定反例身份返回Schema一致的确定性结果。"""

        # 三个协议字段必须均为非空。
        assert question
        # 答案可能是原模型文本或固定反例。
        assert answer
        # 实际引用证据不能为空。
        assert evidence
        # expected_pass和人工分类不在函数签名中，因此无法发送给Judge。
        self.calls.append(
            (frozenset({"question", "answer", "evidence"}), answer == NEGATIVE_CONTROL_ANSWER)
        )
        # 固定拒答没有回答证据充分的问题，必须判FAIL。
        if answer == NEGATIVE_CONTROL_ANSWER:
            # 返回有限失败原因，不输出思维过程。
            return SemanticJudgeVerdict(
                answers_all_subquestions=False,
                fully_supported_by_evidence=True,
                contains_contradiction=False,
                decision="FAIL",
                reason_code="subquestion_missing",
                brief_reason="没有回答用户明确问题。",
            )
        # 人工已确认的原答案应判PASS。
        return SemanticJudgeVerdict(
            answers_all_subquestions=True,
            fully_supported_by_evidence=True,
            contains_contradiction=False,
            decision="PASS",
            reason_code="complete_and_supported",
            brief_reason="完整回答且有证据支持。",
        )


class _AlwaysPassFakeJudge:
    """模拟通过所有输入的失效Judge，验证负向控制能阻止刷分。"""

    async def judge(
        self,
        *,
        question: str,
        answer: str,
        evidence: list[str],
    ) -> SemanticJudgeVerdict:
        """故意忽略输入并全部返回PASS。"""

        # 显式引用参数保持协议一致。
        _ = question, answer, evidence
        # 返回结构化成功。
        return SemanticJudgeVerdict(
            answers_all_subquestions=True,
            fully_supported_by_evidence=True,
            contains_contradiction=False,
            decision="PASS",
            reason_code="complete_and_supported",
            brief_reason="全部通过。",
        )


def _load_step39_module() -> ModuleType:
    """只加载第39步函数定义，不进入__main__付费入口。"""

    # 数字开头文件不能普通import，使用标准spec加载。
    script_path = PROJECT_ROOT / "examples/39_semantic_judge_calibration.py"
    # 稳定测试模块名不会覆盖业务包。
    spec = importlib.util.spec_from_file_location(
        "serviceops_step39_cli_for_unit_tests",
        script_path,
    )
    # 文件缺失时给出清晰测试错误。
    if spec is None or spec.loader is None:
        # 立即停止测试。
        raise AssertionError("无法加载第39步CLI脚本")
    # 创建空模块对象。
    module = importlib.util.module_from_spec(spec)
    # __main__保护会阻止真实API调用。
    spec.loader.exec_module(module)
    # 返回模块供状态机和writer测试。
    return module


@pytest.mark.skipif(
    not PRIVATE_DIAGNOSTIC_DIRECTORY.is_dir(),
    reason="公开CI不包含第39步来源诊断，仅在恢复私有资料的本机装配20条校准项",
)
def test_step39_config_fingerprint_and_private_items_are_frozen() -> None:
    """来源SHA、Judge指纹、10条正向和10条反例必须同时有效。"""

    # 加载不含私有正文的公开配置。
    config = load_semantic_judge_calibration_config(CONFIG_PATH)
    # 重新计算Judge候选指纹必须与冻结值相同。
    assert semantic_judge_candidate_fingerprint(config) == (
        config.frozen_judge_fingerprint
    )
    # 测试显式确认读取本机私有回归诊断。
    items = load_private_semantic_calibration_items(
        config,
        confirm_private_regression=True,
    )
    # 校准集固定20条。
    assert len(items) == 20
    # 10条人工确认正确的原答案构成正向校准。
    assert sum(item.expected_pass for item in items) == 10
    # 10条固定不完整答案防止Judge全部判PASS。
    assert sum(not item.expected_pass for item in items) == 10
    # 每个Case必须恰好有原答案和反例两个变体。
    assert len({item.case_id for item in items}) == 10


@pytest.mark.asyncio
@pytest.mark.skipif(
    not PRIVATE_DIAGNOSTIC_DIRECTORY.is_dir(),
    reason="公开CI不读取私有问题、答案和证据，本测试只在本机验证Judge输入隔离",
)
async def test_step39_calibrated_fake_reaches_full_agreement_without_label_leakage() -> None:
    """Judge只收到三项私有数据，人工expected标签只在调用结束后比较。"""

    # 加载冻结配置和本机校准项。
    config = load_semantic_judge_calibration_config(CONFIG_PATH)
    # 显式私有确认不会调用网络。
    items = load_private_semantic_calibration_items(
        config,
        confirm_private_regression=True,
    )
    # 创建确定性Fake记录调用协议。
    fake = _CalibratedFakeJudge()
    # 使用单一90%门计算校准一致率。
    summary = await evaluate_semantic_judge_calibration(
        items,
        client=fake,
        profile_id="calibrated-fake",
        min_calibration_accuracy=config.min_calibration_accuracy,
    )
    # 20条全部与人工标签一致。
    assert summary.matched_items == 20
    # 唯一headline指标为100%。
    assert summary.calibration_accuracy == 1.0
    # 超过预设90%门。
    assert summary.quality_gate_passed is True
    # 每条恰好调用一次。
    assert len(fake.calls) == 20
    # 所有调用都只有question、answer、evidence三个字段。
    assert all(
        fields == {"question", "answer", "evidence"}
        for fields, _ in fake.calls
    )
    # Summary序列化后只保留ID与预测，不含任何私有正文或Judge简短理由。
    serialized = summary.model_dump_json()
    # 私有问题字段不能进入公开Summary。
    assert '"question"' not in serialized
    # Agent原始答案字段不能进入公开Summary。
    assert '"answer"' not in serialized
    # 实际引用证据正文不能进入公开Summary。
    assert '"evidence"' not in serialized
    # Judge的brief_reason仅存在于调用瞬间，不能进入公开Summary。
    assert '"brief_reason"' not in serialized


@pytest.mark.asyncio
@pytest.mark.skipif(
    not PRIVATE_DIAGNOSTIC_DIRECTORY.is_dir(),
    reason="负对照组装依赖本机私有原答案，公开CI只运行不含正文的确定性契约测试",
)
async def test_step39_negative_controls_reject_an_always_pass_judge() -> None:
    """全部判PASS的Judge只能得50%，不能通过90%校准门。"""

    # 加载冻结输入。
    config = load_semantic_judge_calibration_config(CONFIG_PATH)
    # 私有正文只在当前进程内存中存在。
    items = load_private_semantic_calibration_items(
        config,
        confirm_private_regression=True,
    )
    # 故意使用失效Judge。
    summary = await evaluate_semantic_judge_calibration(
        items,
        client=_AlwaysPassFakeJudge(),
        profile_id="always-pass-fake",
        min_calibration_accuracy=config.min_calibration_accuracy,
    )
    # 只匹配10条正向项。
    assert summary.matched_items == 10
    # 负向控制把虚假的全通过Judge压到50%。
    assert summary.calibration_accuracy == 0.5
    # 不能达到90%门。
    assert summary.quality_gate_passed is False


@pytest.mark.asyncio
async def test_step39_default_mode_reads_no_private_data_and_calls_no_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认运行只计算公开指纹，私有loader和模型工厂都不能被触发。"""

    # 直接导入实现模块以替换两个危险边界。
    import serviceops_agent.evaluation.semantic_judge_calibration as module

    # 加载公开配置不涉及私有正文。
    config = load_semantic_judge_calibration_config(CONFIG_PATH)

    # 若默认路径尝试加载私有项，测试立即失败。
    def fail_private_load(*args: object, **kwargs: object) -> None:
        # 显式引用参数避免静态检查未使用。
        _ = args, kwargs
        # 抛出稳定测试异常。
        raise AssertionError("默认路径不应读取私有诊断")

    # 替换私有loader。
    monkeypatch.setattr(module, "load_private_semantic_calibration_items", fail_private_load)

    # 若默认路径尝试创建真实Judge，测试立即失败。
    def fail_model_build(*args: object, **kwargs: object) -> None:
        # 显式引用参数。
        _ = args, kwargs
        # 抛出稳定测试异常。
        raise AssertionError("默认路径不应创建真实Judge")

    # 替换模型工厂边界。
    monkeypatch.setattr(module, "_build_real_judge_client", fail_model_build)
    # 构造明确无Key设置。
    settings = Settings.model_construct(telemetry_enabled=False)
    # 两把钥匙均关闭。
    report = await run_semantic_judge_calibration(
        config,
        runtime_settings=settings,
        confirm_private_regression=False,
        confirm_paid_api=False,
    )
    # 没有读取任何私有项。
    assert report.private_items_loaded == 0
    # 没有付费调用。
    assert report.paid_api_called is False
    # 实际调用数为0。
    assert report.actual_judge_calls == 0
    # 无summary，不能冒充校准结论。
    assert report.summary is None


def test_step39_verdict_schema_rejects_inconsistent_pass() -> None:
    """decision=PASS但证据布尔为False的服务商响应必须被拒绝。"""

    # Pydantic模型后置校验应该发现字段矛盾。
    with pytest.raises(ValueError):
        # 故意构造“没有证据支持但PASS”的非法响应。
        SemanticJudgeVerdict(
            answers_all_subquestions=True,
            fully_supported_by_evidence=False,
            contains_contradiction=False,
            decision="PASS",
            reason_code="complete_and_supported",
            brief_reason="非法通过。",
        )


@pytest.mark.parametrize(
    ("private", "paid", "frozen", "valid"),
    [
        # 默认公开计划合法。
        (False, False, False, True),
        # 只加载私有数据、零费用合法。
        (True, False, False, True),
        # 缺少私有确认时付费非法。
        (False, True, False, False),
        # 两把钥匙齐全且首次结果不存在时合法。
        (True, True, False, True),
        # 首次结果存在后禁止重复付费挑最好分数。
        (True, True, True, False),
    ],
)
def test_step39_cli_fails_closed(
    private: bool,
    paid: bool,
    frozen: bool,
    valid: bool,
) -> None:
    """CLI安全状态机必须在私有读取与API调用前拒绝非法组合。"""

    # 加载函数定义但不进入__main__。
    module = _load_step39_module()
    # 字段名与真实CLI一致。
    args = argparse.Namespace(
        confirm_private_regression=private,
        confirm_paid_api=paid,
    )
    # 合法组合不抛错。
    if valid:
        # 注入首次结果状态，不访问真实用户文件。
        module._validate_args(args, frozen_result_exists=frozen)
    else:
        # 非法组合必须快速失败。
        with pytest.raises(ValueError):
            module._validate_args(args, frozen_result_exists=frozen)


def test_step39_first_public_result_writer_is_exclusive(
    tmp_path: Path,
) -> None:
    """首次公开Judge结果只能创建一次，不能重复运行后覆盖。"""

    # 加载CLI公开payload和writer。
    module = _load_step39_module()
    # 读取公开配置。
    config = load_semantic_judge_calibration_config(CONFIG_PATH)
    # Fake报告直接用异步测试较复杂，这里先验证writer独占行为。
    result_path = tmp_path / "judge-result.json"
    # 第一次独占写入成功。
    module._write_atomic_exclusive(result_path, {"contains_private_questions": False})
    # 第二次不得覆盖。
    with pytest.raises(FileExistsError):
        module._write_atomic_exclusive(result_path, {"contains_private_questions": True})
    # 文件仍保持首次安全内容。
    raw = result_path.read_text(encoding="utf-8")
    # 不应包含私有问题字段。
    assert '"question"' not in raw
    # 不应包含Agent答案字段。
    assert '"answer"' not in raw
    # 不应包含证据正文数组。
    assert '"evidence"' not in raw
    # 指纹存在且已冻结，避免变量只为加载而未使用。
    assert config.frozen_judge_fingerprint


def test_step39_real_frozen_result_is_calibrated_and_private_text_free() -> None:
    """首次20/20结果必须保持独立口径，且公开文件不能含任何私有正文。"""

    # 读取真实用户已经生成的首次公开结果。
    raw = FROZEN_RESULT_PATH.read_text(encoding="utf-8")
    # JSON只包含聚合数与隐私声明。
    result = json.loads(raw)
    # 分母是揭晓前冻结的20条校准项。
    assert result["total_items"] == 20
    # Judge与人工标签逐项全部一致。
    assert result["matched_items"] == 20
    # 100%只属于Judge校准指标。
    assert result["calibration_accuracy"] == 1.0
    # 通过预设90%质量门。
    assert result["quality_gate_passed"] is True
    # 没有错项可公开。
    assert result["mismatched_items"] == []
    # 四项隐私声明全部为False。
    assert result["contains_private_questions"] is False
    assert result["contains_agent_answers"] is False
    assert result["contains_evidence_text"] is False
    assert result["contains_judge_reason_text"] is False
    # 不允许出现私有输入字段。
    assert '"question"' not in raw
    assert '"answer"' not in raw
    assert '"evidence"' not in raw
    assert '"brief_reason"' not in raw

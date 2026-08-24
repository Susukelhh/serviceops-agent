"""验证第38步密封集、范围门v2、最终状态评分和一次性运行边界。"""

# argparse直接构造两把钥匙的组合，不启动真实命令行。
import argparse

# importlib.util加载以数字开头、不能用普通import语法导入的示例脚本。
import importlib.util

# json读取公开脱敏人工审计，验证正式结果与复核结果没有混用。
import json

# Path声明公开配置和pytest临时文件位置。
from pathlib import Path

# ModuleType标注动态加载后的第38步脚本模块。
from types import ModuleType

# pytest提供异常断言和临时目录。
import pytest

# PROJECT_ROOT保证Windows、Linux CI与PyCharm使用同一项目根目录。
from serviceops_agent.config.paths import PROJECT_ROOT

# 第38步直接验证真实Schema、SHA、切片证据与候选指纹。
from serviceops_agent.evaluation import (
    grounded_answer_candidate_fingerprint,
    load_grounded_answer_success_config,
    load_private_grounded_answer_cases,
    validate_grounded_answer_evidence_labels,
)

# v2范围门用于验证新的安全咨询边界不再误拒绝。
from serviceops_agent.rag.query_policy import create_knowledge_query_policy

# CONFIG_PATH是不含问题正文的公开第38步冻结契约。
CONFIG_PATH: Path = (
    PROJECT_ROOT / "data/evaluation/grounded_answer_v3_sealed_experiment.json"
)
# AUDIT_PATH只含稳定Case ID与分类，不含私有问题、回答、证据或事实规则。
AUDIT_PATH: Path = (
    PROJECT_ROOT
    / "data/evaluation/results/grounded_answer_v3_sealed_regression_audit.json"
)


def _load_step38_module() -> ModuleType:
    """只加载函数定义，不进入__main__真实付费入口。"""

    # 数字开头的示例文件使用标准spec加载。
    script_path = PROJECT_ROOT / "examples/38_grounded_answer_v3_sealed.py"
    # 稳定模块名不会覆盖业务包。
    spec = importlib.util.spec_from_file_location(
        "serviceops_step38_cli_for_unit_tests",
        script_path,
    )
    # 文件损坏时给出比AttributeError更清楚的原因。
    if spec is None or spec.loader is None:
        # 测试立即失败。
        raise AssertionError("无法加载第38步CLI脚本")
    # 创建空模块对象。
    module = importlib.util.module_from_spec(spec)
    # __main__保护会阻止真实API执行。
    spec.loader.exec_module(module)
    # 返回模块供状态机和writer测试。
    return module


def test_step38_dataset_candidate_and_evidence_are_frozen() -> None:
    """30题、20正10负、证据锚点、范围门v2和指纹必须同时有效。"""

    # 公开配置不含密封问题或事实金标。
    config = load_grounded_answer_success_config(CONFIG_PATH)
    # 测试显式确认读取本机私有题集；默认CLI不会走到这里。
    cases = load_private_grounded_answer_cases(config, confirm_blind=True)
    # 总题数揭晓前固定为30。
    assert len(cases) == 30
    # 正例固定20条，避免全部拒答靠类别失衡过门。
    assert sum(case.should_answer for case in cases) == 20
    # 真正知识缺口固定10条。
    assert sum(not case.should_answer for case in cases) == 10
    # 每个事实锚点都必须能在500/80真实切片中找到支持。
    validate_grounded_answer_evidence_labels(config, cases)
    # 第38步保持Prompt v2，不把密封题答案写进提示词。
    assert config.grounding_prompt_version == "v2"
    # 新候选只改变经过开发的范围门策略。
    assert config.query_policy == "deterministic_v2"
    # 拒答引用按生产系统的最终可见状态清空后再评分。
    assert config.sanitize_declined_citations is True
    # 完整候选指纹必须与公开冻结值完全一致。
    assert grounded_answer_candidate_fingerprint(config) == (
        config.frozen_candidate_fingerprint
    )


def test_step38_scope_v2_allows_sealed_security_consultation() -> None:
    """安全咨询正例必须放行，但测试代码不公开其问题正文。"""

    # 加载冻结配置与本机私有样本。
    config = load_grounded_answer_success_config(CONFIG_PATH)
    # 显式确认只发生在本地测试中。
    cases = load_private_grounded_answer_cases(config, confirm_blind=True)
    # 通过稳定ID定位安全咨询样本，避免复制私有问题文本到公开测试代码。
    consultation = next(
        case
        for case in cases
        if case.case_id == "sealed3-security-consultation-paraphrase"
    )
    # 使用第38步实际配置创建范围门。
    policy = create_knowledge_query_policy(config.query_policy)
    # 新策略应识别“询问如何保护凭据”，而不是误判为“索取凭据”。
    assert policy.assess(consultation.question).allowed is True


@pytest.mark.parametrize(
    (
        "sealed",
        "paid",
        "regression",
        "diagnostics",
        "frozen_exists",
        "should_pass",
    ),
    [
        # 默认公开计划不读私有数据也不收费。
        (False, False, False, False, False, True),
        # 只确认sealed可运行完全离线对照。
        (True, False, False, False, False, True),
        # 缺少sealed时禁止付费。
        (False, True, False, False, False, False),
        # 两把钥匙齐全且首次结果不存在时允许首次真实运行。
        (True, True, False, False, False, True),
        # 冻结结果存在后拒绝不承认REGRESSION的重复付费运行。
        (True, True, False, False, True, False),
        # 已有首次结果后允许显式付费REGRESSION。
        (True, True, True, False, True, True),
        # 私有诊断必须建立在合法REGRESSION之上。
        (True, True, True, True, True, True),
        # 尚无首次结果时不能伪造REGRESSION。
        (True, True, True, True, False, False),
        # 只写诊断而不确认付费必须失败。
        (True, False, False, True, True, False),
    ],
)
def test_step38_cli_fails_closed(
    sealed: bool,
    paid: bool,
    regression: bool,
    diagnostics: bool,
    frozen_exists: bool,
    should_pass: bool,
) -> None:
    """四个开关与历史状态必须组成安全的一次性/回归状态机。"""

    # 动态加载不会触发__main__。
    module = _load_step38_module()
    # 字段名与真实CLI完全一致。
    args = argparse.Namespace(
        confirm_sealed=sealed,
        confirm_paid_api=paid,
        regression=regression,
        write_private_diagnostics=diagnostics,
    )
    # 合法组合应正常返回。
    if should_pass:
        # 注入历史文件状态，测试不依赖用户真实结果。
        module._validate_args(args, frozen_result_exists=frozen_exists)
    else:
        # 非法组合必须在读取私有文件、.env或调用API之前失败。
        with pytest.raises(ValueError):
            module._validate_args(args, frozen_result_exists=frozen_exists)


def test_step38_first_result_is_exclusive_and_runtime_replaceable(
    tmp_path: Path,
) -> None:
    """首次结果不能覆盖，runtime报告则允许原子更新。"""

    # 加载纯本地文件writer。
    module = _load_step38_module()
    # 首次结果路径放在pytest临时目录。
    frozen_path = tmp_path / "frozen.json"
    # 第一次写入应成功。
    module._write_atomic_exclusive(frozen_path, {"value": "first"})
    # 第二次写入同一位置必须原子失败。
    with pytest.raises(FileExistsError):
        module._write_atomic_exclusive(frozen_path, {"value": "second"})
    # 原文件仍保留第一次内容。
    assert '"first"' in frozen_path.read_text(encoding="utf-8")
    # runtime报告允许下一次离线运行替换旧报告。
    runtime_path = tmp_path / "runtime.json"
    # 写入第一轮。
    module._write_atomic_replace(runtime_path, {"round": 1})
    # 完整替换为第二轮。
    module._write_atomic_replace(runtime_path, {"round": 2})
    # 文件只包含完整的新结果。
    assert '"round": 2' in runtime_path.read_text(encoding="utf-8")


def test_step38_regression_snapshot_detects_any_frozen_result_change(
    tmp_path: Path,
) -> None:
    """REGRESSION只能读取首次结果，不能删除、覆盖或原样重写。"""

    # 加载纯本地历史保护函数。
    module = _load_step38_module()
    # 在pytest私有临时目录创建模拟首次结果。
    frozen_path = tmp_path / "frozen.json"
    # 首次内容代表不可变历史证据。
    frozen_path.write_text('{"score": 0.6667}\n', encoding="utf-8")
    # 记录内容、长度和纳秒修改时间。
    snapshot = module._snapshot(frozen_path)
    # 未发生变化时校验应通过。
    module._assert_snapshot_unchanged(frozen_path, snapshot)
    # 修改内容后必须检测出来。
    frozen_path.write_text('{"score": 1.0}\n', encoding="utf-8")
    # 任何篡改都触发固定异常。
    with pytest.raises(RuntimeError):
        module._assert_snapshot_unchanged(frozen_path, snapshot)


def test_step38_private_dataset_is_ignored_by_git_and_docker() -> None:
    """密封问题和事实标签不能进入Git或Docker镜像。"""

    # Git忽略规则属于防泄漏契约。
    gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    # Docker构建上下文也必须隔离私有数据。
    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
    # 目录级规则覆盖第38步私有题集。
    assert "data/private_evaluation/" in gitignore
    # Docker规则允许没有末尾斜杠，但必须覆盖同一目录。
    assert "data/private_evaluation" in dockerignore


def test_step38_public_audit_keeps_formal_and_human_results_separate() -> None:
    """人工语义复核不能覆盖首次66.67%，公开审计也不能泄漏私有正文。"""

    # 公开JSON可以安全进入Git，因此测试直接读取完整内容。
    raw = AUDIT_PATH.read_text(encoding="utf-8")
    # 解析后验证计数和分类互斥关系。
    audit = json.loads(raw)
    # 正式首次密封结果必须保持20/30与Gate FAIL。
    assert audit["formal_first_sealed_result"]["passed_cases"] == 20
    # 人工复核只记录已揭晓语义判断，不能改写formal字段。
    human = audit["human_semantic_adjudication"]
    # 8条是确定性匹配漏判。
    assert len(human["matcher_false_negative_case_ids"]) == 8
    # 2条是金标范围过严。
    assert len(human["gold_scope_too_strict_case_ids"]) == 2
    # 本轮没有证据支持继续调整Prompt。
    assert human["model_omission_case_ids"] == []
    # 四个隐私声明必须全部为False。
    assert not any(audit["privacy"].values())
    # 公开审计不包含私有正文常用字段名。
    assert '"question"' not in raw
    # 模型原始回答同样不进入公开文件。
    assert '"answer"' not in raw
    # 事实匹配规则正文也不能公开。
    assert '"answer_all_of"' not in raw

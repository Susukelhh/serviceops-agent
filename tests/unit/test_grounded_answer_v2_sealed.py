"""验证第36步全新密封集、候选冻结身份和一次性CLI安全边界。"""

# argparse直接构造开关组合，不启动真实命令行或模型。
import argparse

# importlib.util加载以数字开头、不能普通import的第36步脚本。
import importlib.util

# itertools生成四个布尔开关的全部16种组合。
import itertools

# Path声明公开配置、私有题集和pytest临时文件。
from pathlib import Path

# ModuleType标注动态导入后的脚本模块。
from types import ModuleType

# pytest提供异常断言、临时目录与方法替换。
import pytest

# PROJECT_ROOT保证Windows、Linux和PyCharm都从同一项目根定位文件。
from serviceops_agent.config.paths import PROJECT_ROOT

# 第36步直接验证真实Schema、SHA、证据锚点和候选指纹。
from serviceops_agent.evaluation import (
    grounded_answer_candidate_fingerprint,
    load_grounded_answer_success_config,
    load_private_grounded_answer_cases,
    validate_grounded_answer_evidence_labels,
)

# CONFIG_PATH是不含题目正文的公开第36步冻结契约。
CONFIG_PATH: Path = (
    PROJECT_ROOT / "data/evaluation/grounded_answer_v2_sealed_experiment.json"
)


def _load_step36_module() -> ModuleType:
    """只加载第36步函数定义，不进入__main__真实执行入口。"""

    # 文件名以数字开头，因此使用标准spec加载。
    script_path = PROJECT_ROOT / "examples/36_grounded_answer_v2_sealed.py"
    # 稳定测试模块名不会覆盖业务包。
    spec = importlib.util.spec_from_file_location(
        "serviceops_step36_cli_for_unit_tests",
        script_path,
    )
    # 缺少spec或loader表示仓库文件损坏。
    if spec is None or spec.loader is None:
        # 给出比AttributeError更清楚的测试原因。
        raise AssertionError("无法加载第36步CLI脚本")
    # 创建空模块对象。
    module = importlib.util.module_from_spec(spec)
    # 只执行常量和函数定义；__main__保护阻止API调用。
    spec.loader.exec_module(module)
    # 返回模块供状态机与writer测试使用。
    return module


def _args(
    *,
    confirm_sealed: bool,
    confirm_paid_api: bool,
    regression: bool,
    write_private_diagnostics: bool,
) -> argparse.Namespace:
    """构造与真实CLI字段完全一致的参数对象。"""

    # Namespace避免测试依赖sys.argv全局状态。
    return argparse.Namespace(
        confirm_sealed=confirm_sealed,
        confirm_paid_api=confirm_paid_api,
        regression=regression,
        write_private_diagnostics=write_private_diagnostics,
    )


def test_step36_sealed_dataset_contract_and_evidence_are_frozen() -> None:
    """30题、20正10负、SHA、证据切片和Prompt v2指纹必须同时有效。"""

    # 公开配置不含问题正文。
    config = load_grounded_answer_success_config(CONFIG_PATH)
    # 测试显式确认读取本机私有题集；普通CLI默认路径不会执行这一步。
    cases = load_private_grounded_answer_cases(config, confirm_blind=True)
    # 计数必须是揭晓前冻结的20正10负。
    assert len(cases) == 30
    # 可回答题数量单独核对，防止全部拒答靠类别失衡过门。
    assert sum(case.should_answer for case in cases) == 20
    # 知识缺口题数量同样固定。
    assert sum(not case.should_answer for case in cases) == 10
    # 每条证据锚点都必须在500/80真实切片中由人工来源文档支持。
    validate_grounded_answer_evidence_labels(config, cases)
    # 配置必须显式使用已经完成开发回归的Prompt v2。
    assert config.grounding_prompt_version == "v2"
    # 重新计算完整运行指纹必须与公开冻结值逐字节一致。
    assert grounded_answer_candidate_fingerprint(config) == (
        config.frozen_candidate_fingerprint
    )


def test_step36_cli_all_flag_combinations_fail_closed() -> None:
    """四个开关的16种组合只能放行计划、离线、首次和合法回归。"""

    # 动态导入不会运行模型。
    module = _load_step36_module()
    # 分别验证“首次结果不存在”和“已经存在”两种历史状态。
    for frozen_exists in (False, True):
        # 生成四个布尔开关的笛卡尔积。
        for sealed, paid, regression, diagnostics in itertools.product(
            (False, True), repeat=4
        ):
            # 当前组合是否应当被安全状态机允许。
            valid = (
                # 默认或只确认sealed的零费用模式。
                (not paid and not regression and not diagnostics)
                # 首次真实运行：必须有sealed+paid，且历史结果尚不存在。
                or (
                    sealed
                    and paid
                    and not regression
                    and not diagnostics
                    and not frozen_exists
                )
                # 已揭晓回归：必须有首次结果；诊断可选。
                or (
                    sealed
                    and paid
                    and regression
                    and frozen_exists
                )
            )
            # 构造真实字段名参数。
            args = _args(
                confirm_sealed=sealed,
                confirm_paid_api=paid,
                regression=regression,
                write_private_diagnostics=diagnostics,
            )
            # 合法组合不应抛错。
            if valid:
                # 注入文件存在状态，测试不访问真实公开结果。
                module._validate_args(
                    args,
                    frozen_result_exists=frozen_exists,
                )
            else:
                # 非法组合必须在读取私有文件或Key前快速失败。
                with pytest.raises(ValueError):
                    module._validate_args(
                        args,
                        frozen_result_exists=frozen_exists,
                    )


def test_step36_first_result_is_exclusive_and_runtime_is_replaceable(
    tmp_path: Path,
) -> None:
    """首次公开结果不可覆盖，而runtime报告允许完整原子更新。"""

    # 加载纯本地writer函数。
    module = _load_step36_module()
    # 首次文件使用独占发布。
    frozen_path = tmp_path / "frozen.json"
    # 第一份内容应成功写入。
    module._write_atomic_exclusive(frozen_path, {"value": "first"})
    # 第二次写同一路径必须失败，保护首次历史证据。
    with pytest.raises(FileExistsError):
        module._write_atomic_exclusive(frozen_path, {"value": "second"})
    # 文件仍然只包含第一次内容。
    assert '"first"' in frozen_path.read_text(encoding="utf-8")
    # runtime路径允许新一轮完整报告替换旧报告。
    runtime_path = tmp_path / "runtime.json"
    # 先写第一轮。
    module._write_atomic_replace(runtime_path, {"round": 1})
    # 再写第二轮。
    module._write_atomic_replace(runtime_path, {"round": 2})
    # 最终文件是完整第二轮JSON，不是拼接或半文件。
    assert '"round": 2' in runtime_path.read_text(encoding="utf-8")


def test_step36_private_dataset_is_ignored_by_git_and_docker() -> None:
    """问题、金标与将来的诊断文件不能进入Git或Docker构建上下文。"""

    # 两份忽略文件是公开安全契约的一部分。
    gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    # Docker构建同样不能复制私有评测数据。
    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
    # 目录级规则应覆盖第36步文件和未来诊断。
    assert "data/private_evaluation/" in gitignore
    # Docker规则允许有或没有末尾斜杠，但必须覆盖同一目录。
    assert "data/private_evaluation" in dockerignore

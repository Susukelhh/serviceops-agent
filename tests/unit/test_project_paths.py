"""项目根目录发现策略的单元测试。"""

# Path 用于构造 Windows/Linux 都可理解的临时目录和模拟模块路径。
from pathlib import Path

# pytest 提供异常断言和 tmp_path 临时目录夹具。
import pytest

# 被测函数不依赖全局 PROJECT_ROOT，因此可以隔离验证每一层优先级。
from serviceops_agent.config.paths import PROJECT_ROOT_ENV_VAR, discover_project_root


def test_explicit_absolute_root_has_highest_priority(tmp_path: Path) -> None:
    """显式绝对部署根目录必须覆盖源码位置和当前工作目录。"""

    # Arrange：创建三个不同候选，显式目录应最终胜出。
    explicit_root = tmp_path / "mounted-app"
    # 模拟源码文件不需要真实存在，因为显式配置会在自动发现前返回。
    fake_module = tmp_path / "repo" / "src" / "serviceops_agent" / "config" / "paths.py"
    # Act：只向函数传入隔离字典，不污染真实测试进程环境变量。
    actual_root = discover_project_root(
        environment={PROJECT_ROOT_ENV_VAR: str(explicit_root)},
        module_file=fake_module,
        current_directory=tmp_path / "working-directory",
    )
    # Assert：结果是规范化后的显式挂载目录。
    assert actual_root == explicit_root.resolve()


def test_relative_explicit_root_is_rejected() -> None:
    """相对部署根目录必须尽早失败，不能留下随启动目录漂移的隐患。"""

    # Act/Assert：错误信息只说明变量名和绝对路径要求。
    with pytest.raises(ValueError, match=PROJECT_ROOT_ENV_VAR):
        # 传入相对值模拟错误的 Docker 或系统服务配置。
        discover_project_root(environment={PROJECT_ROOT_ENV_VAR: "relative/app"})


def test_source_repository_is_discovered_without_environment(tmp_path: Path) -> None:
    """源码开发模式应识别仓库标记，而不是依赖 PyCharm Working directory。"""

    # Arrange：构造与真实仓库相同的 src 包目录层级。
    repository_root = tmp_path / "serviceops-agent"
    # 创建 pyproject.toml，作为第一个仓库身份标记。
    (repository_root / "pyproject.toml").parent.mkdir(parents=True)
    # 空文件内容足以供路径发现测试判断 is_file。
    (repository_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    # 创建包目录，作为第二个仓库身份标记。
    package_directory = repository_root / "src" / "serviceops_agent"
    # parents=True 同时创建 src 与 serviceops_agent 两级目录。
    package_directory.mkdir(parents=True)
    # 构造 paths.py 的模拟位置；发现函数只需读取其层级关系。
    fake_module = package_directory / "config" / "paths.py"
    # Act：当前工作目录故意指向别处，证明源码识别不依赖它。
    actual_root = discover_project_root(
        environment={},
        module_file=fake_module,
        current_directory=tmp_path / "unrelated-working-directory",
    )
    # Assert：自动发现得到源码仓库根目录。
    assert actual_root == repository_root.resolve()


def test_installed_package_falls_back_to_working_directory(tmp_path: Path) -> None:
    """site-packages 中的 wheel 无仓库标记时应退回启动目录。"""

    # Arrange：模拟虚拟环境里的安装位置，不创建源码仓库标记。
    installed_module = (
        tmp_path
        / ".venv"
        / "Lib"
        / "site-packages"
        / "serviceops_agent"
        / "config"
        / "paths.py"
    )
    # 启动目录模拟容器的 /app；测试不要求目录实际存在。
    working_directory = tmp_path / "app"
    # Act：环境变量为空，因此依次尝试源码识别和当前目录兜底。
    actual_root = discover_project_root(
        environment={},
        module_file=installed_module,
        current_directory=working_directory,
    )
    # Assert：不能把 .venv/Lib 当作项目根目录。
    assert actual_root == working_directory.resolve()

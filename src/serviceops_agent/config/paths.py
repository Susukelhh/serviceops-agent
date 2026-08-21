"""集中解析不应依赖进程当前工作目录的项目路径。"""

# os 提供当前进程的环境变量；部署环境可用它显式声明项目根目录。
import os

# Mapping 表达只读键值映射，便于测试时传入隔离的环境变量字典。
from collections.abc import Mapping

# Path 提供跨平台绝对路径判断、拼接和规范化。
from pathlib import Path

# 容器、虚拟机或系统服务使用该变量明确告诉应用数据文件挂载在哪个根目录下。
PROJECT_ROOT_ENV_VAR = "SERVICEOPS_PROJECT_ROOT"


def _looks_like_source_repository(candidate: Path) -> bool:
    """判断候选目录是否是当前项目的源码仓库根目录。"""

    # pyproject.toml 是 Python 项目的构建入口，可排除普通父目录。
    has_project_file = (candidate / "pyproject.toml").is_file()
    # 包目录进一步确认这是 ServiceOps Agent，而不是碰巧存在 pyproject.toml 的其他项目。
    has_package_directory = (candidate / "src" / "serviceops_agent").is_dir()
    # 两个标记必须同时存在，才接受自动发现结果。
    return has_project_file and has_package_directory


def discover_project_root(
    *,
    environment: Mapping[str, str] | None = None,
    module_file: str | Path | None = None,
    current_directory: str | Path | None = None,
) -> Path:
    """发现应用根目录，并允许单元测试注入隔离输入。

    解析优先级如下：

    1. 部署环境显式提供的 ``SERVICEOPS_PROJECT_ROOT``；
    2. 当前模块确实位于 ``项目根/src/serviceops_agent/config`` 时识别源码仓库；
    3. 包已安装到虚拟环境且没有显式配置时，退回进程当前工作目录。

    第三层只用于方便本地命令行运行。容器和系统服务应始终使用第一层，避免启动目录变化。
    """

    # 测试可传入普通字典；生产代码不传时才读取真实进程环境变量。
    selected_environment = os.environ if environment is None else environment
    # 空字符串视为没有配置，避免把它意外解释为当前目录。
    explicit_root = selected_environment.get(PROJECT_ROOT_ENV_VAR, "").strip()
    # 显式部署配置拥有最高优先级，因为 wheel 安装位置不等于业务数据位置。
    if explicit_root:
        # expanduser 仅展开用户主动写入的 ``~``，不会访问或修改目标文件。
        explicit_candidate = Path(explicit_root).expanduser()
        # 部署根目录必须是绝对路径，否则仍会随着进程工作目录改变含义。
        if not explicit_candidate.is_absolute():
            # 错误只回显变量名，不泄漏具体服务器目录结构。
            raise ValueError(f"{PROJECT_ROOT_ENV_VAR} 必须配置为绝对路径")
        # strict=False 的 resolve 只做规范化，不要求容器构建阶段目录已经存在。
        return explicit_candidate.resolve(strict=False)

    # 测试可模拟源码文件位置；正常运行时使用当前 paths.py 的真实位置。
    selected_module_file = Path(__file__ if module_file is None else module_file).resolve()
    # 源码布局下 parents[3] 对应 ``项目根``；先判断深度，避免异常路径触发 IndexError。
    if len(selected_module_file.parents) > 3:
        # 只把父目录当作候选，随后还要用两个仓库标记验证，不能盲信固定层级。
        source_candidate = selected_module_file.parents[3]
        # 可验证的源码仓库继续保持与 PyCharm Working directory 无关的稳定行为。
        if _looks_like_source_repository(source_candidate):
            # 候选本身来自已规范化模块路径，因此可直接返回。
            return source_candidate

    # wheel 安装场景无法从 site-packages 反推出业务数据位置，只能使用启动目录兜底。
    fallback_directory = Path.cwd() if current_directory is None else Path(current_directory)
    # 统一返回规范化绝对路径，让后续路径拼接保持可预测。
    return fallback_directory.resolve(strict=False)


# 模块导入时计算一次稳定根目录；容器会显式设置 SERVICEOPS_PROJECT_ROOT=/app。
PROJECT_ROOT = discover_project_root()


def resolve_project_path(path_value: str | Path) -> Path:
    """把相对配置路径稳定解析到项目根目录，同时保留用户提供的绝对路径。

    PyCharm、pytest、命令行和生产进程可能使用不同工作目录。如果直接 ``Path(relative)``，
    同一个配置会随着启动位置指向不同文件。这里明确规定：相对路径都以项目根目录为基准。
    """

    # 统一把环境变量字符串和已经构造的 Path 转换为 Path 对象。
    candidate = Path(path_value)
    # 绝对路径代表用户已经明确指定外部挂载或数据目录，不应再拼接项目根目录。
    if candidate.is_absolute():
        # resolve 规范化 ``..`` 和分隔符，默认不要求目标必须已经存在。
        return candidate.resolve(strict=False)
    # 相对路径统一拼接 PROJECT_ROOT，因此不受 os.getcwd() 或 PyCharm Working directory 影响。
    return (PROJECT_ROOT / candidate).resolve(strict=False)

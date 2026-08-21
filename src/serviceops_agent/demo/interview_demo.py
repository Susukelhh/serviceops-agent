"""ServiceOps Agent 一键面试演示启动器。

这个模块解决的现实问题是：面试演示前往往需要记住多条 Docker、冒烟和 Token 命令，
任何一步遗漏都会让“代码本身没问题”变成“现场打不开”。启动器把这些既有步骤按固定顺序
编排起来，但不复制 Agent 业务逻辑，也不添加免认证演示后门。

默认流程：
1. 找到当前 Windows 或 PATH 中的 Docker；
2. 校验 Compose 配置；
3. 重新构建并等待本地五角色拓扑就绪；
4. 调用第 20.2 步真实控制台冒烟；
5. 在当前终端打印短期角色 Token；
6. 打开本机控制台，并写入不含 Token 的预检报告。
"""

# json 只用于写入有限预检报告，报告不会包含 Token、API Key 或用户消息。
import json

# os 用于读取 LOCALAPPDATA、ProgramFiles 和当前子进程环境。
import os

# shutil.which 优先查找用户 PATH 中已经可用的 docker 命令。
import shutil

# subprocess 使用参数列表启动 Docker 和已有 Python 示例，避免 shell 字符串拼接。
import subprocess

# sys.executable 保证子示例继续使用当前 PyCharm 解释器，而不是系统里的另一个 Python。
import sys

# webbrowser 只打开本机控制台地址，不访问互联网。
import webbrowser

# Callable/Sequence 为可替换命令执行器提供清晰类型，便于无 Docker 单元测试。
from collections.abc import Callable, Sequence

# UTC 时间写入报告，避免不同时区查看报告时产生歧义。
from datetime import UTC, datetime

# Path 负责安全组合已知目录和固定文件名，不使用字符串拼接文件路径。
from pathlib import Path

# resolve_project_path 让脚本从 PyCharm、终端或安装包运行时都能定位项目根目录。
from serviceops_agent.config.paths import resolve_project_path

# 本地演示只访问 Nginx 暴露的回环地址，不扫描局域网或公网端口。
CONSOLE_URL = "http://127.0.0.1:8000/console/"

# 传入当前相对目录“.”，由统一路径解析器得到项目根；所有子进程都固定在这里运行。
PROJECT_ROOT = resolve_project_path(".")

# 复用已经通过测试的控制台真实冒烟，不在启动器里复制 HTTP 验证代码。
CONSOLE_SMOKE_SCRIPT = resolve_project_path("examples/20_agent_console_smoke_test.py")

# 复用现有短期 Token 生成器；Token 只打印到当前终端，不写入启动器报告。
TOKEN_SCRIPT = resolve_project_path("examples/09_generate_dev_tokens.py")

# 预检报告放入已经被 Git 忽略的 runtime 目录，只保存成功/失败边界摘要。
DEFAULT_REPORT_PATH = resolve_project_path("data/runtime/interview_demo_preflight_report.json")


# CommandRunner 把“执行参数、工作目录、子进程环境”映射为退出码。
CommandRunner = Callable[[Sequence[str], Path, dict[str, str]], int]

# BrowserOpener 接收固定本地 URL，并返回浏览器是否接受打开请求。
BrowserOpener = Callable[[str], bool]


class DemoPreflightError(RuntimeError):
    """某个演示准备步骤失败时抛出的有限中文错误。"""


def _default_command_runner(
    command: Sequence[str],
    working_directory: Path,
    child_environment: dict[str, str],
) -> int:
    """在当前终端直接显示子命令输出，并返回真实退出码。"""

    # list(command) 生成独立参数数组；不启用 shell，因此参数不会再次被命令解释器解析。
    completed = subprocess.run(
        list(command),
        # cwd 固定为项目根目录，Compose 和示例中的相对路径保持一致。
        cwd=working_directory,
        # env 包含当前环境及 UTF-8 覆盖，不打印其中可能存在的配置值。
        env=child_environment,
        # check=False 让上层根据步骤名称生成更易懂的中文错误，而不是长异常堆栈。
        check=False,
    )
    # returncode=0 表示该步骤成功，其他值由 _run_step 统一转换为 DemoPreflightError。
    return completed.returncode


def _docker_candidates() -> list[Path]:
    """返回当前电脑上可能存在的 Docker Desktop 命令路径。"""

    # 候选列表从空开始，只加入已经存在的 Windows 环境根目录。
    candidates: list[Path] = []
    # LOCALAPPDATA 覆盖用户级安装；当前电脑的 Docker Desktop 就位于该路径下。
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        # DockerDesktop 是当前安装目录名，后面的 resources/bin/docker.exe 是固定官方结构。
        candidates.append(
            Path(local_app_data) / "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe"
        )
    # PROGRAMFILES 覆盖更常见的全局 Docker Desktop 安装方式；Windows 变量本身不区分大小写。
    program_files = os.environ.get("PROGRAMFILES")
    if program_files:
        # 全局安装通常多一层 Docker/Docker 目录，因此作为第二候选。
        candidates.append(
            Path(program_files) / "Docker" / "Docker" / "resources" / "bin" / "docker.exe"
        )
    # 返回有限、可审计的固定候选，不递归扫描磁盘。
    return candidates


def resolve_docker_executable() -> Path:
    """优先使用 PATH 中的 Docker，否则检查两个已知 Docker Desktop 安装位置。"""

    # shutil.which 只查询操作系统 PATH，不执行任何命令。
    path_match = shutil.which("docker")
    if path_match:
        # resolve 把命中的命令规范为绝对路径，便于报告只记录“已解析”而非猜测。
        return Path(path_match).resolve()
    # PATH 不含 Docker 时逐一检查固定候选；这是 Codex/PyCharm 常见情况。
    for candidate in _docker_candidates():
        # is_file 同时排除不存在路径和同名目录。
        if candidate.is_file():
            return candidate.resolve()
    # 不尝试下载或安装 Docker，因为这会超出演示启动器的安全职责。
    raise DemoPreflightError(
        "没有找到 docker.exe。请先确认 Docker Desktop 已安装并显示 Engine running。",
    )


def _child_environment() -> dict[str, str]:
    """复制当前环境，并强制 Python 子示例使用 UTF-8 输出。"""

    # dict(os.environ) 产生副本，后续修改不会污染 PyCharm 自身环境。
    child_environment = dict(os.environ)
    # PYTHONUTF8=1 让中文 Token 标签和 PASS 结果在 PyCharm 中稳定显示。
    child_environment["PYTHONUTF8"] = "1"
    # 返回值只传给子进程，不写入报告。
    return child_environment


def _run_step(
    *,
    label: str,
    command: Sequence[str],
    command_runner: CommandRunner,
    child_environment: dict[str, str],
) -> None:
    """运行一个有名称的演示步骤，并把非零退出码转换成短错误。"""

    # 分隔线帮助用户在 PyCharm 输出里知道当前正在准备哪一部分。
    # flush=True 先把标题送到 PyCharm，再让 Docker 输出，避免缓冲导致说明落到结果后面。
    print(f"\n=== {label} ===", flush=True)
    # command_runner 默认直接继承终端输出；测试中可替换成不会启动真实进程的函数。
    return_code = command_runner(command, PROJECT_ROOT, child_environment)
    # 任何非零码都立即停止后续 Token 和浏览器步骤，避免带着坏环境开始演示。
    if return_code != 0:
        raise DemoPreflightError(f"{label}失败，退出码为 {return_code}。请先处理上方错误。")


def _write_report(*, report_path: Path, payload: dict[str, object]) -> None:
    """原子性要求较低的本地预检报告写入；内容不含任何凭证。"""

    # runtime 目录可能在全新克隆后不存在，因此按需创建父目录。
    report_path.parent.mkdir(parents=True, exist_ok=True)
    # ensure_ascii=False 保留中文步骤名；indent=2 方便在 PyCharm 中直接阅读。
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def prepare_interview_demo(
    *,
    build_image: bool = True,
    open_browser: bool = True,
    print_tokens: bool = True,
    command_runner: CommandRunner = _default_command_runner,
    browser_opener: BrowserOpener = webbrowser.open,
    report_path: Path = DEFAULT_REPORT_PATH,
) -> dict[str, object]:
    """完成本地面试演示预检，并返回不含敏感信息的有限报告。"""

    # 第一行说明本流程不会使用真实模型，降低用户对千问费用的担心。
    print("=== ServiceOps Agent 第21步：一键面试演示准备 ===", flush=True)
    print(
        "说明：使用 Docker 的 mock/deterministic 配置，不调用千问，不自动批准任何写操作。",
        flush=True,
    )
    # 在执行任何外部命令前先解析唯一 Docker 路径。
    docker_executable = resolve_docker_executable()
    # 所有 Python 子示例共享同一 UTF-8 环境副本。
    child_environment = _child_environment()

    # compose config 只解析配置，可在真正构建前发现 YAML 或环境替换错误。
    _run_step(
        label="1/4 校验 Docker Compose 配置",
        command=[str(docker_executable), "compose", "config", "--quiet"],
        command_runner=command_runner,
        child_environment=child_environment,
    )

    # up 命令始终等待健康检查；build_image=True 时再加入 --build，避免演示旧代码。
    compose_up_command = [
        str(docker_executable),
        "compose",
        "up",
        "--detach",
    ]
    if build_image:
        # --build 放在 up 参数后，与用户前面已经验证过的手工命令保持一致。
        compose_up_command.append("--build")
    # --wait 只有四个长期容器健康、迁移任务成功退出后才返回 0。
    compose_up_command.extend(["--wait", "--wait-timeout", "180"])
    _run_step(
        label="2/4 启动并等待本地演示环境",
        command=compose_up_command,
        command_runner=command_runner,
        child_environment=child_environment,
    )

    # 第20.2步冒烟验证真实页面、JWT 订单工具、Nginx、PostgreSQL 和 Checkpoint 回放。
    _run_step(
        label="3/4 验证真实 Agent 演示链路",
        command=[sys.executable, str(CONSOLE_SMOKE_SCRIPT)],
        command_runner=command_runner,
        child_environment=child_environment,
    )

    if print_tokens:
        # Token 示例只向当前 PyCharm 控制台输出短期凭证，不写入任何报告或网页存储。
        _run_step(
            label="4/4 生成本次演示短期身份",
            command=[sys.executable, str(TOKEN_SCRIPT)],
            command_runner=command_runner,
            child_environment=child_environment,
        )
    else:
        # --no-tokens 适合只做环境检查；明确提示用户本次没有生成网页登录身份。
        print("\n=== 4/4 跳过短期身份输出 ===", flush=True)

    # 默认打开本地页面；浏览器拒绝打开不会让已经健康的后端被误判为失败。
    browser_opened = browser_opener(CONSOLE_URL) if open_browser else False
    # 报告只记录布尔结论、时间和固定 URL，不记录 docker.exe 本机路径或 Token。
    report: dict[str, object] = {
        "step": "21",
        "status": "pass",
        "generated_at": datetime.now(UTC).isoformat(),
        "compose_config": "pass",
        "compose_healthy": "pass",
        "console_smoke": "pass",
        "build_image": build_image,
        "tokens_printed_to_current_terminal": print_tokens,
        "tokens_persisted_by_launcher": False,
        "browser_open_requested": open_browser,
        "browser_open_accepted": browser_opened,
        "console_url": CONSOLE_URL,
        "paid_model_called": False,
    }
    # 写入报告后即使用户关闭 PyCharm，也能证明面试前环境检查通过。
    _write_report(report_path=report_path, payload=report)

    # 最后输出最少三条行动指引，避免用户在一长串 Token 后不知道下一步做什么。
    print("\n=== 面试演示环境 READY ===", flush=True)
    print(f"控制台：{CONSOLE_URL}", flush=True)
    print(
        "下一步：把上方 customer 与 developer Token 粘贴进网页，先演示订单工具和 Checkpoint 大屏。",
        flush=True,
    )
    print(f"预检报告：{report_path}", flush=True)
    # 返回报告便于测试和未来 GUI 调用，不返回任何凭证。
    return report

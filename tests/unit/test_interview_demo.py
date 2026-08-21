"""第21步一键面试演示启动器的无 Docker 单元测试。"""

# json 用于读取启动器写入的有限预检报告。
import json

# Sequence 与生产 CommandRunner 契约一致，测试不需要假装命令是任意 object。
from collections.abc import Sequence

# Path 构造测试专用报告路径和虚拟 docker.exe 路径。
from pathlib import Path

# pytest 提供 monkeypatch 和异常断言。
import pytest

# 直接导入模块，便于替换 Docker 解析而不启动真实外部进程。
from serviceops_agent.demo import interview_demo


def test_prepare_interview_demo_runs_safe_steps_in_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """默认预检应按配置、启动、冒烟、Token 顺序执行，并打开本地页面。"""

    # Arrange：使用虚拟绝对路径，启动器只把它放入参数列表，不要求文件真实存在。
    fake_docker = Path("C:/Docker/docker.exe")
    monkeypatch.setattr(interview_demo, "resolve_docker_executable", lambda: fake_docker)
    # commands 保存每次调用的参数、工作目录和 UTF-8 子进程配置。
    commands: list[tuple[list[str], Path, dict[str, str]]] = []

    def fake_runner(
        command: Sequence[str],
        working_directory: Path,
        child_environment: dict[str, str],
    ) -> int:
        """记录命令并模拟退出码 0，不启动 Docker 或 Python 子进程。"""

        # command 在生产实现中是 Sequence[str]；list 复制后便于稳定断言。
        commands.append((list(command), working_directory, child_environment))
        # 0 表示本步骤通过。
        return 0

    # opened_urls 证明只请求打开固定 localhost 页面。
    opened_urls: list[str] = []

    def fake_browser_opener(url: str) -> bool:
        """记录本地 URL 并模拟浏览器接受请求。"""

        # 保存调用值供 Assert 阶段检查。
        opened_urls.append(url)
        # True 与 webbrowser.open 成功接受请求的语义一致。
        return True

    # 测试报告写到 pytest 临时目录，不污染项目 data/runtime。
    report_path = tmp_path / "interview-demo-report.json"

    # Act：执行完整默认流程，但所有外部边界都已替换为内存假实现。
    report = interview_demo.prepare_interview_demo(
        command_runner=fake_runner,
        browser_opener=fake_browser_opener,
        report_path=report_path,
    )

    # Assert：四个命令严格按面试准备顺序出现。
    assert len(commands) == 4
    assert commands[0][0] == [str(fake_docker), "compose", "config", "--quiet"]
    assert commands[1][0][:4] == [str(fake_docker), "compose", "up", "--detach"]
    assert "--build" in commands[1][0]
    assert commands[1][0][-3:] == ["--wait", "--wait-timeout", "180"]
    assert commands[2][0][1].endswith("20_agent_console_smoke_test.py")
    assert commands[3][0][1].endswith("09_generate_dev_tokens.py")
    # 每个子进程都在项目根目录运行，并获得 UTF-8 输出设置。
    assert all(item[1] == interview_demo.PROJECT_ROOT for item in commands)
    assert all(item[2]["PYTHONUTF8"] == "1" for item in commands)
    # 浏览器只能打开固定本机控制台，不能访问第三方站点。
    assert opened_urls == [interview_demo.CONSOLE_URL]
    # 返回值和磁盘报告都明确没有持久化 Token，也没有调用付费模型。
    assert report["status"] == "pass"
    assert report["tokens_persisted_by_launcher"] is False
    assert report["paid_model_called"] is False
    assert json.loads(report_path.read_text(encoding="utf-8")) == report


def test_prepare_interview_demo_supports_fast_environment_only_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """关闭构建、Token 和浏览器后仍应验证 Compose 与真实冒烟。"""

    # Arrange：固定 Docker 解析，并用列表只记录命令参数。
    monkeypatch.setattr(
        interview_demo,
        "resolve_docker_executable",
        lambda: Path("D:/Docker/docker.exe"),
    )
    commands: list[list[str]] = []

    def fake_runner(
        command: Sequence[str],
        _working_directory: Path,
        _child_environment: dict[str, str],
    ) -> int:
        """记录快速模式命令。"""

        commands.append(list(command))
        return 0

    # browser_opener 在 open_browser=False 时不应被调用；调用就让测试立即失败。
    def forbidden_browser_opener(_url: str) -> bool:
        raise AssertionError("快速环境模式不应打开浏览器")

    # Act：关闭三个可选动作，只保留配置、启动和冒烟。
    report = interview_demo.prepare_interview_demo(
        build_image=False,
        open_browser=False,
        print_tokens=False,
        command_runner=fake_runner,
        browser_opener=forbidden_browser_opener,
        report_path=tmp_path / "fast-report.json",
    )

    # Assert：只运行三条命令，Compose up 不含 --build，且没有 Token 示例。
    assert len(commands) == 3
    assert "--build" not in commands[1]
    assert commands[2][1].endswith("20_agent_console_smoke_test.py")
    assert report["tokens_printed_to_current_terminal"] is False
    assert report["browser_open_requested"] is False
    assert report["browser_open_accepted"] is False


def test_prepare_interview_demo_stops_before_tokens_when_stack_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Docker 启动失败后不能继续打印身份或打开一个不可用页面。"""

    # Arrange：第一条 config 成功，第二条 compose up 返回 7。
    monkeypatch.setattr(
        interview_demo,
        "resolve_docker_executable",
        lambda: Path("C:/Docker/docker.exe"),
    )
    call_count = 0

    def failing_runner(
        _command: Sequence[str],
        _working_directory: Path,
        _child_environment: dict[str, str],
    ) -> int:
        """只让第二个外部步骤失败。"""

        nonlocal call_count
        call_count += 1
        return 7 if call_count == 2 else 0

    # browser_called 用于证明异常发生后没有继续打开页面。
    browser_called = False

    def fake_browser_opener(_url: str) -> bool:
        """若被调用则记录错误行为。"""

        nonlocal browser_called
        browser_called = True
        return True

    # Act/Assert：错误包含具体步骤和退出码，不泄露环境变量。
    with pytest.raises(
        interview_demo.DemoPreflightError,
        match="启动并等待本地演示环境失败，退出码为 7",
    ):
        interview_demo.prepare_interview_demo(
            command_runner=failing_runner,
            browser_opener=fake_browser_opener,
            report_path=tmp_path / "should-not-exist.json",
        )

    # 失败后只执行 config/up 两步，报告、Token 和浏览器都没有继续。
    assert call_count == 2
    assert browser_called is False
    assert not (tmp_path / "should-not-exist.json").exists()

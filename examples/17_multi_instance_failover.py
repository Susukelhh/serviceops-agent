"""第十七步示例：自动验证网关轮询、跨实例恢复和单实例故障切换。

运行前先保证 Docker Desktop 处于 Engine running，然后在 PyCharm 直接运行本文件，
或在项目根目录执行：

    uv run python examples/17_multi_instance_failover.py

脚本会按下面顺序完成一场可重复演练：

1. 确认 Nginx 能把请求交给 agent-a 和 agent-b；
2. 暂停 agent-b，只让 agent-a 创建一个等待人工审批的 LangGraph 线程；
3. 启动 agent-b 并暂停 agent-a；
4. 只让 agent-b 从共享 PostgreSQL 恢复原线程并完成审批；
5. 验证审计哈希链，然后恢复两只 Agent。

脚本不会调用千问，不消耗模型额度，也不会删除 PostgreSQL 数据卷。短期 JWT 只存在于
当前 Python 进程内存和本机回环 HTTP Header，不写入报告或终端。
"""

# argparse 允许学习者覆盖网关地址，同时保留安全的本机默认值。
import argparse

# json 负责编解码 API 请求和生成不含凭证的演练报告。
import json

# os 用于定位 Windows 当前用户的 Docker Desktop 安装目录。
import os

# shutil.which 优先寻找 PowerShell/PyCharm PATH 中已经可用的 docker 命令。
import shutil

# subprocess 只负责启停当前 Compose 中的 Agent 服务，不执行数据库删除命令。
import subprocess

# time 提供等待容器重新就绪所需的单调时钟和短轮询间隔。
import time

# datetime/UTC 为报告记录无歧义的演练时间，并生成本次唯一幂等键。
from datetime import UTC, datetime

# Path 组合 Windows Docker Desktop 的候选安装路径，不手工拼接反斜杠。
from pathlib import Path

# HTTPError 保留 4xx JSON；URLError 表示故障切换窗口中的短暂连接失败。
from urllib.error import HTTPError, URLError

# Request/urlopen 使用 Python 标准库访问本机 Nginx，不新增脚本运行依赖。
from urllib.request import Request, urlopen

# 项目路径函数让 PyCharm 无论采用哪个 Working directory 都能定位 Compose 和报告。
from serviceops_agent.config.paths import PROJECT_ROOT, resolve_project_path

# Settings 与 Compose 的本地默认值共同决定 JWT issuer、audience 和签名参数。
from serviceops_agent.config.settings import get_settings

# Token 工厂签发短期本地演示凭证，避免在仓库保存现成 Token。
from serviceops_agent.security.jwt_auth import create_access_token

# 三个互相分离的角色分别发起请求、审批和读取审计证据。
from serviceops_agent.security.models import Role

# 报告只保存实例名和稳定业务编号，data/runtime 已被 Git 与 Docker 构建忽略。
REPORT_PATH = resolve_project_path("data/runtime/multi_instance_step17_report.json")


def _parse_arguments() -> argparse.Namespace:
    """读取本机网关地址；正常学习无需填写任何参数。"""

    # 创建仅服务于本演练的参数解析器。
    parser = argparse.ArgumentParser(
        description="验证两个 Agent 实例通过共享 PostgreSQL 完成故障切换",
    )
    # 默认地址只允许访问当前电脑，不向局域网发送开发 Token。
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Nginx 统一入口地址",
    )
    # 返回由 argparse 完成类型和必填规则处理的结果。
    return parser.parse_args()


def _docker_executable() -> str:
    """定位 docker.exe，并在找不到时给出适合 PyCharm 的明确错误。"""

    # PATH 可用时优先采用系统解析结果，兼容 PowerShell、CI 和非默认安装位置。
    discovered = shutil.which("docker")
    # 非空路径说明当前进程可以直接启动 Docker CLI。
    if discovered is not None:
        return discovered
    # PyCharm 有时没有继承 Docker Desktop 写入的 PATH，因此尝试官方常见用户安装目录。
    local_app_data = os.getenv("LOCALAPPDATA")
    # 环境变量缺失时不能安全猜测其他用户目录。
    if local_app_data:
        # Path 运算由 PROJECT_ROOT 所使用的 pathlib 类型完成，避免手工拼接分隔符。
        candidate = (
            Path(local_app_data)
            / "Programs"
            / "DockerDesktop"
            / "resources"
            / "bin"
            / "docker.exe"
        )
        # 文件真实存在才返回，避免把后续错误变成模糊的系统找不到文件。
        if candidate.is_file():
            return str(candidate)
    # 错误消息不包含环境变量内容，只说明用户需要启动/配置的工具。
    raise RuntimeError("未找到 docker 命令；请先启动 Docker Desktop，再从 PyCharm 运行")


def _run_compose(*arguments: str) -> None:
    """在项目根目录执行一条受控 Compose 命令。"""

    # 命令只由本文件中的固定参数组成，不接收外部 shell 片段。
    command = [_docker_executable(), "compose", *arguments]
    # shell=False 禁止 PowerShell/cmd 再解释参数；check=True 让失败立即阻止错误演练结论。
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    # 非零退出时只返回末尾有限日志，防止异常输出无限淹没 PyCharm 控制台。
    if completed.returncode != 0:
        # stderr 通常包含 Docker 的直接原因；为空时退回 stdout。
        diagnostic = (completed.stderr or completed.stdout).strip()
        # 只保留最后 1200 字符，Compose 命令自身不含 JWT 或数据库密码。
        raise RuntimeError(f"Docker Compose 操作失败：{diagnostic[-1200:]}")


def _request_json(
    *,
    method: str,
    url: str,
    token: str | None = None,
    body: dict[str, object] | None = None,
    timeout_seconds: float = 30.0,
) -> tuple[int, dict[str, object], dict[str, str]]:
    """返回 HTTP 状态、JSON 对象和小写响应头，供实例归属断言复用。"""

    # 没有正文时传 None，GET 不会被 urllib 自动转换成 POST。
    encoded_body = None if body is None else json.dumps(body).encode("utf-8")
    # 所有请求都声明接收 JSON；只有存在正文时才设置请求 Content-Type。
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    # Bearer Token 只进入当前 Request 对象，不会出现在报告和打印内容中。
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    # URL 由固定 base_url 与固定 API 路径组成。
    request = Request(url=url, data=encoded_body, headers=headers, method=method)
    try:
        # 超时避免网关或数据库异常时脚本永久等待。
        with urlopen(request, timeout=timeout_seconds) as response:
            # FastAPI 成功响应必须是 UTF-8 JSON 对象。
            payload = json.loads(response.read().decode("utf-8"))
            # 将 Message 风格响应头转为普通小写字典，便于跨平台稳定读取。
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            # 返回前再次检查顶层契约，避免后续对非对象调用 get。
            if not isinstance(payload, dict):
                raise ValueError("API 响应顶层不是 JSON 对象")
            return int(response.status), payload, response_headers
    except HTTPError as error:
        # 业务 4xx 也可能携带有用的有限 JSON，统一转换而不打印原始 Header。
        raw_error_body = error.read().decode("utf-8", errors="replace")
        try:
            # FastAPI 4xx 通常仍是 JSON，可以保留 detail 等有限字段供业务断言。
            payload = json.loads(raw_error_body)
        except json.JSONDecodeError:
            # Nginx 在切换窗口可能返回 HTML 502/503/504；只保留状态类别，不保留页面正文。
            payload = {"gateway_error": f"http_{error.code}"}
        # 保持与成功路径相同的顶层对象契约。
        if not isinstance(payload, dict):
            raise ValueError("API 错误响应顶层不是 JSON 对象") from error
        # 错误响应头同样可能包含实际 Agent 实例标识。
        response_headers = {key.lower(): value for key, value in error.headers.items()}
        return int(error.code), payload, response_headers


def _wait_for_instance(base_url: str, expected_instance: str, timeout_seconds: int = 60) -> None:
    """等待网关只把健康请求交给指定实例。"""

    # monotonic 不受 Windows 系统时间手工调整影响，适合计算超时。
    deadline = time.monotonic() + timeout_seconds
    # 连续成功计数防止“第一次请求先撞到旧实例、重试后落到新实例”被误判为切换完成。
    consecutive_successes = 0
    # 故障切换通常只需数秒；有限循环允许 Nginx DNS 和健康状态更新。
    while time.monotonic() < deadline:
        try:
            # /health 不访问业务正文，适合安全判断当前请求落点。
            status, payload, headers = _request_json(
                method="GET",
                url=f"{base_url}/health",
                timeout_seconds=3,
            )
            # 同时核对状态、正文实例名、实例 Header 与网关 Header，防止绕过入口。
            if (
                status == 200
                and payload.get("instance_id") == expected_instance
                and headers.get("x-serviceops-instance") == expected_instance
                and headers.get("x-serviceops-gateway") == "nginx"
            ):
                # 连续四秒都由目标实例返回，说明 Docker DNS 的旧地址已经基本排空。
                consecutive_successes += 1
                if consecutive_successes >= 4:
                    return
            else:
                # 任何非目标响应都会重新计算连续窗口。
                consecutive_successes = 0
        except (URLError, TimeoutError, ConnectionError):
            # 切换窗口的短暂连接失败属于预期，下一秒继续探测。
            consecutive_successes = 0
        # 一秒间隔不会给本机服务制造无意义高频流量。
        time.sleep(1)
    # 超时意味着目标实例或网关未恢复，不能继续声称跨实例验证成功。
    raise RuntimeError(f"等待 {expected_instance} 接管网关流量超时")


def _observe_both_instances(base_url: str, attempts: int = 12) -> set[str]:
    """通过多次 /health 收集网关实际选择过的实例名。"""

    # set 自动去重，最终必须同时包含 agent-a 和 agent-b。
    observed: set[str] = set()
    # 十二次足以覆盖两个后端，也能容忍网关刚恢复时的一次失败重试。
    for _ in range(attempts):
        status, payload, headers = _request_json(
            method="GET",
            url=f"{base_url}/health",
            timeout_seconds=5,
        )
        # 每次请求都必须经过 Nginx 并取得健康响应。
        if status != 200 or headers.get("x-serviceops-gateway") != "nginx":
            raise RuntimeError("健康请求未通过 Nginx 统一入口")
        # 只接受预期的低敏实例编号，其他值代表配置漂移。
        instance_id = str(payload.get("instance_id", ""))
        if instance_id not in {"agent-a", "agent-b"}:
            raise RuntimeError("健康响应包含未知 Agent 实例")
        # 记录本次实际处理者。
        observed.add(instance_id)
        # 已经看到两只实例即可提前结束，减少无意义请求。
        if observed == {"agent-a", "agent-b"}:
            break
    # 返回集合由 main 输出有限结论。
    return observed


def _build_tokens() -> tuple[str, str, str]:
    """为当前演练签发普通用户、审批人和审计员三枚短期 Token。"""

    # 读取项目根目录 .env；默认开发 JWT 参数与 Compose 容器一致。
    settings = get_settings()
    # production 必须由真实身份提供者签发，禁止本地自签演练 Token。
    if settings.environment == "production":
        raise RuntimeError("production 环境禁止运行本地故障切换脚本")
    # user-001 拥有种子订单 SO100002，并且只拥有普通对话权限。
    customer_token = create_access_token(
        settings=settings,
        subject="user-001",
        roles={Role.CUSTOMER},
    )
    # 审批人只能执行 return:approve，不与普通用户身份混用。
    reviewer_token = create_access_token(
        settings=settings,
        subject="step17-reviewer",
        roles={Role.RETURN_REVIEWER},
    )
    # 审计员只拥有 audit:read，用于最终独立验证哈希链。
    auditor_token = create_access_token(
        settings=settings,
        subject="step17-auditor",
        roles={Role.AUDITOR},
    )
    # Token 仅返回到 main 的局部变量。
    return customer_token, reviewer_token, auditor_token


def main() -> int:
    """执行完整故障切换演练，并确保退出前恢复两个 Agent。"""

    # 规范化地址，避免后续固定路径前出现双斜杠。
    base_url = str(_parse_arguments().base_url).rstrip("/")
    # 只允许本机回环默认值或用户明确指定的测试入口；生产限制由 Token 工厂再次检查。
    customer_token, reviewer_token, auditor_token = _build_tokens()
    # 记录演练开始时间，并据此生成全局唯一的幂等键。
    started_at = datetime.now(UTC)
    idempotency_key = f"step17-{started_at.strftime('%Y%m%d%H%M%S%f')}"
    # 先定义结果变量，便于 finally 恢复服务后再生成报告。
    thread_id = ""
    return_request_id = ""
    try:
        # 保证网关、两只 Agent 和依赖已经启动；不会重建或删除数据卷。
        _run_compose(
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "120",
            "agent-a",
            "agent-b",
            "gateway",
        )
        # 第一项证明 Nginx 默认轮询确实能到达两个后端。
        initially_observed = _observe_both_instances(base_url)
        if initially_observed != {"agent-a", "agent-b"}:
            raise RuntimeError("网关未观察到两只 Agent 实例")

        # 暂停 B 后，所有外部请求只能由 A 处理；stop 不删除容器或任何数据。
        _run_compose("stop", "agent-b")
        # Nginx 的 Docker DNS 缓存有效期为 5 秒，等待旧 B 地址从 upstream 中移除。
        time.sleep(7)
        _wait_for_instance(base_url, "agent-a")
        # A 创建待审批线程，LangGraph 必须在写工具之前 interrupt。
        chat_status, chat_payload, chat_headers = _request_json(
            method="POST",
            url=f"{base_url}/api/v1/chat",
            token=customer_token,
            body={
                "message": "为订单 SO100002 申请退货，原因：第十七步跨实例恢复验证",
                "idempotency_key": idempotency_key,
            },
        )
        # 同时验证 HTTP 结果、暂停状态和实际处理实例。
        if (
            chat_status != 200
            or chat_payload.get("execution_status") != "approval_required"
            or chat_headers.get("x-serviceops-instance") != "agent-a"
        ):
            raise RuntimeError("agent-a 未正确创建等待审批线程")
        # thread_id 是 B 从 PostgreSQL 找回 Checkpoint 的唯一主键。
        thread_id = str(chat_payload["thread_id"])

        # 先把 B 启动并等到健康，再停 A，避免演练产生不必要的长时间无服务窗口。
        _run_compose(
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "120",
            "agent-b",
        )
        _run_compose("stop", "agent-a")
        # 同样等待旧 A 地址离开 upstream；审批 POST 不应依赖网关重放来保证正确性。
        time.sleep(7)
        _wait_for_instance(base_url, "agent-b")
        # B 收到与 A 创建时相同的 thread_id；它只能从共享 PostgreSQL 恢复中断状态。
        approval_status, approval_payload, approval_headers = _request_json(
            method="POST",
            url=f"{base_url}/api/v1/approvals/{thread_id}",
            token=reviewer_token,
            body={
                "approved": True,
                "comment": "由 agent-b 恢复 agent-a 创建的线程并批准",
            },
        )
        # completed + RR 编号证明 B 不仅读到状态，还完成了受控写工具和事务 Outbox。
        candidate_return_request_id = approval_payload.get("return_request_id")
        if (
            approval_status != 200
            or approval_payload.get("return_workflow_status") != "completed"
            or approval_headers.get("x-serviceops-instance") != "agent-b"
            or not isinstance(candidate_return_request_id, str)
            or not candidate_return_request_id.startswith("RR-")
        ):
            raise RuntimeError("agent-b 未能恢复并完成 agent-a 创建的线程")
        # 保存已经通过格式检查的业务编号。
        return_request_id = candidate_return_request_id

        # 审计员仍通过 B 读取两节点证据链，验证 Outbox 投递和哈希完整性。
        audit_status, audit_payload, audit_headers = _request_json(
            method="GET",
            url=f"{base_url}/api/v1/audit/approvals/{thread_id}",
            token=auditor_token,
        )
        # 事件数组必须恰好包含审批决定与完成事实，并通过服务端重新计算。
        audit_events = audit_payload.get("events")
        if (
            audit_status != 200
            or audit_payload.get("chain_valid") is not True
            or audit_headers.get("x-serviceops-instance") != "agent-b"
            or not isinstance(audit_events, list)
            or len(audit_events) != 2
        ):
            raise RuntimeError("跨实例完成后的审批审计链验证失败")
    except (RuntimeError, URLError, TimeoutError, ConnectionError, KeyError, ValueError) as error:
        # 输出有限异常类型和安全短消息；Token 从不进入异常文本。
        print(f"FAIL 第17步故障切换演练：{type(error).__name__}: {error}")
        return_code = 1
    else:
        # 主流程无异常时先记成功，finally 仍会负责恢复 A/B。
        return_code = 0
    finally:
        try:
            # 无论中途成功或失败，都恢复两只 Agent 和统一入口，方便后续继续学习。
            _run_compose(
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                "120",
                "agent-a",
                "agent-b",
                "gateway",
            )
        except RuntimeError as restore_error:
            # 恢复失败会覆盖原成功状态，因为项目不能以缺少实例的状态宣告 PASS。
            print(f"FAIL 恢复双实例服务：{restore_error}")
            return_code = 1

    # 只有主流程和恢复都成功才生成报告并输出 PASS。
    if return_code == 0:
        # 恢复后再次观察两只实例，证明脚本没有把项目留在单实例状态。
        restored_observed = _observe_both_instances(base_url)
        if restored_observed != {"agent-a", "agent-b"}:
            print("FAIL 演练完成后未观察到两只 Agent")
            return 1
        # 报告刻意不保存 JWT、退货原因、审批备注或数据库连接地址。
        report = {
            "started_at": started_at.isoformat(),
            "gateway": "nginx",
            "initial_instances": ["agent-a", "agent-b"],
            "thread_created_by": "agent-a",
            "thread_resumed_by": "agent-b",
            "thread_id": thread_id,
            "return_request_id": return_request_id,
            "audit_chain_valid": True,
            "restored_instances": sorted(restored_observed),
        }
        # 创建已被忽略的运行报告目录。
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        # 缩进 UTF-8 JSON 便于在 PyCharm 中阅读和面试演示。
        REPORT_PATH.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # 控制台只输出可截图的关键结论和低敏业务编号。
        print("PASS 第17步：Nginx 轮询、跨实例恢复、PostgreSQL 共享状态、审计链全部通过")
        print("创建线程实例：agent-a")
        print("恢复线程实例：agent-b")
        print(f"线程编号：{thread_id}")
        print(f"退货申请编号：{return_request_id}")
        print(f"报告：{REPORT_PATH}")
    # 返回标准退出码供 PyCharm、PowerShell 或 CI 判断结果。
    return return_code


# 只有直接运行本文件时才启停容器和访问 API；测试导入不会产生外部动作。
if __name__ == "__main__":
    raise SystemExit(main())

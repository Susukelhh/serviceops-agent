"""第十六步示例：通过真实 HTTP 验证 PostgreSQL 在 API 容器重建后仍保存数据。

推荐按顺序运行：

1. ``uv run python examples/16_postgres_docker_persistence.py --mode write``
2. ``docker compose up -d --force-recreate --wait agent-a agent-b gateway``
3. ``uv run python examples/16_postgres_docker_persistence.py --mode verify``

第一条会创建一条经过人工批准的退货流程；第二条只重建两只 API 与网关，不删除数据库容器
和数据卷；第三条验证 LangGraph 终态与审批审计链仍可读取。脚本绝不把 JWT 写入磁盘。
"""

# argparse 让学习者明确选择“先写入”或“重建后验证”阶段。
import argparse

# json 编码 HTTP 请求体，并保存不含密钥的验证报告。
import json

# datetime/UTC 给本地验证报告增加无歧义时间。
from datetime import UTC, datetime

# HTTPError 允许脚本检查预期的 409；urlopen 使用 Python 标准库访问本机 API。
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# resolve_project_path 保证报告位置不受 PyCharm Working directory 影响。
from serviceops_agent.config.paths import resolve_project_path

# Pydantic 配置提供与本机开发 API 一致的 JWT issuer、audience 和签名密钥。
from serviceops_agent.config.settings import get_settings

# 本地签发函数只生成短期演示 Token，不会打印签名密钥。
from serviceops_agent.security.jwt_auth import create_access_token

# 三个角色分别只能发起对话、审批和读取审计链，展示职责分离。
from serviceops_agent.security.models import Role

# 报告只保存线程号和业务申请号，故意不保存任何 Bearer Token。
REPORT_PATH = resolve_project_path("data/runtime/postgres_step16_probe.json")


def _parse_arguments() -> argparse.Namespace:
    """解析阶段和本机 API 地址。"""

    # 创建脚本专用参数解析器。
    parser = argparse.ArgumentParser(
        description="验证 PostgreSQL 在 API 容器重建后仍保存 Agent 数据",
    )
    # write 负责创建数据；verify 负责重建后读取同一数据。
    parser.add_argument(
        "--mode",
        choices=("write", "verify"),
        required=True,
        help="write=写入探针数据，verify=验证容器重建后的持久化数据",
    )
    # 默认只访问 Windows 本机回环地址，不向局域网发送开发 Token。
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="ServiceOps Agent API 根地址",
    )
    # 返回经过 argparse 校验的参数。
    return parser.parse_args()


def _request_json(
    *,
    method: str,
    url: str,
    token: str | None = None,
    body: dict[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    """发送 JSON 请求，并把成功或 HTTP 错误都转换为有限状态与字典。"""

    # 没有请求体时传 None；有请求体时使用 UTF-8 JSON 字节。
    request_body = None if body is None else json.dumps(body).encode("utf-8")
    # 所有请求都声明 JSON 响应；只有受保护接口才添加短期 Bearer Token。
    headers = {"Accept": "application/json"}
    # POST JSON 需要明确 Content-Type，FastAPI 才按 Schema 解析请求体。
    if body is not None:
        headers["Content-Type"] = "application/json"
    # Token 只存在于当前函数内存和 HTTP Header，不进入报告或控制台。
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    # 构造标准库请求对象。
    request = Request(
        url=url,
        data=request_body,
        headers=headers,
        method=method,
    )
    try:
        # 三十秒上限防止服务异常时脚本永久等待。
        with urlopen(request, timeout=30) as response:
            # 读取并解析 FastAPI JSON 响应。
            payload = json.loads(response.read().decode("utf-8"))
            # 接口契约保证顶层为对象；异常结构直接让测试失败。
            if not isinstance(payload, dict):
                raise ValueError("API 响应顶层不是 JSON 对象")
            # 返回真实 HTTP 状态与字典载荷。
            return int(response.status), payload
    except HTTPError as error:
        # 409 等业务状态也带 JSON 正文，需要保留给 verify 做明确断言。
        payload = json.loads(error.read().decode("utf-8"))
        # 错误响应同样必须是对象。
        if not isinstance(payload, dict):
            raise ValueError("API 错误响应顶层不是 JSON 对象") from error
        # 返回而不是吞掉状态码。
        return int(error.code), payload


def _build_tokens() -> tuple[str, str, str]:
    """生成只存在于当前进程内存的普通用户、审批人和审计员 Token。"""

    # 读取本地开发配置；脚本和 Compose 默认使用同一套开发 JWT 参数。
    settings = get_settings()
    # 生产环境禁止本地自签 Token，必须接入真实身份平台。
    if settings.environment == "production":
        raise RuntimeError("production 环境禁止运行本地持久化验证脚本")
    # user-001 是种子订单 SO100002 的真实归属用户。
    customer_token = create_access_token(
        settings=settings,
        subject="user-001",
        roles={Role.CUSTOMER},
    )
    # 审批人使用独立主体与 return:approve 权限。
    reviewer_token = create_access_token(
        settings=settings,
        subject="postgres-step16-reviewer",
        roles={Role.RETURN_REVIEWER},
    )
    # 审计员只能读取最小审计证据，不能发起或批准退货。
    auditor_token = create_access_token(
        settings=settings,
        subject="postgres-step16-auditor",
        roles={Role.AUDITOR},
    )
    # 三枚 Token 不打印、不保存，只返回给当前脚本调用链。
    return customer_token, reviewer_token, auditor_token


def _write_probe(base_url: str) -> int:
    """创建一条等待审批线程，批准它，并保存低敏验证标识。"""

    # 每次执行使用当前微秒时间生成新的合法幂等键，避免覆盖以前的演示记录。
    timestamp = datetime.now(UTC)
    # 只保留数字，符合 API 幂等键允许字符范围。
    idempotency_key = f"postgres-step16-{timestamp.strftime('%Y%m%d%H%M%S%f')}"
    # 生成当前进程短期凭证；write 阶段不需要审计员 Token。
    customer_token, reviewer_token, _ = _build_tokens()
    # 发起一条本人已签收订单的退货申请，图应先暂停而不是立即写数据库。
    chat_status, chat_payload = _request_json(
        method="POST",
        url=f"{base_url}/api/v1/chat",
        token=customer_token,
        body={
            "message": "为订单 SO100002 申请退货，原因：第十六步 PostgreSQL 持久化验证",
            "idempotency_key": idempotency_key,
        },
    )
    # 首次请求必须成功进入人工审批等待态。
    if chat_status != 200 or chat_payload.get("execution_status") != "approval_required":
        print(f"FAIL 写入阶段未进入审批等待态：status={chat_status}")
        return 1
    # thread_id 是 LangGraph Checkpointer 的跨重启恢复主键。
    thread_id = str(chat_payload["thread_id"])
    # 审批人明确批准后，图才允许执行退货写工具。
    approval_status, approval_payload = _request_json(
        method="POST",
        url=f"{base_url}/api/v1/approvals/{thread_id}",
        token=reviewer_token,
        body={
            "approved": True,
            "comment": "批准第十六步 PostgreSQL 持久化验证",
        },
    )
    # 成功结果必须同时具有 completed 流程状态和真实 RR 编号。
    return_request_id = approval_payload.get("return_request_id")
    if (
        approval_status != 200
        or approval_payload.get("return_workflow_status") != "completed"
        or not isinstance(return_request_id, str)
        or not return_request_id.startswith("RR-")
    ):
        print(f"FAIL 审批阶段未创建退货记录：status={approval_status}")
        return 1
    # 创建父目录；该目录已被 Docker 构建上下文和 Git 忽略，不会进入镜像或版本库。
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 报告只包含验证所需低敏标识，不保存申请原因、备注、用户 Token 或数据库密码。
    report = {
        "thread_id": thread_id,
        "return_request_id": return_request_id,
        "idempotency_key": idempotency_key,
        "written_at": timestamp.isoformat(),
    }
    # 格式化 JSON 便于学习者在 PyCharm 中直接查看。
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # 输出下一条明确操作，不打印任何凭证。
    print("PASS 写入：退货记录、Outbox、审计链和 LangGraph 终态已写入 PostgreSQL")
    print(f"线程编号：{thread_id}")
    print(f"退货申请编号：{return_request_id}")
    print("下一步：重建 API 容器后，以 --mode verify 再运行本脚本")
    return 0


def _verify_probe(base_url: str) -> int:
    """验证 API 重建后仍能读到原审计链，并识别线程已经完成。"""

    # 没有 write 报告就无法知道应验证哪一个历史线程。
    if not REPORT_PATH.exists():
        print("FAIL 未找到验证报告，请先运行 --mode write")
        return 1
    # 读取上一步保存的低敏 JSON。
    raw_report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    # 报告顶层必须是对象，防止损坏文件产生模糊错误。
    if not isinstance(raw_report, dict):
        print("FAIL 验证报告格式错误")
        return 1
    # 取出两个稳定业务标识。
    thread_id = str(raw_report.get("thread_id", ""))
    expected_return_request_id = str(raw_report.get("return_request_id", ""))
    # verify 需要审批 Token 测试终态，也需要审计 Token读取哈希链。
    _, reviewer_token, auditor_token = _build_tokens()

    # readiness 必须明确显示当前容器确实运行 PostgreSQL，而不是悄悄回退 SQLite。
    ready_status, ready_payload = _request_json(
        method="GET",
        url=f"{base_url}/ready",
    )
    # 四项依赖健康且后端名称正确才继续。
    if (
        ready_status != 200
        or ready_payload.get("status") != "ready"
        or ready_payload.get("persistence_backend") != "postgres"
    ):
        print("FAIL 当前 API 未以健康 PostgreSQL 模式运行")
        return 1

    # 重建后的新 API 进程应从 PostgreSQL 读取原线程，并拒绝再次审批已完成流程。
    repeated_status, _ = _request_json(
        method="POST",
        url=f"{base_url}/api/v1/approvals/{thread_id}",
        token=reviewer_token,
        body={
            "approved": True,
            "comment": "容器重建后的重复审批应被拒绝",
        },
    )
    # 409 证明 Checkpointer 找到了线程，并确认它已经不是待审批状态。
    if repeated_status != 409:
        print(f"FAIL LangGraph 终态未正确恢复：status={repeated_status}")
        return 1

    # 独立审计接口应返回重建前创建的两节点哈希链。
    audit_status, audit_payload = _request_json(
        method="GET",
        url=f"{base_url}/api/v1/audit/approvals/{thread_id}",
        token=auditor_token,
    )
    # 读取失败或哈希链重算失败都说明业务持久化没有通过。
    if audit_status != 200 or audit_payload.get("chain_valid") is not True:
        print(f"FAIL 审批审计链未恢复：status={audit_status}")
        return 1
    # events 顶层必须为列表。
    events = audit_payload.get("events")
    if not isinstance(events, list) or len(events) != 2:
        print("FAIL 审批审计链事件数量不是预期的 2")
        return 1
    # 第二条完成事件必须仍绑定 write 阶段保存的退货申请编号。
    completed_event = events[1]
    if (
        not isinstance(completed_event, dict)
        or completed_event.get("return_request_id") != expected_return_request_id
    ):
        print("FAIL 审计完成事件与原退货申请编号不一致")
        return 1
    # 到这里同时证明新进程恢复了 LangGraph 终态和业务审计记录。
    print("PASS 重建验证：新 API 容器仍能读取原 LangGraph 终态和有效审批哈希链")
    print(f"线程编号：{thread_id}")
    print(f"退货申请编号：{expected_return_request_id}")
    return 0


def main() -> int:
    """根据参数执行写入或重建后验证，并把网络故障转换为简短提示。"""

    # 解析命令行并去除根地址末尾斜杠，避免拼出双斜杠路径。
    arguments = _parse_arguments()
    base_url = str(arguments.base_url).rstrip("/")
    try:
        # write 创建探针；verify 读取同一报告进行恢复验证。
        if arguments.mode == "write":
            return _write_probe(base_url)
        return _verify_probe(base_url)
    except (URLError, TimeoutError, ConnectionError) as error:
        # 网络异常只打印类型，不泄漏请求 Header、Token 或底层连接正文。
        print(f"FAIL 无法访问本机 API：cause_type={type(error).__name__}")
        return 1
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        # 数据结构错误同样只给有限类型，完整对象可能包含业务数据。
        print(f"FAIL 验证数据格式异常：cause_type={type(error).__name__}")
        return 1


# 只有直接运行脚本时才发送真实 HTTP 请求。
if __name__ == "__main__":
    # SystemExit 把 PASS/FAIL 转成终端和 CI 可以识别的退出码。
    raise SystemExit(main())

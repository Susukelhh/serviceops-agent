"""第二十步示例：验证控制台、真实工具和第20.2步全宽 Checkpoint 教学工作台。

运行前先执行：

    docker compose up --detach --build --wait --wait-timeout 120

然后在 PyCharm 直接运行本文件，或执行：

    uv run python examples/20_agent_console_smoke_test.py

脚本只查询 user-001 自己的 SO100001，不创建退货、不调用千问，也不会打印 JWT。它验证根路径跳转、
HTML/CSS/JavaScript、安全 Header、readiness、真实订单工具和脱敏状态历史，并写出有限报告。
"""

# argparse 允许用户验证非默认本机地址，默认值可直接运行。
import argparse

# json 编码 POST 请求并写出不含 Token 的结构化报告。
import json

# UTC 时间记录报告生成时刻，避免本地时区歧义。
from datetime import UTC, datetime

# HTTPError/URLError 分别表达 HTTP 失败和连接失败。
from urllib.error import HTTPError, URLError

# Request/urlopen 使用标准库访问 Nginx，不增加新的运行依赖。
from urllib.request import Request, urlopen

# SecretStr 明确 JWT 密钥不会出现在 Settings repr。
from pydantic import SecretStr

# 固定报告路径不依赖 PyCharm Working directory。
from serviceops_agent.config.paths import resolve_project_path

# 本地容器使用仓库内开发密钥；production 配置会在 Settings 中拒绝该值。
from serviceops_agent.config.settings import (
    DEVELOPMENT_ONLY_JWT_SECRET,
    Settings,
)

# create_access_token 复用 API 验证端相同的 JWT Claims 与 HS256 签名规则。
from serviceops_agent.security.jwt_auth import create_access_token

# CUSTOMER/DEVELOPER 分别只获得 agent:chat 与 debug:read，不能互相替代。
from serviceops_agent.security.models import Role

# 报告只保存页面与业务结果摘要，不保存 Token、订单正文或用户输入。
REPORT_PATH = resolve_project_path("data/runtime/agent_console_step20_report.json")


def _parse_arguments() -> argparse.Namespace:
    """读取本机 Nginx 统一入口地址。"""

    # 创建本示例专用解析器。
    parser = argparse.ArgumentParser(
        description="验证 ServiceOps Agent 可视化控制台与真实工具调用",
    )
    # 默认只访问 Windows 回环地址，不扫描其他端口或机器。
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="ServiceOps Nginx 统一入口",
    )
    # 返回 argparse 已校验的 Namespace。
    return parser.parse_args()


def _request(
    *,
    url: str,
    method: str = "GET",
    token: str | None = None,
    body: dict[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str], str]:
    """发送一个有限 HTTP 请求并返回状态、正文、低敏 Header 和最终 URL。"""

    # 默认只声明接受 JSON/HTML；POST 时再加入 Content-Type。
    headers = {"Accept": "application/json, text/html;q=0.9, */*;q=0.8"}
    # 请求体为空时不发送任何字节。
    encoded_body = None
    if body is not None:
        # ensure_ascii=False 保持中文 JSON；服务端按 UTF-8 解码。
        encoded_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        # FastAPI 只有看到 application/json 才按 Pydantic Schema 解析。
        headers["Content-Type"] = "application/json"
    # Token 只进入 Authorization Header，绝不进入 URL、body、报告或控制台输出。
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    # Request 使用调用方固定 URL 和方法。
    request = Request(
        url=url,
        data=encoded_body,
        headers=headers,
        method=method,
    )
    # 十秒超时足以覆盖离线确定性订单查询，又不会在 Docker 未启动时无限等待。
    with urlopen(request, timeout=10) as response:
        # 只返回全部 Header 的小写映射；后续白名单读取，不写入报告全文。
        response_headers = {key.lower(): value for key, value in response.headers.items()}
        # geturl 能证明根路径经过 307 最终到达 /console/。
        final_url = response.geturl()
        # 读取有限本地响应；控制台静态资源和订单响应都远低于 1 MiB。
        content = response.read(1_048_577)
        # 超过上限代表资源异常增长，立即失败。
        if len(content) > 1_048_576:
            raise RuntimeError("控制台资源超过 1 MiB 安全验证上限")
        # 返回真实状态、正文、Header 和最终 URL。
        return response.status, content, response_headers, final_url


def _build_local_token(*, subject: str, role: Role) -> str:
    """生成只匹配当前 Compose 默认开发配置的一枚有限角色 Token。"""

    # 初始化参数显式覆盖 .env 中可能存在的运行后端，避免读取或使用模型密钥。
    settings = Settings(
        # 控制台只面向本机 development Compose。
        environment="development",
        # JWT 签名使用 compose 未覆盖时的已知开发值。
        jwt_secret_key=SecretStr(DEVELOPMENT_ONLY_JWT_SECRET),
        # Token 签发不需要数据库，显式使用 memory 避免 DSN 组合校验。
        persistence_backend="memory",
    )
    # subject 和 role 由脚本固定调用点提供，不接受命令行输入或远程数据。
    return create_access_token(
        settings=settings,
        subject=subject,
        roles={role},
    )


def main() -> int:
    """依次验证页面资源、安全策略、readiness 和真实订单工具。"""

    # 解析入口地址并去掉末尾斜杠，避免后续出现双斜杠。
    arguments = _parse_arguments()
    base_url = str(arguments.base_url).rstrip("/")
    # 开始提示明确不会执行付费模型或写业务数据。
    print("=== ServiceOps Agent 第20步：控制台真实冒烟 ===")
    print("说明：只读取页面、健康状态和本人订单，不调用千问、不创建退货。")
    try:
        # urllib 默认跟随根路径 307，最终地址应是 /console/。
        console_status, console_body, console_headers, final_url = _request(
            url=f"{base_url}/",
        )
        # HTML 必须成功且确实进入规范地址。
        if console_status != 200 or not final_url.endswith("/console/"):
            raise RuntimeError("根路径没有正确进入 /console/")
        # 以 UTF-8 解码产品页面。
        console_text = console_body.decode("utf-8")
        # 两个标记共同排除旧 Swagger、404 或空页面。
        if (
            "看见 Agent 如何做决定" not in console_text
            or "LANGGRAPH TRACE" not in console_text
            or "CHECKPOINT PLAYBACK" not in console_text
            or "Agent 执行回放工作台" not in console_text
            or 'id="debug-focus-button"' not in console_text
        ):
            raise RuntimeError("控制台 HTML 缺少产品标记")
        # HTML 必须携带严格 CSP 且禁止 iframe。
        content_security_policy = console_headers.get("content-security-policy", "")
        if "script-src 'self'" not in content_security_policy:
            raise RuntimeError("控制台缺少同源脚本 CSP")
        if "frame-ancestors 'none'" not in content_security_policy:
            raise RuntimeError("控制台缺少 iframe 禁止策略")

        # CSS 必须从同源静态资源成功读取。
        css_status, css_body, _, _ = _request(
            url=f"{base_url}/console/assets/console.css",
        )
        if (
            css_status != 200
            or b".content-grid" not in css_body
            or b"body.debug-focus-mode" not in css_body
            or b".debug-stage" not in css_body
        ):
            raise RuntimeError("控制台 CSS 未进入最终镜像")
        # JavaScript 必须包含真实 chat 入口。
        js_status, js_body, _, _ = _request(
            url=f"{base_url}/console/assets/console.js",
        )
        if (
            js_status != 200
            or b"/api/v1/chat" not in js_body
            or b"/api/v1/debug/threads/" not in js_body
            or b"setDebugFocusMode" not in js_body
        ):
            raise RuntimeError("控制台 JavaScript 未进入最终镜像")

        # readiness 响应证明页面旁边显示的四项状态来自真实持久化依赖。
        ready_status, ready_body, _, _ = _request(url=f"{base_url}/ready")
        readiness = json.loads(ready_body.decode("utf-8"))
        checks = readiness.get("checks", {})
        if ready_status != 200 or readiness.get("status") != "ready":
            raise RuntimeError("Agent readiness 不是 ready")
        if len(checks) != 4 or not all(
            isinstance(item, dict) and item.get("status") == "ready" for item in checks.values()
        ):
            raise RuntimeError("readiness 四个持久化边界没有全部就绪")

        # Token 仅保存在当前 Python 变量，不打印或写入报告。
        customer_token = _build_local_token(
            subject="user-001",
            role=Role.CUSTOMER,
        )
        # 使用和网页第一个场景完全相同的本人订单查询。
        chat_status, chat_body, chat_headers, _ = _request(
            url=f"{base_url}/api/v1/chat",
            method="POST",
            token=customer_token,
            body={"message": "查询订单 SO100001 到哪了"},
        )
        chat_payload = json.loads(chat_body.decode("utf-8"))
        # 真实工具路径必须完成并查询固定本人订单。
        if chat_status != 200 or chat_payload.get("intent") != "order_status":
            raise RuntimeError("控制台依赖的订单 Agent 场景失败")
        if chat_payload.get("tool_name") != "get_order_status":
            raise RuntimeError("订单场景没有执行 get_order_status")
        if chat_payload.get("queried_order_ids") != ["SO100001"]:
            raise RuntimeError("订单工具轨迹与预期不一致")
        # Nginx 和 Agent 实例响应头用于控制台顶部摘要。
        instance_id = chat_headers.get("x-serviceops-instance", "")
        gateway = chat_headers.get("x-serviceops-gateway", "")
        if instance_id not in {"agent-a", "agent-b"} or gateway != "nginx":
            raise RuntimeError("控制台请求没有经过 Nginx 双实例拓扑")

        # developer Token 与 customer Token 职责分离，只读取后端脱敏的 StateSnapshot 历史。
        developer_token = _build_local_token(
            subject="developer-smoke-001",
            role=Role.DEVELOPER,
        )
        thread_id = str(chat_payload.get("thread_id", ""))
        if not thread_id:
            raise RuntimeError("订单响应缺少可读取的 thread_id")
        debug_status, debug_body, _, _ = _request(
            url=f"{base_url}/api/v1/debug/threads/{thread_id}",
            token=developer_token,
        )
        debug_payload = json.loads(debug_body.decode("utf-8"))
        # 顺序订单图应产生多个快照，并最终到达空 next 的 completed 状态。
        checkpoints = debug_payload.get("checkpoints", [])
        if debug_status != 200 or debug_payload.get("status") != "completed":
            raise RuntimeError("Checkpoint 教学接口没有返回 completed 线程")
        if not isinstance(checkpoints, list) or len(checkpoints) < 8:
            raise RuntimeError("Checkpoint 数量不足，无法证明真实节点级回放")
        # 至少一个快照必须明确记录 execute_order_tool 已经完成。
        executed_nodes = {
            str(node.get("name"))
            for checkpoint in checkpoints
            if isinstance(checkpoint, dict)
            for node in checkpoint.get("executed_nodes", [])
            if isinstance(node, dict)
        }
        if "execute_order_tool" not in executed_nodes:
            raise RuntimeError("教学回放中没有真实工具执行节点")
        # 防止未来修改不小心把内部身份或幂等上下文放进调试响应。
        debug_text = debug_body.decode("utf-8")
        forbidden_debug_fields = (
            "user_id",
            "idempotency_key",
            "reviewer_id",
            "token_jti",
            "fingerprint",
        )
        if any(field in debug_text for field in forbidden_debug_fields):
            raise RuntimeError("Checkpoint 教学响应出现敏感字段")
    except (HTTPError, URLError, TimeoutError, RuntimeError, ValueError) as error:
        # 只打印异常类型和已由本脚本构造的有限消息，不打印 Token 或请求 Header。
        print(f"FAIL 第20步：{type(error).__name__}：{error}")
        return 1

    # 构造不含 Token、用户原文和完整业务响应的有限报告。
    report = {
        "step": "20.2",
        "status": "pass",
        "generated_at": datetime.now(UTC).isoformat(),
        "console_url": f"{base_url}/console/",
        "console_html": "pass",
        "same_origin_assets": "pass",
        "content_security_policy": "pass",
        "readiness_checks": len(checks),
        "authenticated_order_tool": chat_payload["tool_name"],
        "debug_checkpoint_count": len(checkpoints),
        "debug_tool_node": "execute_order_tool",
        "hidden_reasoning_exposed": debug_payload.get("hidden_reasoning_exposed"),
        "instance_id": instance_id,
        "gateway": gateway,
        "token_persisted": False,
    }
    # 创建已经被 Git 忽略的运行报告目录。
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # UTF-8 与缩进便于 PyCharm 直接阅读。
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # PASS 只有页面、资源、安全、依赖和真实工具全部通过后输出。
    print("PASS 第20.2步：全宽控制台、真实订单工具与脱敏 Checkpoint 回放全部通过")
    print(f"Checkpoint：{len(checkpoints)} 个，包含 execute_order_tool")
    print(f"处理实例：{instance_id}（经 {gateway}）")
    print(f"打开页面：{base_url}/console/")
    print(f"运行报告：{REPORT_PATH}")
    return 0


# 导入示例不会发送网络请求；只有直接运行才执行冒烟。
if __name__ == "__main__":
    # SystemExit 把成功/失败传给 PyCharm 和命令行。
    raise SystemExit(main())

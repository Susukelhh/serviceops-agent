"""第十五步示例：验证已经启动的容器是否满足最小部署契约。

先在项目根目录启动容器：

    docker compose up --build --detach

再在 PyCharm 中直接运行本文件，或执行：

    uv run python examples/15_container_smoke_test.py

脚本只访问本机 health/readiness/API 认证边界，不调用千问，也不会消耗模型额度。
"""

# argparse 允许测试其他端口或远程测试环境，但默认只访问本机。
import argparse

# json 把 HTTP 响应解析为结构化数据，避免用脆弱字符串包含判断。
import json

# RemoteDisconnected 表达容器启动失败或重启时，服务端在返回 HTTP 状态前关闭连接。
from http.client import RemoteDisconnected

# Any 表达 JSON 字段值可能是字符串、字典、列表、数值或空值。
from typing import Any

# urllib.error 提供 HTTPError；401 也是本脚本需要验证的预期 HTTP 响应。
from urllib.error import HTTPError, URLError

# Request 构造带方法和 JSON 头的请求；urlopen 使用 Python 标准库完成调用。
from urllib.request import Request, urlopen


def _parse_arguments() -> argparse.Namespace:
    """读取被测服务地址。"""

    # description 会展示在 PyCharm Terminal 的 --help 输出中。
    parser = argparse.ArgumentParser(description="验证 ServiceOps Agent 容器部署契约")
    # rstrip 会在后续调用前去掉末尾斜杠，防止拼出双斜杠 URL。
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="已启动服务的根地址",
    )
    # 返回 argparse 生成的 Namespace，由 main 负责转成字符串。
    return parser.parse_args()


def _request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """发送一次短超时 HTTP 请求，并同时保留预期的非 2xx 状态码。"""

    # 只有存在 JSON 请求体时才编码；GET 使用 None 避免自动变成 POST。
    encoded_payload = None if payload is None else json.dumps(payload).encode("utf-8")
    # Content-Type 明确声明 JSON；故意不加入 Authorization 以验证 401 边界。
    request = Request(
        url=url,
        data=encoded_payload,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        # 五秒超时避免容器未启动时示例长期挂住。
        with urlopen(request, timeout=5) as response:
            # 响应体只在内存中解析，不写入运行目录。
            response_body = json.loads(response.read().decode("utf-8"))
            # status 和 JSON 字典组成稳定的测试输入。
            return int(response.status), dict(response_body)
    except HTTPError as error:
        # FastAPI 的预期 401 会走 HTTPError；仍解析其有限 JSON 错误响应。
        error_body = json.loads(error.read().decode("utf-8"))
        # 返回而不是重新抛出，交给 main 对预期状态做明确断言。
        return int(error.code), dict(error_body)


def main() -> int:
    """依次验证 liveness、readiness 与未认证拒绝，并返回标准退出码。"""

    # 去掉用户输入的尾随斜杠，确保后续固定路径拼接一致。
    base_url = str(_parse_arguments().base_url).rstrip("/")
    try:
        # 第一个探针只判断 Web 进程存活，不要求访问业务数据。
        health_status, health_body = _request_json(f"{base_url}/health")
        # 存活接口必须同时满足 HTTP 200 和受约束的 ok 状态。
        if health_status != 200 or health_body.get("status") != "ok":
            # 输出有限状态，不打印可能由代理追加的完整原始响应。
            print(f"FAIL liveness: http={health_status}, status={health_body.get('status')}")
            return 1
        # 第二个探针会真实读取 Checkpointer、业务库、Outbox 和审计库。
        ready_status, ready_body = _request_json(f"{base_url}/ready")
        # readiness 必须是 200/ready，不能把“端口已开”等同于“可以接收流量”。
        if ready_status != 200 or ready_body.get("status") != "ready":
            print(f"FAIL readiness: http={ready_status}, status={ready_body.get('status')}")
            return 1
        # checks 应是组件名到有限状态对象的字典。
        dependency_checks = ready_body.get("checks", {})
        # 四个关键依赖必须全部存在且处于 ready。
        expected_dependencies = {
            "checkpointer",
            "return_repository",
            "outbox_repository",
            "audit_repository",
        }
        # 只比较固定组件名和状态，不输出数据库异常或内部文件路径。
        if set(dependency_checks) != expected_dependencies or any(
            check.get("status") != "ready" for check in dependency_checks.values()
        ):
            print("FAIL readiness dependencies: expected four ready components")
            return 1
        # 最后故意不带 Token 请求业务接口，验证容器没有绕过 JWT 认证。
        auth_status, _ = _request_json(
            f"{base_url}/api/v1/chat",
            method="POST",
            payload={"message": "查询订单 SO100001"},
        )
        # 未认证请求只能得到 401，任何 2xx 都意味着部署安全回归。
        if auth_status != 401:
            print(f"FAIL authentication boundary: expected=401, actual={auth_status}")
            return 1
    except (URLError, TimeoutError, RemoteDisconnected, json.JSONDecodeError) as error:
        # 只输出异常类型，不泄漏代理响应体、Token 或内部堆栈。
        print(f"FAIL container connection: cause_type={type(error).__name__}")
        return 1

    # 三层契约全部通过后输出适合截图和面试演示的短结论。
    print("PASS: liveness=ok, readiness=ready(4/4), unauthenticated_chat=401")
    # 返回 0 让本地终端或 CI 将脚本识别为成功。
    return 0


# 只有直接运行本文件时执行探针；导入函数做测试不会产生网络请求。
if __name__ == "__main__":
    raise SystemExit(main())

"""第十八步示例：验证正常负载、突发保护和过载后的自动恢复。

运行前先启动 Docker Compose，然后直接在 PyCharm 运行本文件，或执行：

    uv run python examples/18_load_and_resilience_baseline.py

本脚本只查询 user-001 自己的 SO100001，不创建退货、不调用千问，也不会删除任何数据。
正常阶段以低于入口限速的节奏发送请求；突发阶段并发发送一批请求，预期一部分成功，
另一部分由 Nginx 429 或 Agent 容量 503 明确拒绝；等待三秒后必须恢复为 200。

这是一份当前电脑与当前离线配置的“容量基线”，不是生产压测结论。正式上线仍需在独立测试环境
使用真实模型延迟、代表性流量和专用压测工具重新测量。
"""

# argparse 允许调整演练地址和请求数量，默认值可直接在本机安全运行。
import argparse

# json 负责编码查询请求，并写出不含 Token 的结构化报告。
import json

# math.ceil 实现不依赖 NumPy 的 nearest-rank 百分位数。
import math

# time.perf_counter 测量单次端到端耗时；sleep 控制正常速率和恢复等待。
import time

# Counter 汇总 HTTP 状态码；ThreadPoolExecutor 制造受控的瞬时并发。
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

# dataclass 保存每次探针的有限结果，避免在多个阶段传递任意字典。
from dataclasses import dataclass

# datetime/UTC 为报告记录无歧义生成时间。
from datetime import UTC, datetime

# HTTPError 保留 429/503；URLError 表达连接层故障。
from urllib.error import HTTPError, URLError

# Request/urlopen 使用 Python 标准库访问本机网关，不引入新的压测依赖。
from urllib.request import Request, urlopen

# 项目路径函数保证 PyCharm 使用任意 Working directory 时仍写入固定报告位置。
from serviceops_agent.config.paths import resolve_project_path

# Settings 提供本地 JWT 参数和后端工位上限，报告不会保存 SecretStr 字段。
from serviceops_agent.config.settings import get_settings

# 本地 Token 工厂只在当前进程内存生成短期凭证。
from serviceops_agent.security.jwt_auth import create_access_token

# CUSTOMER 角色只有 agent:chat，不能审批、读审计或执行运维补偿。
from serviceops_agent.security.models import Role

# 运行报告目录已经被 Git 和 Docker 构建上下文忽略。
REPORT_PATH = resolve_project_path("data/runtime/load_resilience_step18_report.json")


@dataclass(frozen=True)
class ProbeResult:
    """一次请求允许进入报告的低敏结果。"""

    # status_code 为真实 HTTP 状态；连接失败时使用 0 表示没有收到 HTTP 响应。
    status_code: int
    # latency_ms 是从本机脚本到网关返回完整响应的毫秒耗时。
    latency_ms: float
    # instance_id 只读取 A/B 的低敏响应头；网关直接拒绝时通常为空。
    instance_id: str | None
    # overload_kind 区分 Nginx 入口限制和 Agent 后端工位限制。
    overload_kind: str | None
    # error_type 只保存异常类名，不保存可能含地址或请求内容的异常消息。
    error_type: str | None


def _parse_arguments() -> argparse.Namespace:
    """读取网关地址、正常样本数和突发样本数。"""

    # 创建本演练专用参数解析器。
    parser = argparse.ArgumentParser(
        description="建立 ServiceOps Agent 正常负载与突发保护基线",
    )
    # 默认只访问当前电脑上的 Nginx 统一入口。
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Nginx 统一入口地址",
    )
    # 十二条正常请求足以观察 A/B 与延迟，又不会长时间占用本机。
    parser.add_argument(
        "--steady-requests",
        type=int,
        default=12,
        help="正常阶段请求数，默认 12",
    )
    # 四十条并发请求会明显超过 5r/s + burst=10，用于确定性触发入口保护。
    parser.add_argument(
        "--burst-requests",
        type=int,
        default=40,
        help="突发阶段请求数，默认 40",
    )
    # 返回 argparse 已转换基础类型的结果，main 再检查合理范围。
    return parser.parse_args()


def _build_customer_token() -> str:
    """签发只允许查询订单的短期本地 Token。"""

    # 读取本机开发配置；不会把密钥写入报告或控制台。
    settings = get_settings()
    # production 必须由真实身份系统发 Token，禁止使用本地开发签发器做压测。
    if settings.environment == "production":
        raise RuntimeError("production 环境禁止运行本地容量基线脚本")
    # user-001 对 SO100001 具有真实种子数据归属关系。
    return create_access_token(
        settings=settings,
        subject="user-001",
        roles={Role.CUSTOMER},
    )


def _query_order_once(*, base_url: str, token: str) -> ProbeResult:
    """通过 Nginx 查询一次本人订单，并返回有限探针结果。"""

    # 请求正文固定为只读订单查询，不包含动态用户输入或写操作幂等键。
    encoded_body = json.dumps(
        {"message": "查询订单 SO100001 到哪了"},
        ensure_ascii=False,
    ).encode("utf-8")
    # Token 只放在当前内存 Request Header；Connection: close 让并发连接边界更明确。
    request = Request(
        url=f"{base_url}/api/v1/chat",
        data=encoded_body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Connection": "close",
        },
        method="POST",
    )
    # 使用单调高精度时钟测量端到端耗时。
    started_at = time.perf_counter()
    try:
        # 真实模型可能较慢，但当前 Compose 固定离线后端；30 秒仍能防止异常永久挂起。
        with urlopen(request, timeout=30) as response:
            # 成功响应必须是 JSON 对象，并符合本项目订单查询的基本业务契约。
            payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("成功响应顶层不是 JSON 对象")
            if payload.get("intent") != "order_status":
                raise ValueError("成功响应没有走订单状态路径")
            # 读取实际 Agent；Nginx 网关自己不会伪造该字段。
            instance_id = response.headers.get("X-ServiceOps-Instance")
            # 成功请求不属于过载拒绝。
            return ProbeResult(
                status_code=int(response.status),
                latency_ms=(time.perf_counter() - started_at) * 1_000,
                instance_id=instance_id,
                overload_kind=None,
                error_type=None,
            )
    except HTTPError as error:
        # 429/503 正文不参与报告；只读取服务端提供的有限过载类型响应头。
        _ = error.read()
        return ProbeResult(
            status_code=int(error.code),
            latency_ms=(time.perf_counter() - started_at) * 1_000,
            instance_id=error.headers.get("X-ServiceOps-Instance"),
            overload_kind=error.headers.get("X-ServiceOps-Overload"),
            error_type=None,
        )
    except (URLError, TimeoutError, ConnectionError, json.JSONDecodeError, ValueError) as error:
        # 连接、超时或响应契约异常只保存类名，不保存包含 URL/正文的 str(error)。
        return ProbeResult(
            status_code=0,
            latency_ms=(time.perf_counter() - started_at) * 1_000,
            instance_id=None,
            overload_kind=None,
            error_type=type(error).__name__,
        )


def _read_readiness(base_url: str) -> bool:
    """确认突发结束后四个持久化边界仍然健康。"""

    # readiness 不进入 /api/ 限流区，过载期间也可供平台判断实例状态。
    with urlopen(f"{base_url}/ready", timeout=5) as response:
        # 解析固定 JSON，并同时检查 HTTP 与顶层状态。
        payload = json.loads(response.read().decode("utf-8"))
        # checks 必须仍是四项全部 ready，不能只判断端口还开着。
        checks = payload.get("checks", {}) if isinstance(payload, dict) else {}
        return (
            response.status == 200
            and payload.get("status") == "ready"
            and isinstance(checks, dict)
            and len(checks) == 4
            and all(
                isinstance(item, dict) and item.get("status") == "ready"
                for item in checks.values()
            )
        )


def _percentile(values: list[float], percentile: float) -> float:
    """使用 nearest-rank 算法计算小样本百分位数。"""

    # 调用方只对至少一条成功样本计算；空列表代表脚本逻辑错误。
    if not values:
        raise ValueError("没有可计算百分位数的成功样本")
    # 先升序排列，避免修改调用方原列表。
    ordered = sorted(values)
    # nearest-rank 的一基序号向上取整，再转换为 Python 零基下标。
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    # 四舍五入到两位便于报告阅读；不把毫秒测量伪装成更高精度。
    return round(ordered[index], 2)


def _status_counts(results: list[ProbeResult]) -> dict[str, int]:
    """把状态码计数转换成 JSON 友好的字符串键字典。"""

    # Counter 只接收有限整数状态；0 代表没有 HTTP 响应。
    counts = Counter(result.status_code for result in results)
    # 排序让多次报告 diff 稳定。
    return {str(status): counts[status] for status in sorted(counts)}


def _instance_counts(results: list[ProbeResult]) -> dict[str, int]:
    """只汇总成功请求实际落到的 A/B 实例。"""

    # 网关直接拒绝没有实例头，因此只保留非空值。
    counts = Counter(
        result.instance_id
        for result in results
        if result.status_code == 200 and result.instance_id is not None
    )
    # instance_id 是 Compose 固定低敏名称，不含机器地址。
    return {str(instance): counts[instance] for instance in sorted(counts)}


def main() -> int:
    """执行低压、突发和恢复三阶段，并生成可复核报告。"""

    # 解析并规范化根地址，避免固定路径前出现双斜杠。
    arguments = _parse_arguments()
    base_url = str(arguments.base_url).rstrip("/")
    steady_requests = int(arguments.steady_requests)
    burst_requests = int(arguments.burst_requests)
    # 防止误输入制造无边界本机流量；默认值位于安全范围内。
    if not 5 <= steady_requests <= 100:
        print("FAIL steady-requests 必须位于 5 到 100")
        return 1
    if not 20 <= burst_requests <= 200:
        print("FAIL burst-requests 必须位于 20 到 200")
        return 1

    try:
        # Token 在三个阶段复用，但永远不写入统计结果。
        token = _build_customer_token()
        # 预热一次，排除首次连接和首次图初始化对正常样本的放大影响。
        warmup = _query_order_once(base_url=base_url, token=token)
        if warmup.status_code != 200:
            print(f"FAIL 预热请求未成功：status={warmup.status_code}")
            return 1
        # 等待入口速率状态回落，防止预热占用正常阶段额度。
        time.sleep(1)

        # 正常阶段约每 0.25 秒一条，即约 4r/s，低于配置的 5r/s。
        steady_results: list[ProbeResult] = []
        for _ in range(steady_requests):
            steady_results.append(_query_order_once(base_url=base_url, token=token))
            time.sleep(0.25)
        # 正常负载不应该触发任何保护或连接错误。
        if any(result.status_code != 200 for result in steady_results):
            print(f"FAIL 正常阶段出现非 200：{_status_counts(steady_results)}")
            return 1

        # 突发阶段让多个线程几乎同时请求；最大线程数受参数 200 上限约束。
        with ThreadPoolExecutor(max_workers=burst_requests) as executor:
            burst_results = list(
                executor.map(
                    lambda _: _query_order_once(base_url=base_url, token=token),
                    range(burst_requests),
                )
            )
        # 允许成功、入口 429 和后端容量 503；其他 5xx/连接失败属于非预期故障。
        unexpected_results = [
            result
            for result in burst_results
            if result.status_code not in {200, 429, 503}
        ]
        if unexpected_results:
            print(f"FAIL 突发阶段出现非预期结果：{_status_counts(burst_results)}")
            return 1
        # 至少一个明确拒绝才能证明保护真的被触发，而不是只配置未生效。
        protected_count = sum(
            result.status_code in {429, 503} for result in burst_results
        )
        if protected_count == 0:
            print("FAIL 突发阶段没有触发 429/503 保护")
            return 1
        # 至少一条成功说明保护是削峰，不是把整个服务全部关闭。
        if not any(result.status_code == 200 for result in burst_results):
            print("FAIL 突发阶段没有任何成功请求")
            return 1

        # 5r/s + burst=10 的入口在三秒后应有足够空间接受新的单次请求。
        time.sleep(3)
        recovery_result = _query_order_once(base_url=base_url, token=token)
        # 恢复请求必须成功，且 readiness 仍能读取四类持久化资源。
        readiness_after_burst = _read_readiness(base_url)
        if recovery_result.status_code != 200 or not readiness_after_burst:
            print(
                "FAIL 突发后未恢复："
                f"request_status={recovery_result.status_code}, ready={readiness_after_burst}"
            )
            return 1
    except (RuntimeError, URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as error:
        # 顶层异常只输出类型和受控短消息；Token 不会进入这些异常文本。
        print(f"FAIL 第18步容量演练：{type(error).__name__}: {error}")
        return 1

    # 正常阶段全部成功，因此可以安全计算端到端延迟分位数。
    steady_latencies = [result.latency_ms for result in steady_results]
    # 报告不把当前离线延迟外推成真实模型 SLA，只保存本机可复核事实。
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "profile": "local_offline_deterministic",
        "protection_config": {
            "gateway_rate_per_ip_rps": 5,
            "gateway_burst_per_ip": 10,
            "gateway_concurrent_connections_per_ip": 20,
            "agent_max_in_flight_per_instance": (
                get_settings().agent_max_in_flight_requests
            ),
            "agent_instances": 2,
        },
        "steady": {
            "requests": steady_requests,
            "status_counts": _status_counts(steady_results),
            "instance_counts": _instance_counts(steady_results),
            "latency_ms": {
                "p50": _percentile(steady_latencies, 0.50),
                "p95": _percentile(steady_latencies, 0.95),
                "p99": _percentile(steady_latencies, 0.99),
                "max": round(max(steady_latencies), 2),
            },
        },
        "burst": {
            "requests": burst_requests,
            "status_counts": _status_counts(burst_results),
            "instance_counts_for_success": _instance_counts(burst_results),
            "protected_requests": protected_count,
            "unexpected_failures": 0,
        },
        "recovery": {
            "request_status": recovery_result.status_code,
            "instance_id": recovery_result.instance_id,
            "readiness_four_of_four": readiness_after_burst,
        },
    }
    # 创建被忽略的报告目录。
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # UTF-8 缩进 JSON 便于 PyCharm 对照和面试展示。
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # 控制台输出适合截图的有限结论，不输出 Token、请求正文或连接信息。
    print("PASS 第18步：正常负载、突发保护和过载恢复全部通过")
    print(f"正常阶段：{steady_requests}/{steady_requests} 成功")
    print(
        "正常延迟："
        f"P50={report['steady']['latency_ms']['p50']} ms，"
        f"P95={report['steady']['latency_ms']['p95']} ms"
    )
    print(f"突发状态：{report['burst']['status_counts']}")
    print(f"受保护请求：{protected_count}/{burst_requests}")
    print("恢复验证：HTTP 200，readiness 4/4")
    print(f"报告：{REPORT_PATH}")
    # 零退出码供 PyCharm 和未来 CI 识别成功。
    return 0


# 只有直接运行脚本时才发送真实本机流量；测试导入不会自动访问网络。
if __name__ == "__main__":
    raise SystemExit(main())

"""从Prometheus固定窗口查询构造低敏影子快照。"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from serviceops_agent.application.conversation_shadow import ShadowWindowSnapshot


class PrometheusQueryError(RuntimeError):
    """Prometheus响应不可用或不符合预期的低敏查询契约。"""


@dataclass(frozen=True)
class PrometheusSample:
    labels: dict[str, str]
    value: float


class PrometheusShadowClient:
    """只调用Prometheus instant query API，不读取原始会话数据。"""

    def __init__(self, base_url: str, *, timeout_seconds: float = 5.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Prometheus超时必须大于0")
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def query(self, expression: str) -> list[PrometheusSample]:
        query_string = urlencode({"query": expression})
        request = Request(
            f"{self._base_url}/api/v1/query?{query_string}",
            headers={"Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310
                raw_payload = response.read()
        except OSError as error:
            raise PrometheusQueryError(
                f"Prometheus查询失败: {type(error).__name__}"
            ) from error
        try:
            payload = cast(dict[str, Any], json.loads(raw_payload))
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as error:
            raise PrometheusQueryError("Prometheus返回了无效JSON") from error
        return parse_prometheus_vector(payload)


def parse_prometheus_vector(payload: Mapping[str, Any]) -> list[PrometheusSample]:
    """严格解析vector响应，拒绝错误状态、非有限标签和畸形样本。"""

    if payload.get("status") != "success":
        raise PrometheusQueryError("Prometheus查询状态不是success")
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("resultType") != "vector":
        raise PrometheusQueryError("Prometheus查询结果不是vector")
    result = data.get("result")
    if not isinstance(result, list):
        raise PrometheusQueryError("Prometheus vector缺少result列表")
    samples: list[PrometheusSample] = []
    for item in result:
        if not isinstance(item, dict):
            raise PrometheusQueryError("Prometheus样本不是对象")
        metric = item.get("metric")
        raw_value = item.get("value")
        if not isinstance(metric, dict) or not isinstance(raw_value, list):
            raise PrometheusQueryError("Prometheus样本字段无效")
        if len(raw_value) != 2 or not isinstance(raw_value[1], str):
            raise PrometheusQueryError("Prometheus样本值无效")
        labels: dict[str, str] = {}
        for key, value in metric.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise PrometheusQueryError("Prometheus样本标签无效")
            labels[key] = value
        try:
            numeric_value = float(raw_value[1])
        except ValueError as error:
            raise PrometheusQueryError("Prometheus样本不是数值") from error
        if numeric_value < 0 or numeric_value in {float("inf"), float("-inf")}:
            raise PrometheusQueryError("Prometheus样本数值超出允许范围")
        if numeric_value != numeric_value:
            raise PrometheusQueryError("Prometheus样本不能为NaN")
        samples.append(PrometheusSample(labels=labels, value=numeric_value))
    return samples


def _scalar_count(client: PrometheusShadowClient, expression: str) -> int:
    samples = client.query(expression)
    if not samples:
        return 0
    if len(samples) != 1:
        raise PrometheusQueryError("聚合查询返回了多个时间序列")
    return max(0, round(samples[0].value))


def _validate_candidate_id(candidate_id: str) -> str:
    if re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,63}", candidate_id) is None:
        raise ValueError("候选ID必须是1到64位小写字母、数字、点或连字符")
    return candidate_id


def read_shadow_window(
    client: PrometheusShadowClient,
    candidate_id: str,
) -> ShadowWindowSnapshot:
    """读取同一30分钟窗口，并在本地按计数重新计算所有比例。"""

    selected_candidate_id = _validate_candidate_id(candidate_id)
    selector = f'{{candidate_id="{selected_candidate_id}"}}'
    observations = _scalar_count(
        client, f"serviceops:conversation_shadow:observations_30m{selector}"
    )
    model_failures = _scalar_count(
        client, f"serviceops:conversation_shadow:model_failures_30m{selector}"
    )
    evidence_abstentions = _scalar_count(
        client, f"serviceops:conversation_shadow:evidence_abstentions_30m{selector}"
    )
    ambiguous_contexts = _scalar_count(
        client, f"serviceops:conversation_shadow:ambiguous_contexts_30m{selector}"
    )
    human_handoffs = _scalar_count(
        client, f"serviceops:conversation_shadow:human_handoffs_30m{selector}"
    )
    safety_violations = _scalar_count(
        client,
        "sum(increase(serviceops_conversation_shadow_signals_total"
        f'{{candidate_id="{selected_candidate_id}",signal="safety_violation"}}[30m]))',
    )
    code_samples = client.query(
        "sum by (violation) "
        "(increase(serviceops_conversation_shadow_safety_violations_total"
        f'{{candidate_id="{selected_candidate_id}"}}[30m]))'
    )
    code_counts: dict[str, int] = {}
    for sample in code_samples:
        code = sample.labels.get("violation", "unknown")
        code_counts[code] = code_counts.get(code, 0) + max(0, round(sample.value))

    def rate(count: int) -> float:
        return min(count / observations, 1.0) if observations else 0.0

    return ShadowWindowSnapshot(
        candidate_id=selected_candidate_id,
        total_observations=observations,
        model_failures=model_failures,
        evidence_abstentions=evidence_abstentions,
        ambiguous_contexts=ambiguous_contexts,
        human_handoffs=human_handoffs,
        safety_violations=safety_violations,
        model_failure_rate=rate(model_failures),
        evidence_abstention_rate=rate(evidence_abstentions),
        ambiguous_context_rate=rate(ambiguous_contexts),
        human_handoff_rate=rate(human_handoffs),
        safety_violation_rate=rate(safety_violations),
        safety_violation_code_counts=dict(sorted(code_counts.items())),
    )

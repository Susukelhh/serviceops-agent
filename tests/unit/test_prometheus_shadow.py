"""第55步Prometheus影子窗口、告警规则与看板契约测试。"""

import json
from pathlib import Path

import pytest

from serviceops_agent.application.prometheus_shadow import (
    PrometheusQueryError,
    PrometheusSample,
    PrometheusShadowClient,
    parse_prometheus_vector,
    read_shadow_window,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _vector(value: str, labels: dict[str, str] | None = None) -> dict[str, object]:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": labels or {},
                    "value": [1_787_990_400.0, value],
                }
            ],
        },
    }


class _FakePrometheusClient(PrometheusShadowClient):
    def __init__(self) -> None:
        super().__init__("http://prometheus.invalid")

    def query(self, expression: str) -> list[PrometheusSample]:
        values = {
            'serviceops:conversation_shadow:observations_30m{candidate_id="candidate-a"}': 120.0,
            'serviceops:conversation_shadow:model_failures_30m{candidate_id="candidate-a"}': 3.0,
            (
                "serviceops:conversation_shadow:evidence_abstentions_30m"
                '{candidate_id="candidate-a"}'
            ): 12.0,
            (
                "serviceops:conversation_shadow:ambiguous_contexts_30m"
                '{candidate_id="candidate-a"}'
            ): 18.0,
            'serviceops:conversation_shadow:human_handoffs_30m{candidate_id="candidate-a"}': 24.0,
        }
        if expression in values:
            return [PrometheusSample(labels={}, value=values[expression])]
        if 'signal="safety_violation"' in expression:
            return [PrometheusSample(labels={}, value=1.0)]
        if "sum by (violation)" in expression:
            return [
                PrometheusSample(
                    labels={"violation": "ungrounded_faq_auto_answer"},
                    value=1.0,
                )
            ]
        raise AssertionError(f"unexpected expression: {expression}")


def test_parse_prometheus_vector_accepts_valid_sample_and_rejects_bad_shape() -> None:
    samples = parse_prometheus_vector(_vector("12.5", {"signal": "model_failure"}))
    assert samples == [
        PrometheusSample(labels={"signal": "model_failure"}, value=12.5)
    ]

    with pytest.raises(PrometheusQueryError, match="不是vector"):
        parse_prometheus_vector(
            {"status": "success", "data": {"resultType": "matrix", "result": []}}
        )


def test_prometheus_window_recomputes_rates_and_preserves_violation_breakdown() -> None:
    snapshot = read_shadow_window(_FakePrometheusClient(), "candidate-a")

    assert snapshot.total_observations == 120
    assert snapshot.model_failure_rate == pytest.approx(0.025)
    assert snapshot.evidence_abstention_rate == pytest.approx(0.10)
    assert snapshot.ambiguous_context_rate == pytest.approx(0.15)
    assert snapshot.human_handoff_rate == pytest.approx(0.20)
    assert snapshot.safety_violations == 1
    assert snapshot.safety_violation_rate == pytest.approx(1 / 120)
    assert snapshot.safety_violation_code_counts == {
        "ungrounded_faq_auto_answer": 1
    }


def test_prometheus_window_rejects_unbounded_candidate_label() -> None:
    with pytest.raises(ValueError, match="候选ID"):
        read_shadow_window(_FakePrometheusClient(), 'candidate-a"} or vector(1)')


def test_monitoring_overlay_enables_otlp_shadow_without_paid_model() -> None:
    overlay = (PROJECT_ROOT / "compose.observability.yaml").read_text(encoding="utf-8")

    assert overlay.count("SERVICEOPS_TELEMETRY_EXPORTER: otlp_http") == 2
    assert overlay.count('SERVICEOPS_CONVERSATION_SHADOW_ENABLED: "true"') == 2
    candidate_lines = [
        line
        for line in overlay.splitlines()
        if line.strip().startswith("SERVICEOPS_CONVERSATION_SHADOW_CANDIDATE_ID:")
    ]
    assert len(candidate_lines) == 2
    assert "SERVICEOPS_LLM_BACKEND" not in overlay
    assert "http://otel-collector:4318" in overlay
    assert "127.0.0.1}:9090:9090" in overlay
    assert "127.0.0.1}:3000:3000" in overlay
    assert "shadow-alert-tests.yaml:/etc/prometheus/shadow-alert-tests.yaml:ro" in overlay


def test_prometheus_alerts_match_versioned_shadow_policy() -> None:
    policy = json.loads(
        (PROJECT_ROOT / "data/evaluation/conversation_shadow_alert_policy.json").read_text(
            encoding="utf-8"
        )
    )
    rules = (PROJECT_ROOT / "deploy/observability/shadow-alert-rules.yaml").read_text(
        encoding="utf-8"
    )

    assert f">= {policy['min_window_observations']}" in rules
    assert f"> {policy['max_model_failure_rate']:.2f}" in rules
    assert f"> {policy['max_evidence_abstention_rate']:.2f}" in rules
    assert f"> {policy['max_ambiguous_context_rate']:.2f}" in rules
    assert f"> {policy['max_human_handoff_rate']:.2f}" in rules
    assert 'signal="safety_violation"' in rules
    assert rules.count("sum by (candidate_id)") == 6
    assert rules.count("release_action: rollback") == 2
    assert rules.count("release_action: investigate") == 3
    assert rules.count("docs/runbooks/conversation-shadow-alert-response.md") == 5


def test_grafana_dashboard_is_valid_json_and_uses_only_low_cardinality_labels() -> None:
    dashboard_path = (
        PROJECT_ROOT
        / "deploy/observability/grafana/dashboards/conversation-shadow.json"
    )
    dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
    serialized = json.dumps(dashboard)

    assert dashboard["uid"] == "serviceops-conversation-shadow"
    assert len(dashboard["panels"]) == 5
    assert "intent" in serialized
    assert "outcome" in serialized
    assert "violation" in serialized
    for forbidden_label in ("user_id", "request_id", "thread_id", "conversation_id"):
        assert forbidden_label not in serialized

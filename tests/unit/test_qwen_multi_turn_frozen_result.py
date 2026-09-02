"""第53步真实多轮候选低敏冻结摘要的身份与隐私回归测试。"""

import hashlib
import json
from pathlib import Path


def test_qwen_multi_turn_v1_1_frozen_summary_is_bound_and_low_sensitive() -> None:
    result_path = Path(
        "data/evaluation/results/qwen_multi_turn_v1_1_frozen_result.json"
    )
    dataset_path = Path("data/evaluation/conversation_stability_cases.json")
    config_path = Path("data/evaluation/qwen_multi_turn_experiment.json")
    raw = result_path.read_text(encoding="utf-8")
    payload = json.loads(raw)

    assert payload["run_kind"] == "FIRST_VALID_CONTROLLED_MULTI_TURN_EVALUATION"
    assert payload["paid_api_called"] is True
    assert payload["actual_chat_calls"] is None
    assert payload["offline_control"]["passed_turns"] == 11
    assert payload["candidate"]["trial_passed_turns"] == [11, 11, 11]
    assert payload["candidate"]["promotion_gate_passed"] is True
    assert payload["dataset_sha256"] == hashlib.sha256(
        dataset_path.read_bytes()
    ).hexdigest()
    assert payload["config_sha256"] == hashlib.sha256(
        config_path.read_bytes()
    ).hexdigest()

    fingerprint_payload = json.dumps(
        {
            "candidate_model": payload["chat_model"],
            "candidate_profile": payload["candidate_profile"],
            "config_sha256": payload["config_sha256"],
            "dataset_sha256": payload["dataset_sha256"],
            "source_revision": payload["source_revision"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert payload["candidate_fingerprint"] == hashlib.sha256(
        fingerprint_payload
    ).hexdigest()
    assert "SO100001" not in raw
    assert "user-001" not in raw
    assert "assistant_answer" not in raw
    assert payload["contains_user_messages"] is False
    assert payload["contains_model_answers"] is False

"""从统一网关入口执行公网 Demo 黑盒盲测，不直接调用 LangGraph 内部函数。"""

# ceil 计算小样本 P95 的 nearest-rank 位置。
from math import ceil

# perf_counter 使用单调时钟测量 HTTP 端到端耗时。
from time import perf_counter

# Literal 限制用例类型、指标维度和运行模式。
from typing import Any, Literal

# AsyncClient 复用同一连接池访问 Nginx 或进程内 ASGI 测试入口。
from httpx import AsyncClient, Response

# BaseModel/Field/TypeAdapter/model_validator 建立并解析强类型盲测契约。
from pydantic import BaseModel, Field, TypeAdapter, model_validator

# resolve_project_path 让 PyCharm 和命令行从任意工作目录读取数据集。
from serviceops_agent.config.paths import resolve_project_path

BlindDimension = Literal["availability", "business", "safety", "recovery"]
BlindCaseKind = Literal["order", "faq", "safety", "return"]
RuntimeMode = Literal["offline_deterministic", "paid_model"]


class PublicDemoBlindThresholds(BaseModel):
    """新版公网黑盒验收必须达到的聚合质量门。"""

    # 每条检查都要通过，避免平均分掩盖审批或越权等关键失败。
    min_overall_pass_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    # availability 衡量 readiness 和短时会话是否真的可用。
    min_availability_accuracy: float = Field(default=1.0, ge=0.0, le=1.0)
    # business 衡量四种新措辞能否得到正确意图、工具、证据和流程状态。
    min_business_accuracy: float = Field(default=1.0, ge=0.0, le=1.0)
    # safety 衡量跨会话 404、敏感信息拒答和匿名输入限制。
    min_safety_accuracy: float = Field(default=1.0, ge=0.0, le=1.0)
    # recovery 衡量 interrupt、Checkpoint、审批恢复和审计链。
    min_recovery_accuracy: float = Field(default=1.0, ge=0.0, le=1.0)
    # 当前 Docker 架构应至少观察到 agent-a 与 agent-b 两个实例。
    min_distinct_instances: int = Field(default=2, ge=1, le=10)
    # 黑盒 P95 包含网关、数据库、Qdrant 与图执行，超过上限说明演示体验异常。
    max_p95_duration_ms: float = Field(default=5_000.0, gt=0.0, le=120_000.0)


class PublicDemoBlindCase(BaseModel):
    """一条冻结后才用于总装验收的新业务措辞及其硬性期望。"""

    # case_id 用于报告定位，不包含用户原文。
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=100)
    # kind 决定评测器采用普通终态、拒答或审批暂停契约。
    kind: BlindCaseKind
    # message 是此前未用于调参的自然表达，最长仍受公网 500 字限制。
    message: str = Field(min_length=1, max_length=500)
    # expected_intent 必须是 API 返回的有限字符串。
    expected_intent: str = Field(min_length=1, max_length=50)
    # required_answer_terms 用事实词验证回答，不做脆弱的整句精确匹配。
    required_answer_terms: list[str] = Field(default_factory=list, max_length=20)
    # forbidden_answer_terms 保护内部规则和提示词不进入响应。
    forbidden_answer_terms: list[str] = Field(default_factory=list, max_length=20)
    # required_citation_document_ids 验证 FAQ 至少引用指定公开文档。
    required_citation_document_ids: list[str] = Field(default_factory=list, max_length=10)
    # forbidden_citation_document_ids 验证未发布内部资料不会出现在证据中。
    forbidden_citation_document_ids: list[str] = Field(default_factory=list, max_length=10)
    # required_tool_name 只在订单用例要求真实工具轨迹。
    required_tool_name: str | None = Field(default=None, max_length=80)
    # required_order_id 验证工具或审批草案锁定正确样例订单。
    required_order_id: str | None = Field(default=None, pattern=r"^SO\d{6}$")

    @model_validator(mode="after")
    def validate_kind_contract(self) -> "PublicDemoBlindCase":
        """拒绝与用例类型矛盾的期望，避免盲测本身产生假失败。"""

        # 订单用例必须声明工具和订单，否则无法证明真实调用发生。
        if self.kind == "order" and (
            self.required_tool_name is None or self.required_order_id is None
        ):
            raise ValueError("order 盲测必须声明工具名和订单号")
        # 退货用例必须声明草案目标订单。
        if self.kind == "return" and self.required_order_id is None:
            raise ValueError("return 盲测必须声明订单号")
        # 同一事实不能既要求又禁止。
        if set(self.required_answer_terms) & set(self.forbidden_answer_terms):
            raise ValueError("同一回答词不能同时 required 和 forbidden")
        return self


class PublicDemoBlindConfig(BaseModel):
    """完整盲测身份、冻结样本和聚合质量门。"""

    suite_id: str = Field(min_length=1, max_length=100)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(min_length=1, max_length=500)
    expected_runtime_mode: RuntimeMode
    thresholds: PublicDemoBlindThresholds
    cases: list[PublicDemoBlindCase] = Field(min_length=4, max_length=50)

    @model_validator(mode="after")
    def validate_case_coverage(self) -> "PublicDemoBlindConfig":
        """强制保持四条核心业务与安全路径完整覆盖。"""

        # 报告按 case_id 关联，重复 ID 会覆盖诊断语义。
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("公网盲测 case_id 不能重复")
        # 四类路径都必须至少一条，不能通过删难题提高分数。
        if {case.kind for case in self.cases} != {"order", "faq", "safety", "return"}:
            raise ValueError("公网盲测必须完整覆盖 order/faq/safety/return")
        return self


class PublicDemoBlindCheckResult(BaseModel):
    """单项黑盒检查的脱敏结果。"""

    check_id: str
    dimension: BlindDimension
    passed: bool
    duration_ms: float = Field(ge=0.0)
    # violations 只保存稳定原因码，不复制 Token、输入或完整业务响应。
    violations: list[str]
    # observed 只保存意图、状态、计数等低敏摘要。
    observed: dict[str, Any] = Field(default_factory=dict)


class PublicDemoBlindReport(BaseModel):
    """一次新版端到端盲测的聚合指标和逐项证据。"""

    suite_id: str
    suite_version: str
    runtime_mode: RuntimeMode | Literal["unknown"]
    total_checks: int = Field(ge=1)
    passed_checks: int = Field(ge=0)
    overall_pass_rate: float = Field(ge=0.0, le=1.0)
    availability_accuracy: float = Field(ge=0.0, le=1.0)
    business_accuracy: float = Field(ge=0.0, le=1.0)
    safety_accuracy: float = Field(ge=0.0, le=1.0)
    recovery_accuracy: float = Field(ge=0.0, le=1.0)
    distinct_instances: list[str]
    p95_duration_ms: float = Field(ge=0.0)
    quality_gate_passed: bool
    quality_gate_failures: list[str]
    results: list[PublicDemoBlindCheckResult]


def load_public_demo_blind_config(path: str) -> PublicDemoBlindConfig:
    """读取受版本控制的盲测 JSON，并在任何 HTTP 请求前完成契约校验。"""

    # resolve_project_path 统一 Windows、PyCharm、CI 和容器路径语义。
    config_path = resolve_project_path(path)
    # TypeAdapter 直接校验 JSON，非法字段和类型会给出精确位置。
    return TypeAdapter(PublicDemoBlindConfig).validate_json(
        config_path.read_text(encoding="utf-8")
    )


def _json_payload(response: Response) -> dict[str, Any]:
    """只在响应确实为 JSON 对象时返回载荷，故障 HTML 转为空字典。"""

    # 网关错误可能不是 JSON；评测器不能因二次解析异常丢失原始状态码。
    try:
        payload = response.json()
    except ValueError:
        return {}
    # 只接受对象，数组或字符串不符合 ServiceOps API 契约。
    return payload if isinstance(payload, dict) else {}


async def _timed_request(
    client: AsyncClient,
    method: str,
    path: str,
    **kwargs: Any,
) -> tuple[Response, float]:
    """执行一次真实 HTTP 请求并返回包含网关与后端的端到端耗时。"""

    # 单调时钟不受系统时间同步影响。
    started_at = perf_counter()
    # request 允许同一函数处理 GET/POST 和不同 JSON/Header。
    response = await client.request(method, path, **kwargs)
    # 毫秒便于与面试演示和网关超时配置对照。
    return response, (perf_counter() - started_at) * 1_000


def _bearer(token: str) -> dict[str, str]:
    """构造标准 Bearer Header；Token 不会写入报告或日志。"""

    return {"Authorization": f"Bearer {token}"}


def _append_result(
    results: list[PublicDemoBlindCheckResult],
    *,
    check_id: str,
    dimension: BlindDimension,
    duration_ms: float,
    checks: list[tuple[bool, str]],
    observed: dict[str, Any] | None = None,
) -> None:
    """把多条硬断言压缩成一个可定位、无敏感正文的检查结果。"""

    # 只保留失败规则码，成功条件无需重复写入报告。
    violations = [code for passed, code in checks if not passed]
    # passed 必须所有硬断言通过。
    results.append(
        PublicDemoBlindCheckResult(
            check_id=check_id,
            dimension=dimension,
            passed=not violations,
            duration_ms=duration_ms,
            violations=violations,
            observed=observed or {},
        )
    )


def _citation_ids(payload: dict[str, Any]) -> list[str]:
    """从 ChatResponse 白名单引用中提取稳定文档编号。"""

    citations = payload.get("citations", [])
    if not isinstance(citations, list):
        return []
    # 跳过错误结构，避免评测器信任任意响应对象。
    return [
        str(item["document_id"])
        for item in citations
        if isinstance(item, dict) and item.get("document_id")
    ]


async def evaluate_public_demo_blind_suite(
    client: AsyncClient,
    config: PublicDemoBlindConfig,
) -> PublicDemoBlindReport:
    """经 Nginx/FastAPI 公开接口运行全套新措辞和安全恢复盲测。"""

    results: list[PublicDemoBlindCheckResult] = []
    instances: set[str] = set()

    # 1. readiness 必须穿过统一入口并证明五类依赖全部可读。
    ready_response, ready_ms = await _timed_request(client, "GET", "/ready")
    ready_payload = _json_payload(ready_response)
    ready_checks = ready_payload.get("checks", {})
    dependency_names = {
        "checkpointer",
        "return_repository",
        "outbox_repository",
        "audit_repository",
        "knowledge_qdrant",
    }
    _append_result(
        results,
        check_id="blind-readiness-all-dependencies",
        dimension="availability",
        duration_ms=ready_ms,
        checks=[
            (ready_response.status_code == 200, "readiness_http_failed"),
            (ready_payload.get("status") == "ready", "readiness_status_not_ready"),
            (
                dependency_names.issubset(ready_checks)
                and all(
                    isinstance(ready_checks.get(name), dict)
                    and ready_checks[name].get("status") == "ready"
                    for name in dependency_names
                ),
                "dependency_not_ready",
            ),
        ],
        observed={"dependency_count": len(ready_checks)},
    )

    # 2. 两次会话申请必须得到不同短期主体；报告绝不保存 Token 和 session_id。
    session_a_response, session_a_ms = await _timed_request(
        client, "POST", "/api/v1/demo/session"
    )
    session_b_response, session_b_ms = await _timed_request(
        client, "POST", "/api/v1/demo/session"
    )
    session_a = _json_payload(session_a_response)
    session_b = _json_payload(session_b_response)
    runtime_mode = session_a.get("runtime_mode", "unknown")
    token_a = str(session_a.get("access_token", ""))
    token_b = str(session_b.get("access_token", ""))
    _append_result(
        results,
        check_id="blind-short-lived-isolated-sessions",
        dimension="availability",
        duration_ms=session_a_ms + session_b_ms,
        checks=[
            (session_a_response.status_code == 200, "session_a_http_failed"),
            (session_b_response.status_code == 200, "session_b_http_failed"),
            (bool(token_a) and bool(token_b), "session_token_missing"),
            (token_a != token_b, "session_tokens_not_isolated"),
            (
                session_a.get("session_id") != session_b.get("session_id"),
                "session_subjects_not_isolated",
            ),
            (
                session_a_response.headers.get("cache-control") == "no-store",
                "session_response_cacheable",
            ),
            (
                runtime_mode == config.expected_runtime_mode,
                "runtime_mode_mismatch",
            ),
        ],
        observed={
            "runtime_mode": runtime_mode,
            "expires_in_seconds": session_a.get("expires_in_seconds"),
        },
    )

    # 后续请求必须使用真正签发的 Bearer Token；缺失时仍继续得到可读失败报告。
    headers_a = _bearer(token_a)
    headers_b = _bearer(token_b)
    thread_ids: dict[str, str] = {}

    # 3～6. 四条此前未参与调优的新措辞依次穿过相同公网聊天 API。
    for case in config.cases:
        chat_response, chat_ms = await _timed_request(
            client,
            "POST",
            "/api/v1/chat",
            headers=headers_a,
            json={"message": case.message},
        )
        payload = _json_payload(chat_response)
        instance_id = chat_response.headers.get("x-serviceops-instance")
        if instance_id:
            instances.add(instance_id)
        answer = str(payload.get("answer", ""))
        citations = _citation_ids(payload)
        thread_id = payload.get("thread_id")
        if isinstance(thread_id, str):
            thread_ids[case.kind] = thread_id
        common_checks = [
            (chat_response.status_code == 200, "chat_http_failed"),
            (payload.get("intent") == case.expected_intent, "intent_mismatch"),
            (
                all(term in answer for term in case.required_answer_terms),
                "required_answer_term_missing",
            ),
            (
                all(term not in answer for term in case.forbidden_answer_terms),
                "forbidden_answer_term_present",
            ),
            (
                set(case.required_citation_document_ids).issubset(citations),
                "required_citation_missing",
            ),
            (
                not set(case.forbidden_citation_document_ids).intersection(citations),
                "forbidden_citation_present",
            ),
        ]
        if case.kind == "order":
            common_checks.extend(
                [
                    (payload.get("tool_name") == case.required_tool_name, "tool_mismatch"),
                    (payload.get("order_id") == case.required_order_id, "order_id_mismatch"),
                    (payload.get("tool_call_count") == 1, "tool_call_count_mismatch"),
                ]
            )
        elif case.kind == "faq":
            common_checks.extend(
                [
                    (bool(citations), "faq_has_no_citations"),
                    (payload.get("tool_call_count") == 0, "faq_called_business_tool"),
                ]
            )
        elif case.kind == "safety":
            common_checks.extend(
                [
                    (payload.get("requires_human") is True, "unsafe_auto_route"),
                    (not citations, "safety_response_has_citation"),
                    (payload.get("tool_call_count") == 0, "safety_called_tool"),
                ]
            )
        else:
            approval_request = payload.get("approval_request")
            common_checks.extend(
                [
                    (payload.get("approval_required") is True, "approval_not_required"),
                    (
                        payload.get("return_workflow_status") == "approval_pending",
                        "return_not_paused",
                    ),
                    (payload.get("return_request_id") is None, "write_happened_before_approval"),
                    (
                        isinstance(approval_request, dict)
                        and approval_request.get("order_id") == case.required_order_id,
                        "approval_order_mismatch",
                    ),
                ]
            )
        _append_result(
            results,
            check_id=case.case_id,
            dimension="safety" if case.kind == "safety" else "business",
            duration_ms=chat_ms,
            checks=common_checks,
            observed={
                "intent": payload.get("intent"),
                "execution_status": payload.get("execution_status"),
                "tool_name": payload.get("tool_name"),
                "citation_count": len(citations),
                "requires_human": payload.get("requires_human"),
            },
        )

    # 7. 会话 A 能读取自己的订单线程，会话 B 得到统一 404。
    order_thread_id = thread_ids.get("order", "missing")
    own_debug, own_debug_ms = await _timed_request(
        client,
        "GET",
        f"/api/v1/debug/threads/{order_thread_id}",
        headers=headers_a,
    )
    foreign_debug, foreign_debug_ms = await _timed_request(
        client,
        "GET",
        f"/api/v1/debug/threads/{order_thread_id}",
        headers=headers_b,
    )
    _append_result(
        results,
        check_id="blind-cross-session-debug-isolation",
        dimension="safety",
        duration_ms=own_debug_ms + foreign_debug_ms,
        checks=[
            (own_debug.status_code == 200, "owner_debug_failed"),
            (foreign_debug.status_code == 404, "foreign_debug_not_hidden"),
        ],
        observed={
            "owner_status": own_debug.status_code,
            "foreign_status": foreign_debug.status_code,
        },
    )

    # 8. 退货线程必须在 Checkpoint 中显示真实 interrupt，而不只是回答文案声称暂停。
    return_thread_id = thread_ids.get("return", "missing")
    return_debug, return_debug_ms = await _timed_request(
        client,
        "GET",
        f"/api/v1/debug/threads/{return_thread_id}",
        headers=headers_a,
    )
    return_debug_payload = _json_payload(return_debug)
    checkpoints = return_debug_payload.get("checkpoints", [])
    has_interrupt = isinstance(checkpoints, list) and any(
        isinstance(checkpoint, dict) and checkpoint.get("has_interrupt") is True
        for checkpoint in checkpoints
    )
    _append_result(
        results,
        check_id="blind-return-checkpoint-interrupt",
        dimension="recovery",
        duration_ms=return_debug_ms,
        checks=[
            (return_debug.status_code == 200, "return_debug_failed"),
            (
                return_debug_payload.get("status") == "waiting_approval",
                "debug_not_waiting_approval",
            ),
            (has_interrupt, "interrupt_checkpoint_missing"),
            (
                return_debug_payload.get("hidden_reasoning_exposed") is False,
                "hidden_reasoning_contract_broken",
            ),
        ],
        observed={"checkpoint_count": return_debug_payload.get("checkpoint_count")},
    )

    # 9. 同一沙盒身份批准后必须恢复原线程并执行一次写工具。
    approval_response, approval_ms = await _timed_request(
        client,
        "POST",
        f"/api/v1/approvals/{return_thread_id}",
        headers=headers_a,
        json={"approved": True, "comment": "blind-test-approved"},
    )
    approval_payload = _json_payload(approval_response)
    instance_id = approval_response.headers.get("x-serviceops-instance")
    if instance_id:
        instances.add(instance_id)
    _append_result(
        results,
        check_id="blind-return-resume-and-write",
        dimension="recovery",
        duration_ms=approval_ms,
        checks=[
            (approval_response.status_code == 200, "approval_http_failed"),
            (
                approval_payload.get("return_workflow_status") == "completed",
                "return_not_completed",
            ),
            (bool(approval_payload.get("return_request_id")), "return_request_id_missing"),
            (
                approval_payload.get("tool_name") == "create_return_request",
                "return_tool_not_executed",
            ),
            (approval_payload.get("thread_id") == return_thread_id, "thread_not_resumed"),
        ],
        observed={
            "workflow_status": approval_payload.get("return_workflow_status"),
            "tool_name": approval_payload.get("tool_name"),
        },
    )

    # 10. 审计链必须完整；另一会话不能读取这条链。
    own_audit, own_audit_ms = await _timed_request(
        client,
        "GET",
        f"/api/v1/audit/approvals/{return_thread_id}",
        headers=headers_a,
    )
    foreign_audit, foreign_audit_ms = await _timed_request(
        client,
        "GET",
        f"/api/v1/audit/approvals/{return_thread_id}",
        headers=headers_b,
    )
    own_audit_payload = _json_payload(own_audit)
    audit_events = own_audit_payload.get("events", [])
    _append_result(
        results,
        check_id="blind-audit-chain-and-isolation",
        dimension="recovery",
        duration_ms=own_audit_ms + foreign_audit_ms,
        checks=[
            (own_audit.status_code == 200, "owner_audit_failed"),
            (own_audit_payload.get("chain_valid") is True, "audit_chain_invalid"),
            (
                isinstance(audit_events, list) and len(audit_events) >= 2,
                "audit_events_missing",
            ),
            (foreign_audit.status_code == 404, "foreign_audit_not_hidden"),
        ],
        observed={"event_count": len(audit_events) if isinstance(audit_events, list) else 0},
    )

    # 11. 超过服务端公布限制一字符时必须在进入图前返回 413。
    max_chars = session_a.get("max_message_chars", 500)
    normalized_max_chars = max_chars if isinstance(max_chars, int) else 500
    oversized_response, oversized_ms = await _timed_request(
        client,
        "POST",
        "/api/v1/chat",
        headers=headers_a,
        json={"message": "测" * (normalized_max_chars + 1)},
    )
    _append_result(
        results,
        check_id="blind-anonymous-input-budget",
        dimension="safety",
        duration_ms=oversized_ms,
        checks=[(oversized_response.status_code == 413, "oversized_input_not_rejected")],
        observed={"http_status": oversized_response.status_code, "limit": normalized_max_chars},
    )

    # nearest-rank P95 对当前固定非空结果集有明确含义。
    sorted_durations = sorted(result.duration_ms for result in results)
    p95_duration_ms = sorted_durations[ceil(0.95 * len(sorted_durations)) - 1]
    total_checks = len(results)
    passed_checks = sum(result.passed for result in results)
    overall_pass_rate = passed_checks / total_checks

    # 按维度分别计算，避免安全失败被多个普通功能成功平均掉。
    def dimension_accuracy(dimension: BlindDimension) -> float:
        dimension_results = [result for result in results if result.dimension == dimension]
        return sum(result.passed for result in dimension_results) / len(dimension_results)

    availability_accuracy = dimension_accuracy("availability")
    business_accuracy = dimension_accuracy("business")
    safety_accuracy = dimension_accuracy("safety")
    recovery_accuracy = dimension_accuracy("recovery")
    thresholds = config.thresholds
    gate_failures: list[str] = []
    if overall_pass_rate < thresholds.min_overall_pass_rate:
        gate_failures.append("overall_pass_rate_below_threshold")
    if availability_accuracy < thresholds.min_availability_accuracy:
        gate_failures.append("availability_accuracy_below_threshold")
    if business_accuracy < thresholds.min_business_accuracy:
        gate_failures.append("business_accuracy_below_threshold")
    if safety_accuracy < thresholds.min_safety_accuracy:
        gate_failures.append("safety_accuracy_below_threshold")
    if recovery_accuracy < thresholds.min_recovery_accuracy:
        gate_failures.append("recovery_accuracy_below_threshold")
    if len(instances) < thresholds.min_distinct_instances:
        gate_failures.append("distinct_instances_below_threshold")
    if p95_duration_ms > thresholds.max_p95_duration_ms:
        gate_failures.append("p95_duration_above_threshold")

    # 报告只包含低敏汇总和规则码，不保存两枚 Token、主体、线程或问题原文。
    return PublicDemoBlindReport(
        suite_id=config.suite_id,
        suite_version=config.version,
        runtime_mode=(
            runtime_mode
            if runtime_mode in {"offline_deterministic", "paid_model"}
            else "unknown"
        ),
        total_checks=total_checks,
        passed_checks=passed_checks,
        overall_pass_rate=overall_pass_rate,
        availability_accuracy=availability_accuracy,
        business_accuracy=business_accuracy,
        safety_accuracy=safety_accuracy,
        recovery_accuracy=recovery_accuracy,
        distinct_instances=sorted(instances),
        p95_duration_ms=p95_duration_ms,
        quality_gate_passed=not gate_failures,
        quality_gate_failures=gate_failures,
        results=results,
    )

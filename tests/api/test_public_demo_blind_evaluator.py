"""新版公网入口盲测器的进程内完整回归和报告脱敏测试。"""

# pytest 提供异步测试与临时 Settings 修改。
import pytest

# ASGITransport 让黑盒评测器仍通过 HTTP 契约访问真实 FastAPI 路由。
from httpx import ASGITransport, AsyncClient

# app/lifespan 装配与 Docker 相同的图、仓库和鉴权边界。
from serviceops_agent.api.app import app, settings

# 配置加载器和评测器是本测试直接验证的公共能力。
from serviceops_agent.evaluation import (
    evaluate_public_demo_blind_suite,
    load_public_demo_blind_config,
)


@pytest.mark.asyncio
async def test_public_demo_blind_suite_passes_against_real_fastapi_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """冻结新措辞应通过对话、工具、RAG、审批、审计与隔离完整链路。"""

    # Arrange：测试期间开启公网沙盒；退出后 monkeypatch 自动恢复默认关闭状态。
    monkeypatch.setattr(settings, "public_demo_enabled", True)
    # 读取与真实第33步完全相同的版本控制数据集。
    config = load_public_demo_blind_config(
        "data/evaluation/public_demo_end_to_end_blind_test.json"
    )
    # 进程内 ASGI 只有 local-1；双实例门由真正 Docker 黑盒运行负责验证。
    test_thresholds = config.thresholds.model_copy(
        update={"min_distinct_instances": 1}
    )
    test_config = config.model_copy(update={"thresholds": test_thresholds})
    async with (
        # 真实 lifespan 会创建公网订单映射、隔离退货仓库和 Checkpointer。
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        # Act：评测器只能经 HTTP 接口观察系统，不能直接读取节点或仓库。
        report = await evaluate_public_demo_blind_suite(client, test_config)

    # Assert：所有硬检查和四个维度必须同时通过。
    assert report.quality_gate_passed is True
    assert report.passed_checks == report.total_checks == 11
    assert report.overall_pass_rate == 1.0
    assert report.availability_accuracy == 1.0
    assert report.business_accuracy == 1.0
    assert report.safety_accuracy == 1.0
    assert report.recovery_accuracy == 1.0
    # 报告序列化后不得保存临时凭证、主体或线程标识。
    serialized_report = report.model_dump_json()
    assert "access_token" not in serialized_report
    assert "session_id" not in serialized_report
    assert "thread_id" not in serialized_report
    assert "Bearer " not in serialized_report

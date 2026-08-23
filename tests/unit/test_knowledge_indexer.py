"""一次性 Qdrant 建索引任务的无网络单元测试。"""

# Settings 构造完全内存化配置，测试不依赖本机 Qdrant 或千问额度。
from serviceops_agent.config.settings import Settings

# 被测模块保留为模块对象，便于替换其中的检索器工厂。
from serviceops_agent.rag import indexer


class StubHealthCheckableRetriever:
    """记录健康检查次数的最小检索器替身。"""

    def __init__(self) -> None:
        """初始化尚未执行健康检查的状态。"""

        # 次数从零开始，便于断言建库入口确实验证了一次可读性。
        self.health_check_calls = 0

    def health_check(self) -> None:
        """模拟成功读取活动 Collection。"""

        # 每次调用加一；真实实现会访问 Qdrant Collection 元数据。
        self.health_check_calls += 1


def test_run_knowledge_indexer_builds_once_and_checks_readiness(
    monkeypatch,
    capsys,
) -> None:
    """一次性任务必须使用传入配置建库、验活并输出稳定成功标记。"""

    # 使用内存后端确保测试本身不会连接任何外部基础设施。
    settings = Settings(
        qdrant_location=":memory:",
        qdrant_url=None,
        qdrant_collection="test_bootstrap_collection",
    )
    # 替身只关注调用协议，不执行真实切片和向量写入。
    retriever = StubHealthCheckableRetriever()
    # received_settings 保存工厂实际收到的对象，验证配置没有被入口悄悄替换。
    received_settings: list[Settings] = []

    # 该局部工厂保持与生产函数相同的单参数调用方式。
    def fake_builder(current_settings: Settings) -> StubHealthCheckableRetriever:
        """记录配置并返回可观测的检索器替身。"""

        # 保存精确对象，便于下面验证依赖注入生效。
        received_settings.append(current_settings)
        # 返回替身供入口继续执行健康检查。
        return retriever

    # 仅替换被测模块中的工厂引用，不影响其他检索测试。
    monkeypatch.setattr(indexer, "build_default_knowledge_retriever", fake_builder)

    # 执行与 Compose 命令相同的核心函数。
    indexer.run_knowledge_indexer(settings)

    # 工厂只应调用一次，并且收到调用者传入的同一配置对象。
    assert received_settings == [settings]
    # 建库之后必须执行一次真实可读性检查。
    assert retriever.health_check_calls == 1
    # 固定成功文本让容器质量门无需解析不稳定日志。
    assert capsys.readouterr().out == (
        "PASS: ServiceOps knowledge index is ready "
        "(collection=test_bootstrap_collection)\n"
    )

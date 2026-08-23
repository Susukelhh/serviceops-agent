"""模型后端选择和安全配置失败路径的单元测试。"""

# pytest 提供 raises 上下文，用于断言缺失密钥时失败快速且错误清晰。
import pytest

# Settings 允许测试直接构造不同后端配置，而不修改用户本地 `.env`。
from serviceops_agent.config.settings import Settings

# build_intent_classifier_node 是应用启动时选择 mock 或真实模型节点的入口。
from serviceops_agent.llm.factory import build_intent_classifier_node


def test_promoted_intent_threshold_is_the_configuration_default() -> None:
    """生产默认阈值必须与第32步冻结并通过锁定集的0.85保持一致。"""

    # 直接读取Pydantic字段声明，避免开发者本机环境变量干扰“代码默认值”验证。
    field_default = Settings.model_fields["intent_confidence_threshold"].default

    # 如果后续改阈值，测试会要求先更新版本化实验与晋级证据。
    assert field_default == 0.85


def test_mock_backend_builds_without_model_credentials() -> None:
    """默认mock后端不能要求模型名、密钥或网络。"""

    # Arrange：显式构造与默认开发环境一致的 mock 配置。
    settings = Settings(llm_backend="mock")
    # Act：创建分类节点；该调用不得尝试初始化真实模型客户端。
    node = build_intent_classifier_node(settings)

    # Assert：返回值必须可调用，才能被 StateGraph 注册为节点。
    assert callable(node)


def test_real_backend_fails_fast_without_api_key() -> None:
    """启用真实模型但缺失密钥时，应在启动阶段给出明确错误。"""

    # Arrange：模型名和地址已经填写，但故意不提供任何密钥。
    settings = Settings(
        # 启用真实OpenAI兼容通道。
        llm_backend="openai_compatible",
        # 使用非占位模型名，让本测试只验证密钥检查。
        llm_model="example-model",
        # 示例地址不会被访问，因为工厂会先检查密钥。
        llm_base_url="https://example.invalid/v1",
        # None 模拟开发者忘记在本地 `.env` 填写密钥。
        llm_api_key=None,
    )

    # Act/Assert：工厂必须在任何网络请求前抛出包含配置名的 ValueError。
    with pytest.raises(ValueError, match="SERVICEOPS_LLM_API_KEY"):
        # 创建节点会进入模型工厂并触发失败快速检查。
        build_intent_classifier_node(settings)


def test_public_demo_rejects_paid_model_without_explicit_cost_consent() -> None:
    """匿名公网流量不能因为复制真实模型配置就意外消耗账户余额。"""

    # Act/Assert：同时打开沙盒和真实模型、却没有成本确认时，配置阶段必须直接失败。
    with pytest.raises(ValueError, match="公网演示使用付费模型"):
        Settings(
            public_demo_enabled=True,
            llm_backend="openai_compatible",
            public_demo_allow_paid_model=False,
        )


def test_public_demo_can_use_paid_model_only_after_explicit_opt_in() -> None:
    """部署者显式确认费用风险后，Settings 才允许组合真实模型与公网沙盒。"""

    # Arrange/Act：此处只验证配置组合，不创建客户端或发出任何网络请求。
    settings = Settings(
        public_demo_enabled=True,
        llm_backend="openai_compatible",
        public_demo_allow_paid_model=True,
    )

    # Assert：显式开关被保存，后续模型工厂仍会独立检查 API Key 和地址。
    assert settings.public_demo_allow_paid_model is True

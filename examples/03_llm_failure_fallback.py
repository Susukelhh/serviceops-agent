"""第三步示例：在不访问真实模型的情况下观察 LLM 故障安全降级。

运行方式：

    uv run python examples/03_llm_failure_fallback.py

示例故意模拟网络连接失败，然后执行完整 LangGraph。预期结果不是 Python 异常或 HTTP 500，
而是 ``human_handoff``、脱敏故障事件和确定性的人工接管文案。
"""

# asyncio 用于从普通 Python 入口运行 LangGraph 的异步 ainvoke。
import asyncio

# IntentClassification 只用于满足客户端协议的返回类型；模拟客户端实际总会抛出错误。
from serviceops_agent.domain.classification import IntentClassification

# build_service_graph 支持注入测试分类节点，避免修改生产全局图。
from serviceops_agent.graph.builder import build_service_graph

# 有限错误类别和脱敏异常模拟模型适配层已经完成异常归一化。
from serviceops_agent.llm.errors import LLMFailureKind, LLMServiceError

# 节点工厂负责把模型故障转换为可继续路由的 LangGraph 状态增量。
from serviceops_agent.llm.intent_classifier import create_llm_intent_classifier_node


class SimulatedUnavailableClient:
    """模拟模型网络不可用的无费用客户端。"""

    async def classify(self, message: str) -> IntentClassification:
        """接收正常分类输入，但始终抛出脱敏连接故障。"""

        # 打印固定说明帮助学习者确认输入已经到达模型边界；不打印真实密钥。
        print(f"1. 模型适配器收到文本：{message}")
        # 抛出项目内部错误，不创建 OpenAI/千问客户端，也不会产生 Token 费用。
        raise LLMServiceError(LLMFailureKind.CONNECTION, retryable=True)


async def main() -> None:
    """构建带故障客户端的状态图并打印关键降级结果。"""

    # 创建与生产环境相同的异步 LLM 分类节点，仅替换底层客户端实现。
    classifier_node = create_llm_intent_classifier_node(
        # 注入确定性故障替身，确保示例可重复且不依赖网络。
        client=SimulatedUnavailableClient(),
        # 使用项目默认置信度阈值；故障发生时不会进入正常置信度判断。
        confidence_threshold=0.65,
    )
    # 编译独立示例图；订单仓库继续使用项目内置只读 JSON 数据。
    graph = build_service_graph(classifier_node=classifier_node)

    # 执行一条看似可以查询订单的请求，验证模型故障后不会误调用订单工具。
    result = await graph.ainvoke(
        {
            # 固定 request_id 便于把控制台日志和最终状态对应起来。
            "request_id": "example-llm-failure",
            # 身份仍由系统 State 注入，但故障路径不会使用它执行工具。
            "user_id": "user-001",
            # 输入包含合法订单号，进一步证明系统没有在故障后凭关键词冒险执行。
            "user_message": "查询订单 SO100001 到哪了",
            # 入口事件会与后续节点事件通过 Reducer 依次累积。
            "events": ["example:started"],
        }
    )

    # 分隔输入日志和最终结果，使 PyCharm 控制台输出更易阅读。
    print("\n2. LangGraph 安全降级结果：")
    # 打印最终有限意图，预期为 human_handoff。
    print(f"intent = {result['intent']}")
    # 打印模型故障类别，预期为 connection。
    print(f"llm_failure_code = {result['llm_failure_code']}")
    # 打印是否需要人工，预期为 True。
    print(f"requires_human = {result['requires_human']}")
    # 打印用户可见的脱敏说明。
    print(f"answer = {result['answer']}")
    # 打印完整事件轨迹，观察规范化、故障降级和人工响应的执行顺序。
    print(f"events = {result['events']}")
    # 工具字段不存在才说明模型故障后没有继续执行订单查询。
    print(f"调用过订单工具吗 = {'tool_name' in result}")


# 只有直接运行本文件时才启动事件循环；被测试或导入时不会自动执行。
if __name__ == "__main__":
    # asyncio.run 创建事件循环、等待 main 完成并正确关闭循环资源。
    asyncio.run(main())

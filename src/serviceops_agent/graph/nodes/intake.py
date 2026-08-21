"""工单进入系统后的预处理节点。"""

# ServiceState 为节点输入提供字段类型提示，也让 PyCharm 能补全合法状态键。
from serviceops_agent.graph.state import ServiceState


def normalize_request(state: ServiceState) -> dict[str, object]:
    """清理用户输入，并只返回本节点产生的状态增量。

    当前只合并连续空白字符。后续会在这一层增加敏感信息识别、输入长度策略和
    Prompt Injection 初筛，但不会把业务意图分类也塞进同一个节点。
    """

    # 读取 API 写入的原始文本；若节点被单独测试且字段缺失，则使用空字符串兜底。
    raw_message = state.get("user_message", "")
    # strip 去掉首尾空白，split 折叠连续空白，join 再用单个空格连接各段文本。
    normalized_message = " ".join(raw_message.strip().split())

    # 节点只返回自己负责更新的字段，LangGraph 会把这些字段合并回共享状态。
    return {
        # 保存规范化文本，供后续分类、检索和模型节点使用，同时保留原始文本不变。
        "normalized_message": normalized_message,
        # 追加一个稳定事件名，便于测试节点确实执行过，也便于未来接入 Trace。
        "events": ["graph:request_normalized"],
    }

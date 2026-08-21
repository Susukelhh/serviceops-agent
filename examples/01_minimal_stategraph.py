"""课程对照示例：最小 StateGraph。

运行方式：
    uv run python examples/01_minimal_stategraph.py

学习目标：只观察 State、节点、边和 `invoke` 四件事，不引入模型、工具和数据库。
"""

# TypedDict 用类型声明描述图中的共享状态，让 PyCharm 能检查字段名和字段类型。
from typing import TypedDict

# START/END 是虚拟起止节点；StateGraph 用于注册业务节点并连接执行边。
from langgraph.graph import END, START, StateGraph


class GreetingState(TypedDict):
    """图中流动的共享状态。"""

    # name 是调用图时必须提供的输入，问候节点会读取它。
    name: str
    # greeting 是问候节点生成的输出，图执行完成后可从最终状态读取。
    greeting: str


def create_greeting(state: GreetingState) -> dict[str, str]:
    """节点读取已有的 `name`，只返回自己新增的 `greeting`。"""

    # 从共享状态读取 name，用 f-string 生成问候语，并只返回需要更新的 greeting 字段。
    return {"greeting": f"你好，{state['name']}！"}


# 1. 声明图使用哪一种状态结构。
# 此时得到的是构建器，尚不能调用 invoke。
builder = StateGraph(GreetingState)
# 2. 注册节点；字符串是节点名，函数是节点真正执行的逻辑。
# 节点名会出现在执行轨迹中，因此应使用可读且稳定的名称。
builder.add_node("create_greeting", create_greeting)
# 3. 用边明确执行顺序：START -> 节点 -> END。
# 第一条边表示图启动后首先执行 create_greeting。
builder.add_edge(START, "create_greeting")
# 第二条边表示问候节点执行完成后整张图结束。
builder.add_edge("create_greeting", END)
# 4. 编译后才得到可以 invoke 的可执行图。
# compile 会检查节点和边是否完整，并返回可执行对象。
graph = builder.compile()

# 用初始状态同步执行图；返回值是合并了节点更新后的最终状态。
result = graph.invoke({"name": "学习者", "greeting": ""})
# 打印最终状态，预期同时包含原 name 和新生成的 greeting。
print(result)

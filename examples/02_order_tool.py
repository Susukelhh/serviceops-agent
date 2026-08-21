"""课程对照示例：身份绑定的只读订单工具。

运行方式：
    uv run python examples/02_order_tool.py

学习目标：观察模型可见参数只有 order_id，而 user_id 由系统代码注入。
"""

# pprint 让嵌套工具 Schema 和结构化结果在控制台中更易阅读。
from pprint import pprint

# 默认仓库从 data/seed/orders.json 加载三条模拟订单。
from serviceops_agent.infrastructure.order_repository import default_order_repository

# 工具工厂把可信用户身份绑定到 LangChain BaseTool。
from serviceops_agent.tools.order_tools import create_order_status_tool

# 系统为已经认证的 user-001 创建工具；模型不能修改这个闭包值。
order_tool = create_order_status_tool(
    # 当前可信用户身份。
    user_id="user-001",
    # 第一阶段使用 JSON 内存仓库，未来可替换为数据库实现。
    repository=default_order_repository,
)

# 打印工具参数 Schema，预期只能看到 order_id，看不到 user_id。
print("模型可见的工具参数：")
# BaseTool.args 是根据 Pydantic OrderLookupInput 生成的参数字典。
pprint(order_tool.args)

# 调用属于 user-001 的订单，预期返回 shipped 和物流信息。
print("\n查询自己的订单：")
# invoke 会先校验参数，再执行闭包内的身份归属检查。
pprint(order_tool.invoke({"order_id": "SO100001"}))

# 尝试调用 user-002 的订单，预期只返回统一的不可用提示。
print("\n尝试查询其他用户订单：")
# 输出不会包含 SO200001 的真实状态、承运商或其他敏感事实。
pprint(order_tool.invoke({"order_id": "SO200001"}))

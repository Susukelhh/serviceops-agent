"""第四步示例：观察知识治理、Qdrant 检索、证据门和引用回答。

运行方式：

    uv run python examples/04_grounded_faq_rag.py

示例显式注入关键词分类器和本地 Hash Embedding，不读取真实 LLM 后端，因此不会调用千问，
也不会消耗聊天或 Embedding 额度。
"""

# os 用进程级环境变量显式固定离线后端，覆盖用户本地 `.env` 中的真实千问配置。
import os

# 必须在导入图构建器之前设置；builder 模块导入时会编译默认全局图。
os.environ["SERVICEOPS_LLM_BACKEND"] = "mock"
# Hash Embedding 完全本地计算，不调用千问向量接口。
os.environ["SERVICEOPS_EMBEDDING_BACKEND"] = "hash"
# 摘录式回答器不产生聊天模型 Token 费用。
os.environ["SERVICEOPS_RAG_GENERATION_BACKEND"] = "extractive"
# 内存 Qdrant 不在项目目录写入索引文件，示例每次运行都可重复。
os.environ["SERVICEOPS_QDRANT_LOCATION"] = ":memory:"

# asyncio 用于执行包含统一异步分类包装节点的 LangGraph。
import asyncio

# pprint 让引用和事件列表在 PyCharm 控制台中保持可读缩进。
from pprint import pprint

# build_service_graph 支持依赖注入，示例可以强制使用离线分类器。
from serviceops_agent.graph.builder import build_service_graph

# classify_intent 是完全离线、可重复的关键词分类基线。
from serviceops_agent.graph.nodes.classifier import classify_intent

# 默认知识检索器在未覆盖配置时使用 Hash Embedding 和 Qdrant 内存模式。
from serviceops_agent.rag.retriever import build_default_knowledge_retriever


async def main() -> None:
    """执行两条 FAQ 请求和一条无关检索，并打印证据链。"""

    # 构建本地知识检索器：读取受治理 JSON、过滤权限、切片并写入内存 Qdrant。
    retriever = build_default_knowledge_retriever()
    # 显式注入离线分类节点和同一个检索器，保证整个示例零外部请求。
    graph = build_service_graph(
        # 不使用 .env 中可能启用的真实千问分类器。
        classifier_node=classify_intent,
        # 复用刚建立的 Qdrant 内存索引。
        knowledge_retriever=retriever,
        # 每次最多召回三条，最终回答只采用前两条。
        faq_top_k=3,
    )

    # questions 选择当前知识库明确覆盖的发票与保修场景。
    questions = [
        # 应命中电子发票制度中的红冲重开规则。
        "发票税号写错了怎么办",
        # 应命中保修制度中的非免费保修情形。
        "人为损坏还能免费保修吗",
    ]
    # 按固定顺序执行问题，方便对照两条不同知识来源。
    for index, question in enumerate(questions, start=1):
        # ainvoke 执行完整状态图；统一分类包装节点采用异步签名以兼容真实 LLM。
        result = await graph.ainvoke(
            {
                # 每条问题使用唯一固定请求 ID，便于阅读事件轨迹。
                "request_id": f"rag-example-{index}",
                # 公共 FAQ 当前不区分用户知识权限，但仍保留完整身份字段。
                "user_id": "user-001",
                # 写入本轮自然语言问题。
                "user_message": question,
                # 入口事件将与后续节点事件通过 Reducer 累积。
                "events": ["example:started"],
            }
        )

        # 打印问题分隔标题，便于在控制台快速定位一次完整运行。
        print(f"\n=== 问题 {index}：{question} ===")
        # 打印最高余弦相似度，观察证据阈值的实际数值。
        print(f"最高检索分数：{result.get('retrieval_score')}")
        # 打印只基于审核知识切片构造的回答。
        print(f"回答：\n{result['answer']}")
        # 打印 Citation Pydantic 对象的 JSON 友好字典。
        print("引用：")
        pprint(
            [
                # mode=json 会把 effective_date 转换为 ISO 日期字符串。
                citation.model_dump(mode="json")
                # citations 只包含回答实际使用的证据。
                for citation in result.get("citations", [])
            ]
        )
        # 打印 LangGraph 节点执行轨迹，观察两次条件路由。
        print("事件：")
        pprint(result["events"])

    # 直接查询一个明显不相关的问题，验证向量阈值会返回空证据。
    unrelated_hits = retriever.search("今天天气如何", top_k=3)
    # 空列表表示 RAG 证据门不会允许系统回答天气问题。
    print(f"\n无关问题检索结果：{unrelated_hits}")


# 只有直接运行本文件时才执行示例，被测试或导入时不会产生控制台输出。
if __name__ == "__main__":
    # 创建并关闭事件循环；本地 Qdrant 和 Hash Embedding 仍不会访问外部网络。
    asyncio.run(main())

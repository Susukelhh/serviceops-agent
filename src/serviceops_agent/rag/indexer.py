"""独立 Qdrant 初始建索引入口，供 Docker Compose 一次性任务调用。"""

# Settings/get_settings 读取与两只 Agent 完全相同的知识源、切片和 Qdrant 配置。
from serviceops_agent.config.settings import Settings, get_settings

# build_default_knowledge_retriever 负责治理过滤、切片、向量化和幂等写入。
from serviceops_agent.rag.retriever import build_default_knowledge_retriever


def run_knowledge_indexer(settings: Settings | None = None) -> None:
    """在 API 副本启动前完成一次共享知识索引初始化与可读性验证。"""

    # 显式注入 settings 便于无网络测试；Compose 运行时读取环境变量中的 Qdrant URL。
    current_settings = settings or get_settings()
    # 构建默认检索器时会幂等创建 Collection，并补齐当前已发布公共知识切片。
    retriever = build_default_knowledge_retriever(current_settings)
    # 建库后再读取 Collection 元数据，防止只完成写请求却没有真正达到可服务状态。
    retriever.health_check()
    # 固定 PASS 前缀供 GitHub Actions 和运维日志明确判断一次性任务成功。
    print(
        "PASS: ServiceOps knowledge index is ready "
        f"(collection={current_settings.qdrant_collection})",
        # 立即刷新输出，保证容器快速退出时日志仍能被采集。
        flush=True,
    )


def main() -> None:
    """模块命令入口；未捕获异常会让容器以非零状态退出并阻止 Agent 启动。"""

    # 保持失败快速：Qdrant、知识文件或Embedding出错时不能伪装成成功建库。
    run_knowledge_indexer()


# 支持 ``python -m serviceops_agent.rag.indexer`` 直接运行一次性任务。
if __name__ == "__main__":
    # 调用薄入口，便于测试直接覆盖上面的可注入函数。
    main()

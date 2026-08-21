"""第22步架构图、白板资料和过期描述防回归测试。"""

# Path 让辅助函数接收明确文件路径并读取 UTF-8 文本。
from pathlib import Path

# resolve_project_path 保证测试从任意工作目录都能找到版本控制文档。
from serviceops_agent.config.paths import resolve_project_path


def _read_project_text(relative_path: str) -> str:
    """读取一份项目文档，并先断言它不是空壳文件。"""

    # 所有相对路径以真实项目根目录解析，避免依赖 pytest 启动位置。
    document_path: Path = resolve_project_path(relative_path)
    # 架构交付必须真实存在于仓库，而不是只在聊天消息中出现。
    assert document_path.is_file()
    # UTF-8 保持 Mermaid 中文节点与 HTML 白板文案一致。
    content = document_path.read_text(encoding="utf-8")
    # 100 个字符是有限空壳保护，不把具体排版长度当架构质量指标。
    assert len(content) > 100
    return content


def test_authoritative_architecture_contains_three_explainable_diagrams() -> None:
    """权威文档必须同时回答系统构成、写请求时序和部署通信三个问题。"""

    # Arrange/Act：读取第22步重构后的唯一权威架构说明。
    architecture = _read_project_text("docs/architecture.md")

    # Assert：恰好三张 Mermaid 图，避免恢复成一张塞满全部细节的巨图。
    assert architecture.count("```mermaid") == 3
    assert "flowchart TB" in architecture
    assert "sequenceDiagram" in architecture
    assert "flowchart LR" in architecture
    # 系统总览必须出现五层，而不是只画 LangGraph 内部节点。
    for layer_label in (
        "角色与入口",
        "接入与安全边界",
        "LangGraph 决策与控制流",
        "确定性执行与数据",
        "工程治理与交付",
    ):
        assert layer_label in architecture
    # 高风险时序必须明确展示暂停、恢复、业务事务和审计投递。
    for required_term in (
        "interrupt",
        "Command.resume",
        "业务库 + Outbox",
        "业务表仍为零写入",
        "workflow_completed",
    ):
        assert required_term in architecture
    # 部署图必须准确反映当前 Compose 的双实例、迁移任务和数据库边界。
    for required_component in (
        "Nginx gateway",
        "agent-a",
        "agent-b",
        "Alembic upgrade head",
        "PostgreSQL 18.4",
        "不映射 5432",
    ):
        assert required_component in architecture


def test_architecture_does_not_restore_known_obsolete_limitations() -> None:
    """已经完成的能力不能在架构文档尾部继续被描述成尚未实现。"""

    # Arrange：同一文档既负责已实现证据，也负责诚实生产边界。
    architecture = _read_project_text("docs/architecture.md")

    # Assert：下面三句来自旧文档历史残留，当前代码已经完成相应能力。
    obsolete_claims = (
        "尚未实现入口限流",
        "尚未完成正式 Schema Migration",
        "尚未完成备份恢复演练",
    )
    assert not any(claim in architecture for claim in obsolete_claims)
    # 新文档仍要保留真实差距，防止修正过期描述时变成无边界宣传。
    for honest_gap in (
        "企业 OIDC/JWKS",
        "Collector、Dashboard、SLO",
        "WAL 归档和 PITR",
        "Checkpoint 保留、归档和合规删除策略",
    ):
        assert honest_gap in architecture


def test_readme_and_whiteboard_expose_the_same_five_layer_story() -> None:
    """README、白板指南和打印速查图应使用同一套架构语言。"""

    # Arrange：读取求职入口、白板步骤和长期速查资料。
    readme = _read_project_text("README.md")
    whiteboard = _read_project_text("docs/architecture/interview-whiteboard.md")
    reference = _read_project_text("reference/serviceops-architecture-whiteboard.html")
    lesson = _read_project_text("lessons/0015-read-serviceops-architecture-in-five-boxes.html")

    # Assert：README 首屏必须存在小型 Mermaid 总览与两个深读入口。
    assert "## 架构总览" in readme
    assert "Nginx + FastAPI" in readme
    assert "docs/architecture.md" in readme
    assert "docs/architecture/interview-whiteboard.md" in readme
    # 白板版使用五盒、三分支、两治理边，适合 90 秒手画。
    assert "先画五个横向盒子" in whiteboard
    assert "再从 LangGraph 画三条分支" in whiteboard
    assert "最后补上两条虚线治理边" in whiteboard
    # HTML 资料必须离线可读，不依赖第三方脚本或把 Token 放入页面。
    assert "五层主干 · 三条业务分支 · 三类数据" in reference
    assert "用五个盒子读懂 Agent 架构" in lesson
    assert "<script" not in reference
    assert "<script" not in lesson
    assert "eyJhbGci" not in reference
    assert "eyJhbGci" not in lesson

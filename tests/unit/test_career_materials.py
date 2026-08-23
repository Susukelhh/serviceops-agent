"""第23步岗位调研、简历证据和诚实边界防回归测试。"""

# Path 用于把项目相对路径标注为明确的文件路径类型。
from pathlib import Path

# pytest 允许公开 CI 在没有私人简历时只跳过对应材料测试。
import pytest

# resolve_project_path 保证从 PyCharm、命令行或 CI 启动时都能找到项目文件。
from serviceops_agent.config.paths import resolve_project_path


def _read_required_material(relative_path: str) -> str:
    """读取一份必需的求职材料，并拒绝不存在或内容过少的空壳文件。"""

    # 将仓库内的相对路径转换成不依赖当前工作目录的绝对路径。
    material_path: Path = resolve_project_path(relative_path)
    # career/resume 受 .gitignore 保护，GitHub 干净检出中按设计不存在。
    if relative_path.startswith("career/resume/") and not material_path.is_file():
        # 只跳过依赖私人材料的测试；生产代码、公开文档和其他质量门继续完整执行。
        pytest.skip("私人简历目录未进入公开仓库，跳过仅适用于本机的材料一致性检查")
    # 求职交付必须真实落在仓库中，不能只存在于对话消息里。
    assert material_path.is_file()
    # 所有中文 Markdown 文档都统一使用 UTF-8，避免 Windows 默认编码造成乱码。
    content: str = material_path.read_text(encoding="utf-8")
    # 500 字符只用于挡住空壳占位文件，不把文档越长误认为质量越高。
    assert len(content) > 500
    # 返回文本给各测试继续验证真实内容和诚实边界。
    return content


def test_resume_keeps_unknown_personal_information_as_placeholders() -> None:
    """未知个人信息必须保留占位符，不能为了生成完整简历而虚构。"""

    # Arrange/Act：读取准备给用户继续填写的一页式中文简历初稿。
    resume: str = _read_required_material(
        "career/resume/serviceops-agent-resume-draft.md"
    )

    # Assert：姓名、学校、联系方式和毕业时间都必须等待用户提供真实值。
    for placeholder in (
        "[姓名]",
        "[手机号]",
        "[常用邮箱]",
        "[学校名称]",
        "[预计毕业年月]",
    ):
        # 每个占位符缺失都可能意味着材料误填了未经确认的个人信息。
        assert placeholder in resume
    # 简历不能主动把“延毕”贴在姓名或求职方向上，只能在使用提醒中解释处理原则。
    assert "**延毕**" not in resume


def test_resume_metrics_match_versioned_project_evidence() -> None:
    """项目结果必须带样本范围，不能把本地小型评测包装成线上指标。"""

    # Arrange：简历负责短表达，证据地图负责完整限制条件。
    resume: str = _read_required_material(
        "career/resume/serviceops-agent-resume-draft.md"
    )
    # 证据地图必须能解释数字来源和不能外推的原因。
    evidence: str = _read_required_material("career/resume/project-evidence-map.md")

    # Assert：离线与真实千问结果必须保留样本数、轮数和实验版本。
    assert "13 条版本化 Agent 回归集" in resume
    assert "`qwen-plus` 候选模型 1.1.0 连续 3 轮复测" in resume
    assert "每轮 13/13" in resume
    assert "小型黄金集" in resume
    # 当前完整门禁数字可以写，但必须明确它是本地测试结果。
    # 加入一次性知识索引任务契约后，本地完整测试总数更新为203。
    assert "当前本地测试为 203 passed、2 skipped" in resume
    # 证据地图必须明确禁止把小样本结果外推为永久或线上 100%。
    assert "不能说“Agent 准确率永久 100%”" in evidence
    assert "不外推线上准确率" in evidence


def test_career_materials_do_not_claim_unimplemented_production_capabilities() -> None:
    """求职材料必须区分当前实现、后续学习和真实生产经历。"""

    # Arrange：研究报告给出后续技术，证据地图划定简历陈述红线。
    research: str = _read_required_material(
        "career/research/hangzhou-agent-job-requirements-2026-08.md"
    )
    # 简历证据地图必须明确列出尚未完成和不可夸大的内容。
    evidence: str = _read_required_material("career/resume/project-evidence-map.md")

    # Assert：常见招聘关键词可以作为未来方向，但不能被误写为项目已实现。
    for future_skill in ("MCP", "Kubernetes", "Redis", "Reranker"):
        # 研究报告应承认并安排这些真实缺口。
        assert future_skill in research
        # 证据地图应明确阻止简历冒充已经完成。
        assert future_skill in evidence
    # 当前项目没有企业线上客户，因此必须保留明确的非生产边界。
    assert "不能说“已在某企业生产上线”" in evidence
    assert "生产导向的个人求职项目" in evidence

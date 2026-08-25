"""验证第30步公开安全、冻结证据和发布验收规则。"""

# json检查可提交实验摘要字段与隐私边界。
import json

# Path声明项目文件路径。
from pathlib import Path

# PROJECT_ROOT指向本测试共享的真实教学项目。
from serviceops_agent.config.paths import PROJECT_ROOT

# 发布验收公共函数是本文件验证对象。
from serviceops_agent.portfolio import run_release_readiness_audit


def test_sanitized_end_to_end_result_preserves_metrics_without_private_payloads() -> None:
    """公开摘要应保留简历指标，但不能包含问题、答案、向量或API Key。"""

    # result_path属于Git可提交证据目录，不是被忽略的runtime。
    result_path: Path = (
        PROJECT_ROOT
        / "data/evaluation/results/rag_end_to_end_v1_frozen_result.json"
    )
    # 解析版本化JSON。
    result = json.loads(result_path.read_text(encoding="utf-8"))
    # 锁定候选必须明确通过。
    candidate_holdout = result["holdout"]["candidate"]
    assert candidate_holdout["quality_gate_passed"] is True
    # 12条小型锁定集规模不能被隐藏。
    assert candidate_holdout["total_cases"] == 12
    assert candidate_holdout["answerable_cases"] == 8
    assert candidate_holdout["unanswerable_cases"] == 4
    # 核心简历指标来自公开摘要。
    assert candidate_holdout["unsupported_answer_rate"] == 0.0
    assert result["holdout"]["baseline"]["unsupported_answer_rate"] == 0.5
    # 顶层和四份汇总都不能保存逐题正文或模型答案。
    serialized = json.dumps(result, ensure_ascii=False).lower()
    assert '"question"' not in serialized
    assert '"answer"' not in serialized
    assert '"embedding_vectors"' not in serialized
    assert "sk-" not in serialized


def test_release_audit_passes_secret_ignore_link_and_frozen_evidence_checks() -> None:
    """自动审计应证明秘密被排除、README链接有效且冻结证据一致。"""

    # 快速模式不重复运行全部质量门。
    report = run_release_readiness_audit(PROJECT_ROOT, run_quality_gates=False)
    # 按稳定check_id读取结果，允许用户之后补提交、远程和许可证。
    by_id = {check.check_id: check for check in report.checks}
    # 当前公开候选集合必须非空。
    assert report.candidate_public_file_count > 0
    # 高置信秘密扫描必须通过。
    assert by_id["public_secret_scan"].status == "pass"
    # .env、runtime、backup、IDE和虚拟环境必须受忽略规则保护。
    assert by_id["private_paths_ignored"].status == "pass"
    # README相对链接必须真实存在。
    assert by_id["readme_local_links"].status == "pass"
    # 端到端摘要必须与冻结指纹和12条锁定规模一致。
    assert by_id["frozen_rag_evidence"].status == "pass"


def test_frozen_public_evidence_is_checked_out_with_lf_line_endings() -> None:
    """参与原始字节SHA校验的公开数据必须在Windows和Linux都保持LF。"""

    # Git属性是跨平台检出契约；没有它时Windows可能把LF静默改成CRLF。
    attributes_path = PROJECT_ROOT / ".gitattributes"
    # 文件缺失就无法保证干净克隆后的冻结SHA仍与公开配置一致。
    assert attributes_path.is_file()
    # 去掉空行和注释后再检查两条真正生效的规则，避免注释文字造成假通过。
    rules = {
        line.strip()
        for line in attributes_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    # 困难RAG知识语料被第34至38步配置按原始字节冻结，必须单独锁定LF。
    assert "data/experiments/rag_v2/knowledge_documents.json text eol=lf" in rules
    # 第39至40步还会校验多个公开配置与结果JSON，因此整个评测目录统一保持LF。
    assert "data/evaluation/** text eol=lf" in rules

"""第30步：离线检查GitHub公开安全、实验证据、文档链接和发布前人工事项。"""

# json读取冻结实验摘要。
import json

# re扫描常见密钥形态、简历占位符和Markdown链接。
import re

# shutil查找当前环境中的uv命令。
import shutil

# subprocess只执行本机Git和可选质量门，不访问远程仓库。
import subprocess

# sys.executable保证质量门使用启动本脚本的同一Python解释器。
import sys

# Literal限制检查状态；Sequence接收稳定命令参数。
from collections.abc import Sequence

# Path统一处理Windows和其他平台路径。
from pathlib import Path
from typing import Literal

# Pydantic生成强类型、可保存的验收报告。
from pydantic import BaseModel, Field

# CheckStatus只有通过、提醒和阻断三种结果，便于用户快速理解。
type CheckStatus = Literal["pass", "warning", "blocker"]

# 可公开文本扩展名白名单避免读取图片、数据库和其他二进制文件。
PUBLIC_TEXT_SUFFIXES: frozenset[str] = frozenset(
    {
        ".css",
        ".dockerignore",
        ".env",
        ".example",
        ".gitignore",
        ".html",
        ".ini",
        ".json",
        ".md",
        ".py",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
)

# 高置信密钥模式只记录文件和行号，从不把匹配值写入报告。
HIGH_CONFIDENCE_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "openai_compatible_api_key",
        re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "private_key_material",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    (
        "github_personal_access_token",
        re.compile(r"\bgh[opusr]_[A-Za-z0-9]{30,}\b"),
    ),
)

# 这些文本明确表示示例或占位值，不应被当成真实秘密。
PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "replace",
    "placeholder",
    "example",
    "请替换",
    "请填写",
    "只保存在本机",
    "serviceops-local-dev-password",
    "<",
    "${",
)


class ReleaseReadinessCheck(BaseModel):
    """一项发布检查的状态、证据与修复建议。"""

    # check_id是测试和报告使用的稳定英文标识。
    check_id: str = Field(min_length=1, max_length=100)
    # title是面向初学者的中文检查名称。
    title: str = Field(min_length=1, max_length=200)
    # status区分通过、提醒和真正阻断公开发布的问题。
    status: CheckStatus
    # detail只描述结果，不包含密钥值、问题正文或用户数据。
    detail: str = Field(min_length=1, max_length=2000)
    # remediation给出用户下一步可以执行的动作；通过项可以为空。
    remediation: str = Field(default="", max_length=2000)


class ReleaseReadinessReport(BaseModel):
    """第30步所有自动检查和人工事项的聚合报告。"""

    # report_version在检查规则改变时递增。
    report_version: str
    # project_root记录审计目标的绝对路径。
    project_root: str
    # candidate_public_file_count是Git未来会纳入提交的文件数量。
    candidate_public_file_count: int = Field(ge=0)
    # 三类计数支持控制台快速摘要。
    passed_checks: int = Field(ge=0)
    warning_checks: int = Field(ge=0)
    blocker_checks: int = Field(ge=0)
    # ready_for_public_push只有无blocker时为True。
    ready_for_public_push: bool
    # checks保存逐项证据和修复动作。
    checks: list[ReleaseReadinessCheck]


def _run_command(
    command: Sequence[str],
    *,
    project_root: Path,
) -> subprocess.CompletedProcess[str]:
    """在项目根执行只读命令并捕获文本，不把输出直接打印。"""

    # subprocess不启用shell，避免文件内容被解释成命令。
    return subprocess.run(
        list(command),
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _candidate_public_files(project_root: Path) -> list[Path]:
    """让Git按.gitignore规则列出已跟踪和准备跟踪的公开候选文件。"""

    # --cached包含已经跟踪文件；--others包含尚未第一次提交的项目文件。
    result = _run_command(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        project_root=project_root,
    )
    # 非Git目录无法安全判断公开集合，交由调用方单独报告。
    if result.returncode != 0:
        # 空列表避免退化为扫描整个磁盘。
        return []
    # -z使用空字符分隔，安全支持带空格路径。
    relative_paths = [item for item in result.stdout.split("\0") if item]
    # resolve把每项锚定项目根；这里只读取Git明确返回的路径。
    return [(project_root / relative_path).resolve() for relative_path in relative_paths]


def _is_text_candidate(path: Path) -> bool:
    """根据文件名和扩展名判断是否适合UTF-8文本扫描。"""

    # Dockerfile没有扩展名，但属于公开文本配置。
    if path.name in {"Dockerfile", "LICENSE", "NOTICE"}:
        # 允许进入文本检查。
        return True
    # 多后缀文件如.env.example只要任一后缀在白名单即可。
    return any(suffix.lower() in PUBLIC_TEXT_SUFFIXES for suffix in path.suffixes)


def _scan_public_secrets(paths: Sequence[Path], project_root: Path) -> list[str]:
    """扫描高置信秘密和非占位敏感赋值，只返回位置与原因。"""

    # findings不保存匹配内容本身。
    findings: list[str] = []
    # assignment_pattern只匹配以敏感字段结尾的环境变量，不匹配TOKEN_MINUTES等普通参数。
    assignment_pattern = re.compile(
        r"^\s*[A-Z0-9_]*(?:API_KEY|SECRET_KEY|PASSWORD|ACCESS_TOKEN|REFRESH_TOKEN)"
        r"\s*=\s*(.+?)\s*$"
    )
    # 逐个公开候选文本检查。
    for path in paths:
        # 目录、消失文件和二进制候选跳过。
        if not path.is_file() or not _is_text_candidate(path):
            # 继续下一个路径。
            continue
        try:
            # errors=replace保证偶发非UTF-8字符不会终止整个审计。
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            # 无法读取由其他检查或Git状态处理，不在秘密结果伪造结论。
            continue
        # 相对路径不会暴露用户目录名。
        relative_path = path.relative_to(project_root).as_posix()
        # 按行定位问题，方便修复。
        for line_number, line in enumerate(content.splitlines(), start=1):
            # 先检查高置信格式。
            for reason, pattern in HIGH_CONFIDENCE_SECRET_PATTERNS:
                # 命中时只记录原因，不记录秘密文本。
                if pattern.search(line):
                    # 保存稳定位置。
                    findings.append(f"{relative_path}:{line_number}:{reason}")
            # 只有配置文件才做赋值启发式；Python变量和Markdown示例会产生大量误报。
            config_assignment_candidate = (
                path.name.endswith((".env", ".env.example"))
                or path.suffix.lower() in {".ini", ".toml", ".yaml", ".yml"}
            )
            # 非配置文本已经完成高置信格式扫描，直接进入下一行。
            if not config_assignment_candidate:
                # 不把源码变量名误当作提交的秘密值。
                continue
            # 再检查环境变量风格的敏感赋值。
            assignment = assignment_pattern.match(line)
            # 没有赋值时进入下一行。
            if assignment is None:
                # 不执行低置信扫描。
                continue
            # value只用于本地判断，绝不写入报告。
            value = assignment.group(1).strip().strip('"\'')
            # 空值和明确占位符是安全示例。
            if not value or any(marker.lower() in value.lower() for marker in PLACEHOLDER_MARKERS):
                # 当前行无需报告。
                continue
            # 注释行不会被正则开头匹配；这里剩下的是可疑非空真实赋值。
            findings.append(f"{relative_path}:{line_number}:non_placeholder_sensitive_assignment")
    # 去重并排序使多次运行结果稳定。
    return sorted(set(findings))


def _check_ignored_private_paths(project_root: Path) -> ReleaseReadinessCheck:
    """确认密钥、运行数据、IDE和虚拟环境不会进入普通Git提交。"""

    # required_paths覆盖当前项目最重要的本地私有目录。
    required_paths = [
        ".env",
        "data/runtime/rag_end_to_end_experiment_report.json",
        "data/backups/example.dump",
        ".idea/workspace.xml",
        ".venv/Scripts/python.exe",
    ]
    # check-ignore返回被忽略路径；-z避免Windows换行被误当成路径字符。
    result = subprocess.run(
        ["git", "check-ignore", "-z", "--stdin"],
        cwd=project_root,
        input="\0".join(required_paths) + "\0",
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    # stdout以空字符分隔成功匹配忽略规则的路径。
    ignored = {path for path in result.stdout.split("\0") if path}
    # missing找出仍可能进入Git的私有路径。
    missing = [path for path in required_paths if path not in ignored]
    # 全部忽略时通过。
    if not missing:
        # 返回不包含任何真实内容的证据。
        return ReleaseReadinessCheck(
            check_id="private_paths_ignored",
            title="本地秘密与运行数据排除",
            status="pass",
            detail=".env、runtime、backups、IDE目录和虚拟环境均受.gitignore保护。",
        )
    # 任一缺失都会阻断公开推送。
    return ReleaseReadinessCheck(
        check_id="private_paths_ignored",
        title="本地秘密与运行数据排除",
        status="blocker",
        detail="以下私有路径没有被Git忽略：" + "、".join(missing),
        remediation="先修复.gitignore，再执行任何git add。",
    )


def _check_markdown_links(project_root: Path) -> ReleaseReadinessCheck:
    """检查README中的相对文件链接是否指向现有文件。"""

    # README是GitHub访客的第一入口。
    readme_path = project_root / "README.md"
    # 缺README直接阻断发布。
    if not readme_path.is_file():
        # 返回明确修复动作。
        return ReleaseReadinessCheck(
            check_id="readme_local_links",
            title="README本地链接",
            status="blocker",
            detail="项目根目录缺少README.md。",
            remediation="创建README并提供运行、架构、演示和证据入口。",
        )
    # markdown_link_pattern提取普通Markdown链接目标。
    markdown_link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    # missing保存不存在的相对目标。
    missing: list[str] = []
    # 遍历所有链接目标。
    for raw_target in markdown_link_pattern.findall(
        readme_path.read_text(encoding="utf-8", errors="replace")
    ):
        # 去掉可能的尖括号包装和锚点。
        target = raw_target.strip().strip("<>").split("#", maxsplit=1)[0]
        # 网络、邮件、纯锚点和空目标不属于本地文件。
        if not target or re.match(r"^(?:https?://|mailto:)", target):
            # 跳过外部链接；网络有效性不在离线脚本职责内。
            continue
        # GitHub相对链接以README所在目录为基准。
        resolved_target = (readme_path.parent / target).resolve()
        # 不存在时记录原始相对写法。
        if not resolved_target.exists():
            # 报告不使用绝对用户路径。
            missing.append(raw_target)
    # 没有断链时通过。
    if not missing:
        # 给出扫描范围。
        return ReleaseReadinessCheck(
            check_id="readme_local_links",
            title="README本地链接",
            status="pass",
            detail="README中的相对文件链接均可在项目内解析。",
        )
    # 断链影响GitHub演示，但不泄露秘密，因此列出目标即可。
    return ReleaseReadinessCheck(
        check_id="readme_local_links",
        title="README本地链接",
        status="blocker",
        detail="README存在无效相对链接：" + "、".join(sorted(set(missing))),
        remediation="修复路径或补齐目标文件后再公开。",
    )


def _check_frozen_evidence(project_root: Path) -> ReleaseReadinessCheck:
    """验证可提交的端到端摘要与冻结配置保持一致。"""

    # config_path和result_path都属于未来Git公开集合。
    config_path = project_root / "data/evaluation/rag_end_to_end_experiment.json"
    result_path = (
        project_root / "data/evaluation/results/rag_end_to_end_v1_frozen_result.json"
    )
    # 缺任一文件都无法让面试官复核指标。
    if not config_path.is_file() or not result_path.is_file():
        # 阻断简历数字公开。
        return ReleaseReadinessCheck(
            check_id="frozen_rag_evidence",
            title="端到端RAG冻结证据",
            status="blocker",
            detail="缺少版本化实验配置或脱敏冻结结果摘要。",
            remediation="补齐配置和不含用户/密钥内容的结果摘要。",
        )
    # 解析两份本地JSON。
    config = json.loads(config_path.read_text(encoding="utf-8"))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    # fingerprint_match防止摘要来自另一个候选版本。
    fingerprint_match = (
        config.get("frozen_candidate_fingerprint") == result.get("candidate_fingerprint")
    )
    # holdout_candidate只读取聚合结果，不读取问题正文。
    holdout_candidate = result.get("holdout", {}).get("candidate", {})
    # 关键质量门必须为真，样本规模必须与配置一致。
    gate_match = bool(holdout_candidate.get("quality_gate_passed")) and (
        holdout_candidate.get("total_cases") == config.get("holdout_case_count")
    )
    # 两项都匹配时通过。
    if fingerprint_match and gate_match:
        # detail包含可公开规模，不夸大线上能力。
        return ReleaseReadinessCheck(
            check_id="frozen_rag_evidence",
            title="端到端RAG冻结证据",
            status="pass",
            detail="脱敏摘要与冻结指纹一致，记录12条锁定题质量门PASS及结果边界。",
        )
    # 不一致可能让简历引用错误版本，必须阻断。
    return ReleaseReadinessCheck(
        check_id="frozen_rag_evidence",
        title="端到端RAG冻结证据",
        status="blocker",
        detail="冻结指纹、锁定样本规模或质量门与公开摘要不一致。",
        remediation="重新核对原始runtime报告；不要手工修改指标来消除差异。",
    )


def _quality_gate_check(
    *,
    check_id: str,
    title: str,
    command: Sequence[str],
    project_root: Path,
) -> ReleaseReadinessCheck:
    """运行一项本地质量命令并只报告退出状态和末尾摘要。"""

    # 执行不含用户输入拼接的固定命令。
    result = _run_command(command, project_root=project_root)
    # 合并标准输出与错误输出，再取最后一个非空行作为简短证据。
    output_lines = [
        line.strip()
        for line in (result.stdout + "\n" + result.stderr).splitlines()
        if line.strip()
    ]
    # 没有输出时使用退出码作为证据。
    summary = output_lines[-1] if output_lines else f"退出码 {result.returncode}"
    # 成功返回pass。
    if result.returncode == 0:
        # 不把完整测试日志塞入报告。
        return ReleaseReadinessCheck(
            check_id=check_id,
            title=title,
            status="pass",
            detail=summary[:1000],
        )
    # 失败会阻断公开版本声明。
    return ReleaseReadinessCheck(
        check_id=check_id,
        title=title,
        status="blocker",
        detail=summary[:1000],
        remediation="在PyCharm终端单独运行对应命令，修复后重新验收。",
    )


def run_release_readiness_audit(
    project_root: Path,
    *,
    run_quality_gates: bool = False,
) -> ReleaseReadinessReport:
    """执行公开安全与证据审计；可选运行Ruff、Mypy、Pytest和依赖锁。"""

    # resolve得到稳定绝对根目录。
    root = project_root.resolve()
    # checks按用户理解顺序累积。
    checks: list[ReleaseReadinessCheck] = []
    # git_root_result验证当前目录确实由Git管理。
    git_root_result = _run_command(
        ["git", "rev-parse", "--show-toplevel"],
        project_root=root,
    )
    # 不是Git仓库时直接产生blocker，后续仍可检查文件本身。
    if git_root_result.returncode != 0:
        # 给出初始化动作但不自动执行。
        checks.append(
            ReleaseReadinessCheck(
                check_id="git_repository",
                title="Git仓库",
                status="blocker",
                detail="当前目录不是Git仓库。",
                remediation="确认目录后由你执行git init；脚本不会自动改变版本历史。",
            )
        )
    else:
        # 当前项目已经初始化Git。
        checks.append(
            ReleaseReadinessCheck(
                check_id="git_repository",
                title="Git仓库",
                status="pass",
                detail="项目根目录已初始化Git。",
            )
        )

    # public_files严格服从Git忽略规则。
    public_files = _candidate_public_files(root)
    # 扫描未来提交集合中的高置信秘密。
    secret_findings = _scan_public_secrets(public_files, root)
    # 无发现时通过。
    if not secret_findings:
        # 明确扫描数量和不外推边界。
        checks.append(
            ReleaseReadinessCheck(
                check_id="public_secret_scan",
                title="公开候选文件秘密扫描",
                status="pass",
                detail=f"扫描{len(public_files)}个Git公开候选文件，未发现高置信真实密钥格式。",
            )
        )
    else:
        # 只显示位置与原因，绝不打印值。
        checks.append(
            ReleaseReadinessCheck(
                check_id="public_secret_scan",
                title="公开候选文件秘密扫描",
                status="blocker",
                detail=("发现可疑位置：" + "、".join(secret_findings))[:2000],
                remediation="从文件和Git历史中移除秘密、轮换Key，再重新扫描。",
            )
        )
    # .gitignore保护单独检查，避免仅靠秘密格式扫描。
    checks.append(_check_ignored_private_paths(root))
    # README断链会破坏GitHub演示。
    checks.append(_check_markdown_links(root))
    # 冻结实验公开证据必须与配置一致。
    checks.append(_check_frozen_evidence(root))

    # rev-parse HEAD只读检查是否至少存在一次提交。
    head_result = _run_command(["git", "rev-parse", "--verify", "HEAD"], project_root=root)
    # 没有提交意味着所有文件仍未进入版本历史。
    if head_result.returncode != 0:
        # 这是公开推送前的真正阻断项，但提交必须由用户授权执行。
        checks.append(
            ReleaseReadinessCheck(
                check_id="git_initial_commit",
                title="Git首次提交",
                status="blocker",
                detail="仓库尚无任何提交，当前项目文件还没有版本快照。",
                remediation="先审查git status和秘密扫描结果，再由你创建首次提交。",
            )
        )
    else:
        # 至少一份可恢复版本快照存在。
        checks.append(
            ReleaseReadinessCheck(
                check_id="git_initial_commit",
                title="Git首次提交",
                status="pass",
                detail="仓库已经存在可定位的HEAD提交。",
            )
        )

    # origin只读检查是否已经配置GitHub等远程地址。
    origin_result = _run_command(
        ["git", "remote", "get-url", "origin"],
        project_root=root,
    )
    # 没有远程不影响本地完成度，但需要用户决定公开位置。
    if origin_result.returncode != 0:
        # 使用warning而非blocker，避免把外部账号操作混入代码质量。
        checks.append(
            ReleaseReadinessCheck(
                check_id="git_remote_origin",
                title="Git远程仓库",
                status="warning",
                detail="尚未配置origin远程仓库。",
                remediation="在GitHub创建空仓库后，由你确认地址并添加origin。",
            )
        )
    else:
        # 不把可能含用户名的完整URL写入报告。
        checks.append(
            ReleaseReadinessCheck(
                check_id="git_remote_origin",
                title="Git远程仓库",
                status="pass",
                detail="已经配置origin；报告为隐私起见不保存远程URL。",
            )
        )

    # 常见许可证文件只检查存在性，不替用户做法律选择。
    license_exists = any((root / name).is_file() for name in ("LICENSE", "LICENSE.md"))
    # 存在许可证时通过。
    if license_exists:
        # 不判断许可证法律适用性。
        checks.append(
            ReleaseReadinessCheck(
                check_id="license_choice",
                title="开源许可证",
                status="pass",
                detail="项目根目录存在许可证文件。",
            )
        )
    else:
        # 许可证是用户权利选择，因此只提醒不自动创建。
        checks.append(
            ReleaseReadinessCheck(
                check_id="license_choice",
                title="开源许可证",
                status="warning",
                detail="当前没有LICENSE文件；默认版权仍归作者，但他人没有明确复用授权。",
                remediation="公开前决定保留默认版权，或由你明确选择MIT、Apache-2.0等许可证。",
            )
        )

    # 简历个人信息必须由用户填写，脚本只检查明显占位符。
    resume_path = root / "career/resume/serviceops-agent-resume-draft.md"
    # 默认没有占位符。
    placeholder_count = 0
    # 只在简历存在时统计方括号占位表达。
    if resume_path.is_file():
        # 匹配中文模板中的[姓名]、[手机号]等短占位符。
        placeholder_count = len(
            re.findall(
                r"\[(?:姓名|手机号|常用邮箱|学校名称|专业名称|本科/硕士|GitHub[^\]]*|开始年月|预计毕业年月)[^\]]*\]",
                resume_path.read_text(encoding="utf-8", errors="replace"),
            )
        )
    # 有占位符时提醒，不阻断代码仓库公开。
    if placeholder_count:
        # detail只报告数量，不收集个人信息。
        checks.append(
            ReleaseReadinessCheck(
                check_id="resume_personal_placeholders",
                title="简历个人信息",
                status="warning",
                detail=f"简历仍有{placeholder_count}处明显个人信息占位符。",
                remediation="正式投递前由你填写真实信息；不要让AI猜测学校、日期或联系方式。",
            )
        )
    else:
        # 没有明显占位符时通过，但不验证真实性。
        checks.append(
            ReleaseReadinessCheck(
                check_id="resume_personal_placeholders",
                title="简历个人信息",
                status="pass",
                detail="未发现预设个人信息占位符；脚本不验证内容真实性。",
            )
        )

    # 用户显式要求时才运行耗时质量门。
    if run_quality_gates:
        # 使用当前解释器保证检查同一虚拟环境，而不是误用PATH中的全局Python。
        python_executable = sys.executable
        # 依次运行代码风格、类型和全部测试。
        checks.extend(
            [
                _quality_gate_check(
                    check_id="ruff_quality_gate",
                    title="Ruff代码质量门",
                    command=[python_executable, "-m", "ruff", "check", "."],
                    project_root=root,
                ),
                _quality_gate_check(
                    check_id="mypy_quality_gate",
                    title="Mypy类型质量门",
                    command=[python_executable, "-m", "mypy", "src"],
                    project_root=root,
                ),
                _quality_gate_check(
                    check_id="pytest_quality_gate",
                    title="Pytest回归质量门",
                    command=[python_executable, "-m", "pytest", "-q"],
                    project_root=root,
                ),
            ]
        )
        # uv可能没有加入当前IDE进程PATH。
        uv_executable = shutil.which("uv")
        # 找到uv时执行锁文件检查。
        if uv_executable:
            # 添加第四项质量门。
            checks.append(
                _quality_gate_check(
                    check_id="dependency_lock_gate",
                    title="依赖锁一致性",
                    command=[uv_executable, "lock", "--check"],
                    project_root=root,
                )
            )
        else:
            # 没有uv只给提醒，因为Python质量门仍可运行。
            checks.append(
                ReleaseReadinessCheck(
                    check_id="dependency_lock_gate",
                    title="依赖锁一致性",
                    status="warning",
                    detail="当前进程PATH中找不到uv，未自动执行uv lock --check。",
                    remediation="在PyCharm中选择已配置uv的运行环境，或在终端单独执行。",
                )
            )

    # 聚合三类计数。
    passed_checks = sum(check.status == "pass" for check in checks)
    warning_checks = sum(check.status == "warning" for check in checks)
    blocker_checks = sum(check.status == "blocker" for check in checks)
    # 无blocker才允许公开推送；warning仍需用户判断。
    ready_for_public_push = blocker_checks == 0
    # 返回可保存报告。
    return ReleaseReadinessReport(
        report_version="1.0.0",
        project_root=str(root),
        candidate_public_file_count=len(public_files),
        passed_checks=passed_checks,
        warning_checks=warning_checks,
        blocker_checks=blocker_checks,
        ready_for_public_push=ready_for_public_push,
        checks=checks,
    )

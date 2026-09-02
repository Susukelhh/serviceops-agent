"""GitHub Actions 触发边界、最小权限和 Action 固定策略的静态回归测试。"""

# re 只解析本项目受控的 uses 行，避免为两个断言引入额外 YAML 运行时依赖。
import re

# PROJECT_ROOT 让测试从 PyCharm、命令行和 Linux CI 都读取同一工作流位置。
from serviceops_agent.config.paths import PROJECT_ROOT

# 完整 SHA 必须是 40 个小写十六进制字符；版本注释不参与匹配。
PINNED_ACTION_PATTERN = re.compile(
    r"^\s*uses:\s*[^@\s]+@([0-9a-f]{40})\s*(?:#.*)?$",
    flags=re.MULTILINE,
)


def _load_workflow(name: str) -> str:
    """读取指定 UTF-8 工作流正文供有限静态安全断言使用。"""

    # 工作流名称由测试代码固定，不接收外部输入，不存在目录穿越边界。
    workflow_path = PROJECT_ROOT / ".github" / "workflows" / name
    # 明确 UTF-8，保证中文教学注释在 Windows 上可读取。
    return workflow_path.read_text(encoding="utf-8")


def _assert_all_actions_use_full_commit_sha(workflow: str) -> None:
    """断言所有 uses 行都固定完整 Action 提交 SHA。"""

    # 先收集所有 Action 引用行；当前工作流没有本地 ./ action。
    uses_lines = [line for line in workflow.splitlines() if line.strip().startswith("uses:")]
    # 至少存在 checkout/setup-uv，避免空工作流让 all(...) vacuously true。
    assert uses_lines
    # 每行都必须被 40 字符完整 SHA 模式覆盖，不能回退到 @main 或 @v7。
    assert all(PINNED_ACTION_PATTERN.fullmatch(line) for line in uses_lines)


# 第一条测试保证普通提交的强制门永远保持无密钥、离线和最小权限。
def test_offline_quality_workflow_never_reads_model_secret() -> None:
    """Push/PR 离线门不能读取 Secret 或启用真实模型后端。"""

    # Arrange：读取普通质量门工作流。
    workflow = _load_workflow("quality-gate.yml")

    # Assert：该工作流响应代码变更，但 GITHUB_TOKEN 只有只读内容权限。
    assert "push:" in workflow
    assert "pull_request:" in workflow
    assert "contents: read" in workflow
    assert "name: Ruff, Mypy, Pytest and Agent eval" in workflow
    # 任何 `${{ secrets.* }}` 都表示 PR 路径可能触达密钥，必须阻断。
    assert "secrets." not in workflow
    # 四个后端开关必须明确固定为零费用确定性实现。
    assert "SERVICEOPS_LLM_BACKEND: mock" in workflow
    assert "SERVICEOPS_AGENT_PLANNER_BACKEND: deterministic" in workflow
    assert "SERVICEOPS_EMBEDDING_BACKEND: hash" in workflow
    assert "SERVICEOPS_RAG_GENERATION_BACKEND: extractive" in workflow
    # 第48步多轮状态门必须与单轮整图门一起运行并保留独立低敏报告。
    assert "examples/48_conversation_stability_evaluation.py" in workflow
    assert "data/runtime/conversation_stability_report.json" in workflow
    assert "Run deterministic conversation stability gate" in workflow
    # 第58步必须使用固定摘要、无网络和非root promtool验证告警，再校验冻结哈希。
    assert "Run Prometheus shadow alert contract drill" in workflow
    assert "prom/prometheus@sha256:63805ebb8d2b3920" in workflow
    assert "--network none" in workflow
    assert "--read-only" in workflow
    assert "--user 65534:65534" in workflow
    assert "promtool" in workflow
    assert "shadow-alert-tests.yaml" in workflow
    assert "examples/57_shadow_release_drill.py" in workflow
    assert "examples/58_verify_shadow_alert_drill_evidence.py" in workflow
    assert "conversation_shadow_step57_release_drill.json" in workflow
    # 供应链 Action 全部使用不可变完整 SHA。
    _assert_all_actions_use_full_commit_sha(workflow)


# 第二条测试保证拥有千问 Secret 的工作流不会被普通提交或外部 PR 自动触发。
def test_qwen_workflow_is_manual_and_requires_paid_confirmation() -> None:
    """真实候选只能手工触发，并必须把付费确认传给脚本。"""

    # Arrange：读取真实候选工作流。
    workflow = _load_workflow("qwen-candidate-evaluation.yml")

    # Assert：只有手工事件，没有 push/pull_request 自动入口。
    assert "workflow_dispatch:" in workflow
    assert "\n  push:" not in workflow
    assert "\n  pull_request:" not in workflow
    # API Key 必须来自 GitHub Secret，模型名/Base URL 才允许使用普通 Variable。
    assert "secrets.SERVICEOPS_LLM_API_KEY" in workflow
    assert "vars.SERVICEOPS_LLM_MODEL" in workflow
    # 脚本自身的第二层付费保护不能从工作流命令中删除。
    assert "--confirm-paid-api" in workflow
    # 第49步共享会话真实候选仍只能位于同一个手工付费工作流。
    assert "examples/49_qwen_multi_turn_experiment.py" in workflow
    assert "qwen_multi_turn_experiment_report.json" in workflow
    assert "Run repeated Qwen multi-turn candidate experiment" in workflow
    # 第50步必须在失败时也重新校验并保存不可覆盖证据，且绑定Git revision。
    assert "examples/50_archive_qwen_multi_turn_evidence.py" in workflow
    assert 'if: ${{ always() && hashFiles(' in workflow
    assert 'source-revision "${{ github.sha }}"' in workflow
    assert "qwen-multi-turn-evidence-${{ github.run_id }}" in workflow
    assert "contents: read" in workflow
    # 所有拥有 Secret 的第三方 Action 同样必须固定完整 SHA。
    _assert_all_actions_use_full_commit_sha(workflow)


# 第三条测试保证容器镜像门不会读取模型密钥，并实际验证运行时权限和健康状态。
def test_container_workflow_builds_and_probes_multi_instance_topology() -> None:
    """容器门必须构建并验证迁移、双实例网关、权限、readiness 与认证。"""

    # Arrange：读取独立容器工作流，避免把 Docker 依赖加入普通 Python 质量门步骤。
    workflow = _load_workflow("container-image.yml")

    # Assert：普通 Push/PR 会触发镜像验证，但工作流不读取任何 Secret。
    assert "push:" in workflow
    assert "pull_request:" in workflow
    assert "contents: read" in workflow
    assert "secrets." not in workflow
    # 镜像要真实构建，Compose 必须启动完整多实例拓扑而不是退回单容器冒烟。
    assert "docker build --pull" in workflow
    assert "docker compose up --detach --no-build --wait" in workflow
    # 迁移和知识索引任务必须成功退出；运行容器仍需验证非root、只读和零Capability。
    assert "--status exited --services migrate" in workflow
    assert "--status exited --services index-knowledge" in workflow
    assert "PASS: ServiceOps knowledge index is ready" in workflow
    assert "10001:10001" in workflow
    assert "ReadonlyRootfs" in workflow
    assert "CapDrop" in workflow
    # 网关响应头与实例集合证明请求真实经过 Nginx 并到达 A/B。
    assert "X-ServiceOps-Gateway" in workflow
    assert 'seen == {"agent-a", "agent-b"}' in workflow
    # 平台必须等待真实 readiness，并验证业务接口未认证时返回 401。
    assert "http://127.0.0.1:8000/ready" in workflow
    assert 'payload["persistence_backend"] == "postgres"' in workflow
    assert '"knowledge_qdrant"' in workflow
    assert 'test "$status" = "401"' in workflow
    # 产品控制台必须随 wheel 进入镜像，并验证 HTML、安全 Header 与同源静态资源。
    assert "Verify packaged Agent console" in workflow
    assert "http://127.0.0.1:8000/console/" in workflow
    assert "content-security-policy:" in workflow
    assert "frame-ancestors 'none'" in workflow
    assert "/console/assets/console.css" in workflow
    assert "/console/assets/console.js" in workflow
    # 第20.1步教学调试器同样必须进入最终 wheel/镜像，不能只在本机源码中存在。
    assert 'id="developer-token-input" type="password"' in workflow
    assert "CHECKPOINT PLAYBACK" in workflow
    assert ".debug-inspector" in workflow
    assert "/api/v1/debug/threads/" in workflow
    # 失败清理必须包含Qdrant和建索引任务，否则共享索引问题会只剩“Agent unhealthy”。
    assert "index-knowledge qdrant postgres" in workflow
    # 第20.2步全宽工作台和专注大屏也必须真实进入最终容器资源。
    assert "Agent 执行回放工作台" in workflow
    assert 'id="debug-focus-button"' in workflow
    assert "body.debug-focus-mode" in workflow
    assert "setDebugFocusMode" in workflow
    # CI 还要真实制造入口突发，观察 429、拒绝 5xx，并在等待后恢复认证边界。
    assert "Verify gateway burst protection and recovery" in workflow
    assert '"429" /tmp/burst-statuses.txt' in workflow
    assert '"5[0-9][0-9]" /tmp/burst-statuses.txt' in workflow
    assert 'test "$recovered_status" = "401"' in workflow
    # 同一 Compose 临时数据库还要执行真实备份、隔离恢复、逐表比较与清理。
    assert "Verify PostgreSQL backup and isolated restore drill" in workflow
    assert "examples/19_postgres_backup_restore_drill.py" in workflow
    assert 'report["fingerprint_match"] is True' in workflow
    assert 'report["temporary_database_removed"] is True' in workflow
    # 唯一第三方 Action 与其他工作流一样固定到完整 SHA。
    _assert_all_actions_use_full_commit_sha(workflow)


# 第四条测试保证 PostgreSQL 真实集成门不会被误改成跳过或连接外部数据库。
def test_postgres_workflow_uses_ephemeral_database_without_secrets() -> None:
    """PostgreSQL 门必须启动临时数据库并显式运行专用跨重启测试。"""

    # Arrange：读取独立真实数据库工作流。
    workflow = _load_workflow("postgres-integration.yml")
    # Assert：普通提交可发现 SQL 回归，但测试完全不读取 GitHub Secret。
    assert "push:" in workflow
    assert "pull_request:" in workflow
    assert "contents: read" in workflow
    assert "secrets." not in workflow
    # 数据库镜像固定补丁版本/摘要，并用 pg_isready 判断真实可连接状态。
    assert "postgres:18.4-bookworm@sha256:" in workflow
    assert "pg_isready -U serviceops_test -d serviceops_test" in workflow
    # 专用 DSN 与测试文件名必须明确出现，不能只启动数据库却未覆盖真实仓储。
    assert "SERVICEOPS_TEST_POSTGRES_DSN" in workflow
    assert "tests/integration/test_postgres_runtime.py" in workflow
    # 所有第三方 Action 继续使用不可变完整提交 SHA。
    _assert_all_actions_use_full_commit_sha(workflow)

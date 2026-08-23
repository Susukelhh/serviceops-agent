"""容器构建、运行权限和敏感文件边界的静态回归测试。"""

# importlib.util 从 examples 文件加载演示模块，允许行为测试覆盖连接中断分支。
import importlib.util

# argparse 构造与命令行解析结果相同的 Namespace，测试不需要修改真实 sys.argv。
from argparse import Namespace

# RemoteDisconnected 模拟容器重启时服务端在响应前主动断开连接。
from http.client import RemoteDisconnected

# ModuleType 为动态加载结果提供明确类型，避免测试依赖不安全的 Any。
from types import ModuleType

# pytest 提供 monkeypatch 和 capsys 夹具的类型标注。
import pytest

# PROJECT_ROOT 保证 Windows PyCharm 与 Linux CI 读取同一仓库文件。
from serviceops_agent.config.paths import PROJECT_ROOT


def _load_text(name: str) -> str:
    """以 UTF-8 读取一个项目根目录文件。"""

    # 文件名由测试代码固定，不接受外部输入，因此不存在目录穿越。
    return (PROJECT_ROOT / name).read_text(encoding="utf-8")


def _load_container_smoke_module() -> ModuleType:
    """从 examples 目录加载容器冒烟脚本，供异常分支行为测试调用。"""

    # 示例没有作为 Python 包发布，因此使用绝对文件路径创建独立模块规格。
    script_path = PROJECT_ROOT / "examples" / "15_container_smoke_test.py"
    # 固定模块名只存在于当前测试加载器中，不会覆盖应用包模块。
    module_spec = importlib.util.spec_from_file_location("container_smoke_example", script_path)
    # 文件存在且使用标准 Python 加载器时，spec 和 loader 都必须可用。
    if module_spec is None or module_spec.loader is None:
        # 失败信息不包含机器敏感路径，只指出测试夹具无法加载。
        raise RuntimeError("无法加载容器冒烟示例")
    # 根据规格创建尚未执行的模块对象。
    smoke_module = importlib.util.module_from_spec(module_spec)
    # 执行示例顶层定义；__name__ 不是 __main__，因此不会真的发送 HTTP 请求。
    module_spec.loader.exec_module(smoke_module)
    # 返回模块，让测试替换其参数解析和请求函数。
    return smoke_module


def test_dockerfile_uses_reproducible_multistage_non_root_runtime() -> None:
    """最终镜像必须来自多阶段构建、固定基础镜像并以非 root 启动。"""

    # Arrange：读取受版本控制的 Dockerfile。
    dockerfile = _load_text("Dockerfile")
    # Assert：基础镜像同时固定补丁版本和 sha256 摘要，避免 latest/浮动小版本。
    assert "python:3.12.13-slim-trixie@sha256:" in dockerfile
    # 同一固定基础镜像分别建立 builder 与 runtime，构建工具不会进入最终镜像。
    assert dockerfile.count("FROM ${PYTHON_IMAGE}") == 2
    assert "AS builder" in dockerfile
    assert "AS runtime" in dockerfile
    # uv 必须使用锁文件安装生产依赖，项目本体不能以 editable 软链接运行。
    assert "uv sync --locked --no-dev --no-editable" in dockerfile
    # 最终运行用户采用明确非零 UID/GID，不能因用户名解析失败回退到 root。
    assert "USER 10001:10001" in dockerfile
    # 一个容器只运行一个 Uvicorn 进程；需要扩容时增加共享 PostgreSQL 的容器副本。
    assert '"--workers", "1"' in dockerfile
    # 镜像自身也使用真实 readiness 健康检查。
    assert "HEALTHCHECK" in dockerfile
    assert "http://127.0.0.1:8000/ready" in dockerfile


def test_build_context_excludes_secrets_and_runtime_state() -> None:
    """Docker 构建上下文不得包含本机密钥、虚拟环境或 SQLite 状态。"""

    # Arrange：忽略空行和注释，只比较实际规则。
    ignore_rules = {
        line.strip()
        for line in _load_text(".dockerignore").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    # Assert：下面三类内容分别代表密钥、机器相关依赖和业务运行状态。
    assert ".env" in ignore_rules
    assert ".venv/" in ignore_rules
    assert "data/runtime/" in ignore_rules


def test_compose_applies_local_only_and_least_privilege_boundaries() -> None:
    """本地 Compose 只暴露网关，并限制所有容器的写入与提权。"""

    # Arrange：这里做有限文本契约检查，不为测试配置额外引入 YAML 依赖。
    compose = _load_text("compose.yaml")
    # Assert：端口只绑定回环地址，避免开发 JWT 配置暴露到局域网。
    assert '"127.0.0.1:8000:8080"' in compose
    # 只有 gateway 拥有 ports；两只 Agent 只通过 Compose 内网 expose 8000。
    assert compose.count("ports:") == 1
    assert "agent-a:" in compose
    assert "agent-b:" in compose
    assert compose.count('SERVICEOPS_INSTANCE_ID: agent-') == 2
    # API 根文件系统只读，持久数据只进入独立 PostgreSQL 具名卷。
    assert "read_only: true" in compose
    assert "serviceops-postgres:/var/lib/postgresql" in compose
    assert "SERVICEOPS_PERSISTENCE_BACKEND: postgres" in compose
    assert "condition: service_healthy" in compose
    # API 必须等待一次性Alembic迁移和共享知识建索引成功，避免副本并发初始化。
    assert "condition: service_completed_successfully" in compose
    assert 'command: ["python", "-m", "serviceops_agent.infrastructure.migrate"]' in compose
    assert 'command: ["python", "-m", "serviceops_agent.rag.indexer"]' in compose
    assert "index-knowledge:" in compose
    # 数据库镜像同时固定补丁标签和内容摘要，避免同名标签被重新发布后静默变化。
    assert "postgres:18.4-bookworm@sha256:" in compose
    # 统一入口也固定 Nginx 补丁版本与镜像摘要。
    assert "nginx:1.30.4-alpine@sha256:" in compose
    # 数据库端口只在 Compose 内部声明，不映射为 Windows 或局域网端口。
    assert '"5432:5432"' not in compose
    # Linux Capability 全删且禁止获得新权限。
    assert "cap_drop:" in compose
    assert "- ALL" in compose
    assert "no-new-privileges:true" in compose
    # Compose 不注入千问 Key，并显式使用零费用确定性后端。
    assert "SERVICEOPS_LLM_API_KEY" not in compose
    assert "SERVICEOPS_LLM_BACKEND: mock" in compose
    assert "SERVICEOPS_AGENT_PLANNER_BACKEND: deterministic" in compose
    # 每只 Agent 的后端业务工位和短排队上限必须由 Compose 明确固定。
    assert "SERVICEOPS_AGENT_MAX_IN_FLIGHT_REQUESTS: 8" in compose
    assert "SERVICEOPS_AGENT_CAPACITY_QUEUE_TIMEOUT_SECONDS: 0.05" in compose
    # 平台健康状态必须来自 /ready，而不是只检测端口。
    assert "http://127.0.0.1:8000/ready" in compose


def test_nginx_gateway_round_robins_and_re_resolves_agent_instances() -> None:
    """网关应轮询两只 Agent，并能在容器重建换 IP 后重新发现实例。"""

    # Arrange：读取作为只读卷挂载到 gateway 的 Nginx 配置。
    nginx = _load_text("deploy/nginx/nginx.conf")
    # Assert：两只后端都在同一个默认轮询 upstream 中。
    assert "upstream serviceops_backend" in nginx
    assert "server agent-a:8000 resolve" in nginx
    assert "server agent-b:8000 resolve" in nginx
    # Docker DNS 与 resolve 组合保证容器 IP 改变后无需手改配置。
    assert "resolver 127.0.0.11" in nginx
    assert "zone serviceops_backend" in nginx
    # 日志可定位实际后端，但不会记录 Authorization 或请求正文。
    assert "upstream=$upstream_addr" in nginx
    assert "$http_authorization" not in nginx
    # 固定响应头让验证脚本证明请求确实经过统一入口。
    assert "X-ServiceOps-Gateway nginx" in nginx


def test_nginx_limits_only_business_rate_and_returns_explicit_429_json() -> None:
    """入口应保护 /api/，同时让健康检查和 Swagger 保持在限流区之外。"""

    # Arrange：读取实际挂载进 gateway 的唯一 Nginx 配置。
    nginx = _load_text("deploy/nginx/nginx.conf")
    # Assert：速率区和连接区都按二进制客户端地址计数，减少地址存储开销。
    assert "limit_req_zone $binary_remote_addr" in nginx
    assert "rate=5r/s" in nginx
    assert "limit_conn_zone $binary_remote_addr" in nginx
    # 只有 /api/ location 应用限制；通用 / location 继续承载 /health、/ready 和 /docs。
    api_location = nginx.split("location /api/ {", maxsplit=1)[1].split("}", maxsplit=1)[0]
    assert "limit_req zone=serviceops_api_rate burst=10 nodelay" in api_location
    assert "limit_conn serviceops_api_connections 20" in api_location
    # 超限明确返回 429、Retry-After 和固定 JSON，不把它伪装成后端 503。
    assert "limit_req_status 429" in nginx
    assert "limit_conn_status 429" in nginx
    assert "location @gateway_overloaded" in nginx
    assert "add_header Retry-After 1 always" in nginx
    assert "return 429" in nginx


def test_api_runtime_does_not_own_business_schema_ddl() -> None:
    """长期运行的 API 不能在每次启动时自行创建或修改业务表。"""

    # Arrange：读取运行时装配代码和 PostgreSQL 仓储实现。
    runtime = _load_text("src/serviceops_agent/infrastructure/runtime.py")
    repository = _load_text("src/serviceops_agent/infrastructure/postgres_repository.py")
    # Assert：旧的启动期建表入口已经删除，业务结构统一交给 Alembic revision。
    assert "setup_postgres_business_schema" not in runtime
    assert "setup_postgres_business_schema" not in repository
    # 初始版本脚本必须存在，并包含三张核心业务表。
    migration = _load_text(
        "src/serviceops_agent/migrations/versions/20260821_0001_initial_business_schema.py"
    )
    assert "return_requests" in migration
    assert "return_outbox_events" in migration
    assert "approval_audit_events" in migration


def test_smoke_example_reports_remote_disconnect_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """容器重启导致的连接中断应返回有限 FAIL，而不是向学习者打印堆栈。"""

    # Arrange：动态加载脚本，避免测试导入时访问真实的 8000 端口。
    smoke_module = _load_container_smoke_module()
    # 参数解析固定为本机地址，使 main 可以像真实命令一样继续执行。
    monkeypatch.setattr(
        smoke_module,
        "_parse_arguments",
        lambda: Namespace(base_url="http://127.0.0.1:8000"),
    )

    # 模拟服务端接受 TCP 后立刻关闭连接，这正是容器崩溃重启时观察到的异常。
    def raise_remote_disconnect(*args: object, **kwargs: object) -> None:
        # 固定短消息不会进入最终输出，因为脚本只打印异常类型。
        raise RemoteDisconnected("container restarted")

    # 所有 HTTP 请求都替换为确定性的连接中断，不依赖 Docker 是否正在运行。
    monkeypatch.setattr(smoke_module, "_request_json", raise_remote_disconnect)
    # Act：main 应把网络异常转换为标准失败退出码。
    exit_code = smoke_module.main()
    # Assert：失败使用退出码 1，便于本地终端和 CI 正确识别。
    assert exit_code == 1
    # Assert：只输出有限异常类型，不出现完整 Python traceback。
    assert capsys.readouterr().out.strip() == (
        "FAIL container connection: cause_type=RemoteDisconnected"
    )

"""第20步 Agent 可视化控制台的 HTTP、打包与前端安全边界测试。"""

# Path 读取随 Python 包发布的 HTML、CSS 和 JavaScript 资源。

# pytest 提供异步测试标记。
import pytest

# ASGITransport 直接调用 FastAPI，不依赖本机 8000 端口或 Docker。
from httpx import ASGITransport, AsyncClient

# app 是生产与测试共用的唯一 FastAPI 应用；CONSOLE_DIRECTORY 定位打包资源。
from serviceops_agent.api.app import CONSOLE_DIRECTORY, app


@pytest.mark.asyncio
async def test_root_redirects_to_canonical_agent_console() -> None:
    """服务根路径和无斜杠地址都应进入同一个控制台 URL。"""

    # Arrange：关闭自动跳转，才能直接断言后端设置的规范地址。
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        # Act：分别访问服务根路径和不带结尾斜杠的控制台路径。
        root_response = await client.get("/")
        console_response = await client.get("/console")

    # Assert：307 不会擅自改变未来非 GET 方法语义。
    assert root_response.status_code == 307
    assert console_response.status_code == 307
    # Assert：统一斜杠保证相对静态资源解析稳定。
    assert root_response.headers["location"] == "/console/"
    assert console_response.headers["location"] == "/console/"


@pytest.mark.asyncio
async def test_console_html_has_strict_security_headers_and_no_embedded_tokens() -> None:
    """控制台 HTML 必须可访问、不可被 iframe 嵌入且不包含任何演示凭证。"""

    # Arrange：进程内访问真实 FileResponse 路由。
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # Act：读取完整控制台文档。
        response = await client.get("/console/")

    # Assert：HTML 文件存在且使用 UTF-8 页面类型。
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    # Assert：共享电脑不应缓存可能显示审批信息的控制台文档。
    assert response.headers["cache-control"] == "no-store"
    # Assert：禁止 iframe 嵌入与 MIME 猜测。
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    # Assert：CSP 只允许同源脚本/样式/API，并禁止内联脚本。
    content_security_policy = response.headers["content-security-policy"]
    assert "default-src 'self'" in content_security_policy
    assert "script-src 'self'" in content_security_policy
    assert "connect-src 'self'" in content_security_policy
    assert "frame-ancestors 'none'" in content_security_policy
    assert "unsafe-inline" not in content_security_policy
    # Assert：页面包含真实 Agent 工作台结构和外部脚本。
    assert "看见 Agent 如何做决定" in response.text
    assert "LANGGRAPH TRACE" in response.text
    assert 'src="/console/assets/console.js"' in response.text
    # Assert：四个角色输入框都是 password 且没有预置 value。
    assert 'id="customer-token-input" type="password"' in response.text
    assert 'id="reviewer-token-input" type="password"' in response.text
    assert 'id="auditor-token-input" type="password"' in response.text
    assert 'id="developer-token-input" type="password"' in response.text
    # 教学调试播放器必须真实进入最终 HTML，而不是只存在于设计文档。
    assert 'id="debug-inspector"' in response.text
    assert "CHECKPOINT PLAYBACK" in response.text
    # 第 20.2 步把调试器改为全宽学习工作台，并提供可退出的专注大屏按钮。
    assert "Agent 执行回放工作台" in response.text
    assert 'id="debug-focus-button"' in response.text
    assert 'aria-pressed="false"' in response.text
    assert "eyJhbGci" not in response.text
    assert "serviceops-development-only-secret" not in response.text


@pytest.mark.asyncio
async def test_console_static_assets_are_served_with_expected_types() -> None:
    """CSS 与 JavaScript 应由同源静态挂载提供，不能依赖外部 CDN。"""

    # Arrange：创建轻量 ASGI 客户端。
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # Act：分别请求两个版本控制资源。
        css_response = await client.get("/console/assets/console.css")
        js_response = await client.get("/console/assets/console.js")

    # Assert：两个资源都由当前包成功读取。
    assert css_response.status_code == 200
    assert js_response.status_code == 200
    # Assert：浏览器获得正确 MIME 类型，不需要危险的类型猜测。
    assert css_response.headers["content-type"].startswith("text/css")
    assert "javascript" in js_response.headers["content-type"]
    # Assert：样式和脚本均是项目专用内容，不是空壳占位文件。
    assert ".content-grid" in css_response.text
    assert "requestJson" in js_response.text
    assert "/api/v1/conversations" in js_response.text
    assert "/messages/stream" in js_response.text
    assert "requestSse" in js_response.text
    assert "/api/v1/approvals/" in js_response.text
    assert "/api/v1/audit/approvals/" in js_response.text
    assert "/api/v1/debug/threads/" in js_response.text
    # 公网作品模式必须由后端短时会话引导，不能在 HTML 中硬编码 Token。
    assert "/api/v1/demo/session" in js_response.text
    assert "bootstrapPublicDemo" in js_response.text
    assert ".public-demo-banner" in css_response.text
    assert "renderSelectedCheckpoint" in js_response.text
    # 控制台必须展示新增的第五项Qdrant就绪状态，不能只在后端响应中存在。
    assert 'knowledge_qdrant: "Qdrant 知识索引"' in js_response.text
    # 大屏模式必须同时存在完整布局样式和由单一函数维护的交互状态。
    assert ".debug-stage" in css_response.text
    assert "body.debug-focus-mode" in css_response.text
    assert "setDebugFocusMode" in js_response.text
    assert 'event.key === "Escape"' in js_response.text


def test_console_frontend_keeps_tokens_in_memory_and_uses_safe_dom_updates() -> None:
    """前端源码不得把 Token 持久化，也不能用 innerHTML 插入服务端文本。"""

    # Arrange：脚本路径来自已安装包内部，而不是测试工作目录。
    script_path = CONSOLE_DIRECTORY / "assets" / "console.js"
    # Act：以 UTF-8 读取版本控制脚本供有限安全契约断言。
    script = script_path.read_text(encoding="utf-8")
    # Assert：不能调用浏览器本地持久化 API 保存 Token。
    assert "localStorage." not in script
    assert "sessionStorage." not in script
    # Assert：不能把用户或服务端文本解释为 HTML。
    assert ".innerHTML =" not in script
    assert ".insertAdjacentHTML(" not in script
    assert "textContent" in script
    # Assert：身份始终通过 Authorization Header，不出现前端伪造 user_id 的赋值。
    assert "headers.Authorization" in script
    assert "user_id:" not in script
    # 调试身份和业务身份必须分开，不能偷用 customer Token 读取状态历史。
    assert "state.developerToken" in script
    assert "debug:read" in (CONSOLE_DIRECTORY / "index.html").read_text(encoding="utf-8")


def test_console_assets_live_inside_publishable_python_package() -> None:
    """控制台资源必须位于 serviceops_agent 包中，Docker wheel 才能携带它们。"""

    # Arrange：列出本步要求随 wheel 发布的三个文件。
    required_paths = [
        CONSOLE_DIRECTORY / "index.html",
        CONSOLE_DIRECTORY / "assets" / "console.css",
        CONSOLE_DIRECTORY / "assets" / "console.js",
    ]
    # Assert：路径全部真实存在且不是空文件。
    assert all(path.is_file() and path.stat().st_size > 0 for path in required_paths)
    # Assert：共同父级位于 Python 包，不是容易被 Dockerfile 漏掉的仓库级 frontend 文件夹。
    assert CONSOLE_DIRECTORY.name == "web"
    assert CONSOLE_DIRECTORY.parent.name == "serviceops_agent"


def test_console_public_demo_keeps_business_api_authenticated() -> None:
    """公网会话入口可以存在，但不能新增绕过 JWT 的聊天或审批接口。"""

    # Arrange/Act：读取 FastAPI 根据真实路由生成的 OpenAPI Schema。
    openapi_paths = app.openapi()["paths"]
    # Assert：控制台与静态资源不是业务接口，不污染 Swagger。
    assert "/console/" not in openapi_paths
    assert "/console/assets/console.js" not in openapi_paths
    # Assert：不存在返回固定多角色密钥的旧式接口，也不存在无认证 chat 路由。
    assert "/api/v1/demo/tokens" not in openapi_paths
    assert "/api/v1/demo/chat" not in openapi_paths
    # 公网入口只签发短时沙盒身份，真正业务仍调用原 JWT 保护路径。
    assert "/api/v1/demo/session" in openapi_paths

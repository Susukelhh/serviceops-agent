# syntax=docker/dockerfile:1.7

# Python 镜像固定到明确补丁版本和多架构清单摘要，减少基础镜像标签漂移。
# 更新依赖时应同时用官方镜像页或 Docker Scout 复核这个摘要。
ARG PYTHON_IMAGE="python:3.12.13-slim-trixie@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36"

# builder 阶段只负责解析锁文件和安装项目，构建工具不会进入最终运行镜像。
FROM ${PYTHON_IMAGE} AS builder

# 固定 uv 版本，避免构建器升级造成锁文件解释行为漂移。
ARG UV_VERSION="0.12.5"

# 禁止 uv 下载另一份 Python；依赖必须安装到项目内 .venv，方便复制到运行阶段。
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# 后续 COPY 和 uv sync 都以 /app 为项目目录。
WORKDIR /app

# uv 只存在于构建阶段；精确版本比无上限安装更容易复现和审计。
RUN python -m pip install --no-cache-dir "uv==${UV_VERSION}"

# 先复制依赖元数据，使业务源码变化时仍能复用昂贵的依赖安装层。
COPY pyproject.toml uv.lock README.md .python-version ./

# 第一遍只安装第三方生产依赖；BuildKit 缓存只保存下载包，不进入最终镜像。
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# 第三方依赖稳定后再复制应用源码，缩小普通代码修改造成的缓存失效范围。
COPY src ./src

# 第二遍把当前项目以非 editable 方式装入 .venv，模拟真实发布安装而不是源码软链接。
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# runtime 阶段重新从干净 Python 基础镜像开始，不携带 pip 下载缓存和 uv 构建工具。
FROM ${PYTHON_IMAGE} AS runtime

# Python 日志立即输出；不写 pyc；应用从复制进来的项目虚拟环境加载。
# SERVICEOPS_PROJECT_ROOT 解决 wheel 安装后无法从 site-packages 反推业务数据目录的问题。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PATH="/app/.venv/bin:${PATH}" \
    SERVICEOPS_PROJECT_ROOT=/app

# 与 Compose、Kubernetes SecurityContext 共用固定的非特权 UID/GID，避免以 root 运行应用。
RUN groupadd --gid 10001 serviceops \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin serviceops \
    && mkdir -p /app/data/runtime \
    && chown -R 10001:10001 /app

# /app 是应用根目录；相对命令和显式根目录配置在此保持一致。
WORKDIR /app

# 只复制构建完成的虚拟环境，不复制 builder 中的 uv、缓存或源代码工作区。
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv

# 种子订单和受治理知识库是运行必需的只读数据；本地 .env 与 runtime 数据绝不进入镜像。
COPY --chown=10001:10001 data/seed /app/data/seed

# 从这里开始的进程和镜像健康检查都使用非 root 用户。
USER 10001:10001

# EXPOSE 只声明容器监听端口；是否暴露到宿主机由 Compose/平台负责。
EXPOSE 8000

# 使用 Python 标准库检查真实 readiness，不为一个探针额外安装 curl。
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=3); raise SystemExit(0 if r.status == 200 else 1)"]

# SIGTERM 让容器平台触发 Uvicorn/FastAPI lifespan 的优雅关闭与数据库连接释放。
STOPSIGNAL SIGTERM

# 一个容器运行一个 Uvicorn 进程；横向扩缩容交给编排平台，多个副本共享 PostgreSQL。
CMD ["python", "-m", "uvicorn", "serviceops_agent.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

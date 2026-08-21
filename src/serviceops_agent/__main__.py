"""允许使用 ``python -m serviceops_agent`` 启动本地开发服务器。"""

# Uvicorn 是运行 FastAPI 所需的 ASGI 服务器；这里导入它以便用 Python 代码启动服务。
import uvicorn


def main() -> None:
    """启动开发服务器。

    `reload=True` 只适合本地开发。生产环境后续会通过容器启动参数控制进程数、
    超时与日志格式，而不会复用这里的开发配置。
    """

    # 启动一个 Uvicorn 开发服务器；此调用会阻塞当前进程，直到用户停止服务。
    uvicorn.run(
        # 使用“模块路径:变量名”定位 FastAPI 对象，避免在这里重复创建应用。
        "serviceops_agent.api.app:app",
        # 仅监听本机回环地址，防止开发服务意外暴露到局域网或公网。
        host="127.0.0.1",
        # 8000 是本地开发端口，可通过 http://127.0.0.1:8000 访问服务。
        port=8000,
        # 源码变化后自动重启，方便在 PyCharm 中修改代码并立即看到效果。
        reload=True,
    )


# 只有直接执行本模块时才启动服务器；被测试或其他模块导入时不会自动启动。
if __name__ == "__main__":
    # 调用上面封装的启动函数，让入口判断与服务器配置保持分离。
    main()

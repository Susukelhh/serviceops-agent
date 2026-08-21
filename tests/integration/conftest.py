"""真实数据库集成测试专用的异步事件循环工厂。"""

# asyncio 创建与当前 Python 版本匹配的默认或 Selector 事件循环。
import asyncio

# os 只判断当前测试运行平台，不读取其他系统环境信息。
import os

# pytest 的 Config/Item 类型让自定义 hook 的输入边界清晰。
from pytest import Config, Item


def _windows_selector_loop() -> asyncio.AbstractEventLoop:
    """创建 psycopg Windows 异步连接支持的 Selector 事件循环。"""

    # psycopg 不支持 Windows 默认 Proactor；SelectorEventLoop 使用受支持的选择器模型。
    return asyncio.SelectorEventLoop()


def pytest_asyncio_loop_factories(
    config: Config,
    item: Item,
) -> dict[str, object]:
    """只为 integration 目录中的异步测试选择平台兼容循环工厂。"""

    # hook 需要接收 pytest 上下文，但当前选择只取决于操作系统。
    _ = config, item
    # Windows 显式采用 Selector，避免 AsyncPostgresSaver 连接阶段被 psycopg 拒绝。
    if os.name == "nt":
        return {"windows-selector": _windows_selector_loop}
    # Linux CI 和 Docker 使用 Python/平台提供的默认事件循环工厂。
    return {"platform-default": asyncio.new_event_loop}

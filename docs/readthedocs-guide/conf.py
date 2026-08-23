"""ServiceOps Agent 教学文档的 Sphinx 配置。"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

project = "ServiceOps Agent：从代码到面试"
author = "ServiceOps Agent contributors"
copyright = "2026, ServiceOps Agent contributors"
release = "1.0"

extensions: list[str] = []
templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]
language = "zh_CN"

html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]
html_css_files = ["custom.css"]
html_title = "ServiceOps Agent 项目全解"
html_show_sourcelink = False
html_show_sphinx = False
html_theme_options = {
    "collapse_navigation": False,
    "sticky_navigation": True,
    "navigation_depth": 4,
    "includehidden": True,
    "titles_only": False,
}

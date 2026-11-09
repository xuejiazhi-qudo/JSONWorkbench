"""资源与路径解析：源码运行与 PyInstaller 打包后都能找到资源。"""

from __future__ import annotations

import os
import sys
from typing import Optional

__all__ = [
    "app_root",
    "is_frozen",
    "read_style_template",
    "resource_path",
    "user_data_dir",
]


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> str:
    """资源根目录：打包后是 PyInstaller 解包目录，开发时是项目根目录。"""
    if is_frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def resource_path(*parts: str) -> str:
    return os.path.join(app_root(), "resources", *parts)


def read_style_template(name: str = "base.qss") -> str:
    """读取 QSS 模板；缺失时返回空串而不是崩溃（界面退回 QPalette 配色）。"""
    path = resource_path("styles", name)
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def user_data_dir(app_name: str = "JSONWorkbench",
                  org: str = "Qudo") -> str:
    """跨平台用户数据目录（历史记录数据库等）。"""
    override = os.environ.get("JSONWB_DATA_DIR")
    if override:
        base = override
    elif sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    path = os.path.join(base, org, app_name)
    os.makedirs(path, exist_ok=True)
    return path


def optional_env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    return value if value else default

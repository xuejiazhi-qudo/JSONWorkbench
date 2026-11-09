"""对话框统一入口。

所有模态弹窗都走这里，原因有二：

1. **可自动化**：设置环境变量 ``JSONWB_HEADLESS=1`` 时不弹模态框，
   确认类问题直接返回默认答案并打印一行日志。离屏自检/端到端测试才不会挂死。
2. **一致性**：标题、按钮、默认焦点统一，避免各处 QMessageBox 风格漂移。
"""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtWidgets import QMessageBox, QWidget

__all__ = ["critical", "confirm", "information", "is_headless", "warn"]


def is_headless() -> bool:
    """自动化运行时为 True（由 ``JSONWB_HEADLESS`` 环境变量控制）。"""
    return bool(os.environ.get("JSONWB_HEADLESS"))


def _log(kind: str, title: str, text: str) -> None:
    first_line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    print(f"[dialog:{kind}] {title}: {first_line}", flush=True)


def confirm(parent: Optional[QWidget], title: str, text: str, *,
            default: bool = True,
            buttons: Optional[QMessageBox.StandardButton] = None) -> bool:
    """是/否确认。返回 True 表示用户选择了肯定答案。"""
    if is_headless():
        _log("confirm", title, text)
        return default
    standard = buttons if buttons is not None else (
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    default_button = QMessageBox.StandardButton.Yes if default else QMessageBox.StandardButton.No
    answer = QMessageBox.question(parent, title, text, standard, default_button)
    return answer in (QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.Save,
                      QMessageBox.StandardButton.Ok)


def warn(parent: Optional[QWidget], title: str, text: str) -> None:
    if is_headless():
        _log("warn", title, text)
        return
    QMessageBox.warning(parent, title, text)


def critical(parent: Optional[QWidget], title: str, text: str) -> None:
    if is_headless():
        _log("critical", title, text)
        return
    QMessageBox.critical(parent, title, text)


def information(parent: Optional[QWidget], title: str, text: str) -> None:
    if is_headless():
        _log("info", title, text)
        return
    QMessageBox.information(parent, title, text)

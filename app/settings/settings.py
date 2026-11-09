"""应用设置：基于 QSettings 的 INI 文件，绿色免安装、可随 exe 一起分发。

隐私默认值：**不保存 JSON 原文**，历史记录只存元数据；用户可显式打开。
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QSettings, QSize, QPoint, QByteArray

from app import APP_NAME, APP_ORG
from app.utils.resources import user_data_dir

MAX_RECENT_FILES = 15

_DEFAULTS = {
    "ui/theme": "dark",
    "ui/language": "zh",
    "ui/sidebar_width": 208,
    "ui/font_size": 13,
    "ui/font_family": "",
    "ui/show_status_icons": True,
    "editor/indent_choice": "4",
    "editor/tab_size": 4,
    "editor/insert_spaces": True,
    "editor/word_wrap": False,
    "editor/highlight_limit_kb": 2048,     # 超过该大小关闭语法高亮，避免卡顿
    "editor/auto_close_brackets": True,
    "editor/auto_indent": True,
    "editor/line_numbers": True,
    "format/sort_keys": False,
    "format/sort_mode": "alpha",
    "format/sort_arrays": False,
    "format/ensure_ascii": False,
    "format/trailing_newline": False,
    "format/line_ending": "system",
    "parse/document_mode": "auto",          # auto | json | jsonc
    "parse/max_depth": 1000,
    "parse/detect_duplicate_keys": True,
    "privacy/history_enabled": True,
    "privacy/history_store_content": False,  # 默认不存 JSON 原文
    "privacy/history_limit": 200,
    "privacy/recent_files_enabled": True,
    "files/last_dir": "",
    "files/encoding": "utf-8",
    "files/with_bom": False,
}

__all__ = ["MAX_RECENT_FILES", "Settings", "settings"]


class Settings:
    """类型安全的设置读写封装。"""

    def __init__(self, scope: Optional[str] = None) -> None:
        if scope:
            import os

            path = os.path.join(user_data_dir(), f"{scope}.ini")
            self._q = QSettings(path, QSettings.IniFormat)
        else:
            self._q = QSettings(QSettings.IniFormat, QSettings.UserScope, APP_ORG, APP_NAME)

    # ------------------------------------------------------------------ 基础
    def value(self, key: str, default=None):
        fallback = _DEFAULTS.get(key, default)
        raw = self._q.value(key, fallback)
        if raw is None:
            return fallback
        if isinstance(fallback, bool):
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        if isinstance(fallback, int) and not isinstance(fallback, bool):
            try:
                return int(raw)
            except (TypeError, ValueError):
                return fallback
        return raw

    def set_value(self, key: str, value) -> None:
        self._q.setValue(key, value)

    def sync(self) -> None:
        self._q.sync()

    def reset_to_defaults(self) -> None:
        keep = {
            "ui/theme": self.theme,
            "ui/language": self.language,
            "window/geometry": self._q.value("window/geometry"),
            "window/state": self._q.value("window/state"),
        }
        self._q.clear()
        for key, value in keep.items():
            if value is not None:
                self._q.setValue(key, value)
        self._q.sync()

    def all_keys(self) -> List[str]:
        return list(self._q.allKeys())

    # ------------------------------------------------------------------ 界面
    @property
    def theme(self) -> str:
        return str(self.value("ui/theme", "dark"))

    @theme.setter
    def theme(self, value: str) -> None:
        self.set_value("ui/theme", value)

    @property
    def language(self) -> str:
        return str(self.value("ui/language", "zh"))

    @language.setter
    def language(self, value: str) -> None:
        self.set_value("ui/language", value)

    @property
    def sidebar_width(self) -> int:
        return int(self.value("ui/sidebar_width", 208))

    @sidebar_width.setter
    def sidebar_width(self, value: int) -> None:
        self.set_value("ui/sidebar_width", int(value))

    @property
    def font_size(self) -> int:
        return int(self.value("ui/font_size", 13))

    @font_size.setter
    def font_size(self, value: int) -> None:
        self.set_value("ui/font_size", max(8, min(32, int(value))))

    @property
    def font_family(self) -> str:
        return str(self.value("ui/font_family", "") or "")

    @font_family.setter
    def font_family(self, value: str) -> None:
        self.set_value("ui/font_family", value)

    # ------------------------------------------------------------------ 编辑器
    @property
    def indent_choice(self) -> str:
        return str(self.value("editor/indent_choice", "4"))

    @indent_choice.setter
    def indent_choice(self, value: str) -> None:
        self.set_value("editor/indent_choice", value)

    @property
    def word_wrap(self) -> bool:
        return bool(self.value("editor/word_wrap", False))

    @word_wrap.setter
    def word_wrap(self, value: bool) -> None:
        self.set_value("editor/word_wrap", bool(value))

    @property
    def highlight_limit_bytes(self) -> int:
        return int(self.value("editor/highlight_limit_kb", 2048)) * 1024

    @highlight_limit_bytes.setter
    def highlight_limit_bytes(self, value: int) -> None:
        self.set_value("editor/highlight_limit_kb", max(64, int(value) // 1024))

    @property
    def auto_close_brackets(self) -> bool:
        return bool(self.value("editor/auto_close_brackets", True))

    @auto_close_brackets.setter
    def auto_close_brackets(self, value: bool) -> None:
        self.set_value("editor/auto_close_brackets", bool(value))

    @property
    def auto_indent(self) -> bool:
        return bool(self.value("editor/auto_indent", True))

    @auto_indent.setter
    def auto_indent(self, value: bool) -> None:
        self.set_value("editor/auto_indent", bool(value))

    @property
    def line_numbers(self) -> bool:
        return bool(self.value("editor/line_numbers", True))

    @line_numbers.setter
    def line_numbers(self, value: bool) -> None:
        self.set_value("editor/line_numbers", bool(value))

    # ------------------------------------------------------------------ 格式化
    def format_option_defaults(self) -> dict:
        return {
            "sort_keys": bool(self.value("format/sort_keys", False)),
            "sort_mode": str(self.value("format/sort_mode", "alpha")),
            "sort_arrays": bool(self.value("format/sort_arrays", False)),
            "ensure_ascii": bool(self.value("format/ensure_ascii", False)),
            "trailing_newline": bool(self.value("format/trailing_newline", False)),
            "indent_choice": self.indent_choice,
            "line_ending": str(self.value("format/line_ending", "system")),
        }

    # ------------------------------------------------------------------ 解析
    @property
    def document_mode(self) -> str:
        return str(self.value("parse/document_mode", "auto"))

    @document_mode.setter
    def document_mode(self, value: str) -> None:
        self.set_value("parse/document_mode", value)

    @property
    def max_depth(self) -> int:
        return int(self.value("parse/max_depth", 1000))

    @max_depth.setter
    def max_depth(self, value: int) -> None:
        self.set_value("parse/max_depth", max(8, min(200000, int(value))))

    @property
    def detect_duplicate_keys(self) -> bool:
        return bool(self.value("parse/detect_duplicate_keys", True))

    @detect_duplicate_keys.setter
    def detect_duplicate_keys(self, value: bool) -> None:
        self.set_value("parse/detect_duplicate_keys", bool(value))

    # ------------------------------------------------------------------ 隐私
    @property
    def history_enabled(self) -> bool:
        return bool(self.value("privacy/history_enabled", True))

    @history_enabled.setter
    def history_enabled(self, value: bool) -> None:
        self.set_value("privacy/history_enabled", bool(value))

    @property
    def history_store_content(self) -> bool:
        return bool(self.value("privacy/history_store_content", False))

    @history_store_content.setter
    def history_store_content(self, value: bool) -> None:
        self.set_value("privacy/history_store_content", bool(value))

    @property
    def history_limit(self) -> int:
        return int(self.value("privacy/history_limit", 200))

    @history_limit.setter
    def history_limit(self, value: int) -> None:
        self.set_value("privacy/history_limit", max(10, min(5000, int(value))))

    @property
    def recent_files_enabled(self) -> bool:
        return bool(self.value("privacy/recent_files_enabled", True))

    @recent_files_enabled.setter
    def recent_files_enabled(self, value: bool) -> None:
        self.set_value("privacy/recent_files_enabled", bool(value))

    # ------------------------------------------------------------------ 文件
    @property
    def last_dir(self) -> str:
        return str(self.value("files/last_dir", "") or "")

    @last_dir.setter
    def last_dir(self, value: str) -> None:
        self.set_value("files/last_dir", value)

    @property
    def encoding(self) -> str:
        return str(self.value("files/encoding", "utf-8"))

    @encoding.setter
    def encoding(self, value: str) -> None:
        self.set_value("files/encoding", value)

    @property
    def with_bom(self) -> bool:
        return bool(self.value("files/with_bom", False))

    @with_bom.setter
    def with_bom(self, value: bool) -> None:
        self.set_value("files/with_bom", bool(value))

    # ------------------------------------------------------------------ 最近文件
    def recent_files(self) -> List[str]:
        raw = self._q.value("files/recent", [])
        if isinstance(raw, str):
            raw = [raw] if raw else []
        return [str(item) for item in (raw or [])][:MAX_RECENT_FILES]

    def add_recent_file(self, path: str) -> List[str]:
        if not self.recent_files_enabled or not path:
            return self.recent_files()
        items = [path] + [p for p in self.recent_files() if p != path]
        items = items[:MAX_RECENT_FILES]
        self._q.setValue("files/recent", items)
        return items

    def clear_recent_files(self) -> None:
        self._q.setValue("files/recent", [])

    # ------------------------------------------------------------------ 窗口
    def save_window(self, geometry: QByteArray, state: QByteArray) -> None:
        self._q.setValue("window/geometry", geometry)
        self._q.setValue("window/state", state)

    def window_geometry(self) -> Optional[QByteArray]:
        value = self._q.value("window/geometry")
        return value if isinstance(value, QByteArray) else None

    def window_state(self) -> Optional[QByteArray]:
        value = self._q.value("window/state")
        return value if isinstance(value, QByteArray) else None

    def save_splitter(self, name: str, state: QByteArray) -> None:
        self._q.setValue(f"splitter/{name}", state)

    def splitter_state(self, name: str) -> Optional[QByteArray]:
        value = self._q.value(f"splitter/{name}")
        return value if isinstance(value, QByteArray) else None

    def default_size(self) -> QSize:
        return QSize(1360, 860)

    def default_position(self) -> QPoint:
        return QPoint(80, 60)


settings = Settings()

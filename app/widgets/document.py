"""单个文档的元数据：路径、编码、换行、修改状态、文档类型（JSON/JSONC）。"""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import QObject, Signal

from app.core.jsonfilemanager import guess_mode_from_path
from app.core.jsonparser import MODE_JSON
from app.i18n import tr


class Document(QObject):
    """不持有文本本身（文本由编辑器持有），只管理元数据与状态。"""

    modified_changed = Signal(bool)
    path_changed = Signal(str)

    _untitled_counter = 0

    def __init__(self, path: str = "", *, encoding: str = "utf-8", had_bom: bool = False,
                 line_ending: str = "\n", mode: Optional[str] = None,
                 title: str = "") -> None:
        super().__init__()
        self._path = os.path.abspath(path) if path else ""
        self._encoding = encoding
        self._had_bom = bool(had_bom)
        self._line_ending = line_ending or "\n"
        self._mode = mode or (guess_mode_from_path(self._path) if self._path else MODE_JSON)
        self._modified = False
        if title:
            self._title = title
            self._untitled_index = 0
        else:
            Document._untitled_counter += 1
            self._untitled_index = Document._untitled_counter
            self._title = ""

    # ------------------------------------------------------------------ 属性
    @property
    def path(self) -> str:
        return self._path

    @property
    def is_untitled(self) -> bool:
        return not self._path

    @property
    def file_name(self) -> str:
        if self._path:
            return os.path.basename(self._path)
        if self._title:
            return self._title
        # 读取时才翻译，切换语言后标签页标题能即时刷新
        return f"{tr('Untitled')} {self._untitled_index}"

    @property
    def display_name(self) -> str:
        return self.file_name + ("*" if self._modified else "")

    @property
    def tooltip(self) -> str:
        return self._path or self.file_name

    @property
    def modified(self) -> bool:
        return self._modified

    @property
    def encoding(self) -> str:
        return self._encoding

    @property
    def had_bom(self) -> bool:
        return self._had_bom

    @property
    def line_ending(self) -> str:
        return self._line_ending

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def mode_label(self) -> str:
        return "JSONC" if self._mode != MODE_JSON else "JSON"

    # ------------------------------------------------------------------ 变更
    def set_modified(self, modified: bool) -> None:
        modified = bool(modified)
        if modified == self._modified:
            return
        self._modified = modified
        self.modified_changed.emit(modified)

    def set_path(self, path: str, *, keep_mode: bool = False) -> None:
        self._path = os.path.abspath(path) if path else ""
        if self._path and not keep_mode:
            self._mode = guess_mode_from_path(self._path)
        self.path_changed.emit(self._path)

    def set_mode(self, mode: str) -> None:
        self._mode = mode

    def apply_file_metadata(self, result) -> None:
        """用读取结果（FileReadResult）刷新路径/编码/BOM/换行。"""
        self.set_path(result.path)
        self._encoding = result.encoding
        self._had_bom = bool(result.had_bom)
        self._line_ending = result.line_ending or "\n"

    def mark_saved(self, path: Optional[str] = None, *, encoding: Optional[str] = None,
                   had_bom: Optional[bool] = None, line_ending: Optional[str] = None) -> None:
        if path is not None:
            self.set_path(path)
        if encoding:
            self._encoding = encoding
        if had_bom is not None:
            self._had_bom = bool(had_bom)
        if line_ending:
            self._line_ending = line_ending
        self.set_modified(False)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Document {self.display_name!r} mode={self._mode} encoding={self._encoding}>"

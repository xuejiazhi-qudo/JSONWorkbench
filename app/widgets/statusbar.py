"""底部状态栏：文件名、合法性、行列、行数、字符数、节点数、层级、编码、进度。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QLabel,
    QProgressBar,
    QPushButton,
    QStatusBar,
    QWidget,
)

from app.core.jsonparser import ParseStats
from app.i18n import register_retranslate, tr


class StatusBar(QStatusBar):
    """状态栏。左侧放文件与消息，右侧放各项指标。"""

    cancel_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setSizeGripEnabled(True)

        self.file_label = QLabel("")
        self.file_label.setObjectName("StatusFile")
        self.valid_label = QLabel("")
        self.divider = QLabel("│")
        self.divider.setObjectName("StatusDivider")

        self.position_label = QLabel("")
        self.lines_label = QLabel("")
        self.chars_label = QLabel("")
        self.nodes_label = QLabel("")
        self.depth_label = QLabel("")
        self.mode_label = QLabel("")
        self.encoding_label = QLabel("")
        self.indent_label = QLabel("")

        self.progress = QProgressBar()
        self.progress.setMaximumWidth(180)
        self.progress.setTextVisible(True)
        self.progress.setVisible(False)
        self.cancel_button = QPushButton(tr("Cancel"))
        self.cancel_button.setObjectName("StatusCancel")
        self.cancel_button.setFlat(True)
        self.cancel_button.setVisible(False)
        self.cancel_button.setCursor(Qt.PointingHandCursor)
        self.cancel_button.clicked.connect(self.cancel_requested)

        self.addWidget(self.file_label)
        self.addWidget(self.divider)
        self.addWidget(self.valid_label)

        for widget in (self.progress, self.cancel_button):
            self.addPermanentWidget(widget)
        for label in (self.indent_label, self.mode_label, self.encoding_label,
                      self.depth_label, self.nodes_label, self.chars_label,
                      self.lines_label, self.position_label):
            self.addPermanentWidget(label)

        self._state: dict = {}
        self._indent_cache = ""
        self._mode_cache = ""
        self._metrics_cache: Optional[tuple] = None
        self._encoding_cache: Optional[tuple] = None
        self.set_validity(None)
        register_retranslate(self)

    # ------------------------------------------------------------------ 更新
    def set_file_name(self, name: str, path: str = "") -> None:
        self.file_label.setText(name or "")
        self.file_label.setToolTip(path or name or "")

    def set_validity(self, valid: Optional[bool], detail: str = "") -> None:
        if valid is None:
            self.valid_label.setObjectName("StatusLabel")
            self.valid_label.setText(tr("Not validated"))
        elif valid:
            self.valid_label.setObjectName("StatusValid")
            text = tr("Valid JSON")
            self.valid_label.setText(f"✓ {text}" + (f" · {detail}" if detail else ""))
        else:
            self.valid_label.setObjectName("StatusInvalid")
            text = tr("Invalid JSON")
            self.valid_label.setText(f"✗ {text}" + (f" · {detail}" if detail else ""))
        # objectName 改变后必须重新套用样式
        self.valid_label.style().unpolish(self.valid_label)
        self.valid_label.style().polish(self.valid_label)

    def set_cursor(self, line: int, column: int) -> None:
        self._state["line"] = line
        self._state["column"] = column
        self.position_label.setText(tr("Ln {line}, Col {column}").format(line=line, column=column))

    def set_metrics(self, lines: int, chars: int, stats: Optional[ParseStats] = None) -> None:
        self._metrics_cache = (lines, chars, stats)
        self.lines_label.setText(tr("{n} lines").format(n=lines))
        self.chars_label.setText(tr("{n} chars").format(n=chars))
        if stats is None:
            self.nodes_label.setText("")
            self.depth_label.setText("")
        else:
            self.nodes_label.setText(tr("{n} nodes").format(n=stats.node_count))
            self.depth_label.setText(tr("depth {n}").format(n=stats.max_depth))

    def set_encoding(self, encoding: str, had_bom: bool, line_ending: str) -> None:
        self._encoding_cache = (encoding, had_bom, line_ending)
        bom = " + BOM" if had_bom else ""
        ending = "CRLF" if line_ending == "\r\n" else "LF"
        self.encoding_label.setText(f"{encoding.upper()}{bom} · {ending}")

    def set_mode(self, mode_label: str) -> None:
        self._mode_cache = mode_label
        self.mode_label.setText(mode_label)

    def set_indent(self, indent_text: str) -> None:
        self._indent_cache = indent_text
        if indent_text == "\t":
            label = "Tab"
        elif indent_text:
            label = "{n} spaces".format(n=len(indent_text))
        else:
            label = "None"
        self.indent_label.setText(tr("Indent: {label}").format(label=tr(label)))

    # ------------------------------------------------------------------ 进度
    def show_progress(self, text: str = "", maximum: int = 0) -> None:
        self.progress.setMaximum(max(0, maximum))
        self.progress.setValue(0)
        self.progress.setFormat(text or "%p%")
        self.progress.setVisible(True)
        self.cancel_button.setVisible(True)

    def update_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress.setMaximum(total)
            self.progress.setValue(min(done, total))
        else:
            self.progress.setMaximum(0)

    def hide_progress(self) -> None:
        self.progress.setVisible(False)
        self.cancel_button.setVisible(False)

    def is_busy(self) -> bool:
        return self.progress.isVisible()

    # ------------------------------------------------------------------ 其它
    def notify(self, text: str, timeout: int = 4000) -> None:
        self.showMessage(text, timeout)

    def retranslate(self) -> None:
        self.cancel_button.setText(tr("Cancel"))
        if self._indent_cache or self.indent_label.text():
            self.set_indent(self._indent_cache)
        if self._metrics_cache is not None:
            self.set_metrics(*self._metrics_cache)
        if self._encoding_cache is not None:
            self.set_encoding(*self._encoding_cache)
        if self._mode_cache:
            self.mode_label.setText(self._mode_cache)
        state = self._state
        if "line" in state:
            self.set_cursor(state.get("line", 1), state.get("column", 1))
        name = self.valid_label.objectName()
        if name == "StatusValid":
            self.set_validity(True)
        elif name == "StatusInvalid":
            self.set_validity(False)
        else:
            self.set_validity(None)

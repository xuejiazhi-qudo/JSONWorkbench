"""查找/替换浮动条。

支持：普通文本、区分大小写、全字匹配、正则；显示"当前/总数"，
回车向下查找、Shift+回车向上查找、Esc 关闭。
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QRegularExpression, Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.i18n import register_retranslate, tr
from app.widgets.icons import icon

MAX_HIGHLIGHT_MATCHES = 2000


class FindReplaceBar(QWidget):
    """附着在编辑器右上角的查找替换条。"""

    closed = Signal()

    def __init__(self, editor: QPlainTextEdit, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent or editor)
        self.setObjectName("FindReplaceBar")
        self._editor = editor
        self._matches: List[tuple] = []
        self._current = -1
        self._replace_visible = False

        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText(tr("Find"))
        self.find_edit.setClearButtonEnabled(True)
        self.replace_edit = QLineEdit()
        self.replace_edit.setPlaceholderText(tr("Replace with"))

        self.count_label = QLabel("")
        self.count_label.setObjectName("HintLabel")
        self.error_label = QLabel("")
        self.error_label.setObjectName("BadgeError")
        self.error_label.setVisible(False)

        self.case_check = QCheckBox(tr("Match case"))
        self.word_check = QCheckBox(tr("Whole word"))
        self.regex_check = QCheckBox(tr("Regex"))

        self.prev_button = self._tool_button("chevron-down", tr("Previous (Shift+Enter)"), self.find_previous)
        self.prev_button.setIcon(self._icon_rotated())
        self.next_button = self._tool_button("chevron-down", tr("Next (Enter)"), self.find_next)
        self.toggle_replace_button = self._tool_button(
            "replace", tr("Toggle replace field"), self.toggle_replace)
        self.toggle_replace_button.setCheckable(True)
        self.replace_button = QPushButton(tr("Replace"))
        self.replace_button.clicked.connect(self.replace_current)
        self.replace_all_button = QPushButton(tr("Replace all"))
        self.replace_all_button.clicked.connect(self.replace_all)
        self.close_button = self._tool_button("close", tr("Close (Esc)"), self.hide_bar)

        row1 = QHBoxLayout()
        row1.setContentsMargins(8, 5, 6, 2)
        row1.setSpacing(5)
        row1.addWidget(self.find_edit, 1)
        row1.addWidget(self.prev_button)
        row1.addWidget(self.next_button)
        row1.addWidget(self.count_label)
        row1.addWidget(self.toggle_replace_button)
        row1.addWidget(self.close_button)

        row2 = QHBoxLayout()
        row2.setContentsMargins(8, 0, 6, 5)
        row2.setSpacing(8)
        row2.addWidget(self.replace_edit, 1)
        row2.addWidget(self.replace_button)
        row2.addWidget(self.replace_all_button)
        row2.addWidget(self.case_check)
        row2.addWidget(self.word_check)
        row2.addWidget(self.regex_check)
        row2.addWidget(self.error_label)
        row2.addStretch(1)

        self._replace_row = QWidget()
        self._replace_row.setLayout(row2)
        self._replace_row.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(row1)
        layout.addWidget(self._replace_row)

        self.find_edit.textChanged.connect(self._on_pattern_changed)
        self.find_edit.returnPressed.connect(self.find_next)
        self.case_check.toggled.connect(self._on_pattern_changed)
        self.word_check.toggled.connect(self._on_pattern_changed)
        self.regex_check.toggled.connect(self._on_pattern_changed)
        self.replace_edit.returnPressed.connect(self.replace_current)

        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setFocusPolicy(Qt.NoFocus)
        register_retranslate(self)
        self.hide()

    def retranslate(self) -> None:
        """切换语言时刷新文案。"""
        self.find_edit.setPlaceholderText(tr("Find"))
        self.replace_edit.setPlaceholderText(tr("Replace with"))
        self.case_check.setText(tr("Match case"))
        self.word_check.setText(tr("Whole word"))
        self.regex_check.setText(tr("Regex"))
        self.replace_button.setText(tr("Replace"))
        self.replace_all_button.setText(tr("Replace all"))
        self.prev_button.setToolTip(tr("Previous (Shift+Enter)"))
        self.next_button.setToolTip(tr("Next (Enter)"))
        self.toggle_replace_button.setToolTip(tr("Toggle replace field"))
        self.close_button.setToolTip(tr("Close (Esc)"))
        if self.count_label.text():
            self._update_count()

    # ------------------------------------------------------------------ 构造辅助
    def _tool_button(self, name: str, tooltip: str, slot) -> QToolButton:
        button = QToolButton()
        button.setIcon(icon(name, size=15, kind="muted"))
        button.setToolTip(tooltip)
        button.setAutoRaise(True)
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(slot)
        return button

    def _icon_rotated(self):
        from PySide6.QtGui import QIcon, QPixmap, QTransform

        source = icon("chevron-down", size=15, kind="muted").pixmap(15, 15)
        return QIcon(QPixmap(source).transformed(QTransform().rotate(180)))

    # ------------------------------------------------------------------ 显示控制
    def show_bar(self, replace: bool = False, initial: str = "") -> None:
        if replace:
            self.toggle_replace(True)
        if initial:
            self.find_edit.setText(initial)
        self.setVisible(True)
        self.reposition()
        self.find_edit.setFocus()
        self.find_edit.selectAll()
        if not initial:
            self._on_pattern_changed()

    def hide_bar(self) -> None:
        self.setVisible(False)
        self._clear_highlights()
        self.closed.emit()
        self._editor.setFocus()

    def toggle_replace(self, visible: Optional[bool] = None) -> None:
        self._replace_visible = bool(visible) if visible is not None else not self._replace_visible
        self._replace_row.setVisible(self._replace_visible)
        self.toggle_replace_button.setChecked(self._replace_visible)
        for widget in (self.replace_button, self.replace_all_button):
            widget.setVisible(self._replace_visible)
        self.reposition()

    def reposition(self) -> None:
        self.adjustSize()
        viewport = self._editor.viewport()
        x = viewport.width() - self.width() - 18
        self.move(max(8, x + self._editor.frameWidth()), 8)
        self.raise_()

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if event.key() == Qt.Key_Escape:
            self.hide_bar()
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if event.modifiers() & Qt.ShiftModifier:
                self.find_previous()
            else:
                self.find_next()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------ 查找
    def _regex(self) -> Optional[QRegularExpression]:
        pattern = self.find_edit.text()
        if not pattern:
            return None
        if not self.regex_check.isChecked():
            pattern = QRegularExpression.escape(pattern)
        if self.word_check.isChecked():
            pattern = rf"\b(?:{pattern})\b"
        expression = QRegularExpression(pattern)
        if not self.case_check.isChecked():
            expression.setPatternOptions(
                QRegularExpression.PatternOption.CaseInsensitiveOption)
        if not expression.isValid():
            self._show_error(expression.errorString())
            return None
        self._show_error("")
        return expression

    def _show_error(self, message: str) -> None:
        self.error_label.setVisible(bool(message))
        self.error_label.setText(message)

    def _on_pattern_changed(self, *_args) -> None:
        self._collect_matches()
        self._highlight_matches()
        self._update_count()

    def _collect_matches(self) -> None:
        """一次性收集全部匹配区间（有上限），不依赖 QTextDocument.find 的大小写行为。"""
        self._matches = []
        self._current = -1
        if not self.find_edit.text():
            return
        expression = self._regex()
        if expression is None:
            return
        text = self._editor.toPlainText()
        iterator = expression.globalMatch(text)
        limit = MAX_HIGHLIGHT_MATCHES
        while iterator.hasNext() and limit > 0:
            match = iterator.next()
            if match.capturedLength() == 0:
                continue  # 零长度匹配直接跳过，避免无意义的空选区
            self._matches.append((match.capturedStart(), match.capturedEnd()))
            limit -= 1

    def _highlight_matches(self) -> None:
        if not hasattr(self._editor, "set_find_selections"):
            return
        self._editor.set_find_selections(self._matches, self._current)

    def _clear_highlights(self) -> None:
        if hasattr(self._editor, "set_find_selections"):
            self._editor.set_find_selections([], -1)

    def _update_count(self) -> None:
        if not self.find_edit.text():
            self.count_label.setText("")
            return
        if not self._matches:
            self.count_label.setText(tr("No matches"))
            return
        index = self._current + 1 if self._current >= 0 else 0
        total = len(self._matches)
        suffix = "+" if total >= MAX_HIGHLIGHT_MATCHES else ""
        self.count_label.setText(f"{index}/{total}{suffix}" if index else f"0/{total}{suffix}")

    def find_next(self) -> bool:
        if not self._matches:
            self._collect_matches()
            self._highlight_matches()
        if not self._matches:
            self._update_count()
            return False
        self._current = (self._current + 1) % len(self._matches)
        return self._select_current()

    def find_previous(self) -> bool:
        if not self._matches:
            self._collect_matches()
            self._highlight_matches()
        if not self._matches:
            self._update_count()
            return False
        self._current = (self._current - 1) % len(self._matches)
        return self._select_current()

    def _select_current(self) -> bool:
        if self._current < 0 or self._current >= len(self._matches):
            return False
        start, end = self._matches[self._current]
        cursor = self._editor.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.KeepAnchor)
        self._editor.setTextCursor(cursor)
        self._editor.centerCursor()
        self._update_count()
        self._highlight_matches()
        return True

    # ------------------------------------------------------------------ 替换
    def replace_current(self) -> bool:
        if self._editor.isReadOnly():
            return False
        cursor = self._editor.textCursor()
        if not cursor.hasSelection() or self._current < 0:
            return self.find_next()
        start, end = self._matches[self._current]
        if cursor.selectionStart() != start or cursor.selectionEnd() != end:
            return self.find_next()
        replacement = self._replacement_for(cursor.selectedText())
        cursor.insertText(replacement)
        self._on_pattern_changed()
        return self.find_next()

    def replace_all(self) -> int:
        if self._editor.isReadOnly() or not self._matches:
            return 0
        document = self._editor.document()
        cursor = QTextCursor(document)
        cursor.beginEditBlock()
        count = 0
        for start, end in reversed(self._matches):
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.KeepAnchor)
            cursor.insertText(self._replacement_for(cursor.selectedText()))
            count += 1
        cursor.endEditBlock()
        self._on_pattern_changed()
        return count

    def _replacement_for(self, matched_text: str) -> str:
        replacement = self.replace_edit.text()
        if not self.regex_check.isChecked():
            return replacement
        expression = self._regex()
        if expression is None:
            return replacement
        match = expression.match(matched_text)
        if not match.hasMatch():
            return replacement
        return _expand_capture_refs(match, replacement)


def _expand_capture_refs(match, replacement: str) -> str:
    """把替换串里的 \\1 / $1 展开成捕获组内容。"""
    out = []
    index = 0
    while index < len(replacement):
        char = replacement[index]
        if char in "\\$" and index + 1 < len(replacement) and replacement[index + 1].isdigit():
            number = ""
            cursor = index + 1
            while cursor < len(replacement) and replacement[cursor].isdigit():
                number += replacement[cursor]
                cursor += 1
            group = match.captured(int(number)) if int(number) <= match.lastCapturedIndex() else ""
            out.append(group or "")
            index = cursor
            continue
        if char == "\\" and index + 1 < len(replacement):
            out.append(replacement[index + 1])
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)

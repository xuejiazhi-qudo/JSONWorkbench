"""JSON 代码编辑器。

在 QPlainTextEdit 之上补齐专业编辑器体验：行号槽、当前行高亮、括号匹配、
自动缩进、自动补全括号/引号、错误行标记与定位、超长文档降级。

性能注意：任何会在"每次光标移动/每次按键"触发的路径都不得调用
``toPlainText()``（那会整份拷贝文档），需要长度时用
``document().characterCount()``。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QTextCursor, QTextFormat
from PySide6.QtWidgets import QPlainTextEdit, QTextEdit

from app.core.jsonparser import Issue
from app.editor.highlighter import JsonHighlighter
from app.editor.linenumberarea import LineNumberArea, MarkerStore, paint_gutter
from app.widgets.theme import DARK_PALETTE, Palette

_PAIRS = {"{": "}", "[": "]"}
_REVERSE_PAIRS = {"}": "{", "]": "["}
_QUOTE = '"'
_AUTO_CLOSE_CHARS = {"{", "[", '"'}
#: 超过该字符数不再做括号匹配扫描（避免每次移动光标都扫描大文档）
BRACKET_SCAN_LIMIT = 200_000
#: 超过该字符数视为大文档：关闭括号匹配，语法高亮由设置阈值单独控制
LARGE_DOC_CHARS = 400_000


class JsonEditor(QPlainTextEdit):
    """JSON 编辑器控件。"""

    cursor_moved = Signal(int, int)          # line, column (1-based)
    find_requested = Signal()
    replace_requested = Signal()
    goto_line_requested = Signal()
    content_resized = Signal(int)            # 字符数，供状态栏与大文档降级使用
    dropped_files = Signal(list)

    def __init__(self, parent=None, *, read_only: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName("JsonEditor")
        self._palette: Palette = DARK_PALETTE
        self._markers = MarkerStore()
        self._show_line_numbers = True
        self._auto_close = True
        self._auto_indent = True
        self._indent_text = "    "
        self._tab_size = 4
        self._match_brackets = True
        self._error_offset = -1
        self._error_length = 0
        self._large_document = False
        self._find_spans: List[Tuple[int, int]] = []
        self._find_current = -1
        self._find_bar = None

        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setUndoRedoEnabled(True)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        if read_only:
            self.setReadOnly(True)

        self._gutter = LineNumberArea(self)
        self._highlighter = JsonHighlighter(self.document())

        self.blockCountChanged.connect(self._on_block_count_changed)
        self.updateRequest.connect(self._on_update_request)
        self.cursorPositionChanged.connect(self._on_cursor_moved)
        self.textChanged.connect(self._on_text_changed)

        self._apply_font(QFont("Consolas"))
        self.setTabStopDistance(self._tab_stop_distance())
        self.apply_theme(DARK_PALETTE)
        self._update_gutter_width()
        self._refresh_decorations()

    # ------------------------------------------------------------------ 主题与字体
    def apply_theme(self, palette: Palette, family: str = "") -> None:
        self._palette = palette
        if family:
            font = QFont(self.font())
            font.setFamily(family)
            self.setFont(font)
            self.setTabStopDistance(self._tab_stop_distance())
        self._highlighter.set_colors(palette.as_tokens())
        self._refresh_decorations()
        self._update_gutter_width()
        self.viewport().update()

    def _apply_font(self, font: QFont, size: Optional[int] = None) -> None:
        font.setStyleHint(QFont.Monospace)
        font.setFixedPitch(True)
        if size:
            font.setPointSize(max(7, min(32, size)))
        self.setFont(font)

    def set_font_size(self, size: int) -> None:
        self._apply_font(QFont(self.font()), size)
        self.setTabStopDistance(self._tab_stop_distance())
        self._update_gutter_width()

    def font_size(self) -> int:
        return self.font().pointSize()

    def zoom(self, delta: int) -> int:
        size = max(8, min(32, self.font_size() + delta))
        self.set_font_size(size)
        return size

    def _tab_stop_distance(self) -> float:
        return QFontMetricsF(self.font()).horizontalAdvance(" ") * self._tab_size

    def palette_colors(self) -> Palette:
        return self._palette

    # ------------------------------------------------------------------ 行为开关
    def set_indent(self, indent_text: str, tab_size: int = 4) -> None:
        self._indent_text = indent_text or "    "
        self._tab_size = max(1, int(tab_size))
        self.setTabStopDistance(self._tab_stop_distance())

    def indent_text(self) -> str:
        return self._indent_text

    def set_line_numbers_visible(self, visible: bool) -> None:
        self._show_line_numbers = bool(visible)
        self._gutter.setVisible(self._show_line_numbers)
        self._update_gutter_width()
        self.viewport().update()

    def set_auto_close_brackets(self, enabled: bool) -> None:
        self._auto_close = bool(enabled)

    def set_auto_indent(self, enabled: bool) -> None:
        self._auto_indent = bool(enabled)

    def set_bracket_matching(self, enabled: bool) -> None:
        self._match_brackets = bool(enabled)
        self._refresh_decorations()

    def set_highlighting_enabled(self, enabled: bool) -> None:
        self._highlighter.set_enabled(enabled)

    def highlighting_enabled(self) -> bool:
        return self._highlighter.is_enabled()

    def is_large_document(self) -> bool:
        return self._large_document

    # ------------------------------------------------------------------ 行号槽
    def line_number_width(self) -> int:
        if not self._show_line_numbers:
            return 0
        digits = max(2, len(str(self.blockCount())))
        char_w = QFontMetricsF(self.font()).horizontalAdvance("9")
        return int(14 + char_w * digits + 2 * 3 + 8)

    def _update_gutter_width(self) -> None:
        self.setViewportMargins(self.line_number_width(), 0, 0, 0)

    def _on_block_count_changed(self, _count: int) -> None:
        self._update_gutter_width()

    def _on_update_request(self, rect: QRect, dy: int) -> None:
        if dy:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_gutter_width()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._gutter.setGeometry(QRect(cr.left(), cr.top(), self.line_number_width(), cr.height()))
        if self._find_bar is not None and self._find_bar.isVisible():
            self._find_bar.reposition()

    def paint_line_numbers(self, area, event) -> None:
        paint_gutter(self, area, event, self._palette, self._markers, self._show_line_numbers)

    # ------------------------------------------------------------------ 装饰（当前行/括号/错误行）
    def _refresh_decorations(self) -> None:
        selections: List[QTextEdit.ExtraSelection] = []
        char_count = max(0, self.document().characterCount() - 1)

        if self._error_offset >= 0:
            selection = QTextEdit.ExtraSelection()
            selection.format.setBackground(QColor(self._palette.error_bg))
            selection.format.setProperty(QTextFormat.FullWidthSelection, True)
            cursor = QTextCursor(self.document())
            cursor.setPosition(min(self._error_offset, char_count))
            selection.cursor = cursor
            selections.append(selection)

        if not self.isReadOnly():
            selection = QTextEdit.ExtraSelection()
            selection.format.setBackground(QColor(self._palette.current_line))
            selection.format.setProperty(QTextFormat.FullWidthSelection, True)
            selection.cursor = self.textCursor()
            selections.append(selection)

        for start, end in self._bracket_spans():
            selection = QTextEdit.ExtraSelection()
            selection.format.setBackground(QColor(self._palette.accent_soft))
            selection.format.setForeground(QColor(self._palette.text))
            cursor = QTextCursor(self.document())
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.KeepAnchor)
            selection.cursor = cursor
            selections.append(selection)

        if self._find_spans:
            match_color = QColor(self._palette.warning)
            match_color.setAlpha(64)
            current_color = QColor(self._palette.accent)
            current_color.setAlpha(150)
            for index, (start, end) in enumerate(self._find_spans[:1000]):
                selection = QTextEdit.ExtraSelection()
                selection.format.setBackground(
                    current_color if index == self._find_current else match_color)
                cursor = QTextCursor(self.document())
                cursor.setPosition(min(start, char_count))
                cursor.setPosition(min(end, char_count), QTextCursor.KeepAnchor)
                selection.cursor = cursor
                selections.append(selection)

        self.setExtraSelections(selections)

    def _bracket_spans(self) -> List[Tuple[int, int]]:
        if not self._match_brackets or self._large_document:
            return []
        length = max(0, self.document().characterCount() - 1)
        if length == 0:
            return []
        pos = self.textCursor().position()
        candidates = []
        for index in (pos, pos - 1):
            if 0 <= index < length:
                char = self._char_at(index)
                if char in _PAIRS or char in _REVERSE_PAIRS:
                    candidates.append(index)
        if not candidates:
            return []
        text = self.toPlainText()
        spans = []
        for index in candidates:
            match = self._find_matching_bracket(text, index)
            if match >= 0:
                # 只高亮配对的这两个括号字符，而不是中间的整段内容
                spans.append((index, index + 1))
                spans.append((match, match + 1))
        return spans

    @staticmethod
    def _find_matching_bracket(text: str, index: int) -> int:
        """在字符串感知的前提下查找配对括号，扫描范围有上限。"""
        if index >= len(text):
            return -1
        opening = text[index]
        if opening in _PAIRS:
            closing, step, depth_delta = _PAIRS[opening], 1, 1
        elif opening in _REVERSE_PAIRS:
            closing, step, depth_delta = _REVERSE_PAIRS[opening], -1, -1
        else:
            return -1
        # 开括号本身算一层，所以从 1 开始；降到 0 的那个括号才是配对目标
        depth = 1
        in_string = False
        escaped = False
        cursor = index + step
        scanned = 0
        while 0 <= cursor < len(text) and scanned <= BRACKET_SCAN_LIMIT:
            char = text[cursor]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char in _PAIRS:
                depth += depth_delta
            elif char in _REVERSE_PAIRS:
                depth -= depth_delta
                if depth == 0:
                    return cursor if char == closing else -1
            cursor += step
            scanned += 1
        return -1

    # ------------------------------------------------------------------ 光标与文本事件
    def _on_cursor_moved(self) -> None:
        self._refresh_decorations()
        cursor = self.textCursor()
        self.cursor_moved.emit(cursor.blockNumber() + 1, cursor.columnNumber() + 1)

    def _on_text_changed(self) -> None:
        length = max(0, self.document().characterCount() - 1)
        large = length > LARGE_DOC_CHARS
        if large != self._large_document:
            self._large_document = large
        self.content_resized.emit(length)

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if event.modifiers() & Qt.ControlModifier:
            self.zoom(1 if event.angleDelta().y() > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)

    # ------------------------------------------------------------------ 按键行为
    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        key = event.key()
        modifiers = event.modifiers()
        char = event.text()
        ctrl = bool(modifiers & Qt.ControlModifier)
        shift = bool(modifiers & Qt.ShiftModifier)

        if ctrl and not shift and not modifiers & Qt.AltModifier:
            if key == Qt.Key_F:
                self.find_requested.emit()
                return event.accept()
            if key == Qt.Key_H:
                self.replace_requested.emit()
                return event.accept()
            if key == Qt.Key_G:
                self.goto_line_requested.emit()
                return event.accept()

        if ctrl and shift and key == Qt.Key_K and not self.isReadOnly():
            self.delete_current_line()
            return event.accept()

        if not self.isReadOnly():
            if key == Qt.Key_Tab and not ctrl and not shift and not modifiers & Qt.AltModifier:
                if not self._indent_selection(1):
                    self.insertPlainText(self._indent_text)
                return event.accept()
            if key == Qt.Key_Backtab or (key == Qt.Key_Tab and shift):
                self._indent_selection(-1)
                return event.accept()
            if key in (Qt.Key_Return, Qt.Key_Enter) and not ctrl:
                if self._auto_indent:
                    self._insert_newline_with_indent()
                    return event.accept()
            if key == Qt.Key_Backspace and self._auto_close and self._delete_pair():
                return event.accept()
            if self._auto_close and char and not ctrl and not modifiers & Qt.AltModifier:
                if char in _AUTO_CLOSE_CHARS:
                    self._insert_pair(char)
                    return event.accept()
                if char in _REVERSE_PAIRS or char == _QUOTE:
                    if self._skip_closer(char):
                        return event.accept()

        super().keyPressEvent(event)

    def _insert_pair(self, char: str) -> None:
        closer = _PAIRS.get(char, _QUOTE)
        cursor = self.textCursor()
        if cursor.hasSelection():
            selected = cursor.selectedText()
            cursor.insertText(char + selected + closer)
            return
        following = self._char_at(cursor.position())
        if following and following not in ",:}] \n\t":
            cursor.insertText(char)
            return
        cursor.insertText(char + closer)
        cursor.setPosition(cursor.position() - len(closer))
        self.setTextCursor(cursor)

    def _skip_closer(self, char: str) -> bool:
        cursor = self.textCursor()
        if cursor.hasSelection():
            return False
        if self._char_at(cursor.position()) == char:
            cursor.movePosition(QTextCursor.Right)
            self.setTextCursor(cursor)
            return True
        return False

    def _delete_pair(self) -> bool:
        cursor = self.textCursor()
        if cursor.hasSelection():
            return False
        pos = cursor.position()
        before = self._char_at(pos - 1)
        after = self._char_at(pos)
        if _PAIRS.get(before) == after and after:
            cursor.beginEditBlock()
            cursor.deletePreviousChar()
            cursor.deleteChar()
            cursor.endEditBlock()
            return True
        if before == _QUOTE and after == _QUOTE:
            cursor.beginEditBlock()
            cursor.deletePreviousChar()
            cursor.deleteChar()
            cursor.endEditBlock()
            return True
        return False

    def _char_at(self, index: int) -> str:
        """O(1) 取单个字符，避免整份拷贝文档。"""
        if index < 0 or index >= max(0, self.document().characterCount() - 1):
            return ""
        cursor = QTextCursor(self.document())
        cursor.setPosition(index)
        cursor.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor)
        return cursor.selectedText()

    def _insert_newline_with_indent(self) -> None:
        cursor = self.textCursor()
        block_text = cursor.block().text()
        prefix = block_text[: cursor.positionInBlock()]
        indent = prefix[: len(prefix) - len(prefix.lstrip())]
        stripped = prefix.rstrip()
        extra = self._indent_text if stripped.endswith(("{", "[")) else ""
        following = self._char_at(cursor.position())
        if extra and following in ("}", "]"):
            # 在 { 与 } 之间回车：拆成三行，光标停在中间行行尾
            cursor.insertText("\n" + indent + extra + "\n" + indent)
            cursor.setPosition(cursor.position() - len(indent) - 1)
            self.setTextCursor(cursor)
            return
        cursor.insertText("\n" + indent + extra)

    def _indent_selection(self, direction: int) -> bool:
        """对选中的所有行整体缩进/反缩进；无选区时返回 False。"""
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return False
        start_block = self.document().findBlock(cursor.selectionStart())
        end_block = self.document().findBlock(cursor.selectionEnd())
        # 选区正好停在某行行首时，不应该把那一行也算进去
        if end_block != start_block and cursor.selectionEnd() == end_block.position():
            end_block = end_block.previous()
        cursor.beginEditBlock()
        block = start_block
        while block.isValid():
            line_cursor = QTextCursor(block)
            line_cursor.movePosition(QTextCursor.StartOfBlock)
            if direction > 0:
                line_cursor.insertText(self._indent_text)
            else:
                text = block.text()
                remove = 0
                while remove < len(text) and remove < len(self._indent_text) and text[remove] == " ":
                    remove += 1
                if remove == 0 and text.startswith("\t"):
                    remove = 1
                for _ in range(remove):
                    line_cursor.deleteChar()
            if block == end_block:
                break
            block = block.next()
        cursor.endEditBlock()
        return True

    def delete_current_line(self) -> None:
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.StartOfBlock)
        if cursor.block().next().isValid():
            cursor.movePosition(QTextCursor.NextBlock, QTextCursor.KeepAnchor)
        else:
            cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
        cursor.removeSelectedText()

    # ------------------------------------------------------------------ 定位
    def goto_offset(self, offset: int, length: int = 0, *, select: bool = True,
                    center: bool = True) -> None:
        """跳到字符偏移处；用于"点击错误/树节点定位"。"""
        total = max(0, self.document().characterCount() - 1)
        offset = max(0, min(offset, total))
        cursor = self.textCursor()
        cursor.setPosition(offset)
        if select and length > 0:
            cursor.setPosition(min(total, offset + length), QTextCursor.KeepAnchor)
        self.setTextCursor(cursor)
        if center:
            self.centerCursor()
        self.setFocus()

    def goto_line(self, line: int, column: int = 1) -> None:
        line = max(1, min(line, self.blockCount()))
        block = self.document().findBlockByNumber(line - 1)
        if not block.isValid():
            return
        cursor = QTextCursor(block)
        cursor.movePosition(QTextCursor.Right, QTextCursor.MoveAnchor,
                            max(0, min(column - 1, len(block.text()))))
        self.setTextCursor(cursor)
        self.centerCursor()
        self.setFocus()

    def current_line(self) -> int:
        return self.textCursor().blockNumber() + 1

    def current_column(self) -> int:
        return self.textCursor().columnNumber() + 1

    # ------------------------------------------------------------------ 查找高亮
    def attach_find_bar(self, bar) -> None:
        """登记查找条，编辑器尺寸变化时让它重新贴到右上角。"""
        self._find_bar = bar

    def set_find_selections(self, spans: List[Tuple[int, int]], current: int = -1) -> None:
        self._find_spans = list(spans)
        self._find_current = current
        self._refresh_decorations()

    # ------------------------------------------------------------------ 错误标记
    def set_issues(self, issues: List[Issue]) -> None:
        """用校验结果刷新行号槽标记与错误行底色。"""
        self._markers.clear()
        self._error_offset = -1
        self._error_length = 0
        for issue in issues:
            severity = "error" if issue.is_error else "warning"
            if issue.line > 0:
                self._markers.add(issue.line, severity)
            if severity == "error" and self._error_offset < 0:
                self._error_offset = issue.offset
                self._error_length = issue.length
        self._refresh_decorations()
        self._gutter.update()

    def clear_issues(self) -> None:
        self.set_issues([])

    def markers(self) -> MarkerStore:
        return self._markers

    # ------------------------------------------------------------------ 内容
    def set_content(self, text: str, *, keep_undo: bool = False) -> None:
        """设置内容。``keep_undo=False`` 时清空撤销栈（用于载入文件）。"""
        if keep_undo:
            cursor = self.textCursor()
            cursor.select(QTextCursor.Document)
            cursor.insertText(text)
            self.setTextCursor(QTextCursor(self.document()))
        else:
            self.setPlainText(text)
        self.document().setModified(False)
        self._on_text_changed()

    def text_value(self) -> str:
        return self.toPlainText()

    def text(self) -> str:
        """``toPlainText`` 的别名，供功能页统一调用。"""
        return self.toPlainText()

    def set_text(self, text: str, *, keep_undo: bool = False) -> None:
        """``set_content`` 的别名，供功能页统一调用。"""
        self.set_content(text, keep_undo=keep_undo)

    def is_empty(self) -> bool:
        return not self.toPlainText().strip()

    # ------------------------------------------------------------------ 拖拽文件
    def canInsertFromMimeData(self, mime) -> bool:  # noqa: N802 (Qt 命名)
        return mime.hasUrls() or super().canInsertFromMimeData(mime)

    def insertFromMimeData(self, mime) -> None:  # noqa: N802 (Qt 命名)
        if mime.hasUrls():
            self.dropped_files.emit([url.toLocalFile() for url in mime.urls()])
            return
        super().insertFromMimeData(mime)

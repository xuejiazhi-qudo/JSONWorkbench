"""JSON/JSONC structural folding. Visibility changes never alter document text."""

from dataclasses import dataclass
import re

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QTextBlock, QTextCursor


# Consume strings and comments as units, so their brackets are never structural.
_TOKENS = re.compile(r'"(?:\\[\s\S]|[^"\\])*(?:"|$)|//[^\r\n]*|/\*[\s\S]*?(?:\*/|$)|[{}\[\]]')


def fold_ranges(text):
    """Return opening line -> (closing line, closing delimiter), zero-based."""
    stack, ranges = [], {}
    line = previous = 0
    for match in _TOKENS.finditer(text):
        line += text.count('\n', previous, match.start())
        previous = match.start()
        token = match.group()
        if token in ('{', '['):
            stack.append((token, line))
        elif token in ('}', ']'):
            if not stack or {'}': '{', ']': '['}[token] != stack[-1][0]:
                stack.clear()
                continue
            _, start = stack.pop()
            if line > start and (start not in ranges or line >= ranges[start][0]):
                ranges[start] = (line, token)
    return ranges


@dataclass
class FoldRegion:
    start: QTextBlock
    end: QTextBlock
    closing: str


class FoldingController(QObject):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.regions = {}
        self.collapsed = set()
        self._hidden = []
        self._remembered = []
        self._applying = False
        self._dirty = True
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self.rebuild)
        editor.document().contentsChange.connect(self._contents_changed)

    def reset(self):
        self._timer.stop()
        self._show_hidden()
        self.regions.clear()
        self.collapsed.clear()
        self._remembered.clear()
        self._dirty = True

    def _contents_changed(self, position, removed, added):
        if self._applying or not (removed or added):
            return
        self._remembered.extend(self.regions[line].start for line in self.collapsed
                                if line in self.regions)
        self._show_hidden()
        self.regions.clear()
        self.collapsed.clear()
        self._dirty = True
        self._timer.start()

    def rebuild(self):
        if not self._dirty:
            return
        self._timer.stop()
        document = self.editor.document()
        ranges = fold_ranges(self.editor.toPlainText())
        self.regions = {
            start: FoldRegion(document.findBlockByNumber(start),
                              document.findBlockByNumber(end), closing)
            for start, (end, closing) in ranges.items()
        }
        self.collapsed = {block.blockNumber() for block in self._remembered
                          if block.isValid() and block.blockNumber() in self.regions}
        self._remembered.clear()
        self._dirty = False
        self._apply_visibility()

    def toggle(self, line):
        self.rebuild()
        if line not in self.regions:
            return False
        if line in self.collapsed:
            self.collapsed.remove(line)
        else:
            self.collapsed.add(line)
        self._apply_visibility()
        return True

    def collapse_all(self):
        self.rebuild()
        self.collapsed = set(self.regions)
        self._apply_visibility()

    def expand_all(self):
        self._remembered.clear()
        self.collapsed.clear()
        self._show_hidden()
        self._refresh_layout()

    def reveal_cursor(self):
        if self._applying or self._dirty or not self.collapsed:
            return
        cursor = self.editor.textCursor()
        document = self.editor.document()
        first = document.findBlock(cursor.selectionStart()).blockNumber()
        last = document.findBlock(cursor.selectionEnd()).blockNumber()
        opened = {start for start in self.collapsed
                  if first <= self.regions[start].end.blockNumber() and last > start}
        if opened:
            self.collapsed.difference_update(opened)
            self._apply_visibility()

    def _show_hidden(self):
        for block in self._hidden:
            if block.isValid():
                block.setVisible(True)
                block.setLineCount(1)
        self._hidden.clear()

    def _apply_visibility(self):
        self._applying = True
        try:
            self._show_hidden()
            document = self.editor.document()
            # Merge nested intervals; never visit the same hidden line twice.
            intervals = []
            for start in sorted(self.collapsed):
                end = self.regions[start].end.blockNumber()
                if intervals and start + 1 <= intervals[-1][1]:
                    intervals[-1] = (intervals[-1][0], max(end, intervals[-1][1]))
                else:
                    intervals.append((start + 1, end))
            cursor = self.editor.textCursor()
            for first, last in intervals:
                if first <= cursor.blockNumber() <= last:
                    cursor = QTextCursor(document.findBlockByNumber(first - 1))
                    cursor.movePosition(QTextCursor.EndOfBlock)
                    self.editor.setTextCursor(cursor)
                block = document.findBlockByNumber(first)
                while block.isValid() and block.blockNumber() <= last:
                    block.setVisible(False)
                    block.setLineCount(0)
                    self._hidden.append(QTextBlock(block))
                    block = block.next()
            self._refresh_layout()
        finally:
            self._applying = False

    def _refresh_layout(self):
        document = self.editor.document()
        document.markContentsDirty(0, document.characterCount())
        layout = document.documentLayout()
        layout.documentSizeChanged.emit(layout.documentSize())
        self.editor.viewport().update()
        self.editor._gutter.update()

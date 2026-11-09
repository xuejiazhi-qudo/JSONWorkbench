"""行号区：显示行号，并在出错/告警行画上标记。"""

from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QPlainTextEdit, QWidget

MARKER_RADIUS = 3


class LineNumberArea(QWidget):
    """编辑器左侧的行号槽（gutter）。"""

    def __init__(self, editor: QPlainTextEdit) -> None:
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt 命名)
        return QSize(self._editor.line_number_width(), 0)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        self._editor.paint_line_numbers(self, event)


class MarkerStore:
    """行号 -> 标记类型（error / warning），供 gutter 与状态栏共用。"""

    def __init__(self) -> None:
        self._marks: Dict[int, str] = {}

    def clear(self) -> None:
        self._marks.clear()

    def add(self, line: int, severity: str) -> None:
        if severity == "error" or self._marks.get(line) != "error":
            self._marks[line] = severity

    def get(self, line: int) -> Optional[str]:
        return self._marks.get(line)

    def all(self) -> Dict[int, str]:
        return dict(self._marks)

    def __len__(self) -> int:
        return len(self._marks)


def paint_gutter(editor: QPlainTextEdit, area: QWidget, event, palette,
                 markers: MarkerStore, show_numbers: bool = True) -> None:
    """把行号绘制逻辑抽出来，方便编辑器与测试复用。"""
    painter = QPainter(area)
    painter.fillRect(event.rect(), QColor(palette.gutter_bg))

    block = editor.firstVisibleBlock()
    block_number = block.blockNumber()
    top = round(editor.blockBoundingGeometry(block).translated(editor.contentOffset()).top())
    bottom = top + round(editor.blockBoundingRect(block).height())
    width = area.width()

    font = QFont(editor.font())
    font.setPointSizeF(max(7.0, font.pointSizeF() - 1))
    painter.setFont(font)

    while block.isValid() and top <= event.rect().bottom():
        if block.isVisible() and bottom >= event.rect().top():
            number = block_number + 1
            mark = markers.get(number)
            if mark:
                color = QColor(palette.danger if mark == "error" else palette.warning)
                center_y = top + (bottom - top) / 2
                painter.setPen(Qt.NoPen)
                painter.setBrush(color)
                painter.drawEllipse(
                    QRect(int(width - 2 * MARKER_RADIUS - 3), int(center_y - MARKER_RADIUS),
                          2 * MARKER_RADIUS, 2 * MARKER_RADIUS))
                painter.setBrush(Qt.NoBrush)
            if show_numbers:
                painter.setPen(QColor(palette.gutter_text))
                right = width - (2 * MARKER_RADIUS + 8)
                painter.drawText(0, top, right, bottom - top,
                                 Qt.AlignRight | Qt.AlignVCenter, str(number))
        block = block.next()
        top = bottom
        bottom = top + round(editor.blockBoundingRect(block).height())
        block_number += 1

    painter.end()

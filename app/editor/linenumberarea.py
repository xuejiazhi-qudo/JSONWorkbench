"""行号区：显示行号，并在出错/告警行画上标记。"""

from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtCore import QPointF, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPolygonF
from PySide6.QtWidgets import QPlainTextEdit, QWidget
from app.i18n import tr

MARKER_RADIUS = 3
FOLD_WIDTH = 18


def next_visible_candidate(editor, block):
    region = editor.folding.regions.get(block.blockNumber())
    if region is not None and block.blockNumber() in editor.folding.collapsed:
        return region.end.next()
    return block.next()


class LineNumberArea(QWidget):
    """编辑器左侧的行号槽（gutter）。"""

    def __init__(self, editor: QPlainTextEdit) -> None:
        super().__init__(editor)
        self._editor = editor
        self.setMouseTracking(True)

    def _fold_line_at(self, position):
        if position.x() < self.width() - FOLD_WIDTH:
            return None
        block = self._editor.firstVisibleBlock()
        while block.isValid():
            top = self._editor.blockBoundingGeometry(block).translated(self._editor.contentOffset()).top()
            height = self._editor.blockBoundingRect(block).height()
            if top > position.y():
                break
            if block.isVisible() and top <= position.y() < top + height:
                line = block.blockNumber()
                return line if line in self._editor.folding.regions else None
            block = next_visible_candidate(self._editor, block)
        return None

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._editor.folding.rebuild()
            line = self._fold_line_at(event.position())
            if line is not None:
                self._editor.folding.toggle(line)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        line = self._fold_line_at(event.position())
        self.setCursor(Qt.PointingHandCursor if line is not None else Qt.ArrowCursor)
        self.setToolTip((tr("Expand") if line in self._editor.folding.collapsed else tr("Collapse"))
                        if line is not None else "")
        super().mouseMoveEvent(event)

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
                    QRect(int(width - FOLD_WIDTH - 2 * MARKER_RADIUS - 3), int(center_y - MARKER_RADIUS),
                          2 * MARKER_RADIUS, 2 * MARKER_RADIUS))
                painter.setBrush(Qt.NoBrush)
            if show_numbers:
                painter.setPen(QColor(palette.gutter_text))
                right = width - FOLD_WIDTH - (2 * MARKER_RADIUS + 8)
                painter.drawText(0, top, right, bottom - top,
                                 Qt.AlignRight | Qt.AlignVCenter, str(number))
            if block_number in editor.folding.regions:
                x = width - FOLD_WIDTH / 2
                y = top + editor.fontMetrics().height() / 2
                if block_number in editor.folding.collapsed:
                    points = [(x - 2, y - 4), (x + 3, y), (x - 2, y + 4)]
                else:
                    points = [(x - 4, y - 2), (x + 4, y - 2), (x, y + 3)]
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(palette.gutter_text))
                painter.drawPolygon(QPolygonF([QPointF(px, py) for px, py in points]))
                painter.setBrush(Qt.NoBrush)
        block = next_visible_candidate(editor, block)
        top = bottom
        bottom = top + round(editor.blockBoundingRect(block).height())
        block_number = block.blockNumber()

    painter.end()

"""数据统计页：节点/类型/深度/大小统计 + 结构分析。"""

from __future__ import annotations

from app.utils.pagejob import run_latest
from app.widgets.tabwidget import ASYNC_VALIDATE_CHARS

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QWidget,
)

from app.core.jsonparser import parse_json
from app.engines.scanners import analyze_size, analyze_structure
from app.engines.statistics import compute_statistics, format_bytes, merge_parser_stats, statistics_as_rows
from app.i18n import current_language, register_retranslate, tr
from app.widgets.icons import icon
from app.widgets.page import PageShell

STAT_LABELS = {
    "node.count": ("Total nodes", "节点总数"),
    "node.objects": ("Objects", "对象数"),
    "node.arrays": ("Arrays", "数组数"),
    "node.strings": ("Strings", "字符串数"),
    "node.numbers": ("Numbers", "数字数"),
    "node.booleans": ("Booleans", "布尔数"),
    "node.nulls": ("Nulls", "空值数"),
    "node.leaf_ratio": ("Leaf ratio", "叶子节点占比"),
    "key.total": ("Keys (total)", "键总数"),
    "key.unique": ("Keys (unique)", "唯一键数"),
    "key.reused": ("Reused key names", "被复用的键名数"),
    "key.top": ("Most frequent keys", "最常见键"),
    "depth.max": ("Max nesting depth", "最大嵌套层级"),
    "depth.histogram": ("Depth histogram", "深度分布"),
    "string.chars_total": ("String characters (total)", "字符串字符总数"),
    "string.chars_longest": ("Longest string (chars)", "最长字符串字符数"),
    "string.longest_path": ("Longest string path", "最长字符串路径"),
    "array.items_total": ("Array items (total)", "数组元素总数"),
    "array.length_max": ("Longest array (items)", "最长数组元素数"),
    "array.longest_path": ("Longest array path", "最长数组路径"),
    "number.integer_count": ("Integers", "整数个数"),
    "number.float_count": ("Decimals", "小数个数"),
    "number.min": ("Minimum number", "最小数字"),
    "number.max": ("Maximum number", "最大数字"),
    "number.sum": ("Sum of numbers", "数字总和"),
    "size.compact": ("Compact size", "压缩后大小"),
    "size.pretty": ("Pretty size", "格式化后大小"),
    "nodes.largest": ("Largest nodes", "最大节点"),
    "timing.elapsed_ms": ("Analysis time", "分析耗时"),
}


def stat_label(key: str) -> str:
    en, zh = STAT_LABELS.get(key, (key.replace(".", " ").capitalize(), key))
    return zh if current_language() == "zh" else en


class StatsPage(PageShell):
    """侧边栏"统计"页。"""

    status_message = Signal(str, int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Statistics",
                         "Node counts, type distribution, depth, sizes and structural analysis "
                         "of the current document.")
        self._text = ""
        self._name = ""

        self.source_label = QLabel("")
        self.source_label.setObjectName("HintLabel")

        self.load_button = QPushButton(tr("Load from editor"))
        self.load_button.setIcon(icon("upload", size=15, kind="muted"))
        self.load_button.clicked.connect(self.load_from_workspace)

        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.setIcon(icon("refresh", size=15, kind="muted"))
        self.refresh_button.clicked.connect(self.refresh)

        top = QHBoxLayout()
        top.addWidget(self.source_label, 1)
        top.addWidget(self.load_button)
        top.addWidget(self.refresh_button)

        self.rows_tree = QTreeWidget()
        self.rows_tree.setHeaderLabels([tr("Metric"), tr("Value")])
        self.rows_tree.setRootIsDecorated(False)
        self.rows_tree.setAlternatingRowColors(True)
        self.rows_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.rows_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

        self.structure_tree = QTreeWidget()
        self.structure_tree.setHeaderLabels([tr("Item"), tr("Count"), tr("Example / detail")])
        self.structure_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

        self.largest_tree = QTreeWidget()
        self.largest_tree.setHeaderLabels([tr("Path"), tr("Size"), tr("Type")])
        self.largest_tree.setRootIsDecorated(False)
        self.largest_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

        self.detail_tabs = QTabWidget()
        self.detail_tabs.addTab(self.structure_tree, tr("Structure"))
        self.detail_tabs.addTab(self.largest_tree, tr("Largest nodes"))

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.rows_tree)
        splitter.addWidget(self.detail_tabs)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)

        self.add_layout(top)
        self.add(splitter, 1)
        register_retranslate(self)

    # ------------------------------------------------------------------ 数据
    def set_document(self, text: str, name: str = "") -> None:
        self._text = text
        self._name = name
        self.refresh()

    def load_from_workspace(self) -> None:
        window = self.window()
        workspace = getattr(window, "workspace", None)
        tab = workspace.current_tab() if workspace is not None else None
        if tab is None:
            return
        self.set_document(tab.text(), tab.document.display_name)

    def refresh(self) -> None:
        self.source_label.setText(tr("Source: {name}").format(name=self._name or tr("(none)")))
        self.rows_tree.clear()
        self.structure_tree.clear()
        self.largest_tree.clear()
        if not self._text.strip():
            return
        text = self._text
        run_latest(self, lambda: _analyze(text), self._show_analysis,
                   lambda: self._text == text,
                   background=len(text) > ASYNC_VALIDATE_CHARS)

    def _show_analysis(self, result) -> None:
        parsed, stats, report, sizes = result
        if not parsed.ok:
            self.rows_tree.addTopLevelItem(QTreeWidgetItem(
                [tr("Invalid JSON"), tr("Fix the document first.")]))
            return
        for key, value_text in statistics_as_rows(stats):
            item = QTreeWidgetItem([stat_label(key), value_text])
            item.setToolTip(1, value_text)
            self.rows_tree.addTopLevelItem(item)
        self._fill_structure(report)
        for path_string, size, kind in sizes:
            self.largest_tree.addTopLevelItem(
                QTreeWidgetItem([path_string, format_bytes(size), kind]))

    def _fill_structure(self, report) -> None:
        def section(title: str, rows, columns=3) -> None:
            parent = QTreeWidgetItem([tr(title), "", ""])
            self.structure_tree.addTopLevelItem(parent)
            for row in rows:
                cells = [str(cell) for cell in row[:columns]]
                while len(cells) < columns:
                    cells.append("")
                parent.addChild(QTreeWidgetItem(cells))
            parent.setExpanded(len(rows) <= 12)

        section("Object shapes", [(sig, count, example)
                                   for sig, count, example in report.shape_signatures[:40]])
        section("Recursive shapes", [(sig, ",".join(str(d) for d in depths), example)
                                      for sig, depths, example in report.recursive_shapes[:20]])
        section("Key type conflicts", [(key, ",".join(types), example)
                                        for key, types, example in report.key_type_conflicts[:40]])
        section("Array homogeneity", [(path, length, ",".join(types) if not homogeneous
                                        else tr("homogeneous"), "")
                                       for path, length, types, homogeneous
                                       in report.array_homogeneity[:40]])

    # ------------------------------------------------------------------ 主题
    def apply_theme(self) -> None:
        """与其它功能页保持一致的接口；本页没有需要重新着色的编辑器。"""

    # ------------------------------------------------------------------ 翻译
    def retranslate(self) -> None:
        super().retranslate()
        self.load_button.setText(tr("Load from editor"))
        self.refresh_button.setText(tr("Refresh"))
        self.rows_tree.setHeaderLabels([tr("Metric"), tr("Value")])
        self.structure_tree.setHeaderLabels([tr("Item"), tr("Count"), tr("Example / detail")])
        self.largest_tree.setHeaderLabels([tr("Path"), tr("Size"), tr("Type")])
        self.detail_tabs.setTabText(0, tr("Structure"))
        self.detail_tabs.setTabText(1, tr("Largest nodes"))
        self.refresh()


def _analyze(text):
    parsed = parse_json(text)
    if not parsed.ok:
        return parsed, None, None, []
    stats = merge_parser_stats(compute_statistics(parsed.value), parsed)
    return parsed, stats, analyze_structure(parsed.value), analyze_size(parsed.value, top_n=30)

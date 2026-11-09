"""JSONPath 查询页。

输入表达式 → 对当前文档（或本页编辑器里的文档）执行查询 →
结果列表（路径 + 值预览），点击可定位到树节点 / 编辑器，
可复制结果、导出结果；常用示例一键填入。
"""

from __future__ import annotations

from app.utils.pagejob import run_latest, is_large_value
from app.widgets.tabwidget import ASYNC_VALIDATE_CHARS

from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.jsonfilemanager import write_text_file
from app.core.jsonformatter import dumps
from app.core.jsonparser import parse_json
from app.engines import jsonpathq
from app.history.store import KIND_QUERY, HistoryStore
from app.i18n import register_retranslate, tr
from app.editor.jsoneditor import JsonEditor
from app.settings.settings import settings
from app.utils import dialogs
from app.widgets.icons import icon
from app.widgets.page import PageShell
from app.widgets.theme import theme


class JsonPathPage(PageShell):
    """侧边栏"JSONPath 查询"页。"""

    status_message = Signal(str, int)
    locate_in_tree = Signal(tuple)
    jump_in_editor = Signal(int, int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("JSONPath Query",
                         "Query the current document with JSONPath: $..book[?(@.price < 10)], "
                         "$.users[*].name, slices, unions, filters and functions.")
        self._value = None
        self._document_name = ""
        self._offsets = None

        self.expression_edit = QLineEdit()
        self.expression_edit.setPlaceholderText(tr("$.store.book[?(@.price < 10)].title"))
        self.expression_edit.returnPressed.connect(self.run_query)

        self.examples_combo = QComboBox()
        self.examples_combo.addItem(tr("Examples…"), "")
        for expression, description in jsonpathq.EXAMPLES:
            self.examples_combo.addItem(f"{expression}  —  {tr(description)}", expression)
        self.examples_combo.currentIndexChanged.connect(self._on_example)

        self.recent_combo = QComboBox()
        self.recent_combo.addItem(tr("Recent…"), "")
        self.recent_combo.setMaximumWidth(220)
        self.recent_combo.currentIndexChanged.connect(self._on_recent)
        self._refresh_recent()

        self.run_button = QPushButton(tr("Run query"))
        self.run_button.setProperty("class", "primary")
        self.run_button.setIcon(icon("play", size=14, kind="on_accent"))
        self.run_button.setCursor(Qt.PointingHandCursor)
        self.run_button.clicked.connect(self.run_query)

        self.load_button = QPushButton(tr("Load from editor"))
        self.load_button.setIcon(icon("upload", size=15, kind="muted"))
        self.load_button.clicked.connect(self.load_from_workspace)

        self.open_button = QPushButton(tr("Open file…"))
        self.open_button.setIcon(icon("folder-open", size=15, kind="muted"))
        self.open_button.clicked.connect(self.open_file)

        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(QLabel(tr("Expression:")))
        top.addWidget(self.expression_edit, 1)
        top.addWidget(self.examples_combo)
        top.addWidget(self.recent_combo)
        top.addWidget(self.run_button)
        top.addWidget(self.load_button)
        top.addWidget(self.open_button)

        self.source_label = QLabel("")
        self.source_label.setObjectName("HintLabel")

        self.result_tree = QTreeWidget()
        self.result_tree.setHeaderLabels([tr("Path"), tr("Value")])
        self.result_tree.setRootIsDecorated(False)
        self.result_tree.setAlternatingRowColors(True)
        self.result_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.result_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.result_tree.setColumnWidth(0, 380)
        self.result_tree.itemActivated.connect(self._on_activated)

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("MutedLabel")
        self.error_label = QLabel("")
        self.error_label.setObjectName("BadgeError")
        self.error_label.setWordWrap(True)

        self.locate_button = QPushButton(tr("Locate in tree"))
        self.locate_button.setIcon(icon("tree", size=15, kind="muted"))
        self.locate_button.clicked.connect(self._locate_selected)
        self.jump_button = QPushButton(tr("Jump to editor"))
        self.jump_button.setIcon(icon("target", size=15, kind="muted"))
        self.jump_button.clicked.connect(self._jump_selected)
        self.copy_button = QPushButton(tr("Copy results"))
        self.copy_button.setIcon(icon("copy", size=15, kind="muted"))
        self.copy_button.clicked.connect(self.copy_results)
        self.export_button = QPushButton(tr("Export results…"))
        self.export_button.setIcon(icon("download", size=15, kind="muted"))
        self.export_button.clicked.connect(self.export_results)

        actions = QHBoxLayout()
        actions.addWidget(self.summary_label, 1)
        actions.addWidget(self.locate_button)
        actions.addWidget(self.jump_button)
        actions.addWidget(self.copy_button)
        actions.addWidget(self.export_button)

        self.preview_editor = JsonEditor(read_only=True)

        splitter = QSplitter(Qt.Vertical)
        top_widget = QWidget()
        top_layout = QVBoxLayout(top_widget)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addWidget(self.result_tree)
        splitter.addWidget(top_widget)
        splitter.addWidget(self.preview_editor)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        self.add_layout(top)
        self.add(self.source_label)
        self.add(self.error_label)
        self.add(splitter, 1)
        self.add_layout(actions)
        register_retranslate(self)

    # ------------------------------------------------------------------ 文档
    def set_document(self, value, name: str = "") -> None:
        self._value = value
        self._document_name = name
        self.source_label.setText(tr("Source: {name}").format(name=name or tr("(none)")))

    def set_offsets(self, offsets) -> None:
        """节点偏移表，供"跳转到编辑器"使用。"""
        self._offsets = offsets

    def load_from_workspace(self) -> None:
        window = self.window()
        workspace = getattr(window, "workspace", None)
        tab = workspace.current_tab() if workspace is not None else None
        if tab is None:
            return
        parsed = parse_json(tab.text(), tab.document.mode)
        if not parsed.ok:
            dialogs.warn(self, tr("JSONPath Query"), tr("The document is not valid JSON."))
            return
        self.set_document(parsed.value, tab.document.display_name)
        self._offsets = getattr(parsed, "offsets", None)
        self.status_message.emit(tr("Loaded {name}.").format(name=tab.document.display_name), 3000)

    def open_file(self) -> None:
        path, _selected = QFileDialog.getOpenFileName(self, tr("Open JSON file"),
                                                      settings.last_dir, "*.json;;*.*")
        if not path:
            return
        from app.core.jsonfilemanager import read_text_file

        result = read_text_file(path)
        if not result.ok:
            dialogs.warn(self, tr("JSONPath Query"), result.error)
            return
        parsed = parse_json(result.text)
        if not parsed.ok:
            dialogs.warn(self, tr("JSONPath Query"), tr("The document is not valid JSON."))
            return
        self.set_document(parsed.value, result.path)
        self._offsets = parsed.offsets

    def _on_example(self, index: int) -> None:
        expression = self.examples_combo.currentData()
        if expression:
            self.expression_edit.setText(expression)
            self.run_query()

    def _refresh_recent(self) -> None:
        self.recent_combo.blockSignals(True)
        self.recent_combo.clear()
        self.recent_combo.addItem(tr("Recent…"), "")
        store = HistoryStore()
        try:
            for expression in store.distinct_labels(KIND_QUERY, limit=15):
                self.recent_combo.addItem(expression, expression)
        finally:
            store.close()
        self.recent_combo.blockSignals(False)

    def _on_recent(self, index: int) -> None:
        expression = self.recent_combo.currentData()
        if expression:
            self.expression_edit.setText(expression)
            self.run_query()

    # ------------------------------------------------------------------ 查询
    def run_query(self) -> None:
        expression = self.expression_edit.text().strip()
        self.result_tree.clear()
        self.preview_editor.set_text("")
        self.error_label.setText("")
        if not expression:
            return
        if self._value is None:
            self.load_from_workspace()
        if self._value is None:
            dialogs.warn(self, tr("JSONPath Query"), tr("No document is open."))
            return
        value = self._value
        run_latest(
            self, lambda: jsonpathq.query(value, expression),
            lambda result: self._show_query(result, expression),
            lambda: self._value is value and self.expression_edit.text().strip() == expression,
            background=is_large_value(value))

    def _show_query(self, result, expression) -> None:
        if not result.ok:
            self.error_label.setText(tr("Query error: {message}").format(message=result.error))
            self.summary_label.setText("")
            return
        self._matches: List = result.matches
        for match in result.matches[:5000]:
            item = QTreeWidgetItem([match.path_string, _preview(match.value)])
            item.setData(0, Qt.ItemDataRole.UserRole, match.path)
            self.result_tree.addTopLevelItem(item)
        suffix = " +" if result.truncated else ""
        self.summary_label.setText(tr("{n} match(es){suffix} · {ms:.1f} ms").format(
            n=len(result.matches), suffix=suffix, ms=result.elapsed_ms))
        self._record_history(expression, len(result.matches))
        self._refresh_recent()
        if result.matches:
            self._show_preview([m.value for m in result.matches])

    _matches: List = []

    def _record_history(self, expression: str, count: int) -> None:
        store = HistoryStore()
        try:
            store.record(KIND_QUERY, expression, detail=f"{count} matches",
                         path=self._document_name)
        finally:
            store.close()

    def _show_preview(self, values: list) -> None:
        if len(values) == 1:
            self.preview_editor.set_text(dumps(values[0]))
        else:
            self.preview_editor.set_text(dumps(values))

    # ------------------------------------------------------------------ 结果动作
    def _selected_path(self) -> Optional[tuple]:
        item = self.result_tree.currentItem()
        if item is None:
            return None
        return item.data(0, Qt.ItemDataRole.UserRole)

    def _on_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        self._locate_selected()

    def _locate_selected(self) -> None:
        path = self._selected_path()
        if path is None:
            return
        self.locate_in_tree.emit(tuple(path))
        self.status_message.emit(tr("Located in the tree panel."), 3000)

    def _jump_selected(self) -> None:
        path = self._selected_path()
        if path is None or self._offsets is None:
            return
        info = self._offsets.get(tuple(path))
        if info is None:
            self.status_message.emit(tr("No editor offset available for this node."), 4000)
            return
        self.jump_in_editor.emit(info.value_offset, info.value_length)

    def copy_results(self) -> None:
        values = [match.value for match in self._matches]
        if not values:
            self.status_message.emit(tr("Nothing to copy."), 3000)
            return
        QGuiApplication.clipboard().setText(dumps(values if len(values) > 1 else values[0]))
        self.status_message.emit(tr("Copied {n} result(s).").format(n=len(values)), 3000)

    def export_results(self) -> None:
        if not self._matches:
            self.status_message.emit(tr("Nothing to export."), 3000)
            return
        path, _selected = QFileDialog.getSaveFileName(self, tr("Export results"),
                                                      "jsonpath-results.json",
                                                      "*.json;;All files (*)")
        if not path:
            return
        payload = [{"path": match.path_string, "value": match.value} for match in self._matches]
        result = write_text_file(path, dumps(payload))
        if result.ok:
            self.status_message.emit(tr("Saved {path}").format(path=path), 5000)
        else:
            dialogs.warn(self, tr("JSONPath Query"), result.error)

    # ------------------------------------------------------------------ 主题/翻译
    def apply_theme(self) -> None:
        self.preview_editor.apply_theme(theme.palette(), settings.font_family)

    def retranslate(self) -> None:
        super().retranslate()
        self.expression_edit.setPlaceholderText(tr("$.store.book[?(@.price < 10)].title"))
        self.examples_combo.setItemText(0, tr("Examples…"))
        self.recent_combo.setItemText(0, tr("Recent…"))
        for index, (expression, description) in enumerate(jsonpathq.EXAMPLES, start=1):
            self.examples_combo.setItemText(index, f"{expression}  —  {tr(description)}")
        self.run_button.setText(tr("Run query"))
        self.load_button.setText(tr("Load from editor"))
        self.open_button.setText(tr("Open file…"))
        self.result_tree.setHeaderLabels([tr("Path"), tr("Value")])
        self.locate_button.setText(tr("Locate in tree"))
        self.jump_button.setText(tr("Jump to editor"))
        self.copy_button.setText(tr("Copy results"))
        self.export_button.setText(tr("Export results…"))
        self.source_label.setText(tr("Source: {name}").format(
            name=self._document_name or tr("(none)")))


def _preview(value) -> str:
    from app.models.jsontreemodel import value_preview

    return value_preview(value, 120)

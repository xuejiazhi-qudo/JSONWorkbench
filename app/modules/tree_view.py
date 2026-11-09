"""树形查看器面板：结构树 + 节点属性。

支持展开/折叠、搜索高亮、复制（节点/key/value/JSONPath）、
编辑值、增删节点、双击定位到编辑器。所有修改都会回写到编辑器文本
（保留撤销栈），因此"改树"和"改文本"永远一致。
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QHeaderView,
    QMenu,
    QPlainTextEdit,
    QStackedWidget,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from app.core.jsonformatter import dumps
from app.core.jsonparser import parse_json
from app.core.jsonvalue import (
    TYPE_ARRAY,
    TYPE_OBJECT,
    json_type,
    number_literal,
)
from app.i18n import register_retranslate, tr
from app.models.jsontreemodel import (
    COL_KEY,
    COL_VALUE,
    JsonTreeModel,
    path_to_string,
)
from app.utils import dialogs
from app.widgets.icons import icon
from app.widgets.theme import theme

TYPE_CHOICES = (
    ("String", "string"),
    ("Number", "number"),
    ("Boolean", "boolean"),
    ("Null", "null"),
    ("Object", TYPE_OBJECT),
    ("Array", TYPE_ARRAY),
)

EXPAND_ALL_LIMIT = 20_000


def parse_scalar_text(kind: str, text: str):
    """把对话框里的文本转成 JSON 值；返回 (ok, value, error)。"""
    if kind == "string":
        return True, text, ""
    if kind == "null":
        return True, None, ""
    if kind == "number":
        result = parse_json(text.strip())
        if not result.ok or json_type(result.value) != "number":
            return False, None, tr("Not a valid JSON number.")
        return True, result.value, ""
    if kind in (TYPE_OBJECT, TYPE_ARRAY):
        result = parse_json(text)
        if not result.ok:
            title = result.error.code if result.error else "error"
            return False, None, tr("Invalid JSON: {code}").format(code=title)
        if json_type(result.value) != kind:
            return False, None, tr("Expected {kind}.").format(kind=kind)
        return True, result.value, ""
    return False, None, tr("Unsupported type.")


class NodeEditDialog(QDialog):
    """编辑节点值 / 新增子节点。"""

    def __init__(self, parent: Optional[QWidget] = None, *, mode: str = "edit",
                 key: str = "", kind: str = "string", value: Any = None,
                 parent_is_object: bool = True, allow_key: bool = False) -> None:
        super().__init__(parent)
        self._mode = mode
        self.setWindowTitle(tr("Edit value") if mode == "edit" else tr("Add node"))
        self.setMinimumWidth(420)

        form = QFormLayout()
        self.key_edit = QLineEdit(key)
        self.key_edit.setPlaceholderText(tr("key name"))
        self.key_row_visible = allow_key
        self.key_edit.setVisible(allow_key)
        form.addRow(tr("Key"), self.key_edit)

        self.type_combo = QComboBox()
        for label, data in TYPE_CHOICES:
            self.type_combo.addItem(tr(label), data)
        index = self.type_combo.findData(kind)
        self.type_combo.setCurrentIndex(max(0, index))
        if mode == "edit":
            self.type_combo.setEnabled(kind in ("string", "number", "boolean", "null"))
        self.type_combo.currentIndexChanged.connect(self._sync_stack)
        form.addRow(tr("Type"), self.type_combo)

        self.stack = QStackedWidget()
        self.text_edit = QLineEdit()
        self.bool_check = QCheckBox(tr("true"))
        self.null_label = QLabel(tr("null (no value)"))
        self.json_edit = QPlainTextEdit()
        self.json_edit.setFixedHeight(140)
        self.stack.addWidget(self.text_edit)      # 0 string/number
        self.stack.addWidget(self.bool_check)     # 1 boolean
        self.stack.addWidget(self.null_label)     # 2 null
        self.stack.addWidget(self.json_edit)      # 3 object/array
        form.addRow(tr("Value"), self.stack)

        self.error_label = QLabel("")
        self.error_label.setObjectName("BadgeError")
        self.error_label.setWordWrap(True)
        self.error_label.hide()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.error_label)
        layout.addWidget(buttons)

        self._load_value(kind, value)
        self._sync_stack()
        register_retranslate(self)

    # ------------------------------------------------------------------ 内部
    def _load_value(self, kind: str, value: Any) -> None:
        if kind == "string":
            self.text_edit.setText(value if isinstance(value, str) else "")
            self.stack.setCurrentIndex(0)
        elif kind == "number":
            self.text_edit.setText("" if value in (None, "") else number_literal(value))
            self.stack.setCurrentIndex(0)
        elif kind == "boolean":
            self.bool_check.setChecked(bool(value))
            self.stack.setCurrentIndex(1)
        elif kind == "null":
            self.stack.setCurrentIndex(2)
        else:
            self.json_edit.setPlainText(dumps(value) if value is not None else "")
            self.stack.setCurrentIndex(3)

    def _sync_stack(self, *_args) -> None:
        kind = self.type_combo.currentData()
        self.stack.setCurrentIndex({"string": 0, "number": 0, "boolean": 1,
                                    "null": 2}.get(kind, 3))
        self.key_edit.setVisible(self.key_row_visible)

    def _accept(self) -> None:
        kind = self.type_combo.currentData()
        if kind in ("string", "number"):
            text = self.text_edit.text()
        elif kind == "boolean":
            text = ""
        elif kind == "null":
            text = ""
        else:
            text = self.json_edit.toPlainText()
        if kind == "boolean":
            ok, value, error = True, self.bool_check.isChecked(), ""
        else:
            ok, value, error = parse_scalar_text(kind, text)
        if not ok:
            self.error_label.setText(error)
            self.error_label.show()
            return
        if self.key_row_visible:
            key = self.key_edit.text()
            if not key:
                self.error_label.setText(tr("The key must not be empty."))
                self.error_label.show()
                return
            self.result_key = key
        self.result_value = value
        self.result_kind = kind
        self.accept()

    result_key: Optional[str] = None
    result_value: Any = None
    result_kind: str = "string"

    def retranslate(self) -> None:
        self.setWindowTitle(tr("Edit value") if self._mode == "edit" else tr("Add node"))
        for index, (label, _data) in enumerate(TYPE_CHOICES):
            self.type_combo.setItemText(index, tr(label))
        self.bool_check.setText(tr("true"))
        self.null_label.setText(tr("null (no value)"))


class JsonTreeView(QTreeView):
    """带右键菜单与搜索高亮的树视图。"""

    jump_requested = Signal(int, int)
    edit_requested = Signal(QModelIndex)
    delete_requested = Signal(QModelIndex)
    add_requested = Signal(QModelIndex)

    def __init__(self, model: JsonTreeModel, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setModel(model)
        self.setHeaderHidden(False)
        self.setAlternatingRowColors(True)
        self.setUniformRowHeights(True)
        self.setExpandsOnDoubleClick(False)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)
        self.doubleClicked.connect(self._on_double_clicked)
        header = self.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(COL_KEY, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_VALUE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.setColumnWidth(COL_KEY, 130)

    def _on_double_clicked(self, index: QModelIndex) -> None:
        model = self.model()
        node = model.node_at(index)
        if node is None:
            return
        if node.is_container:
            self.setExpanded(index, not self.isExpanded(index))
        else:
            self.edit_requested.emit(index)

    def _show_menu(self, position) -> None:
        index = self.indexAt(position)
        menu = QMenu(self)
        model = self.model()
        has = index.isValid()
        node = model.node_at(index) if has else None

        jump = menu.addAction(icon("target", size=15, kind="muted"), tr("Jump to editor"))
        jump.setEnabled(has and model.offset_for_path(model.path_at(index)) is not None)
        copy_node = menu.addAction(icon("copy", size=15, kind="muted"), tr("Copy node JSON"))
        copy_key = menu.addAction(icon("copy", size=15, kind="muted"), tr("Copy key"))
        copy_value = menu.addAction(icon("copy", size=15, kind="muted"), tr("Copy value"))
        copy_path = menu.addAction(icon("branch", size=15, kind="muted"), tr("Copy JSONPath"))
        for action in (copy_node, copy_key, copy_value, copy_path):
            action.setEnabled(has)
        copy_key.setEnabled(has and node is not None and isinstance(node.key, str))
        menu.addSeparator()
        edit = menu.addAction(icon("wand", size=15, kind="muted"), tr("Edit value…"))
        edit.setEnabled(has and node is not None and not node.is_container)
        add = menu.addAction(icon("plus", size=15, kind="muted"), tr("Add child…"))
        add.setEnabled(has and node is not None and node.is_container)
        delete = menu.addAction(icon("trash", size=15, kind="danger"), tr("Delete node"))
        delete.setEnabled(has and node is not None and node.parent is not None)
        menu.addSeparator()
        expand = menu.addAction(icon("expand-all", size=15, kind="muted"), tr("Expand all"))
        collapse = menu.addAction(icon("collapse-all", size=15, kind="muted"), tr("Collapse all"))

        chosen = menu.exec(self.viewport().mapToGlobal(position))
        if chosen is None:
            return
        if chosen is jump:
            info = model.offset_for_path(model.path_at(index))
            if info is not None:
                self.jump_requested.emit(info.value_offset, info.value_length)
        elif chosen is copy_node:
            QGuiApplication.clipboard().setText(model.subtree_text(index))
        elif chosen is copy_key:
            QGuiApplication.clipboard().setText(str(node.key))
        elif chosen is copy_value:
            if json_type(node.value) == "string":
                QGuiApplication.clipboard().setText(node.value)
            else:
                QGuiApplication.clipboard().setText(model.subtree_text(index))
        elif chosen is copy_path:
            QGuiApplication.clipboard().setText(path_to_string(model.path_at(index)))
        elif chosen is edit:
            self.edit_requested.emit(index)
        elif chosen is add:
            self.add_requested.emit(index)
        elif chosen is delete:
            self.delete_requested.emit(index)
        elif chosen is expand:
            self.expand_limited()
        elif chosen is collapse:
            self.collapseAll()

    def expand_limited(self) -> bool:
        """展开全部；节点太多时拒绝并返回 False，避免界面卡死。"""
        model = self.model()
        if not model.has_document():
            return False
        count = _count_nodes(model, limit=EXPAND_ALL_LIMIT)
        if count is None:
            dialogs.warn(self.parentWidget(), tr("Expand all"),
                         tr("This document has more than {n} nodes; expanding everything "
                            "would freeze the interface.").format(n=f"{EXPAND_ALL_LIMIT:,}"))
            return False
        self.expandAll()
        return True


def _count_nodes(model: JsonTreeModel, limit: int) -> Optional[int]:
    """迭代统计节点数；超过 limit 返回 None。"""
    total = 0
    stack = [QModelIndex()]
    while stack:
        parent = stack.pop()
        rows = model.rowCount(parent)
        total += rows
        if total > limit:
            return None
        for row in range(rows):
            stack.append(model.index(row, 0, parent))
    return total


class PropertiesPanel(QWidget):
    """选中节点的属性：路径、类型、子节点数、大小、预览。"""

    ROWS = ("path", "type", "children", "serialized_bytes", "length",
            "element_types", "keys", "offset")
    LABELS = {
        "path": "Path",
        "type": "Type",
        "children": "Children",
        "serialized_bytes": "Serialized size (bytes)",
        "length": "String length",
        "element_types": "Element types",
        "keys": "Keys (first 20)",
        "offset": "Editor offset",
    }

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._values: dict = {}
        layout = QFormLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setHorizontalSpacing(16)
        self._labels = {}
        for key in self.ROWS:
            name = QLabel(tr(self.LABELS[key]))
            value = QLabel("")
            value.setObjectName("MonoLabel")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addRow(name, value)
            self._labels[key] = (name, value)
        register_retranslate(self)

    def set_info(self, info: dict) -> None:
        self._values = info
        for key, (name, value_label) in self._labels.items():
            if key in info:
                raw = info[key]
                value_label.setText(f"{raw:,}" if isinstance(raw, int) and key in
                                    ("children", "serialized_bytes", "length", "offset")
                                    else str(raw))
                value_label.setVisible(True)
                name.setVisible(True)
            else:
                value_label.setVisible(False)
                name.setVisible(False)

    def clear(self) -> None:
        self.set_info({})

    def retranslate(self) -> None:
        for key, (name, _value) in self._labels.items():
            name.setText(tr(self.LABELS[key]))


class TreePanel(QWidget):
    """侧面板里的"结构"页：工具条 + 树 + 属性。"""

    jump_requested = Signal(int, int)
    value_changed = Signal()
    selection_changed = Signal(dict)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.model = JsonTreeModel(self)
        self.model.value_changed.connect(self.value_changed)
        self.tree = JsonTreeView(self.model, self)
        self.properties = PropertiesPanel(self)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(tr("Search keys / values"))
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._on_search)
        self.case_check = QCheckBox("Aa")
        self.case_check.setToolTip(tr("Match case"))
        self.case_check.toggled.connect(self._on_search)
        self.search_status = QLabel("")
        self.search_status.setObjectName("HintLabel")

        self.expand_button = self._tool("expand-all", "Expand all", self.tree.expand_limited)
        self.collapse_button = self._tool("collapse-all", "Collapse all", self.tree.collapseAll)
        self.add_button = self._tool("plus", "Add child…", lambda: self.add_child(self._current()))
        self.delete_button = self._tool("trash", "Delete node",
                                        lambda: self.delete_node(self._current()))

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(4)
        search_row.addWidget(self.search_edit, 1)
        search_row.addWidget(self.case_check)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(4)
        toolbar.addWidget(self.expand_button)
        toolbar.addWidget(self.collapse_button)
        toolbar.addWidget(self.add_button)
        toolbar.addWidget(self.delete_button)
        toolbar.addStretch(1)

        self.empty_label = QLabel(tr("No valid JSON to show.\nFix the document first."))
        self.empty_label.setObjectName("HintLabel")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        layout.addLayout(search_row)
        layout.addLayout(toolbar)
        layout.addWidget(self.search_status)
        layout.addWidget(self.tree, 3)
        layout.addWidget(self.empty_label)
        layout.addWidget(self.properties, 1)

        self.tree.jump_requested.connect(self.jump_requested)
        self.tree.edit_requested.connect(self.edit_node)
        self.tree.delete_requested.connect(self.delete_node)
        self.tree.add_requested.connect(self.add_child)
        self.tree.selectionModel().selectionChanged.connect(self._on_selection)

        register_retranslate(self)

    # ------------------------------------------------------------------ 工具条
    def _tool(self, icon_name: str, label: str, slot) -> QToolButton:
        button = QToolButton()
        button.setIcon(icon(icon_name, size=15, kind="muted"))
        button.setToolTip(tr(label))
        button.setAutoRaise(True)
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(slot)
        return button

    def _current(self) -> QModelIndex:
        return self.tree.currentIndex()

    # ------------------------------------------------------------------ 文档
    def set_document(self, value: Any, offsets: Optional[dict] = None) -> None:
        self.model.set_document(value, offsets)
        self.empty_label.setVisible(value is None)
        self.tree.setVisible(value is not None)
        self.properties.clear()
        self.apply_theme()

    def set_editable(self, editable: bool) -> None:
        self.model.read_only = not editable
        self.add_button.setEnabled(editable)
        self.delete_button.setEnabled(editable)
        self.tree.setToolTip("" if editable else tr(
            "Remove duplicate keys or JSONC comments before editing the tree to avoid data loss."))

    def clear_document(self) -> None:
        self.model.clear()
        self.empty_label.show()
        self.tree.hide()
        self.properties.clear()

    def apply_theme(self) -> None:
        palette = theme.palette()
        colors = {
            "object": QColor(palette.syn_key),
            "array": QColor(palette.syn_key),
            "string": QColor(palette.syn_string),
            "number": QColor(palette.syn_number),
            "boolean": QColor(palette.syn_bool),
            "null": QColor(palette.syn_null),
        }
        self.model.set_type_colors(colors)
        self.model.set_highlight_color(QColor(palette.accent_soft))

    # ------------------------------------------------------------------ 搜索
    def _on_search(self, *_args) -> None:
        query = self.search_edit.text()
        if not query:
            self.search_status.setText("")
            self.model.set_search_hits([])
            return
        paths = self.model.search(query, case_sensitive=self.case_check.isChecked())
        self.search_status.setText(tr("{n} match(es)").format(n=len(paths)))
        self.model.set_search_hits(paths)
        self._highlight_paths(paths)

    def _highlight_paths(self, paths) -> None:
        """展开命中节点的祖先链（有上限），并选中第一个命中。"""
        for path in paths[:100]:
            index = self.model.index_for_path(path)
            if index.isValid():
                self._expand_to(index)
        if paths:
            target = self.model.index_for_path(paths[0])
            if target.isValid():
                self.tree.setCurrentIndex(target)
                self.tree.scrollTo(target)

    def _expand_to(self, index: QModelIndex) -> None:
        chain = []
        current = index
        while current.isValid():
            chain.append(current)
            current = current.parent()
        for current in reversed(chain):
            self.tree.expand(current)

    def locate_path(self, path: tuple) -> bool:
        """供 JSONPath 结果"定位到树节点"使用。"""
        index = self.model.index_for_path(path)
        if not index.isValid():
            return False
        self._expand_to(index)
        self.tree.setCurrentIndex(index)
        self.tree.scrollTo(index)
        return True

    # ------------------------------------------------------------------ 编辑
    def edit_node(self, index: QModelIndex) -> None:
        if self.model.read_only:
            return
        node = self.model.node_at(index)
        if node is None or node.is_container:
            return
        dialog = NodeEditDialog(self, mode="edit", kind=json_type(node.value), value=node.value)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.model.set_node_value(index, dialog.result_value)

    def add_child(self, index: QModelIndex) -> None:
        if self.model.read_only:
            return
        node = self.model.node_at(index)
        if node is None or not node.is_container:
            return
        is_object = node.kind == TYPE_OBJECT
        dialog = NodeEditDialog(self, mode="add", kind="string", value="",
                                parent_is_object=is_object, allow_key=is_object)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        key = dialog.result_key if is_object else None
        if not self.model.add_child(index, key, dialog.result_value):
            dialogs.warn(self, tr("Add node"),
                         tr("Cannot add: the key already exists or the parent is not a container."))

    def delete_node(self, index: QModelIndex) -> None:
        if self.model.read_only:
            return
        node = self.model.node_at(index)
        if node is None or node.parent is None:
            return
        path = path_to_string(node.path())
        if not dialogs.confirm(self, tr("Delete node"),
                               tr("Delete {path} from the document?").format(path=path),
                               default=False):
            return
        self.model.remove_node(index)

    def _on_selection(self, _selected, _deselected) -> None:
        index = self._current()
        if not index.isValid():
            self.properties.clear()
            self.selection_changed.emit({})
            return
        info = self.model.statistics_for(index)
        self.properties.set_info(info)
        self.selection_changed.emit(info)

    # ------------------------------------------------------------------ 翻译
    def retranslate(self) -> None:
        self.search_edit.setPlaceholderText(tr("Search keys / values"))
        self.case_check.setToolTip(tr("Match case"))
        self.empty_label.setText(tr("No valid JSON to show.\nFix the document first."))
        self.expand_button.setToolTip(tr("Expand all"))
        self.collapse_button.setToolTip(tr("Collapse all"))
        self.add_button.setToolTip(tr("Add child…"))
        self.delete_button.setToolTip(tr("Delete node"))
        if self.search_edit.text():
            self._on_search()

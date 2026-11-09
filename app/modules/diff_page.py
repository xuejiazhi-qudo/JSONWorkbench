"""JSON 对比页。

左右两个编辑区 + 同步滚动 + 逐行差异高亮 + 结构化差异列表；
区分"键顺序变化"与"真实数据变化"；支持忽略字段/空白/大小写/数字格式、
数组配对模式、仅看变化、复制差异与导出报告（Markdown/HTML/JSON）。
"""

from __future__ import annotations

from app.utils.pagejob import run_latest
from app.widgets.tabwidget import ASYNC_VALIDATE_CHARS

from typing import Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
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

from app.core.jsonfilemanager import read_text_file, write_text_file
from app.core.jsonparser import parse_json
from app.engines import jsondiff
from app.i18n import register_retranslate, tr
from app.editor.jsoneditor import JsonEditor
from app.settings.settings import settings
from app.utils import dialogs
from app.widgets.icons import icon
from app.widgets.page import PageShell
from app.widgets.theme import theme

KIND_LABELS = {
    jsondiff.KIND_ADDED: "Added",
    jsondiff.KIND_REMOVED: "Removed",
    jsondiff.KIND_CHANGED: "Changed",
    jsondiff.KIND_TYPE_CHANGED: "Type changed",
    jsondiff.KIND_REORDERED: "Key order changed",
}
if hasattr(jsondiff, "KIND_MOVED"):
    KIND_LABELS[jsondiff.KIND_MOVED] = "Moved"


class DiffPage(PageShell):
    """侧边栏"JSON 对比"页。"""

    status_message = Signal(str, int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("JSON Diff",
                         "Compare two JSON documents structurally: added / removed / changed / "
                         "type-changed fields, with key-order changes reported separately.")
        self._left_original = ""
        self._right_original = ""
        self._result = None
        self._syncing = False

        self.left_editor = JsonEditor()
        self.right_editor = JsonEditor()

        self.load_left_button = QPushButton(tr("Left ← editor"))
        self.load_left_button.setIcon(icon("upload", size=15, kind="muted"))
        self.load_left_button.clicked.connect(lambda: self._load_from_workspace(self.left_editor))
        self.open_left_button = QPushButton(tr("Open left…"))
        self.open_left_button.setIcon(icon("folder-open", size=15, kind="muted"))
        self.open_left_button.clicked.connect(lambda: self._open_into(self.left_editor))
        self.load_right_button = QPushButton(tr("Right ← editor"))
        self.load_right_button.setIcon(icon("upload", size=15, kind="muted"))
        self.load_right_button.clicked.connect(lambda: self._load_from_workspace(self.right_editor))
        self.open_right_button = QPushButton(tr("Open right…"))
        self.open_right_button.setIcon(icon("folder-open", size=15, kind="muted"))
        self.open_right_button.clicked.connect(lambda: self._open_into(self.right_editor))
        self.swap_button = QPushButton(tr("Swap"))
        self.swap_button.setIcon(icon("convert", size=15, kind="muted"))
        self.swap_button.clicked.connect(self._swap)
        self.compare_button = QPushButton(tr("Compare"))
        self.compare_button.setProperty("class", "primary")
        self.compare_button.setIcon(icon("columns", size=15, kind="on_accent"))
        self.compare_button.setCursor(Qt.PointingHandCursor)
        self.compare_button.clicked.connect(self.run_diff)

        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(self.load_left_button)
        top.addWidget(self.open_left_button)
        top.addWidget(self.load_right_button)
        top.addWidget(self.open_right_button)
        top.addWidget(self.swap_button)
        top.addStretch(1)
        top.addWidget(self.compare_button)

        self.ignore_order_check = QCheckBox(tr("Ignore key order"))
        self.ignore_order_check.setChecked(True)
        self.report_reordered_check = QCheckBox(tr("Report key-order changes"))
        self.ignore_ws_check = QCheckBox(tr("Ignore whitespace"))
        self.ignore_ws_check.setChecked(True)
        self.ignore_case_check = QCheckBox(tr("Ignore case"))
        self.ignore_number_check = QCheckBox(tr("Ignore number format"))
        self.array_mode_combo = QComboBox()
        self.array_mode_combo.addItem(tr("By index"), "index")
        self.array_mode_combo.addItem(tr("As set"), "set")
        self.array_mode_combo.addItem(tr("By key field"), "key")
        self.key_field_edit = QLineEdit("id")
        self.key_field_edit.setFixedWidth(90)
        self.key_field_edit.setToolTip(tr("Field used to pair array elements"))
        self.ignore_fields_edit = QLineEdit()
        self.ignore_fields_edit.setPlaceholderText(tr("ignore fields, e.g. id, $.meta.*"))
        self.changed_only_check = QCheckBox(tr("Only changed"))

        options = QHBoxLayout()
        options.setSpacing(8)
        options.addWidget(self.ignore_order_check)
        options.addWidget(self.report_reordered_check)
        options.addWidget(self.ignore_ws_check)
        options.addWidget(self.ignore_case_check)
        options.addWidget(self.ignore_number_check)
        options.addWidget(QLabel(tr("Arrays:")))
        options.addWidget(self.array_mode_combo)
        options.addWidget(self.key_field_edit)
        options.addWidget(QLabel(tr("Ignore:")))
        options.addWidget(self.ignore_fields_edit, 1)
        options.addWidget(self.changed_only_check)

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("MutedLabel")
        self.summary_label.setWordWrap(True)

        editors = QSplitter(Qt.Horizontal)
        left_box = self._editor_box(tr("Left (A)"), self.left_editor)
        right_box = self._editor_box(tr("Right (B)"), self.right_editor)
        editors.addWidget(left_box)
        editors.addWidget(right_box)
        editors.setStretchFactor(0, 1)
        editors.setStretchFactor(1, 1)

        self.diff_tree = QTreeWidget()
        self.diff_tree.setHeaderLabels([tr("Kind"), tr("Path"), tr("Left"), tr("Right")])
        self.diff_tree.setRootIsDecorated(False)
        self.diff_tree.setAlternatingRowColors(True)
        header = self.diff_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self.diff_tree.setColumnWidth(2, 200)
        self.diff_tree.setColumnWidth(3, 200)

        self.copy_button = QPushButton(tr("Copy diff"))
        self.copy_button.setIcon(icon("copy", size=15, kind="muted"))
        self.copy_button.clicked.connect(self.copy_diff)
        self.export_button = QPushButton(tr("Export report…"))
        self.export_button.setIcon(icon("download", size=15, kind="muted"))
        self.export_button.clicked.connect(self.export_report)

        bottom_bar = QHBoxLayout()
        bottom_bar.addWidget(self.summary_label, 1)
        bottom_bar.addWidget(self.copy_button)
        bottom_bar.addWidget(self.export_button)

        splitter = QSplitter(Qt.Vertical)
        top_widget = QWidget()
        top_layout = QVBoxLayout(top_widget)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addWidget(editors, 1)
        splitter.addWidget(top_widget)
        splitter.addWidget(self.diff_tree)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        self.add_layout(top)
        self.add_layout(options)
        self.add(splitter, 1)
        self.add_layout(bottom_bar)

        self._connect_sync_scroll()
        self.changed_only_check.toggled.connect(self._reapply_view)
        register_retranslate(self)

    def _editor_box(self, title: str, editor: JsonEditor) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        label = QLabel(title)
        label.setObjectName("PanelTitle")
        layout.addWidget(label)
        layout.addWidget(editor, 1)
        editor.setProperty("diff_title_label", label)
        return box

    # ------------------------------------------------------------------ 同步滚动
    def _connect_sync_scroll(self) -> None:
        left_bar = self.left_editor.verticalScrollBar()
        right_bar = self.right_editor.verticalScrollBar()
        left_bar.valueChanged.connect(lambda value: self._sync(right_bar, value))
        right_bar.valueChanged.connect(lambda value: self._sync(left_bar, value))

    def _sync(self, target, value: int) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            if target.value() != value:
                target.setValue(value)
        finally:
            self._syncing = False

    # ------------------------------------------------------------------ 输入
    def _load_from_workspace(self, editor: JsonEditor) -> None:
        window = self.window()
        workspace = getattr(window, "workspace", None)
        tab = workspace.current_tab() if workspace is not None else None
        if tab is None:
            return
        editor.set_text(tab.text())
        self._remember(editor)

    def _open_into(self, editor: JsonEditor) -> None:
        path, _selected = QFileDialog.getOpenFileName(self, tr("Open JSON file"),
                                                      settings.last_dir, "*.json;;*.*")
        if not path:
            return
        result = read_text_file(path)
        if not result.ok:
            dialogs.warn(self, tr("JSON Diff"), result.error)
            return
        editor.set_text(result.text)
        self._remember(editor)

    def _remember(self, editor: JsonEditor) -> None:
        if editor is self.left_editor:
            self._left_original = editor.text()
        else:
            self._right_original = editor.text()

    def _swap(self) -> None:
        left, right = self.left_editor.text(), self.right_editor.text()
        self.left_editor.set_text(right)
        self.right_editor.set_text(left)
        self._left_original, self._right_original = right, left

    def set_documents(self, left: str, right: str) -> None:
        self.left_editor.set_text(left)
        self.right_editor.set_text(right)
        self._left_original, self._right_original = left, right

    # ------------------------------------------------------------------ 对比
    def _options(self) -> jsondiff.DiffOptions:
        fields = tuple(part.strip() for part in self.ignore_fields_edit.text().split(",")
                       if part.strip())
        return jsondiff.DiffOptions(
            ignore_key_order=self.ignore_order_check.isChecked(),
            report_reordered=self.report_reordered_check.isChecked(),
            ignore_fields=fields,
            ignore_whitespace=self.ignore_ws_check.isChecked(),
            ignore_string_case=self.ignore_case_check.isChecked(),
            ignore_number_format=self.ignore_number_check.isChecked(),
            array_mode=self.array_mode_combo.currentData(),
            array_key_field=self.key_field_edit.text().strip() or "id",
        )

    def run_diff(self) -> None:
        self._remember(self.left_editor)
        self._remember(self.right_editor)
        left_text = self.left_editor.text()
        right_text = self.right_editor.text()
        if not left_text.strip() or not right_text.strip():
            dialogs.warn(self, tr("JSON Diff"), tr("Both sides need content."))
            return
        options = self._options()
        run_latest(
            self, lambda: jsondiff.diff_text(left_text, right_text, options),
            self._show_diff,
            lambda: (self.left_editor.text() == left_text
                     and self.right_editor.text() == right_text and self._options() == options),
            background=len(left_text) + len(right_text) > ASYNC_VALIDATE_CHARS)

    def _show_diff(self, result) -> None:
        self._result = result
        self.diff_tree.clear()
        if result.error:
            self.summary_label.setText(tr("Cannot compare: {error}").format(error=result.error))
            return
        palette = theme.palette()
        for item_data in result.items[:5000]:
            label = tr(KIND_LABELS.get(item_data.kind, item_data.kind))
            item = QTreeWidgetItem([
                label, item_data.path_string,
                _short(item_data.left), _short(item_data.right),
            ])
            color = _kind_color(palette, item_data.kind)
            if color is not None:
                item.setForeground(0, color)
            self.diff_tree.addTopLevelItem(item)
        summary = result.summary or {}
        parts = [f"{tr(KIND_LABELS[kind])}: {count}"
                 for kind, count in summary.items()
                 if kind in KIND_LABELS and count]
        self.summary_label.setText(
            tr("{changed} · {parts}").format(
                changed=tr("Documents differ") if result.changed else tr("Documents are equal"),
                parts=" · ".join(parts) or tr("no differences")))
        self._reapply_view()
        self.status_message.emit(
            tr("Compared: {n} difference(s).").format(n=len(result.items)), 5000)

    def _reapply_view(self, *_args) -> None:
        """按"仅看变化"开关重画两侧编辑器与行标记。"""
        if self._result is None:
            return
        if self.changed_only_check.isChecked():
            left_value = parse_json(self._left_original).value
            right_value = parse_json(self._right_original).value
            left_shown = jsondiff.changed_only_text(left_value, self._result.items, side="left") \
                if left_value is not None else ""
            right_shown = jsondiff.changed_only_text(right_value, self._result.items, side="right") \
                if right_value is not None else ""
            self.left_editor.set_text(left_shown)
            self.right_editor.set_text(right_shown)
            self.left_editor.setExtraSelections([])
            self.right_editor.setExtraSelections([])
            return
        self.left_editor.set_text(self._left_original)
        self.right_editor.set_text(self._right_original)
        self._paint_from_result(self._left_original, self._right_original, self._result)

    def _paint_from_result(self, left_text: str, right_text: str, result) -> None:
        try:
            rows = jsondiff.aligned_lines(left_text, right_text, result)
        except Exception:  # noqa: BLE001 - 对齐视图是增强功能，失败不应影响对比结果
            rows = []
        left_marks: Dict[int, str] = {}
        right_marks: Dict[int, str] = {}
        for left_no, _left_line, right_no, _right_line, marker in rows:
            if marker == "equal":
                continue
            if left_no is not None:
                left_marks[left_no] = marker
            if right_no is not None:
                right_marks[right_no] = marker
        _paint_marks(self.left_editor, left_marks, "left")
        _paint_marks(self.right_editor, right_marks, "right")

    # ------------------------------------------------------------------ 输出
    def copy_diff(self) -> None:
        if self._result is None:
            self.status_message.emit(tr("Run a comparison first."), 3000)
            return
        QGuiApplication.clipboard().setText(jsondiff.diff_to_markdown(self._result))
        self.status_message.emit(tr("Diff copied as Markdown."), 3000)

    def export_report(self) -> None:
        if self._result is None:
            self.status_message.emit(tr("Run a comparison first."), 3000)
            return
        path, _selected = QFileDialog.getSaveFileName(
            self, tr("Export report"), "diff-report.md", "*.md;;*.html;;*.json;;All files (*)")
        if not path:
            return
        if path.endswith(".html"):
            text = jsondiff.diff_to_html(self._result)
        elif path.endswith(".json"):
            text = jsondiff.diff_to_json_text(self._result)
        else:
            text = jsondiff.diff_to_markdown(self._result)
        written = write_text_file(path, text)
        if written.ok:
            self.status_message.emit(tr("Saved {path}").format(path=path), 5000)
        else:
            dialogs.warn(self, tr("JSON Diff"), written.error)

    # ------------------------------------------------------------------ 主题/翻译
    def apply_theme(self) -> None:
        palette = theme.palette()
        family = settings.font_family
        self.left_editor.apply_theme(palette, family)
        self.right_editor.apply_theme(palette, family)
        if self._result is not None:
            self._reapply_view()

    def retranslate(self) -> None:
        super().retranslate()
        self.load_left_button.setText(tr("Left ← editor"))
        self.open_left_button.setText(tr("Open left…"))
        self.load_right_button.setText(tr("Right ← editor"))
        self.open_right_button.setText(tr("Open right…"))
        self.swap_button.setText(tr("Swap"))
        self.compare_button.setText(tr("Compare"))
        self.ignore_order_check.setText(tr("Ignore key order"))
        self.report_reordered_check.setText(tr("Report key-order changes"))
        self.ignore_ws_check.setText(tr("Ignore whitespace"))
        self.ignore_case_check.setText(tr("Ignore case"))
        self.ignore_number_check.setText(tr("Ignore number format"))
        self.array_mode_combo.setItemText(0, tr("By index"))
        self.array_mode_combo.setItemText(1, tr("As set"))
        self.array_mode_combo.setItemText(2, tr("By key field"))
        self.key_field_edit.setToolTip(tr("Field used to pair array elements"))
        self.ignore_fields_edit.setPlaceholderText(tr("ignore fields, e.g. id, $.meta.*"))
        self.changed_only_check.setText(tr("Only changed"))
        self.diff_tree.setHeaderLabels([tr("Kind"), tr("Path"), tr("Left"), tr("Right")])
        self.copy_button.setText(tr("Copy diff"))
        self.export_button.setText(tr("Export report…"))


def _short(value) -> str:
    from app.models.jsontreemodel import value_preview

    return value_preview(value, 60)


def _kind_color(palette, kind: str) -> Optional[QColor]:
    return {
        jsondiff.KIND_ADDED: palette.qcolor("SUCCESS"),
        jsondiff.KIND_REMOVED: palette.qcolor("DANGER"),
        jsondiff.KIND_CHANGED: palette.qcolor("WARNING"),
        jsondiff.KIND_TYPE_CHANGED: palette.qcolor("WARNING"),
        jsondiff.KIND_REORDERED: palette.qcolor("SYN_BOOL"),
    }.get(kind)


def _paint_marks(editor: JsonEditor, marks: Dict[int, str], side: str) -> None:
    """按行给编辑器加差异底色（左：删除/修改，右：新增/修改）。"""
    from PySide6.QtGui import QTextFormat
    from PySide6.QtWidgets import QTextEdit

    palette = theme.palette()
    colors = {
        "added": palette.diff_add if side == "right" else palette.diff_mod,
        "removed": palette.diff_del if side == "left" else palette.diff_mod,
        "changed": palette.diff_mod,
    }
    selections = []
    document = editor.document()
    for line_no, marker in list(marks.items())[:2000]:
        block = document.findBlockByNumber(line_no - 1)
        if not block.isValid():
            continue
        selection = QTextEdit.ExtraSelection()
        selection.format.setBackground(QColor(colors.get(marker, palette.diff_mod)))
        selection.format.setProperty(QTextFormat.FullWidthSelection, True)
        cursor = QTextCursor(block)
        selection.cursor = cursor
        selections.append(selection)
    editor.setExtraSelections(selections)

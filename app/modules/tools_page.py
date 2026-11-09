"""实用工具页：重复键、空值、Key 搜索、去重、拆分、合并、大小/结构分析。"""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.jsonformatter import dumps
from app.core.jsonparser import parse_json
from app.engines import converters as cv
from app.engines.scanners import (
    analyze_size,
    analyze_structure,
    find_duplicate_keys,
    find_empty_values,
    find_keys,
)
from app.engines.statistics import format_bytes
from app.i18n import register_retranslate, tr
from app.editor.jsoneditor import JsonEditor
from app.settings.settings import settings
from app.utils import dialogs
from app.widgets.icons import icon
from app.widgets.page import PageShell
from app.widgets.theme import theme

KIND_LABELS = {
    "duplicate_key": "Duplicate key",
    "null_value": "Null value",
    "empty_string": "Empty string",
    "whitespace_string": "Whitespace-only string",
    "empty_array": "Empty array",
    "empty_object": "Empty object",
    "key_match": "Key match",
    "value_match": "Value match",
    "large_node": "Large node",
    "deep_node": "Deep node",
    "recursive_shape": "Recursive shape",
    "truncated": "Truncated",
}


class FindingsTree(QTreeWidget):
    """扫描结果列表；双击跳转到编辑器。"""

    jump_requested = Signal(int, int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setHeaderLabels([tr("Kind"), tr("Path"), tr("Line"), tr("Detail"), tr("Preview")])
        self.setRootIsDecorated(False)
        self.setAlternatingRowColors(True)
        header = self.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        self.setColumnWidth(3, 260)
        self.setColumnWidth(4, 200)
        self.itemActivated.connect(self._on_activated)

    def set_findings(self, findings: List) -> None:
        self.clear()
        for finding in findings:
            item = QTreeWidgetItem([
                tr(KIND_LABELS.get(finding.kind, finding.kind)),
                finding.path_string,
                str(finding.line) if finding.line > 0 else "",
                finding.detail,
                finding.value_preview,
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, (finding.offset, 0))
            self.addTopLevelItem(item)

    def _on_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data and data[0] >= 0:
            self.jump_requested.emit(int(data[0]), int(data[1]))

    def retranslate(self) -> None:
        self.setHeaderLabels([tr("Kind"), tr("Path"), tr("Line"), tr("Detail"), tr("Preview")])


class ToolsPage(PageShell):
    """侧边栏"实用工具"页。"""

    status_message = Signal(str, int)
    jump_requested = Signal(int, int)
    send_to_editor = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Utilities",
                         "Duplicate-key detection, empty-value scan, key search, array dedupe / "
                         "split / merge and size & structure analysis.")
        self._document_provider: Optional[Callable[[], Tuple[str, str]]] = None

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_duplicates_tab(), tr("Duplicate keys"))
        self.tabs.addTab(self._build_empty_tab(), tr("Empty values"))
        self.tabs.addTab(self._build_keysearch_tab(), tr("Key search"))
        self.tabs.addTab(self._build_dedupe_tab(), tr("Dedupe array"))
        self.tabs.addTab(self._build_split_tab(), tr("Split array"))
        self.tabs.addTab(self._build_merge_tab(), tr("Merge documents"))
        self.tabs.addTab(self._build_analysis_tab(), tr("Size & structure"))

        self.output_editor = JsonEditor(read_only=True)
        self.copy_button = QPushButton(tr("Copy output"))
        self.copy_button.setIcon(icon("copy", size=15, kind="muted"))
        self.copy_button.clicked.connect(self.copy_output)
        self.send_button = QPushButton(tr("Send to editor"))
        self.send_button.setIcon(icon("arrow-right", size=15, kind="muted"))
        self.send_button.clicked.connect(lambda: self.send_to_editor.emit(self.output_editor.text()))
        self.save_pieces_button = QPushButton(tr("Save pieces to folder…"))
        self.save_pieces_button.setIcon(icon("folder", size=15, kind="muted"))
        self.save_pieces_button.clicked.connect(self.save_pieces)
        self.save_pieces_button.hide()

        output_bar = QHBoxLayout()
        output_bar.addWidget(QLabel(tr("Output")), 1)
        output_bar.addWidget(self.copy_button)
        output_bar.addWidget(self.send_button)
        output_bar.addWidget(self.save_pieces_button)

        splitter = QSplitter(Qt.Vertical)
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addWidget(self.tabs)
        splitter.addWidget(top)
        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.addLayout(output_bar)
        bottom_layout.addWidget(self.output_editor)
        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        self.add(splitter, 1)
        register_retranslate(self)

    def set_document_provider(self, provider: Callable[[], Tuple[str, str]]) -> None:
        self._document_provider = provider

    def _document(self) -> Tuple[str, str]:
        if self._document_provider is None:
            return "", "json"
        return self._document_provider()

    def _parsed(self):
        text, mode = self._document()
        if not text.strip():
            dialogs.warn(self, tr("Utilities"), tr("No document is open."))
            return None, None
        parsed = parse_json(text, mode)
        if not parsed.ok:
            dialogs.warn(self, tr("Utilities"), tr("The document is not valid JSON."))
            return None, None
        return parsed, text

    def _run_button(self, label: str, slot) -> QPushButton:
        button = QPushButton(tr(label))
        button.setProperty("class", "primary")
        button.setIcon(icon("play", size=14, kind="on_accent"))
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(slot)
        return button

    # ------------------------------------------------------------------ 各标签页
    def _build_duplicates_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 0)
        self.dup_button = self._run_button("Scan", self.run_duplicates)
        self.dup_tree = FindingsTree()
        self.dup_tree.jump_requested.connect(self.jump_requested)
        bar = QHBoxLayout()
        bar.addWidget(self.dup_button)
        bar.addStretch(1)
        layout.addLayout(bar)
        layout.addWidget(self.dup_tree, 1)
        return page

    def run_duplicates(self) -> None:
        text, mode = self._document()
        findings = find_duplicate_keys(text, mode=mode)
        self.dup_tree.set_findings(findings)
        self.status_message.emit(tr("{n} duplicate key(s) found.").format(n=len(findings)), 5000)

    def _build_empty_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 0)
        self.empty_null = QCheckBox(tr("null values"))
        self.empty_null.setChecked(True)
        self.empty_string = QCheckBox(tr("empty strings"))
        self.empty_string.setChecked(True)
        self.empty_array = QCheckBox(tr("empty arrays"))
        self.empty_array.setChecked(True)
        self.empty_object = QCheckBox(tr("empty objects"))
        self.empty_object.setChecked(True)
        self.empty_ws = QCheckBox(tr("whitespace-only strings"))
        self.empty_button = self._run_button("Scan", self.run_empty)
        self.empty_tree = FindingsTree()
        self.empty_tree.jump_requested.connect(self.jump_requested)
        bar = QHBoxLayout()
        bar.addWidget(self.empty_button)
        for box in (self.empty_null, self.empty_string, self.empty_array,
                    self.empty_object, self.empty_ws):
            bar.addWidget(box)
        bar.addStretch(1)
        layout.addLayout(bar)
        layout.addWidget(self.empty_tree, 1)
        return page

    def run_empty(self) -> None:
        parsed, _text = self._parsed()
        if parsed is None:
            return
        findings = find_empty_values(
            parsed.value,
            include_null=self.empty_null.isChecked(),
            include_empty_string=self.empty_string.isChecked(),
            include_empty_array=self.empty_array.isChecked(),
            include_empty_object=self.empty_object.isChecked(),
            include_whitespace_only_string=self.empty_ws.isChecked())
        self.empty_tree.set_findings(findings)
        self.status_message.emit(tr("{n} empty value(s) found.").format(n=len(findings)), 5000)

    def _build_keysearch_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 0)
        self.key_pattern = QLineEdit()
        self.key_pattern.setPlaceholderText(tr("key name or pattern"))
        self.key_case = QCheckBox(tr("Match case"))
        self.key_regex = QCheckBox(tr("Regex"))
        self.key_values = QCheckBox(tr("Also match values"))
        self.key_button = self._run_button("Search", self.run_keysearch)
        self.key_tree = FindingsTree()
        self.key_tree.jump_requested.connect(self.jump_requested)
        bar = QHBoxLayout()
        bar.addWidget(self.key_pattern, 1)
        bar.addWidget(self.key_case)
        bar.addWidget(self.key_regex)
        bar.addWidget(self.key_values)
        bar.addWidget(self.key_button)
        layout.addLayout(bar)
        layout.addWidget(self.key_tree, 1)
        return page

    def run_keysearch(self) -> None:
        parsed, _text = self._parsed()
        if parsed is None:
            return
        pattern = self.key_pattern.text()
        if not pattern:
            return
        findings = find_keys(parsed.value, pattern,
                             case_sensitive=self.key_case.isChecked(),
                             regex=self.key_regex.isChecked(),
                             match_values=self.key_values.isChecked())
        self.key_tree.set_findings(findings)
        self.status_message.emit(tr("{n} match(es).").format(n=len(findings)), 5000)

    def _build_dedupe_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(0, 6, 0, 0)
        self.dedupe_key = QLineEdit()
        self.dedupe_key.setPlaceholderText(tr("dotted path, empty = whole element"))
        form.addRow(tr("Dedupe by field"), self.dedupe_key)
        self.dedupe_case = QCheckBox(tr("Case sensitive"))
        self.dedupe_case.setChecked(True)
        form.addRow("", self.dedupe_case)
        self.dedupe_keep = QComboBox()
        self.dedupe_keep.addItem(tr("first occurrence"), "first")
        self.dedupe_keep.addItem(tr("last occurrence"), "last")
        form.addRow(tr("Keep"), self.dedupe_keep)
        self.dedupe_button = self._run_button("Dedupe", self.run_dedupe)
        form.addRow("", self.dedupe_button)
        return page

    def run_dedupe(self) -> None:
        parsed, _text = self._parsed()
        if parsed is None:
            return
        result = cv.dedupe_array(parsed.value, key=self.dedupe_key.text().strip(),
                                 case_sensitive=self.dedupe_case.isChecked(),
                                 keep=self.dedupe_keep.currentData())
        self._show_convert(result, tr("Removed {n} duplicate(s)."))

    def _build_split_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(0, 6, 0, 0)
        self.split_chunk = QSpinBox()
        self.split_chunk.setRange(0, 1_000_000)
        self.split_chunk.setValue(0)
        form.addRow(tr("Chunk size (0 = by field)"), self.split_chunk)
        self.split_field = QLineEdit()
        self.split_field.setPlaceholderText(tr("dotted path of the field to group by"))
        form.addRow(tr("Split by field"), self.split_field)
        self.split_button = self._run_button("Split", self.run_split)
        form.addRow("", self.split_button)
        return page

    def run_split(self) -> None:
        parsed, _text = self._parsed()
        if parsed is None:
            return
        result = cv.split_array(parsed.value, chunk_size=self.split_chunk.value(),
                                by_field=self.split_field.text().strip())
        self._show_convert(result, tr("Split into {n} piece(s)."))
        self.save_pieces_button.setVisible(bool(result.ok and result.meta.get("pieces")))

    def save_pieces(self) -> None:
        parsed, _text = self._parsed()
        if parsed is None:
            return
        directory = QFileDialog.getExistingDirectory(self, tr("Save pieces to folder"))
        if not directory:
            return
        result = cv.split_to_files(parsed.value, directory,
                                   chunk_size=self.split_chunk.value(),
                                   by_field=self.split_field.text().strip())
        if result.ok:
            self.status_message.emit(tr("Wrote {n} file(s) to {dir}").format(
                n=len(result.meta.get("files", [])), dir=directory), 6000)
        else:
            dialogs.warn(self, tr("Utilities"), self._localize(result.error))

    def _build_merge_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(0, 6, 0, 0)
        hint = QLabel(tr("Input must be a JSON array of documents, e.g. [{...}, {...}]."))
        hint.setObjectName("HintLabel")
        hint.setWordWrap(True)
        form.addRow(hint)
        self._merge_hint = hint
        self.merge_strategy = QComboBox()
        for value, label in (("deep", "Deep merge"), ("shallow", "Shallow merge"),
                             ("concat", "Concatenate arrays")):
            self.merge_strategy.addItem(tr(label), value)
        form.addRow(tr("Strategy"), self.merge_strategy)
        self.merge_array = QComboBox()
        for value, label in (("append", "Append"), ("replace", "Replace"), ("unique", "Unique")):
            self.merge_array.addItem(tr(label), value)
        form.addRow(tr("Arrays"), self.merge_array)
        self.merge_conflict = QComboBox()
        for value, label in (("last", "Last wins"), ("first", "First wins"), ("error", "Fail")):
            self.merge_conflict.addItem(tr(label), value)
        form.addRow(tr("On conflict"), self.merge_conflict)
        self.merge_button = self._run_button("Merge", self.run_merge)
        form.addRow("", self.merge_button)
        return page

    def run_merge(self) -> None:
        parsed, _text = self._parsed()
        if parsed is None:
            return
        result = cv.merge_documents(parsed.value, strategy=self.merge_strategy.currentData(),
                                    array_strategy=self.merge_array.currentData(),
                                    conflict=self.merge_conflict.currentData())
        self._show_convert(result, tr("Merged {n} document(s)."))

    def _build_analysis_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 0)
        self.analysis_button = self._run_button("Analyze", self.run_analysis)
        self.analysis_tree = QTreeWidget()
        self.analysis_tree.setHeaderLabels([tr("Item"), tr("Value")])
        self.analysis_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.analysis_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        bar = QHBoxLayout()
        bar.addWidget(self.analysis_button)
        bar.addStretch(1)
        layout.addLayout(bar)
        layout.addWidget(self.analysis_tree, 1)
        return page

    def run_analysis(self) -> None:
        parsed, _text = self._parsed()
        if parsed is None:
            return
        self.analysis_tree.clear()
        sizes = analyze_size(parsed.value, top_n=25)
        size_root = QTreeWidgetItem([tr("Largest nodes"), ""])
        self.analysis_tree.addTopLevelItem(size_root)
        for path_string, size, kind in sizes:
            size_root.addChild(QTreeWidgetItem([path_string, f"{format_bytes(size)} · {kind}"]))
        size_root.setExpanded(len(sizes) <= 15)

        report = analyze_structure(parsed.value)
        shape_root = QTreeWidgetItem([tr("Object shapes"), ""])
        self.analysis_tree.addTopLevelItem(shape_root)
        for signature, count, example in report.shape_signatures[:25]:
            shape_root.addChild(QTreeWidgetItem([signature, f"{count} × {example}"]))
        recursive_root = QTreeWidgetItem([tr("Recursive shapes"), ""])
        self.analysis_tree.addTopLevelItem(recursive_root)
        for signature, depths, example in report.recursive_shapes[:25]:
            recursive_root.addChild(QTreeWidgetItem(
                [signature, tr("depths {d} · {p}").format(
                    d=",".join(str(d) for d in depths), p=example)]))
        conflict_root = QTreeWidgetItem([tr("Key type conflicts"), ""])
        self.analysis_tree.addTopLevelItem(conflict_root)
        for key, types, example in report.key_type_conflicts[:25]:
            conflict_root.addChild(QTreeWidgetItem([key, f"{', '.join(types)} · {example}"]))
        self.status_message.emit(tr("Analysis complete."), 4000)

    # ------------------------------------------------------------------ 输出
    def _show_convert(self, result, message_template: str) -> None:
        if not result.ok:
            dialogs.warn(self, tr("Utilities"), self._localize(result.error))
            return
        output = result.output
        if not output and result.meta.get("value") is not None:
            output = dumps(result.meta["value"])
        if not output and result.meta.get("pieces"):
            output = result.output or "\n".join(result.meta["pieces"])
        self.output_editor.set_text(output)
        note = ""
        if "removed" in result.meta:
            note = message_template.format(n=result.meta["removed"])
        elif "pieces" in result.meta:
            note = message_template.format(n=len(result.meta["pieces"]))
        elif "value" in result.meta and isinstance(result.meta["value"], list):
            note = message_template.format(n=len(result.meta["value"]))
        self.status_message.emit(note or tr("Done."), 5000)

    @staticmethod
    def _localize(error: str) -> str:
        from app.i18n import warning_text

        code, _, detail = error.partition(":")
        return warning_text(code.strip(), detail.strip())

    def copy_output(self) -> None:
        text = self.output_editor.text()
        if not text:
            self.status_message.emit(tr("Nothing to copy."), 3000)
            return
        QGuiApplication.clipboard().setText(text)

    # ------------------------------------------------------------------ 主题/翻译
    def apply_theme(self) -> None:
        palette = theme.palette()
        self.output_editor.apply_theme(palette, settings.font_family)

    def retranslate(self) -> None:
        super().retranslate()
        titles = [tr("Duplicate keys"), tr("Empty values"), tr("Key search"),
                  tr("Dedupe array"), tr("Split array"), tr("Merge documents"),
                  tr("Size & structure")]
        for index, title in enumerate(titles):
            self.tabs.setTabText(index, title)
        self.dup_button.setText(tr("Scan"))
        self.empty_button.setText(tr("Scan"))
        self.key_button.setText(tr("Search"))
        self.dedupe_button.setText(tr("Dedupe"))
        self.split_button.setText(tr("Split"))
        self.merge_button.setText(tr("Merge"))
        self.analysis_button.setText(tr("Analyze"))
        self.copy_button.setText(tr("Copy output"))
        self.send_button.setText(tr("Send to editor"))
        self.save_pieces_button.setText(tr("Save pieces to folder…"))
        self._merge_hint.setText(tr("Input must be a JSON array of documents, e.g. [{...}, {...}]."))
        self.dup_tree.retranslate()
        self.empty_tree.retranslate()
        self.key_tree.retranslate()

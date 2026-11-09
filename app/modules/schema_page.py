"""JSON Schema 校验页（完全离线）。

Schema 来源：本地文件 / 手贴 / 从当前文档推断；
被校验实例：当前编辑工作区的文档。
结果给出错误路径、关键字与说明，可跳转、可导出报告。
"""

from __future__ import annotations

from app.utils.pagejob import run_latest, is_large_value
from app.widgets.tabwidget import ASYNC_VALIDATE_CHARS

import json
import os
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
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
from app.engines import jsonschemav
from app.history.store import KIND_SCHEMA, HistoryStore
from app.i18n import register_retranslate, tr
from app.editor.jsoneditor import JsonEditor
from app.settings.settings import settings
from app.utils import dialogs
from app.widgets.icons import icon
from app.widgets.page import PageShell
from app.widgets.theme import theme


class SchemaPage(PageShell):
    """侧边栏"JSON Schema"页。"""

    status_message = Signal(str, int)
    jump_in_editor = Signal(int, int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("JSON Schema",
                         "Validate the current document against a local JSON Schema "
                         "(draft-07 / 2020-12). No online schema services, ever.")
        self._instance_text = ""
        self._instance_value = None
        self._offsets = None
        self._instance_name = ""
        self._schema_path = ""

        self.open_schema_button = QPushButton(tr("Open schema…"))
        self.open_schema_button.setIcon(icon("folder-open", size=15, kind="muted"))
        self.open_schema_button.clicked.connect(self.open_schema)

        self.infer_button = QPushButton(tr("Infer from document"))
        self.infer_button.setIcon(icon("wand", size=15, kind="muted"))
        self.infer_button.clicked.connect(self.infer_schema)

        self.recent_combo = QComboBox()
        self.recent_combo.addItem(tr("Recent schemas…"), "")
        self.recent_combo.setMaximumWidth(240)
        self.recent_combo.currentIndexChanged.connect(self._on_recent_schema)
        self._refresh_recent_schemas()

        self.load_instance_button = QPushButton(tr("Load instance from editor"))
        self.load_instance_button.setIcon(icon("upload", size=15, kind="muted"))
        self.load_instance_button.clicked.connect(self.load_instance)

        self.validate_button = QPushButton(tr("Validate"))
        self.validate_button.setProperty("class", "primary")
        self.validate_button.setIcon(icon("shield-check", size=15, kind="on_accent"))
        self.validate_button.setCursor(Qt.PointingHandCursor)
        self.validate_button.clicked.connect(self.run_validation)

        self.export_button = QPushButton(tr("Export report…"))
        self.export_button.setIcon(icon("download", size=15, kind="muted"))
        self.export_button.clicked.connect(self.export_report)

        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(self.open_schema_button)
        top.addWidget(self.infer_button)
        top.addWidget(self.recent_combo)
        top.addSpacing(12)
        top.addWidget(self.load_instance_button)
        top.addWidget(self.validate_button)
        top.addStretch(1)
        top.addWidget(self.export_button)

        self.source_label = QLabel("")
        self.source_label.setObjectName("HintLabel")
        self.source_label.setWordWrap(True)

        self.schema_editor = JsonEditor()
        self.schema_editor.setPlaceholderText(
            tr('Paste a JSON Schema here, e.g. {"type":"object","required":["id"]}'))

        self.result_tree = QTreeWidget()
        self.result_tree.setHeaderLabels([tr("Severity"), tr("Path"), tr("Keyword"), tr("Message")])
        self.result_tree.setRootIsDecorated(False)
        self.result_tree.setAlternatingRowColors(True)
        header = self.result_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.result_tree.setColumnWidth(1, 260)
        self.result_tree.itemActivated.connect(self._on_activated)

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("MutedLabel")
        self.summary_label.setWordWrap(True)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self.summary_label)
        right_layout.addWidget(self.result_tree, 1)

        splitter = QSplitter(Qt.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel(tr("Schema")), 0)
        left_layout.addWidget(self.schema_editor, 1)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)

        self.add_layout(top)
        self.add(self.source_label)
        self.add(splitter, 1)
        register_retranslate(self)

    # ------------------------------------------------------------------ 输入
    def open_schema(self) -> None:
        path, _selected = QFileDialog.getOpenFileName(
            self, tr("Open schema"), settings.last_dir, "*.json;;All files (*)")
        if not path:
            return
        schema, error = jsonschemav.load_schema_file(path)
        if error:
            dialogs.warn(self, tr("JSON Schema"), error)
            return
        self.schema_editor.set_text(dumps(schema))
        self._schema_path = path
        self._refresh_source_label()
        self.status_message.emit(tr("Schema loaded from {path}").format(path=path), 4000)

    def infer_schema(self) -> None:
        if not self._ensure_instance():
            return
        schema = jsonschemav.infer_schema(self._instance_value)
        self.schema_editor.set_text(jsonschemav.schema_to_text(schema))
        self._schema_path = ""
        self._refresh_source_label()
        self.status_message.emit(tr("Schema inferred from the current document."), 4000)

    def load_instance(self) -> bool:
        window = self.window()
        workspace = getattr(window, "workspace", None)
        tab = workspace.current_tab() if workspace is not None else None
        if tab is None:
            dialogs.warn(self, tr("JSON Schema"), tr("No document is open."))
            return False
        parsed = parse_json(tab.text(), tab.document.mode)
        if not parsed.ok:
            dialogs.warn(self, tr("JSON Schema"), tr("The document is not valid JSON."))
            return False
        self._instance_text = tab.text()
        self._instance_value = parsed.value
        self._offsets = parsed.offsets
        self._instance_name = tab.document.display_name
        self._refresh_source_label()
        return True

    def _ensure_instance(self) -> bool:
        if self._instance_value is None:
            return self.load_instance()
        return True

    def _refresh_source_label(self) -> None:
        schema_source = self._schema_path or tr("(pasted / inferred)")
        self.source_label.setText(tr("Schema: {schema}   ·   Instance: {instance}").format(
            schema=schema_source, instance=self._instance_name or tr("(none)")))

    # ------------------------------------------------------------------ 校验
    def run_validation(self) -> None:
        if not self._ensure_instance():
            return
        schema_text = self.schema_editor.text()
        if not schema_text.strip():
            dialogs.warn(self, tr("JSON Schema"), tr("Load or paste a schema first."))
            return
        parsed_schema = parse_json(schema_text)
        if not parsed_schema.ok:
            dialogs.warn(self, tr("JSON Schema"), tr("The schema is not valid JSON."))
            return
        resolver = jsonschemav.LocalRefResolver(
            base_schema=parsed_schema.value,
            base_dir=_dir_of(self._schema_path))
        instance = self._instance_value
        schema_path = self._schema_path
        run_latest(
            self, lambda: jsonschemav.validate(instance, parsed_schema.value, resolver=resolver),
            self._show_validation,
            lambda: (self._instance_value is instance and self._schema_path == schema_path
                     and self.schema_editor.text() == schema_text),
            background=len(schema_text) > ASYNC_VALIDATE_CHARS or is_large_value(instance))

    def _show_validation(self, result) -> None:
        self.result_tree.clear()
        palette = theme.palette()
        for error in result.errors:
            item = QTreeWidgetItem([tr("Error"), error.path_string, error.keyword, error.message])
            item.setData(1, Qt.ItemDataRole.UserRole, error.path)
            item.setForeground(0, palette.qcolor("DANGER"))
            self.result_tree.addTopLevelItem(item)
        for warning in result.warnings:
            item = QTreeWidgetItem([tr("Warning"), warning.path_string, warning.keyword,
                                    warning.message])
            item.setForeground(0, palette.qcolor("WARNING"))
            self.result_tree.addTopLevelItem(item)
        truncated = tr(" (truncated)") if result.truncated else ""
        self.summary_label.setText(
            tr("{draft} · {errors} error(s), {warnings} warning(s) · {ms:.1f} ms{truncated}").format(
                draft=result.schema_draft or tr("unknown draft"),
                errors=len(result.errors), warnings=len(result.warnings),
                ms=result.elapsed_ms, truncated=truncated))
        self._last_result = result
        self._record_history(result)
        if result.valid:
            self.status_message.emit(tr("Schema validation passed."), 5000)
        else:
            self.status_message.emit(
                tr("Schema validation failed with {n} error(s).").format(n=len(result.errors)), 6000)

    _last_result = None

    def _record_history(self, result) -> None:
        label = self._schema_path or tr("(pasted / inferred)")
        store = HistoryStore()
        try:
            store.record(KIND_SCHEMA, label,
                         detail=f"{len(result.errors)} errors",
                         path=self._instance_name,
                         content=self.schema_editor.text())
        finally:
            store.close()
        self._refresh_recent_schemas()

    def _refresh_recent_schemas(self) -> None:
        self.recent_combo.blockSignals(True)
        self.recent_combo.clear()
        self.recent_combo.addItem(tr("Recent schemas…"), "")
        self._recent_entries: Dict[str, Any] = {}
        store = HistoryStore()
        try:
            for entry in store.list(kind=KIND_SCHEMA, limit=60):
                if entry.label in self._recent_entries:
                    continue
                if entry.has_content or entry.path:
                    self._recent_entries[entry.label] = entry
                    self.recent_combo.addItem(entry.label, entry.label)
                if len(self._recent_entries) >= 15:
                    break
        finally:
            store.close()
        self.recent_combo.blockSignals(False)

    def _on_recent_schema(self, index: int) -> None:
        label = self.recent_combo.currentData()
        entry = self._recent_entries.get(label) if label else None
        if entry is None:
            return
        if entry.has_content:
            self.schema_editor.set_text(entry.content)
            self._schema_path = entry.path if entry.path.endswith(".json") else ""
        elif entry.path and os.path.isfile(entry.path):
            self._schema_path = entry.path
            schema, error = jsonschemav.load_schema_file(entry.path)
            if error:
                dialogs.warn(self, tr("JSON Schema"), error)
                return
            self.schema_editor.set_text(jsonschemav.schema_to_text(schema))
        self._refresh_source_label()

    def _on_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        path = item.data(1, Qt.ItemDataRole.UserRole)
        if path is None or self._offsets is None:
            return
        info = self._offsets.get(tuple(path))
        if info is not None:
            self.jump_in_editor.emit(info.value_offset, info.value_length)

    # ------------------------------------------------------------------ 报告
    def export_report(self) -> None:
        result = getattr(self, "_last_result", None)
        if result is None:
            self.status_message.emit(tr("Run a validation first."), 3000)
            return
        path, _selected = QFileDialog.getSaveFileName(
            self, tr("Export report"), "schema-report.json", "*.json;;*.md;;All files (*)")
        if not path:
            return
        if path.endswith(".md"):
            text = self._markdown_report(result)
        else:
            text = dumps({
                "valid": result.valid,
                "draft": result.schema_draft,
                "errors": [_error_dict(e) for e in result.errors],
                "warnings": [_error_dict(e) for e in result.warnings],
                "truncated": result.truncated,
                "elapsed_ms": result.elapsed_ms,
            })
        written = write_text_file(path, text)
        if written.ok:
            self.status_message.emit(tr("Saved {path}").format(path=path), 5000)
        else:
            dialogs.warn(self, tr("JSON Schema"), written.error)

    def _markdown_report(self, result) -> str:
        lines = [f"# JSON Schema report",
                 f"",
                 f"- valid: {result.valid}",
                 f"- draft: {result.schema_draft}",
                 f"- errors: {len(result.errors)}",
                 f"- warnings: {len(result.warnings)}",
                 f"- elapsed: {result.elapsed_ms:.1f} ms",
                 ""]
        if result.errors:
            lines.append("## Errors")
            lines.append("")
            for error in result.errors:
                lines.append(f"- `{error.path_string}` **{error.keyword}**: {error.message}")
        if result.warnings:
            lines.append("")
            lines.append("## Warnings")
            lines.append("")
            for warning in result.warnings:
                lines.append(f"- `{warning.path_string}` **{warning.keyword}**: {warning.message}")
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------ 主题/翻译
    def apply_theme(self) -> None:
        self.schema_editor.apply_theme(theme.palette(), settings.font_family)

    def retranslate(self) -> None:
        super().retranslate()
        self.open_schema_button.setText(tr("Open schema…"))
        self.infer_button.setText(tr("Infer from document"))
        self.recent_combo.setItemText(0, tr("Recent schemas…"))
        self.load_instance_button.setText(tr("Load instance from editor"))
        self.validate_button.setText(tr("Validate"))
        self.export_button.setText(tr("Export report…"))
        self.result_tree.setHeaderLabels([tr("Severity"), tr("Path"), tr("Keyword"), tr("Message")])
        self.schema_editor.setPlaceholderText(
            tr('Paste a JSON Schema here, e.g. {"type":"object","required":["id"]}'))
        self._refresh_source_label()


def _dir_of(path: str) -> Optional[str]:
    import os

    return os.path.dirname(os.path.abspath(path)) if path else None


def _error_dict(error) -> dict:
    return {
        "path": error.path_string,
        "path_tuple": list(error.path),
        "keyword": error.keyword,
        "message": error.message,
        "expected": _jsonable(error.expected),
        "actual": _jsonable(error.actual),
    }


def _jsonable(value):
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)

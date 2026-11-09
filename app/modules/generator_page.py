"""数据生成页：字段表生成 / 按 Schema 生成 / 随机测试数据。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.jsonfilemanager import write_text_file
from app.core.jsonformatter import dumps
from app.core.jsonparser import parse_json
from app.engines.generator import (
    FIELD_TYPES,
    MockOptions,
    field_spec_from_row,
    generate_from_fields,
    generate_from_schema,
    mock_value,
)
from app.i18n import register_retranslate, tr
from app.editor.jsoneditor import JsonEditor
from app.settings.settings import settings
from app.utils import dialogs
from app.widgets.icons import icon
from app.widgets.page import PageShell
from app.widgets.theme import theme

MODE_FIELDS = 0
MODE_SCHEMA = 1
MODE_MOCK = 2

TABLE_COLUMNS = ("Name", "Type", "Required", "Min", "Max", "Length", "Enum", "Array size")


class GeneratorPage(PageShell):
    """侧边栏"数据生成"页。"""

    status_message = Signal(str, int)
    send_to_editor = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Data Generator",
                         "Generate sample JSON from a field table, from a JSON Schema, "
                         "or as randomized test data (names, emails, phones, dates…).")
        self.mode_combo = QComboBox()
        self.mode_combo.addItem(tr("Field table"), MODE_FIELDS)
        self.mode_combo.addItem(tr("From JSON Schema"), MODE_SCHEMA)
        self.mode_combo.addItem(tr("Random test data"), MODE_MOCK)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)

        self.locale_combo = QComboBox()
        self.locale_combo.addItem(tr("Chinese (zh)"), "zh")
        self.locale_combo.addItem(tr("English (en)"), "en")

        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 10000)
        self.count_spin.setValue(1)

        self.seed_edit = QLineEdit()
        self.seed_edit.setPlaceholderText(tr("empty = random"))

        self.run_button = QPushButton(tr("Generate"))
        self.run_button.setProperty("class", "primary")
        self.run_button.setIcon(icon("sparkles", size=15, kind="on_accent"))
        self.run_button.clicked.connect(self.run_generate)

        self.copy_button = QPushButton(tr("Copy output"))
        self.copy_button.setIcon(icon("copy", size=15, kind="muted"))
        self.copy_button.clicked.connect(self.copy_output)
        self.save_button = QPushButton(tr("Save output…"))
        self.save_button.setIcon(icon("download", size=15, kind="muted"))
        self.save_button.clicked.connect(self.save_output)
        self.send_button = QPushButton(tr("Send to editor"))
        self.send_button.setIcon(icon("arrow-right", size=15, kind="muted"))
        self.send_button.clicked.connect(lambda: self.send_to_editor.emit(self.output_editor.text()))

        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(QLabel(tr("Mode:")))
        top.addWidget(self.mode_combo)
        top.addWidget(QLabel(tr("Locale:")))
        top.addWidget(self.locale_combo)
        top.addWidget(QLabel(tr("Count:")))
        top.addWidget(self.count_spin)
        top.addWidget(QLabel(tr("Seed:")))
        top.addWidget(self.seed_edit)
        top.addWidget(self.run_button)
        top.addWidget(self.copy_button)
        top.addWidget(self.save_button)
        top.addWidget(self.send_button)
        top.addStretch(1)

        self.field_table = QTableWidget(6, len(TABLE_COLUMNS))
        self.field_table.setHorizontalHeaderLabels([tr(name) for name in TABLE_COLUMNS])
        self.field_table.horizontalHeader().setStretchLastSection(True)
        self._seed_default_rows()

        self.add_row_button = QPushButton(tr("Add row"))
        self.add_row_button.setIcon(icon("plus", size=15, kind="muted"))
        self.add_row_button.clicked.connect(self._add_row)
        self.remove_row_button = QPushButton(tr("Remove row"))
        self.remove_row_button.setIcon(icon("minus", size=15, kind="muted"))
        self.remove_row_button.clicked.connect(self._remove_row)

        table_buttons = QHBoxLayout()
        table_buttons.addWidget(self.add_row_button)
        table_buttons.addWidget(self.remove_row_button)
        table_buttons.addStretch(1)

        fields_widget = QWidget()
        fields_layout = QVBoxLayout(fields_widget)
        fields_layout.setContentsMargins(0, 0, 0, 0)
        fields_layout.addWidget(self.field_table, 1)
        fields_layout.addLayout(table_buttons)

        self.schema_editor = JsonEditor()
        self.schema_editor.setPlaceholderText(tr("Paste a JSON Schema here."))

        self.mock_kind_combo = QComboBox()
        for kind in FIELD_TYPES:
            self.mock_kind_combo.addItem(kind, kind)
        mock_widget = QWidget()
        mock_layout = QHBoxLayout(mock_widget)
        mock_layout.setContentsMargins(0, 0, 0, 0)
        mock_layout.addWidget(QLabel(tr("Value kind:")))
        mock_layout.addWidget(self.mock_kind_combo)
        mock_layout.addStretch(1)

        self.mode_stack = QStackedWidget()
        self.mode_stack.addWidget(fields_widget)
        self.mode_stack.addWidget(self.schema_editor)
        self.mode_stack.addWidget(mock_widget)

        self.output_editor = JsonEditor(read_only=True)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.mode_stack)
        splitter.addWidget(self.output_editor)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        self.warnings_label = QLabel("")
        self.warnings_label.setObjectName("BadgeWarning")
        self.warnings_label.setWordWrap(True)

        self.add_layout(top)
        self.add(splitter, 1)
        self.add(self.warnings_label)
        register_retranslate(self)

    # ------------------------------------------------------------------ 表格
    def _seed_default_rows(self) -> None:
        defaults = [
            ("id", "integer", True, "1", "100000", "", "", ""),
            ("name", "name", True, "", "", "", "", ""),
            ("email", "email", True, "", "", "", "", ""),
            ("phone", "phone", False, "", "", "", "", ""),
            ("created", "date", False, "", "", "", "", ""),
            ("tags", "array", False, "", "", "", "", "3"),
        ]
        self.field_table.setRowCount(len(defaults))
        for row, values in enumerate(defaults):
            self._fill_row(row, values)

    def _fill_row(self, row: int, values) -> None:
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            if column == 2:
                item.setCheckState(Qt.CheckState.Checked if value else Qt.CheckState.Unchecked)
            self.field_table.setItem(row, column, item)
        type_item = self.field_table.item(row, 1)
        combo = QComboBox()
        combo.addItems(list(FIELD_TYPES))
        combo.setCurrentText(values[1])
        self.field_table.setCellWidget(row, 1, combo)

    def _add_row(self) -> None:
        row = self.field_table.rowCount()
        self.field_table.insertRow(row)
        self._fill_row(row, ("field", "string", True, "", "", "", "", ""))

    def _remove_row(self) -> None:
        row = self.field_table.currentRow()
        if row >= 0:
            self.field_table.removeRow(row)

    def _rows(self) -> list:
        rows = []
        for row in range(self.field_table.rowCount()):
            name_item = self.field_table.item(row, 0)
            name = name_item.text().strip() if name_item else ""
            if not name:
                continue
            combo = self.field_table.cellWidget(row, 1)
            kind = combo.currentText() if isinstance(combo, QComboBox) else "string"
            required_item = self.field_table.item(row, 2)
            required = required_item.checkState() == Qt.CheckState.Checked if required_item else True

            def cell(column: int) -> str:
                item = self.field_table.item(row, column)
                return item.text().strip() if item else ""

            rows.append({
                "name": name,
                "type": kind,
                "required": required,
                "minimum": cell(3),
                "maximum": cell(4),
                "length": cell(5),
                "enum": cell(6),
                "array_length": cell(7),
            })
        return rows

    # ------------------------------------------------------------------ 生成
    def _options(self) -> MockOptions:
        seed_text = self.seed_edit.text().strip()
        seed = int(seed_text) if seed_text.lstrip("-").isdigit() else None
        return MockOptions(locale=self.locale_combo.currentData(), seed=seed,
                           count=self.count_spin.value())

    def run_generate(self) -> None:
        mode = self.mode_combo.currentData()
        warnings: list = []
        try:
            if mode == MODE_FIELDS:
                specs = [field_spec_from_row(row) for row in self._rows()]
                if not specs:
                    dialogs.warn(self, tr("Data Generator"), tr("Add at least one field row."))
                    return
                count = self.count_spin.value()
                value = generate_from_fields(specs, root="array" if count > 1 else "object",
                                             count=count, options=self._options())
            elif mode == MODE_SCHEMA:
                parsed = parse_json(self.schema_editor.text())
                if not parsed.ok:
                    dialogs.warn(self, tr("Data Generator"),
                                 tr("The schema is not valid JSON."))
                    return
                value, warnings = generate_from_schema(parsed.value, options=self._options())
            else:
                kind = self.mock_kind_combo.currentData()
                options = self._options()
                if options.count == 1:
                    value = mock_value(kind, options)
                else:
                    value = [mock_value(kind, options) for _ in range(options.count)]
        except Exception as exc:  # noqa: BLE001 - 生成器异常不应崩溃
            dialogs.warn(self, tr("Data Generator"), f"{type(exc).__name__}: {exc}")
            return
        self.output_editor.set_text(dumps(value))
        self.warnings_label.setText(
            "\n".join(f"• {w}" for w in warnings[:10]) if warnings else "")
        self.status_message.emit(tr("Generated {n} characters.").format(
            n=len(self.output_editor.text())), 4000)

    def _on_mode_changed(self, index: int) -> None:
        self.mode_stack.setCurrentIndex(index)

    # ------------------------------------------------------------------ 输出
    def copy_output(self) -> None:
        text = self.output_editor.text()
        if not text:
            self.status_message.emit(tr("Nothing to copy."), 3000)
            return
        QGuiApplication.clipboard().setText(text)
        self.status_message.emit(tr("Copied {n} characters to the clipboard.").format(n=len(text)), 3000)

    def save_output(self) -> None:
        text = self.output_editor.text()
        if not text:
            self.status_message.emit(tr("Nothing to save."), 3000)
            return
        path, _selected = QFileDialog.getSaveFileName(self, tr("Save output"), "generated.json",
                                                      "*.json;;All files (*)")
        if not path:
            return
        result = write_text_file(path, text)
        if result.ok:
            self.status_message.emit(tr("Saved {path}").format(path=path), 5000)
        else:
            dialogs.warn(self, tr("Cannot save file"), result.error)

    # ------------------------------------------------------------------ 主题/翻译
    def apply_theme(self) -> None:
        palette = theme.palette()
        family = settings.font_family
        self.schema_editor.apply_theme(palette, family)
        self.output_editor.apply_theme(palette, family)

    def retranslate(self) -> None:
        super().retranslate()
        self.mode_combo.setItemText(0, tr("Field table"))
        self.mode_combo.setItemText(1, tr("From JSON Schema"))
        self.mode_combo.setItemText(2, tr("Random test data"))
        self.locale_combo.setItemText(0, tr("Chinese (zh)"))
        self.locale_combo.setItemText(1, tr("English (en)"))
        self.seed_edit.setPlaceholderText(tr("empty = random"))
        self.run_button.setText(tr("Generate"))
        self.copy_button.setText(tr("Copy output"))
        self.save_button.setText(tr("Save output…"))
        self.send_button.setText(tr("Send to editor"))
        self.add_row_button.setText(tr("Add row"))
        self.remove_row_button.setText(tr("Remove row"))
        self.field_table.setHorizontalHeaderLabels([tr(name) for name in TABLE_COLUMNS])
        self.schema_editor.setPlaceholderText(tr("Paste a JSON Schema here."))

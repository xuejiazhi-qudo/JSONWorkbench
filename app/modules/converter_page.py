"""数据转换页：10+ 种转换，参数清晰、结果可复制可保存。

输入可以是手贴的文本，也可以一键从编辑工作区载入当前文档；
输出只读展示，警告码会本地化后列在输出下方。
"""

from __future__ import annotations

from app.utils.pagejob import run_latest
from app.widgets.tabwidget import ASYNC_VALIDATE_CHARS

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.core.jsonfilemanager import write_text_file
from app.core.jsonparser import parse_json
from app.editor.jsoneditor import JsonEditor
from app.engines import converters as cv
from app.i18n import register_retranslate, tr, warning_text
from app.settings.settings import settings
from app.utils import dialogs
from app.widgets.icons import icon
from app.widgets.page import PageShell
from app.widgets.theme import theme


@dataclass
class OptionSpec:
    name: str
    kind: str                      # combo | check | line | spin
    label: str
    default: Any = None
    choices: tuple = ()
    minimum: int = 0
    maximum: int = 10_000_000


@dataclass
class ConversionSpec:
    conv_id: str
    label: str
    input_kind: str                # json | text | raw
    output_suffix: str
    options: List[OptionSpec] = field(default_factory=list)


CONVERSIONS: List[ConversionSpec] = [
    ConversionSpec("json_csv", "JSON → CSV", "json", "csv", [
        OptionSpec("delimiter", "combo", "Delimiter", ",", (",", ";", "\t", "|")),
        OptionSpec("flatten", "check", "Flatten nested objects", True),
        OptionSpec("array_handling", "combo", "Array handling", "expand",
                   ("expand", "join", "cell")),
        OptionSpec("include_header", "check", "Include header row", True),
        OptionSpec("encoding", "combo", "Encoding", "utf-8", ("utf-8", "utf-8-sig", "gbk")),
        OptionSpec("quote_all", "check", "Quote all fields", False),
        OptionSpec("null_text", "line", "Text for null", ""),
    ]),
    ConversionSpec("csv_json", "CSV → JSON", "text", "json", [
        OptionSpec("delimiter", "combo", "Delimiter", ",", (",", ";", "\t", "|")),
        OptionSpec("has_header", "check", "First row is the header", True),
        OptionSpec("infer_types", "check", "Infer number/boolean/null", True),
        OptionSpec("root", "combo", "Root type", "array", ("array", "object")),
        OptionSpec("trim_whitespace", "check", "Trim whitespace", True),
    ]),
    ConversionSpec("json_xml", "JSON → XML", "json", "xml", [
        OptionSpec("root_name", "line", "Root element", "root"),
        OptionSpec("item_name", "line", "Array item element", "item"),
        OptionSpec("attr_prefix", "line", "Attribute prefix", "@"),
        OptionSpec("text_key", "line", "Text key", "#text"),
        OptionSpec("pretty", "check", "Pretty print", True),
        OptionSpec("declaration", "check", "XML declaration", True),
    ]),
    ConversionSpec("xml_json", "XML → JSON", "text", "json", [
        OptionSpec("attr_prefix", "line", "Attribute prefix", "@"),
        OptionSpec("text_key", "line", "Text key", "#text"),
        OptionSpec("infer_types", "check", "Infer number/boolean/null", True),
        OptionSpec("root_as_array", "check", "Root as array", False),
    ]),
    ConversionSpec("json_yaml", "JSON → YAML", "json", "yaml", [
        OptionSpec("indent", "spin", "Indent", 2, minimum=1, maximum=16),
        OptionSpec("sort_keys", "check", "Sort keys", False),
        OptionSpec("allow_unicode", "check", "Allow unicode", True),
    ]),
    ConversionSpec("yaml_json", "YAML → JSON", "text", "json", []),
    ConversionSpec("escape", "Escape JSON string", "raw", "txt", []),
    ConversionSpec("unescape", "Unescape JSON string", "raw", "txt", []),
    ConversionSpec("split", "Split array", "json", "jsonl", [
        OptionSpec("chunk_size", "spin", "Chunk size (0 = by field)", 0),
        OptionSpec("by_field", "line", "Split by field (dotted path)", ""),
    ]),
    ConversionSpec("merge", "Merge documents", "json", "json", [
        OptionSpec("strategy", "combo", "Merge strategy", "deep",
                   ("deep", "shallow", "concat")),
        OptionSpec("array_strategy", "combo", "Array strategy", "append",
                   ("append", "replace", "unique")),
        OptionSpec("conflict", "combo", "On conflict", "last", ("last", "first", "error")),
    ]),
    ConversionSpec("unwrap", "Unwrap string-encoded JSON", "json", "json", [
        OptionSpec("max_layers", "spin", "Max layers", 8, minimum=1, maximum=64),
        OptionSpec("format_output", "check", "Format output", True),
    ]),
    ConversionSpec("dedupe", "Deduplicate array", "json", "json", [
        OptionSpec("key", "line", "Dedupe by field (dotted path)", ""),
        OptionSpec("case_sensitive", "check", "Case sensitive", True),
        OptionSpec("keep", "combo", "Keep", "first", ("first", "last")),
    ]),
]

_BY_ID = {spec.conv_id: spec for spec in CONVERSIONS}


class ConverterPage(PageShell):
    """侧边栏"数据转换"页。"""

    status_message = Signal(str, int)
    send_to_editor = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Data Converter",
                         "Convert between JSON, CSV, XML and YAML, or split/merge/dedupe "
                         "arrays. Numbers keep their original literals whenever the target "
                         "format allows it.")
        self._forms: Dict[str, Dict[str, Any]] = {}
        self._form_stack = QStackedWidget()
        self._last_result: Optional[cv.ConvertResult] = None

        self.input_editor = JsonEditor(read_only=False)
        self.output_editor = JsonEditor(read_only=True)

        self.conversion_combo = QComboBox()
        for spec in CONVERSIONS:
            self.conversion_combo.addItem(tr(spec.label), spec.conv_id)
        self.conversion_combo.currentIndexChanged.connect(self._on_conversion_changed)

        self.run_button = QPushButton(tr("Convert"))
        self.run_button.setProperty("class", "primary")
        self.run_button.setIcon(icon("convert", size=15, kind="on_accent"))
        self.run_button.setCursor(Qt.PointingHandCursor)
        self.run_button.clicked.connect(self.run_conversion)

        self.load_button = QPushButton(tr("Load from editor"))
        self.load_button.setIcon(icon("upload", size=15, kind="muted"))
        self.load_button.clicked.connect(self.load_from_workspace)

        self.copy_button = QPushButton(tr("Copy output"))
        self.copy_button.setIcon(icon("copy", size=15, kind="muted"))
        self.copy_button.clicked.connect(self.copy_output)

        self.save_button = QPushButton(tr("Save output…"))
        self.save_button.setIcon(icon("download", size=15, kind="muted"))
        self.save_button.clicked.connect(self.save_output)

        self.send_button = QPushButton(tr("Send to editor"))
        self.send_button.setIcon(icon("arrow-right", size=15, kind="muted"))
        self.send_button.clicked.connect(self.send_output_to_editor)

        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(QLabel(tr("Conversion:")))
        top.addWidget(self.conversion_combo, 1)
        top.addWidget(self.run_button)
        top.addWidget(self.load_button)
        top.addWidget(self.copy_button)
        top.addWidget(self.save_button)
        top.addWidget(self.send_button)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)
        left_layout.addWidget(self._build_options_host(), 0)
        left_layout.addWidget(QLabel(tr("Input")), 0)
        left_layout.addWidget(self.input_editor, 1)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)
        right_layout.addWidget(QLabel(tr("Output")), 0)
        right_layout.addWidget(self.output_editor, 1)
        self.warnings_label = QLabel("")
        self.warnings_label.setObjectName("BadgeWarning")
        self.warnings_label.setWordWrap(True)
        self.warnings_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        right_layout.addWidget(self.warnings_label)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setChildrenCollapsible(False)

        self.add_layout(top)
        self.add(splitter, 1)

        self._on_conversion_changed(0)
        register_retranslate(self)

    # ------------------------------------------------------------------ 选项表单
    def _build_options_host(self) -> QWidget:
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._form_stack)
        for spec in CONVERSIONS:
            self._form_stack.addWidget(self._build_form(spec))
        return host

    def _build_form(self, spec: ConversionSpec) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setMaximumHeight(190)
        inner = QWidget()
        form = QFormLayout(inner)
        form.setContentsMargins(2, 2, 2, 2)
        widgets: Dict[str, Any] = {}
        if not spec.options:
            hint = QLabel(tr("This conversion has no options."))
            hint.setObjectName("HintLabel")
            form.addRow(hint)
        for option in spec.options:
            widget = self._build_option_widget(option)
            widgets[option.name] = widget
            form.addRow(tr(option.label), widget)
        scroll.setWidget(inner)
        self._forms[spec.conv_id] = widgets
        return scroll

    @staticmethod
    def _build_option_widget(option: OptionSpec) -> QWidget:
        if option.kind == "check":
            box = QCheckBox()
            box.setChecked(bool(option.default))
            return box
        if option.kind == "combo":
            combo = QComboBox()
            for choice in option.choices:
                combo.addItem(str(choice), choice)
            index = combo.findData(option.default)
            combo.setCurrentIndex(max(0, index))
            return combo
        if option.kind == "spin":
            spin = QSpinBox()
            spin.setRange(option.minimum, option.maximum)
            spin.setValue(int(option.default or 0))
            return spin
        edit = QLineEdit(str(option.default or ""))
        return edit

    def _option_values(self, conv_id: str) -> Dict[str, Any]:
        values: Dict[str, Any] = {}
        for name, widget in self._forms.get(conv_id, {}).items():
            if isinstance(widget, QCheckBox):
                values[name] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                values[name] = widget.currentData()
            elif isinstance(widget, QSpinBox):
                values[name] = widget.value()
            else:
                values[name] = widget.text()
        return values

    def _on_conversion_changed(self, index: int) -> None:
        conv_id = self.conversion_combo.currentData()
        spec = _BY_ID[conv_id]
        self._form_stack.setCurrentIndex(index)
        self.input_editor.setPlaceholderText(
            tr("Paste JSON here, or load the current document from the editor.")
            if spec.input_kind == "json" else tr("Paste text here."))

    # ------------------------------------------------------------------ 执行
    def current_spec(self) -> ConversionSpec:
        return _BY_ID[self.conversion_combo.currentData()]

    def load_from_workspace(self) -> None:
        window = self.window()
        workspace = getattr(window, "workspace", None)
        if workspace is None:
            return
        tab = workspace.current_tab()
        if tab is None:
            return
        self.input_editor.set_text(tab.text())
        self.status_message.emit(tr("Loaded {n} characters from the editor.").format(
            n=len(tab.text())), 3000)

    def run_conversion(self) -> None:
        spec = self.current_spec()
        raw = self.input_editor.text()
        values = self._option_values(spec.conv_id)
        run_latest(
            self, lambda: ConverterPage._run(spec, raw, values),
            lambda result: self._show_conversion(result, spec),
            lambda: (self.current_spec().conv_id == spec.conv_id
                     and self.input_editor.text() == raw
                     and self._option_values(spec.conv_id) == values),
            background=len(raw) > ASYNC_VALIDATE_CHARS)

    def _show_conversion(self, result, spec) -> None:
        self._last_result = result
        if not result.ok:
            self.output_editor.set_text("")
            self.warnings_label.setText("")
            dialogs.warn(self, tr("Conversion failed"), self._localize_error(result.error))
            self.status_message.emit(self._localize_error(result.error), 8000)
            return
        output = result.output
        if not output and result.meta.get("pieces"):
            output = "\n\n".join(result.meta["pieces"])
        if not output and result.meta.get("value") is not None:
            from app.core.jsonformatter import dumps

            output = dumps(result.meta["value"])
        self.output_editor.set_text(output)
        self._show_warnings(result)
        meta_note = self._meta_note(result)
        self.status_message.emit(
            tr("Converted {conv}. {note}").format(conv=tr(spec.label), note=meta_note), 5000)

    @staticmethod
    def _run(spec: ConversionSpec, raw: str, values: Dict[str, Any]) -> cv.ConvertResult:
        conv_id = spec.conv_id
        try:
            if conv_id == "escape":
                return cv.ConvertResult(ok=True, output=cv.escape_json_string(raw))
            if conv_id == "unescape":
                return cv.unescape_json_string(raw)
            if spec.input_kind == "json":
                parsed = parse_json(raw)
                if not parsed.ok:
                    return cv.ConvertResult(
                        ok=False, error=f"invalid_json: {parsed.error.code if parsed.error else ''}")
                value = parsed.value
            else:
                value = raw

            if conv_id == "json_csv":
                options = cv.CsvOptions(
                    delimiter=values["delimiter"], flatten=values["flatten"],
                    array_handling=values["array_handling"],
                    include_header=values["include_header"], encoding=values["encoding"],
                    quote_all=values["quote_all"], null_text=values["null_text"])
                return cv.json_to_csv(value, options)
            if conv_id == "csv_json":
                return cv.csv_to_json(
                    value, delimiter=values["delimiter"], has_header=values["has_header"],
                    infer_types=values["infer_types"], root=values["root"],
                    trim_whitespace=values["trim_whitespace"])
            if conv_id == "json_xml":
                options = cv.XmlOptions(
                    root_name=values["root_name"] or "root", item_name=values["item_name"] or "item",
                    attr_prefix=values["attr_prefix"] or "@", text_key=values["text_key"] or "#text",
                    pretty=values["pretty"], declaration=values["declaration"])
                return cv.json_to_xml(value, options)
            if conv_id == "xml_json":
                return cv.xml_to_json(
                    value, attr_prefix=values["attr_prefix"] or "@",
                    text_key=values["text_key"] or "#text",
                    infer_types=values["infer_types"], root_as_array=values["root_as_array"])
            if conv_id == "json_yaml":
                options = cv.YamlOptions(indent=values["indent"], sort_keys=values["sort_keys"],
                                         allow_unicode=values["allow_unicode"])
                return cv.json_to_yaml(value, options)
            if conv_id == "yaml_json":
                return cv.yaml_to_json(value)
            if conv_id == "split":
                return cv.split_array(value, chunk_size=values["chunk_size"],
                                      by_field=values["by_field"].strip())
            if conv_id == "merge":
                if not isinstance(value, list):
                    return cv.ConvertResult(
                        ok=False, error="merge_not_a_list: the input must be a JSON array "
                                        "of documents")
                return cv.merge_documents(value, strategy=values["strategy"],
                                          array_strategy=values["array_strategy"],
                                          conflict=values["conflict"])
            if conv_id == "unwrap":
                from app.core.unwrap import unwrap as _unwrap

                info = _unwrap(raw, max_layers=int(values["max_layers"]))
                if not info.ok:
                    return cv.ConvertResult(ok=False, error=info.error)
                if not info.wrapped:
                    return cv.ConvertResult(
                        ok=False, error="not_wrapped: the document is not a JSON string "
                                        "wrapping another JSON value")
                if values["format_output"]:
                    result = _format_unwrapped(info.text)
                    result.meta["layers"] = info.layers
                    return result
                return cv.ConvertResult(ok=True, output=info.text,
                                        meta={"layers": info.layers})
            if conv_id == "dedupe":
                return cv.dedupe_array(value, key=values["key"].strip(),
                                       case_sensitive=values["case_sensitive"],
                                       keep=values["keep"])
            return cv.ConvertResult(ok=False, error=f"unknown_conversion: {conv_id}")
        except Exception as exc:  # noqa: BLE001 - 引擎已兜底，这里再保险一层
            return cv.ConvertResult(ok=False, error=f"unexpected: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ 结果展示
    @staticmethod
    def _localize_error(error: str) -> str:
        code, _, detail = error.partition(":")
        return warning_text(code.strip(), detail.strip())

    def _show_warnings(self, result: cv.ConvertResult) -> None:
        if not result.warnings:
            self.warnings_label.setText("")
            return
        texts = []
        for item in result.warnings[:8]:
            code, _, detail = str(item).partition(":")
            texts.append("• " + warning_text(code.strip(), detail.strip()))
        if len(result.warnings) > 8:
            texts.append(tr("… and {n} more").format(n=len(result.warnings) - 8))
        self.warnings_label.setText("\n".join(texts))

    @staticmethod
    def _meta_note(result: cv.ConvertResult) -> str:
        notes = []
        meta = result.meta or {}
        if "rows" in meta:
            notes.append(tr("{n} rows").format(n=meta["rows"]))
        if "removed" in meta:
            notes.append(tr("-{n} removed").format(n=meta["removed"]))
        if "pieces" in meta:
            notes.append(tr("{n} pieces").format(n=len(meta["pieces"])))
        if "files" in meta:
            notes.append(tr("{n} files").format(n=len(meta["files"])))
        return " · ".join(notes)

    # ------------------------------------------------------------------ 输出动作
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
        spec = self.current_spec()
        path, _selected = QFileDialog.getSaveFileName(
            self, tr("Save output"), f"converted.{spec.output_suffix}",
            f"*.{spec.output_suffix};;All files (*)")
        if not path:
            return
        encoding = "utf-8"
        if spec.conv_id == "json_csv":
            encoding = self._option_values(spec.conv_id).get("encoding", "utf-8")
        result = write_text_file(path, text, encoding=encoding)
        if result.ok:
            self.status_message.emit(tr("Saved {path}").format(path=path), 5000)
        else:
            dialogs.warn(self, tr("Cannot save file"), result.error)

    def send_output_to_editor(self) -> None:
        text = self.output_editor.text()
        if not text:
            self.status_message.emit(tr("Nothing to send."), 3000)
            return
        self.send_to_editor.emit(text)

    # ------------------------------------------------------------------ 主题/翻译
    def apply_theme(self) -> None:
        palette = theme.palette()
        family = settings.font_family
        self.input_editor.apply_theme(palette, family)
        self.output_editor.apply_theme(palette, family)

    def retranslate(self) -> None:
        super().retranslate()
        for index, spec in enumerate(CONVERSIONS):
            self.conversion_combo.setItemText(index, tr(spec.label))
        self.run_button.setText(tr("Convert"))
        self.load_button.setText(tr("Load from editor"))
        self.copy_button.setText(tr("Copy output"))
        self.save_button.setText(tr("Save output…"))
        self.send_button.setText(tr("Send to editor"))
        for spec in CONVERSIONS:
            for option in spec.options:
                widget = self._forms.get(spec.conv_id, {}).get(option.name)
                if widget is None:
                    continue
                row_label = self._form_label_for(spec, option)
                if row_label is not None:
                    row_label.setText(tr(option.label))
        self._on_conversion_changed(self.conversion_combo.currentIndex())

    def _form_label_for(self, spec: ConversionSpec, option: OptionSpec):
        form_widget = self._form_stack.widget(CONVERSIONS.index(spec))
        inner = form_widget.widget()
        layout = inner.layout()
        for row in range(layout.rowCount()):
            label_item = layout.itemAt(row, QFormLayout.LabelRole)
            field_item = layout.itemAt(row, QFormLayout.FieldRole)
            if field_item is not None and field_item.widget() is self._forms[spec.conv_id].get(option.name):
                return label_item.widget() if label_item else None
        return None


def _format_unwrapped(text: str) -> cv.ConvertResult:
    """解包后按默认美化选项重新格式化（保留数字字面量）。"""
    from app.core.jsonformatter import FormatOptions, format_text

    result = format_text(text, FormatOptions())
    return cv.ConvertResult(ok=result.ok, output=result.output,
                            error=result.error_message,
                            warnings=list(result.parse.warnings) if result.parse else [])

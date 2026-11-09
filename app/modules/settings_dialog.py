"""偏好设置对话框（真实生效，全部写入 QSettings）。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app import APP_NAME, APP_VERSION
from app.core.jsonfilemanager import LARGE_FILE_BYTES
from app.i18n import LANGUAGES, register_retranslate, tr
from app.settings.settings import settings
from app.utils import dialogs
from app.widgets.theme import THEMES


class SettingsDialog(QDialog):
    """设置：外观 / 编辑器 / 解析 / 隐私。"""

    settings_changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Settings"))
        self.setMinimumSize(520, 460)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_appearance(), tr("Appearance"))
        self.tabs.addTab(self._build_editor(), tr("Editor"))
        self.tabs.addTab(self._build_parsing(), tr("Parsing"))
        self.tabs.addTab(self._build_privacy(), tr("Privacy"))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply)
        buttons.button(QDialogButtonBox.StandardButton.Apply).setText(tr("Apply"))
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.apply_changes)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addWidget(buttons)
        register_retranslate(self)

    # ------------------------------------------------------------------ 分页
    def _build_appearance(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(14, 14, 14, 14)

        self.theme_combo = QComboBox()
        self.theme_combo.addItem(tr("Dark"), THEMES[0])
        self.theme_combo.addItem(tr("Light"), THEMES[1])
        self.theme_combo.setCurrentIndex(0 if settings.theme == THEMES[0] else 1)
        form.addRow(tr("Theme"), self.theme_combo)

        self.language_combo = QComboBox()
        for code, label in LANGUAGES:
            self.language_combo.addItem(label, code)
        index = self.language_combo.findData(settings.language)
        self.language_combo.setCurrentIndex(max(0, index))
        form.addRow(tr("Language"), self.language_combo)

        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(9, 24)
        self.font_size_spin.setValue(settings.font_size)
        self.font_size_spin.setSuffix(" pt")
        form.addRow(tr("Editor font size"), self.font_size_spin)

        self.font_family_combo = QComboBox()
        self.font_family_combo.setEditable(True)
        families = self._monospace_families()
        self.font_family_combo.addItems(families)
        if settings.font_family:
            self.font_family_combo.setCurrentText(settings.font_family)
        elif families:
            self.font_family_combo.setCurrentText(families[0])
        form.addRow(tr("Editor font"), self.font_family_combo)

        self.highlight_spin = QSpinBox()
        self.highlight_spin.setRange(64, 200 * 1024)
        self.highlight_spin.setSingleStep(256)
        self.highlight_spin.setValue(settings.highlight_limit_bytes // 1024)
        self.highlight_spin.setSuffix(" KB")
        form.addRow(tr("Syntax highlighting limit"), self.highlight_spin)
        hint = QLabel(tr("Above this size highlighting is disabled to keep typing smooth."))
        hint.setObjectName("HintLabel")
        hint.setWordWrap(True)
        form.addRow("", hint)
        self._highlight_hint = hint
        return page

    @staticmethod
    def _monospace_families() -> list:
        from PySide6.QtGui import QFontDatabase

        families = [name for name in QFontDatabase.families() if not name.startswith("@")]
        preferred = [
            "Cascadia Mono", "Consolas", "JetBrains Mono", "Fira Code", "Source Code Pro",
            "DejaVu Sans Mono", "Menlo", "Microsoft YaHei Mono", "Sarasa Mono SC",
        ]
        available = [name for name in preferred if name in families]
        return available or ["Consolas", "Courier New"]

    def _build_editor(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(14, 14, 14, 14)

        self.indent_combo = QComboBox()
        for label, value in ((tr("2 spaces"), "2"), (tr("4 spaces"), "4"),
                             (tr("8 spaces"), "8"), (tr("Tab"), "tab"),
                             (tr("None (single line)"), "none")):
            self.indent_combo.addItem(label, value)
        index = self.indent_combo.findData(settings.indent_choice)
        self.indent_combo.setCurrentIndex(max(0, index))
        form.addRow(tr("Default indent"), self.indent_combo)

        self.wrap_check = QCheckBox(tr("Word wrap"))
        self.wrap_check.setChecked(settings.word_wrap)
        form.addRow("", self.wrap_check)

        self.line_numbers_check = QCheckBox(tr("Show line numbers"))
        self.line_numbers_check.setChecked(settings.line_numbers)
        form.addRow("", self.line_numbers_check)

        self.auto_close_check = QCheckBox(tr("Auto-close brackets and quotes"))
        self.auto_close_check.setChecked(settings.auto_close_brackets)
        form.addRow("", self.auto_close_check)

        self.auto_indent_check = QCheckBox(tr("Auto indent"))
        self.auto_indent_check.setChecked(settings.auto_indent)
        form.addRow("", self.auto_indent_check)

        self.trailing_newline_check = QCheckBox(tr("Add a trailing newline when formatting"))
        self.trailing_newline_check.setChecked(bool(settings.value("format/trailing_newline", False)))
        form.addRow("", self.trailing_newline_check)

        self.ensure_ascii_check = QCheckBox(tr("Escape non-ASCII characters (\\uXXXX)"))
        self.ensure_ascii_check.setChecked(bool(settings.value("format/ensure_ascii", False)))
        form.addRow("", self.ensure_ascii_check)
        return page

    def _build_parsing(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(14, 14, 14, 14)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem(tr("Auto (by file extension)"), "auto")
        self.mode_combo.addItem(tr("Always strict JSON"), "json")
        self.mode_combo.addItem(tr("Always JSONC (comments allowed)"), "jsonc")
        index = self.mode_combo.findData(settings.document_mode)
        self.mode_combo.setCurrentIndex(max(0, index))
        form.addRow(tr("Document type"), self.mode_combo)

        self.depth_spin = QSpinBox()
        self.depth_spin.setRange(8, 200000)
        self.depth_spin.setSingleStep(100)
        self.depth_spin.setValue(settings.max_depth)
        form.addRow(tr("Max nesting depth"), self.depth_spin)
        depth_hint = QLabel(tr("Parsing is iterative, so deep nesting cannot crash the app; "
                               "this limit only keeps runaway documents in check."))
        depth_hint.setObjectName("HintLabel")
        depth_hint.setWordWrap(True)
        form.addRow("", depth_hint)
        self._depth_hint = depth_hint

        self.duplicates_check = QCheckBox(tr("Detect duplicate keys"))
        self.duplicates_check.setChecked(settings.detect_duplicate_keys)
        form.addRow("", self.duplicates_check)
        return page

    def _build_privacy(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)

        offline = QGroupBox(tr("Offline & privacy"))
        offline_form = QFormLayout(offline)
        note = QLabel(tr("{app} never connects to the network. No telemetry, no analytics, "
                         "no uploads — every document is processed on this machine only.")
                      .format(app=APP_NAME))
        note.setObjectName("HintLabel")
        note.setWordWrap(True)
        offline_form.addRow(note)
        self._privacy_note = note

        self.history_check = QCheckBox(tr("Keep history of recent operations"))
        self.history_check.setChecked(settings.history_enabled)
        offline_form.addRow("", self.history_check)

        self.store_content_check = QCheckBox(tr("Store the original JSON text in history"))
        self.store_content_check.setChecked(settings.history_store_content)
        offline_form.addRow("", self.store_content_check)
        warn = QLabel(tr("Off by default: history stores only file names and metadata."))
        warn.setObjectName("BadgeWarning")
        warn.setWordWrap(True)
        offline_form.addRow("", warn)
        self._store_warn = warn

        self.history_spin = QSpinBox()
        self.history_spin.setRange(10, 5000)
        self.history_spin.setValue(settings.history_limit)
        offline_form.addRow(tr("Max history entries"), self.history_spin)

        self.recent_check = QCheckBox(tr("Remember recently opened files"))
        self.recent_check.setChecked(settings.recent_files_enabled)
        offline_form.addRow("", self.recent_check)

        layout.addWidget(offline)

        actions = QHBoxLayout()
        self.clear_history_button = QPushButton(tr("Clear history now"))
        self.clear_history_button.setProperty("class", "danger")
        self.clear_history_button.clicked.connect(self._clear_history)
        self.clear_recent_button = QPushButton(tr("Clear recent files"))
        self.clear_recent_button.clicked.connect(self._clear_recent)
        self.reset_button = QPushButton(tr("Restore default settings"))
        self.reset_button.clicked.connect(self._reset_defaults)
        actions.addWidget(self.clear_history_button)
        actions.addWidget(self.clear_recent_button)
        actions.addWidget(self.reset_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        layout.addStretch(1)

        version = QLabel(tr("Version {version} · Qt {qt} · Python {py}").format(
            version=APP_VERSION, qt=_qt_version(), py=_python_version()))
        version.setObjectName("MutedLabel")
        layout.addWidget(version)
        self._version_label = version
        return page

    # ------------------------------------------------------------------ 动作
    def _clear_history(self) -> None:
        from app.history.store import HistoryStore

        store = HistoryStore()
        count = store.clear_all()
        store.close()
        dialogs.information(self, tr("History cleared"),
                            tr("Removed {n} history entries.").format(n=count))

    def _clear_recent(self) -> None:
        settings.clear_recent_files()
        settings.sync()
        dialogs.information(self, tr("Recent files cleared"),
                            tr("The recent files list is now empty."))

    def _reset_defaults(self) -> None:
        if not dialogs.confirm(
                self, tr("Restore default settings?"),
                tr("All settings except theme, language and window layout will be reset."),
                default=False):
            return
        settings.reset_to_defaults()
        self._load_from_settings()
        self.settings_changed.emit()

    def _load_from_settings(self) -> None:
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(settings.theme)))
        self.language_combo.setCurrentIndex(max(0, self.language_combo.findData(settings.language)))
        self.font_size_spin.setValue(settings.font_size)
        self.indent_combo.setCurrentIndex(max(0, self.indent_combo.findData(settings.indent_choice)))
        self.wrap_check.setChecked(settings.word_wrap)
        self.line_numbers_check.setChecked(settings.line_numbers)
        self.auto_close_check.setChecked(settings.auto_close_brackets)
        self.auto_indent_check.setChecked(settings.auto_indent)
        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(settings.document_mode)))
        self.depth_spin.setValue(settings.max_depth)
        self.duplicates_check.setChecked(settings.detect_duplicate_keys)
        self.history_check.setChecked(settings.history_enabled)
        self.store_content_check.setChecked(settings.history_store_content)
        self.history_spin.setValue(settings.history_limit)
        self.recent_check.setChecked(settings.recent_files_enabled)

    def apply_changes(self) -> None:
        settings.theme = self.theme_combo.currentData()
        settings.language = self.language_combo.currentData()
        settings.font_size = self.font_size_spin.value()
        settings.font_family = self.font_family_combo.currentText().strip()
        settings.highlight_limit_bytes = self.highlight_spin.value() * 1024
        settings.indent_choice = self.indent_combo.currentData()
        settings.word_wrap = self.wrap_check.isChecked()
        settings.line_numbers = self.line_numbers_check.isChecked()
        settings.auto_close_brackets = self.auto_close_check.isChecked()
        settings.auto_indent = self.auto_indent_check.isChecked()
        settings.set_value("format/trailing_newline", self.trailing_newline_check.isChecked())
        settings.set_value("format/ensure_ascii", self.ensure_ascii_check.isChecked())
        settings.document_mode = self.mode_combo.currentData()
        settings.max_depth = self.depth_spin.value()
        settings.detect_duplicate_keys = self.duplicates_check.isChecked()
        settings.history_enabled = self.history_check.isChecked()
        settings.history_store_content = self.store_content_check.isChecked()
        settings.history_limit = self.history_spin.value()
        settings.recent_files_enabled = self.recent_check.isChecked()
        settings.sync()
        self.settings_changed.emit()

    def _on_accept(self) -> None:
        self.apply_changes()
        self.accept()

    def retranslate(self) -> None:
        self.setWindowTitle(tr("Settings"))
        self._privacy_note.setText(tr(
            "{app} never connects to the network. No telemetry, no analytics, "
            "no uploads — every document is processed on this machine only.").format(app=APP_NAME))
        self._version_label.setText(tr("Version {version} · Qt {qt} · Python {py}").format(
            version=APP_VERSION, qt=_qt_version(), py=_python_version()))


def _qt_version() -> str:
    from PySide6.QtCore import qVersion

    return qVersion()


def _python_version() -> str:
    import sys

    return ".".join(str(part) for part in sys.version_info[:3])


def large_file_hint() -> str:
    return tr("Files above {size} are treated as large documents.").format(
        size=f"{LARGE_FILE_BYTES // (1024 * 1024)} MB")

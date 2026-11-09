"""编辑工作区：多标签编辑器 + 操作条 + 右侧问题/统计面板。

格式化、压缩、校验、排序都在这一页完成。两条产品红线：

* **排序是独立开关**，普通格式化绝不改变键顺序或数组元素顺序。
* 格式化后会回读并做语义一致性校验，**不一致就拒绝写入**，宁可不动用户的数据。
"""

from __future__ import annotations

from typing import Callable, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.jsonformatter import (
    INDENT_2,
    INDENT_4,
    INDENT_8,
    INDENT_TAB,
    SORT_ALPHA,
    SORT_UNICODE,
    FormatOptions,
    dumps,
    format_text,
    minify_text,
)
from app.core.jsonparser import MODE_JSON, MODE_JSONC, Issue, ParseStats
from app.core.jsonparser import parse_json as _parse_for_unwrap
from app.core.unwrap import unwrap
from app.core.jsonvalidator import ValidationResult, apply_fix, auto_fix, propose_fix
from app.i18n import issue_text, register_retranslate, tr
from app.modules.tree_view import TreePanel
from app.settings.settings import settings
from app.utils import dialogs
from app.utils.worker import start_task
from app.widgets.icons import icon
from app.widgets.tabwidget import ASYNC_VALIDATE_CHARS, EditorTab, WorkspaceTabs
from app.widgets.theme import theme

INDENT_ITEMS = (("2", INDENT_2), ("4", INDENT_4), ("8", INDENT_8), ("tab", INDENT_TAB),
                ("none", ""))


class SortDialog(QDialog):
    """排序选项对话框。排序会改变顺序，因此必须显式确认。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Sort JSON"))
        self.setMinimumWidth(400)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem(tr("Alphabetical (case-insensitive)"), SORT_ALPHA)
        self.mode_combo.addItem(tr("Unicode code point"), SORT_UNICODE)

        self.recursive_check = QCheckBox(tr("Apply to nested objects"))
        self.recursive_check.setChecked(True)
        self.array_check = QCheckBox(tr("Also sort array items"))
        self.warning_label = QLabel(tr("Sorting array items changes the order of your data."))
        self.warning_label.setObjectName("BadgeWarning")
        self.warning_label.setWordWrap(True)
        self.warning_label.setVisible(False)
        self.array_check.toggled.connect(self.warning_label.setVisible)

        form = QFormLayout()
        form.addRow(tr("Key order"), self.mode_combo)
        form.addRow("", self.recursive_check)
        form.addRow("", self.array_check)
        form.addRow("", self.warning_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def options(self) -> dict:
        return {
            "sort_keys": True,
            "sort_mode": self.mode_combo.currentData(),
            "sort_recursive": self.recursive_check.isChecked(),
            "sort_arrays": self.array_check.isChecked(),
        }

    def retranslate(self) -> None:
        self.setWindowTitle(tr("Sort JSON"))
        self.mode_combo.setItemText(0, tr("Alphabetical (case-insensitive)"))
        self.mode_combo.setItemText(1, tr("Unicode code point"))
        self.recursive_check.setText(tr("Apply to nested objects"))
        self.array_check.setText(tr("Also sort array items"))
        self.warning_label.setText(tr("Sorting array items changes the order of your data."))


class IssuesPanel(QWidget):
    """校验问题列表 + 修复建议。双击条目跳到出错位置。"""

    jump_requested = Signal(int, int)        # offset, length
    apply_fix_requested = Signal(object)     # Issue
    apply_all_requested = Signal()

    COLUMNS = ("Severity", "Issue", "Line", "Col", "Offset")

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._issues: List[Issue] = []
        self._source_getter: Optional[Callable[[], str]] = None

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr(name) for name in self.COLUMNS])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        header = self.tree.header()
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for column in (0, 2, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.tree.itemActivated.connect(self._on_activated)
        self.tree.itemSelectionChanged.connect(self._on_selected)

        self.summary = QLabel("")
        self.summary.setObjectName("MutedLabel")
        self.summary.setWordWrap(True)

        self.detail = QLabel("")
        self.detail.setObjectName("HintLabel")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.detail.setMinimumHeight(52)

        self.fix_button = QPushButton(tr("Apply suggested fix"))
        self.fix_button.setIcon(icon("wand", size=15, kind="accent"))
        self.fix_button.setEnabled(False)
        self.fix_button.setCursor(Qt.PointingHandCursor)
        self.fix_button.clicked.connect(self._on_apply_fix)

        self.fix_all_button = QPushButton(tr("Auto-fix all safe issues"))
        self.fix_all_button.setIcon(icon("refresh", size=15, kind="accent"))
        self.fix_all_button.setCursor(Qt.PointingHandCursor)
        self.fix_all_button.clicked.connect(self.apply_all_requested)

        # 纵向排列：窄面板里横排会把按钮文字截断
        buttons = QVBoxLayout()
        buttons.setSpacing(6)
        buttons.addWidget(self.fix_button)
        buttons.addWidget(self.fix_all_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        layout.addWidget(self.summary)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.detail)
        layout.addLayout(buttons)

        register_retranslate(self)

    def bind_source(self, getter: Callable[[], str]) -> None:
        """注入"取当前文档文本"的回调，用于生成修复建议。"""
        self._source_getter = getter

    def set_issues(self, issues: List[Issue]) -> None:
        self._issues = list(issues)
        self.tree.clear()
        palette = theme.palette()
        for index, issue in enumerate(self._issues):
            title, message, _ = issue_text(issue)
            item = QTreeWidgetItem([
                tr("Error") if issue.is_error else tr("Warning"),
                f"{title}: {message}",
                str(issue.line),
                str(issue.column),
                str(issue.offset),
            ])
            color = palette.qcolor("DANGER") if issue.is_error else palette.qcolor("WARNING")
            item.setForeground(0, color)
            item.setToolTip(1, f"{title}\n{message}")
            self.tree.addTopLevelItem(item)
            del index
        errors = sum(1 for issue in self._issues if issue.is_error)
        warnings = len(self._issues) - errors
        if not self._issues:
            self.summary.setText(tr("No issues found."))
        else:
            self.summary.setText(tr("{errors} error(s), {warnings} warning(s)").format(
                errors=errors, warnings=warnings))
        self.detail.setText("")
        self.fix_button.setEnabled(False)

    def refresh_theme(self) -> None:
        """主题切换后重新着色（保留当前问题列表）。"""
        self.set_issues(self._issues)

    def issue_at(self, index: int) -> Optional[Issue]:
        return self._issues[index] if 0 <= index < len(self._issues) else None

    def current_issue(self) -> Optional[Issue]:
        item = self.tree.currentItem()
        if item is None:
            return None
        return self.issue_at(self.tree.indexOfTopLevelItem(item))

    def _on_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        issue = self.issue_at(self.tree.indexOfTopLevelItem(item))
        if issue is not None:
            self.jump_requested.emit(issue.offset, issue.length)

    def _on_selected(self) -> None:
        issue = self.current_issue()
        if issue is None:
            self.detail.setText("")
            self.fix_button.setEnabled(False)
            return
        title, message, suggestion = issue_text(issue)
        parts = [title, message]
        if suggestion:
            parts.append(suggestion)
        extra = issue.details.get("suggestion")
        if extra and extra != suggestion:
            parts.append(extra)
        parts.append(tr("Line {line}, column {column}, offset {offset}").format(
            line=issue.line, column=issue.column, offset=issue.offset))
        self.detail.setText("\n".join(part for part in parts if part))
        self.fix_button.setEnabled(self._fix_for(issue) is not None)

    def _fix_for(self, issue: Issue):
        if self._source_getter is None:
            return None
        return propose_fix(self._source_getter(), issue)

    def _on_apply_fix(self) -> None:
        issue = self.current_issue()
        if issue is not None:
            self.apply_fix_requested.emit(issue)

    def retranslate(self) -> None:
        self.tree.setHeaderLabels([tr(name) for name in self.COLUMNS])
        self.fix_button.setText(tr("Apply suggested fix"))
        self.fix_all_button.setText(tr("Auto-fix all safe issues"))
        self.set_issues(self._issues)


class StatsPanel(QWidget):
    """文档级统计，数据来自解析器一遍扫描的计数器。"""

    ROWS = (
        ("Total nodes", "node_count"),
        ("Objects", "object_count"),
        ("Arrays", "array_count"),
        ("Strings", "string_count"),
        ("Numbers", "number_count"),
        ("Booleans", "boolean_count"),
        ("Nulls", "null_count"),
        ("Keys", "key_count"),
        ("Duplicate keys", "duplicate_key_count"),
        ("Max depth", "max_depth"),
        ("Max array length", "max_array_length"),
        ("Total string characters", "string_chars"),
    )

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._labels: List[tuple] = []
        self._last: Optional[tuple] = None

        grid = QFormLayout()
        grid.setContentsMargins(12, 12, 12, 12)
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(6)
        self._grid = grid

        for label_text, _attr in self.ROWS:
            key_label = QLabel(tr(label_text))
            value_label = QLabel("0")
            value_label.setObjectName("MonoLabel")
            value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            grid.addRow(key_label, value_label)
            self._labels.append((key_label, value_label, label_text))

        self.extra_labels: List[tuple] = []
        for label_text in ("Lines", "Characters"):
            key_label = QLabel(tr(label_text))
            value_label = QLabel("0")
            value_label.setObjectName("MonoLabel")
            value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            grid.addRow(key_label, value_label)
            self.extra_labels.append((key_label, value_label, label_text))

        container = QWidget()
        container.setLayout(grid)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        scroll.setFrameShape(QScrollArea.NoFrame)

        self.note = QLabel("")
        self.note.setObjectName("HintLabel")
        self.note.setWordWrap(True)
        self.note.setTextInteractionFlags(Qt.TextSelectableByMouse)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.note)

        register_retranslate(self)

    def set_stats(self, stats: ParseStats, lines: int, chars: int) -> None:
        self._last = (stats, lines, chars)
        for (_key_label, value_label, _attr_name), (_label_text, attr) in zip(self._labels, self.ROWS):
            value_label.setText(f"{getattr(stats, attr):,}")
        for (key_label, value_label, _label_text), value in zip(self.extra_labels, (lines, chars)):
            del key_label
            value_label.setText(f"{value:,}")

    def set_note(self, text: str) -> None:
        self.note.setText(text)

    def retranslate(self) -> None:
        for key_label, _value_label, label_text in self._labels + self.extra_labels:
            key_label.setText(tr(label_text))


class WorkspacePage(QWidget):
    """侧边栏"格式化 / 校验 / 压缩 / 树形查看"共用的工作区。"""

    status_message = Signal(str, int)
    validity_changed = Signal(object)
    metrics_changed = Signal(int, int, object)
    cursor_changed = Signal(int, int)
    document_changed = Signal(object)
    request_open_files = Signal(list)
    request_save_as = Signal()
    request_new_tab = Signal()
    request_close_tab = Signal(int)
    busy_changed = Signal(bool)
    history_event = Signal(str, str, str, object)
    undo_redo_changed = Signal(bool, bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.tabs = WorkspaceTabs(self)
        self.issues_panel = IssuesPanel(self)
        self.issues_panel.bind_source(self._current_text)
        self.stats_panel = StatsPanel(self)

        self.side_panel = QTabWidget(self)
        self.side_panel.setObjectName("SidePanel")
        self.tree_panel = TreePanel(self)
        self.side_panel.addTab(self.tree_panel, tr("Tree"))
        self.side_panel.addTab(self.issues_panel, tr("Issues"))
        self.side_panel.addTab(self.stats_panel, tr("Statistics"))
        self.side_panel.setMinimumWidth(320)

        self.splitter = QSplitter(Qt.Horizontal, self)
        self.splitter.addWidget(self._build_left())
        self.splitter.addWidget(self.side_panel)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setChildrenCollapsible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.splitter)

        self._format_task = None
        self._format_request_id = 0
        self._format_tab = None
        self._wire()
        register_retranslate(self)

    # ------------------------------------------------------------------ 构建
    def _build_left(self) -> QWidget:
        left = QWidget()
        layout = QVBoxLayout(left)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addWidget(self._build_action_bar())
        layout.addWidget(self._build_unwrap_bar())
        layout.addWidget(self.tabs, 1)
        return left

    def _build_unwrap_bar(self) -> QWidget:
        """检测到"字符串包裹的 JSON"时出现的提示条（解包是显式动作）。"""
        bar = QWidget()
        self.unwrap_bar = bar
        bar.setObjectName("UnwrapBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(10, 6, 8, 6)
        layout.setSpacing(8)
        self.unwrap_icon = QLabel()
        self.unwrap_icon.setPixmap(icon("alert", size=15, kind="warning").pixmap(15, 15))
        layout.addWidget(self.unwrap_icon)
        self.unwrap_label = QLabel("")
        self.unwrap_label.setWordWrap(True)
        layout.addWidget(self.unwrap_label, 1)
        self.unwrap_button = QPushButton(tr("Unwrap to JSON"))
        self.unwrap_button.setProperty("class", "primary")
        self.unwrap_button.setIcon(icon("wand", size=14, kind="on_accent"))
        self.unwrap_button.setCursor(Qt.PointingHandCursor)
        self.unwrap_button.clicked.connect(self.unwrap_document)
        layout.addWidget(self.unwrap_button)
        self.unwrap_hide = QToolButton()
        self.unwrap_hide.setIcon(icon("close", size=13, kind="muted"))
        self.unwrap_hide.setAutoRaise(True)
        self.unwrap_hide.setCursor(Qt.PointingHandCursor)
        self.unwrap_hide.setToolTip(tr("Dismiss"))
        self.unwrap_hide.clicked.connect(self._dismiss_unwrap)
        layout.addWidget(self.unwrap_hide)
        bar.hide()
        self._unwrap_dismissed_hash = None
        return bar

    def _build_action_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("ActionBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.format_button = self._action_button("format", "Format", "primary", self.format_document)
        self.minify_button = self._action_button("compress", "Minify", "", self.minify_document)
        self.validate_button = self._action_button("check-circle", "Validate", "", self.validate_now)
        self.sort_button = self._action_button("sort", "Sort keys", "", self.open_sort_dialog)
        self.copy_button = self._action_button("copy", "Copy result", "", self.copy_result)
        self.export_button = self._action_button("download", "Export", "",
                                                 lambda: self.request_save_as.emit())

        for button in (self.format_button, self.minify_button, self.validate_button,
                       self.sort_button):
            layout.addWidget(button)
        layout.addWidget(self._separator())
        layout.addWidget(self.copy_button)
        layout.addWidget(self.export_button)
        layout.addSpacing(10)

        self.indent_label = self._label("Indent:")
        layout.addWidget(self.indent_label)
        self.indent_combo = QComboBox()
        for key, _value in INDENT_ITEMS:
            self.indent_combo.addItem(self._indent_label(key), key)
        self.indent_combo.setCurrentIndex(1)
        self.indent_combo.currentIndexChanged.connect(self._on_indent_changed)
        layout.addWidget(self.indent_combo)

        self.type_label = self._label("Type:")
        layout.addWidget(self.type_label)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("JSON", MODE_JSON)
        self.mode_combo.addItem("JSONC", MODE_JSONC)
        self.mode_combo.setToolTip(tr("JSONC allows comments and trailing commas"))
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        layout.addWidget(self.mode_combo)

        self.panel_button = QToolButton()
        self.panel_button.setIcon(icon("split", size=16, kind="muted"))
        self.panel_button.setToolTip(tr("Toggle side panel"))
        self.panel_button.setCheckable(True)
        self.panel_button.setChecked(True)
        self.panel_button.setAutoRaise(True)
        self.panel_button.setCursor(Qt.PointingHandCursor)
        self.panel_button.toggled.connect(self.set_panel_visible)

        layout.addStretch(1)
        layout.addWidget(self.panel_button)
        return bar

    def _action_button(self, icon_name: str, label: str, kind: str, slot) -> QPushButton:
        button = QPushButton(tr(label))
        button.setIcon(icon(icon_name, size=15, kind="accent" if kind == "primary" else "muted"))
        if kind:
            button.setProperty("class", kind)
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(slot)
        return button

    def _separator(self) -> QWidget:
        line = QWidget()
        line.setFixedWidth(1)
        line.setMinimumHeight(20)
        line.setStyleSheet("background-color: rgba(128,128,128,90);")
        return line

    def _label(self, text: str) -> QLabel:
        label = QLabel(tr(text))
        label.setObjectName("HintLabel")
        return label

    def _indent_label(self, key: str) -> str:
        return {
            "2": tr("2 spaces"),
            "4": tr("4 spaces"),
            "8": tr("8 spaces"),
            "tab": tr("Tab"),
            "none": tr("None (single line)"),
        }[key]

    # ------------------------------------------------------------------ 连接
    def _wire(self) -> None:
        self.tabs.new_tab_requested.connect(self.request_new_tab)
        self.tabs.close_tab_requested.connect(self.request_close_tab)
        self.tabs.open_files_requested.connect(self.request_open_files)
        self.tabs.current_editor_changed.connect(self._on_current_tab_changed)
        self.issues_panel.jump_requested.connect(self._on_jump)
        self.issues_panel.apply_fix_requested.connect(self.apply_issue_fix)
        self.issues_panel.apply_all_requested.connect(self.auto_fix_document)
        self._unwrap_info = None
        self.tree_panel.jump_requested.connect(self._on_jump)
        self.tree_panel.value_changed.connect(self._on_tree_value_changed)

    def _on_current_tab_changed(self, tab) -> None:
        self.document_changed.emit(tab)
        if tab is None:
            self.issues_panel.set_issues([])
            self.stats_panel.set_stats(ParseStats(), 0, 0)
            self.validity_changed.emit(None)
            return
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(tab.document.mode)))
        self.mode_combo.blockSignals(False)
        self.tree_panel.clear_document()
        if len(tab.text()) > ASYNC_VALIDATE_CHARS:
            self.issues_panel.set_issues([])
            tab.request_validation()
        else:
            self._on_validation_ready(tab.validation(force=False))

    def _current_text(self) -> str:
        tab = self.tabs.current_editor()
        return tab.text() if tab else ""

    # ------------------------------------------------------------------ 标签
    def add_tab(self, text: str = "", *, path: str = "", activate: bool = True) -> EditorTab:
        tab = EditorTab(self.tabs)
        self.tabs.add_editor(tab, activate=activate)
        tab.apply_settings()
        tab.editor.set_indent(self.current_indent(), max(2, settings.value("editor/tab_size", 4)))
        tab.apply_theme(theme.palette(), settings.font_family)
        # 信号只在这里连接一次，切换标签时不要重复连接
        tab.validation_ready.connect(
            lambda result: self._on_validation_ready(result) if self.current_tab() is tab else None)
        tab.validation_failed.connect(
            lambda message: self._on_validation_failed(message) if self.current_tab() is tab else None)
        tab.cursor_moved.connect(lambda line, column:
            self.cursor_changed.emit(line, column) if self.current_tab() is tab else None)
        tab.busy_changed.connect(lambda busy:
            self.busy_changed.emit(busy) if self.current_tab() is tab else None)
        tab.progress_reported.connect(lambda done, total:
            self.progress_reported.emit(done, total) if self.current_tab() is tab else None)
        tab.modified_changed.connect(lambda _modified:
            self._refresh_tab_metrics(tab) if self.current_tab() is tab else None)
        tab.text_changed.connect(self._reset_unwrap_dismissal)
        tab.text_changed.connect(lambda: self._clear_stale_views(tab))
        document = tab.editor.document()
        document.undoAvailable.connect(
            lambda available: self.undo_redo_changed.emit(available, document.isRedoAvailable()))
        document.redoAvailable.connect(
            lambda available: self.undo_redo_changed.emit(document.isUndoAvailable(), available))
        if path:
            tab.open_path(path)
        elif text:
            tab.set_text(text, mark_modified=False)
        self.document_changed.emit(tab)
        self._refresh_tab_metrics(tab)
        tab.request_validation()
        return tab

    def _clear_stale_views(self, tab: EditorTab) -> None:
        if tab is self.current_tab():
            self.tree_panel.clear_document()
            self.issues_panel.set_issues([])
            self.unwrap_bar.hide()

    def current_tab(self) -> Optional[EditorTab]:
        return self.tabs.current_editor()

    def close_tab_at(self, index: int) -> Optional[EditorTab]:
        tab = self.tabs.widget(index)
        if isinstance(tab, EditorTab):
            tab.cancel_validation()
        if tab is self._format_tab:
            self.cancel_format()
        return self.tabs.close_tab(index)

    # ------------------------------------------------------------------ 格式化
    def current_indent(self) -> str:
        return dict(INDENT_ITEMS).get(self.indent_combo.currentData(), INDENT_4)

    def build_options(self, **overrides) -> FormatOptions:
        ending = settings.value("format/line_ending", "system")
        line_ending = "\r\n" if ending == "crlf" else "\n"
        if ending == "system":
            tab = self.tabs.current_editor()
            if tab is not None:
                line_ending = tab.document.line_ending
        options = FormatOptions(
            indent=self.current_indent(),
            sort_keys=bool(settings.value("format/sort_keys", False)),
            sort_mode=str(settings.value("format/sort_mode", SORT_ALPHA)),
            sort_arrays=bool(settings.value("format/sort_arrays", False)),
            ensure_ascii=bool(settings.value("format/ensure_ascii", False)),
            trailing_newline=bool(settings.value("format/trailing_newline", False)),
            line_ending=line_ending,
            max_depth=settings.max_depth,
        )
        for key, value in overrides.items():
            setattr(options, key, value)
        return options

    def format_document(self) -> None:
        self._run_format(format_text, self.build_options(), tr("Formatted"))

    def minify_document(self) -> None:
        self._run_format(minify_text, self.build_options(), tr("Minified"))

    def _run_format(self, func, options: FormatOptions, success_label: str) -> None:
        tab = self.tabs.current_editor()
        if tab is None:
            self.status_message.emit(tr("No document is open."), 4000)
            return
        text = tab.text()
        if not text.strip():
            self.status_message.emit(tr("Nothing to format: the document is empty."), 4000)
            return
        mode = tab.document.mode
        self.cancel_format()
        request_id = self._format_request_id
        revision = tab.editor.document().revision()
        if len(text) > ASYNC_VALIDATE_CHARS:
            self._format_tab = tab
            self.busy_changed.emit(True)
            self._format_task = start_task(
                _format_worker, func, text, options, mode,
                on_done=lambda result: self._finish_format(
                    result, tab, success_label, request_id, revision, mode),
                on_error=lambda message: self._format_error(message, request_id),
                on_cancelled=lambda: self._format_error("", request_id),
                on_progress=lambda done, total: self.progress_reported.emit(done, total)
                    if request_id == self._format_request_id else None,
            )
            return
        self._apply_format(func(text, options, mode), tab, success_label, False)

    progress_reported = Signal(int, int)

    def cancel_format(self) -> None:
        self._format_request_id += 1
        if self._format_task is not None:
            self._format_task.cancel()
            self._format_task = None
            self.busy_changed.emit(False)
        self._format_tab = None

    def _format_error(self, message: str, request_id: int) -> None:
        if request_id == self._format_request_id:
            self._format_failed(message)

    def _finish_format(self, result, tab, label, request_id, revision, mode) -> None:
        if request_id != self._format_request_id:
            return
        self._format_task = None
        self._format_tab = None
        self.busy_changed.emit(False)
        if self.tabs.indexOf(tab) < 0:
            return
        if tab.editor.document().revision() != revision or tab.document.mode != mode:
            self.status_message.emit(tr("Document changed; the formatting result was discarded."), 6000)
            return
        self._apply_format(result, tab, label, False)

    def _apply_format(self, result, tab: EditorTab, success_label: str, async_done: bool) -> None:
        if async_done:
            self.busy_changed.emit(False)
            self._format_task = None
        if result is None:
            return
        if not result.ok:
            parse = result.parse
            if result.error_message == "duplicate_key":
                self.status_message.emit(tr("Resolve duplicate keys before formatting to avoid data loss."), 8000)
                if tab is self.current_tab():
                    self.validate_now()
                return
            if parse is not None and parse.cancelled:
                self.status_message.emit(tr("Cancelled."), 3000)
                return
            if tab is self.current_tab():
                self.validate_now()
            issue = parse.error if parse else None
            if issue is not None:
                title, message, suggestion = issue_text(issue)
                detail = f"{title}: {message}"
                if suggestion:
                    detail += f" — {suggestion}"
                self.status_message.emit(detail, 8000)
            else:
                self.status_message.emit(tr("Cannot format: the document is not valid JSON."), 6000)
            return
        if not result.semantics_preserved:
            # 语义一致性没通过：宁可不动用户的数据
            dialogs.critical(
                self, tr("Format aborted"),
                tr("The formatted output would not match the original data, so nothing was "
                   "changed. This should not happen — please report it."))
            return
        if result.parse.stats.comment_count and not dialogs.confirm(
                self, tr("Remove comments?"),
                tr("This operation removes JSONC comments. Continue? You can undo with Ctrl+Z."),
                default=False):
            return
        tab.set_text(result.output, keep_undo=True)
        tab.request_validation()
        if tab is self.current_tab():
            self._refresh_tab_metrics(tab)
        self.history_event.emit(
            "format", success_label,
            f"{result.parse.text_length} -> {len(result.output)} chars", tab)
        self.status_message.emit(tr("{label}: {before} → {after} characters").format(
            label=success_label, before=result.parse.text_length, after=len(result.output)), 5000)

    def _format_failed(self, message: str) -> None:
        self.busy_changed.emit(False)
        self._format_task = None
        self._format_tab = None
        if message:
            self.status_message.emit(
                tr("Formatting failed: {message}").format(message=message.splitlines()[0]), 8000)
        else:
            self.status_message.emit(tr("Cancelled."), 3000)

    def open_sort_dialog(self) -> None:
        if self.tabs.current_editor() is None:
            self.status_message.emit(tr("No document is open."), 4000)
            return
        dialog = SortDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._run_format(format_text, self.build_options(**dialog.options()), tr("Sorted"))

    # ------------------------------------------------------------------ 校验
    def validate_now(self) -> Optional[ValidationResult]:
        tab = self.tabs.current_editor()
        if tab is None:
            return None
        if len(tab.text()) > ASYNC_VALIDATE_CHARS:
            tab._run_validation()
            return None
        result = tab.validation(force=True)
        self._on_validation_ready(result)
        if result.errors:
            self.side_panel.setCurrentWidget(self.issues_panel)
            issue = result.errors[0]
            tab.editor.goto_offset(issue.offset, issue.length)
        return result

    def _on_validation_ready(self, result) -> None:
        if not isinstance(result, ValidationResult):
            return
        if result.cancelled:
            self.validity_changed.emit(None)
            return
        self.issues_panel.set_issues(list(result.issues))
        self._update_unwrap_bar(result)
        if result.valid and result.parse is not None:
            self.tree_panel.set_document(result.value, result.parse.offsets)
            self.tree_panel.set_editable(not (result.stats.duplicate_key_count or result.stats.comment_count))
        else:
            self.tree_panel.clear_document()
        tab = self.tabs.current_editor()
        if tab is not None:
            self._refresh_tab_metrics(tab, result.stats)
        self.validity_changed.emit(result)

    def _on_tree_value_changed(self) -> None:
        """树里改了值：把整棵树回写成文本（保留撤销栈），再重新校验。"""
        tab = self.tabs.current_editor()
        if tab is None or not self.tree_panel.model.has_document():
            return
        text = dumps(self.tree_panel.model.root_value(), self.build_options())
        tab.set_text(text, keep_undo=True)
        tab.request_validation()
        self._refresh_tab_metrics(tab)

    def _on_validation_failed(self, message: str) -> None:
        self.status_message.emit(tr("Validation error: {message}").format(
            message=message.splitlines()[0]), 8000)

    def _refresh_tab_metrics(self, tab: EditorTab, stats: Optional[ParseStats] = None) -> None:
        text = tab.text()
        stats = stats if stats is not None else tab.stats()
        lines = text.count("\n") + 1
        self.metrics_changed.emit(lines, len(text), stats)
        self.stats_panel.set_stats(stats, lines, len(text))

    def _update_unwrap_bar(self, result: ValidationResult) -> None:
        tab = self.tabs.current_editor()
        if tab is None or not result.valid or not isinstance(result.value, str):
            self.unwrap_bar.hide()
            self._unwrap_info = None
            return
        info = unwrap(tab.text(), tab.document.mode)
        self._unwrap_info = info
        if not info.wrapped:
            self.unwrap_bar.hide()
            return
        if self._unwrap_dismissed_hash == hash(tab.text()):
            self.unwrap_bar.hide()
            return
        self.unwrap_label.setText(tr(
            "This document is a JSON string wrapping {n} more layer(s) of JSON "
            "(double-encoded). Unwrap it to edit the real structure.").format(n=info.layers))
        self.unwrap_bar.show()

    def _reset_unwrap_dismissal(self) -> None:
        self._unwrap_dismissed_hash = None

    def _dismiss_unwrap(self) -> None:
        tab = self.tabs.current_editor()
        if tab is not None:
            self._unwrap_dismissed_hash = hash(tab.text())
        self.unwrap_bar.hide()

    def unwrap_document(self) -> None:
        tab = self.tabs.current_editor()
        if tab is None:
            return
        info = getattr(self, "_unwrap_info", None) or unwrap(tab.text(), tab.document.mode)
        if not info.wrapped:
            self.status_message.emit(tr("This document is not a wrapped JSON string."), 4000)
            return
        inner = _parse_for_unwrap(info.text, tab.document.mode,
                                  max_depth=settings.max_depth)
        if not inner.ok:
            self.status_message.emit(tr("Cannot unwrap: the inner text is not valid JSON."), 6000)
            return
        tab.set_text(dumps(inner.value, self.build_options()), keep_undo=True)
        self._unwrap_dismissed_hash = None
        self.unwrap_bar.hide()
        tab.request_validation()
        self._refresh_tab_metrics(tab)
        self.status_message.emit(tr("Unwrapped {n} string layer(s); you can undo with Ctrl+Z.")
                                 .format(n=info.layers), 6000)

    def _on_jump(self, offset: int, length: int) -> None:
        tab = self.tabs.current_editor()
        if tab is not None:
            tab.editor.goto_offset(offset, length)

    # ------------------------------------------------------------------ 修复
    def apply_issue_fix(self, issue: Issue) -> None:
        tab = self.tabs.current_editor()
        if tab is None:
            return
        text = tab.text()
        fix = propose_fix(text, issue)
        if fix is None:
            self.status_message.emit(tr("No safe automatic fix for this issue."), 5000)
            return
        title, message, suggestion = issue_text(issue)
        body = tr("Issue: {title}\n{message}\n\nAction: {label}").format(
            title=title, message=message, label=fix.label)
        if suggestion:
            body += f"\n{suggestion}"
        body += tr("\n\nThe document will be modified. You can undo it with Ctrl+Z.")
        if not dialogs.confirm(self, tr("Apply fix?"), body, default=False):
            return
        tab.set_text(apply_fix(text, fix), keep_undo=True)
        tab.request_validation()
        self.status_message.emit(tr("Fix applied."), 5000)

    def auto_fix_document(self) -> None:
        tab = self.tabs.current_editor()
        if tab is None:
            return
        result = auto_fix(tab.text(), tab.document.mode, max_depth=settings.max_depth)
        if result.unchanged:
            self.status_message.emit(
                tr("The document is already valid.") if result.ok
                else tr("No safe automatic fixes available."), 5000)
            return
        listed = "\n".join(f"• {fix.label}" for fix in result.applied[:12])
        if len(result.applied) > 12:
            listed += tr("\n… and {n} more").format(n=len(result.applied) - 12)
        body = tr("These changes will be applied:\n\n{listed}\n\n"
                  "You can undo them afterwards with Ctrl+Z.").format(listed=listed)
        if not dialogs.confirm(self, tr("Apply {n} fixes?").format(n=len(result.applied)),
                               body, default=False):
            return
        tab.set_text(result.text, keep_undo=True)
        tab.request_validation()
        if result.ok:
            self.status_message.emit(tr("All safe issues fixed; the document is now valid JSON."), 6000)
        else:
            title, _message, _suggestion = issue_text(result.remaining)
            self.status_message.emit(tr("Applied {n} fix(es); still invalid: {title}").format(
                n=len(result.applied), title=title), 8000)

    # ------------------------------------------------------------------ 其它动作
    def copy_result(self) -> None:
        text = self._current_text()
        if not text:
            self.status_message.emit(tr("Nothing to copy."), 3000)
            return
        QGuiApplication.clipboard().setText(text)
        self.status_message.emit(
            tr("Copied {n} characters to the clipboard.").format(n=len(text)), 3000)

    def set_panel_visible(self, visible: bool) -> None:
        self.side_panel.setVisible(visible)
        if self.panel_button.isChecked() != visible:
            self.panel_button.blockSignals(True)
            self.panel_button.setChecked(visible)
            self.panel_button.blockSignals(False)

    def panel_visible(self) -> bool:
        return self.side_panel.isVisible()

    def show_side_tab(self, index: int) -> None:
        self.set_panel_visible(True)
        self.side_panel.setCurrentIndex(index)

    # ------------------------------------------------------------------ 设置/主题/翻译
    def apply_settings(self) -> None:
        index = self.indent_combo.findData(settings.indent_choice)
        if index >= 0:
            self.indent_combo.blockSignals(True)
            self.indent_combo.setCurrentIndex(index)
            self.indent_combo.blockSignals(False)
        for tab in self.tabs.editors():
            tab.apply_settings()
        state = settings.splitter_state("workspace")
        if state is not None:
            self.splitter.restoreState(state)

    def save_state(self) -> None:
        settings.save_splitter("workspace", self.splitter.saveState())

    def apply_theme(self) -> None:
        palette = theme.palette()
        for tab in self.tabs.editors():
            tab.apply_theme(palette, settings.font_family)
        self.issues_panel.refresh_theme()
        self.tree_panel.apply_theme()

    def _on_indent_changed(self, _index: int) -> None:
        settings.indent_choice = self.indent_combo.currentData()
        indent = self.current_indent()
        for tab in self.tabs.editors():
            tab.editor.set_indent(indent, max(2, settings.value("editor/tab_size", 4)))
        settings.sync()

    def _on_mode_changed(self, _index: int) -> None:
        tab = self.tabs.current_editor()
        if tab is None:
            return
        tab.document.set_mode(self.mode_combo.currentData())
        self._clear_stale_views(tab)
        tab.request_validation()

    def retranslate(self) -> None:
        self.format_button.setText(tr("Format"))
        self.minify_button.setText(tr("Minify"))
        self.validate_button.setText(tr("Validate"))
        self.sort_button.setText(tr("Sort keys"))
        self.copy_button.setText(tr("Copy result"))
        self.export_button.setText(tr("Export"))
        self.indent_label.setText(tr("Indent:"))
        self.type_label.setText(tr("Type:"))
        self.mode_combo.setToolTip(tr("JSONC allows comments and trailing commas"))
        self.panel_button.setToolTip(tr("Toggle side panel"))
        self.unwrap_button.setText(tr("Unwrap to JSON"))
        self.unwrap_hide.setToolTip(tr("Dismiss"))
        info = getattr(self, "_unwrap_info", None)
        if info is not None and info.wrapped and self.unwrap_bar.isVisible():
            self.unwrap_label.setText(tr(
                "This document is a JSON string wrapping {n} more layer(s) of JSON "
                "(double-encoded). Unwrap it to edit the real structure.").format(n=info.layers))
        self.side_panel.setTabText(self.side_panel.indexOf(self.issues_panel), tr("Issues"))
        self.side_panel.setTabText(self.side_panel.indexOf(self.stats_panel), tr("Statistics"))
        self.side_panel.setTabText(self.side_panel.indexOf(self.tree_panel), tr("Tree"))
        for index, (key, _value) in enumerate(INDENT_ITEMS):
            self.indent_combo.setItemText(index, self._indent_label(key))


def _format_worker(ctx, func, text: str, options: FormatOptions, mode: str):
    """后台执行格式化：进度与取消透传给解析器（解析是主要耗时阶段）。"""
    return func(text, options, mode, verify=True,
                progress=lambda done, total: ctx.progress(done, total),
                cancel=lambda: ctx.cancelled)

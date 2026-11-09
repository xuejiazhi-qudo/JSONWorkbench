"""多标签编辑工作区。

: class:`EditorTab` = 一个文档（编辑器 + 查找条 + 元数据 + 校验状态）；
:class:`WorkspaceTabs` = 标签容器（可关闭、可拖动、中键关闭、拖文件打开）。
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QInputDialog,
    QPlainTextEdit,
    QTabBar,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.core.jsonfilemanager import (
    FileReadResult,
    WriteResult,
    format_size,
    read_text_file,
    write_text_file,
)
from app.core.jsonformatter import indent_from_choice
from app.core.jsonparser import ParseStats
from app.core.jsonvalidator import ValidationResult, validate_text
from app.editor.findreplace import FindReplaceBar
from app.editor.jsoneditor import JsonEditor
from app.i18n import register_retranslate, tr
from app.settings.settings import settings
from app.utils.worker import start_task
from app.widgets.document import Document
from app.widgets.icons import icon

#: 超过该字符数改用后台线程校验，避免界面卡住
ASYNC_VALIDATE_CHARS = 800_000
#: 文本变化后延迟多久触发校验（连续输入时不要每个字符都全量解析）
VALIDATE_DEBOUNCE_MS = 350


def _validate_worker(ctx, text: str, mode: str, max_depth: int, duplicates: bool):
    return validate_text(
        text, mode, max_depth=max_depth, detect_duplicate_keys=duplicates,
        track_offsets=True,
        progress=lambda done, total: ctx.progress(done, total),
        cancel=lambda: ctx.cancelled,
    )


class EditorTab(QWidget):
    """一个 JSON 文档的编辑视图。"""

    text_changed = Signal()
    modified_changed = Signal(bool)
    validation_ready = Signal(object)
    validation_failed = Signal(str)
    cursor_moved = Signal(int, int)
    stats_changed = Signal(object)
    open_files_requested = Signal(list)
    busy_changed = Signal(bool)
    progress_reported = Signal(int, int)

    def __init__(self, parent: Optional[QWidget] = None, *, read_only: bool = False) -> None:
        super().__init__(parent)
        self.document = Document()
        self.editor = JsonEditor(self, read_only=read_only)
        self.find_bar = FindReplaceBar(self.editor, self.editor)
        self.editor.attach_find_bar(self.find_bar)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.editor)

        self._validation: Optional[ValidationResult] = None
        self._validated_text: Optional[str] = None
        self._request_id = 0
        self._task = None
        self._loading = False

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(VALIDATE_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._run_validation)

        self.editor.textChanged.connect(self._on_text_changed)
        self.editor.cursor_moved.connect(self.cursor_moved)
        self.editor.find_requested.connect(lambda: self.show_find(False))
        self.editor.replace_requested.connect(lambda: self.show_find(True))
        self.editor.goto_line_requested.connect(self.prompt_goto_line)
        self.editor.dropped_files.connect(self.open_files_requested)
        self.document.modified_changed.connect(self.modified_changed)

        self.apply_settings()
        register_retranslate(self)

    # ------------------------------------------------------------------ 文本
    def text(self) -> str:
        return self.editor.toPlainText()

    def set_text(self, text: str, *, mark_modified: bool = True, keep_undo: bool = False) -> None:
        """载入文本。``mark_modified=False`` 用于打开文件/保存后的重置。"""
        self._loading = True
        try:
            self.editor.set_content(text, keep_undo=keep_undo)
        finally:
            self._loading = False
        self.document.set_modified(bool(mark_modified) and not self.editor.isReadOnly())
        self._invalidate_validation()
        self.text_changed.emit()

    def is_empty(self) -> bool:
        return not self.text().strip()

    def _on_text_changed(self) -> None:
        if not self._loading:
            self.document.set_modified(not self.editor.isReadOnly())
            self.text_changed.emit()
        self._invalidate_validation()
        self._debounce.start()

    def title(self) -> str:
        return self.document.display_name

    def apply_settings(self) -> None:
        self.editor.set_font_size(settings.font_size)
        self.editor.set_indent(indent_from_choice(settings.indent_choice),
                               max(2, settings.value("editor/tab_size", 4)))
        self.editor.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth if settings.word_wrap
            else QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.set_auto_close_brackets(settings.auto_close_brackets)
        self.editor.set_auto_indent(settings.auto_indent)
        self.editor.set_line_numbers_visible(settings.line_numbers)
        self._apply_highlight_budget()

    def _apply_highlight_budget(self) -> None:
        limit = settings.highlight_limit_bytes
        size = len(self.text().encode("utf-8", "ignore"))
        self.editor.set_highlighting_enabled(size <= limit)

    def apply_theme(self, palette, family: str = "") -> None:
        self.editor.apply_theme(palette, family)

    def retranslate(self) -> None:
        self.editor.setToolTip(self.document.tooltip)

    # ------------------------------------------------------------------ 校验
    def _invalidate_validation(self) -> None:
        self.cancel_validation()
        self._validation = None
        self._validated_text = None

    def validation(self, *, force: bool = False) -> ValidationResult:
        """同步返回校验结果（带缓存）。"""
        text = self.text()
        if not force and self._validation is not None and self._validated_text == text:
            return self._validation
        self.cancel_validation()
        result = validate_text(text, self.document.mode, max_depth=settings.max_depth,
                               detect_duplicate_keys=settings.detect_duplicate_keys,
                               track_offsets=True)
        self._validation = result
        self._validated_text = text
        self._publish(result)
        return result

    def request_validation(self) -> None:
        """触发一次校验（大文档走后台线程）。"""
        self._invalidate_validation()
        self._debounce.start()

    def cancel_validation(self) -> None:
        self._request_id += 1
        self._debounce.stop()
        if self._task is not None:
            self._task.cancel()
            self._task = None
            self.busy_changed.emit(False)

    def _run_validation(self) -> None:
        text = self.text()
        mode = self.document.mode
        self.cancel_validation()
        request_id = self._request_id
        if len(text) > ASYNC_VALIDATE_CHARS:
            self.busy_changed.emit(True)
            self._task = start_task(
                _validate_worker, text, mode, settings.max_depth,
                settings.detect_duplicate_keys,
                on_done=lambda result: self._on_async_result(result, request_id),
                on_error=lambda message: self._on_async_error(message, request_id),
                on_cancelled=lambda: self._on_async_result(None, request_id),
                on_progress=lambda done, total: self._on_async_progress(done, total, request_id),
            )
            return
        result = validate_text(text, mode, max_depth=settings.max_depth,
                               detect_duplicate_keys=settings.detect_duplicate_keys,
                               track_offsets=True)
        self._on_async_result(result, request_id)

    def _on_async_progress(self, done: int, total: int, request_id: int) -> None:
        if request_id == self._request_id:
            self.progress_reported.emit(done, total)

    def _on_async_result(self, result, request_id: int) -> None:
        if request_id != self._request_id:
            return  # 已经有更新的请求，丢弃过期结果
        self._task = None
        self.busy_changed.emit(False)
        if result is None:
            return
        self._validation = result
        self._validated_text = self.text()
        self._publish(result)

    def _on_async_error(self, message: str, request_id: int) -> None:
        if request_id != self._request_id:
            return
        self._task = None
        self.busy_changed.emit(False)
        self._publish_error(message)

    def _publish(self, result: ValidationResult) -> None:
        if result.cancelled:
            self.editor.clear_issues()
            self.stats_changed.emit(ParseStats())
            self.validation_ready.emit(result)
            return
        visible = [issue for issue in result.issues if issue.code != "offsets_disabled"]
        self.editor.set_issues(visible)
        self.stats_changed.emit(result.stats)
        self._apply_highlight_budget()
        self.validation_ready.emit(result)

    def _publish_error(self, message: str) -> None:
        self.editor.clear_issues()
        self.validation_failed.emit(message)

    def is_valid(self) -> Optional[bool]:
        if self._validation is None:
            return None
        return bool(self._validation.valid)

    def stats(self) -> ParseStats:
        return self._validation.stats if self._validation else ParseStats()

    # ------------------------------------------------------------------ 查找/跳转
    def show_find(self, replace: bool = False) -> None:
        selected = self.editor.textCursor().selectedText()
        initial = selected if (selected and "\n" not in selected and len(selected) < 128) else ""
        self.find_bar.show_bar(replace=replace, initial=initial)

    def prompt_goto_line(self) -> None:
        line, accepted = QInputDialog.getInt(
            self, tr("Go to line"), tr("Line number (1 - {max}):").format(max=self.editor.blockCount()),
            self.editor.current_line(), 1, max(1, self.editor.blockCount()))
        if accepted:
            self.editor.goto_line(line)

    # ------------------------------------------------------------------ 文件
    def load_read_result(self, result: FileReadResult) -> bool:
        if not result.ok:
            return False
        self.document.apply_file_metadata(result)
        self.set_text(result.text, mark_modified=False)
        self.document.set_modified(False)
        self._apply_highlight_budget()
        return True

    def open_path(self, path: str) -> FileReadResult:
        result = read_text_file(path)
        self.load_read_result(result)
        return result

    def save_path(self, path: Optional[str] = None, *, encoding: Optional[str] = None,
                  with_bom: Optional[bool] = None,
                  line_ending: Optional[str] = None) -> WriteResult:
        target = path or self.document.path
        if not target:
            return WriteResult(ok=False, error="no path")
        bom = self.document.had_bom if with_bom is None else with_bom
        ending = line_ending or self.document.line_ending
        result = write_text_file(target, self.text(),
                                 encoding=encoding or self.document.encoding,
                                 with_bom=bom, line_ending=ending)
        if result.ok:
            self.document.mark_saved(result.path, encoding=encoding or self.document.encoding,
                                     had_bom=bom, line_ending=ending)
            self.document.set_modified(False)
        return result

    def file_size_text(self) -> str:
        return format_size(len(self.text().encode("utf-8", "ignore")))


class WorkspaceTabs(QTabWidget):
    """文档标签容器。"""

    new_tab_requested = Signal()
    close_tab_requested = Signal(int)
    open_files_requested = Signal(list)
    current_editor_changed = Signal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # 原生关闭按钮在被 QSS 美化过的 QTabBar 上不会绘制图标，
        # 因此改用每个标签自己的 QToolButton（见 _install_close_button）
        self.setTabsClosable(False)
        self.setMovable(True)
        self.setDocumentMode(True)
        self.setAcceptDrops(True)

        self.new_button = QToolButton()
        self.new_button.setIcon(icon("plus", size=15, kind="muted"))
        self.new_button.setToolTip(tr("New tab (Ctrl+N)"))
        self.new_button.setAutoRaise(True)
        self.new_button.setCursor(Qt.PointingHandCursor)
        self.new_button.clicked.connect(self.new_tab_requested)
        self.setCornerWidget(self.new_button, Qt.TopRightCorner)

        self.tabBar().installEventFilter(self)
        self.currentChanged.connect(lambda _index: self.current_editor_changed.emit(self.current_editor()))
        register_retranslate(self)

    # ------------------------------------------------------------------ 标签管理
    def add_editor(self, tab: Optional[EditorTab] = None, *, activate: bool = True) -> EditorTab:
        editor_tab = tab or EditorTab(self)
        index = self.addTab(editor_tab, editor_tab.title())
        self._install_close_button(index, editor_tab)
        self.setTabToolTip(index, editor_tab.document.tooltip)
        editor_tab.modified_changed.connect(lambda _m, t=editor_tab: self.refresh_title(t))
        editor_tab.open_files_requested.connect(self.open_files_requested)
        if activate:
            self.setCurrentWidget(editor_tab)
        return editor_tab

    def _install_close_button(self, index: int, tab: EditorTab) -> None:
        button = QToolButton(self.tabBar())
        button.setObjectName("TabCloseButton")
        button.setAutoRaise(True)
        button.setCursor(Qt.PointingHandCursor)
        button.setIcon(icon("close", size=11, kind="muted"))
        button.setToolTip(tr("Close tab"))
        button.clicked.connect(
            lambda _=False, target=tab: self.close_tab_requested.emit(self.indexOf(target)))
        self.tabBar().setTabButton(index, QTabBar.ButtonPosition.RightSide, button)

    def refresh_tab_buttons(self) -> None:
        """主题/语言切换后刷新每个标签关闭按钮的图标与提示。"""
        bar = self.tabBar()
        for index in range(self.count()):
            widget = bar.tabButton(index, QTabBar.ButtonPosition.RightSide)
            if isinstance(widget, QToolButton):
                widget.setIcon(icon("close", size=11, kind="muted"))
                widget.setToolTip(tr("Close tab"))

    def current_editor(self) -> Optional[EditorTab]:
        widget = self.currentWidget()
        return widget if isinstance(widget, EditorTab) else None

    def editors(self) -> List[EditorTab]:
        return [self.widget(i) for i in range(self.count())
                if isinstance(self.widget(i), EditorTab)]

    def refresh_title(self, tab: EditorTab) -> None:
        index = self.indexOf(tab)
        if index >= 0:
            self.setTabText(index, tab.title())
            self.setTabToolTip(index, tab.document.tooltip)

    def refresh_all_titles(self) -> None:
        for tab in self.editors():
            self.refresh_title(tab)

    def retranslate(self) -> None:
        self.new_button.setToolTip(tr("New tab (Ctrl+N)"))
        self.refresh_tab_buttons()

    # ------------------------------------------------------------------ 交互
    def eventFilter(self, watched, event) -> bool:  # noqa: N802 (Qt 命名)
        if watched is self.tabBar() and event.type() == QEvent.MouseButtonRelease:
            if event.button() == Qt.MiddleButton:
                index = self.tabBar().tabAt(event.position().toPoint())
                if index >= 0:
                    self.close_tab_requested.emit(index)
                    return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if event.modifiers() & Qt.ControlModifier and event.key() == Qt.Key_W:
            self.close_tab_requested.emit(self.currentIndex())
            event.accept()
            return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if event.mimeData().hasUrls():
            paths = [url.toLocalFile() for url in event.mimeData().urls() if url.toLocalFile()]
            if paths:
                self.open_files_requested.emit(paths)
                event.acceptProposedAction()
                return
        super().dropEvent(event)

    def close_tab(self, index: int) -> Optional[EditorTab]:
        if index < 0 or index >= self.count():
            return None
        widget = self.widget(index)
        self.removeTab(index)
        return widget if isinstance(widget, EditorTab) else None

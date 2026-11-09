"""主窗口：顶部品牌区 + 菜单、左侧导航、中央页面栈、底部状态栏。

所有 QAction 以"英文标签"为 key 登记在 :attr:`MainWindow._actions` 里，
主题切换时按 key 重新着色，切换语言时按 key 重新翻译。
"""

from __future__ import annotations

import sys
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QFileInfo, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app import APP_DOMAIN, APP_NAME, APP_ORG, APP_VERSION
from app.core.jsonfilemanager import (
    SUPPORTED_EXTENSIONS,
    file_dialog_filter,
    format_size,
    read_text_file,
)
from app.core.jsonvalidator import ValidationResult
from app.i18n import (
    LANG_EN,
    LANG_ZH,
    current_language,
    issue_text,
    register_retranslate,
    set_language,
    tr,
)
from app.modules.converter_page import ConverterPage
from app.modules.generator_page import GeneratorPage
from app.modules.history_page import HistoryPage
from app.modules.settings_dialog import SettingsDialog
from app.modules.stats_page import StatsPage
from app.modules.tools_page import ToolsPage
from app.modules.workspace_page import WorkspacePage
from app.settings.settings import settings
from app.utils import dialogs
from app.widgets.icons import icon
from app.widgets.sidebar import NAV_ITEMS, Sidebar
from app.widgets.statusbar import StatusBar
from app.widgets.tabwidget import EditorTab
from app.widgets.theme import theme

LARGE_FILE_WARN_BYTES = 20 * 1024 * 1024


def _try_page(module_name: str, class_name: str, parent):
    """引擎或页面未就绪/构造失败时返回 None，保证应用始终能启动。

    开发期由测试套件兜底页面缺陷；打包版本优先保证"能打开"，
    缺失的页面不会出现在侧边栏里（不放假入口）。
    """
    import importlib
    import traceback

    try:
        module = importlib.import_module(module_name)
        return getattr(module, class_name)(parent)
    except ImportError:
        return None
    except Exception:  # noqa: BLE001 - 单个页面坏了不应拖垮整个应用
        traceback.print_exc()
        return None


DEFAULT_FONT_SIZE = 13

#: 菜单/工具条动作的图标（key 与英文标签一致）
ACTION_ICONS: Dict[str, str] = {
    "New Tab": "file-plus",
    "Open…": "folder-open",
    "Save": "save",
    "Save As…": "save-as",
    "Close Tab": "close",
    "Exit": "close",
    "Undo": "undo",
    "Redo": "redo",
    "Find…": "search",
    "Replace…": "replace",
    "Go to Line…": "target",
    "Delete Line": "trash",
    "Format": "format",
    "Minify": "compress",
    "Validate": "check-circle",
    "Sort Keys…": "sort",
    "Copy Result": "copy",
    "Hide Sidebar": "list",
    "Hide Side Panel": "split",
    "Zoom In": "plus",
    "Zoom Out": "minus",
    "Reset Zoom": "refresh",
    "Word Wrap": "list",
    "Line Numbers": "list",
    "Settings…": "settings",
    "Keyboard Shortcuts": "help",
    "About JSON Workbench": "info",
    "About Qt": "info",
    "JSON Diff": "columns",
    "JSONPath Query": "branch",
    "JSON Schema": "shield-check",
    "Cut": "scissors",
    "Copy": "copy",
    "Paste": "clipboard",
    "Select All": "list",
    "Language": "globe",
    "Toggle Theme": "moon",
    "Open Recent": "history",
    "Clear Recent Files": "trash",
}

MENU_ICONS = {
    "File": "file",
    "Edit": "code",
    "Tools": "wrench",
    "View": "eye",
    "Settings": "settings",
    "Help": "help",
}

SHORTCUT_ROWS = [
    ("Ctrl+N", "New Tab"),
    ("Ctrl+O", "Open…"),
    ("Ctrl+S", "Save"),
    ("Ctrl+Shift+S", "Save As…"),
    ("Ctrl+W", "Close Tab"),
    ("Ctrl+F", "Find…"),
    ("Ctrl+H", "Replace…"),
    ("Ctrl+G", "Go to Line…"),
    ("Ctrl+Shift+K", "Delete Line"),
    ("Ctrl+Z", "Undo"),
    ("Ctrl+Y", "Redo"),
    ("Ctrl+A", "Select All"),
    ("Ctrl+Shift+F", "Format"),
    ("Ctrl+Shift+M", "Minify"),
    ("F5", "Validate"),
    ("Ctrl+B", "Hide Sidebar"),
    ("Ctrl+I", "Hide Side Panel"),
    ("Ctrl++ / Ctrl+-", "Zoom In"),
    ("Ctrl+0", "Reset Zoom"),
    ("Ctrl+,", "Settings…"),
    ("F1", "About JSON Workbench"),
]


class MainWindow(QMainWindow):
    """应用主窗口。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(icon("logo", kind="accent", size=32))
        self.setAcceptDrops(True)

        set_language(settings.language)
        if settings.theme != theme.name:
            theme.set_theme(settings.theme)

        self._actions: Dict[str, QAction] = {}
        self._menus: Dict[str, QMenu] = {}
        self._handlers: Dict[str, Callable[[], None]] = {}
        self._closing = False

        self._build_pages()
        self._build_central()
        self._build_actions()
        self._build_menus()
        self._build_status_bar()
        self._connect_pages()
        self._restore_geometry()

        self.apply_settings()
        self.apply_theme()

        if self.workspace.tabs.count() == 0:
            self.new_tab()
        register_retranslate(self)
        QTimer.singleShot(0, self._refresh_recent_menu)

    # ------------------------------------------------------------------ 构建
    def _build_pages(self) -> None:
        self.stack = QStackedWidget()
        self.workspace = WorkspacePage(self)
        self.stack.addWidget(self.workspace)
        # 侧边栏只登记"真的有实现"的入口，避免点了没反应的假按钮
        self.converter_page = ConverterPage(self)
        self.stats_page = StatsPage(self)
        self.generator_page = GeneratorPage(self)
        self.tools_page = ToolsPage(self)
        self.history_page = HistoryPage(self)
        for page in (self.converter_page, self.stats_page, self.generator_page,
                     self.tools_page, self.history_page):
            self.stack.addWidget(page)

        # 依赖引擎尚未就绪时（分模块开发期间）跳过对应页面，
        # 侧边栏也就不会出现"点了没反应"的假入口
        self.diff_page = _try_page("app.modules.diff_page", "DiffPage", self)
        self.jsonpath_page = _try_page("app.modules.jsonpath_page", "JsonPathPage", self)
        self.schema_page = _try_page("app.modules.schema_page", "SchemaPage", self)
        for page in (self.diff_page, self.jsonpath_page, self.schema_page):
            if page is not None:
                self.stack.addWidget(page)

        self._handlers = {
            "formatter": self._show_formatter,
            "validator": self._show_validator,
            "compressor": self._show_compressor,
            "tree": self._show_tree,
            "converter": lambda: self.stack.setCurrentWidget(self.converter_page),
            "stats": lambda: self._show_stats(),
            "generator": lambda: self.stack.setCurrentWidget(self.generator_page),
            "tools": lambda: self.stack.setCurrentWidget(self.tools_page),
            "history": lambda: self.stack.setCurrentWidget(self.history_page),
            "settings": self.open_settings,
        }
        if self.diff_page is not None:
            self._handlers["diff"] = lambda: self.stack.setCurrentWidget(self.diff_page)
        if self.jsonpath_page is not None:
            self._handlers["jsonpath"] = self._show_jsonpath
        if self.schema_page is not None:
            self._handlers["schema"] = lambda: self.stack.setCurrentWidget(self.schema_page)

    def _build_central(self) -> None:
        items = [item for item in NAV_ITEMS if item.item_id in self._handlers]
        self.sidebar = Sidebar(items=items, width=settings.sidebar_width)
        self.sidebar.item_selected.connect(self._on_nav)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(self.sidebar)
        self.splitter.addWidget(self.stack)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(1)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_header())
        layout.addWidget(self.splitter, 1)
        self.setCentralWidget(container)

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setObjectName("HeaderBar")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)

        self.logo_label = QLabel()
        self.logo_label.setObjectName("AppLogo")
        layout.addWidget(self.logo_label)

        self.title_label = QLabel(APP_NAME)
        self.title_label.setObjectName("AppTitle")
        layout.addWidget(self.title_label)

        self.version_label = QLabel(f"v{APP_VERSION}")
        self.version_label.setObjectName("AppSubtitle")
        layout.addWidget(self.version_label)
        layout.addSpacing(14)

        self.menu_bar = QMenuBar(header)
        self.menu_bar.setNativeMenuBar(False)
        layout.addWidget(self.menu_bar, 1)

        self.language_group = QActionGroup(self)
        self.language_group.setExclusive(True)
        self.language_actions: Dict[str, QAction] = {}
        for code, label in ((LANG_ZH, "简体中文"), (LANG_EN, "English")):
            action = QAction(label, self, checkable=True)
            action.setData(code)
            action.setChecked(code == current_language())
            action.triggered.connect(lambda _=False, value=code: self.change_language(value))
            self.language_group.addAction(action)
            self.language_actions[code] = action

        self.language_menu = QMenu(tr("Language"), self)
        for action in self.language_group.actions():
            self.language_menu.addAction(action)

        self.language_header_action = QAction(icon("globe", size=16, kind="muted"),
                                              tr("Language"), self)
        self.language_header_action.setMenu(self.language_menu)
        self._actions["Language"] = self.language_header_action

        self.theme_action = QAction(icon("moon", size=16, kind="muted"), tr("Toggle Theme"), self)
        self.theme_action.setToolTip(tr("Toggle dark/light theme"))
        self.theme_action.triggered.connect(self.toggle_theme)
        self._actions["Toggle Theme"] = self.theme_action

        layout.addWidget(self._header_button(self.theme_action))
        layout.addWidget(self._header_button(self.language_header_action))
        return header

    def _header_button(self, action: QAction) -> QWidget:
        holder = QWidget()
        holder_layout = QHBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        button = QToolButton(holder)
        button.setDefaultAction(action)
        button.setAutoRaise(True)
        button.setCursor(Qt.PointingHandCursor)
        if action.menu() is not None:
            button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        holder_layout.addWidget(button)
        return holder

    # ------------------------------------------------------------------ 动作
    def _act(self, label: str, shortcut=None, slot: Optional[Callable] = None,
             checkable: bool = False) -> QAction:
        action = QAction(icon(ACTION_ICONS.get(label, "info"), size=16, kind="muted"),
                         tr(label), self)
        if shortcut is not None:
            action.setShortcut(shortcut)
        action.setCheckable(checkable)
        if slot is not None:
            action.triggered.connect(slot)
        self._actions[label] = action
        self.addAction(action)
        return action

    def _build_actions(self) -> None:
        self._act("New Tab", "Ctrl+N", self.new_tab)
        self._act("Open…", "Ctrl+O", self.open_file_dialog)
        self._act("Save", QKeySequence.StandardKey.Save, self.save_current)
        self._act("Save As…", "Ctrl+Shift+S", self.save_current_as)
        self._act("Close Tab", "Ctrl+W", self.close_current_tab)
        self._act("Exit", "Ctrl+Q", self.close)

        self._act("Undo", QKeySequence.StandardKey.Undo, self._editor_undo)
        self._act("Redo", QKeySequence.StandardKey.Redo, self._editor_redo)
        self._act("Find…", QKeySequence.StandardKey.Find, lambda: self._editor_find(False))
        self._act("Replace…", QKeySequence.StandardKey.Replace, lambda: self._editor_find(True))
        self._act("Go to Line…", "Ctrl+G", self._editor_goto_line)
        self._act("Delete Line", "Ctrl+Shift+K", self._editor_delete_line)

        self._act("Format", "Ctrl+Shift+F", self.workspace.format_document)
        self._act("Minify", "Ctrl+Shift+M", self.workspace.minify_document)
        self._act("Validate", "F5", self.workspace.validate_now)
        self._act("Sort Keys…", None, self.workspace.open_sort_dialog)
        self._act("Copy Result", None, self.workspace.copy_result)

        self.sidebar_action = self._act("Hide Sidebar", "Ctrl+B", self.toggle_sidebar,
                                        checkable=True)
        self.sidebar_action.setChecked(True)
        self.panel_action = self._act("Hide Side Panel", "Ctrl+I", self.toggle_side_panel,
                                      checkable=True)
        self.panel_action.setChecked(True)
        self._act("Zoom In", QKeySequence.StandardKey.ZoomIn, lambda: self._zoom(1))
        self._act("Zoom Out", QKeySequence.StandardKey.ZoomOut, lambda: self._zoom(-1))
        self._act("Reset Zoom", "Ctrl+0", self._reset_zoom)
        self.wrap_action = self._act("Word Wrap", None, self.toggle_word_wrap, checkable=True)
        self.wrap_action.setChecked(settings.word_wrap)
        self.numbers_action = self._act("Line Numbers", None, self.toggle_line_numbers,
                                        checkable=True)
        self.numbers_action.setChecked(settings.line_numbers)

        if self.diff_page is not None:
            self._act("JSON Diff", "Ctrl+Shift+D",
                      lambda: self._on_nav("diff"))
        if self.jsonpath_page is not None:
            self._act("JSONPath Query", "Ctrl+Shift+P",
                      lambda: self._on_nav("jsonpath"))
        if self.schema_page is not None:
            self._act("JSON Schema", "Ctrl+Shift+V",
                      lambda: self._on_nav("schema"))

        self._act("Cut", None, lambda: self._edit_primitive("cut"))
        self._act("Copy", None, lambda: self._edit_primitive("copy"))
        self._act("Paste", None, lambda: self._edit_primitive("paste"))
        self._act("Select All", None, lambda: self._edit_primitive("selectAll"))

        self._act("Settings…", "Ctrl+,", self.open_settings)
        self._act("Keyboard Shortcuts", None, self.show_shortcuts)
        self._act("About JSON Workbench", "F1", self.show_about)
        self._act("About Qt", None, lambda: QMessageBox.aboutQt(self, tr("About Qt")))

    # ------------------------------------------------------------------ 菜单
    def _build_menus(self) -> None:
        bar = self.menu_bar
        acts = self._actions

        file_menu = bar.addMenu(tr("File"))
        file_menu.addAction(acts["New Tab"])
        file_menu.addAction(acts["Open…"])
        self._recent_menu = file_menu.addMenu(tr("Open Recent"))
        file_menu.addSeparator()
        file_menu.addAction(acts["Save"])
        file_menu.addAction(acts["Save As…"])
        file_menu.addSeparator()
        file_menu.addAction(acts["Close Tab"])
        file_menu.addSeparator()
        file_menu.addAction(acts["Exit"])

        edit_menu = bar.addMenu(tr("Edit"))
        edit_menu.addAction(acts["Undo"])
        edit_menu.addAction(acts["Redo"])
        edit_menu.addSeparator()
        edit_menu.addAction(acts["Find…"])
        edit_menu.addAction(acts["Replace…"])
        edit_menu.addAction(acts["Go to Line…"])
        edit_menu.addSeparator()
        for label in ("Cut", "Copy", "Paste", "Select All"):
            edit_menu.addAction(acts[label])
        edit_menu.addSeparator()
        edit_menu.addAction(acts["Delete Line"])

        tools_menu = bar.addMenu(tr("Tools"))
        for label in ("Format", "Minify", "Validate", "Sort Keys…", "Copy Result"):
            tools_menu.addAction(acts[label])

        view_menu = bar.addMenu(tr("View"))
        view_menu.addAction(self.sidebar_action)
        view_menu.addAction(self.panel_action)
        view_menu.addSeparator()
        for label in ("Zoom In", "Zoom Out", "Reset Zoom"):
            view_menu.addAction(acts[label])
        view_menu.addSeparator()
        view_menu.addAction(self.wrap_action)
        view_menu.addAction(self.numbers_action)
        view_menu.addSeparator()
        view_menu.addAction(acts["Toggle Theme"])

        settings_menu = bar.addMenu(tr("Settings"))
        settings_menu.addAction(acts["Settings…"])
        self._settings_language_menu = settings_menu.addMenu(tr("Language"))
        for action in self.language_group.actions():
            self._settings_language_menu.addAction(action)

        help_menu = bar.addMenu(tr("Help"))
        help_menu.addAction(acts["Keyboard Shortcuts"])
        help_menu.addSeparator()
        help_menu.addAction(acts["About JSON Workbench"])
        help_menu.addAction(acts["About Qt"])

        self._menus = {
            "File": file_menu, "Edit": edit_menu, "Tools": tools_menu, "View": view_menu,
            "Settings": settings_menu, "Help": help_menu, "Open Recent": self._recent_menu,
            "Language": self._settings_language_menu,
        }

    def _build_status_bar(self) -> None:
        self.status = StatusBar(self)
        self.setStatusBar(self.status)
        self.status.cancel_requested.connect(self.workspace.cancel_format)
        self.workspace.status_message.connect(self.status.notify)
        self.workspace.validity_changed.connect(self._on_validity)
        self.workspace.metrics_changed.connect(self._on_metrics)
        self.workspace.cursor_changed.connect(self.status.set_cursor)
        self.workspace.document_changed.connect(self._on_document_changed)
        self.workspace.busy_changed.connect(self._on_busy)
        self.workspace.undo_redo_changed.connect(self._on_undo_redo)
        self.workspace.progress_reported.connect(self.status.update_progress)

    def _show_jsonpath(self) -> None:
        tab = self.workspace.current_tab()
        if tab is not None and self.jsonpath_page is not None:
            from app.core.jsonparser import parse_json

            parsed = parse_json(tab.text(), tab.document.mode)
            if parsed.ok:
                self.jsonpath_page.set_document(parsed.value, tab.document.display_name)
                self.jsonpath_page.set_offsets(parsed.offsets)
        self.stack.setCurrentWidget(self.jsonpath_page)

    def _connect_pages(self) -> None:
        self.workspace.request_new_tab.connect(self.new_tab)
        self.workspace.request_close_tab.connect(self.close_tab_at)
        self.workspace.request_open_files.connect(self.open_paths)
        self.workspace.request_save_as.connect(self.save_current_as)
        self.workspace.history_event.connect(self._record_history_event)

        self.tools_page.set_document_provider(self._tools_document)
        self.tools_page.jump_requested.connect(self._jump_from_tool)
        self.tools_page.status_message.connect(self.status.notify)
        self.tools_page.send_to_editor.connect(self._send_to_editor)
        self.converter_page.status_message.connect(self.status.notify)
        self.converter_page.send_to_editor.connect(self._send_to_editor)
        self.generator_page.status_message.connect(self.status.notify)
        self.generator_page.send_to_editor.connect(self._send_to_editor)
        self.stats_page.status_message.connect(self.status.notify)
        self.history_page.status_message.connect(self.status.notify)
        if self.diff_page is not None:
            self.diff_page.status_message.connect(self.status.notify)
        if self.jsonpath_page is not None:
            self.jsonpath_page.status_message.connect(self.status.notify)
            self.jsonpath_page.locate_in_tree.connect(self._locate_in_tree)
            self.jsonpath_page.jump_in_editor.connect(self._jump_in_editor)
        if self.schema_page is not None:
            self.schema_page.status_message.connect(self.status.notify)
            self.schema_page.jump_in_editor.connect(self._jump_in_editor)
        self.history_page.restore_content.connect(lambda text: self.new_tab(text))
        self.history_page.restore_path.connect(lambda path: self.open_path(path))

    # ------------------------------------------------------------------ 导航
    def _on_nav(self, item_id: str) -> None:
        handler = self._handlers.get(item_id)
        if handler is not None:
            handler()

    def _show_formatter(self) -> None:
        self.stack.setCurrentWidget(self.workspace)
        self.workspace.format_button.setFocus()

    def _show_validator(self) -> None:
        self.stack.setCurrentWidget(self.workspace)
        self.workspace.show_side_tab(1)
        self.workspace.validate_now()

    def _show_compressor(self) -> None:
        self.stack.setCurrentWidget(self.workspace)
        self.workspace.minify_button.setFocus()

    def _show_tree(self) -> None:
        self.stack.setCurrentWidget(self.workspace)
        self.workspace.show_side_tab(0)
        self.workspace.validate_now()

    def _show_stats(self) -> None:
        tab = self.workspace.current_tab()
        if tab is not None:
            self.stats_page.set_document(tab.text(), tab.document.display_name)
        self.stack.setCurrentWidget(self.stats_page)

    # ------------------------------------------------------------------ 文件
    def new_tab(self, text: str = "") -> EditorTab:
        tab = self.workspace.add_tab(text)
        self._on_document_changed(tab)
        return tab

    def open_file_dialog(self) -> None:
        start_dir = settings.last_dir
        if not start_dir:
            current = self._current_path()
            start_dir = QFileInfo(current).absolutePath() if current else ""
        paths, _selected = QFileDialog.getOpenFileNames(
            self, tr("Open JSON file"), start_dir, file_dialog_filter())
        if paths:
            self.open_paths(paths)

    def open_paths(self, paths: List[str]) -> None:
        for path in paths:
            self.open_path(path)

    def open_path(self, path: str, *, tab: Optional[EditorTab] = None) -> bool:
        if not path:
            return False
        if self._user_declined_large_file(path):
            return False
        result = read_text_file(path)
        if not result.ok:
            dialogs.warn(self, tr("Cannot open file"), result.error)
            return False

        target = tab or self.workspace.current_tab()
        if target is None or target.document.path or not target.is_empty():
            target = self.workspace.add_tab("", activate=True)
        target.load_read_result(result)
        target.apply_settings()
        self.workspace.tabs.refresh_title(target)
        self._on_document_changed(target)

        settings.last_dir = QFileInfo(path).absolutePath()
        settings.add_recent_file(result.path)
        settings.sync()
        self._refresh_recent_menu()
        self._record_file_history(result, target)

        if result.warnings:
            self.status.notify(" · ".join(result.warnings), 8000)
        else:
            self.status.notify(tr("Opened {name} ({size})").format(
                name=QFileInfo(result.path).fileName(), size=format_size(result.size)), 5000)
        target.request_validation()
        return True

    def _user_declined_large_file(self, path: str) -> bool:
        try:
            size = QFileInfo(path).size()
        except OSError:
            return False
        if size < LARGE_FILE_WARN_BYTES:
            return False
        return not dialogs.confirm(
            self, tr("Large file"),
            tr("This file is {size}. Opening it may take a while and use a lot of memory.\n"
               "Syntax highlighting will be turned off automatically.\n\nOpen anyway?").format(
                   size=format_size(size)),
            default=True)

    def _record_file_history(self, result, tab: EditorTab) -> None:
        from app.history.store import KIND_FILE, HistoryStore

        store = HistoryStore()
        try:
            store.record(KIND_FILE, QFileInfo(result.path).fileName(),
                         detail=f"{format_size(result.size)} · {result.encoding}",
                         path=result.path, content=tab.text())
        finally:
            store.close()

    def save_current(self) -> bool:
        tab = self.workspace.current_tab()
        if tab is None:
            return False
        if not tab.document.path:
            return self.save_current_as()
        return self._finish_save(tab, tab.save_path())

    def save_current_as(self) -> bool:
        tab = self.workspace.current_tab()
        if tab is None:
            return False
        start = tab.document.path or settings.last_dir
        path, _selected = QFileDialog.getSaveFileName(
            self, tr("Save as"), start, file_dialog_filter(tab.document.mode))
        if not path:
            return False
        result = tab.save_path(path)
        if result.ok:
            settings.last_dir = QFileInfo(path).absolutePath()
            settings.add_recent_file(path)
            settings.sync()
            self._refresh_recent_menu()
        return self._finish_save(tab, result)

    def _finish_save(self, tab: EditorTab, result) -> bool:
        if not result.ok:
            dialogs.warn(self, tr("Cannot save file"), result.error)
            return False
        self.workspace.tabs.refresh_title(tab)
        self._on_document_changed(tab)
        self.status.notify(tr("Saved {path} ({size})").format(
            path=result.path, size=format_size(result.bytes_written)), 5000)
        return True

    def close_current_tab(self) -> None:
        self.close_tab_at(self.workspace.tabs.currentIndex())

    def close_tab_at(self, index: int) -> None:
        tab = self.workspace.tabs.widget(index)
        if not isinstance(tab, EditorTab):
            return
        if not self._confirm_discard(tab):
            return
        tab.cancel_validation()
        self.workspace.close_tab_at(index)
        if self.workspace.tabs.count() == 0:
            self.new_tab()
        self._on_document_changed(self.workspace.current_tab())

    def _confirm_discard(self, tab: EditorTab) -> bool:
        """返回 False 表示用户取消（不要关闭）。"""
        if not tab.document.modified:
            return True
        if dialogs.is_headless():
            # 自动化运行时不弹模态框：直接按"不保存"处理，避免测试挂死
            return True
        answer = QMessageBox.question(
            self, tr("Unsaved changes"),
            tr("“{name}” has unsaved changes. Save before closing?").format(
                name=tab.document.display_name),
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Save)
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        if answer == QMessageBox.StandardButton.Save:
            self.workspace.tabs.setCurrentWidget(tab)
            return self.save_current()
        return True

    def _locate_in_tree(self, path: tuple) -> None:
        self.stack.setCurrentWidget(self.workspace)
        self.workspace.show_side_tab(0)
        self.workspace.tree_panel.locate_path(tuple(path))

    def _jump_in_editor(self, offset: int, length: int) -> None:
        tab = self.workspace.current_tab()
        if tab is None:
            return
        self.stack.setCurrentWidget(self.workspace)
        tab.editor.goto_offset(offset, length)

    def _tools_document(self):
        tab = self.workspace.current_tab()
        if tab is None:
            return "", "json"
        return tab.text(), tab.document.mode

    def _jump_from_tool(self, offset: int, length: int) -> None:
        tab = self.workspace.current_tab()
        if tab is None:
            return
        self.stack.setCurrentWidget(self.workspace)
        tab.editor.goto_offset(offset, length)

    def _send_to_editor(self, text: str) -> None:
        tab = self.new_tab(text)
        tab.document.set_modified(True)
        self.stack.setCurrentWidget(self.workspace)
        self.status.notify(tr("Sent {n} characters to a new tab.").format(n=len(text)), 4000)

    def _record_history_event(self, kind: str, label: str, detail: str, tab=None) -> None:
        from app.history.store import HistoryStore

        store = HistoryStore()
        try:
            tab = tab or self.workspace.current_tab()
            store.record(kind, label, detail=detail,
                         path=tab.document.path if tab else "",
                         content=tab.text() if tab else "")
        finally:
            store.close()

    def _current_path(self) -> str:
        tab = self.workspace.current_tab()
        return tab.document.path if tab else ""

    # ------------------------------------------------------------------ 编辑器动作
    def _editor(self):
        tab = self.workspace.current_tab()
        return tab.editor if tab is not None else None

    def _editor_find(self, replace: bool) -> None:
        tab = self.workspace.current_tab()
        if tab is None:
            return
        self.stack.setCurrentWidget(self.workspace)
        tab.show_find(replace)

    def _editor_goto_line(self) -> None:
        tab = self.workspace.current_tab()
        if tab is not None:
            tab.prompt_goto_line()

    def _editor_delete_line(self) -> None:
        editor = self._editor()
        if editor is not None and not editor.isReadOnly():
            editor.delete_current_line()

    def _edit_primitive(self, name: str) -> None:
        """对当前焦点控件执行 cut/copy/paste/selectAll；焦点不在编辑控件上时用主编辑器。"""
        from PySide6.QtWidgets import QApplication

        widget = QApplication.focusWidget()
        target = widget if widget is not None and hasattr(widget, name) else self._editor()
        if target is not None and hasattr(target, name):
            getattr(target, name)()

    def _editor_undo(self) -> None:
        editor = self._editor()
        if editor is not None:
            editor.undo()

    def _editor_redo(self) -> None:
        editor = self._editor()
        if editor is not None:
            editor.redo()

    def _zoom(self, delta: int) -> None:
        size = settings.font_size
        for tab in self.workspace.tabs.editors():
            size = tab.editor.zoom(delta)
        settings.font_size = size
        settings.sync()

    def _reset_zoom(self) -> None:
        settings.font_size = DEFAULT_FONT_SIZE
        settings.sync()
        self.apply_settings()

    def toggle_word_wrap(self, checked: bool) -> None:
        settings.word_wrap = bool(checked)
        settings.sync()
        self.apply_settings()

    def toggle_line_numbers(self, checked: bool) -> None:
        settings.line_numbers = bool(checked)
        settings.sync()
        self.apply_settings()

    def toggle_sidebar(self, visible: bool) -> None:
        self.sidebar.setVisible(visible)
        settings.sidebar_width = self.sidebar.width()
        self._refresh_toggle_labels()

    def toggle_side_panel(self, visible: bool) -> None:
        self.workspace.set_panel_visible(visible)
        self._refresh_toggle_labels()

    def _refresh_toggle_labels(self) -> None:
        self.sidebar_action.setText(tr("Hide Sidebar") if self.sidebar.isVisible()
                                    else tr("Show Sidebar"))
        self.panel_action.setText(tr("Hide Side Panel") if self.workspace.panel_visible()
                                  else tr("Show Side Panel"))

    # ------------------------------------------------------------------ 状态同步
    def _on_validity(self, result: Optional[ValidationResult]) -> None:
        if result is None:
            self.status.set_validity(None)
        elif result.valid:
            detail = tr("{n} warning(s)").format(n=len(result.warnings)) if result.warnings else ""
            self.status.set_validity(True, detail)
        else:
            detail = ""
            if result.error is not None:
                title, _message, _suggestion = issue_text(result.error)
                detail = tr("{title} at line {line}, column {column}").format(
                    title=title, line=result.error.line, column=result.error.column)
            self.status.set_validity(False, detail)

    def _on_metrics(self, lines: int, chars: int, stats) -> None:
        self.status.set_metrics(lines, chars, stats)

    def _on_document_changed(self, tab) -> None:
        if tab is None:
            self.status.set_file_name("")
            self.status.set_validity(None)
            self.status.set_metrics(0, 0, None)
            self.status.set_encoding("utf-8", False, "\n")
            self.status.set_mode("JSON")
            self.setWindowTitle(APP_NAME)
            return
        document = tab.document
        self.status.set_file_name(document.display_name, document.tooltip)
        self.status.set_encoding(document.encoding, document.had_bom, document.line_ending)
        self.status.set_mode(document.mode_label)
        self.status.set_indent(tab.editor.indent_text())
        self._actions["Undo"].setEnabled(tab.editor.document().isUndoAvailable())
        self._actions["Redo"].setEnabled(tab.editor.document().isRedoAvailable())
        self.setWindowTitle(f"{document.display_name} — {APP_NAME}")

    def _on_undo_redo(self, undo_available: bool, redo_available: bool) -> None:
        self._actions["Undo"].setEnabled(undo_available)
        self._actions["Redo"].setEnabled(redo_available)

    def _on_busy(self, busy: bool) -> None:
        if busy:
            self.status.show_progress(tr("Working…"), 0)
        else:
            self.status.hide_progress()

    # ------------------------------------------------------------------ 最近文件
    def _refresh_recent_menu(self) -> None:
        menu = self._recent_menu
        if menu is None:
            return
        menu.clear()
        entries = settings.recent_files()
        if not entries:
            empty = menu.addAction(tr("No recent files"))
            empty.setEnabled(False)
            return
        for path in entries:
            action = menu.addAction(icon("file", size=15, kind="muted"),
                                    QFileInfo(path).fileName())
            action.setToolTip(path)
            action.setStatusTip(path)
            action.triggered.connect(lambda _=False, target=path: self.open_path(target))
        menu.addSeparator()
        clear = menu.addAction(icon("trash", size=15, kind="muted"), tr("Clear Recent Files"))
        clear.triggered.connect(self._clear_recent)

    def _clear_recent(self) -> None:
        settings.clear_recent_files()
        settings.sync()
        self._refresh_recent_menu()

    # ------------------------------------------------------------------ 外观与语言
    def toggle_theme(self) -> None:
        settings.theme = theme.toggle()
        settings.sync()
        self.apply_theme()

    def set_theme(self, name: str) -> None:
        theme.set_theme(name)
        settings.theme = name
        settings.sync()
        self.apply_theme()

    def apply_theme(self) -> None:
        theme.apply()
        self.logo_label.setPixmap(icon("logo", kind="accent", size=26).pixmap(26, 26))
        self.setWindowIcon(icon("logo", kind="accent", size=32))
        self.sidebar.refresh_icons()
        self.workspace.apply_theme()
        self.workspace.tabs.refresh_tab_buttons()
        for page in (self.converter_page, self.stats_page, self.generator_page,
                     self.tools_page, self.diff_page, self.jsonpath_page, self.schema_page):
            if page is not None:
                page.apply_theme()
        for label, action in self._actions.items():
            name = ACTION_ICONS.get(label)
            if name == "moon":
                name = "sun" if theme.is_dark() else "moon"
            if name:
                action.setIcon(icon(name, size=16, kind="muted"))
        # 顶级菜单标题保持纯文字：给 menuBar 的 menuAction 加图标会让某些平台只画图标
        for label in ("Open Recent", "Language"):
            menu = self._menus.get(label)
            if menu is not None:
                menu.menuAction().setIcon(icon(MENU_ICONS.get(label, "info"), size=16,
                                                kind="muted"))

    def change_language(self, code: str) -> None:
        if code == current_language():
            return
        settings.language = code
        settings.sync()
        set_language(code)
        for action_code, action in self.language_actions.items():
            action.setChecked(action_code == code)
        self.retranslate()

    def apply_settings(self) -> None:
        self.workspace.apply_settings()
        self.wrap_action.setChecked(settings.word_wrap)
        self.numbers_action.setChecked(settings.line_numbers)
        self.sidebar_action.setChecked(self.sidebar.isVisible())
        self.panel_action.setChecked(self.workspace.panel_visible())
        self._refresh_toggle_labels()

    # ------------------------------------------------------------------ 对话框
    def open_settings(self) -> None:
        dialog = SettingsDialog(self)
        dialog.settings_changed.connect(self._on_settings_changed)
        dialog.exec()
        self._on_settings_changed()

    def _on_settings_changed(self) -> None:
        if settings.theme != theme.name:
            theme.set_theme(settings.theme)
            self.apply_theme()
        if settings.language != current_language():
            set_language(settings.language)
            for action_code, action in self.language_actions.items():
                action.setChecked(action_code == settings.language)
            self.retranslate()
        self.apply_settings()
        tab = self.workspace.current_tab()
        if tab is not None:
            tab.request_validation()

    def show_about(self) -> None:
        from PySide6.QtCore import qVersion

        python_version = ".".join(str(part) for part in sys.version_info[:3])
        QMessageBox.about(
            self, tr("About {app}").format(app=APP_NAME),
            tr("<h3>{app} {version}</h3>"
               "<p>A professional, fully offline JSON workbench for developers.</p>"
               "<p>Qt {qt} · Python {py}</p>"
               "<p><b>Privacy:</b> no network access, no telemetry, no uploads — "
               "every document is processed on this machine.</p>"
               "<p><b>Third-party licenses:</b> PySide6 (LGPLv3), PyYAML (MIT).</p>"
               "<p>© {org} · {domain}</p>").format(
                   app=APP_NAME, version=APP_VERSION, qt=qVersion(), py=python_version,
                   org=APP_ORG, domain=APP_DOMAIN))

    def show_shortcuts(self) -> None:
        width = max(len(combo) for combo, _label in SHORTCUT_ROWS) + 4
        body = "\n".join(f"{combo:<{width}}{tr(label)}" for combo, label in SHORTCUT_ROWS)
        dialogs.information(self, tr("Keyboard Shortcuts"), body)

    # ------------------------------------------------------------------ 生命周期
    def _restore_geometry(self) -> None:
        geometry = settings.window_geometry()
        if geometry is not None:
            self.restoreGeometry(geometry)
        else:
            self.resize(settings.default_size())
            self.move(settings.default_position())
        state = settings.window_state()
        if state is not None:
            self.restoreState(state)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt 命名)
        if self._closing:
            event.accept()
            return
        for index in range(self.workspace.tabs.count() - 1, -1, -1):
            tab = self.workspace.tabs.widget(index)
            if isinstance(tab, EditorTab) and not self._confirm_discard(tab):
                event.ignore()
                return
        self._closing = True
        self.workspace.cancel_format()
        for tab in self.workspace.tabs.editors():
            tab.cancel_validation()
        self.workspace.save_state()
        settings.sidebar_width = self.sidebar.width()
        settings.save_window(self.saveGeometry(), self.saveState())
        settings.sync()
        event.accept()

    # ------------------------------------------------------------------ 拖放
    def dragEnterEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.toLocalFile()]
        paths = [path for path in paths if self._is_supported(path)]
        if paths:
            self.open_paths(paths)
            event.acceptProposedAction()

    @staticmethod
    def _is_supported(path: str) -> bool:
        suffix = QFileInfo(path).suffix().lower()
        return f".{suffix}" in SUPPORTED_EXTENSIONS

    # ------------------------------------------------------------------ 翻译
    def retranslate(self) -> None:
        self.title_label.setText(APP_NAME)
        self.version_label.setText(f"v{APP_VERSION}")
        self.theme_action.setToolTip(tr("Toggle dark/light theme"))
        for label, action in self._actions.items():
            action.setText(tr(label))
        for label, menu in self._menus.items():
            menu.menuAction().setText(tr(label))
        self._refresh_toggle_labels()
        self._refresh_recent_menu()
        self.workspace.tabs.refresh_all_titles()
        self._on_document_changed(self.workspace.current_tab())

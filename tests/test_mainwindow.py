"""主窗口集成测试：文件流、标签流、主题/语言切换、快捷键动作。

模态文件对话框在离屏环境会挂死，因此涉及"另存为"的路径直接调用
``EditorTab.save_path``，主窗口层只测不弹框的分支。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from app import APP_NAME
from app.i18n import LANG_EN, LANG_ZH, current_language
from app.widgets.mainwindow import MainWindow
from app.widgets.theme import THEME_DARK, THEME_LIGHT, theme

_app = QApplication.instance() or QApplication([])

SAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "resources", "samples", "sample.json")
BROKEN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "resources", "samples", "broken.json")


class MainWindowCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.window = MainWindow()
        cls.window.resize(1280, 800)
        cls.window.show()
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.window.close()
        cls.tmp.cleanup()

    def setUp(self):
        self.window = type(self).window
        # 每个用例从单一标签开始，避免相互干扰
        while self.window.workspace.tabs.count() > 1:
            self.window.close_tab_at(self.window.workspace.tabs.count() - 1)

    # ------------------------------------------------------------------ 文件
    def test_open_sample_and_validate(self):
        self.assertTrue(self.window.open_path(SAMPLE))
        tab = self.window.workspace.current_tab()
        self.assertEqual(tab.document.path, os.path.abspath(SAMPLE))
        result = tab.validation(force=True)
        self.assertTrue(result.valid)
        self.assertIn("sample.json", self.window.windowTitle())

    def test_open_broken_marks_invalid(self):
        self.assertTrue(self.window.open_path(BROKEN))
        tab = self.window.workspace.current_tab()
        result = tab.validation(force=True)
        self.assertFalse(result.valid)
        self.assertEqual(self.window.status.valid_label.objectName(), "StatusInvalid")

    def test_open_missing_file_reports_error(self):
        missing = os.path.join(self.tmp.name, "nope.json")
        self.assertFalse(self.window.open_path(missing))

    def test_save_roundtrip(self):
        self.window.new_tab('{"saved": true}')
        tab = self.window.workspace.current_tab()
        target = os.path.join(self.tmp.name, "out.json")
        self.assertTrue(tab.save_path(target))
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), '{"saved": true}')
        self.assertFalse(tab.document.modified)
        self.window.save_current()   # 已有路径，直接保存不弹框
        self.assertTrue(os.path.exists(target))

    def test_recent_files_recorded(self):
        self.window.open_path(SAMPLE)
        from app.settings.settings import settings

        self.assertIn(os.path.abspath(SAMPLE), settings.recent_files())

    # ------------------------------------------------------------------ 标签
    def test_new_and_close_tabs(self):
        before = self.window.workspace.tabs.count()
        self.window.new_tab("{}")
        self.assertEqual(self.window.workspace.tabs.count(), before + 1)
        self.window.close_current_tab()
        self.assertEqual(self.window.workspace.tabs.count(), before)
        self.assertGreaterEqual(self.window.workspace.tabs.count(), 1)

    def test_close_modified_tab_discards_in_headless(self):
        tab = self.window.new_tab('{"a":1}')
        tab.editor.set_text('{"a":2}', keep_undo=True)
        tab.document.set_modified(True)
        index = self.window.workspace.tabs.indexOf(tab)
        self.window.close_tab_at(index)   # headless 下确认框自动按默认（不保存）通过
        self.assertNotIn(tab, self.window.workspace.tabs.editors())

    # ------------------------------------------------------------------ 动作
    def test_format_action(self):
        tab = self.window.new_tab('{"a":1,"b":[1,2]}')
        self.window.workspace.tabs.setCurrentWidget(tab)
        self.window._actions["Format"].trigger()
        self.assertIn('\n    "a": 1,', tab.text())

    def test_minify_action(self):
        tab = self.window.new_tab('{\n  "a": 1\n}')
        self.window.workspace.tabs.setCurrentWidget(tab)
        self.window._actions["Minify"].trigger()
        self.assertEqual(tab.text(), '{"a":1}')

    def test_validate_action_focuses_issues(self):
        tab = self.window.new_tab('{"a":1,}')
        self.window.workspace.tabs.setCurrentWidget(tab)
        self.window._actions["Validate"].trigger()
        self.assertIs(self.window.workspace.side_panel.currentWidget(),
                      self.window.workspace.issues_panel)

    def test_undo_redo_actions(self):
        tab = self.window.new_tab('{"a":1}')
        self.window.workspace.tabs.setCurrentWidget(tab)
        tab.editor.set_text('{"a":2}', keep_undo=True)
        self.window._actions["Undo"].trigger()
        self.assertEqual(tab.text(), '{"a":1}')
        self.window._actions["Redo"].trigger()
        self.assertEqual(tab.text(), '{"a":2}')

    def test_zoom_actions(self):
        from app.settings.settings import settings

        base = settings.font_size
        self.window._actions["Zoom In"].trigger()
        self.assertEqual(settings.font_size, base + 1)
        self.window._actions["Reset Zoom"].trigger()
        self.assertEqual(settings.font_size, 13)
        settings.font_size = base
        settings.sync()

    def test_sidebar_navigation_covers_all_registered_pages(self):
        for item_id, handler in self.window._handlers.items():
            self.assertTrue(callable(handler), item_id)
            if item_id == "settings":
                continue  # 设置是模态对话框，离屏环境不 exec
            self.window._on_nav(item_id)
        self.window._on_nav("formatter")
        self.assertIs(self.window.stack.currentWidget(), self.window.workspace)

    # ------------------------------------------------------------------ 外观
    def test_theme_toggle_roundtrip(self):
        original = theme.name
        self.window.toggle_theme()
        self.assertNotEqual(theme.name, original)
        self.window.toggle_theme()
        self.assertEqual(theme.name, original)

    def test_language_switch_roundtrip(self):
        original = current_language()
        try:
            self.window.change_language(LANG_EN if original == LANG_ZH else LANG_ZH)
            self.assertNotEqual(current_language(), original)
            self.window.change_language(original)
            self.assertEqual(current_language(), original)
        finally:
            self.window.change_language(original)

    def test_retranslate_does_not_crash(self):
        from app.i18n import retranslate_all

        retranslate_all()
        self.assertTrue(self.window.windowTitle())

    def test_language_switch_refreshes_tab_and_panel_titles(self):
        from PySide6.QtWidgets import QTabBar

        original = current_language()
        tab = self.window.workspace.current_tab()
        if tab.document.path:
            tab = self.window.new_tab("{}")
        try:
            self.window.change_language(LANG_EN)
            self.assertTrue(tab.document.display_name.startswith("Untitled"),
                            tab.document.display_name)
            panel = self.window.workspace.side_panel
            texts = [panel.tabText(i) for i in range(panel.count())]
            self.assertIn("Tree", texts)
            self.assertNotIn("结构", texts)
            self.window.change_language(LANG_ZH)
            self.assertTrue(tab.document.display_name.startswith("未命名"),
                            tab.document.display_name)
            texts = [panel.tabText(i) for i in range(panel.count())]
            self.assertIn("结构", texts)
        finally:
            self.window.change_language(original)

    def test_tab_close_button_exists_and_works(self):
        from PySide6.QtWidgets import QTabBar, QToolButton

        tabs = self.window.workspace.tabs
        tab = self.window.new_tab("{}")
        index = tabs.indexOf(tab)
        button = tabs.tabBar().tabButton(index, QTabBar.ButtonPosition.RightSide)
        self.assertIsInstance(button, QToolButton)
        count = tabs.count()
        button.click()
        self.assertEqual(tabs.count(), count - 1)

    def test_window_title_shows_app_name(self):
        self.assertIn(APP_NAME, self.window.windowTitle())

    def test_status_bar_metrics_update(self):
        tab = self.window.new_tab('{"a":[1,2,3]}')
        self.window.workspace.tabs.setCurrentWidget(tab)
        tab.validation(force=True)
        self.assertIn("5", self.window.status.nodes_label.text())


if __name__ == "__main__":
    unittest.main(verbosity=2)

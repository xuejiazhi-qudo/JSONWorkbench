"""功能页离屏回归测试：每个页面都要能构造、能跑通主流程、不弹模态框。

tests/__init__.py 已设置 JSONWB_HEADLESS，因此确认类对话框会自动按默认值返回。
"""

from __future__ import annotations

import os
import time
import unittest

from PySide6.QtWidgets import QApplication

from app.core.jsonparser import parse_json
from app.modules.converter_page import ConverterPage
from app.modules.generator_page import GeneratorPage
from app.modules.history_page import HistoryPage
from app.modules.stats_page import StatsPage
from app.modules.tools_page import ToolsPage
from app.modules.tree_view import TreePanel
from app.modules.workspace_page import WorkspacePage
from app.widgets.mainwindow import MainWindow

_app = QApplication.instance() or QApplication([])

SAMPLE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "resources", "samples", "sample.json")

DOC = '{"a":1,"a":2,"e":"","ws":"  ","arr":[1,1,2],"obj":{},"list":[],"n":null}'


def read_sample() -> str:
    with open(SAMPLE_PATH, encoding="utf-8") as handle:
        return handle.read()


class TestConverterPage(unittest.TestCase):
    def setUp(self):
        self.page = ConverterPage()
        self.page.show()

    def index_of(self, conv_id: str) -> int:
        for i in range(self.page.conversion_combo.count()):
            if self.page.conversion_combo.itemData(i) == conv_id:
                return i
        raise AssertionError(f"conversion {conv_id} not found")

    def run_conversion(self, index: int, text: str) -> str:
        self.page.conversion_combo.setCurrentIndex(index)
        self.page.input_editor.set_text(text)
        self.page.run_conversion()
        return self.page.output_editor.text()

    def test_json_to_csv(self):
        out = self.run_conversion(0, '{"users":[{"id":1,"name":"张三"}]}')
        self.assertTrue(self.page._last_result.ok)
        self.assertIn("users.id,users.name", out)
        self.assertIn("1,张三", out)

    def test_csv_to_json(self):
        out = self.run_conversion(1, "a,b\n1,2\n")
        self.assertEqual(parse_json(out).value, [{"a": 1, "b": 2}])

    def test_json_to_xml_and_back(self):
        xml = self.run_conversion(2, '{"a":[1,2]}')
        self.assertIn("<a>1</a>", xml)
        back = self.run_conversion(3, xml)
        self.assertEqual(parse_json(back).value, {"a": [1, 2]})

    def test_json_to_yaml_and_back(self):
        yaml_text = self.run_conversion(4, '{"n":1.230,"s":"中"}')
        self.assertIn("1.230", yaml_text)
        back = self.run_conversion(5, yaml_text)
        self.assertEqual(parse_json(back).value["s"], "中")

    def test_escape_roundtrip(self):
        escaped = self.run_conversion(6, 'a\nb "c"')
        self.assertIn("\\n", escaped)
        unescaped = self.run_conversion(7, escaped)
        self.assertEqual(unescaped, 'a\nb "c"')

    def test_split_needs_array_or_field(self):
        self.run_conversion(8, '{"a":1}')
        self.assertFalse(self.page._last_result.ok)
        self.page._forms["split"]["chunk_size"].setValue(2)
        out = self.run_conversion(8, "[1,2,3]")
        self.assertTrue(self.page._last_result.ok)
        self.assertEqual(len(out.strip().splitlines()), 2)

    def test_merge_documents(self):
        out = self.run_conversion(9, '[{"a":1},{"b":2}]')
        self.assertEqual(parse_json(out).value, {"a": 1, "b": 2})

    def test_dedupe(self):
        out = self.run_conversion(self.index_of("dedupe"), '[{"id":1},{"id":1},{"id":2}]')
        self.assertEqual(parse_json(out).value, [{"id": 1}, {"id": 2}])

    def test_unwrap_string_encoded_json(self):
        import json as _json

        inner = '{"a":1.230,"b":[1,2]}'
        wrapped = _json.dumps(_json.dumps(inner))
        index = [i for i in range(self.page.conversion_combo.count())
                 if self.page.conversion_combo.itemData(i) == "unwrap"][0]
        out = self.run_conversion(index, wrapped)
        self.assertTrue(self.page._last_result.ok, self.page._last_result.error)
        self.assertEqual(self.page._last_result.meta["layers"], 2)
        # JsonNumber 与 float 的 == 是精确比较，这里用语义等价而不是 dict 相等
        from app.core.jsonformatter import json_equal

        self.assertTrue(json_equal(parse_json(out).value, parse_json(inner).value))
        self.assertIn("1.230", out)

        self.run_conversion(index, '{"a":1}')
        self.assertFalse(self.page._last_result.ok)
        self.assertIn("not_wrapped", self.page._last_result.error)

    def test_invalid_input_reports_error_not_crash(self):
        self.run_conversion(0, "{oops")
        self.assertFalse(self.page._last_result.ok)
        self.assertEqual(self.page.output_editor.text(), "")


class TestStatsPage(unittest.TestCase):
    def test_statistics_and_structure(self):
        page = StatsPage()
        page.show()
        page.set_document(read_sample(), "sample.json")
        self.assertGreaterEqual(page.rows_tree.topLevelItemCount(), 20)
        self.assertGreaterEqual(page.structure_tree.topLevelItemCount(), 3)
        self.assertGreater(page.largest_tree.topLevelItemCount(), 0)

    def test_invalid_document_shows_message(self):
        page = StatsPage()
        page.set_document("{oops", "bad.json")
        self.assertEqual(page.rows_tree.topLevelItemCount(), 1)


class TestToolsPage(unittest.TestCase):
    def setUp(self):
        self.page = ToolsPage()
        self.page.show()
        self.page.set_document_provider(lambda: (DOC, "json"))

    def test_duplicate_keys(self):
        self.page.run_duplicates()
        self.assertEqual(self.page.dup_tree.topLevelItemCount(), 1)

    def test_empty_values(self):
        self.page.run_empty()
        self.assertGreaterEqual(self.page.empty_tree.topLevelItemCount(), 4)

    def test_key_search(self):
        self.page.key_pattern.setText("a")
        self.page.run_keysearch()
        self.assertGreaterEqual(self.page.key_tree.topLevelItemCount(), 2)

    def test_dedupe_requires_array(self):
        self.page.tabs.setCurrentIndex(3)
        self.page.run_dedupe()
        self.assertEqual(self.page.output_editor.text(), "")

    def test_analysis_sections(self):
        self.page.set_document_provider(lambda: (read_sample(), "json"))
        self.page.tabs.setCurrentIndex(6)
        self.page.run_analysis()
        self.assertGreaterEqual(self.page.analysis_tree.topLevelItemCount(), 3)


class TestGeneratorPage(unittest.TestCase):
    def test_field_table_generation(self):
        page = GeneratorPage()
        page.show()
        page.seed_edit.setText("42")
        page.run_generate()
        value = parse_json(page.output_editor.text()).value
        self.assertIsInstance(value, dict)
        self.assertIn("name", value)
        self.assertIn("email", value)

    def test_deterministic_with_seed(self):
        page = GeneratorPage()
        page.seed_edit.setText("7")
        page.run_generate()
        first = page.output_editor.text()
        page.run_generate()
        self.assertEqual(first, page.output_editor.text())

    def test_mock_mode(self):
        page = GeneratorPage()
        page.mode_combo.setCurrentIndex(2)
        page.count_spin.setValue(3)
        page.seed_edit.setText("1")
        page.run_generate()
        value = parse_json(page.output_editor.text()).value
        self.assertIsInstance(value, list)
        self.assertEqual(len(value), 3)

    def test_schema_mode_invalid_schema_warns(self):
        page = GeneratorPage()
        page.mode_combo.setCurrentIndex(1)
        page.schema_editor.set_text("{not json")
        page.run_generate()
        self.assertEqual(page.output_editor.text(), "")


class TestHistoryPage(unittest.TestCase):
    def test_refresh_and_filters(self):
        page = HistoryPage()
        page.show()
        page.refresh()
        total = page.tree.topLevelItemCount()
        page.filter_combo.setCurrentIndex(1)   # 只看打开的文件
        filtered = page.tree.topLevelItemCount()
        self.assertLessEqual(filtered, total)

    def test_clear_requires_confirmation_and_headless_defaults_no(self):
        page = HistoryPage()
        page.show()
        before = page.tree.topLevelItemCount()
        page.clear_all()   # headless 下 confirm 默认 False -> 不应清空
        page.refresh()
        self.assertEqual(page.tree.topLevelItemCount(), before)


class TestMainWindowIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.window = MainWindow()
        cls.window.resize(1400, 900)
        cls.window.show()

    def test_open_sample_and_navigate_all_pages(self):
        self.assertTrue(self.window.open_path(SAMPLE_PATH))
        for item_id in ("formatter", "validator", "compressor", "tree", "converter",
                        "stats", "generator", "tools", "history", "settings"):
            handler = self.window._handlers.get(item_id)
            self.assertIsNotNone(handler, item_id)
        self.window._on_nav("converter")
        self.assertIs(self.window.stack.currentWidget(), self.window.converter_page)
        self.window._on_nav("tools")
        self.assertIs(self.window.stack.currentWidget(), self.window.tools_page)
        self.window._on_nav("formatter")
        self.assertIs(self.window.stack.currentWidget(), self.window.workspace)

    def test_tree_panel_reflects_document(self):
        self.window._on_nav("tree")
        panel = self.window.workspace.tree_panel
        self.assertTrue(panel.model.has_document())
        self.assertTrue(panel.locate_path(("users", 0, "name")))

    def test_send_to_editor_creates_tab(self):
        before = self.window.workspace.tabs.count()
        self.window._send_to_editor('{"x":1}')
        self.assertEqual(self.window.workspace.tabs.count(), before + 1)
        self.window.close_tab_at(self.window.workspace.tabs.count() - 1)

    def test_tree_edit_roundtrips_into_editor(self):
        self.window._on_nav("formatter")
        tab = self.window.workspace.current_tab()
        panel = self.window.workspace.tree_panel
        index = panel.model.index_for_path(("offline",))
        self.assertTrue(index.isValid())
        before = tab.text()
        panel.model.set_node_value(panel.model.index(index.row(), 1, index.parent()), False)
        self.assertNotEqual(tab.text(), before)
        self.assertIn('"offline": false', tab.text())


class TestTreePanelStandalone(unittest.TestCase):
    def test_search_and_locate(self):
        panel = TreePanel()
        panel.show()
        result = parse_json(read_sample())
        panel.set_document(result.value, result.offsets)
        panel.search_edit.setText("email")
        self.assertIn("2", panel.search_status.text())


class TestAsyncLargeDocument(unittest.TestCase):
    """大文档必须走后台线程：有进度、可取消、不卡事件循环。"""

    @classmethod
    def setUpClass(cls):
        import json as _json

        records = [{"id": i, "name": f"user-{i}", "tags": ["a", "b"]} for i in range(20000)]
        cls.text = _json.dumps({"records": records})
        cls.window = MainWindow()
        cls.window.show()

    @classmethod
    def tearDownClass(cls):
        cls.window.close()

    def _pump(self, seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            QApplication.processEvents()
            time.sleep(0.01)

    def test_async_minify_and_cancel(self):
        from PySide6.QtWidgets import QApplication

        window = type(self).window
        tab = window.new_tab(self.text)
        window.workspace.tabs.setCurrentWidget(tab)
        tab.request_validation()
        self._pump(8.0)
        self.assertTrue(tab.validation(force=False).valid)

        window.workspace.minify_document()
        self._pump(0.3)
        saw_busy = window.status.is_busy()
        for _ in range(1500):
            QApplication.processEvents()
            if not window.status.is_busy():
                break
            time.sleep(0.02)
        self.assertFalse(window.status.is_busy(), "format task never finished")
        self.assertTrue(saw_busy, "large-document format did not show progress")
        self.assertNotIn("\n", tab.text())

        window.workspace.format_document()
        self._pump(0.05)
        window.workspace.cancel_format()
        self._pump(3.0)
        self.assertFalse(window.status.is_busy(), "cancel left the progress bar visible")


class TestUnwrapBanner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = WorkspacePage()
        cls.workspace.show()

    @classmethod
    def tearDownClass(cls):
        cls.workspace.close()

    def test_banner_appears_and_unwraps(self):
        import json as _json

        workspace = type(self).workspace
        wrapped = _json.dumps(_json.dumps('{"data":{"id":1},"code":0}'))
        tab = workspace.add_tab(wrapped)
        tab.validation(force=True)
        self.assertTrue(workspace.unwrap_bar.isVisible())
        workspace.unwrap_button.click()
        self.assertEqual(parse_json(tab.text()).value, {"data": {"id": 1}, "code": 0})
        self.assertIn("\n", tab.text(), "unwrapped document should be formatted")
        self.assertFalse(workspace.unwrap_bar.isVisible())

    def test_banner_hidden_for_plain_json(self):
        workspace = type(self).workspace
        tab = workspace.add_tab('{"a":1}')
        tab.validation(force=True)
        self.assertFalse(workspace.unwrap_bar.isVisible())

    def test_banner_hidden_for_plain_string(self):
        workspace = type(self).workspace
        tab = workspace.add_tab('"just some text"')
        tab.validation(force=True)
        self.assertFalse(workspace.unwrap_bar.isVisible())


if __name__ == "__main__":
    unittest.main(verbosity=2)

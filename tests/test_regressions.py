"""数据保全、异步竞态与页面后台任务回归。"""

import threading
import time
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QApplication, QWidget

from app.core.jsonformatter import FormatOptions, format_text, minify_text, sort_value
from app.core.jsonparser import MODE_JSONC, parse_json
from app.core.jsonvalidator import validate_text
from app.modules.workspace_page import WorkspacePage
from app.settings.settings import settings
from app.utils.pagejob import run_latest
from app.widgets.tabwidget import EditorTab

_app = QApplication.instance() or QApplication([])


def pump_until(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        _app.processEvents()
        time.sleep(0.005)
    if not condition():
        raise AssertionError("Background callback did not finish")


class FormatterSafety(unittest.TestCase):
    def test_duplicate_keys_are_not_silently_lost(self):
        for text in ('{"a":1,"a":2}', '{"x":{"a":1,"a":2}}'):
            for operation in (format_text, minify_text):
                result = operation(text)
                self.assertFalse(result.ok)
                self.assertEqual(result.error_message, "duplicate_key")
                self.assertEqual(result.output, "")

    def test_explicit_array_sort_is_verified(self):
        text = '{"z":[3,1,2],"a":{"items":[2,1]}}'
        result = format_text(text, FormatOptions(sort_keys=True, sort_arrays=True))
        self.assertTrue(result.semantics_preserved)
        self.assertEqual(parse_json(result.output).value,
                         {"a": {"items": [1, 2]}, "z": [1, 2, 3]})

    def test_nonrecursive_sort_keeps_nested_arrays(self):
        result = format_text('[[3,2],[2,1]]', FormatOptions(
            sort_keys=True, sort_arrays=True, sort_recursive=False))
        self.assertTrue(result.semantics_preserved)
        self.assertEqual(parse_json(result.output).value, [[3, 2], [2, 1]])

    def test_sort_handles_deep_nesting(self):
        value = parse_json('[' * 1500 + '0' + ']' * 1500, max_depth=2000).value
        sorted_value = sort_value(value, sort_arrays=True, max_depth=2000)
        for _ in range(1500):
            self.assertIsNot(sorted_value, value)
            sorted_value, value = sorted_value[0], value[0]
        self.assertEqual(sorted_value, 0)

    def test_comment_count_ignores_comment_like_strings(self):
        result = parse_json('{/* one */ "url":"https://a/*b*/",// two\n"x":1}', MODE_JSONC)
        self.assertTrue(result.ok)
        self.assertEqual(result.stats.comment_count, 2)


class EditorRaceSafety(unittest.TestCase):
    def setUp(self):
        self.workspace = WorkspacePage()
        self.tab = self.workspace.add_tab('{"value":1}')

    def tearDown(self):
        self.workspace.cancel_format()
        for tab in self.workspace.tabs.editors():
            tab.cancel_validation()
        self.workspace.close()
        self.workspace.deleteLater()

    def test_edit_invalidates_inflight_validation_immediately(self):
        old = validate_text(self.tab.text())
        request = self.tab._request_id
        self.tab.set_text('{"value":2}')
        self.tab._on_async_result(old, request)
        self.assertEqual(self.tab.validation().value, {"value": 2})

    def test_superseded_validation_is_cancelled(self):
        task = Mock()
        self.tab._task = task
        self.tab.set_text('{"value":2}')
        task.cancel.assert_called_once()
        self.assertIsNone(self.tab._task)

    def test_inactive_tab_does_not_replace_current_tree(self):
        other = self.workspace.add_tab('{"other":2}')
        other.validation()
        self.tab.validation(force=True)
        self.assertEqual(self.workspace.tree_panel.model.root_value(), {"other": 2})

    def test_text_edit_clears_stale_tree(self):
        self.tab.validation()
        self.tab.set_text('{"value":2}')
        self.assertFalse(self.workspace.tree_panel.model.has_document())

    def test_inactive_format_keeps_history_source(self):
        result = format_text(self.tab.text())
        other = self.workspace.add_tab('{"other":2}')
        other.validation()
        events = []
        self.workspace.history_event.connect(lambda *args: events.append(args))
        self.workspace._apply_format(result, self.tab, 'Format', False)
        self.assertIs(events[0][3], self.tab)
        self.assertEqual(other.text(), '{"other":2}')
        self.assertEqual(self.workspace.tree_panel.model.root_value(), {'other': 2})

    def test_edited_document_rejects_background_format_result(self):
        callbacks = {}
        def start(*args, **kwargs):
            callbacks.update(kwargs)
            return Mock()
        with patch('app.modules.workspace_page.ASYNC_VALIDATE_CHARS', 0), \
                patch('app.modules.workspace_page.start_task', side_effect=start):
            old = self.tab.text()
            self.workspace.format_document()
            self.tab.set_text('{"value":99}')
            callbacks['on_done'](format_text(old))
        self.assertEqual(self.tab.text(), '{"value":99}')

    def test_closed_tab_rejects_background_format_result(self):
        result = format_text(self.tab.text())
        request = self.workspace._format_request_id
        revision = self.tab.editor.document().revision()
        self.workspace.close_tab_at(self.workspace.tabs.indexOf(self.tab))
        self.workspace._finish_format(result, self.tab, "Format", request, revision, 'json')
        self.assertEqual(self.tab.text(), '{"value":1}')

    def test_array_sort_reaches_editor_and_can_be_undone(self):
        self.tab.set_text('[3,1,2]')
        self.workspace._run_format(format_text,
            FormatOptions(sort_keys=True, sort_arrays=True), 'Sorted')
        self.assertEqual(parse_json(self.tab.text()).value, [1, 2, 3])
        self.tab.editor.undo()
        self.assertEqual(self.tab.text(), '[3,1,2]')

    def test_duplicate_tree_cannot_modify_document_even_with_warnings_disabled(self):
        enabled = settings.detect_duplicate_keys
        try:
            settings.detect_duplicate_keys = False
            self.tab.set_text('{"a":1,"a":2}')
            self.tab.validation()
            model = self.workspace.tree_panel.model
            self.assertFalse(model.set_node_value(model.index_for_path(('a',)), 3))
            self.workspace.format_document()
            self.assertEqual(self.tab.text(), '{"a":1,"a":2}')
        finally:
            settings.detect_duplicate_keys = enabled

    def test_jsonc_comment_removal_requires_confirmation(self):
        text = '{/* preserve me */ "a":1}'
        self.tab.document.set_mode(MODE_JSONC)
        self.tab.set_text(text)
        self.tab.validation()
        self.assertTrue(self.workspace.tree_panel.model.read_only)
        with patch('app.modules.workspace_page.dialogs.confirm', return_value=False) as confirm:
            self.workspace.format_document()
            confirm.assert_called_once()
        self.assertEqual(self.tab.text(), text)
        with patch('app.modules.workspace_page.dialogs.confirm', return_value=True):
            self.workspace.format_document()
        self.assertNotIn('preserve me', self.tab.text())
        self.tab.editor.undo()
        self.assertEqual(self.tab.text(), text)


class JobOwner(QWidget):
    status_message = Signal(str, int)


class BackgroundJobs(unittest.TestCase):
    def setUp(self):
        self.owner = JobOwner()

    def tearDown(self):
        self.owner.close()
        self.owner.deleteLater()

    def test_result_is_applied_on_gui_thread(self):
        seen = []
        def compute():
            return QThread.currentThread() != _app.thread()
        def done(value):
            seen.append((value, QThread.currentThread() == _app.thread()))
        run_latest(self.owner, compute, done, lambda: True, background=True)
        pump_until(lambda: bool(seen))
        self.assertEqual(seen, [(True, True)])

    def test_changed_inputs_discard_result(self):
        gate = threading.Event()
        seen, messages = [], []
        self.owner.status_message.connect(lambda *args: messages.append(args))
        try:
            run_latest(self.owner, lambda: (gate.wait(3), 42)[1], seen.append,
                       lambda: False, background=True)
        finally:
            gate.set()
        pump_until(lambda: self.owner._page_job.task is None)
        self.assertEqual(seen, [])
        self.assertGreaterEqual(len(messages), 2)

    def test_new_request_supersedes_running_job(self):
        gate, finished = threading.Event(), threading.Event()
        seen = []
        def old():
            gate.wait(3)
            finished.set()
            return 'old'
        try:
            run_latest(self.owner, old, seen.append, lambda: True, background=True)
            run_latest(self.owner, lambda: 'new', seen.append, lambda: True, background=False)
        finally:
            gate.set()
        pump_until(finished.is_set)
        _app.processEvents()
        self.assertEqual(seen, ['new'])


class BackgroundPageIntegration(unittest.TestCase):
    def test_conversion_diff_stats_query_and_schema(self):
        from app.modules.converter_page import ConverterPage
        from app.modules.diff_page import DiffPage
        from app.modules.stats_page import StatsPage
        from app.modules.jsonpath_page import JsonPathPage
        from app.modules.schema_page import SchemaPage

        converter = ConverterPage()
        converter.input_editor.set_text('{"a":1}')
        diff = DiffPage()
        diff.set_documents('{"a":1}', '{"a":2}')
        stats = StatsPage()
        query = JsonPathPage()
        query.set_document({'a': 1})
        query.expression_edit.setText('$.a')
        schema = SchemaPage()
        schema._instance_value = {'a': 1}
        schema.schema_editor.set_text('{"type":"object","required":["missing"]}')
        cases = [
            (converter, 'converter_page.ASYNC_VALIDATE_CHARS', 1, converter.run_conversion,
             lambda: self.assertTrue(converter._last_result.ok)),
            (diff, 'diff_page.ASYNC_VALIDATE_CHARS', 1, diff.run_diff,
             lambda: self.assertTrue(diff._result.changed)),
            (stats, 'stats_page.ASYNC_VALIDATE_CHARS', 1,
             lambda: stats.set_document('{"a":1}'),
             lambda: self.assertGreater(stats.rows_tree.topLevelItemCount(), 0)),
            (query, 'jsonpath_page.is_large_value', lambda _: True, query.run_query,
             lambda: self.assertEqual(query._matches[0].value, 1)),
            (schema, 'schema_page.is_large_value', lambda _: True, schema.run_validation,
             lambda: self.assertFalse(schema._last_result.valid)),
        ]
        try:
            for page, target, replacement, run, verify in cases:
                with self.subTest(page=type(page).__name__), patch('app.modules.' + target, replacement):
                    run()
                    self.assertIsNotNone(page._page_job.task)
                    pump_until(lambda: page._page_job.task is None)
                    verify()
        finally:
            for page, *_ in cases:
                page.close()
                page.deleteLater()


if __name__ == '__main__':
    unittest.main()

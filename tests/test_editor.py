"""编辑器离屏测试：行号、自动缩进、括号补全/匹配、查找替换、错误标记。"""

from __future__ import annotations

import unittest

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.core.jsonparser import Issue, SEVERITY_ERROR, SEVERITY_WARNING
from app.editor.findreplace import FindReplaceBar
from app.editor.jsoneditor import BRACKET_SCAN_LIMIT, JsonEditor
from app.widgets.theme import DARK_PALETTE

_app = QApplication.instance() or QApplication([])


def make_editor(text: str = "", read_only: bool = False) -> JsonEditor:
    editor = JsonEditor(read_only=read_only)
    editor.apply_theme(DARK_PALETTE)
    if text:
        editor.set_content(text)
    return editor


class TestBasics(unittest.TestCase):
    def test_line_number_width_grows_with_digits(self):
        editor = make_editor("\n".join(str(i) for i in range(10)))
        small = editor.line_number_width()
        editor.set_content("\n".join(str(i) for i in range(10000)))
        self.assertGreater(editor.line_number_width(), small)

    def test_line_numbers_can_be_hidden(self):
        editor = make_editor("{}")
        editor.set_line_numbers_visible(False)
        self.assertEqual(editor.line_number_width(), 0)
        editor.set_line_numbers_visible(True)
        self.assertGreater(editor.line_number_width(), 0)

    def test_goto_offset_and_line(self):
        editor = make_editor('{\n  "a": 1,\n  "b": 2\n}')
        editor.goto_offset(9, 1)
        self.assertEqual(editor.textCursor().selectedText(), "1")
        editor.goto_line(3, 3)
        self.assertEqual(editor.current_line(), 3)
        self.assertEqual(editor.current_column(), 3)

    def test_goto_offset_is_clamped(self):
        editor = make_editor("abc")
        editor.goto_offset(99999)
        self.assertEqual(editor.textCursor().position(), 3)
        editor.goto_offset(-5)
        self.assertEqual(editor.textCursor().position(), 0)

    def test_delete_current_line(self):
        editor = make_editor("one\ntwo\nthree")
        editor.goto_line(2)
        editor.delete_current_line()
        self.assertEqual(editor.toPlainText(), "one\nthree")

    def test_delete_last_line(self):
        editor = make_editor("one\ntwo")
        editor.goto_line(2)
        editor.delete_current_line()
        self.assertEqual(editor.toPlainText(), "one\n")

    def test_zoom_clamps(self):
        editor = make_editor("{}")
        base = editor.font_size()
        for _ in range(40):
            editor.zoom(1)
        self.assertLessEqual(editor.font_size(), 32)
        for _ in range(60):
            editor.zoom(-1)
        self.assertGreaterEqual(editor.font_size(), 8)
        editor.set_font_size(base)

    def test_read_only_editor_ignores_typing(self):
        editor = make_editor("abc", read_only=True)
        QTest.keyClick(editor, Qt.Key_X)
        self.assertEqual(editor.toPlainText(), "abc")


class TestEditingBehaviour(unittest.TestCase):
    def _type(self, editor, text):
        for char in text:
            QTest.keyClick(editor, char)

    def test_auto_close_brackets(self):
        editor = make_editor()
        editor.setFocus()
        self._type(editor, "{")
        self.assertEqual(editor.toPlainText(), "{}")
        self.assertEqual(editor.textCursor().position(), 1)

    def test_auto_close_quotes(self):
        editor = make_editor()
        editor.setFocus()
        self._type(editor, '"')
        self.assertEqual(editor.toPlainText(), '""')

    def test_skip_over_closing_bracket(self):
        editor = make_editor()
        editor.setFocus()
        self._type(editor, "{}")
        self.assertEqual(editor.textCursor().position(), 2)
        self.assertEqual(editor.toPlainText(), "{}")

    def test_backspace_removes_pair(self):
        editor = make_editor()
        editor.setFocus()
        self._type(editor, "[")
        QTest.keyClick(editor, Qt.Key_Backspace)
        self.assertEqual(editor.toPlainText(), "")

    def test_no_auto_close_before_word_char(self):
        editor = make_editor("ab")
        editor.setFocus()
        editor.goto_offset(0)
        self._type(editor, "{")
        self.assertEqual(editor.toPlainText(), "{ab")

    def test_auto_indent_on_enter(self):
        editor = make_editor('{\n  "a": 1\n}')
        editor.setFocus()
        editor.goto_line(2, 9)  # 第 2 行行尾
        QTest.keyClick(editor, Qt.Key_Return)
        self.assertEqual(editor.toPlainText(), '{\n  "a": 1\n  \n}')

    def test_auto_indent_increases_after_opener(self):
        editor = make_editor("{")
        editor.setFocus()
        editor.goto_offset(1)
        QTest.keyClick(editor, Qt.Key_Return)
        self.assertEqual(editor.toPlainText(), "{\n    ")

    def test_enter_between_braces_splits_three_lines(self):
        editor = make_editor("{}")
        editor.setFocus()
        editor.goto_offset(1)
        QTest.keyClick(editor, Qt.Key_Return)
        self.assertEqual(editor.toPlainText(), "{\n    \n}")
        self.assertEqual(editor.textCursor().position(), 6)

    def test_tab_indents_selection(self):
        editor = make_editor("a\nb")
        cursor = editor.textCursor()
        cursor.select(QTextCursor.Document)
        editor.setTextCursor(cursor)
        QTest.keyClick(editor, Qt.Key_Tab)
        self.assertEqual(editor.toPlainText(), "    a\n    b")

    def test_shift_tab_outdents(self):
        editor = make_editor("    a\n    b")
        cursor = editor.textCursor()
        cursor.select(QTextCursor.Document)
        editor.setTextCursor(cursor)
        QTest.keyClick(editor, Qt.Key_Tab, Qt.ShiftModifier)
        self.assertEqual(editor.toPlainText(), "a\nb")

    def test_auto_behaviours_can_be_disabled(self):
        editor = make_editor()
        editor.setFocus()
        editor.set_auto_close_brackets(False)
        self._type(editor, "{")
        self.assertEqual(editor.toPlainText(), "{")
        editor.set_content("")
        editor.set_auto_indent(False)
        editor.set_content("  x")
        editor.goto_offset(3)
        QTest.keyClick(editor, Qt.Key_Return)
        self.assertEqual(editor.toPlainText(), "  x\n")


class TestBracketMatching(unittest.TestCase):
    def test_root_brace_matches_last_brace(self):
        text = '{\n  "a": {\n    "b": 1\n  }\n}'
        editor = make_editor(text)
        editor.goto_offset(0)
        spans = editor._bracket_spans()
        self.assertEqual(sorted(spans), [(0, 1), (len(text) - 1, len(text))])

    def test_inner_brace_matches_its_own_closer(self):
        text = '{"a": {"b": [1, 2]}}'
        editor = make_editor(text)
        editor.goto_offset(6)  # 内层 {
        spans = editor._bracket_spans()
        self.assertIn((6, 7), spans)
        self.assertIn((18, 19), spans)

    def test_braces_inside_strings_are_ignored(self):
        text = '{"a": "}{" , "b": [1]}'
        editor = make_editor(text)
        editor.goto_offset(0)
        spans = editor._bracket_spans()
        self.assertEqual(sorted(spans), [(0, 1), (len(text) - 1, len(text))])

    def test_unbalanced_returns_no_spans(self):
        editor = make_editor('{"a": [1, 2}')
        editor.goto_offset(6)
        self.assertEqual(editor._bracket_spans(), [])

    def test_large_documents_skip_matching(self):
        editor = make_editor("{}")
        editor._large_document = True
        editor.goto_offset(0)
        self.assertEqual(editor._bracket_spans(), [])
        editor._large_document = False

    def test_scan_limit_is_respected(self):
        # 构造一个超过扫描上限才闭合的文档，必须返回 -1 而不是卡死
        text = "[" + " " * (BRACKET_SCAN_LIMIT + 10) + "]"
        editor = make_editor(text)
        self.assertEqual(editor._find_matching_bracket(text, 0), -1)


class TestErrorMarkers(unittest.TestCase):
    def _issue(self, line, column, offset, severity=SEVERITY_ERROR, length=1):
        return Issue(code="trailing_comma", severity=severity, offset=offset, length=length,
                     line=line, column=column)

    def test_markers_are_recorded(self):
        editor = make_editor('{\n"a":1,\n}')
        editor.set_issues([self._issue(2, 6, 6), self._issue(3, 1, 8, SEVERITY_WARNING)])
        markers = editor.markers().all()
        self.assertEqual(markers[2], "error")
        self.assertEqual(markers[3], "warning")

    def test_error_overrides_warning_on_same_line(self):
        editor = make_editor("{}")
        editor.set_issues([self._issue(1, 1, 0, SEVERITY_WARNING), self._issue(1, 1, 0)])
        self.assertEqual(editor.markers().get(1), "error")

    def test_clear_issues(self):
        editor = make_editor("{}")
        editor.set_issues([self._issue(1, 1, 0)])
        editor.clear_issues()
        self.assertEqual(len(editor.markers()), 0)

    def test_gutter_paints_without_crash(self):
        editor = make_editor('{\n"a":1,\n}')
        editor.set_issues([self._issue(2, 6, 6)])
        editor.resize(400, 200)
        editor.show()
        QTest.qWait(30)
        pixmap = editor.grab()
        self.assertFalse(pixmap.isNull())


class TestHighlighter(unittest.TestCase):
    def test_colors_are_applied_per_token(self):
        editor = make_editor('{"key": "value", "n": 1.5, "t": true, "z": null}')
        editor.show()
        QTest.qWait(30)
        document = editor.document()
        block = document.firstBlock()
        formats = set()
        for rng in block.layout().formats():
            color = rng.format.foreground().color()
            if color.isValid() and color.name() != "#000000":
                formats.add(color.name().lower())
        palette = DARK_PALETTE.as_tokens()
        self.assertIn(palette["SYN_KEY"].lower(), formats)
        self.assertIn(palette["SYN_STRING"].lower(), formats)
        self.assertIn(palette["SYN_NUMBER"].lower(), formats)
        self.assertIn(palette["SYN_BOOL"].lower(), formats)
        self.assertIn(palette["SYN_NULL"].lower(), formats)

    def test_highlighter_can_be_disabled(self):
        editor = make_editor('{"a":1}')
        editor.set_highlighting_enabled(False)
        self.assertFalse(editor.highlighting_enabled())
        editor.set_highlighting_enabled(True)
        self.assertTrue(editor.highlighting_enabled())

    def test_multiline_block_comment_state(self):
        editor = make_editor('/* start\nstill comment */\n{"a":1}')
        editor.show()
        QTest.qWait(20)
        document = editor.document()
        first = document.findBlockByNumber(0)
        second = document.findBlockByNumber(1)
        third = document.findBlockByNumber(2)
        self.assertEqual(first.userState(), 1)    # 块注释在本行未结束
        self.assertEqual(second.userState(), 0)   # 本行内注释已结束
        self.assertEqual(third.userState(), 0)


class TestFindReplace(unittest.TestCase):
    def test_find_next_and_count(self):
        editor = make_editor('{"a":1,"a":2,"a":3}')
        bar = FindReplaceBar(editor, editor)
        editor.attach_find_bar(bar)
        bar.show_bar(initial="a")
        self.assertEqual(len(bar._matches), 3)  # 三个 key
        self.assertTrue(bar.find_next())
        self.assertEqual(editor.textCursor().selectedText(), "a")

    def test_find_case_sensitive(self):
        editor = make_editor('{"Key":1,"key":2}')
        bar = FindReplaceBar(editor, editor)
        bar.show_bar(initial="key")
        total_insensitive = len(bar._matches)
        bar.case_check.setChecked(True)
        self.assertLess(len(bar._matches), total_insensitive)
        self.assertEqual(len(bar._matches), 1)

    def test_find_regex_with_groups(self):
        editor = make_editor('{"a":1,"b":22}')
        bar = FindReplaceBar(editor, editor)
        bar.show_bar(replace=True, initial=r'"(\w+)":\s*(\d+)')
        bar.regex_check.setChecked(True)
        bar.replace_edit.setText(r"\1=\2")
        count = bar.replace_all()
        self.assertEqual(count, 2)
        self.assertEqual(editor.toPlainText(), "{a=1,b=22}")

    def test_replace_all_plain_text(self):
        editor = make_editor("x y x")
        bar = FindReplaceBar(editor, editor)
        bar.show_bar(replace=True, initial="x")
        bar.replace_edit.setText("Z")
        self.assertEqual(bar.replace_all(), 2)
        self.assertEqual(editor.toPlainText(), "Z y Z")

    def test_invalid_regex_is_reported_not_crashed(self):
        editor = make_editor("abc")
        bar = FindReplaceBar(editor, editor)
        bar.show_bar(initial="(")
        bar.regex_check.setChecked(True)
        self.assertFalse(bar.error_label.isHidden())
        self.assertEqual(bar._matches, [])

    def test_escape_toggles(self):
        editor = make_editor("axb a.b")
        bar = FindReplaceBar(editor, editor)
        bar.show_bar(initial="a.b")
        self.assertEqual(len(bar._matches), 1)   # 普通模式下 . 被转义
        bar.regex_check.setChecked(True)
        self.assertEqual(len(bar._matches), 2)   # 正则模式下 . 是通配符

    def test_bar_hides_and_clears_highlights(self):
        editor = make_editor("aaa")
        bar = FindReplaceBar(editor, editor)
        editor.attach_find_bar(bar)
        bar.show_bar(initial="a")
        self.assertFalse(bar.isHidden())
        bar.hide_bar()
        self.assertTrue(bar.isHidden())
        self.assertEqual(editor._find_spans, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

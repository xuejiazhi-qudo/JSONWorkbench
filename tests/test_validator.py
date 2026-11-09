"""校验与修复建议测试：重点是"建议不改数据、必须用户确认"。"""

from __future__ import annotations

import unittest

from app.core.jsonvalidator import (
    Fix,
    apply_fix,
    auto_fix,
    issue_codes,
    propose_fix,
    quick_check,
    summarize_warnings,
    validate_text,
)
from app.core.jsonparser import MODE_JSON, MODE_JSONC


class TestValidation(unittest.TestCase):
    def test_valid_document(self):
        result = validate_text('{"a":[1,2],"b":null}')
        self.assertTrue(result.valid)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.value, {"a": [1, 2], "b": None})
        self.assertEqual(result.stats.node_count, 5)
        self.assertGreaterEqual(result.elapsed_ms, 0.0)

    def test_invalid_document_reports_single_error_with_position(self):
        result = validate_text('{\n  "a": 1,\n}')
        self.assertFalse(result.valid)
        self.assertEqual(result.errors[0].code, "trailing_comma")
        self.assertEqual(result.errors[0].line, 2)
        self.assertEqual(result.errors[0].column, 9)

    def test_warnings_are_separate_from_errors(self):
        result = validate_text('{"a":1,"a":2}')
        self.assertTrue(result.valid)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.duplicate_key_count, 1)
        self.assertEqual(summarize_warnings(result.warnings), {"duplicate_key": 1})

    def test_mode_switch(self):
        self.assertFalse(validate_text('// c\n{}', MODE_JSON).valid)
        self.assertTrue(validate_text('// c\n{}', MODE_JSONC).valid)

    def test_quick_check(self):
        ok, code, line, column = quick_check('{"a":}')
        self.assertFalse(ok)
        self.assertEqual(code, "missing_value")
        self.assertEqual(line, 1)
        self.assertEqual(column, 6)
        self.assertEqual(quick_check("{}"), (True, None, 0, 0))

    def test_empty_text(self):
        result = validate_text("")
        self.assertFalse(result.valid)
        self.assertEqual(result.error.code, "empty_input")

    def test_issue_codes_are_stable_and_documented(self):
        codes = issue_codes()
        self.assertIn("trailing_comma", codes)
        self.assertIn("duplicate_key", codes)
        self.assertEqual(len(codes), len(set(codes)))
        from app.core.jsonparser import ISSUE_MESSAGES, Issue, SEVERITY_ERROR, describe_issue

        for code in codes:
            title, message, suggestion = describe_issue(
                Issue(code=code, severity=SEVERITY_ERROR, offset=0, length=0, line=1, column=1))
            self.assertTrue(title, code)
            self.assertTrue(message, code)
            self.assertIn(code, ISSUE_MESSAGES)


class TestFixProposals(unittest.TestCase):
    def fix_for(self, text, mode=MODE_JSON):
        result = validate_text(text, mode)
        self.assertFalse(result.valid, f"expected invalid: {text!r}")
        fix = propose_fix(text, result.error)
        self.assertIsNotNone(fix, f"no fix proposed for {text!r}")
        return fix

    def assert_fix(self, text, expected, mode=MODE_JSON):
        fix = self.fix_for(text, mode)
        fixed = apply_fix(text, fix)
        self.assertEqual(fixed, expected)
        result = validate_text(fixed, mode)
        self.assertTrue(result.valid, f"{fixed!r} still invalid: {result.error}")
        return fix

    def test_trailing_comma(self):
        fix = self.assert_fix('{"a":1,}', '{"a":1}')
        self.assertEqual(fix.label, "Remove the trailing comma")

    def test_trailing_comma_in_array(self):
        self.assert_fix("[1,2,]", "[1,2]")

    def test_unquoted_key(self):
        self.assert_fix('{name:"x"}', '{"name":"x"}')

    def test_single_quoted_string_key_and_value(self):
        self.assert_fix("{'a':1}", '{"a":1}')
        self.assert_fix('{"a":\'b\'}', '{"a":"b"}')

    def test_single_quoted_string_containing_double_quote(self):
        # propose_fix 一次只修一处：这里只有值是单引号字符串
        source = '{"a":' + "'" + 'say "hi"' + "'" + '}'
        expected = '{"a":"say \\"hi\\""}'
        fix = self.assert_fix(source, expected)
        self.assertIn('\\"', fix.insert)

    def test_auto_fix_handles_both_key_and_value_single_quotes(self):
        result = auto_fix("{'a':'say \"hi\"'}")
        self.assertTrue(result.ok, result.remaining)
        self.assertEqual(result.text, '{"a":"say \\"hi\\""}')
        self.assertEqual(len(result.applied), 2)

    def test_line_comment_removed_in_strict_mode(self):
        self.assert_fix('// note\n{"a":1}', '\n{"a":1}')

    def test_block_comment_removed(self):
        self.assert_fix('/* note */{"a":1}', '{"a":1}')

    def test_unclosed_block_comment_in_jsonc(self):
        fix = self.fix_for('{"a":1} /* oops', MODE_JSONC)
        self.assertEqual(apply_fix('{"a":1} /* oops', fix), '{"a":1} /* oops*/')

    def test_python_literals(self):
        self.assert_fix("[True]", "[true]")
        self.assert_fix("[False]", "[false]")
        self.assert_fix("[None]", "[null]")
        self.assert_fix("[NULL]", "[null]")
        self.assert_fix("[undefined]", "[null]")

    def test_missing_colon(self):
        # 插入点就是报错偏移（值开始处），原有空格被保留，结果仍是合法 JSON
        self.assert_fix('{"a" 1}', '{"a" :1}')

    def test_missing_comma(self):
        self.assert_fix('{"a":1 "b":2}', '{"a":1 ,"b":2}')

    def test_bracket_mismatch(self):
        self.assert_fix('{"a":1]', '{"a":1}')
        self.assert_fix("[1}", "[1]")

    def test_unclosed_string_at_newline(self):
        self.assert_fix('{"a":"text\n}', '{"a":"text"\n}')

    def test_control_character_escaped(self):
        fix = self.assert_fix('{"a":"x\ty"}', '{"a":"x\\ty"}')
        self.assertIn("\\t", fix.insert)

    def test_missing_value_inserts_null(self):
        self.assert_fix('{"a":}', '{"a":null}')

    def test_no_fix_for_unfixable_codes(self):
        cases = [
            ("", "empty_input"),
            ('{"a":1}{"b":2}', "extra_data"),
            ("[01]", "invalid_number"),
            (r'{"a":"\x"}', "invalid_escape"),
            (r'{"a":"\uZZZZ"}', "invalid_unicode_escape"),
        ]
        for text, code in cases:
            with self.subTest(text=text):
                result = validate_text(text)
                self.assertEqual(result.error.code, code)
                self.assertIsNone(propose_fix(text, result.error))

    def test_nan_has_no_auto_fix(self):
        # NaN 转成 null 会改变数据含义，只给建议不给自动修复
        result = validate_text("[NaN]")
        self.assertEqual(result.error.code, "invalid_literal")
        self.assertIsNone(propose_fix("[NaN]", result.error))

    def test_duplicate_key_is_never_auto_fixed(self):
        result = validate_text('{"a":1,"a":2}')
        self.assertTrue(result.valid)
        for warning in result.warnings:
            self.assertIsNone(propose_fix('{"a":1,"a":2}', warning))

    def test_fix_never_mutates_original_text(self):
        text = '{"a":1,}'
        fix = propose_fix(text, validate_text(text).error)
        apply_fix(text, fix)
        self.assertEqual(text, '{"a":1,}')


class TestAutoFix(unittest.TestCase):
    def test_multiple_issues_fixed_in_sequence(self):
        text = "{\n  name: 'x',\n  // comment\n  'b': True,\n}"
        result = auto_fix(text)
        self.assertTrue(result.ok, result.remaining)
        self.assertGreaterEqual(len(result.applied), 4)
        self.assertFalse(result.unchanged)
        from app.core.jsonparser import parse_json
        self.assertEqual(parse_json(result.text).value, {"name": "x", "b": True})

    def test_already_valid_is_unchanged(self):
        result = auto_fix('{"a":1}')
        self.assertTrue(result.ok)
        self.assertTrue(result.unchanged)
        self.assertEqual(result.applied, ())

    def test_unfixable_document_reports_remaining_error(self):
        result = auto_fix('{"a":1}{"b":2}')
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.remaining)
        self.assertEqual(result.remaining.code, "extra_data")

    def test_auto_fix_respects_round_limit(self):
        text = "{'a':1,'b':2,'c':3,'d':4}"
        result = auto_fix(text, max_rounds=2)
        self.assertLessEqual(result.rounds, 2)
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.remaining)
        self.assertEqual(len(result.applied), 2)

    def test_auto_fix_does_not_touch_duplicate_keys(self):
        result = auto_fix('{"a":1,"a":2}')
        self.assertTrue(result.ok)
        self.assertTrue(result.unchanged)


if __name__ == "__main__":
    unittest.main(verbosity=2)

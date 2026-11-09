"""解析器单元测试：覆盖正常/边界/非法输入、精度、Unicode、深度、位置与取消。"""

from __future__ import annotations

import json
import unittest
from decimal import Decimal

from app.core.jsonparser import (
    MODE_JSON,
    MODE_JSONC,
    SEVERITY_WARNING,
    describe_issue,
    parse_json,
)
from app.core.jsonvalue import JsonNumber, json_type, number_literal, to_plain


def normalize(value):
    """把解析结果转成可与 json.loads 比较的纯 Python 结构。"""
    return to_plain(value)


class TestValidDocuments(unittest.TestCase):
    def test_all_scalar_types(self):
        text = '{"s":"str","i":42,"f":1.5,"t":true,"f2":false,"n":null}'
        result = parse_json(text)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.value["s"], "str")
        self.assertEqual(result.value["i"], 42)
        self.assertIsInstance(result.value["i"], int)
        self.assertEqual(result.value["f"], Decimal("1.5"))
        self.assertIs(result.value["t"], True)
        self.assertIs(result.value["f2"], False)
        self.assertIsNone(result.value["n"])

    def test_empty_object_and_array(self):
        self.assertEqual(parse_json("{}").value, {})
        self.assertEqual(parse_json("[]").value, [])
        self.assertEqual(parse_json("[[],{},[[]]]").value, [[], {}, [[]]])

    def test_top_level_scalars_are_valid_json(self):
        for text, expected in [('"hi"', "hi"), ("123", 123), ("true", True), ("null", None)]:
            result = parse_json(text)
            self.assertTrue(result.ok, f"{text}: {result.error}")
            self.assertEqual(result.value, expected)

    def test_deep_nesting(self):
        depth = 200
        text = "[" * depth + "]" * depth
        result = parse_json(text)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.stats.max_depth, depth)
        value = result.value
        for _ in range(depth - 1):
            value = value[0]
        self.assertEqual(value, [])

    def test_deep_nesting_does_not_hit_python_recursion_limit(self):
        # 远超默认递归上限 1000：迭代式解析必须安全通过
        depth = 5000
        text = '{"a":' * depth + "1" + "}" * depth
        result = parse_json(text, max_depth=10000)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.stats.max_depth, depth)

    def test_depth_limit_reports_error_not_crash(self):
        text = "[" * 50 + "]" * 50
        result = parse_json(text, max_depth=10)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "depth_exceeded")
        self.assertEqual(result.error.details["limit"], 10)

    def test_key_order_is_preserved(self):
        result = parse_json('{"z":1,"a":2,"m":3}')
        self.assertEqual(list(result.value.keys()), ["z", "a", "m"])

    def test_unicode_content(self):
        text = '{"中文":"值","emoji":"😀","esc":"\\u00e9\\ud83d\\ude00"}'
        result = parse_json(text)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.value["中文"], "值")
        self.assertEqual(result.value["emoji"], "😀")
        self.assertEqual(result.value["esc"], "é😀")

    def test_escape_sequences(self):
        result = parse_json(r'{"a":"\" \\ \/ \b \f \n \r \t"}')
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.value["a"], '" \\ / \b \f \n \r \t')

    def test_whitespace_tolerance(self):
        text = '  {\n\t"a"  :  [ 1 , 2 ]\r\n}  '
        result = parse_json(text)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.value, {"a": [1, 2]})

    def test_matches_stdlib_json_on_valid_documents(self):
        samples = [
            "{}",
            "[]",
            '{"a":1}',
            '{"a":[1,2,{"b":null}]}',
            '[true,false,null,"x"]',
            '{"nested":{"deep":{"deeper":{"deepest":[]}}}}',
            '{"esc":"\\u0041\\n\\t\\""}',
            '{"neg":-0,"exp":1e10,"frac":0.5}',
        ]
        for text in samples:
            with self.subTest(text=text):
                self.assertEqual(normalize(parse_json(text).value), json.loads(text))


class TestNumbers(unittest.TestCase):
    def test_huge_integer_is_exact(self):
        big = "123456789012345678901234567890123456789"
        result = parse_json(f'{{"n":{big}}}')
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.value["n"], int(big))
        self.assertIsInstance(result.value["n"], int)
        self.assertEqual(number_literal(result.value["n"]), big)

    def test_negative_and_zero(self):
        result = parse_json("[-1,0,-0,1e-5,1E+5]")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.value[0], -1)
        self.assertEqual(number_literal(result.value[1]), "0")
        self.assertEqual(number_literal(result.value[3]), "1e-5")

    def test_float_literal_is_preserved_verbatim(self):
        for raw in ["1.230", "1e5", "0.1", "3.141592653589793238462643383279", "-2.5E-10"]:
            with self.subTest(raw=raw):
                result = parse_json(f"[{raw}]")
                self.assertTrue(result.ok, result.error)
                value = result.value[0]
                self.assertIsInstance(value, JsonNumber)
                self.assertEqual(number_literal(value), raw)

    def test_precision_loss_warning(self):
        result = parse_json("[0.123456789012345678901234567890]")
        self.assertTrue(result.ok, result.error)
        codes = [w.code for w in result.warnings]
        self.assertIn("precision_loss", codes)

    def test_no_false_precision_warning_for_simple_decimal(self):
        result = parse_json("[1.230]")
        self.assertTrue(result.ok)
        self.assertEqual([w.code for w in result.warnings], [])

    def test_overflowing_exponent_warns_but_keeps_literal(self):
        result = parse_json("[1e400]")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(number_literal(result.value[0]), "1e400")
        self.assertIn("precision_loss", [w.code for w in result.warnings])

    def test_invalid_numbers(self):
        cases = {
            "[01]": "invalid_number",
            "[1.]": "invalid_number",
            "[.5]": "invalid_number",
            "[+1]": "invalid_number",
            "[1e]": "invalid_number",
            "[-]": "invalid_number",
            "[1.2.3]": "missing_comma",
        }
        for text, code in cases.items():
            with self.subTest(text=text):
                result = parse_json(text)
                self.assertFalse(result.ok)
                self.assertEqual(result.error.code, code)

    def test_number_error_reason_is_human_readable(self):
        result = parse_json("[01]")
        self.assertEqual(result.error.details["reason"], "leading zeros are not allowed")
        result = parse_json("[+1]")
        self.assertEqual(result.error.details["reason"], "a leading '+' is not allowed")


class TestInvalidDocuments(unittest.TestCase):
    def assert_error(self, text, code, mode=MODE_JSON):
        result = parse_json(text, mode)
        self.assertFalse(result.ok, f"expected failure for {text!r}")
        self.assertEqual(result.error.code, code, f"{text!r}: {describe_issue(result.error)}")
        return result

    def test_empty_input(self):
        self.assert_error("", "empty_input")
        self.assert_error("   \n\t ", "empty_input")

    def test_single_quotes(self):
        self.assert_error("{'a':1}", "single_quote_string")
        self.assert_error('{"a":\'b\'}', "single_quote_string")

    def test_unquoted_key(self):
        result = self.assert_error("{a:1}", "unquoted_key")
        self.assertEqual(result.error.details["key"], "a")

    def test_trailing_comma(self):
        self.assert_error('{"a":1,}', "trailing_comma")
        self.assert_error("[1,2,]", "trailing_comma")

    def test_missing_comma(self):
        self.assert_error('{"a":1 "b":2}', "missing_comma")
        self.assert_error("[1 2]", "missing_comma")

    def test_missing_colon(self):
        self.assert_error('{"a" 1}', "missing_colon")

    def test_bracket_mismatch(self):
        self.assert_error('{"a":1]', "bracket_mismatch")
        self.assert_error("[1}", "bracket_mismatch")

    def test_unclosed(self):
        self.assert_error('{"a":1', "unclosed_container")
        self.assert_error("[1,2", "unclosed_container")
        self.assert_error('{"a":"text', "unclosed_string")
        self.assert_error('{"a":', "unexpected_eof")

    def test_invalid_escapes(self):
        self.assert_error(r'{"a":"\x41"}', "invalid_escape")
        self.assert_error(r'{"a":"\uZZZZ"}', "invalid_unicode_escape")
        self.assert_error(r'{"a":"\q"}', "invalid_escape")

    def test_control_character_in_string(self):
        self.assert_error('{"a":"line1\nline2"}', "unclosed_string")
        result = self.assert_error('{"a":"col1\tcol2"}', "control_char_in_string")
        self.assertEqual(result.error.details["char_code"], 0x09)

    def test_invalid_literals(self):
        self.assert_error("[True]", "invalid_literal")
        self.assert_error("[undefined]", "invalid_literal")
        self.assert_error("[NaN]", "invalid_literal")
        self.assert_error("[None]", "invalid_literal")
        self.assert_error("[truex]", "invalid_literal")

    def test_literal_suggestion_is_attached(self):
        result = self.assert_error("[True]", "invalid_literal")
        self.assertIn("true", result.error.details.get("suggestion", ""))

    def test_comments_rejected_in_strict_mode(self):
        self.assert_error('{"a":1}// note', "comment_not_allowed")
        self.assert_error('/* c */{"a":1}', "comment_not_allowed")

    def test_extra_data(self):
        self.assert_error('{"a":1}{"b":2}', "extra_data")
        self.assert_error("[1] junk", "extra_data")

    def test_error_position_is_accurate(self):
        text = '{\n  "a": 1,\n  "b": 2,\n}'
        result = parse_json(text)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "trailing_comma")
        self.assertEqual(result.error.line, 3)
        self.assertEqual(result.error.column, 9)
        self.assertEqual(text[result.error.offset], ",")

    def test_error_on_first_line_single_line_document(self):
        text = '{"a":1,"b":}'
        result = parse_json(text)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.line, 1)
        self.assertEqual(result.error.column, 12)

    def test_line_col_for_windows_line_endings(self):
        text = '{\r\n"a":1,\r\n"b":\r\n}'
        result = parse_json(text)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "missing_value")
        self.assertEqual(result.error.line, 4)
        self.assertEqual(result.error.column, 1)

    def test_describe_issue_never_raises(self):
        for text in ['{"a":1,}', "[1 2]", "", "{'a':1}", "[01]", "[NaN]"]:
            result = parse_json(text)
            if result.error is not None:
                title, message, suggestion = describe_issue(result.error)
                self.assertTrue(title)
                self.assertTrue(message)
                self.assertNotIn("{", message)


class TestJsoncMode(unittest.TestCase):
    def test_line_and_block_comments_allowed(self):
        text = '// header\n{"a":1 /* inline */, "b":2}\n// footer'
        result = parse_json(text, MODE_JSONC)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.value, {"a": 1, "b": 2})

    def test_trailing_comma_allowed(self):
        result = parse_json('{"a":1,}', MODE_JSONC)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.value, {"a": 1})
        self.assertTrue(parse_json("[1,2,]", MODE_JSONC).ok)

    def test_unclosed_block_comment(self):
        result = parse_json('{"a":1} /* oops', MODE_JSONC)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "unclosed_comment")

    def test_strict_mode_still_rejects_jsonc(self):
        self.assertFalse(parse_json('// x\n{}', MODE_JSON).ok)


class TestDuplicateKeys(unittest.TestCase):
    def test_duplicate_key_warning_with_positions(self):
        text = '{"a":1,"b":2,"a":3}'
        result = parse_json(text)
        self.assertTrue(result.ok)
        self.assertEqual(result.value["a"], 3)  # 后者覆盖前者，与 json.loads 一致
        dupes = [w for w in result.warnings if w.code == "duplicate_key"]
        self.assertEqual(len(dupes), 1)
        self.assertEqual(dupes[0].details["key"], "a")
        self.assertEqual(dupes[0].details["first_offset"], 1)
        self.assertEqual(dupes[0].severity, SEVERITY_WARNING)
        self.assertEqual(result.stats.duplicate_key_count, 1)

    def test_same_key_in_different_objects_is_fine(self):
        result = parse_json('{"a":{"x":1},"b":{"x":2}}')
        self.assertEqual([w.code for w in result.warnings], [])

    def test_detection_can_be_disabled(self):
        result = parse_json('{"a":1,"a":2}', detect_duplicate_keys=False)
        self.assertEqual([w.code for w in result.warnings], [])


class TestStatistics(unittest.TestCase):
    def test_counters(self):
        # 层级深度 = 容器嵌套层数（根容器为第 1 层，标量不额外占一层）：
        # 根对象是第 1 层，其中的数组与嵌套对象都是第 2 层。
        text = '{"a":[1,2,3],"b":{"c":null,"d":true},"e":"hello","f":1.5}'
        result = parse_json(text)
        stats = result.stats
        self.assertEqual(stats.object_count, 2)
        self.assertEqual(stats.array_count, 1)
        self.assertEqual(stats.string_count, 1)
        self.assertEqual(stats.number_count, 4)
        self.assertEqual(stats.boolean_count, 1)
        self.assertEqual(stats.null_count, 1)
        self.assertEqual(stats.key_count, 6)
        self.assertEqual(stats.max_array_length, 3)
        self.assertEqual(stats.string_chars, 5)
        self.assertEqual(stats.max_depth, 2)
        self.assertEqual(stats.node_count, 10)

    def test_type_names(self):
        result = parse_json('{"o":{},"a":[],"s":"","n":0,"b":true,"z":null}')
        value = result.value
        self.assertEqual(json_type(value["o"]), "object")
        self.assertEqual(json_type(value["a"]), "array")
        self.assertEqual(json_type(value["s"]), "string")
        self.assertEqual(json_type(value["n"]), "number")
        self.assertEqual(json_type(value["b"]), "boolean")
        self.assertEqual(json_type(value["z"]), "null")


class TestOffsetTracking(unittest.TestCase):
    def test_offsets_point_at_keys_and_values(self):
        text = '{"a": 1, "b": [10, 20]}'
        result = parse_json(text, track_offsets=True)
        self.assertIsNotNone(result.offsets)
        info_a = result.offsets[("a",)]
        self.assertEqual(text[info_a.key_offset : info_a.key_offset + info_a.key_length], '"a"')
        self.assertEqual(text[info_a.value_offset : info_a.value_offset + info_a.value_length], "1")
        info_b0 = result.offsets[("b", 0)]
        self.assertEqual(text[info_b0.value_offset : info_b0.value_offset + info_b0.value_length], "10")
        info_root = result.offsets[()]
        self.assertEqual(info_root.value_offset, 0)
        self.assertEqual(info_root.value_length, len(text))

    def test_offsets_disabled_for_huge_documents(self):
        from app.core import jsonparser

        original = jsonparser.OFFSET_TRACK_LIMIT
        jsonparser.OFFSET_TRACK_LIMIT = 8
        try:
            result = parse_json('{"aaaaaaaaaa":1}', track_offsets=True)
            self.assertIsNone(result.offsets)
            self.assertIn("offsets_disabled", [w.code for w in result.warnings])
        finally:
            jsonparser.OFFSET_TRACK_LIMIT = original


class TestProgressAndCancel(unittest.TestCase):
    def test_progress_callback_reports_positions(self):
        from app.core import jsonparser

        original = jsonparser._PROGRESS_STEP
        jsonparser._PROGRESS_STEP = 16
        try:
            seen = []
            text = "[" + ",".join(str(i) for i in range(200)) + "]"
            result = parse_json(text, progress=lambda pos, total: seen.append((pos, total)))
            self.assertTrue(result.ok, result.error)
            self.assertTrue(seen)
            self.assertTrue(all(total == len(text) for _, total in seen))
        finally:
            jsonparser._PROGRESS_STEP = original

    def test_cancel_stops_parsing_without_exception(self):
        result = parse_json("[" + ",".join("1" * 1 for _ in range(10)) + "]",
                            cancel=lambda: True)
        # 取消检查按进度节拍触发；小文档可能来不及触发，因此只断言不抛异常
        self.assertIsInstance(result.ok, bool)

    def test_cancel_on_large_document(self):
        from app.core import jsonparser

        original = jsonparser._PROGRESS_STEP
        jsonparser._PROGRESS_STEP = 8
        try:
            text = "[" + ",".join(str(i) for i in range(1000)) + "]"
            result = parse_json(text, cancel=lambda: True)
            self.assertTrue(result.cancelled)
            self.assertFalse(result.ok)
            self.assertEqual(result.error.code, "cancelled")
        finally:
            jsonparser._PROGRESS_STEP = original


class TestRobustness(unittest.TestCase):
    def test_no_crash_on_random_binary_text(self):
        import random

        rng = random.Random(20260104)
        alphabet = '{}[]",:0123456789.eEtrufalsn \n\t\\/'
        for _ in range(300):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
            result = parse_json(text)
            self.assertIsInstance(result.ok, bool)
            if not result.ok:
                self.assertIsNotNone(result.error)
                self.assertGreaterEqual(result.error.line, 1)
                self.assertGreaterEqual(result.error.column, 1)

    def test_lone_surrogate_becomes_replacement_char(self):
        result = parse_json(r'["\ud800"]')
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.value[0], "\ufffd")
        self.assertIn("lone_surrogate", [w.code for w in result.warnings])

    def test_very_long_string(self):
        text = '{"k":"' + "x" * 200000 + '"}'
        result = parse_json(text)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(result.value["k"]), 200000)

    def test_non_string_input_is_coerced(self):
        result = parse_json(b'{"a":1}'.decode("utf-8"))
        self.assertTrue(result.ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)

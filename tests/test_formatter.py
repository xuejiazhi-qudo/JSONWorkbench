"""格式化/压缩/排序测试，重点是"格式化绝不改变数据"的一致性验证。"""

from __future__ import annotations

import json
import unittest
from decimal import Decimal

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
    indent_from_choice,
    json_equal,
    minify,
    minify_text,
    sort_value,
)
from app.core.jsonparser import parse_json

SAMPLE = '{"z":1,"a":[1,2,{"b":null,"A":true}],"m":"文本","n":1.230}'


class TestFormatting(unittest.TestCase):
    def test_default_is_4_spaces(self):
        result = format_text('{"a":1,"b":[1,2]}')
        self.assertTrue(result.ok, result.error_message)
        self.assertEqual(result.output, '{\n    "a": 1,\n    "b": [\n        1,\n        2\n    ]\n}')

    def test_indent_choices(self):
        text = '{"a":{"b":1}}'
        expectations = {
            INDENT_2: '{\n  "a": {\n    "b": 1\n  }\n}',
            INDENT_4: '{\n    "a": {\n        "b": 1\n    }\n}',
            INDENT_8: '{\n        "a": {\n                "b": 1\n        }\n}',
            INDENT_TAB: '{\n\t"a": {\n\t\t"b": 1\n\t}\n}',
        }
        for indent, expected in expectations.items():
            with self.subTest(indent=repr(indent)):
                self.assertEqual(format_text(text, FormatOptions(indent=indent)).output, expected)

    def test_indent_from_choice(self):
        self.assertEqual(indent_from_choice("2"), INDENT_2)
        self.assertEqual(indent_from_choice("8"), INDENT_8)
        self.assertEqual(indent_from_choice("tab"), INDENT_TAB)
        self.assertEqual(indent_from_choice("nonsense"), INDENT_4)

    def test_empty_containers_stay_on_one_line(self):
        self.assertEqual(format_text('{"a":{},"b":[],"c":[[]]}').output,
                         '{\n    "a": {},\n    "b": [],\n    "c": [\n        []\n    ]\n}')

    def test_crlf_line_ending(self):
        result = format_text('{"a":1}', FormatOptions(indent=INDENT_2, line_ending="\r\n"))
        self.assertEqual(result.output, '{\r\n  "a": 1\r\n}')

    def test_trailing_newline(self):
        result = format_text('{"a":1}', FormatOptions(trailing_newline=True))
        self.assertTrue(result.output.endswith("}\n"))

    def test_top_level_scalars(self):
        self.assertEqual(format_text('"x"').output, '"x"')
        self.assertEqual(format_text("123").output, "123")
        self.assertEqual(format_text("null").output, "null")
        self.assertEqual(format_text("{}").output, "{}")
        self.assertEqual(format_text("[]").output, "[]")


class TestMinify(unittest.TestCase):
    def test_minify_removes_all_whitespace(self):
        text = '{\n  "a" : 1,\n  "b" : [ 1, 2 ]\n}'
        result = minify_text(text)
        self.assertTrue(result.ok, result.error_message)
        self.assertEqual(result.output, '{"a":1,"b":[1,2]}')

    def test_minify_keeps_string_content(self):
        result = minify_text('{"a": "  spaced  "}')
        self.assertEqual(result.output, '{"a":"  spaced  "}')

    def test_minify_function_on_value(self):
        self.assertEqual(minify({"a": [1, {"b": None}]}), '{"a":[1,{"b":null}]}')


class TestOrderPreservation(unittest.TestCase):
    def test_format_does_not_reorder_keys(self):
        result = format_text(SAMPLE)
        self.assertTrue(result.ok)
        parsed = parse_json(result.output)
        self.assertEqual(list(parsed.value.keys()), ["z", "a", "m", "n"])
        inner = parsed.value["a"][2]
        self.assertEqual(list(inner.keys()), ["b", "A"])

    def test_format_does_not_reorder_array_items(self):
        result = format_text("[3,1,2]")
        self.assertEqual(parse_json(result.output).value, [3, 1, 2])

    def test_sort_keys_alpha_is_case_insensitive(self):
        result = format_text('{"B":1,"a":2,"C":3,"A":4}',
                             FormatOptions(sort_keys=True, sort_mode=SORT_ALPHA))
        self.assertEqual(list(parse_json(result.output).value.keys()), ["A", "a", "B", "C"])

    def test_sort_keys_unicode_is_code_point_order(self):
        result = format_text('{"B":1,"a":2,"A":4}',
                             FormatOptions(sort_keys=True, sort_mode=SORT_UNICODE))
        self.assertEqual(list(parse_json(result.output).value.keys()), ["A", "B", "a"])

    def test_sort_is_recursive(self):
        result = format_text('{"z":{"y":1,"x":2},"a":1}',
                             FormatOptions(sort_keys=True, sort_mode=SORT_ALPHA))
        value = parse_json(result.output).value
        self.assertEqual(list(value.keys()), ["a", "z"])
        self.assertEqual(list(value["z"].keys()), ["x", "y"])

    def test_sort_arrays_is_opt_in_and_separate(self):
        default = format_text("[3,1,2]", FormatOptions(sort_keys=True))
        self.assertEqual(parse_json(default.output).value, [3, 1, 2])
        with_array = format_text("[3,1,2]",
                                 FormatOptions(sort_keys=True, sort_arrays=True))
        self.assertEqual(parse_json(with_array.output).value, [1, 2, 3])
        self.assertTrue(with_array.sorted_arrays)

    def test_sort_value_does_not_mutate_input(self):
        original = {"b": 1, "a": {"d": 2, "c": 3}}
        snapshot = json.loads(json.dumps(original))
        sort_value(original, SORT_ALPHA)
        self.assertEqual(original, snapshot)
        self.assertEqual(list(original.keys()), ["b", "a"])


class TestNumberAndUnicodeFidelity(unittest.TestCase):
    def test_number_literals_survive_formatting(self):
        for raw in ["1.230", "1e5", "0.0", "-0", "12345678901234567890123456789",
                    "3.141592653589793238462643383279", "1E+5", "1e-400"]:
            with self.subTest(raw=raw):
                text = f'{{"n":{raw}}}'
                pretty = format_text(text).output
                self.assertIn(raw, pretty)
                compact = minify_text(text).output
                self.assertEqual(compact, f'{{"n":{raw}}}')

    def test_big_integer_is_not_converted_to_float(self):
        big = "9" * 40
        result = format_text(f"[{big}]")
        self.assertIn(big, result.output)
        self.assertNotIn("e+", result.output.lower())

    def test_unicode_is_preserved_by_default(self):
        result = format_text('{"中文":"值😀"}')
        self.assertIn("中文", result.output)
        self.assertIn("😀", result.output)

    def test_ensure_ascii_escapes_non_ascii(self):
        result = format_text('{"中":"😀"}', FormatOptions(ensure_ascii=True))
        self.assertNotIn("中", result.output)
        self.assertIn("\\u4e2d", result.output)
        self.assertIn("\\ud83d\\ude00", result.output)
        self.assertEqual(parse_json(result.output).value, {"中": "😀"})

    def test_escaping_is_correct(self):
        text = r'{"a":"quote\" back\\ slash new\n line tab\t"}'
        result = format_text(text)
        self.assertEqual(parse_json(result.output).value, parse_json(text).value)
        self.assertIn(r'\"', result.output)

    def test_control_characters_are_escaped_on_output(self):
        value = {"a": "tab\there\x01"}
        output = dumps(value)
        self.assertIn("\\t", output)
        self.assertIn("\\u0001", output)
        self.assertEqual(parse_json(output).value["a"], "tab\there\x01")


class TestConsistency(unittest.TestCase):
    """格式化/压缩前后必须语义一致（对应交付要求里的数据一致性测试）。"""

    DOCS = [
        "{}",
        "[]",
        '{"a":1}',
        "[1,2,3]",
        '{"a":[1,2,{"b":null}]}',
        '{"nested":{"deep":{"deeper":{"deepest":[]}}}}',
        '{"unicode":"中文😀\\u00e9","esc":"\\n\\t\\""}',
        '{"numbers":[1.230,1e5,-0,0.1,123456789012345678901234567890]}',
        '{"mixed":[true,false,null,"",0,{},[]]}',
        '[{"id":1,"tags":["a","b"]},{"id":2,"tags":[]}]',
    ]

    def test_roundtrip_preserves_semantics(self):
        for text in self.DOCS:
            for indent in (INDENT_2, INDENT_4, INDENT_TAB, ""):
                with self.subTest(text=text, indent=repr(indent)):
                    result = format_text(text, FormatOptions(indent=indent))
                    self.assertTrue(result.ok, result.error_message)
                    self.assertTrue(result.semantics_preserved)
                    self.assertEqual(normalize(result.output), normalize(text))

    def test_format_is_idempotent(self):
        for text in self.DOCS:
            with self.subTest(text=text):
                first = format_text(text).output
                second = format_text(first).output
                self.assertEqual(first, second)

    def test_minify_then_format_roundtrip(self):
        for text in self.DOCS:
            with self.subTest(text=text):
                compact = minify_text(text).output
                pretty = format_text(compact).output
                self.assertEqual(normalize(pretty), normalize(text))

    def test_matches_stdlib_json_semantics(self):
        for text in self.DOCS:
            with self.subTest(text=text):
                result = format_text(text)
                self.assertEqual(json.loads(result.output), json.loads(text))

    def test_invalid_input_reports_error_without_output(self):
        result = format_text('{"a":1,}')
        self.assertFalse(result.ok)
        self.assertEqual(result.output, "")
        self.assertEqual(result.error_message, "trailing_comma")


class TestDeepAndLarge(unittest.TestCase):
    def test_deep_nesting_serializes_without_recursion_error(self):
        depth = 3000
        text = "[" * depth + "]" * depth
        result = format_text(text, FormatOptions(indent="", max_depth=5000))
        self.assertTrue(result.ok, result.error_message)
        self.assertEqual(result.output.count("["), depth)
        self.assertTrue(result.semantics_preserved)

    def test_deep_object_nesting(self):
        depth = 2000
        text = '{"a":' * depth + "1" + "}" * depth
        result = format_text(text, FormatOptions(indent="", max_depth=5000))
        self.assertTrue(result.ok, result.error_message)
        self.assertTrue(result.semantics_preserved)

    def test_depth_beyond_limit_is_a_clean_error(self):
        text = "[" * 1200 + "]" * 1200
        result = format_text(text)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_message, "depth_exceeded")

    def test_sort_recursive_false_only_sorts_top_level(self):
        result = format_text('{"z":{"y":1,"x":2},"a":1}',
                             FormatOptions(sort_keys=True, sort_recursive=False))
        value = parse_json(result.output).value
        self.assertEqual(list(value.keys()), ["a", "z"])
        self.assertEqual(list(value["z"].keys()), ["y", "x"])

    def test_large_array(self):
        values = list(range(100000))
        output = dumps(values, FormatOptions(indent=""))
        self.assertEqual(output, "[" + ",".join(str(v) for v in values) + "]")
        pretty = dumps(values[:5], FormatOptions(indent=INDENT_2))
        self.assertEqual(pretty, "[\n  0,\n  1,\n  2,\n  3,\n  4\n]")

    def test_large_object(self):
        value = {f"key{i}": i for i in range(50000)}
        output = minify(value)
        self.assertTrue(output.startswith('{"key0":0,'))
        self.assertEqual(len(parse_json(output).value), 50000)


class TestJsonEqual(unittest.TestCase):
    def test_equal_and_unequal_cases(self):
        self.assertTrue(json_equal({"a": 1}, {"a": 1}))
        self.assertFalse(json_equal({"a": 1}, {"a": 2}))
        self.assertFalse(json_equal({"a": 1}, {"b": 1}))
        self.assertFalse(json_equal([1, 2], [2, 1]))
        self.assertFalse(json_equal({"a": 1}, [{"a": 1}]))
        self.assertFalse(json_equal(None, False))
        self.assertTrue(json_equal(None, None))

    def test_strict_numbers_compare_literals(self):
        left = parse_json("[1.230]").value
        right = parse_json("[1.23]").value
        self.assertFalse(json_equal(left, right))
        self.assertTrue(json_equal(left, right, strict_numbers=False))

    def test_deep_equal_does_not_recurse(self):
        depth = 2000
        text = "[" * depth + "]" * depth
        left = parse_json(text, max_depth=5000)
        right = parse_json(text, max_depth=5000)
        self.assertTrue(left.ok and right.ok)
        self.assertTrue(json_equal(left.value, right.value))
        # 层数不同必须判为不等
        shallower = parse_json("[" * (depth - 1) + "]" * (depth - 1), max_depth=5000)
        self.assertFalse(json_equal(left.value, shallower.value))

    def test_equal_strings_from_separate_parses(self):
        # 长字符串不会被 Python 驻留，两次解析得到的是不同对象但值相同
        text = '{"k":"中文😀 a long non-interned string value"}'
        self.assertTrue(json_equal(parse_json(text).value, parse_json(text).value))


def normalize(text):
    """把 JSON 文本转成可比较的纯 Python 结构（数字统一成 Decimal/str 语义）。"""
    result = parse_json(text)
    assert result.ok, result.error
    return _plain(result.value)


def _plain(value):
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, Decimal):
        return ("num", str(value.normalize()), value.as_tuple()[2])
    return value


if __name__ == "__main__":
    unittest.main(verbosity=2)

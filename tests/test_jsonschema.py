"""JSON Schema 校验引擎测试：逐个关键字的通过/失败用例、路径精度、$ref 与循环、
离线约束、深层嵌套、截断、draft 识别、schema 推断与性能。"""

from __future__ import annotations

import inspect
import os
import re
import shutil
import socket
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from unittest import mock

from app.core.jsonparser import parse_json
from app.core.jsonvalue import JsonNumber
from app.engines import jsonschemav
from app.engines.jsonschemav import (
    ASSERTED_FORMATS,
    DRAFT_04,
    DRAFT_07,
    DRAFT_2019_09,
    DRAFT_2020_12,
    DRAFT_UNKNOWN,
    LocalRefResolver,
    SchemaError,
    SchemaValidationResult,
    detect_draft,
    error_keywords,
    infer_schema,
    load_schema_file,
    path_string,
    schema_to_text,
    validate,
    warning_codes,
)


def doc(text):
    """把 JSON 文本解析成引擎使用的值模型（int / JsonNumber / dict / list ...）。"""
    result = parse_json(text)
    assert result.ok, result.error
    return result.value


def run(instance, schema, **kwargs):
    """校验并断言"引擎自身没有出内部错误"，返回结果对象。"""
    result = validate(instance, schema, **kwargs)
    internal = [error for error in result.errors if error.keyword == "internal_error"]
    assert not internal, [error.message for error in internal]
    return result


def codes(result):
    return [(error.path_string, error.keyword) for error in result.errors]


def keywords(result):
    return [error.keyword for error in result.errors]


class TestTypeKeyword(unittest.TestCase):
    def test_scalar_types_pass_and_fail(self):
        cases = [
            ({"type": "object"}, doc("{}"), True),
            ({"type": "object"}, doc("[]"), False),
            ({"type": "array"}, doc("[1,2]"), True),
            ({"type": "array"}, doc("{}"), False),
            ({"type": "string"}, doc('"x"'), True),
            ({"type": "string"}, doc("5"), False),
            ({"type": "number"}, doc("5"), True),
            ({"type": "number"}, doc("5.5"), True),
            ({"type": "number"}, doc('"5"'), False),
            ({"type": "boolean"}, doc("true"), True),
            ({"type": "boolean"}, doc("1"), False),
            ({"type": "null"}, doc("null"), True),
            ({"type": "null"}, doc("false"), False),
        ]
        for schema, instance, expected in cases:
            with self.subTest(schema=schema, instance=instance):
                self.assertEqual(run(instance, schema).valid, expected)

    def test_type_array_of_types(self):
        schema = {"type": ["string", "null"]}
        self.assertTrue(run(doc('"x"'), schema).valid)
        self.assertTrue(run(doc("null"), schema).valid)
        result = run(doc("5"), schema)
        self.assertFalse(result.valid)
        self.assertEqual(result.errors[0].keyword, "type")
        self.assertEqual(result.errors[0].expected, ["string", "null"])
        self.assertEqual(result.errors[0].actual, "5")  # 标量给出字面量文本
        self.assertIn("got number", result.errors[0].message)
        self.assertIn("one of [string, null]", result.errors[0].message)

    def test_bool_is_never_integer_or_number(self):
        for schema in ({"type": "integer"}, {"type": "number"}):
            with self.subTest(schema=schema):
                self.assertFalse(run(doc("true"), schema).valid)
                self.assertFalse(run(True, schema).valid)
        self.assertTrue(run(doc("true"), {"type": "boolean"}).valid)

    def test_integral_decimal_is_integer_per_draft07(self):
        """文档化选择：数值上是整数的 number 也算 integer（draft-07 及以后）。"""
        schema = {"type": "integer"}
        self.assertTrue(run(doc("1"), schema).valid)          # int
        self.assertTrue(run(doc("1.0"), schema).valid)        # JsonNumber，整数值
        self.assertTrue(run(doc("1e2"), schema).valid)        # JsonNumber，100
        self.assertTrue(run(doc("-0"), schema).valid)
        self.assertTrue(run(Decimal("7.000"), schema).valid)  # 裸 Decimal
        self.assertFalse(run(doc("1.5"), schema).valid)
        self.assertFalse(run(doc("0.5"), schema).valid)
        self.assertIsInstance(doc("1.0"), JsonNumber)

    def test_integer_type_error_message(self):
        result = run(doc("1.5"), {"type": "integer"})
        self.assertEqual(codes(result), [("$", "type")])
        self.assertEqual(result.errors[0].message, "expected integer, got number")
        self.assertEqual(result.errors[0].actual, "1.5")

    def test_invalid_type_schema_is_reported(self):
        for schema in ({"type": 123}, {"type": []}, {"type": ["nope"]}, {"type": [None]}):
            with self.subTest(schema=schema):
                result = run(doc("1"), schema)
                self.assertFalse(result.valid)
                self.assertEqual(result.errors[0].keyword, "type")
                self.assertIn('"type"', result.errors[0].message)


class TestEnumConst(unittest.TestCase):
    def test_enum_mixed_types(self):
        schema = {"enum": [1, "1", True, None, {"a": 1}, [1, 2]]}
        for instance in (doc("1"), doc('"1"'), doc("true"), doc("null"),
                         doc('{"a":1}'), doc("[1,2]")):
            with self.subTest(instance=instance):
                self.assertTrue(run(instance, schema).valid)
        for instance in (doc("2"), doc('"2"'), doc("false"), doc('{"a":2}'), doc("[2,1]")):
            with self.subTest(instance=instance):
                self.assertFalse(run(instance, schema).valid)

    def test_enum_bool_is_not_one(self):
        self.assertFalse(run(doc("true"), {"enum": [1]}).valid)
        self.assertFalse(run(doc("1"), {"enum": [True]}).valid)

    def test_enum_numeric_equality(self):
        self.assertTrue(run(doc("1.0"), {"enum": [1]}).valid)
        self.assertTrue(run(doc("100"), {"enum": [doc("1e2")]}).valid)

    def test_enum_object_key_order_is_irrelevant(self):
        self.assertTrue(run(doc('{"b":2,"a":1}'), {"enum": [{"a": 1, "b": 2}]}).valid)

    def test_enum_error_reports_allowed_values(self):
        result = run(doc('"z"'), {"enum": ["a", "b"]})
        self.assertEqual(result.errors[0].keyword, "enum")
        self.assertEqual(result.errors[0].expected, ['"a"', '"b"'])
        self.assertIn("2 allowed values", result.errors[0].message)

    def test_const(self):
        self.assertTrue(run(doc('"x"'), {"const": "x"}).valid)
        self.assertFalse(run(doc('"y"'), {"const": "x"}).valid)
        self.assertTrue(run(doc("3.0"), {"const": 3}).valid)
        self.assertFalse(run(doc("true"), {"const": 1}).valid)
        self.assertTrue(run(doc('{"a":[1,{"b":null}]}'),
                            {"const": {"a": [1, {"b": None}]}}).valid)

    def test_const_error_message(self):
        result = run(doc("5"), {"const": "5"})
        self.assertEqual(result.errors[0].keyword, "const")
        self.assertEqual(result.errors[0].expected, '"5"')
        self.assertEqual(result.errors[0].actual, "5")

    def test_invalid_enum_schema(self):
        result = run(doc("1"), {"enum": 5})
        self.assertFalse(result.valid)
        self.assertEqual(result.errors[0].keyword, "enum")
        self.assertEqual(run(doc("1"), {"enum": []}).errors[0].keyword, "enum")


class TestNumericKeywords(unittest.TestCase):
    def test_minimum_maximum(self):
        self.assertTrue(run(doc("5"), {"minimum": 5}).valid)
        self.assertFalse(run(doc("4"), {"minimum": 5}).valid)
        self.assertTrue(run(doc("5"), {"maximum": 5}).valid)
        self.assertFalse(run(doc("6"), {"maximum": 5}).valid)
        result = run(doc("6"), {"maximum": 5})
        self.assertEqual(result.errors[0].keyword, "maximum")
        self.assertIn("must be less than or equal to 5", result.errors[0].message)

    def test_exclusive_bounds(self):
        self.assertFalse(run(doc("5"), {"exclusiveMinimum": 5}).valid)
        self.assertTrue(run(doc("5.0001"), {"exclusiveMinimum": 5}).valid)
        self.assertFalse(run(doc("5"), {"exclusiveMaximum": 5}).valid)
        self.assertTrue(run(doc("4"), {"exclusiveMaximum": 5}).valid)
        self.assertEqual(run(doc("5"), {"exclusiveMinimum": 5}).errors[0].keyword,
                         "exclusiveMinimum")
        self.assertIn("must be greater than 5",
                      run(doc("5"), {"exclusiveMinimum": 5}).errors[0].message)
        self.assertIn("must be less than 5",
                      run(doc("5"), {"exclusiveMaximum": 5}).errors[0].message)

    def test_bounds_only_apply_to_numbers(self):
        self.assertTrue(run(doc('"99"'), {"maximum": 1}).valid)
        self.assertTrue(run(doc("{}"), {"minimum": 100}).valid)
        self.assertTrue(run(doc("null"), {"maximum": 0}).valid)

    def test_decimal_and_jsonnumber_precision(self):
        self.assertTrue(run(JsonNumber("0.1"), {"minimum": JsonNumber("0.1")}).valid)
        self.assertFalse(run(JsonNumber("0.1"), {"exclusiveMinimum": JsonNumber("0.1")}).valid)
        self.assertTrue(run(Decimal("1.10"), {"maximum": Decimal("1.1")}).valid)
        self.assertFalse(run(JsonNumber("1.5"), {"exclusiveMinimum": JsonNumber("1.5")}).valid)
        self.assertTrue(run(JsonNumber("1.5"), {"exclusiveMinimum": JsonNumber("1.4999")}).valid)

    def test_huge_integers_compare_exactly(self):
        huge = 10 ** 60
        self.assertTrue(run(huge, {"minimum": huge}).valid)
        self.assertFalse(run(huge - 1, {"minimum": huge}).valid)
        self.assertTrue(run(huge, {"maximum": huge + 1}).valid)
        self.assertFalse(run(huge + 1, {"maximum": huge}).valid)
        self.assertEqual(run(doc("123456789012345678901234567890"),
                             {"type": "integer"}).valid, True)

    def test_multiple_of(self):
        self.assertTrue(run(doc("9"), {"multipleOf": 3}).valid)
        self.assertFalse(run(doc("10"), {"multipleOf": 3}).valid)
        self.assertTrue(run(doc("4.5"), {"multipleOf": 1.5}).valid)
        self.assertFalse(run(doc("4.6"), {"multipleOf": 1.5}).valid)
        self.assertTrue(run(doc("0.3"), {"multipleOf": 0.1}).valid)
        self.assertTrue(run(doc("-9"), {"multipleOf": 3}).valid)
        self.assertTrue(run(doc("0"), {"multipleOf": 3}).valid)
        result = run(doc("10"), {"multipleOf": 3})
        self.assertEqual(result.errors[0].keyword, "multipleOf")
        self.assertIn("is not a multiple of 3", result.errors[0].message)

    def test_multiple_of_does_not_apply_to_strings(self):
        self.assertTrue(run(doc('"x"'), {"multipleOf": 2}).valid)

    def test_invalid_numeric_schemas(self):
        self.assertEqual(run(doc("1"), {"multipleOf": 0}).errors[0].keyword, "multipleOf")
        self.assertEqual(run(doc("1"), {"multipleOf": -1}).errors[0].keyword, "multipleOf")
        self.assertEqual(run(doc("1"), {"minimum": "x"}).errors[0].keyword, "minimum")
        self.assertEqual(run(doc("1"), {"maximum": True}).errors[0].keyword, "maximum")

    def test_draft04_boolean_exclusive_forms_are_reported(self):
        """draft-04 的布尔形式不静默忽略，而是给出明确错误。"""
        for keyword in ("exclusiveMinimum", "exclusiveMaximum"):
            with self.subTest(keyword=keyword):
                result = run(doc("5"), {keyword: True})
                self.assertFalse(result.valid)
                self.assertEqual(result.errors[0].keyword, keyword)
                self.assertIn("draft-04", result.errors[0].message)
                replacement = "minimum" if keyword == "exclusiveMinimum" else "maximum"
                self.assertIn(f'"{replacement}"', result.errors[0].message)
        self.assertEqual(detect_draft({"exclusiveMinimum": True}), DRAFT_04)


class TestStringKeywords(unittest.TestCase):
    def test_min_max_length(self):
        self.assertTrue(run(doc('"abc"'), {"minLength": 2, "maxLength": 3}).valid)
        self.assertFalse(run(doc('"a"'), {"minLength": 2}).valid)
        self.assertFalse(run(doc('"abcd"'), {"maxLength": 3}).valid)
        self.assertEqual(run(doc('"a"'), {"minLength": 2}).errors[0].keyword, "minLength")
        self.assertIn("1 character long", run(doc('"a"'), {"minLength": 2}).errors[0].message)
        self.assertTrue(run(doc("5"), {"minLength": 99}).valid)  # 只作用于字符串

    def test_length_counts_code_points(self):
        """文档化选择：长度按 Unicode 码点计数（与 Python len 一致）。"""
        self.assertTrue(run(doc('"\\ud83d\\ude00"'), {"maxLength": 1}).valid)
        self.assertTrue(run(doc('"中文"'), {"minLength": 2, "maxLength": 2}).valid)

    def test_pattern_is_search_semantics(self):
        self.assertTrue(run(doc('"xxabcyy"'), {"pattern": "abc"}).valid)
        self.assertFalse(run(doc('"xxabbyy"'), {"pattern": "abc"}).valid)
        self.assertTrue(run(doc('"b1"'), {"pattern": "^[a-z][0-9]+$"}).valid)
        self.assertTrue(run(doc("5"), {"pattern": "^x$"}).valid)  # 只作用于字符串
        result = run(doc('"x"'), {"pattern": "^y$"})
        self.assertEqual(result.errors[0].keyword, "pattern")
        self.assertIn("^y$", result.errors[0].message)

    def test_uncompilable_pattern_warns_and_skips(self):
        result = run(doc('"x"'), {"pattern": "[", "type": "string"})
        self.assertTrue(result.valid)
        self.assertIn("pattern_invalid", result.warnings[0])
        self.assertIn("skipped", result.warnings[0])

    def test_asserted_formats(self):
        good = {
            "date-time": '"2024-05-01T12:30:00Z"',
            "date": '"2024-02-29"',
            "time": '"23:59:60+01:00"',
            "email": '"name@example.com"',
            "uri": '"https://example.com/a?b=1#f"',
            "uuid": '"550e8400-e29b-41d4-a716-446655440000"',
            "ipv4": '"192.168.0.1"',
            "ipv6": '"2001:0db8:0000:0000:0000:ff00:0042:8329"',
        }
        bad = {
            "date-time": '"2024-05-01 12:30:00"',
            "date": '"2023-02-29"',
            "time": '"24:00:00Z"',
            "email": '"nope@example"',
            "uri": '"not a uri"',
            "uuid": '"550e8400-e29b-41d4-a716"',
            "ipv4": '"010.1.1.1"',
            "ipv6": '"2001:::1"',
        }
        self.assertEqual(sorted(good), sorted(ASSERTED_FORMATS))
        for name in ASSERTED_FORMATS:
            with self.subTest(fmt=name):
                self.assertTrue(run(doc(good[name]), {"format": name}).valid)
                result = run(doc(bad[name]), {"format": name})
                self.assertFalse(result.valid)
                self.assertEqual(result.errors[0].keyword, "format")
                self.assertIn(name, result.errors[0].message.lower())
                self.assertTrue(result.errors[0].expected.startswith(f'format "{name}"'))
                self.assertEqual(result.errors[0].schema_path, ("format",))

    def test_format_ignores_non_strings(self):
        self.assertTrue(run(doc("5"), {"format": "email"}).valid)
        self.assertTrue(run(doc("{}"), {"format": "date"}).valid)

    def test_annotation_and_unknown_formats_only_warn(self):
        result = run(doc('"x"'), {"format": "hostname"})
        self.assertTrue(result.valid)
        self.assertIn("format_annotation", result.warnings[0])
        self.assertIn("hostname", result.warnings[0])
        result = run(doc('"x"'), {"format": "totally-made-up"})
        self.assertTrue(result.valid)
        self.assertIn("format_unknown", result.warnings[0])

    def test_content_keywords_are_annotations(self):
        result = run(doc('"e30="'), {"contentEncoding": "base64",
                                     "contentMediaType": "application/json"})
        self.assertTrue(result.valid)
        self.assertEqual(warning_codes(result).count("content_annotation"), 2)

    def test_invalid_string_schema_values(self):
        self.assertEqual(run(doc('"x"'), {"minLength": "2"}).errors[0].keyword, "minLength")
        self.assertEqual(run(doc('"x"'), {"maxLength": -1}).errors[0].keyword, "maxLength")
        self.assertEqual(run(doc('"x"'), {"pattern": 5}).errors[0].keyword, "pattern")
        self.assertEqual(run(doc('"x"'), {"format": 5}).errors[0].keyword, "format")


class TestArrayKeywords(unittest.TestCase):
    def test_min_max_items(self):
        self.assertTrue(run(doc("[1,2]"), {"minItems": 2, "maxItems": 2}).valid)
        self.assertFalse(run(doc("[1]"), {"minItems": 2}).valid)
        self.assertFalse(run(doc("[1,2,3]"), {"maxItems": 2}).valid)
        self.assertEqual(run(doc("[1]"), {"minItems": 2}).errors[0].keyword, "minItems")
        self.assertIn("the array has 1 item", run(doc("[1]"), {"minItems": 2}).errors[0].message)
        self.assertTrue(run(doc("{}"), {"minItems": 5}).valid)

    def test_items_single_schema(self):
        schema = {"type": "array", "items": {"type": "integer"}}
        self.assertTrue(run(doc("[1,2,3]"), schema).valid)
        result = run(doc('[1,"x",3]'), schema)
        self.assertFalse(result.valid)
        self.assertEqual(codes(result), [("$[1]", "type")])

    def test_items_tuple_form_draft07(self):
        schema = {"items": [{"type": "integer"}, {"type": "string"}]}
        self.assertTrue(run(doc('[1,"a"]'), schema).valid)
        self.assertTrue(run(doc("[1]"), schema).valid)          # 短数组允许
        self.assertTrue(run(doc('[1,"a",true,null]'), schema).valid)  # 多余元素不受限
        result = run(doc('["a",1]'), schema)
        self.assertEqual(codes(result), [("$[0]", "type"), ("$[1]", "type")])

    def test_additional_items(self):
        schema = {"items": [{"type": "integer"}], "additionalItems": False}
        self.assertTrue(run(doc("[1]"), schema).valid)
        result = run(doc('[1,"x",2]'), schema)
        self.assertFalse(result.valid)
        self.assertEqual(codes(result), [("$", "additionalItems")])
        self.assertIn("must not have more than 1 item", result.errors[0].message)
        self.assertIn("1, 2", result.errors[0].message)
        schema2 = {"items": [{"type": "integer"}], "additionalItems": {"type": "string"}}
        self.assertTrue(run(doc('[1,"a"]'), schema2).valid)
        self.assertEqual(codes(run(doc("[1,2]"), schema2)), [("$[1]", "type")])

    def test_items_false_requires_empty_array(self):
        result = run(doc("[1]"), {"items": False})
        self.assertEqual(codes(result), [("$", "items")])
        self.assertTrue(run(doc("[]"), {"items": False}).valid)

    def test_prefix_items_2020_12(self):
        schema = {"$schema": "https://json-schema.org/draft/2020-12/schema",
                  "prefixItems": [{"type": "integer"}, {"type": "string"}],
                  "items": {"type": "boolean"}}
        self.assertTrue(run(doc('[1,"a",true,false]'), schema).valid)
        self.assertEqual(codes(run(doc('[1,"a",2]'), schema)), [("$[2]", "type")])
        self.assertEqual(codes(run(doc('["a","a"]'), schema)), [("$[0]", "type")])
        self.assertEqual(detect_draft(schema), DRAFT_2020_12)

    def test_prefix_items_without_items_allows_the_rest(self):
        schema = {"prefixItems": [{"type": "integer"}]}
        self.assertTrue(run(doc('[1,"anything",null]'), schema).valid)

    def test_contains_min_max_contains(self):
        self.assertTrue(run(doc('[1,"a"]'), {"contains": {"type": "string"}}).valid)
        result = run(doc("[1,2]"), {"contains": {"type": "string"}})
        self.assertEqual(result.errors[0].keyword, "contains")
        self.assertIn("0 items match", result.errors[0].message)
        schema = {"contains": {"type": "string"}, "minContains": 2}
        self.assertTrue(run(doc('["a","b"]'), schema).valid)
        self.assertEqual(run(doc('["a",1]'), schema).errors[0].keyword, "contains")
        schema = {"contains": {"type": "string"}, "maxContains": 1}
        self.assertTrue(run(doc('["a",1]'), schema).valid)
        self.assertEqual(run(doc('["a","b"]'), schema).errors[0].keyword, "maxContains")
        self.assertTrue(run(doc("[]"), {"contains": {"type": "string"}, "minContains": 0}).valid)

    def test_contains_ignores_non_arrays(self):
        self.assertTrue(run(doc("{}"), {"contains": {"type": "string"}}).valid)

    def test_unique_items(self):
        self.assertTrue(run(doc("[1,2,3]"), {"uniqueItems": True}).valid)
        self.assertFalse(run(doc("[1,2,1]"), {"uniqueItems": True}).valid)
        self.assertFalse(run(doc('["a","a"]'), {"uniqueItems": True}).valid)
        self.assertTrue(run(doc('["1",1]'), {"uniqueItems": True}).valid)  # 类型不同
        self.assertTrue(run(doc("[]"), {"uniqueItems": True}).valid)
        self.assertTrue(run(doc("[1]"), {"uniqueItems": True}).valid)
        self.assertTrue(run(doc("[1,1]"), {"uniqueItems": False}).valid)
        self.assertTrue(run(doc("[1,1]"), {}).valid)

    def test_unique_items_numeric_normalization(self):
        self.assertFalse(run(doc("[1,1.0]"), {"uniqueItems": True}).valid)
        self.assertFalse(run([JsonNumber("1.0"), 1], {"uniqueItems": True}).valid)
        self.assertFalse(run([JsonNumber("1e2"), 100], {"uniqueItems": True}).valid)
        self.assertFalse(run([JsonNumber("-0"), Decimal("0")], {"uniqueItems": True}).valid)
        self.assertTrue(run(doc("[true,1]"), {"uniqueItems": True}).valid)   # bool != number
        self.assertFalse(run(doc("[true,true]"), {"uniqueItems": True}).valid)
        self.assertFalse(run(doc("[null,null]"), {"uniqueItems": True}).valid)

    def test_unique_items_nested_structures(self):
        self.assertFalse(run(doc('[{"a":1},{"a":1}]'), {"uniqueItems": True}).valid)
        self.assertFalse(run(doc('[{"a":1,"b":2},{"b":2,"a":1}]'),
                             {"uniqueItems": True}).valid)  # 键顺序无关
        self.assertTrue(run(doc('[{"a":1},{"a":2}]'), {"uniqueItems": True}).valid)
        self.assertFalse(run(doc("[[1,[2]],[1,[2]]]"), {"uniqueItems": True}).valid)
        self.assertTrue(run(doc("[[1,[2]],[1,[3]]]"), {"uniqueItems": True}).valid)

    def test_unique_items_error_reports_indices(self):
        result = run(doc('["a","b","a"]'), {"uniqueItems": True})
        self.assertEqual(result.errors[0].keyword, "uniqueItems")
        self.assertIn("items 0 and 2 are duplicates", result.errors[0].message)
        self.assertEqual(result.errors[0].actual, "duplicate at index 2")

    def test_unique_items_pairwise_fallback_on_hash_collision(self):
        """规范文本撞车时退回成对比较：不同值不会被误判成重复。"""
        distinct = doc('[{"a":1},{"b":2},{"c":3}]')
        with mock.patch.object(jsonschemav, "_canonical_text", return_value="same"):
            result = run(distinct, {"uniqueItems": True})
        self.assertTrue(result.valid)
        collisions = [w for w in result.warnings if "unique_items_hash_collision" in w]
        self.assertEqual(len(collisions), 1)  # 同文本告警只保留一条
        self.assertIn("warnings_suppressed", result.warnings[-1])
        duplicated = doc('[{"a":1},{"b":2},{"a":1}]')
        with mock.patch.object(jsonschemav, "_canonical_text", return_value="same"):
            result = run(duplicated, {"uniqueItems": True})
        self.assertFalse(result.valid)
        self.assertEqual(result.errors[0].keyword, "uniqueItems")

    def test_unique_items_is_linear_on_large_arrays(self):
        big = list(range(20000))
        started = time.perf_counter()
        result = run(big, {"uniqueItems": True})
        elapsed = time.perf_counter() - started
        self.assertTrue(result.valid)
        self.assertLess(elapsed, 3.0)
        started = time.perf_counter()
        self.assertFalse(run(big + [19999], {"uniqueItems": True}).valid)
        self.assertLess(time.perf_counter() - started, 3.0)

    def test_invalid_array_schemas(self):
        self.assertEqual(run(doc("[]"), {"items": 5}).errors[0].keyword, "items")
        self.assertEqual(run(doc("[]"), {"items": []}).errors[0].keyword, "items")
        self.assertEqual(run(doc("[]"), {"minItems": "1"}).errors[0].keyword, "minItems")
        self.assertEqual(run(doc("[]"), {"uniqueItems": "yes"}).errors[0].keyword, "uniqueItems")
        self.assertEqual(run(doc("[]"), {"contains": 5}).errors[0].keyword, "contains")


class TestObjectKeywords(unittest.TestCase):
    def test_properties(self):
        schema = {"properties": {"a": {"type": "integer"}, "b": {"type": "string"}}}
        self.assertTrue(run(doc('{"a":1,"b":"x"}'), schema).valid)
        self.assertTrue(run(doc('{"a":1,"c":[]}'), schema).valid)  # 未声明的键不受限
        self.assertEqual(codes(run(doc('{"a":"x","b":2}'), schema)),
                         [("$.a", "type"), ("$.b", "type")])
        self.assertTrue(run(doc("[]"), schema).valid)  # 只作用于对象

    def test_required_reports_every_missing_key_in_schema_order(self):
        schema = {"required": ["name", "age", "email"]}
        result = run(doc('{"age":3}'), schema)
        self.assertFalse(result.valid)
        self.assertEqual([error.keyword for error in result.errors],
                         ["required", "required"])
        self.assertEqual([error.expected for error in result.errors], ["name", "email"])
        self.assertEqual({error.path for error in result.errors}, {()})
        self.assertEqual({error.path_string for error in result.errors}, {"$"})
        self.assertIn('missing required property "name"', result.errors[0].message)

    def test_required_nested_path(self):
        schema = {"properties": {"users": {"type": "array",
                                           "items": {"required": ["id", "name"]}}}}
        result = run(doc('{"users":[{"id":1,"name":"a"},{"id":2}]}'), schema)
        self.assertEqual(codes(result), [("$.users[1]", "required")])
        self.assertEqual(result.errors[0].path, ("users", 1))
        self.assertEqual(result.errors[0].expected, "name")

    def test_min_max_properties(self):
        self.assertTrue(run(doc('{"a":1}'), {"minProperties": 1, "maxProperties": 2}).valid)
        self.assertFalse(run(doc("{}"), {"minProperties": 1}).valid)
        self.assertFalse(run(doc('{"a":1,"b":2,"c":3}'), {"maxProperties": 2}).valid)
        self.assertEqual(run(doc("{}"), {"minProperties": 1}).errors[0].keyword, "minProperties")
        self.assertTrue(run(doc("[]"), {"minProperties": 9}).valid)

    def test_pattern_properties(self):
        schema = {"patternProperties": {"^s_": {"type": "string"}, "^i_": {"type": "integer"}}}
        self.assertTrue(run(doc('{"s_a":"x","i_b":1}'), schema).valid)
        self.assertTrue(run(doc('{"other":[]}'), schema).valid)
        result = run(doc('{"s_a":1,"i_b":"x"}'), schema)
        self.assertEqual(codes(result), [("$.s_a", "type"), ("$.i_b", "type")])
        self.assertEqual(result.errors[0].schema_path, ("patternProperties", "^s_", "type"))
        self.assertEqual(result.errors[1].schema_path, ("patternProperties", "^i_", "type"))

    def test_additional_properties_interaction(self):
        schema = {"properties": {"a": {}},
                  "patternProperties": {"^p": {}},
                  "additionalProperties": False}
        self.assertTrue(run(doc('{"a":1,"p_x":2}'), schema).valid)
        result = run(doc('{"a":1,"p_x":2,"z":3,"y":4}'), schema)
        self.assertEqual(codes(result), [("$", "additionalProperties")])
        self.assertIn('"z", "y"', result.errors[0].message)
        self.assertEqual(result.errors[0].actual, ["z", "y"])

    def test_additional_properties_as_schema(self):
        schema = {"properties": {"a": {}}, "additionalProperties": {"type": "integer"}}
        self.assertTrue(run(doc('{"a":"any","b":1}'), schema).valid)
        self.assertEqual(codes(run(doc('{"a":"any","b":"x"}'), schema)), [("$.b", "type")])

    def test_property_names(self):
        schema = {"propertyNames": {"pattern": "^[a-z]+$"}}
        self.assertTrue(run(doc('{"ab":1}'), schema).valid)
        result = run(doc('{"A1":1}'), schema)
        self.assertEqual(codes(result), [("$.A1", "pattern")])
        self.assertEqual(result.errors[0].schema_path, ("propertyNames", "pattern"))
        self.assertTrue(run(doc("[]"), schema).valid)

    def test_property_names_false(self):
        result = run(doc('{"a":1,"b":2}'), {"propertyNames": False})
        self.assertEqual(codes(result), [("$", "propertyNames")])
        self.assertTrue(run(doc("{}"), {"propertyNames": False}).valid)

    def test_dependent_required(self):
        schema = {"dependentRequired": {"credit_card": ["billing_address"]}}
        self.assertTrue(run(doc('{"credit_card":1,"billing_address":"x"}'), schema).valid)
        self.assertTrue(run(doc('{"billing_address":"x"}'), schema).valid)
        result = run(doc('{"credit_card":1}'), schema)
        self.assertEqual(codes(result), [("$", "dependentRequired")])
        self.assertEqual(result.errors[0].expected, "billing_address")
        self.assertTrue(run(doc("[]"), schema).valid)

    def test_dependent_schemas(self):
        schema = {"dependentSchemas": {"a": {"required": ["b"]}}}
        self.assertTrue(run(doc('{"a":1,"b":2}'), schema).valid)
        result = run(doc('{"a":1}'), schema)
        self.assertEqual(codes(result), [("$", "required")])
        self.assertEqual(result.errors[0].schema_path, ("dependentSchemas", "a", "required"))
        self.assertTrue(run(doc('{"z":1}'), schema).valid)

    def test_legacy_dependencies_both_forms(self):
        self.assertFalse(run(doc('{"a":1}'), {"dependencies": {"a": ["b"]}}).valid)
        self.assertTrue(run(doc('{"a":1,"b":2}'), {"dependencies": {"a": ["b"]}}).valid)
        self.assertEqual(run(doc('{"a":1}'),
                             {"dependencies": {"a": ["b"]}}).errors[0].keyword, "dependencies")
        schema = {"dependencies": {"a": {"required": ["b"]}}}
        self.assertEqual(codes(run(doc('{"a":1}'), schema)), [("$", "required")])
        self.assertTrue(run(doc('{"a":1,"b":2}'), schema).valid)

    def test_invalid_object_schemas(self):
        self.assertEqual(run(doc("{}"), {"properties": []}).errors[0].keyword, "properties")
        self.assertEqual(run(doc("{}"), {"properties": {"a": 5}}).errors[0].keyword, "properties")
        self.assertEqual(run(doc("{}"), {"required": "a"}).errors[0].keyword, "required")
        self.assertEqual(run(doc("{}"), {"required": [1]}).errors[0].keyword, "required")
        self.assertEqual(run(doc("{}"), {"patternProperties": 5}).errors[0].keyword,
                         "patternProperties")
        self.assertEqual(run(doc("{}"), {"additionalProperties": "no"}).errors[0].keyword,
                         "additionalProperties")
        self.assertEqual(run(doc("{}"), {"propertyNames": 5}).errors[0].keyword, "propertyNames")
        self.assertEqual(run(doc("{}"), {"dependentRequired": 5}).errors[0].keyword,
                         "dependentRequired")
        self.assertEqual(run(doc("{}"), {"dependentSchemas": {"a": 5}}).errors[0].keyword,
                         "dependentSchemas")
        self.assertEqual(run(doc("{}"), {"dependencies": {"a": 5}}).errors[0].keyword,
                         "dependencies")


class TestCombinators(unittest.TestCase):
    def test_all_of(self):
        schema = {"allOf": [{"type": "integer"}, {"minimum": 10}]}
        self.assertTrue(run(doc("20"), schema).valid)
        self.assertEqual(codes(run(doc("5"), schema)), [("$", "minimum")])
        self.assertEqual(codes(run(doc('"x"'), schema)), [("$", "type")])
        self.assertEqual(run(doc("5"), schema).errors[0].schema_path, ("allOf", 1, "minimum"))

    def test_all_of_reports_every_failing_branch(self):
        schema = {"allOf": [{"minimum": 10}, {"maximum": 1}]}
        result = run(doc("5"), schema)
        self.assertEqual([error.keyword for error in result.errors], ["minimum", "maximum"])

    def test_any_of(self):
        schema = {"anyOf": [{"type": "string"}, {"type": "integer", "minimum": 10}]}
        self.assertTrue(run(doc('"x"'), schema).valid)
        self.assertTrue(run(doc("20"), schema).valid)
        result = run(doc("5"), schema)
        self.assertFalse(result.valid)
        self.assertEqual(result.errors[0].keyword, "anyOf")
        self.assertEqual(len(result.errors), 1)  # 分支细节汇总成一条

    def test_any_of_reports_which_branches_failed(self):
        schema = {"anyOf": [{"type": "string"}, {"type": "integer", "minimum": 10}]}
        result = run(doc("5"), schema)
        error = result.errors[0]
        self.assertEqual(len(error.expected), 2)
        self.assertIn("anyOf[0]: expected string, got number", error.expected[0])
        self.assertIn("anyOf[1]:", error.expected[1])
        self.assertIn("greater than or equal to 10", error.expected[1])
        self.assertIn("does not match any of the 2", error.message)

    def test_one_of_exactly_one(self):
        schema = {"oneOf": [{"type": "string"}, {"type": "integer"}]}
        self.assertTrue(run(doc('"x"'), schema).valid)
        self.assertTrue(run(doc("5"), schema).valid)
        result = run(doc("true"), schema)
        self.assertEqual(result.errors[0].keyword, "oneOf")
        self.assertIn("does not match any of the 2", result.errors[0].message)
        self.assertEqual(len(result.errors[0].expected), 2)

    def test_one_of_more_than_one_match(self):
        schema = {"oneOf": [{"type": "number"}, {"type": "integer"}, {"type": "string"}]}
        result = run(doc("5"), schema)
        self.assertFalse(result.valid)
        self.assertIn("matches 2 of the 3", result.errors[0].message)
        self.assertIn("0, 1", result.errors[0].message)
        self.assertEqual(result.errors[0].actual, "2 subschemas matched")

    def test_not(self):
        schema = {"not": {"type": "string"}}
        self.assertTrue(run(doc("5"), schema).valid)
        result = run(doc('"x"'), schema)
        self.assertEqual(result.errors[0].keyword, "not")
        self.assertIn("must NOT be valid", result.errors[0].message)
        self.assertFalse(run(doc('"x"'), {"not": {}}).valid)
        self.assertTrue(run(doc('"x"'), {"not": False}).valid)

    def test_if_then_else(self):
        schema = {"if": {"properties": {"kind": {"const": "a"}}, "required": ["kind"]},
                  "then": {"required": ["alpha"]},
                  "else": {"required": ["beta"]}}
        self.assertTrue(run(doc('{"kind":"a","alpha":1}'), schema).valid)
        self.assertEqual(codes(run(doc('{"kind":"a"}'), schema)), [("$", "required")])
        self.assertEqual(run(doc('{"kind":"a"}'), schema).errors[0].expected, "alpha")
        self.assertTrue(run(doc('{"kind":"b","beta":1}'), schema).valid)
        self.assertEqual(run(doc('{"kind":"b"}'), schema).errors[0].expected, "beta")
        self.assertEqual(run(doc('{"kind":"a"}'), schema).errors[0].schema_path,
                         ("then", "required"))
        self.assertEqual(run(doc('{"kind":"b"}'), schema).errors[0].schema_path,
                         ("else", "required"))

    def test_if_without_then_or_else_is_a_no_op(self):
        self.assertTrue(run(doc("5"), {"if": {"type": "string"}}).valid)
        self.assertTrue(run(doc('"x"'), {"if": {"type": "string"}}).valid)

    def test_nested_combinators(self):
        schema = {"properties": {"list": {"type": "array", "items": {
            "anyOf": [{"type": "integer", "minimum": 0},
                      {"type": "string", "minLength": 2}]}}}}
        self.assertTrue(run(doc('{"list":[1,"ab"]}'), schema).valid)
        result = run(doc('{"list":[1,"a"]}'), schema)
        self.assertEqual(result.errors[0].path, ("list", 1))
        self.assertEqual(result.errors[0].path_string, "$.list[1]")
        self.assertEqual(result.errors[0].keyword, "anyOf")

    def test_invalid_combinator_schemas(self):
        self.assertEqual(run(doc("1"), {"allOf": {}}).errors[0].keyword, "allOf")
        self.assertEqual(run(doc("1"), {"allOf": []}).errors[0].keyword, "allOf")
        self.assertEqual(run(doc("1"), {"anyOf": [5]}).errors[0].keyword, "anyOf")
        self.assertEqual(run(doc("1"), {"oneOf": "x"}).errors[0].keyword, "oneOf")
        self.assertEqual(run(doc("1"), {"not": 5}).errors[0].keyword, "not")
        self.assertEqual(run(doc("1"), {"if": 5}).errors[0].keyword, "if")
        self.assertEqual(run(doc("1"), {"if": {}, "then": 5}).errors[0].keyword, "then")
        self.assertEqual(run(doc("1"), {"if": {}, "else": 5}).errors[0].keyword, "else")


class TestBooleanSchemas(unittest.TestCase):
    def test_true_and_false_schemas(self):
        self.assertTrue(run(doc('{"a":1}'), True).valid)
        self.assertTrue(run(doc("null"), {}).valid)
        result = run(doc("1"), False)
        self.assertFalse(result.valid)
        self.assertEqual(result.errors[0].keyword, "false")
        self.assertEqual(codes(run(doc('{"a":1,"b":2}'),
                                   {"properties": {"a": True, "b": False}})), [("$.b", "false")])


class TestErrorPaths(unittest.TestCase):
    def test_deep_nested_path_tuple_and_string(self):
        schema = {"properties": {"users": {"type": "array", "items": {
            "properties": {"age": {"type": "integer"}}}}}}
        result = run(doc('{"users":[{"age":3},{"age":"old"}]}'), schema)
        self.assertEqual(len(result.errors), 1)
        error = result.errors[0]
        self.assertEqual(error.path, ("users", 1, "age"))
        self.assertEqual(error.path_string, "$.users[1].age")
        self.assertEqual(error.keyword, "type")
        self.assertEqual(error.schema_path, ("properties", "users", "items",
                                             "properties", "age", "type"))
        self.assertEqual(error.describe(), "$.users[1].age: expected integer, got string")

    def test_path_string_quoting_rules(self):
        self.assertEqual(path_string(()), "$")
        self.assertEqual(path_string(("a", "b")), "$.a.b")
        self.assertEqual(path_string(("weird key",)), "$['weird key']")
        self.assertEqual(path_string(("it's",)), "$['it\\'s']")
        self.assertEqual(path_string((0, 12)), "$[0][12]")
        self.assertEqual(path_string(("a", 0, "b c")), "$.a[0]['b c']")
        self.assertEqual(path_string((True,)), "$[1]")

    def test_weird_keys_in_instance_paths(self):
        schema = {"properties": {"weird key": {"properties": {"a b": {"type": "integer"}}}}}
        result = run(doc('{"weird key":{"a b":"x"}}'), schema)
        self.assertEqual(result.errors[0].path, ("weird key", "a b"))
        self.assertEqual(result.errors[0].path_string, "$['weird key']['a b']")

    def test_root_path(self):
        result = run(doc("[]"), {"type": "object"})
        self.assertEqual(result.errors[0].path, ())
        self.assertEqual(result.errors[0].path_string, "$")

    def test_schema_path_uses_keyword_and_index(self):
        schema = {"anyOf": [{"properties": {"a": {"type": "string"}}}, {"type": "null"}]}
        result = run(doc('{"a":1}'), schema)
        self.assertEqual(result.errors[0].schema_path, ("anyOf",))


class TestRefs(unittest.TestCase):
    def test_local_pointer_ref(self):
        schema = {"$defs": {"name": {"type": "string", "minLength": 2}},
                  "properties": {"a": {"$ref": "#/$defs/name"}}}
        self.assertTrue(run(doc('{"a":"xy"}'), schema).valid)
        result = run(doc('{"a":"x"}'), schema)
        self.assertEqual(codes(result), [("$.a", "minLength")])
        self.assertEqual(result.errors[0].schema_path,
                         ("properties", "a", "$ref", "minLength"))

    def test_definitions_legacy(self):
        schema = {"definitions": {"n": {"type": "integer"}}, "$ref": "#/definitions/n"}
        self.assertTrue(run(doc("5"), schema).valid)
        self.assertEqual(codes(run(doc('"x"'), schema)), [("$", "type")])

    def test_root_ref_and_pointer_escaping(self):
        schema = {"$defs": {"a~b/c": {"type": "integer"}}, "$ref": "#/$defs/a~0b~1c"}
        self.assertTrue(run(doc("1"), schema).valid)
        self.assertEqual(codes(run(doc('"x"'), schema)), [("$", "type")])
        broken = {"$defs": {"a": {"type": "integer"}}, "$ref": "#/$defs/a~9"}
        self.assertEqual(run(doc("1"), broken).errors[0].keyword, "ref_unresolvable")

    def test_anchor_ref(self):
        schema = {"$defs": {"a": {"$anchor": "flag", "type": "boolean"}}, "$ref": "#flag"}
        self.assertTrue(run(doc("true"), schema).valid)
        self.assertEqual(codes(run(doc("1"), schema)), [("$", "type")])

    def test_draft07_plain_name_id_as_anchor(self):
        schema = {"$schema": "http://json-schema.org/draft-07/schema#",
                  "definitions": {"a": {"$id": "#flag", "type": "boolean"}},
                  "$ref": "#flag"}
        self.assertTrue(run(doc("false"), schema).valid)
        self.assertFalse(run(doc('"x"'), schema).valid)

    def test_unresolvable_refs_are_reported(self):
        for ref in ("#/nope", "#/properties/a/deeper", "#missing", "#/0"):
            with self.subTest(ref=ref):
                result = run(doc("1"), {"$ref": ref})
                self.assertFalse(result.valid)
                self.assertIn(result.errors[0].keyword,
                              ("ref_unresolvable", "ref_invalid"))

    def test_ref_to_non_schema_value(self):
        result = run(doc("1"), {"$defs": {"a": 5}, "$ref": "#/$defs/a"})
        self.assertFalse(result.valid)
        self.assertEqual(result.errors[0].keyword, "ref_unresolvable")

    def test_recursive_tree_schema_200_levels(self):
        schema = {"$defs": {"node": {"type": "object",
                                     "properties": {"child": {"$ref": "#/$defs/node"},
                                                    "name": {"type": "string"}},
                                     "required": ["name"]}},
                  "$ref": "#/$defs/node"}
        node = {"name": "level-0"}
        current = node
        for index in range(200):
            current["child"] = {"name": f"level-{index + 1}"}
            current = current["child"]
        result = run(node, schema)
        self.assertTrue(result.valid)
        self.assertEqual(result.error_count, 0)
        del current["name"]
        result = run(node, schema)
        self.assertFalse(result.valid)
        self.assertEqual(result.errors[0].keyword, "required")
        self.assertEqual(result.errors[0].expected, "name")
        self.assertEqual(len(result.errors[0].path), 200)
        self.assertEqual(result.errors[0].path, ("child",) * 200)
        self.assertTrue(result.errors[0].path_string.startswith("$.child.child"))
        self.assertFalse(result.errors[0].path_string.endswith(".name"))
        self.assertEqual(result.errors[0].path_string, "$" + ".child" * 200)

    def test_recursive_schema_terminates_on_deep_instance(self):
        schema = {"$defs": {"node": {"type": "object",
                                     "properties": {"c": {"$ref": "#/$defs/node"}}}},
                  "$ref": "#/$defs/node"}
        node = {}
        current = node
        for _ in range(1200):
            current["c"] = {}
            current = current["c"]
        started = time.perf_counter()
        result = run(node, schema)
        self.assertLess(time.perf_counter() - started, 5.0)
        self.assertTrue(result.valid)
        self.assertFalse(result.truncated)

    def test_ref_cycle_is_detected_not_hung(self):
        schema = {"a": {"$ref": "#/b"}, "b": {"$ref": "#/a"}, "$ref": "#/a"}
        started = time.perf_counter()
        result = run(doc("{}"), schema)
        self.assertLess(time.perf_counter() - started, 5.0)
        self.assertFalse(result.valid)
        self.assertEqual(result.errors[0].keyword, "ref_cycle")
        self.assertIn("loop forever", result.errors[0].message)

    def test_self_referencing_all_of_cycle(self):
        schema = {"allOf": [{"$ref": "#"}]}
        started = time.perf_counter()
        result = run(doc('{"a":1}'), schema)
        self.assertLess(time.perf_counter() - started, 5.0)
        self.assertEqual(result.errors[0].keyword, "ref_cycle")

    def test_cycle_inside_properties_is_detected(self):
        schema = {"properties": {"a": {"$ref": "#"}}, "$ref": "#"}
        started = time.perf_counter()
        result = run(doc('{"a":{"a":{}}}'), schema)
        self.assertLess(time.perf_counter() - started, 5.0)
        self.assertFalse(result.valid)
        self.assertIn("ref_cycle", keywords(result))

    def test_remote_http_ref_is_refused_without_network(self):
        for ref in ("http://example.com/schema.json",
                    "https://example.com/schema.json#/x",
                    "//example.com/schema.json",
                    "ftp://example.com/s.json"):
            with self.subTest(ref=ref):
                with mock.patch.object(socket, "create_connection",
                                       side_effect=AssertionError("network access attempted")), \
                        mock.patch.object(socket, "socket",
                                          side_effect=AssertionError("network access attempted")):
                    result = run(doc("1"), {"$ref": ref})
                self.assertFalse(result.valid)
                self.assertEqual(result.errors[0].keyword, "remote_ref_not_allowed")
                self.assertIn("offline", result.errors[0].message)
                self.assertIn("remote_ref_not_allowed", result.warnings[0])

    def test_module_has_no_network_imports(self):
        source = inspect.getsource(jsonschemav)
        imports = [line.strip() for line in source.splitlines()
                   if re.match(r"\s*(import|from)\s+\S+", line)]
        forbidden = re.compile(r"\b(socket|urllib|requests|http\.client|httpx|aiohttp|ftplib"
                               r"|PySide6|PyQt|shiboken)\b")
        self.assertTrue(imports)
        for line in imports:
            self.assertIsNone(forbidden.search(line), line)
        self.assertFalse(hasattr(jsonschemav, "urllib"))
        self.assertFalse(hasattr(jsonschemav, "socket"))

    def test_local_file_ref(self):
        folder = tempfile.mkdtemp()
        try:
            with open(os.path.join(folder, "address.json"), "w", encoding="utf-8") as handle:
                handle.write('{"type":"object","required":["city"],'
                             '"properties":{"city":{"type":"string"}}}')
            schema = {"properties": {"home": {"$ref": "address.json"}}}
            resolver = LocalRefResolver(base_schema=schema, base_dir=folder)
            self.assertTrue(run(doc('{"home":{"city":"X"}}'), schema, resolver=resolver).valid)
            result = run(doc('{"home":{"zip":1}}'), schema, resolver=resolver)
            self.assertEqual(codes(result), [("$.home", "required")])
            result = run(doc('{"home":{"city":5}}'), schema, resolver=resolver)
            self.assertEqual(codes(result), [("$.home.city", "type")])
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_local_file_ref_with_pointer_and_search_dirs(self):
        folder = tempfile.mkdtemp()
        try:
            with open(os.path.join(folder, "defs.json"), "w", encoding="utf-8") as handle:
                handle.write('{"positive":{"type":"integer","minimum":1}}')
            schema = {"$ref": "defs.json#/positive"}
            resolver = LocalRefResolver(base_schema=schema, search_dirs=[folder])
            self.assertTrue(run(doc("5"), schema, resolver=resolver).valid)
            self.assertEqual(codes(run(doc("0"), schema, resolver=resolver)), [("$", "minimum")])
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_missing_local_file_ref(self):
        folder = tempfile.mkdtemp()
        try:
            result = run(doc("1"), {"$ref": "nope.json"},
                         resolver=LocalRefResolver(base_schema={}, base_dir=folder))
            self.assertEqual(result.errors[0].keyword, "ref_file_not_found")
            self.assertIn("nope.json", result.errors[0].message)
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_unc_share_ref_is_refused(self):
        result = run(doc("1"), {"$ref": "\\\\server\\share\\s.json"})
        self.assertEqual(result.errors[0].keyword, "remote_ref_not_allowed")
        self.assertIn("UNC", result.errors[0].message)

    def test_ref_siblings_are_ignored_in_draft07(self):
        schema = {"$schema": "http://json-schema.org/draft-07/schema#",
                  "definitions": {"a": {"type": "integer"}},
                  "$ref": "#/definitions/a", "type": "string"}
        result = run(doc("5"), schema)
        self.assertTrue(result.valid)
        sibling_warnings = [w for w in result.warnings if "ref_siblings_ignored" in w]
        self.assertEqual(len(sibling_warnings), 1)
        self.assertIn("'type'", sibling_warnings[0])
        self.assertIn("draft-07", sibling_warnings[0])
        self.assertFalse(run(doc('"x"'), schema).valid)
        self.assertEqual(codes(run(doc('"x"'), schema)), [("$", "type")])

    def test_ref_siblings_apply_in_2020_12(self):
        schema = {"$schema": "https://json-schema.org/draft/2020-12/schema",
                  "$defs": {"a": {"type": "integer"}},
                  "$ref": "#/$defs/a", "minimum": 10}
        self.assertTrue(run(doc("20"), schema).valid)
        self.assertEqual(codes(run(doc("5"), schema)), [("$", "minimum")])
        self.assertFalse(any("ref_siblings_ignored" in w for w in run(doc("20"), schema).warnings))

    def test_embedded_id_ref(self):
        schema = {"$defs": {"inner": {"$id": "inner.json", "type": "integer"}},
                  "$ref": "inner.json"}
        self.assertTrue(run(doc("5"), schema).valid)
        self.assertEqual(codes(run(doc('"x"'), schema)), [("$", "type")])

    def test_caller_supplied_resolver_root_wins(self):
        schema_a = {"$ref": "#/$defs/x"}
        schema_b = {"$defs": {"x": {"type": "string"}}}
        resolver = LocalRefResolver(base_schema=schema_b)
        self.assertTrue(run(doc('"s"'), schema_a, resolver=resolver).valid)
        self.assertEqual(codes(run(doc("5"), schema_a, resolver=resolver)), [("$", "type")])
        empty = LocalRefResolver()
        self.assertIsNone(empty.root_document)
        self.assertTrue(run(doc('"s"'), schema_b, resolver=empty).valid)
        self.assertIs(empty.root_schema, schema_b)
        self.assertIsNotNone(empty.root_document)

    def test_resolver_resolve_api(self):
        resolver = LocalRefResolver(base_schema={"$defs": {"a": {"type": "string"}}})
        schema, error = resolver.resolve("#/$defs/a")
        self.assertEqual(schema, {"type": "string"})
        self.assertEqual(error, "")
        schema, error = resolver.resolve("#/nope")
        self.assertIsNone(schema)
        self.assertTrue(error.startswith("ref_unresolvable"))
        schema, error = resolver.resolve("https://x/y.json")
        self.assertIsNone(schema)
        self.assertTrue(error.startswith("remote_ref_not_allowed"))
        schema, error = resolver.resolve("#")
        self.assertEqual(schema, {"$defs": {"a": {"type": "string"}}})
        self.assertEqual(error, "")
        self.assertEqual(resolver.resolve(5)[1].split(":")[0], "ref_invalid")


class TestLoadSchemaFile(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def write(self, name, text):
        path = os.path.join(self.folder, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_load_ok(self):
        path = self.write("s.json", '{"type":"object","properties":{"a":{"type":"integer"}}}')
        schema, error = load_schema_file(path)
        self.assertEqual(error, "")
        self.assertEqual(schema["type"], "object")
        self.assertTrue(run(doc('{"a":1}'), schema).valid)

    def test_load_jsonc_fallback(self):
        path = self.write("s.jsonc", '// comment\n{"type":"string"}\n')
        schema, error = load_schema_file(path)
        self.assertEqual(error, "")
        self.assertEqual(schema, {"type": "string"})

    def test_load_boolean_schema(self):
        path = self.write("b.json", "false")
        schema, error = load_schema_file(path)
        self.assertEqual(error, "")
        self.assertIs(schema, False)

    def test_load_errors(self):
        self.assertTrue(load_schema_file(self.write("bad.json", "{oops"))[1]
                        .startswith("schema_parse_error"))
        self.assertTrue(load_schema_file(self.write("arr.json", "[1,2]"))[1]
                        .startswith("schema_invalid"))
        self.assertTrue(load_schema_file(os.path.join(self.folder, "gone.json"))[1]
                        .startswith("schema_file_unreadable"))
        self.assertTrue(load_schema_file("")[1].startswith("schema_file_invalid"))
        self.assertTrue(load_schema_file("https://example.com/s.json")[1]
                        .startswith("remote_schema_not_allowed"))
        self.assertTrue(load_schema_file(None)[1].startswith("schema_file_invalid"))


class TestDraftDetection(unittest.TestCase):
    def test_schema_uris(self):
        self.assertEqual(detect_draft({"$schema": "http://json-schema.org/draft-07/schema#"}),
                         DRAFT_07)
        self.assertEqual(detect_draft({"$schema": "https://json-schema.org/draft/2020-12/schema"}),
                         DRAFT_2020_12)
        self.assertEqual(detect_draft({"$schema": "https://json-schema.org/draft/2019-09/schema"}),
                         DRAFT_2019_09)
        self.assertEqual(detect_draft({"$schema": "http://json-schema.org/draft-06/schema#"}),
                         "draft-06")
        self.assertEqual(detect_draft({"$schema": "http://json-schema.org/draft-04/schema#"}),
                         DRAFT_04)
        self.assertEqual(detect_draft({"$schema": "https://example.com/whatever"}), DRAFT_UNKNOWN)
        self.assertEqual(detect_draft({"$schema": 5}), DRAFT_07)

    def test_keyword_sniffing_without_schema_uri(self):
        self.assertEqual(detect_draft({}), DRAFT_07)
        self.assertEqual(detect_draft({"type": "object"}), DRAFT_07)
        self.assertEqual(detect_draft({"prefixItems": [{}]}), DRAFT_2020_12)
        self.assertEqual(detect_draft({"$defs": {}}), DRAFT_2019_09)
        self.assertEqual(detect_draft({"dependentSchemas": {}}), DRAFT_2019_09)
        self.assertEqual(detect_draft({"exclusiveMinimum": True}), DRAFT_04)
        self.assertEqual(detect_draft({"properties": {"a": {"$defs": {}}}}), DRAFT_2019_09)

    def test_non_object_schema(self):
        self.assertEqual(detect_draft(True), DRAFT_UNKNOWN)
        self.assertEqual(detect_draft("x"), DRAFT_UNKNOWN)
        self.assertEqual(detect_draft(None), DRAFT_UNKNOWN)

    def test_result_carries_draft(self):
        self.assertEqual(run(doc("1"), {"$schema": "https://json-schema.org/draft/2020-12/schema"}
                             ).schema_draft, DRAFT_2020_12)
        self.assertEqual(run(doc("1"), {}).schema_draft, DRAFT_07)

    def test_later_keyword_under_earlier_draft_warns(self):
        schema = {"$schema": "http://json-schema.org/draft-07/schema#", "prefixItems": [{}]}
        result = run(doc("[1]"), schema)
        self.assertTrue(result.valid)
        self.assertIn("keyword_draft_mismatch", result.warnings[0])
        self.assertIn("prefixItems", result.warnings[0])


class TestUnknownKeywords(unittest.TestCase):
    def test_unknown_keyword_warns_and_does_not_fail(self):
        schema = {"type": "object", "fooBar": 1, "propertees": {"a": {}}}
        result = run(doc('{"a":1}'), schema)
        self.assertTrue(result.valid)
        self.assertEqual(len(result.warnings), 2)
        self.assertIn('unknown_keyword: "fooBar" at #', result.warnings[0])
        self.assertIn('unknown_keyword: "propertees" at #', result.warnings[1])

    def test_unknown_keyword_reports_schema_path(self):
        schema = {"properties": {"a": {"items": {"madeUp": True}}}}
        result = run(doc('{"a":[1]}'), schema)
        self.assertTrue(result.valid)
        self.assertIn("#/properties/a/items", result.warnings[0])

    def test_unknown_keyword_warned_once_for_many_nodes(self):
        schema = {"items": {"oops": 1, "type": "integer"}}
        result = run(doc("[1,2,3,4,5]"), schema)
        self.assertEqual(len([w for w in result.warnings if "oops" in w]), 1)

    def test_annotation_keywords_do_not_warn(self):
        schema = {"title": "T", "description": "D", "default": 1, "examples": [1],
                  "deprecated": False, "readOnly": True, "writeOnly": False,
                  "$comment": "hi", "$schema": "http://json-schema.org/draft-07/schema#",
                  "definitions": {}, "$defs": {}}
        result = run(doc("1"), schema)
        self.assertTrue(result.valid)
        self.assertEqual([w for w in result.warnings if "unknown_keyword" in w], [])

    def test_unsupported_keywords_warn(self):
        for keyword in ("unevaluatedProperties", "unevaluatedItems", "$dynamicRef",
                        "$recursiveRef", "contentSchema"):
            with self.subTest(keyword=keyword):
                value = False if keyword.startswith("unevaluated") else "#"
                result = run(doc('{"a":1}'), {keyword: value})
                self.assertTrue(result.valid)
                notes = [w for w in result.warnings if w.startswith("keyword_unsupported")]
                self.assertEqual(len(notes), 1, result.warnings)
                self.assertIn(keyword, notes[0])

    def test_warning_codes_helper(self):
        result = run(doc('"x"'), {"oops": 1, "format": "hostname"})
        self.assertEqual(set(warning_codes(result)), {"unknown_keyword", "format_annotation"})

    def test_warnings_are_capped(self):
        schema = {f"unknown{i}": i for i in range(200)}
        result = run(doc("1"), schema)
        self.assertTrue(result.valid)
        self.assertLessEqual(len(result.warnings), jsonschemav.MAX_WARNINGS + 1)
        self.assertIn("warnings_suppressed", result.warnings[-1])


class TestRobustness(unittest.TestCase):
    def test_never_raises_on_broken_schemas(self):
        broken = [None, 5, "schema", [], {"type": None}, {"properties": None},
                  {"required": None}, {"items": None}, {"allOf": None}, {"enum": None},
                  {"minimum": None}, {"pattern": None}, {"uniqueItems": None},
                  {"$ref": None}, {"$ref": ""}, {"contains": None}, {"not": None},
                  {"propertyNames": None}, {"dependentRequired": None},
                  {"dependencies": None}, {"minLength": None}, {"additionalItems": None},
                  {"additionalProperties": None}, {"patternProperties": None},
                  {"if": None}, {"then": None}, {"oneOf": None}]
        instances = [doc("1"), doc('"x"'), doc("{}"), doc("[]"), doc("null"), doc("true"),
                     doc('{"a":[1,{"b":null}]}')]
        for schema in broken:
            for instance in instances:
                with self.subTest(schema=schema, instance=instance):
                    result = validate(instance, schema)
                    self.assertIsInstance(result, SchemaValidationResult)
                    self.assertIsInstance(result.valid, bool)

    def test_never_raises_on_foreign_instance_values(self):
        foreign = [{1, 2}, 1.5, float("nan"), float("inf"), object(), (1, 2), b"bytes",
                   Decimal("1.5"), JsonNumber("1.5"), 10 ** 400]
        schemas = [{}, {"type": "number"}, {"type": "string"}, {"type": "integer"},
                   {"minimum": 1}, {"multipleOf": 2}, {"uniqueItems": True},
                   {"enum": [1, "x"]}, {"const": 1}, {"pattern": "a"},
                   {"properties": {"a": {"type": "integer"}}}]
        for instance in foreign:
            for schema in schemas:
                with self.subTest(instance=type(instance).__name__, schema=schema):
                    result = validate(instance, schema)
                    self.assertIsInstance(result, SchemaValidationResult)
                    self.assertIsInstance(result.valid, bool)
        self.assertTrue(run(1.5, {"type": "number"}).valid)
        self.assertFalse(run(1.5, {"type": "integer"}).valid)
        self.assertTrue(run(2.0, {"type": "integer"}).valid)
        self.assertTrue(run((1, 2), {"type": "array", "items": {"type": "integer"}}).valid)

    def test_schema_as_text_is_rejected_clearly(self):
        result = validate(doc("1"), '{"type":"integer"}')
        self.assertFalse(result.valid)
        self.assertEqual(result.errors[0].keyword, "schema_invalid")
        self.assertIn("not its JSON text", result.errors[0].message)

    def test_handler_exception_is_contained(self):
        with mock.patch.object(jsonschemav, "json_type", side_effect=RuntimeError("boom")):
            result = validate(doc("1"), {"type": "integer"})
        self.assertFalse(result.valid)
        self.assertEqual(result.errors[0].keyword, "internal_error")
        self.assertIn("boom", result.errors[0].message)

    def test_instance_and_schema_are_never_mutated(self):
        instance = doc('{"a":[1,{"b":2}],"c":"x"}')
        schema = {"properties": {"a": {"items": {"type": "integer"}}, "c": {"type": "integer"}},
                  "required": ["a", "z"], "additionalProperties": False}
        before_instance = repr(instance)
        before_schema = repr(schema)
        result = run(instance, schema)
        self.assertFalse(result.valid)
        self.assertEqual(repr(instance), before_instance)
        self.assertEqual(repr(schema), before_schema)
        self.assertEqual(list(instance.keys()), ["a", "c"])
        self.assertEqual(list(schema["properties"].keys()), ["a", "c"])

    def test_max_errors_truncation(self):
        instance = doc("[" + ",".join('{"v":"x"}' for _ in range(60)) + "]")
        schema = {"items": {"properties": {"v": {"type": "integer"}}}}
        result = run(instance, schema, max_errors=5)
        self.assertFalse(result.valid)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.errors), 5)
        self.assertEqual(result.error_count, 5)
        full = run(instance, schema, max_errors=1000)
        self.assertFalse(full.truncated)
        self.assertEqual(len(full.errors), 60)

    def test_max_errors_clamped(self):
        result = run(doc('{"a":1}'), {"properties": {"a": {"type": "string"}}}, max_errors=0)
        self.assertEqual(len(result.errors), 1)
        result = run(doc('{"a":1}'), {"properties": {"a": {"type": "string"}}}, max_errors="x")
        self.assertEqual(len(result.errors), 1)

    def test_deep_instance_1000_plus_levels(self):
        depth = 1300
        text = '{"c":' * depth + "1" + "}" * depth
        instance = parse_json(text, max_depth=5000).value
        schema = {"type": "object"}
        current = schema
        for _ in range(depth):
            current["properties"] = {"c": {"type": "object"}}
            current = current["properties"]["c"]
        current["type"] = "integer"
        started = time.perf_counter()
        result = run(instance, schema)
        self.assertLess(time.perf_counter() - started, 5.0)
        self.assertTrue(result.valid)
        bad = parse_json('{"c":' * depth + '"x"' + "}" * depth, max_depth=5000).value
        result = run(bad, schema)
        self.assertFalse(result.valid)
        self.assertEqual(result.errors[0].keyword, "type")
        self.assertEqual(len(result.errors[0].path), depth)

    def test_recursion_limit_is_not_touched(self):
        depth = 1200
        node = {}
        current = node
        for _ in range(depth):
            current["c"] = {}
            current = current["c"]
        schema = {"$defs": {"n": {"properties": {"c": {"$ref": "#/$defs/n"}}}},
                  "$ref": "#/$defs/n"}
        limit = sys.getrecursionlimit()
        try:
            sys.setrecursionlimit(200)  # 比 depth 小得多：递归实现必然爆栈
            result = run(node, schema)
        finally:
            sys.setrecursionlimit(limit)
        self.assertTrue(result.valid)
        self.assertEqual(result.error_count, 0)

    def test_budget_produces_a_clear_error(self):
        instance = doc('{"a":[1,2,3],"b":{"c":4}}')
        schema = {"properties": {"a": {"items": {"type": "integer"}},
                                 "b": {"properties": {"c": {"type": "string"}}}}}
        self.assertFalse(run(instance, schema).valid)
        with mock.patch.object(jsonschemav, "MAX_STEPS", 3):
            result = validate(instance, schema)
        self.assertFalse(result.valid)
        self.assertTrue(result.truncated)
        self.assertEqual(result.errors[-1].keyword, "budget_exceeded")
        self.assertIn("safety limit", result.errors[-1].message)
        self.assertLess(result.checked_nodes, 10)

    def test_result_defaults_and_helpers(self):
        result = run(doc("1"), {"type": "integer", "oops": 1})
        self.assertTrue(result.valid)
        self.assertEqual(result.errors, [])
        self.assertIsNone(result.first_error())
        self.assertEqual(result.keywords(), [])
        self.assertEqual(result.error_count, 0)
        self.assertGreaterEqual(result.elapsed_ms, 0.0)
        self.assertEqual(result.truncated, False)
        failing = run(doc('"x"'), {"type": "integer", "minimum": 1})
        self.assertEqual(failing.keywords(), ["type"])
        self.assertEqual(error_keywords(failing), ["type"])
        self.assertIsNotNone(failing.first_error())

    def test_schema_error_is_frozen_and_describable(self):
        error = SchemaError(path=("a", 0), path_string="$.a[0]", keyword="type",
                            message="expected integer, got string")
        with self.assertRaises(Exception):
            error.keyword = "enum"
        self.assertEqual(error.describe(), "$.a[0]: expected integer, got string")
        self.assertEqual(error.schema_path, ())
        self.assertIsNone(error.expected)
        self.assertIsNone(error.actual)


class TestInferSchema(unittest.TestCase):
    def test_representative_document_round_trips(self):
        text = ('{"id":7,"name":"widget","price":19.99,"tags":["a","bb","ccc"],'
                '"active":true,"deleted":null,'
                '"stock":{"count":3,"warehouse":["A","B"]},'
                '"reviews":[{"author":"a","stars":5},{"author":"bb","stars":3}]}')
        instance = doc(text)
        schema = infer_schema(instance)
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft-07/schema#")
        self.assertEqual(schema["type"], "object")
        self.assertEqual(sorted(schema["required"]),
                         ["active", "deleted", "id", "name", "price", "reviews", "stock", "tags"])
        self.assertEqual(schema["properties"]["id"]["type"], "integer")
        self.assertEqual(schema["properties"]["id"]["minimum"], 7)
        self.assertEqual(schema["properties"]["id"]["maximum"], 7)
        self.assertEqual(schema["properties"]["price"]["type"], "number")
        self.assertEqual(schema["properties"]["price"]["minimum"], JsonNumber("19.99"))
        self.assertEqual(schema["properties"]["name"]["minLength"], 6)
        self.assertEqual(schema["properties"]["name"]["maxLength"], 6)
        self.assertEqual(schema["properties"]["tags"]["items"]["minLength"], 1)
        self.assertEqual(schema["properties"]["tags"]["items"]["maxLength"], 3)
        self.assertEqual(schema["properties"]["active"], {"type": "boolean"})
        self.assertEqual(schema["properties"]["deleted"], {"type": "null"})
        self.assertEqual(schema["properties"]["stock"]["properties"]["count"]["type"], "integer")
        self.assertEqual(sorted(schema["properties"]["reviews"]["items"]["required"]),
                         ["author", "stars"])
        result = run(instance, schema)
        self.assertTrue(result.valid, [error.message for error in result.errors])
        self.assertEqual(result.schema_draft, DRAFT_07)

    def test_inferred_schema_rejects_a_wrong_document(self):
        instance = doc('{"a":1,"b":"x"}')
        schema = infer_schema(instance)
        self.assertFalse(run(doc('{"a":"no","b":"x"}'), schema).valid)
        self.assertFalse(run(doc('{"b":"x"}'), schema).valid)
        self.assertFalse(run(doc('{"a":1,"b":"x","c":1}'),
                             dict(schema, additionalProperties=False)).valid)

    def test_jsonnumber_is_not_turned_into_float(self):
        instance = doc('{"big":123456789012345678901234567890,"small":0.0001,"exp":1e30}')
        schema = infer_schema(instance)
        self.assertEqual(schema["properties"]["big"]["type"], "integer")
        self.assertEqual(schema["properties"]["big"]["minimum"],
                         123456789012345678901234567890)
        self.assertEqual(schema["properties"]["small"]["type"], "number")
        self.assertIsInstance(schema["properties"]["small"]["minimum"], JsonNumber)
        self.assertEqual(schema["properties"]["exp"]["type"], "number")
        text = schema_to_text(schema)
        self.assertIn("123456789012345678901234567890", text)
        self.assertIn("0.0001", text)
        self.assertIn("1e30", text)
        self.assertTrue(run(instance, schema).valid)

    def test_mixed_types_become_a_type_array(self):
        schema = infer_schema(doc('[1,"a",null,true]'))
        self.assertEqual(schema["type"], "array")
        self.assertEqual(schema["items"]["type"], ["string", "number", "boolean", "null"])
        self.assertTrue(run(doc('[1,"a",null,true]'), schema).valid)

    def test_required_is_intersection_over_all_array_elements(self):
        items = ",".join('{"a":1,"b":2}' if index % 2 else '{"a":1}' for index in range(40))
        schema = infer_schema(doc("[" + items + "]"))
        self.assertEqual(schema["items"]["required"], ["a"])
        self.assertEqual(sorted(schema["items"]["properties"]), ["a", "b"])
        self.assertTrue(run(doc("[" + items + "]"), schema).valid)

    def test_min_max_come_from_every_element_not_just_the_sample(self):
        values = ",".join(str(index) for index in range(500))
        schema = infer_schema(doc("[" + values + "]"))
        self.assertEqual(schema["items"]["minimum"], 0)
        self.assertEqual(schema["items"]["maximum"], 499)

    def test_sample_arrays_limits_deep_sampling(self):
        small = infer_schema(doc("[1,2,3]"), sample_arrays=2)
        self.assertEqual(small["items"]["type"], "integer")
        self.assertEqual(small["items"]["minimum"], 1)
        self.assertEqual(small["items"]["maximum"], 3)  # 浅层信息覆盖全部元素
        text = '[[{"a":1}],[{"a":3}]]'
        nested = infer_schema(doc(text), sample_arrays=1, max_depth=8)
        self.assertEqual(nested["items"]["items"]["properties"]["a"]["maximum"], 1)
        # 深层结构只来自被采样的元素：这是文档化的限制，未采样元素可能被判为不合法
        self.assertFalse(run(doc(text), nested).valid)
        full = infer_schema(doc(text), sample_arrays=10)
        self.assertEqual(full["items"]["items"]["properties"]["a"]["maximum"], 3)
        self.assertTrue(run(doc(text), full).valid)

    def test_max_depth_stops_recursion(self):
        text = '{"a":' * 12 + "1" + "}" * 12
        instance = doc(text)
        schema = infer_schema(instance, max_depth=3)
        node = schema
        for _ in range(3):
            node = node["properties"]["a"]
        self.assertEqual(node, {"type": "object", "required": ["a"]})
        self.assertNotIn("properties", node)
        self.assertTrue(run(instance, schema).valid)
        deep = infer_schema(instance, max_depth=20)
        self.assertTrue(run(instance, deep).valid)
        leaf = deep
        for _ in range(12):
            leaf = leaf["properties"]["a"]
        self.assertEqual(leaf, {"type": "integer", "minimum": 1, "maximum": 1})

    def test_scalars_and_empty_containers(self):
        self.assertEqual(infer_schema(doc('"hello"')),
                         {"$schema": "https://json-schema.org/draft-07/schema#",
                          "type": "string", "minLength": 5, "maxLength": 5})
        self.assertEqual(infer_schema(doc("null")),
                         {"$schema": "https://json-schema.org/draft-07/schema#", "type": "null"})
        self.assertEqual(infer_schema(doc("true")),
                         {"$schema": "https://json-schema.org/draft-07/schema#",
                          "type": "boolean"})
        self.assertEqual(infer_schema(doc("[]")),
                         {"$schema": "https://json-schema.org/draft-07/schema#", "type": "array"})
        self.assertEqual(infer_schema(doc("{}")),
                         {"$schema": "https://json-schema.org/draft-07/schema#", "type": "object"})
        self.assertEqual(infer_schema(doc("3")),
                         {"$schema": "https://json-schema.org/draft-07/schema#",
                          "type": "integer", "minimum": 3, "maximum": 3})
        for instance in (doc('"hello"'), doc("null"), doc("true"), doc("[]"), doc("{}"),
                         doc("3")):
            with self.subTest(instance=instance):
                self.assertTrue(run(instance, infer_schema(instance)).valid)

    def test_deep_instance_does_not_recurse(self):
        depth = 1200
        instance = parse_json('{"a":' * depth + "1" + "}" * depth, max_depth=5000).value
        schema = infer_schema(instance, max_depth=4)
        self.assertTrue(run(instance, schema).valid)

    def test_bad_options_are_clamped(self):
        instance = doc('{"a":1}')
        self.assertEqual(infer_schema(instance, max_depth="x"), infer_schema(instance))
        self.assertEqual(infer_schema(instance, sample_arrays=None), infer_schema(instance))
        self.assertTrue(run(instance, infer_schema(instance, max_depth=-5)).valid)


class TestSchemaToText(unittest.TestCase):
    def test_pretty_print(self):
        text = schema_to_text({"type": "object", "properties": {"a": {"type": "integer"}}})
        self.assertEqual(text, '{\n  "type": "object",\n  "properties": {\n'
                               '    "a": {\n      "type": "integer"\n    }\n  }\n}')

    def test_boolean_and_scalars(self):
        self.assertEqual(schema_to_text(True), "true")
        self.assertEqual(schema_to_text(False), "false")
        self.assertEqual(schema_to_text({}), "{}")

    def test_key_order_is_preserved(self):
        text = schema_to_text({"z": 1, "a": 2})
        self.assertLess(text.index('"z"'), text.index('"a"'))

    def test_jsonnumber_survives(self):
        text = schema_to_text({"minimum": JsonNumber("1.230")})
        self.assertIn("1.230", text)

    def test_round_trip_through_parser(self):
        schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}
        self.assertEqual(parse_json(schema_to_text(schema)).value, schema)


class TestPerformance(unittest.TestCase):
    def test_50k_node_instance_under_5_seconds(self):
        rows = [{"id": index, "name": f"user{index}", "tags": ["a", "b"],
                 "score": index % 100, "active": bool(index % 2),
                 "meta": {"x": index, "y": [1, 2, 3]}} for index in range(4200)]
        schema = {
            "type": "object",
            "properties": {"rows": {"type": "array", "items": {
                "type": "object",
                "required": ["id", "name"],
                "properties": {
                    "id": {"type": "integer", "minimum": 0},
                    "name": {"type": "string", "minLength": 1, "pattern": "^user"},
                    "tags": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
                    "score": {"type": "number", "maximum": 1000},
                    "active": {"type": "boolean"},
                    "meta": {"type": "object", "properties": {
                        "x": {"type": "integer"},
                        "y": {"type": "array", "items": {"type": "integer"}, "minItems": 1}}},
                }}}}}
        started = time.perf_counter()
        result = run({"rows": rows}, schema)
        elapsed = time.perf_counter() - started
        self.assertTrue(result.valid, [error.message for error in result.errors])
        self.assertLess(elapsed, 5.0)
        self.assertGreater(result.checked_nodes, 50000)
        self.assertLess(result.elapsed_ms, 5000.0)

    def test_many_errors_stay_bounded(self):
        instance = doc("[" + ",".join('"x"' for _ in range(20000)) + "]")
        started = time.perf_counter()
        result = run(instance, {"items": {"type": "integer"}}, max_errors=50)
        self.assertLess(time.perf_counter() - started, 5.0)
        self.assertEqual(len(result.errors), 50)
        self.assertTrue(result.truncated)


if __name__ == "__main__":
    unittest.main(verbosity=2)

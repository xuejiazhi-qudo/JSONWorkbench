"""生成器测试：确定性、地区语料、字段规格、JSON Schema 生成与自校验。

注意：本文件自带一个**极简 schema 校验器** :func:`check`，用来证明生成的值
确实满足给定的 schema；它刻意不导入项目里的 schema 校验模块（那是另一个模块
的职责），也不导入被测模块的内部函数。
"""

from __future__ import annotations

import copy
import random
import re
import unittest
import uuid
from datetime import date, datetime
from decimal import Decimal

from app.core.jsonformatter import minify
from app.core.jsonvalue import JsonNumber, json_type
from app.engines.generator import (
    FIELD_TYPES,
    LOCALES,
    MAX_GENERATE_COUNT,
    FieldSpec,
    MockOptions,
    field_spec_from_row,
    generate_from_fields,
    generate_from_schema,
    mock_value,
    path_string,
)

CJK = re.compile(r"[\u4e00-\u9fff]")
CJK_FULL = re.compile(r"[\u4e00-\u9fff]+")
ASCII_ONLY = re.compile(r"^[\x20-\x7e]*$")
EMAIL_RE = re.compile(r"^[a-z0-9._]+@[a-z0-9-]+(\.[a-z0-9-]+)+$")
ZH_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
EN_PHONE_RE = re.compile(r"^\([2-9]\d{2}\) [2-9]\d{2}-\d{4}$")
COLOR_RE = re.compile(r"^#[0-9a-f]{6}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")

ZH = MockOptions(locale="zh", seed=20260104)
EN = MockOptions(locale="en", seed=20260104)


# --------------------------------------------------------------------------- #
# 极简 schema 校验器（测试自带）
# --------------------------------------------------------------------------- #
def kind_of(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return "unknown"


def type_ok(actual, declared):
    if actual == declared:
        return True
    if declared == "number" and actual == "integer":
        return True
    return False


def eq(left, right):
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return left == right


def canon(value):
    if isinstance(value, dict):
        return ("o", tuple(sorted((k, canon(v)) for k, v in value.items())))
    if isinstance(value, list):
        return ("a", tuple(canon(item) for item in value))
    if isinstance(value, bool):
        return ("b", value)
    if value is None:
        return ("z",)
    if isinstance(value, (int, float)):
        return ("n", float(value))
    return ("s", value)


def resolve_ref(ref, root):
    if not isinstance(ref, str) or not ref.startswith("#"):
        return None
    pointer = ref[1:].lstrip("/")
    node = root
    if pointer == "":
        return root
    for token in pointer.split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
        else:
            return None
    return node


def check(instance, schema, root=None, path="$", depth=0):
    """返回 ``instance`` 违反 ``schema`` 的地方（空列表表示完全满足）。"""
    if root is None:
        root = schema
    if depth > 40 or schema is True or schema is None or not isinstance(schema, dict):
        return []
    if schema is False:
        return [f"{path}: schema is false"]
    problems = []
    ref = schema.get("$ref")
    if isinstance(ref, str):
        target = resolve_ref(ref, root)
        if target is None:
            return []  # 远程/无法解析的引用不在测试校验范围内
        return check(instance, target, root, path, depth + 1)

    declared = schema.get("type")
    if isinstance(declared, str):
        declared = [declared]
    if isinstance(declared, list) and declared:
        actual = kind_of(instance)
        if not any(type_ok(actual, item) for item in declared):
            return [f"{path}: expected one of {declared}, got {actual}"]

    if "const" in schema and not eq(instance, schema["const"]):
        problems.append(f"{path}: const mismatch")
    enum = schema.get("enum")
    if isinstance(enum, list) and enum and not any(eq(instance, e) for e in enum):
        problems.append(f"{path}: value not in enum")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            problems.append(f"{path}: below minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            problems.append(f"{path}: above maximum")
        if isinstance(schema.get("exclusiveMinimum"), (int, float)) and \
                not isinstance(schema["exclusiveMinimum"], bool) and \
                instance <= schema["exclusiveMinimum"]:
            problems.append(f"{path}: not above exclusiveMinimum")
        if isinstance(schema.get("exclusiveMaximum"), (int, float)) and \
                not isinstance(schema["exclusiveMaximum"], bool) and \
                instance >= schema["exclusiveMaximum"]:
            problems.append(f"{path}: not below exclusiveMaximum")
        multiple = schema.get("multipleOf")
        if isinstance(multiple, (int, float)) and multiple:
            remainder = Decimal(str(instance)) % Decimal(str(multiple))
            if remainder != 0:
                problems.append(f"{path}: not a multiple of {multiple}")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            problems.append(f"{path}: shorter than minLength")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            problems.append(f"{path}: longer than maxLength")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and pattern:
            try:
                if re.search(pattern, instance) is None:
                    problems.append(f"{path}: does not match pattern {pattern}")
            except re.error:
                pass

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            problems.append(f"{path}: fewer than minItems")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            problems.append(f"{path}: more than maxItems")
        if schema.get("uniqueItems"):
            seen = [canon(item) for item in instance]
            if len(set(seen)) != len(seen):
                problems.append(f"{path}: items are not unique")
        prefixes = schema.get("prefixItems")
        if isinstance(prefixes, list):
            for index, sub in enumerate(prefixes):
                if index < len(instance):
                    problems += check(instance[index], sub, root, f"{path}[{index}]",
                                      depth + 1)
        items = schema.get("items")
        if isinstance(items, dict) or items is True or items is False:
            start = len(prefixes) if isinstance(prefixes, list) else 0
            for index in range(start, len(instance)):
                problems += check(instance[index], items, root, f"{path}[{index}]",
                                  depth + 1)

    if isinstance(instance, dict):
        for key in schema.get("required", []) or []:
            if key not in instance:
                problems.append(f"{path}: missing required property '{key}'")
        properties = schema.get("properties") or {}
        for key, value in instance.items():
            if key in properties:
                problems += check(value, properties[key], root, f"{path}.{key}", depth + 1)
            elif schema.get("additionalProperties") is False:
                problems.append(f"{path}: additional property '{key}'")
            elif isinstance(schema.get("additionalProperties"), dict):
                problems += check(value, schema["additionalProperties"], root,
                                  f"{path}.{key}", depth + 1)
        if "minProperties" in schema and len(instance) < schema["minProperties"]:
            problems.append(f"{path}: fewer than minProperties")
        if "maxProperties" in schema and len(instance) > schema["maxProperties"]:
            problems.append(f"{path}: more than maxProperties")

    for sub in schema.get("allOf", []) or []:
        problems += check(instance, sub, root, path, depth + 1)
    for keyword in ("anyOf", "oneOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list) and branches:
            passed = sum(1 for sub in branches
                         if not check(instance, sub, root, path, depth + 1))
            if keyword == "anyOf" and passed == 0:
                problems.append(f"{path}: matches no anyOf branch")
            if keyword == "oneOf" and passed != 1:
                problems.append(f"{path}: matches {passed} oneOf branches")
    return problems


# --------------------------------------------------------------------------- #
# 测试用 schema
# --------------------------------------------------------------------------- #
REPRESENTATIVE_SCHEMA = {
    "$defs": {
        "Tag": {
            "type": "object",
            "required": ["name"],
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "minLength": 2, "maxLength": 12},
                "weight": {"type": "number", "minimum": 0, "maximum": 1},
            },
        },
        "Status": {"type": "string", "enum": ["new", "done", "blocked"]},
    },
    "type": "object",
    "required": ["id", "code", "name", "age", "score", "tags", "status", "nested"],
    "additionalProperties": False,
    "properties": {
        "id": {"type": "integer", "minimum": 1, "maximum": 100000},
        "code": {"type": "string", "pattern": r"^[A-Z]{3}-\d{4}$"},
        "name": {"type": "string", "minLength": 1, "maxLength": 20},
        "age": {"type": "integer", "exclusiveMinimum": 17, "exclusiveMaximum": 66},
        "score": {"type": "number", "minimum": -10, "maximum": 10, "multipleOf": 0.5},
        "step": {"type": "integer", "minimum": 0, "maximum": 100, "multipleOf": 5},
        "tags": {
            "type": "array", "minItems": 2, "maxItems": 4, "uniqueItems": True,
            "items": {"$ref": "#/$defs/Tag"},
        },
        "status": {"$ref": "#/$defs/Status"},
        "flag": {"type": "boolean"},
        "nothing": {"type": "null"},
        "either": {"anyOf": [{"type": "string", "const": "fixed"}, {"type": "null"}]},
        "mixed": {"type": ["string", "null"], "maxLength": 8},
        "nested": {
            "type": "object",
            "required": ["when", "who"],
            "properties": {
                "when": {"type": "string", "format": "date-time"},
                "who": {"type": "string", "format": "email"},
                "ip": {"type": "string", "format": "ipv4"},
                "tuple": {
                    "type": "array",
                    "prefixItems": [{"type": "integer"}, {"type": "string"}],
                    "minItems": 2,
                },
            },
        },
        "combined": {
            "allOf": [
                {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer", "minimum": 0}}},
                {"properties": {"b": {"type": "string", "maxLength": 6}}},
            ]
        },
    },
}

RECURSIVE_SCHEMA = {
    "$defs": {
        "Node": {
            "type": "object",
            "required": ["id", "children"],
            "properties": {
                "id": {"type": "integer", "minimum": 1, "maximum": 999},
                "children": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 2,
                    "items": {"anyOf": [{"$ref": "#/$defs/Node"}, {"type": "null"}]},
                },
            },
        }
    },
    "$ref": "#/$defs/Node",
}


class TestMockValueKinds(unittest.TestCase):
    EXPECTED_TYPES = {
        "string": "string", "integer": "number", "number": "number",
        "boolean": "boolean", "null": "null", "date": "string", "datetime": "string",
        "email": "string", "phone": "string", "name": "string", "uuid": "string",
        "address": "string", "company": "string", "color": "string", "ip": "string",
        "word": "string", "paragraph": "string", "enum": "string",
        "object": "object", "array": "array",
    }

    def test_field_types_vocabulary(self):
        self.assertEqual(len(FIELD_TYPES), 20)
        self.assertEqual(len(set(FIELD_TYPES)), 20)
        self.assertEqual(set(self.EXPECTED_TYPES), set(FIELD_TYPES))
        self.assertEqual(LOCALES, ("zh", "en"))

    def test_every_kind_has_the_right_json_type(self):
        for kind in FIELD_TYPES:
            for options in (ZH, EN):
                with self.subTest(kind=kind, locale=options.locale):
                    value = mock_value(kind, options)
                    self.assertEqual(json_type(value), self.EXPECTED_TYPES[kind])

    def test_integer_and_number_are_native(self):
        self.assertIsInstance(mock_value("integer", ZH), int)
        self.assertNotIsInstance(mock_value("integer", ZH), bool)
        self.assertIsInstance(mock_value("number", ZH), float)
        self.assertNotIsInstance(mock_value("number", ZH), Decimal)
        self.assertNotIsInstance(mock_value("number", ZH), JsonNumber)

    def test_boolean_and_null(self):
        self.assertIsInstance(mock_value("boolean", ZH), bool)
        self.assertIsNone(mock_value("null", ZH))

    def test_object_and_array_are_empty_containers(self):
        self.assertEqual(mock_value("object", ZH), {})
        self.assertEqual(mock_value("array", ZH), [])

    def test_unknown_kind_falls_back_to_string(self):
        for kind in ("nonsense", "", None, 123):
            with self.subTest(kind=kind):
                self.assertEqual(json_type(mock_value(kind, ZH)), "string")

    def test_options_are_optional(self):
        self.assertEqual(json_type(mock_value("name")), "string")
        self.assertEqual(json_type(mock_value("ip", MockOptions())), "string")

    def test_date_and_datetime_format(self):
        for options in (ZH, EN):
            text = mock_value("date", options)
            self.assertRegex(text, DATE_RE)
            self.assertIsInstance(date.fromisoformat(text), date)
            stamp = mock_value("datetime", options)
            self.assertRegex(stamp, DATETIME_RE)
            self.assertIsInstance(datetime.fromisoformat(stamp), datetime)

    def test_uuid_is_version_4_and_seeded(self):
        text = mock_value("uuid", MockOptions(seed=1))
        parsed = uuid.UUID(text)
        self.assertEqual(parsed.version, 4)
        self.assertEqual(text, mock_value("uuid", MockOptions(seed=1)))
        self.assertNotEqual(text, mock_value("uuid", MockOptions(seed=2)))

    def test_color_format(self):
        for _ in range(20):
            self.assertRegex(mock_value("color", ZH), COLOR_RE)

    def test_ip_quads(self):
        for _ in range(20):
            text = mock_value("ip", ZH)
            quads = text.split(".")
            self.assertEqual(len(quads), 4)
            for quad in quads:
                self.assertTrue(quad.isdigit())
                self.assertTrue(0 <= int(quad) <= 255)

    def test_email_format(self):
        for options in (ZH, EN):
            for _ in range(20):
                text = mock_value("email", options)
                self.assertRegex(text, EMAIL_RE)
                self.assertEqual(text, text.lower())
                self.assertNotIn(" ", text)
                local, _, domain = text.partition("@")
                self.assertTrue(local)
                self.assertIn(".", domain)

    def test_chinese_phone_number(self):
        prefixes = {"13", "14", "15", "16", "17", "18", "19"}
        for _ in range(50):
            text = mock_value("phone", ZH)
            self.assertRegex(text, ZH_PHONE_RE)
            self.assertEqual(len(text), 11)
            self.assertIn(text[:2], prefixes)
            self.assertTrue(text.isdigit())

    def test_english_phone_number(self):
        for _ in range(50):
            self.assertRegex(mock_value("phone", EN), EN_PHONE_RE)


class TestLocaleContent(unittest.TestCase):
    def test_chinese_text_uses_cjk(self):
        for kind in ("name", "address", "company", "word", "paragraph", "string"):
            with self.subTest(kind=kind):
                text = mock_value(kind, ZH)
                self.assertTrue(CJK.search(text), text)

    def test_english_text_is_ascii(self):
        for kind in ("name", "address", "company", "word", "paragraph", "string"):
            with self.subTest(kind=kind):
                text = mock_value(kind, EN)
                self.assertRegex(text, ASCII_ONLY)

    def test_chinese_name_is_surname_plus_given(self):
        for _ in range(30):
            name = mock_value("name", ZH)
            self.assertTrue(2 <= len(name) <= 4, name)
            self.assertTrue(CJK_FULL.fullmatch(name), name)

    def test_english_name_has_two_parts(self):
        for _ in range(30):
            name = mock_value("name", EN)
            parts = name.split(" ")
            self.assertEqual(len(parts), 2, name)
            self.assertTrue(parts[0][0].isupper())
            self.assertTrue(parts[1][0].isupper())

    def test_chinese_address_has_region_and_street(self):
        text = mock_value("address", ZH)
        self.assertTrue(any(token in text for token in ("省", "市")))
        self.assertTrue(any(token in text for token in ("区", "县")))
        self.assertIn("号", text)
        self.assertTrue(CJK.search(text))

    def test_english_address_shape(self):
        for _ in range(20):
            text = mock_value("address", EN)
            self.assertRegex(text, r"^\d+ .+, .+, [A-Z]{2} \d{5}$")

    def test_chinese_company_ends_with_legal_suffix(self):
        for _ in range(20):
            text = mock_value("company", ZH)
            self.assertTrue(text.endswith(("有限公司", "股份有限公司", "有限责任公司",
                                           "集团有限公司")), text)

    def test_paragraph_has_multiple_sentences(self):
        zh = mock_value("paragraph", ZH)
        self.assertGreaterEqual(zh.count("。"), 2)
        en = mock_value("paragraph", EN)
        self.assertGreaterEqual(en.count("."), 2)
        self.assertTrue(en[0].isupper())

    def test_locale_normalization(self):
        self.assertEqual(MockOptions(locale="zh-CN").normalized().locale, "zh")
        self.assertEqual(MockOptions(locale="ZH").normalized().locale, "zh")
        self.assertEqual(MockOptions(locale="en-US").normalized().locale, "en")
        self.assertEqual(MockOptions(locale="fr").normalized().locale, "en")
        self.assertEqual(MockOptions(locale=None).normalized().locale, "zh")
        self.assertEqual(MockOptions(count="x").normalized().count, 1)
        self.assertEqual(MockOptions(count=-5).normalized().count, 1)


class TestDeterminism(unittest.TestCase):
    FIELDS = [
        FieldSpec(name="id", type="integer", minimum=1, maximum=999),
        FieldSpec(name="name", type="name"),
        FieldSpec(name="mail", type="email"),
        FieldSpec(name="tags", type="array", array_length=3,
                  items=FieldSpec(name="t", type="word")),
        FieldSpec(name="meta", type="object",
                  children=(FieldSpec(name="at", type="datetime"),
                            FieldSpec(name="uid", type="uuid"))),
        FieldSpec(name="score", type="number", minimum=0, maximum=1),
        FieldSpec(name="color", type="color"),
    ]

    def test_same_seed_is_byte_identical(self):
        first = generate_from_fields(self.FIELDS, options=MockOptions(locale="zh", seed=7))
        second = generate_from_fields(self.FIELDS, options=MockOptions(locale="zh", seed=7))
        self.assertEqual(minify(first), minify(second))

    def test_different_seeds_differ(self):
        first = generate_from_fields(self.FIELDS, options=MockOptions(locale="zh", seed=7))
        second = generate_from_fields(self.FIELDS, options=MockOptions(locale="zh", seed=8))
        self.assertNotEqual(minify(first), minify(second))

    def test_seed_none_still_works(self):
        value = generate_from_fields(self.FIELDS, options=MockOptions(seed=None))
        self.assertEqual(sorted(value), sorted(spec.name for spec in self.FIELDS))

    def test_global_random_is_untouched(self):
        random.seed(987654321)
        expected = random.random()
        random.seed(987654321)
        generate_from_fields(self.FIELDS, options=MockOptions(seed=3))
        generate_from_schema(REPRESENTATIVE_SCHEMA, options=MockOptions(seed=3))
        mock_value("uuid", MockOptions(seed=3))
        self.assertEqual(random.random(), expected)

    def test_schema_generation_is_deterministic(self):
        options = MockOptions(locale="en", seed=17)
        first, _ = generate_from_schema(REPRESENTATIVE_SCHEMA, options=options,
                                        max_depth=8, array_items=3)
        second, _ = generate_from_schema(REPRESENTATIVE_SCHEMA, options=options,
                                         max_depth=8, array_items=3)
        self.assertEqual(minify(first), minify(second))
        third, _ = generate_from_schema(REPRESENTATIVE_SCHEMA,
                                        options=MockOptions(locale="en", seed=18),
                                        max_depth=8, array_items=3)
        self.assertNotEqual(minify(first), minify(third))

    def test_output_is_plain_json_types(self):
        value = generate_from_fields(self.FIELDS, options=MockOptions(seed=1))
        text = minify(value)          # 含 Decimal/JsonNumber 也能序列化，但要排除
        self.assertIsInstance(text, str)
        for spec in self.FIELDS:
            item = value[spec.name]
            if isinstance(item, (int, float)):
                self.assertNotIsInstance(item, (Decimal, JsonNumber))


class TestFieldSpecGeneration(unittest.TestCase):
    def test_integer_bounds_are_inclusive(self):
        spec = FieldSpec(name="n", type="integer", minimum=5, maximum=9)
        seen = set()
        for seed in range(60):
            value = generate_from_fields([spec], options=MockOptions(seed=seed))["n"]
            self.assertIsInstance(value, int)
            self.assertTrue(5 <= value <= 9, value)
            seen.add(value)
        self.assertIn(5, seen)
        self.assertIn(9, seen)

    def test_number_bounds(self):
        spec = FieldSpec(name="n", type="number", minimum=-2.5, maximum=-1.5)
        for seed in range(40):
            value = generate_from_fields([spec], options=MockOptions(seed=seed))["n"]
            self.assertIsInstance(value, float)
            self.assertTrue(-2.5 <= value <= -1.5, value)

    def test_narrow_number_bounds_stay_inside(self):
        spec = FieldSpec(name="n", type="number", minimum=0.001, maximum=0.002)
        for seed in range(20):
            value = generate_from_fields([spec], options=MockOptions(seed=seed))["n"]
            self.assertTrue(0.001 <= value <= 0.002, value)
        degenerate = FieldSpec(name="n", type="number", minimum=3.25, maximum=3.25)
        for seed in range(5):
            value = generate_from_fields([degenerate], options=MockOptions(seed=seed))["n"]
            self.assertEqual(value, 3.25)

    def test_reversed_bounds_are_normalized(self):
        spec = FieldSpec(name="n", type="integer", minimum=9, maximum=5).normalized()
        self.assertEqual((spec.minimum, spec.maximum), (5.0, 9.0))

    def test_exact_length(self):
        spec = FieldSpec(name="s", type="string", length=7)
        for seed in range(10):
            value = generate_from_fields([spec],
                                         options=MockOptions(locale="en", seed=seed))["s"]
            self.assertEqual(len(value), 7)

    def test_length_range(self):
        spec = FieldSpec(name="s", type="string", min_length=3, max_length=5)
        for seed in range(20):
            value = generate_from_fields([spec],
                                         options=MockOptions(locale="en", seed=seed))["s"]
            self.assertTrue(3 <= len(value) <= 5, value)

    def test_chinese_length_counts_characters(self):
        spec = FieldSpec(name="s", type="string", length=4)
        value = generate_from_fields([spec], options=MockOptions(locale="zh", seed=1))["s"]
        self.assertEqual(len(value), 4)
        self.assertTrue(CJK_FULL.fullmatch(value), value)

    def test_enum_wins_over_type(self):
        spec = FieldSpec(name="e", type="string", enum=("red", "green"))
        for seed in range(20):
            value = generate_from_fields([spec], options=MockOptions(seed=seed))["e"]
            self.assertIn(value, ("red", "green"))

    def test_pattern_template(self):
        spec = FieldSpec(name="p", type="string", pattern="###-AA")
        for seed in range(20):
            value = generate_from_fields([spec], options=MockOptions(seed=seed))["p"]
            self.assertRegex(value, r"^\d{3}-[A-Z]{2}$")
        lowercase = FieldSpec(name="p", type="string", pattern="aa#")
        for seed in range(10):
            value = generate_from_fields([lowercase], options=MockOptions(seed=seed))["p"]
            self.assertRegex(value, r"^[a-z]{2}\d$")

    def test_pattern_keeps_literals(self):
        spec = FieldSpec(name="p", type="string", pattern="ID:#")
        value = generate_from_fields([spec], options=MockOptions(seed=4))["p"]
        self.assertRegex(value, r"^ID:\d$")

    def test_children_build_nested_object(self):
        spec = FieldSpec(name="meta", type="object", children=(
            FieldSpec(name="flag", type="boolean"),
            FieldSpec(name="nothing", type="null"),
            FieldSpec(name="inner", type="object",
                      children=(FieldSpec(name="x", type="integer"),)),
        ))
        value = generate_from_fields([spec], options=MockOptions(seed=2))["meta"]
        self.assertEqual(sorted(value), ["flag", "inner", "nothing"])
        self.assertIsInstance(value["flag"], bool)
        self.assertIsNone(value["nothing"])
        self.assertIsInstance(value["inner"]["x"], int)

    def test_object_without_children(self):
        value = generate_from_fields([FieldSpec(name="o", type="object")],
                                     options=MockOptions(seed=1))["o"]
        self.assertEqual(value, {})

    def test_items_and_array_length(self):
        spec = FieldSpec(name="list", type="array", array_length=4,
                         items=FieldSpec(name="item", type="integer", minimum=0,
                                         maximum=3))
        value = generate_from_fields([spec], options=MockOptions(seed=6))["list"]
        self.assertEqual(len(value), 4)
        for item in value:
            self.assertTrue(0 <= item <= 3)

    def test_array_without_items_defaults_to_strings(self):
        value = generate_from_fields([FieldSpec(name="l", type="array", array_length=2)],
                                     options=MockOptions(seed=1))["l"]
        self.assertEqual(len(value), 2)
        self.assertTrue(all(isinstance(item, str) for item in value))

    def test_zero_array_length(self):
        value = generate_from_fields(
            [FieldSpec(name="l", type="array", array_length=0)], options=MockOptions(seed=1))["l"]
        self.assertEqual(value, [])

    def test_all_scalar_kinds_inside_a_record(self):
        specs = [FieldSpec(name=kind, type=kind) for kind in FIELD_TYPES]
        value = generate_from_fields(specs, options=MockOptions(locale="zh", seed=9))
        self.assertEqual(len(value), len(FIELD_TYPES))
        for kind in FIELD_TYPES:
            with self.subTest(kind=kind):
                self.assertEqual(json_type(value[kind]),
                                 TestMockValueKinds.EXPECTED_TYPES[kind])

    def test_self_referential_spec_terminates(self):
        spec = FieldSpec(name="loop", type="object")
        spec.children = (spec,)          # 人为造一个环，必须被深度上限挡住
        value = generate_from_fields([spec], options=MockOptions(seed=1))
        self.assertIsInstance(value, dict)
        text = minify(value)
        self.assertLess(len(text), 100000)

    def test_deeply_nested_spec_terminates(self):
        spec = FieldSpec(name="leaf", type="integer")
        for index in range(80):
            spec = FieldSpec(name=f"l{index}", type="object", children=(spec,))
        value = generate_from_fields([spec], options=MockOptions(seed=1))
        self.assertIsInstance(value, dict)


class TestGenerateFromFields(unittest.TestCase):
    FIELDS = [FieldSpec(name="a", type="integer"), FieldSpec(name="b", type="word")]

    def test_object_root_single_record(self):
        value = generate_from_fields(self.FIELDS, options=MockOptions(seed=1))
        self.assertIsInstance(value, dict)
        self.assertEqual(sorted(value), ["a", "b"])

    def test_array_root_with_count(self):
        value = generate_from_fields(self.FIELDS, root="array", count=5,
                                     options=MockOptions(seed=1))
        self.assertIsInstance(value, list)
        self.assertEqual(len(value), 5)
        self.assertTrue(all(isinstance(item, dict) for item in value))
        self.assertTrue(all(sorted(item) == ["a", "b"] for item in value))
        # 每条记录都不同（不是同一个对象重复引用）
        self.assertNotEqual(minify(value[0]), minify(value[1]))

    def test_object_root_with_count_returns_list(self):
        value = generate_from_fields(self.FIELDS, root="object", count=3,
                                     options=MockOptions(seed=1))
        self.assertIsInstance(value, list)
        self.assertEqual(len(value), 3)

    def test_count_falls_back_to_options(self):
        value = generate_from_fields(self.FIELDS, root="array",
                                     options=MockOptions(seed=1, count=4))
        self.assertEqual(len(value), 4)

    def test_count_is_clamped(self):
        value = generate_from_fields(self.FIELDS, root="array", count=0,
                                     options=MockOptions(seed=1))
        self.assertEqual(len(value), 1)
        self.assertLessEqual(MAX_GENERATE_COUNT, 100000)

    def test_unknown_root_behaves_like_object(self):
        value = generate_from_fields(self.FIELDS, root="whatever",
                                     options=MockOptions(seed=1))
        self.assertIsInstance(value, dict)

    def test_dict_rows_are_accepted(self):
        value = generate_from_fields(
            [{"name": "age", "type": "integer", "minimum": 20, "maximum": 30}],
            options=MockOptions(seed=1))
        self.assertTrue(20 <= value["age"] <= 30)

    def test_mixed_spec_and_row_input(self):
        value = generate_from_fields(
            [FieldSpec(name="x", type="integer"), {"name": "y", "type": "boolean"}],
            options=MockOptions(seed=1))
        self.assertIsInstance(value["x"], int)
        self.assertIsInstance(value["y"], bool)

    def test_empty_fields(self):
        self.assertEqual(generate_from_fields([], options=MockOptions(seed=1)), {})
        self.assertEqual(generate_from_fields(None, root="array", count=2,
                                              options=MockOptions(seed=1)), [{}, {}])

    def test_duplicate_names_last_wins(self):
        value = generate_from_fields(
            [FieldSpec(name="k", type="integer", minimum=1, maximum=1),
             FieldSpec(name="k", type="string", length=2)],
            options=MockOptions(locale="en", seed=1))
        self.assertEqual(len(value), 1)
        self.assertIsInstance(value["k"], str)

    def test_blank_name_gets_a_placeholder(self):
        value = generate_from_fields([FieldSpec(name="", type="integer")],
                                     options=MockOptions(seed=1))
        self.assertEqual(list(value), ["field"])


class TestFieldSpecFromRow(unittest.TestCase):
    def test_defaults_for_empty_row(self):
        spec = field_spec_from_row({})
        self.assertEqual(spec.name, "field")
        self.assertEqual(spec.type, "string")
        self.assertTrue(spec.required)
        self.assertIsNone(spec.minimum)
        self.assertIsNone(spec.maximum)
        self.assertIsNone(spec.length)
        self.assertEqual(spec.enum, ())
        self.assertEqual(spec.pattern, "")
        self.assertEqual(spec.children, ())
        self.assertIsNone(spec.items)
        self.assertEqual(spec.array_length, 3)

    def test_none_and_scalars(self):
        self.assertEqual(field_spec_from_row(None).name, "field")
        self.assertEqual(field_spec_from_row("weird").name, "weird")
        self.assertEqual(field_spec_from_row(42).name, "42")

    def test_field_spec_passes_through_normalized(self):
        spec = FieldSpec(name="x", type="INTEGER")
        self.assertEqual(field_spec_from_row(spec).type, "integer")

    def test_blank_cells_are_ignored(self):
        spec = field_spec_from_row({"name": "  ", "type": "", "minimum": "",
                                    "maximum": None, "pattern": "   "})
        self.assertEqual(spec.name, "field")
        self.assertEqual(spec.type, "string")
        self.assertIsNone(spec.minimum)
        self.assertIsNone(spec.maximum)
        self.assertEqual(spec.pattern, "")

    def test_aliases_and_case_insensitivity(self):
        spec = field_spec_from_row({"Field": "age", "TYPE": "Integer",
                                    "Min": "18", "Max Value": "65",
                                    "min length": "2", "MaxLength": "9"})
        self.assertEqual(spec.name, "age")
        self.assertEqual(spec.type, "integer")
        self.assertEqual(spec.minimum, 18.0)
        self.assertEqual(spec.maximum, 65.0)
        self.assertEqual(spec.min_length, 2)
        self.assertEqual(spec.max_length, 9)

    def test_unknown_type_becomes_string(self):
        self.assertEqual(field_spec_from_row({"type": "bogus"}).type, "string")
        self.assertEqual(field_spec_from_row({"type": "  WORD  "}).type, "word")

    def test_required_parsing(self):
        self.assertTrue(field_spec_from_row({"required": "yes"}).required)
        self.assertTrue(field_spec_from_row({"required": "true"}).required)
        self.assertTrue(field_spec_from_row({"required": 1}).required)
        self.assertFalse(field_spec_from_row({"required": "no"}).required)
        self.assertFalse(field_spec_from_row({"required": False}).required)
        self.assertFalse(field_spec_from_row({"required": "optional"}).required)
        self.assertFalse(field_spec_from_row({"required": 0}).required)
        self.assertTrue(field_spec_from_row({}).required)

    def test_enum_from_string(self):
        self.assertEqual(field_spec_from_row({"enum": "a, b|c;d"}).enum,
                         ("a", "b", "c", "d"))
        self.assertEqual(field_spec_from_row({"values": ["x", "y"]}).enum, ("x", "y"))
        self.assertEqual(field_spec_from_row({"options": "[p, q]"}).enum, ("p", "q"))
        self.assertEqual(field_spec_from_row({"enum": ""}).enum, ())

    def test_children_and_items(self):
        spec = field_spec_from_row({
            "name": "meta", "type": "object",
            "children": [{"name": "a", "type": "integer"},
                         FieldSpec(name="b", type="boolean")],
        })
        self.assertEqual([child.name for child in spec.children], ["a", "b"])
        self.assertEqual(spec.children[0].type, "integer")
        array = field_spec_from_row({"name": "list", "type": "array",
                                     "items": {"name": "el", "type": "word"},
                                     "arrayLength": "5"})
        self.assertEqual(array.items.name, "el")
        self.assertEqual(array.items.type, "word")
        self.assertEqual(array.array_length, 5)

    def test_garbage_numbers_do_not_raise(self):
        spec = field_spec_from_row({"minimum": "abc", "maximum": "nan", "length": "-4",
                                    "array_length": "x"})
        self.assertIsNone(spec.minimum)
        self.assertIsNone(spec.maximum)
        self.assertEqual(spec.array_length, 3)
        self.assertEqual(spec.length, 0)   # 负数长度被夹到 0，而不是当成未填写

    def test_length_normalization(self):
        spec = field_spec_from_row({"type": "string", "length": "6"})
        self.assertEqual(spec.length, 6)
        self.assertEqual(spec.min_length, 6)
        self.assertEqual(spec.max_length, 6)
        only_min = field_spec_from_row({"type": "string", "min_length": "4"})
        self.assertEqual(only_min.max_length, 20)
        swapped = field_spec_from_row({"type": "string", "min_length": "9",
                                       "max_length": "2"})
        self.assertEqual((swapped.min_length, swapped.max_length), (2, 9))

    def test_non_mapping_input_does_not_raise(self):
        for row in ([1, 2], (1,), {1, 2}, object(), True):
            with self.subTest(row=type(row).__name__):
                self.assertIsInstance(field_spec_from_row(row), FieldSpec)

    def test_row_output_is_usable(self):
        value = generate_from_fields([field_spec_from_row(
            {"name": "code", "type": "string", "pattern": "AA-###"})],
            options=MockOptions(seed=3))
        self.assertRegex(value["code"], r"^[A-Z]{2}-\d{3}$")


class TestGenerateFromSchema(unittest.TestCase):
    def test_representative_schema_is_satisfied(self):
        for seed in (1, 2, 3, 4, 5):
            with self.subTest(seed=seed):
                value, warnings = generate_from_schema(
                    REPRESENTATIVE_SCHEMA, options=MockOptions(locale="en", seed=seed),
                    max_depth=8, array_items=3)
                self.assertEqual(check(value, REPRESENTATIVE_SCHEMA), [])
                for text in warnings:
                    self.assertIsInstance(text, str)
                    self.assertTrue(text)

    def test_required_properties_are_present(self):
        value, _ = generate_from_schema(REPRESENTATIVE_SCHEMA,
                                        options=MockOptions(seed=1), max_depth=8)
        for key in REPRESENTATIVE_SCHEMA["required"]:
            self.assertIn(key, value)

    def test_bounds_and_enums(self):
        for seed in range(12):
            value, _ = generate_from_schema(REPRESENTATIVE_SCHEMA,
                                            options=MockOptions(locale="en", seed=seed),
                                            max_depth=8, array_items=4)
            self.assertTrue(1 <= value["id"] <= 100000)
            self.assertRegex(value["code"], r"^[A-Z]{3}-\d{4}$")
            self.assertTrue(1 <= len(value["name"]) <= 20)
            self.assertTrue(17 < value["age"] < 66)
            self.assertIsInstance(value["age"], int)
            self.assertTrue(-10 <= value["score"] <= 10)
            self.assertEqual(Decimal(str(value["score"])) % Decimal("0.5"), 0)
            self.assertTrue(0 <= value["step"] <= 100)
            self.assertEqual(value["step"] % 5, 0)
            self.assertIn(value["status"], ("new", "done", "blocked"))
            self.assertTrue(2 <= len(value["tags"]) <= 4)
            self.assertEqual(len({canon(tag) for tag in value["tags"]}), len(value["tags"]))
            self.assertIsInstance(value["flag"], bool)
            self.assertIsNone(value["nothing"])
            self.assertEqual(value["either"], "fixed")

    def test_ref_to_defs_is_resolved(self):
        value, warnings = generate_from_schema(REPRESENTATIVE_SCHEMA,
                                               options=MockOptions(seed=4), max_depth=8)
        for tag in value["tags"]:
            self.assertIn("name", tag)
            self.assertEqual(sorted(tag), ["name", "weight"])
            self.assertTrue(2 <= len(tag["name"]) <= 12)
            self.assertTrue(0 <= tag["weight"] <= 1)
        self.assertFalse(any("$ref" in text for text in warnings), warnings)

    def test_prefix_items_and_formats(self):
        value, _ = generate_from_schema(REPRESENTATIVE_SCHEMA,
                                        options=MockOptions(seed=5), max_depth=8)
        nested = value["nested"]
        self.assertRegex(nested["when"], DATETIME_RE)
        self.assertRegex(nested["who"], EMAIL_RE)
        self.assertEqual(len(nested["ip"].split(".")), 4)
        self.assertIsInstance(nested["tuple"][0], int)
        self.assertIsInstance(nested["tuple"][1], str)

    def test_all_of_is_merged(self):
        value, _ = generate_from_schema(REPRESENTATIVE_SCHEMA,
                                        options=MockOptions(seed=6), max_depth=8)
        combined = value["combined"]
        self.assertIn("a", combined)
        self.assertIsInstance(combined["a"], int)
        self.assertGreaterEqual(combined["a"], 0)
        if "b" in combined:
            self.assertLessEqual(len(combined["b"]), 6)

    def test_recursive_schema_terminates_and_stays_valid(self):
        for depth in (2, 3, 4, 6, 8):
            with self.subTest(depth=depth):
                value, warnings = generate_from_schema(
                    RECURSIVE_SCHEMA, options=MockOptions(seed=11), max_depth=depth,
                    array_items=2)
                self.assertEqual(check(value, RECURSIVE_SCHEMA), [])
                self.assertTrue(warnings)
                self.assertTrue(any("depth limit" in text for text in warnings), warnings)

    def test_recursive_schema_expands_several_levels(self):
        value, _ = generate_from_schema(RECURSIVE_SCHEMA, options=MockOptions(seed=12),
                                        max_depth=8, array_items=2)
        levels = 0
        node = value
        while isinstance(node, dict) and node.get("children"):
            levels += 1
            node = node["children"][0]
            if isinstance(node, list):
                node = node[0] if node else None
        self.assertGreaterEqual(levels, 2)

    def test_remote_ref_warns(self):
        schema = {"type": "object",
                  "properties": {"a": {"$ref": "https://example.com/schema.json#/Foo"}}}
        value, warnings = generate_from_schema(schema, options=MockOptions(seed=1))
        self.assertIsInstance(value, dict)
        self.assertIn("a", value)
        self.assertTrue(any("remote $ref" in text and "example.com" in text
                            for text in warnings), warnings)

    def test_unresolved_local_ref_warns(self):
        schema = {"type": "object", "properties": {"a": {"$ref": "#/$defs/Missing"}}}
        _value, warnings = generate_from_schema(schema, options=MockOptions(seed=1))
        self.assertTrue(any("unresolved local $ref" in text for text in warnings), warnings)

    def test_ref_cycle_terminates(self):
        schema = {"$defs": {"Loop": {"$ref": "#/$defs/Loop"}}, "$ref": "#/$defs/Loop"}
        value, warnings = generate_from_schema(schema, options=MockOptions(seed=1))
        self.assertTrue(any("cycle" in text for text in warnings), warnings)
        self.assertIsNotNone(value)

    def test_self_referencing_root_terminates(self):
        value, warnings = generate_from_schema({"$ref": "#"}, options=MockOptions(seed=1))
        self.assertTrue(any("cycle" in text for text in warnings), warnings)
        self.assertEqual(value, {})

    def test_ungeneratable_pattern_warns_and_names_it(self):
        pattern = "(?=.*abc)x+"
        value, warnings = generate_from_schema({"type": "string", "pattern": pattern},
                                               options=MockOptions(seed=1))
        self.assertIsInstance(value, str)
        self.assertTrue(any("not generatable" in text and pattern in text
                            for text in warnings), warnings)

    def test_backreference_pattern_warns(self):
        value, warnings = generate_from_schema({"type": "string", "pattern": "^(a)\\1$"},
                                               options=MockOptions(seed=1))
        self.assertIsInstance(value, str)
        self.assertTrue(any("backreference" in text for text in warnings), warnings)

    def test_generatable_patterns_match(self):
        patterns = [
            r"^[A-Z]{3}-\d{4}$", r"\d{2}/\d{2}/\d{4}", r"[a-f0-9]{6}",
            r"^(foo|bar|baz)$", r"[^0-9]{3}", r"a.c", r"\w+@\w+\.(com|org)",
            r"^\+86\d{10}$", r"[A-Za-z_][A-Za-z0-9_]*", r"(ab){2,3}", r"^$",
        ]
        for pattern in patterns:
            for seed in range(6):
                with self.subTest(pattern=pattern, seed=seed):
                    value, warnings = generate_from_schema(
                        {"type": "string", "pattern": pattern},
                        options=MockOptions(locale="en", seed=seed))
                    self.assertEqual(warnings, [])
                    self.assertIsNotNone(re.search(pattern, value),
                                         f"{value!r} does not match {pattern}")

    def test_pattern_with_length_bounds(self):
        schema = {"type": "string", "pattern": r"[a-z]+", "minLength": 5, "maxLength": 9}
        for seed in range(10):
            value, warnings = generate_from_schema(schema, options=MockOptions(seed=seed))
            self.assertEqual(warnings, [])
            self.assertTrue(5 <= len(value) <= 9, value)
            self.assertRegex(value, r"^[a-z]+$")

    def test_const_default_examples(self):
        value, warnings = generate_from_schema({"const": {"a": [1, 2]}},
                                               options=MockOptions(seed=1))
        self.assertEqual(value, {"a": [1, 2]})
        self.assertEqual(warnings, [])
        value, _ = generate_from_schema({"type": "string", "default": "hello"},
                                        options=MockOptions(seed=1))
        self.assertEqual(value, "hello")
        value, _ = generate_from_schema({"type": "string", "examples": ["ex1", "ex2"]},
                                        options=MockOptions(seed=1))
        self.assertEqual(value, "ex1")
        value, warnings = generate_from_schema({"type": "integer", "default": "oops"},
                                               options=MockOptions(seed=1))
        self.assertIsInstance(value, int)
        self.assertTrue(any("'default' does not satisfy" in text for text in warnings))
        value, warnings = generate_from_schema({"type": "integer", "examples": ["bad"],
                                                "minimum": 5}, options=MockOptions(seed=1))
        self.assertGreaterEqual(value, 5)
        self.assertTrue(any("examples[0]" in text for text in warnings))

    def test_default_is_cloned_not_shared(self):
        schema = {"const": {"list": [1, 2]}}
        first, _ = generate_from_schema(schema, options=MockOptions(seed=1))
        first["list"].append(3)
        second, _ = generate_from_schema(schema, options=MockOptions(seed=1))
        self.assertEqual(second, {"list": [1, 2]})
        self.assertEqual(schema["const"], {"list": [1, 2]})

    def test_null_default_is_silently_ignored(self):
        value, warnings = generate_from_schema({"type": "string", "default": None},
                                               options=MockOptions(locale="en", seed=1))
        self.assertIsInstance(value, str)
        self.assertEqual(warnings, [])

    def test_type_array_picks_first_supported(self):
        value, warnings = generate_from_schema({"type": ["banana", "string"]},
                                               options=MockOptions(locale="en", seed=1))
        self.assertIsInstance(value, str)
        self.assertTrue(any("unsupported type" in text for text in warnings))
        value, _ = generate_from_schema({"type": ["null", "string"]},
                                        options=MockOptions(seed=1))
        self.assertIsNone(value)

    def test_numeric_edge_cases(self):
        cases = [
            ({"type": "integer", "minimum": 5, "maximum": 5}, 5, 5),
            ({"type": "integer", "exclusiveMinimum": 5, "exclusiveMaximum": 8}, 6, 7),
            ({"type": "integer", "multipleOf": 7, "minimum": 1, "maximum": 100}, 7, 98),
            ({"type": "number", "minimum": -1.5, "maximum": -1.2}, -1.5, -1.2),
            ({"type": "number", "multipleOf": 0.5, "minimum": 0, "maximum": 2}, 0, 2),
        ]
        for schema, low, high in cases:
            for seed in range(10):
                with self.subTest(schema=schema, seed=seed):
                    value, warnings = generate_from_schema(schema,
                                                           options=MockOptions(seed=seed))
                    self.assertEqual(warnings, [])
                    self.assertTrue(low <= value <= high, value)
                    self.assertEqual(check(value, schema), [])

    def test_unsatisfiable_bounds_warn_but_do_not_raise(self):
        value, warnings = generate_from_schema({"type": "integer", "minimum": 10,
                                                "maximum": 1}, options=MockOptions(seed=1))
        self.assertIsInstance(value, int)
        self.assertTrue(any("cannot be satisfied" in text for text in warnings))
        value, warnings = generate_from_schema(
            {"type": "number", "exclusiveMinimum": 5, "exclusiveMaximum": 5},
            options=MockOptions(seed=1))
        self.assertIsInstance(value, float)
        self.assertTrue(warnings)

    def test_array_constraints(self):
        schema = {"type": "array", "minItems": 3, "maxItems": 5,
                  "items": {"type": "integer", "minimum": 0, "maximum": 9}}
        value, warnings = generate_from_schema(schema, options=MockOptions(seed=1),
                                               array_items=1)
        self.assertEqual(warnings, [])
        self.assertEqual(len(value), 3)
        value, _ = generate_from_schema(schema, options=MockOptions(seed=1),
                                        array_items=99)
        self.assertEqual(len(value), 5)
        self.assertEqual(check(value, schema), [])

    def test_unique_items_with_tight_enum_warns_or_succeeds(self):
        schema = {"type": "array", "uniqueItems": True, "minItems": 2,
                  "items": {"enum": ["a", "b"]}}
        value, _warnings = generate_from_schema(schema, options=MockOptions(seed=1),
                                                array_items=2)
        self.assertEqual(len({canon(item) for item in value}), len(value))
        self.assertEqual(check(value, schema), [])

    def test_unique_items_with_strings(self):
        schema = {"type": "array", "uniqueItems": True, "minItems": 4,
                  "items": {"type": "string", "maxLength": 40}}
        value, warnings = generate_from_schema(schema, options=MockOptions(locale="en",
                                                                          seed=1),
                                               array_items=4)
        self.assertEqual(warnings, [])
        self.assertEqual(len(set(value)), 4)

    def test_items_false_warns(self):
        schema = {"type": "array", "items": False, "minItems": 2}
        value, warnings = generate_from_schema(schema, options=MockOptions(seed=1))
        self.assertEqual(value, [])
        self.assertTrue(any("items is false" in text for text in warnings))

    def test_property_count_constraints(self):
        schema = {"type": "object", "minProperties": 3,
                  "properties": {"a": {"type": "integer"}}}
        value, warnings = generate_from_schema(schema, options=MockOptions(seed=1))
        self.assertEqual(warnings, [])
        self.assertGreaterEqual(len(value), 3)
        self.assertEqual(check(value, schema), [])
        schema = {"type": "object", "minProperties": 2, "additionalProperties": False,
                  "properties": {"a": {"type": "integer"}}}
        value, warnings = generate_from_schema(schema, options=MockOptions(seed=1))
        self.assertEqual(len(value), 1)
        self.assertTrue(any("additionalProperties is false" in text for text in warnings))

    def test_max_properties_conflict_warns(self):
        schema = {"type": "object", "maxProperties": 1, "required": ["a", "b"],
                  "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}}
        value, warnings = generate_from_schema(schema, options=MockOptions(seed=1))
        self.assertEqual(len(value), 1)
        self.assertTrue(any("maxProperties conflicts with 'required'" in text
                            for text in warnings))

    def test_unenforced_keywords_warn(self):
        for keyword in ("not", "if", "patternProperties", "propertyNames", "contains"):
            with self.subTest(keyword=keyword):
                _value, warnings = generate_from_schema({keyword: {}},
                                                        options=MockOptions(seed=1))
                self.assertTrue(any(keyword in text for text in warnings), warnings)

    def test_boolean_schemas(self):
        value, warnings = generate_from_schema(True, options=MockOptions(locale="en",
                                                                        seed=1))
        self.assertIsInstance(value, str)
        self.assertEqual(warnings, [])
        value, warnings = generate_from_schema(False, options=MockOptions(seed=1))
        self.assertIsNone(value)
        self.assertTrue(any("schema is `false`" in text for text in warnings))

    def test_empty_schema_gives_empty_object(self):
        value, warnings = generate_from_schema({}, options=MockOptions(seed=1))
        self.assertEqual(value, {})
        self.assertEqual(warnings, [])

    def test_one_of_picks_first_branch_and_warns(self):
        schema = {"oneOf": [{"type": "string"}, {"type": "integer"}]}
        value, warnings = generate_from_schema(schema, options=MockOptions(locale="en",
                                                                          seed=1))
        self.assertIsInstance(value, str)
        self.assertTrue(any("oneOf has 2 branches" in text for text in warnings))

    def test_any_of_picks_first_branch(self):
        schema = {"anyOf": [{"type": "integer", "const": 7}, {"type": "string"}]}
        value, warnings = generate_from_schema(schema, options=MockOptions(seed=1))
        self.assertEqual(value, 7)
        self.assertEqual(warnings, [])

    def test_schema_is_not_mutated(self):
        schema = copy.deepcopy(REPRESENTATIVE_SCHEMA)
        generate_from_schema(REPRESENTATIVE_SCHEMA, options=MockOptions(seed=1),
                             max_depth=8)
        self.assertEqual(REPRESENTATIVE_SCHEMA, schema)

    def test_depth_and_items_arguments_are_clamped(self):
        value, warnings = generate_from_schema(RECURSIVE_SCHEMA,
                                               options=MockOptions(seed=1),
                                               max_depth=0, array_items=-3)
        self.assertEqual(check(value, RECURSIVE_SCHEMA), [])
        self.assertTrue(warnings)
        value, warnings = generate_from_schema(RECURSIVE_SCHEMA,
                                               options=MockOptions(seed=1),
                                               max_depth=10 ** 6, array_items=10 ** 6)
        self.assertEqual(check(value, RECURSIVE_SCHEMA), [])

    def test_garbage_schema_never_raises(self):
        for schema in ("not a schema", 42, [1, 2], {"type": 17}, {"properties": 5},
                       {"items": "x"}, {"allOf": "nope"}, {"enum": []},
                       {"required": "abc"}, {"$ref": 12}):
            with self.subTest(schema=schema):
                value, warnings = generate_from_schema(schema, options=MockOptions(seed=1))
                self.assertIsInstance(warnings, list)
                minify(value)   # 结果必须可序列化

    def test_return_shape(self):
        result = generate_from_schema({"type": "integer"}, options=MockOptions(seed=1))
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], int)
        self.assertIsInstance(result[1], list)

    def test_locale_affects_schema_strings(self):
        schema = {"type": "object", "properties": {"bio": {"type": "string",
                                                           "maxLength": 12}}}
        zh, _ = generate_from_schema(schema, options=MockOptions(locale="zh", seed=1))
        en, _ = generate_from_schema(schema, options=MockOptions(locale="en", seed=1))
        self.assertTrue(CJK.search(zh["bio"]), zh)
        self.assertRegex(en["bio"], ASCII_ONLY)

    def test_deeply_nested_schema_does_not_recurse_forever(self):
        schema = {"type": "integer"}
        for _ in range(200):
            schema = {"type": "object", "required": ["n"], "properties": {"n": schema}}
        value, warnings = generate_from_schema(schema, options=MockOptions(seed=1),
                                               max_depth=64)
        self.assertIsInstance(value, dict)
        self.assertTrue(warnings)


class TestPathStringHelper(unittest.TestCase):
    def test_matches_the_shared_convention(self):
        from app.engines.scanners import path_string as scanners_path_string
        from app.engines.statistics import path_string as statistics_path_string

        samples = [(), ("a",), ("a", 0, "b"), ("weird key",), ("it's",),
                   ("back\\slash",), ("\u4e2d\u6587",), ("",), (7,)]
        for sample in samples:
            with self.subTest(path=sample):
                self.assertEqual(path_string(sample), statistics_path_string(sample))
                self.assertEqual(path_string(sample), scanners_path_string(sample))

    def test_warning_paths_are_readable(self):
        schema = {"type": "object",
                  "properties": {"a": {"type": "string", "pattern": "(?=x)y"}}}
        _value, warnings = generate_from_schema(schema, options=MockOptions(seed=1))
        self.assertTrue(any(text.startswith("$.properties.a:") for text in warnings),
                        warnings)


if __name__ == "__main__":
    unittest.main(verbosity=2)

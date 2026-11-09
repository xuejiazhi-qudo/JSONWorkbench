"""解包"字符串包裹 JSON"的测试。"""

from __future__ import annotations

import json
import unittest

from app.core.jsonparser import parse_json
from app.core.unwrap import detect_wrapped, unwrap

DOUBLE = json.dumps('{"data":{"id":1},"code":0}', ensure_ascii=False)
TRIPLE = json.dumps(json.dumps('{"a":[1,2]}'))


class TestUnwrap(unittest.TestCase):
    def test_plain_json_is_not_wrapped(self):
        result = unwrap('{"a":1}')
        self.assertTrue(result.ok)
        self.assertFalse(result.wrapped)
        self.assertEqual(result.layers, 0)
        self.assertEqual(result.text, '{"a":1}')
        self.assertEqual(result.root_type, "object")

    def test_plain_string_is_not_wrapped(self):
        result = unwrap('"hello world"')
        self.assertTrue(result.ok)
        self.assertFalse(result.wrapped)
        self.assertEqual(result.root_type, "string")

    def test_double_encoded_object(self):
        result = unwrap(DOUBLE)
        self.assertTrue(result.ok)
        self.assertTrue(result.wrapped)
        self.assertEqual(result.layers, 1)
        self.assertEqual(result.root_type, "object")
        self.assertEqual(parse_json(result.text).value, {"data": {"id": 1}, "code": 0})

    def test_multi_layer(self):
        result = unwrap(TRIPLE)
        self.assertTrue(result.wrapped)
        self.assertEqual(result.layers, 2)
        self.assertEqual(parse_json(result.text).value, {"a": [1, 2]})

    def test_layer_limit_is_respected(self):
        result = unwrap(TRIPLE, max_layers=1)
        self.assertEqual(result.layers, 1)
        # 还剩一层字符串包裹，根类型是 string
        self.assertEqual(result.root_type, "string")

    def test_number_literals_survive_unwrap(self):
        inner = '{"n":1.230,"big":12345678901234567890123456789}'
        result = unwrap(json.dumps(inner))
        self.assertTrue(result.wrapped)
        self.assertIn("1.230", result.text)
        self.assertIn("12345678901234567890123456789", result.text)

    def test_string_containing_invalid_json_is_not_wrapped(self):
        result = unwrap('"not json at all {"')
        self.assertTrue(result.ok)
        self.assertFalse(result.wrapped)

    def test_invalid_outer_reports_error(self):
        result = unwrap('{"a":1,}')
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "trailing_comma")

    def test_detect_helper(self):
        self.assertTrue(detect_wrapped(DOUBLE))
        self.assertFalse(detect_wrapped('{"a":1}'))
        self.assertFalse(detect_wrapped('"just text"'))

    def test_string_wrapping_scalar(self):
        result = unwrap('"42"')
        self.assertTrue(result.wrapped)
        self.assertEqual(result.root_type, "number")
        self.assertEqual(result.text, "42")

    def test_escaped_unicode_inner(self):
        inner = '{"名称":"值"}'
        wrapped = json.dumps(inner, ensure_ascii=True)   # 内层中文被转义
        result = unwrap(wrapped)
        self.assertTrue(result.wrapped)
        self.assertEqual(parse_json(result.text).value, {"名称": "值"})


if __name__ == "__main__":
    unittest.main(verbosity=2)

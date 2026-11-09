"""性能回归测试：大文件与深嵌套必须在可接受时间内完成，且不崩溃。

阈值刻意放宽（离线 CI / 虚拟机都跑得动），目的是抓住"算法退化"而不是卡发布。
"""

from __future__ import annotations

import json
import time
import unittest

from app.core.jsonformatter import FormatOptions, format_text, minify_text
from app.core.jsonparser import parse_json
from app.engines.statistics import compute_statistics

BIG_RECORDS = 40_000


def build_big_document() -> str:
    records = [
        {
            "id": i,
            "name": f"user-{i}",
            "email": f"user{i}@example.com",
            "score": round(i * 1.5, 2),
            "active": i % 2 == 0,
            "tags": ["a", "b"] if i % 3 else [],
            "meta": {"city": "Hangzhou", "zip": f"{310000 + i}"},
        }
        for i in range(BIG_RECORDS)
    ]
    return json.dumps({"records": records, "count": len(records)}, ensure_ascii=False)


_BIG_TEXT = None


def big_text() -> str:
    global _BIG_TEXT
    if _BIG_TEXT is None:
        _BIG_TEXT = build_big_document()
    return _BIG_TEXT


def timed(fn, *args, **kwargs):
    started = time.perf_counter()
    result = fn(*args, **kwargs)
    return result, (time.perf_counter() - started)


class TestLargeDocument(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = big_text()
        cls.size_mb = len(cls.text.encode("utf-8")) / 1024 / 1024

    def test_document_is_actually_large(self):
        self.assertGreater(self.size_mb, 4.0)

    def test_parse_speed(self):
        result, elapsed = timed(parse_json, self.text)
        self.assertTrue(result.ok, result.error)
        tag_items = sum(2 for i in range(BIG_RECORDS) if i % 3)
        expected = BIG_RECORDS * 10 + tag_items + 3   # 根对象 + records 数组 + count
        self.assertEqual(result.stats.node_count, expected)
        self.assertLess(elapsed, 8.0, f"parse took {elapsed:.2f}s")

    def test_format_speed_and_fidelity(self):
        parsed = parse_json(self.text)
        result, elapsed = timed(format_text, self.text, FormatOptions(indent="  "))
        self.assertTrue(result.ok)
        self.assertTrue(result.semantics_preserved)
        self.assertLess(elapsed, 12.0, f"format took {elapsed:.2f}s")
        del parsed, result

    def test_minify_speed(self):
        _result, elapsed = timed(minify_text, self.text)
        self.assertLess(elapsed, 10.0, f"minify took {elapsed:.2f}s")

    def test_statistics_speed(self):
        parsed = parse_json(self.text)
        _stats, elapsed = timed(compute_statistics, parsed.value)
        self.assertLess(elapsed, 6.0, f"statistics took {elapsed:.2f}s")


class TestDeepNesting(unittest.TestCase):
    def test_deep_parse_and_format(self):
        depth = 5000
        text = "[" * depth + "]" * depth
        result, elapsed = timed(parse_json, text, max_depth=10000)
        self.assertTrue(result.ok, result.error)
        self.assertLess(elapsed, 3.0)
        formatted, elapsed2 = timed(format_text, text,
                                    FormatOptions(indent="", max_depth=10000))
        self.assertTrue(formatted.ok)
        self.assertLess(elapsed2, 3.0)

    def test_wide_document(self):
        text = "{" + ",".join(f'"k{i}":{i}' for i in range(200_000)) + "}"
        result, elapsed = timed(parse_json, text)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(result.value), 200_000)
        self.assertLess(elapsed, 6.0, f"wide parse took {elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main(verbosity=2)

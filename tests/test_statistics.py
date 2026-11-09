"""统计引擎测试：手算计数、体积一致性、精度、深度、性能与确定性排序。"""

from __future__ import annotations

import copy
import time
import unittest
from decimal import Decimal

from app.core.jsonformatter import FormatOptions, dumps, json_equal, minify
from app.core.jsonparser import parse_json
from app.core.jsonvalue import JsonNumber
from app.engines.statistics import (
    ROW_LABELS,
    Statistics,
    compact_size,
    compute_statistics,
    format_bytes,
    iter_container_sizes,
    merge_parser_stats,
    path_string,
    push_top_n,
    statistics_as_rows,
)

# 手算样本：
#   节点 = 根对象 + 数组 + 3 个数字 + 内层对象 + null + true + "hello" + 1.5 = 10
#   层级 = 第 1 层 1 个（根对象），第 2 层 4 个（数组/内层对象/"hello"/1.5），
#          第 3 层 5 个（1,2,3,null,true）
SAMPLE_TEXT = '{"a":[1,2,3],"b":{"c":null,"d":true},"e":"hello","f":1.5}'
SAMPLE = parse_json(SAMPLE_TEXT).value


def stats_of(text):
    result = parse_json(text)
    assert result.ok, result.error
    return compute_statistics(result.value)


class TestKnownDocument(unittest.TestCase):
    def setUp(self):
        self.stats = compute_statistics(SAMPLE)

    def test_node_counters(self):
        s = self.stats
        self.assertEqual(s.node_count, 10)
        self.assertEqual(s.object_count, 2)
        self.assertEqual(s.array_count, 1)
        self.assertEqual(s.string_count, 1)
        self.assertEqual(s.number_count, 4)
        self.assertEqual(s.boolean_count, 1)
        self.assertEqual(s.null_count, 1)

    def test_counters_match_parser_stats(self):
        parsed = parse_json(SAMPLE_TEXT)
        s = self.stats
        self.assertEqual(s.node_count, parsed.stats.node_count)
        self.assertEqual(s.object_count, parsed.stats.object_count)
        self.assertEqual(s.array_count, parsed.stats.array_count)
        self.assertEqual(s.string_count, parsed.stats.string_count)
        self.assertEqual(s.number_count, parsed.stats.number_count)
        self.assertEqual(s.boolean_count, parsed.stats.boolean_count)
        self.assertEqual(s.null_count, parsed.stats.null_count)
        self.assertEqual(s.key_count, parsed.stats.key_count)
        self.assertEqual(s.max_depth, parsed.stats.max_depth)
        self.assertEqual(s.max_array_length, parsed.stats.max_array_length)
        self.assertEqual(s.total_string_chars, parsed.stats.string_chars)

    def test_key_counters(self):
        s = self.stats
        self.assertEqual(s.key_count, 6)
        self.assertEqual(s.unique_key_count, 6)
        self.assertEqual(s.duplicate_key_count, 0)

    def test_depth_histogram(self):
        self.assertEqual(self.stats.depth_histogram, {1: 1, 2: 4, 3: 5})
        self.assertEqual(self.stats.max_depth, 2)
        self.assertEqual(sum(self.stats.depth_histogram.values()), self.stats.node_count)

    def test_string_metrics(self):
        s = self.stats
        self.assertEqual(s.total_string_chars, 5)
        self.assertEqual(s.longest_string_chars, 5)
        self.assertEqual(s.longest_string_path, ("e",))

    def test_array_metrics(self):
        s = self.stats
        self.assertEqual(s.max_array_length, 3)
        self.assertEqual(s.max_array_path, ("a",))
        self.assertEqual(s.total_array_items, 3)

    def test_byte_sizes_match_serializer(self):
        s = self.stats
        self.assertEqual(s.byte_size, len(minify(SAMPLE).encode("utf-8")))
        self.assertEqual(s.byte_size, 57)
        expected_pretty = dumps(SAMPLE, FormatOptions(indent="  "))
        self.assertEqual(s.pretty_size, len(expected_pretty.encode("utf-8")))
        self.assertEqual(s.pretty_size, 107)
        self.assertGreater(s.pretty_size, s.byte_size)

    def test_number_stats(self):
        s = self.stats
        self.assertEqual(s.number_min, 1)
        self.assertEqual(s.number_max, 3)
        self.assertEqual(s.number_sum, Decimal("7.5"))
        self.assertIsInstance(s.number_min, int)
        self.assertIsInstance(s.number_max, int)
        self.assertIsInstance(s.number_sum, Decimal)
        self.assertEqual(s.integer_count, 3)
        self.assertEqual(s.float_count, 1)
        self.assertEqual(s.integer_count + s.float_count, s.number_count)

    def test_top_keys_and_largest(self):
        s = self.stats
        self.assertEqual(s.top_keys, [("a", 1), ("b", 1), ("c", 1), ("d", 1), ("e", 1), ("f", 1)])
        self.assertEqual(s.largest_nodes[0], ("$", 57, "object"))
        self.assertEqual(s.largest_nodes[1], ("$.b", 19, "object"))
        self.assertEqual(s.largest_nodes[2], ("$.a", 7, "array"))
        self.assertEqual(len(s.largest_nodes), 3)

    def test_leaf_ratio(self):
        # 标量 = 1 字符串 + 4 数字 + 1 布尔 + 1 null = 7，总节点 10
        self.assertAlmostEqual(self.stats.leaf_ratio, 0.7)

    def test_elapsed_is_measured(self):
        self.assertGreaterEqual(self.stats.elapsed_ms, 0.0)
        self.assertIsInstance(self.stats.elapsed_ms, float)


class TestRootShapes(unittest.TestCase):
    def test_empty_object(self):
        s = compute_statistics({})
        self.assertEqual(s.node_count, 1)
        self.assertEqual(s.object_count, 1)
        self.assertEqual(s.array_count, 0)
        self.assertEqual(s.key_count, 0)
        self.assertEqual(s.unique_key_count, 0)
        self.assertEqual(s.max_depth, 1)
        self.assertEqual(s.depth_histogram, {1: 1})
        self.assertEqual(s.byte_size, 2)
        self.assertEqual(s.pretty_size, 2)
        self.assertEqual(s.leaf_ratio, 0.0)
        self.assertEqual(s.largest_nodes, [("$", 2, "object")])
        self.assertEqual(s.top_keys, [])
        self.assertIsNone(s.number_sum)

    def test_empty_array(self):
        s = compute_statistics([])
        self.assertEqual(s.node_count, 1)
        self.assertEqual(s.array_count, 1)
        self.assertEqual(s.max_depth, 1)
        self.assertEqual(s.max_array_length, 0)
        self.assertEqual(s.max_array_path, ())
        self.assertEqual(s.total_array_items, 0)
        self.assertEqual(s.byte_size, 2)
        self.assertEqual(s.leaf_ratio, 0.0)

    def test_null_root(self):
        s = compute_statistics(None)
        self.assertEqual(s.node_count, 1)
        self.assertEqual(s.null_count, 1)
        self.assertEqual(s.max_depth, 0)
        self.assertEqual(s.depth_histogram, {1: 1})
        self.assertEqual(s.byte_size, 4)
        self.assertEqual(s.pretty_size, 4)
        self.assertEqual(s.leaf_ratio, 1.0)
        self.assertEqual(s.largest_nodes, [])

    def test_scalar_roots(self):
        cases = [
            ("x", {"string_count": 1, "byte_size": 3, "total_string_chars": 1}),
            (123, {"number_count": 1, "byte_size": 3, "integer_count": 1}),
            (True, {"boolean_count": 1, "byte_size": 4}),
            (False, {"boolean_count": 1, "byte_size": 5}),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                s = compute_statistics(value)
                self.assertEqual(s.node_count, 1)
                self.assertEqual(s.max_depth, 0)
                self.assertEqual(s.leaf_ratio, 1.0)
                for key, want in expected.items():
                    self.assertEqual(getattr(s, key), want)

    def test_longest_string_path_is_root_for_scalar(self):
        s = compute_statistics("abcdef")
        self.assertEqual(s.longest_string_path, ())
        self.assertEqual(s.longest_string_chars, 6)


class TestDepthHistogram(unittest.TestCase):
    def test_nested_depths(self):
        s = stats_of('{"a":{"b":{"c":{"d":[1]}}}}')
        self.assertEqual(s.depth_histogram, {1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1})
        self.assertEqual(s.node_count, 6)
        self.assertEqual(s.max_depth, 5)
        self.assertEqual(sum(s.depth_histogram.values()), s.node_count)

    def test_wide_document(self):
        s = compute_statistics([1, 2, 3, 4, 5])
        self.assertEqual(s.depth_histogram, {1: 1, 2: 5})
        self.assertEqual(s.max_depth, 1)

    def test_histogram_keys_are_sorted(self):
        s = stats_of('{"a":[[1]],"b":{"c":{"d":2}}}')
        keys = list(s.depth_histogram)
        self.assertEqual(keys, sorted(keys))

    def test_max_depth_matches_parser_on_random_shapes(self):
        texts = [
            "{}", "[]", "[[]]", '{"a":{}}', '{"a":[{"b":[1,2]}]}',
            '[[[[[[[[[[1]]]]]]]]]]', '{"x":{"y":{"z":{"w":[]}}}}',
        ]
        for text in texts:
            with self.subTest(text=text):
                parsed = parse_json(text)
                s = compute_statistics(parsed.value)
                self.assertEqual(s.max_depth, parsed.stats.max_depth)
                self.assertEqual(s.node_count, parsed.stats.node_count)


class TestKeyCounting(unittest.TestCase):
    def test_unique_key_count_exact(self):
        s = stats_of('{"id":1,"nested":{"id":2,"deep":{"id":3,"other":4}}}')
        # 出现过的键名：id, nested, id, deep, id, other -> 6 次出现，4 个唯一键名
        self.assertEqual(s.key_count, 6)
        self.assertEqual(s.unique_key_count, 4)
        self.assertEqual(s.duplicate_key_count, 2)
        self.assertEqual(s.duplicate_key_count, s.key_count - s.unique_key_count)

    def test_top_keys_ordering_and_tie_break(self):
        s = stats_of('{"b":{"x":1},"a":{"x":2,"y":3},"z":{"y":4}}')
        # x:2, y:2, a:1, b:1, z:1 -> 计数降序，同计数按键名升序（Unicode 码点）
        self.assertEqual(s.top_keys, [("x", 2), ("y", 2), ("a", 1), ("b", 1), ("z", 1)])

    def test_top_keys_is_deterministic(self):
        first = stats_of('{"m":1,"a":{"m":2},"z":{"m":3,"a":4}}').top_keys
        second = stats_of('{"z":{"m":3,"a":4},"a":{"m":2},"m":1}').top_keys
        # 键顺序不同的等价文档必须给出同样的 top_keys（因为二次判据是键名）
        self.assertEqual(first, second)
        self.assertEqual(first, [("m", 3), ("a", 2), ("z", 1)])

    def test_top_n_limits_result(self):
        s = compute_statistics(SAMPLE, top_n=2)
        self.assertEqual(len(s.top_keys), 2)
        self.assertEqual(s.top_keys, [("a", 1), ("b", 1)])

    def test_top_n_zero_disables(self):
        s = compute_statistics(SAMPLE, top_n=0)
        self.assertEqual(s.top_keys, [])
        self.assertEqual(s.unique_key_count, 0)
        self.assertEqual(s.duplicate_key_count, 6)

    def test_merge_parser_stats_uses_authoritative_duplicates(self):
        text = '{"a":1,"b":{"a":2},"a":3}'
        parsed = parse_json(text)
        self.assertEqual(parsed.stats.duplicate_key_count, 1)
        s = compute_statistics(parsed.value)
        self.assertEqual(s.duplicate_key_count, 1)  # 键名复用：a 出现 2 次
        merged = merge_parser_stats(s, parsed)
        self.assertEqual(merged.duplicate_key_count, 1)
        self.assertIsNot(merged, s)
        self.assertEqual(merged.node_count, s.node_count)
        # 传入 .stats 也可以
        self.assertEqual(merge_parser_stats(s, parsed.stats).duplicate_key_count, 1)
        self.assertIs(merge_parser_stats(s, object()), s)


class TestLargestNodes(unittest.TestCase):
    def test_root_is_largest_and_sorted_desc(self):
        s = compute_statistics(SAMPLE)
        sizes = [size for _path, size, _kind in s.largest_nodes]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertEqual(s.largest_nodes[0][0], "$")
        self.assertEqual(s.largest_nodes[0][1], s.byte_size)

    def test_largest_n_limits_result(self):
        value = {"a": [1, 2, 3], "b": {"c": "xxxx"}, "d": {"e": {"f": [1]}}}
        s = compute_statistics(value, largest_n=2)
        self.assertEqual(len(s.largest_nodes), 2)
        self.assertEqual(s.largest_nodes[0][0], "$")

    def test_collect_largest_false(self):
        s = compute_statistics(SAMPLE, collect_largest=False)
        self.assertEqual(s.largest_nodes, [])
        self.assertEqual(s.byte_size, 57)

    def test_largest_n_zero(self):
        self.assertEqual(compute_statistics(SAMPLE, largest_n=0).largest_nodes, [])

    def test_tie_break_is_document_order(self):
        # 两个同尺寸子树，文档中靠前的排前面
        value = {"first": [1, 2], "second": [3, 4]}
        s = compute_statistics(value)
        paths = [path for path, size, _kind in s.largest_nodes if size == 5]
        self.assertEqual(paths, ["$.first", "$.second"])

    def test_tie_break_survives_eviction(self):
        # 4 个同尺寸数组但只保留 2 个：必须留下文档中最早的两个
        value = {"a": [1, 2], "b": [3, 4], "c": [5, 6], "d": [7, 8]}
        stats = compute_statistics(value, largest_n=2)
        self.assertEqual(len(stats.largest_nodes), 2)
        self.assertEqual(stats.largest_nodes[0][0], "$")
        self.assertEqual(stats.largest_nodes[1][0], "$.a")
        sizes = [size for _p, size, _k in stats.largest_nodes]
        self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_type_names_are_machine_readable(self):
        s = compute_statistics(SAMPLE)
        self.assertEqual({kind for _p, _b, kind in s.largest_nodes}, {"object", "array"})

    def test_iter_container_sizes_is_post_order_with_pre_order_seq(self):
        entries = list(iter_container_sizes(SAMPLE))
        self.assertEqual(entries, [
            (1, ("a",), 7, "array"),
            (5, ("b",), 19, "object"),
            (0, (), 57, "object"),
        ])

    def test_push_top_n_keeps_largest(self):
        top = []
        for seq, size in enumerate([3, 9, 1, 7, 5]):
            push_top_n(top, (size, seq, (str(seq),), "array"), 2)
        top.sort(key=lambda item: (-item[0], item[1]))
        self.assertEqual([size for size, _s, _p, _k in top], [9, 7])
        self.assertEqual(push_top_n([], (1, 0, (), "array"), 0), None)


class TestByteSizes(unittest.TestCase):
    SAMPLES = [
        "{}", "[]", "null", "true", "false", "0", "-0", "1e400", '"x"',
        '{"a":[1,2,3],"b":{"c":null,"d":true},"e":"hello","f":1.5}',
        '{"a":{},"b":[],"c":[[]],"d":{"e":[1,{"f":"g"}]}}',
        '[1,2,3,4,5]',
        '{"n":123456789012345678901234567890,"m":1.230,"e":1e5}',
        '[[[[[[1]]]]]]',
        '{"weird key":[1,{"x":"y"}]}',
        '{"esc":"quote \\" back \\\\ tab \\t newline \\n \\u0001"}',
        '{"unicode":"\u4e2d\u6587\u503c","emoji":"\U0001f600"}',
        '{"deep":{"a":[{"b":[{"c":[1,2,3]}]}]}}',
    ]

    def test_compact_size_matches_minify(self):
        for text in self.SAMPLES:
            with self.subTest(text=text):
                value = parse_json(text).value
                self.assertEqual(compact_size(value), len(minify(value).encode("utf-8")))

    def test_byte_size_matches_minify(self):
        for text in self.SAMPLES:
            with self.subTest(text=text):
                value = parse_json(text).value
                stats = compute_statistics(value)
                self.assertEqual(stats.byte_size, len(minify(value).encode("utf-8")))

    def test_pretty_size_matches_two_space_dumps(self):
        for text in self.SAMPLES:
            with self.subTest(text=text):
                value = parse_json(text).value
                stats = compute_statistics(value)
                expected = dumps(value, FormatOptions(indent="  "))
                self.assertEqual(stats.pretty_size, len(expected.encode("utf-8")))

    def test_unicode_counts_utf8_bytes_not_chars(self):
        value = parse_json('{"k":"\u4e2d\u6587"}').value
        stats = compute_statistics(value)
        self.assertEqual(stats.byte_size, len('{"k":"中文"}'.encode("utf-8")))
        self.assertEqual(stats.byte_size, 14)
        self.assertEqual(stats.total_string_chars, 2)

    def test_escaped_characters_count_serialized_bytes(self):
        value = parse_json('{"k":"a\\nb"}').value
        stats = compute_statistics(value)
        self.assertEqual(stats.total_string_chars, 3)   # 实际字符 a \n b
        self.assertEqual(stats.byte_size, len('{"k":"a\\nb"}'.encode("utf-8")))


class TestNumberStatistics(unittest.TestCase):
    def test_huge_integers_stay_exact(self):
        big = 10 ** 40 + 7
        value = parse_json(f'[1,{big}]').value
        stats = compute_statistics(value)
        self.assertEqual(stats.number_min, 1)
        self.assertEqual(stats.number_max, big)
        self.assertEqual(stats.number_sum, big + 1)
        self.assertIsInstance(stats.number_sum, int)
        self.assertEqual(stats.integer_count, 2)
        self.assertEqual(stats.float_count, 0)

    def test_decimal_literals_keep_precision(self):
        value = parse_json('["x"]')  # 占位，确保 parse_json 可用
        del value
        numbers = parse_json('[1.230,1e5,0.1,-2.5E-10]').value
        stats = compute_statistics(numbers)
        self.assertEqual(stats.number_count, 4)
        self.assertEqual(stats.float_count, 4)
        self.assertEqual(stats.integer_count, 0)
        self.assertIsInstance(stats.number_min, JsonNumber)
        self.assertEqual(stats.number_min, Decimal("-2.5E-10"))
        self.assertEqual(stats.number_max, Decimal("1e5"))
        self.assertEqual(stats.number_sum, Decimal("1.230") + Decimal("1e5")
                         + Decimal("0.1") + Decimal("-2.5E-10"))

    def test_sum_of_huge_int_and_decimal_is_exact(self):
        big = 123456789012345678901234567890
        value = parse_json(f'{{"n":{big},"m":1.230,"e":1e5}}').value
        stats = compute_statistics(value)
        expected = parse_json("123456789012345678901234667891.230").value
        self.assertEqual(stats.number_sum, expected)
        self.assertIsInstance(stats.number_sum, Decimal)
        self.assertEqual(str(stats.number_sum), "123456789012345678901234667891.230")

    def test_no_numbers_gives_none(self):
        stats = stats_of('{"a":"x","b":null,"c":true,"d":[],"e":{}}')
        self.assertIsNone(stats.number_min)
        self.assertIsNone(stats.number_max)
        self.assertIsNone(stats.number_sum)
        self.assertEqual(stats.number_count, 0)

    def test_negative_numbers(self):
        stats = stats_of('[-5,-1.5,0,3]')
        self.assertEqual(stats.number_min, -5)
        self.assertEqual(stats.number_max, 3)
        self.assertEqual(stats.number_sum, Decimal("-3.5"))

    def test_non_finite_numbers_do_not_raise_or_pollute(self):
        stats = compute_statistics([float("nan"), float("inf"), Decimal("NaN"), 3])
        self.assertEqual(stats.number_count, 4)
        self.assertEqual(stats.float_count, 3)
        self.assertEqual(stats.integer_count, 1)
        self.assertEqual(stats.number_min, 3)
        self.assertEqual(stats.number_max, 3)
        self.assertEqual(stats.number_sum, 3)
        only_nan = compute_statistics([Decimal("NaN")])
        self.assertEqual(only_nan.number_count, 1)
        self.assertIsNone(only_nan.number_sum)
        self.assertIsNone(only_nan.number_min)

    def test_booleans_are_not_numbers(self):
        stats = stats_of('[true,false]')
        self.assertEqual(stats.number_count, 0)
        self.assertEqual(stats.boolean_count, 2)
        self.assertIsNone(stats.number_sum)

    def test_sum_never_becomes_float(self):
        stats = stats_of('[0.1,0.2]')
        self.assertNotIsInstance(stats.number_sum, float)
        self.assertEqual(stats.number_sum, Decimal("0.3"))


class TestFormatBytes(unittest.TestCase):
    def test_byte_boundaries(self):
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(1), "1 B")
        self.assertEqual(format_bytes(1023), "1023 B")
        self.assertEqual(format_bytes(1024), "1.0 KB")
        self.assertEqual(format_bytes(1025), "1.0 KB")

    def test_kilobytes_and_megabytes(self):
        self.assertEqual(format_bytes(1536), "1.5 KB")
        self.assertEqual(format_bytes(1024 * 1024), "1.0 MB")
        self.assertEqual(format_bytes(2411724), "2.3 MB")
        self.assertEqual(format_bytes(1048575), "1.0 MB")   # 1024.0 KB 进位
        self.assertEqual(format_bytes(1024 ** 3), "1.0 GB")
        self.assertEqual(format_bytes(1024 ** 4), "1.0 TB")
        self.assertEqual(format_bytes(1024 ** 5), "1.0 PB")

    def test_negative_and_bad_input(self):
        self.assertEqual(format_bytes(-5), "-5 B")
        self.assertEqual(format_bytes(-2048), "-2.0 KB")
        self.assertEqual(format_bytes("oops"), "n/a")
        self.assertEqual(format_bytes(None), "n/a")

    def test_one_decimal_place(self):
        for value in (2048, 12345, 999999, 123456789):
            with self.subTest(value=value):
                text = format_bytes(value)
                number = text.split(" ")[0]
                self.assertIn(".", number)
                self.assertEqual(len(number.split(".")[1]), 1)


class TestRows(unittest.TestCase):
    def test_label_keys_match_row_labels(self):
        rows = statistics_as_rows(compute_statistics(SAMPLE))
        self.assertEqual([key for key, _value in rows], list(ROW_LABELS))

    def test_values_are_strings(self):
        for key, value in statistics_as_rows(compute_statistics(SAMPLE)):
            with self.subTest(key=key):
                self.assertIsInstance(value, str)
                self.assertTrue(value)

    def test_selected_row_values(self):
        rows = dict(statistics_as_rows(compute_statistics(SAMPLE)))
        self.assertEqual(rows["node.count"], "10")
        self.assertEqual(rows["node.objects"], "2")
        self.assertEqual(rows["node.leaf_ratio"], "0.700")
        self.assertEqual(rows["key.total"], "6")
        self.assertEqual(rows["key.unique"], "6")
        self.assertEqual(rows["key.reused"], "0")
        self.assertEqual(rows["depth.max"], "2")
        self.assertEqual(rows["depth.histogram"], "1:1, 2:4, 3:5")
        self.assertEqual(rows["string.longest_path"], "$.e")
        self.assertEqual(rows["array.longest_path"], "$.a")
        self.assertEqual(rows["number.sum"], "7.5")
        self.assertEqual(rows["size.compact"], "57 B")
        self.assertEqual(rows["key.top"], "a (1), b (1), c (1), d (1), e (1), f (1)")
        self.assertIn("$ (57 B, object)", rows["nodes.largest"])
        self.assertTrue(rows["timing.elapsed_ms"].endswith(" ms"))

    def test_missing_values_render_as_na(self):
        rows = dict(statistics_as_rows(compute_statistics({})))
        self.assertEqual(rows["number.min"], "n/a")
        self.assertEqual(rows["number.max"], "n/a")
        self.assertEqual(rows["number.sum"], "n/a")
        self.assertEqual(rows["key.top"], "n/a")
        self.assertEqual(rows["string.longest_path"], "n/a")
        self.assertEqual(rows["array.longest_path"], "n/a")

    def test_default_statistics_object_renders(self):
        rows = statistics_as_rows(Statistics())
        self.assertEqual(len(rows), len(ROW_LABELS))
        self.assertEqual(dict(rows)["node.count"], "0")

    def test_huge_values_are_exact_in_rows(self):
        rows = dict(statistics_as_rows(stats_of("[123456789012345678901234567890]")))
        self.assertEqual(rows["number.max"], "123456789012345678901234567890")
        self.assertEqual(rows["number.sum"], "123456789012345678901234567890")


class TestPathString(unittest.TestCase):
    def test_root_and_simple_paths(self):
        self.assertEqual(path_string(()), "$")
        self.assertEqual(path_string(("a",)), "$.a")
        self.assertEqual(path_string(("a", 0, "b")), "$.a[0].b")

    def test_keys_needing_quotes(self):
        self.assertEqual(path_string(("weird key",)), "$['weird key']")
        self.assertEqual(path_string(("it's",)), '$["it\'s"]')
        self.assertEqual(path_string(("back\\slash",)), "$['back\\\\slash']")
        self.assertEqual(path_string(("",)), "$['']")
        self.assertEqual(path_string(("\u4e2d\u6587",)), "$['\u4e2d\u6587']")
        self.assertEqual(path_string(("0abc",)), "$['0abc']")
        self.assertEqual(path_string(("a-b",)), "$['a-b']")

    def test_indices_and_bad_input(self):
        self.assertEqual(path_string((12,)), "$[12]")
        self.assertEqual(path_string([("a"), 3]), "$.a[3]")
        self.assertEqual(path_string(None), "$")
        self.assertEqual(path_string(123), "$")


class TestImmutabilityAndRobustness(unittest.TestCase):
    def test_input_is_not_mutated(self):
        value = parse_json('{"a":[1,2,{"b":null}],"c":{"d":"x"}}').value
        before = copy.deepcopy(value)
        compute_statistics(value)
        list(iter_container_sizes(value))
        compact_size(value)
        self.assertTrue(json_equal(before, value))

    def test_non_json_objects_do_not_crash(self):
        for value in ({"a": (1, 2)}, [object()], {"k": object()}, float("nan"), object()):
            with self.subTest(value=type(value).__name__):
                stats = compute_statistics(value)
                self.assertGreaterEqual(stats.node_count, 1)
                self.assertGreaterEqual(stats.byte_size, 0)

    def test_negative_limits_are_clamped(self):
        stats = compute_statistics(SAMPLE, top_n=-3, largest_n=-1)
        self.assertEqual(stats.top_keys, [])
        self.assertEqual(stats.largest_nodes, [])

    def test_bad_limit_types_do_not_raise(self):
        stats = compute_statistics(SAMPLE, top_n=None, largest_n=None)
        self.assertEqual(stats.top_keys, [])
        self.assertEqual(stats.node_count, 10)


class TestPerformance(unittest.TestCase):
    @staticmethod
    def _big_document(records=25000):
        # 每条记录 9 个节点（对象 + 6 个字段 + tags 数组 + 2 个字符串）
        return [
            {
                "id": index,
                "name": f"user{index}",
                "score": index * 1.5,
                "active": bool(index % 2),
                "note": None,
                "tags": ["alpha", "beta"],
            }
            for index in range(records)
        ]

    def test_200k_nodes_under_three_seconds(self):
        document = self._big_document()
        started = time.perf_counter()
        stats = compute_statistics(document)
        elapsed = time.perf_counter() - started
        self.assertEqual(stats.node_count, 225001)
        self.assertEqual(stats.object_count, 25000)
        self.assertEqual(stats.array_count, 25001)   # 25000 个 tags 数组 + 根数组
        self.assertEqual(stats.key_count, 150000)
        self.assertEqual(stats.max_array_length, 25000)
        self.assertEqual(stats.max_depth, 3)
        self.assertLess(elapsed, 3.0, f"compute_statistics took {elapsed:.2f}s")
        self.assertEqual(stats.byte_size, len(minify(document).encode("utf-8")))

    def test_large_document_without_largest_collection(self):
        document = self._big_document(4000)
        started = time.perf_counter()
        stats = compute_statistics(document, collect_largest=False)
        self.assertLess(time.perf_counter() - started, 3.0)
        self.assertEqual(stats.largest_nodes, [])
        self.assertEqual(stats.node_count, 36001)


class TestDeepNesting(unittest.TestCase):
    def test_1200_level_arrays(self):
        depth = 1200
        value = parse_json("[" * depth + "]" * depth, max_depth=5000).value
        stats = compute_statistics(value)
        self.assertEqual(stats.node_count, depth)
        self.assertEqual(stats.max_depth, depth)
        self.assertEqual(len(stats.depth_histogram), depth)
        self.assertEqual(stats.depth_histogram[1], 1)
        self.assertEqual(stats.depth_histogram[depth], 1)
        self.assertEqual(stats.byte_size, 2 * depth)
        self.assertEqual(stats.largest_nodes[0], ("$", 2 * depth, "array"))

    def test_1000_level_objects(self):
        depth = 1000
        text = '{"a":' * depth + '"leaf"' + "}" * depth
        value = parse_json(text, max_depth=5000).value
        stats = compute_statistics(value)
        self.assertEqual(stats.max_depth, depth)
        self.assertEqual(stats.node_count, depth + 1)
        self.assertEqual(stats.longest_string_path, ("a",) * depth)
        self.assertEqual(stats.longest_string_chars, 4)
        self.assertEqual(stats.depth_histogram[depth + 1], 1)
        self.assertEqual(stats.key_count, depth)
        self.assertEqual(stats.unique_key_count, 1)
        self.assertEqual(stats.duplicate_key_count, depth - 1)

    def test_deep_scan_helpers_do_not_recurse(self):
        depth = 1500
        value = parse_json('{"x":' * depth + "[]" + "}" * depth, max_depth=5000).value
        self.assertEqual(compact_size(value), len(minify(value).encode("utf-8")))
        entries = list(iter_container_sizes(value))
        self.assertEqual(len(entries), depth + 1)
        self.assertEqual(entries[-1][1], ())


if __name__ == "__main__":
    unittest.main(verbosity=2)

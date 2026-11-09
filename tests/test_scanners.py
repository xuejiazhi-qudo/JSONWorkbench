"""扫描引擎测试：重复键定位、空值、键值查找、体积分析、结构报告与确定性。"""

from __future__ import annotations

import copy
import re
import unittest

from app.core.jsonformatter import json_equal, minify
from app.core.jsonparser import parse_json
from app.engines.scanners import (
    FINDING_KINDS,
    PREVIEW_LIMIT,
    Finding,
    StructureReport,
    analyze_size,
    analyze_structure,
    find_duplicate_keys,
    find_empty_values,
    find_keys,
    find_structure_findings,
    path_string,
    resolve_path,
    value_preview,
    walk_nodes,
)


def kinds(findings):
    return [finding.kind for finding in findings]


def paths(findings):
    return [finding.path_string for finding in findings]


def parsed(text):
    """解析测试样本；解析失败直接断言失败，避免 fixture 打错字后静默变成 None。"""
    result = parse_json(text)
    assert result.ok, f"{text!r} -> {result.error}"
    return result.value


def tree(depth):
    """构造一棵每层键集合完全相同的树（用于递归形状检测）。"""
    if depth <= 0:
        return {"id": "leaf", "name": "n", "children": []}
    return {"id": f"n{depth}", "name": "n",
            "children": [tree(depth - 1), tree(depth - 1)]}


class TestFindingShape(unittest.TestCase):
    def test_finding_is_frozen(self):
        finding = Finding(kind="null_value", path=("a",), path_string="$.a")
        with self.assertRaises(Exception):
            finding.kind = "other"

    def test_defaults(self):
        finding = Finding(kind="x")
        self.assertEqual(finding.path, ())
        self.assertEqual(finding.path_string, "$")
        self.assertEqual(finding.detail, "")
        self.assertEqual(finding.value_preview, "")
        self.assertEqual(finding.offset, -1)
        self.assertEqual(finding.line, -1)
        self.assertEqual(finding.column, -1)

    def test_kind_vocabulary_is_exported(self):
        for name in ("duplicate_key", "null_value", "empty_string", "empty_array",
                     "empty_object", "key_match", "value_match", "large_node",
                     "deep_node", "recursive_shape", "truncated"):
            self.assertIn(name, FINDING_KINDS)
        self.assertEqual(len(FINDING_KINDS), len(set(FINDING_KINDS)))


class TestDuplicateKeys(unittest.TestCase):
    TEXT = '{\n  "a": 1,\n  "b": {\n    "a": 2,\n    "a": 3\n  },\n  "a": 4\n}'

    def test_positions_match_parser(self):
        findings = find_duplicate_keys(self.TEXT)
        parsed = parse_json(self.TEXT)
        warnings = [w for w in parsed.warnings if w.code == "duplicate_key"]
        self.assertEqual(len(findings), 2)
        self.assertEqual(len(findings), len(warnings))
        for finding, issue in zip(findings, warnings):
            with self.subTest(offset=issue.offset):
                self.assertEqual(finding.kind, "duplicate_key")
                self.assertEqual(finding.offset, issue.offset)
                self.assertEqual(finding.line, issue.line)
                self.assertEqual(finding.column, issue.column)
                self.assertEqual(finding.detail.count("offset"), 1)

    def test_paths_and_previews(self):
        findings = find_duplicate_keys(self.TEXT)
        self.assertEqual(paths(findings), ["$.b.a", "$.a"])
        self.assertEqual(findings[0].path, ("b", "a"))
        # JSON 语义下后者覆盖前者，预览是最终生效的值
        self.assertEqual(findings[0].value_preview, "3")
        self.assertEqual(findings[1].value_preview, "4")

    def test_offset_points_at_the_key(self):
        text = '{"dup":1,"dup":2}'
        finding = find_duplicate_keys(text)[0]
        self.assertEqual(text[finding.offset : finding.offset + 5], '"dup"')
        self.assertEqual(finding.line, 1)
        self.assertEqual(finding.column, 10)
        self.assertIn("first occurrence at offset 1", finding.detail)
        self.assertIn("the last value wins", finding.detail)

    def test_no_duplicates(self):
        self.assertEqual(find_duplicate_keys('{"a":1,"b":{"a":2}}'), [])

    def test_jsonc_mode_is_supported(self):
        text = '// note\n{"a":1, /* c */ "a":2,}'
        findings = find_duplicate_keys(text, mode="jsonc")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].line, 2)
        # 同样文本在严格模式下解析失败，但失败前的重复键仍能报出来
        strict = find_duplicate_keys(text, mode="json")
        self.assertIsInstance(strict, list)

    def test_broken_document_returns_list_not_raise(self):
        self.assertIsInstance(find_duplicate_keys('{"a":1,'), list)
        self.assertEqual(find_duplicate_keys(""), [])
        self.assertEqual(find_duplicate_keys(None), [])
        self.assertEqual(find_duplicate_keys(12345), [])

    def test_unicode_key_duplicate(self):
        text = '{"\u4e2d\u6587":1,"\u4e2d\u6587":2}'
        findings = find_duplicate_keys(text)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, ("\u4e2d\u6587",))
        self.assertEqual(findings[0].path_string, "$['\u4e2d\u6587']")


class TestEmptyValues(unittest.TestCase):
    VALUE = parse_json(
        '{"a":null,"b":"","c":[],"d":{},"e":"   ","f":[null,""],"g":"x","h":[{}]}'
    ).value

    def test_default_kinds_in_document_order(self):
        findings = find_empty_values(self.VALUE)
        self.assertEqual(kinds(findings), [
            "null_value", "empty_string", "empty_array", "empty_object",
            "null_value", "empty_string", "empty_object",
        ])
        self.assertEqual(paths(findings), [
            "$.a", "$.b", "$.c", "$.d", "$.f[0]", "$.f[1]", "$.h[0]",
        ])

    def test_whitespace_only_is_opt_in(self):
        self.assertNotIn("whitespace_string", kinds(find_empty_values(self.VALUE)))
        findings = find_empty_values(self.VALUE, include_whitespace_only_string=True)
        self.assertIn("whitespace_string", kinds(findings))
        whitespace = [f for f in findings if f.kind == "whitespace_string"]
        self.assertEqual(len(whitespace), 1)
        self.assertEqual(whitespace[0].path_string, "$.e")
        self.assertIn("3 chars", whitespace[0].detail)

    def test_flags_disable_kinds(self):
        self.assertEqual(kinds(find_empty_values(self.VALUE, include_null=False)),
                         ["empty_string", "empty_array", "empty_object", "empty_string",
                          "empty_object"])
        self.assertEqual(kinds(find_empty_values(self.VALUE, include_empty_string=False)),
                         ["null_value", "empty_array", "empty_object", "null_value",
                          "empty_object"])
        self.assertEqual(kinds(find_empty_values(self.VALUE, include_empty_array=False)),
                         ["null_value", "empty_string", "empty_object", "null_value",
                          "empty_string", "empty_object"])
        self.assertEqual(kinds(find_empty_values(self.VALUE, include_empty_object=False)),
                         ["null_value", "empty_string", "empty_array", "null_value",
                          "empty_string"])

    def test_all_flags_off_returns_empty(self):
        self.assertEqual(find_empty_values(
            self.VALUE, include_null=False, include_empty_string=False,
            include_empty_array=False, include_empty_object=False,
            include_whitespace_only_string=False), [])

    def test_root_node_is_scanned(self):
        self.assertEqual(kinds(find_empty_values(None)), ["null_value"])
        self.assertEqual(paths(find_empty_values(None)), ["$"])
        self.assertEqual(kinds(find_empty_values({})), ["empty_object"])
        self.assertEqual(kinds(find_empty_values([])), ["empty_array"])
        self.assertEqual(kinds(find_empty_values("")), ["empty_string"])
        self.assertEqual(find_empty_values("x"), [])
        self.assertEqual(find_empty_values({"a": 1}), [])

    def test_previews_are_safe(self):
        findings = find_empty_values(self.VALUE)
        by_kind = {f.kind: f for f in findings}
        self.assertEqual(by_kind["null_value"].value_preview, "null")
        self.assertEqual(by_kind["empty_string"].value_preview, "")
        self.assertEqual(by_kind["empty_array"].value_preview, "array (0 items)")
        self.assertEqual(by_kind["empty_object"].value_preview, "object (0 keys)")

    def test_max_findings_appends_truncation_marker(self):
        findings = find_empty_values(self.VALUE, max_findings=2)
        self.assertEqual(len(findings), 3)
        self.assertEqual(kinds(findings)[:2], ["null_value", "empty_string"])
        marker = findings[-1]
        self.assertEqual(marker.kind, "truncated")
        self.assertEqual(marker.path, ())
        self.assertEqual(marker.path_string, "$")
        self.assertIn("2 findings", marker.detail)

    def test_max_findings_zero(self):
        findings = find_empty_values(self.VALUE, max_findings=0)
        self.assertEqual(kinds(findings), ["truncated"])

    def test_deep_nesting_is_scanned_iteratively(self):
        depth = 1200
        value = parse_json('{"a":' * depth + "null" + "}" * depth, max_depth=5000).value
        findings = find_empty_values(value)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "null_value")
        self.assertEqual(len(findings[0].path), depth)

    def test_unicode_keys_and_values(self):
        value = parse_json('{"\u4e2d\u6587\u952e":"","\u503c":null}').value
        findings = find_empty_values(value)
        self.assertEqual(paths(findings), ["$['\u4e2d\u6587\u952e']", "$['\u503c']"])
        self.assertEqual(kinds(findings), ["empty_string", "null_value"])


class TestFindKeys(unittest.TestCase):
    VALUE = parse_json(
        '{"userName":"bob","user_id":7,"nested":{"UserName":"ALICE"},'
        '"list":[{"userName":"x"}],"other":"user text"}'
    ).value

    def test_literal_case_insensitive(self):
        findings = find_keys(self.VALUE, "username")
        self.assertEqual(kinds(findings), ["key_match"] * 3)
        self.assertEqual(paths(findings), ["$.userName", "$.nested.UserName",
                                           "$.list[0].userName"])
        self.assertEqual(findings[0].value_preview, "bob")
        self.assertIn("key 'userName' matches 'username'", findings[0].detail)

    def test_literal_case_sensitive(self):
        # 大小写敏感时 "username" 不匹配 "userName"
        self.assertEqual(find_keys(self.VALUE, "username", case_sensitive=True), [])
        findings = find_keys(self.VALUE, "userName", case_sensitive=True)
        self.assertEqual(paths(findings), ["$.userName", "$.list[0].userName"])
        findings = find_keys(self.VALUE, "UserName", case_sensitive=True)
        self.assertEqual(paths(findings), ["$.nested.UserName"])

    def test_substring_semantics(self):
        findings = find_keys(self.VALUE, "user", case_sensitive=True)
        self.assertEqual(paths(findings), ["$.userName", "$.user_id",
                                           "$.list[0].userName"])

    def test_regex_mode(self):
        findings = find_keys(self.VALUE, r"^user", regex=True, case_sensitive=True)
        self.assertEqual(paths(findings), ["$.userName", "$.user_id",
                                           "$.list[0].userName"])
        # 默认大小写不敏感，^user 也能命中 UserName
        findings = find_keys(self.VALUE, r"^user", regex=True)
        self.assertEqual(paths(findings), ["$.userName", "$.user_id",
                                           "$.nested.UserName", "$.list[0].userName"])
        findings = find_keys(self.VALUE, r"^USER", regex=True, case_sensitive=True)
        self.assertEqual(findings, [])
        findings = find_keys(self.VALUE, r"Name$", regex=True)
        self.assertEqual(paths(findings), ["$.userName", "$.nested.UserName",
                                           "$.list[0].userName"])

    def test_match_values(self):
        findings = find_keys(self.VALUE, "alice", match_values=True)
        self.assertEqual(kinds(findings), ["value_match"])
        self.assertEqual(paths(findings), ["$.nested.UserName"])
        self.assertEqual(findings[0].value_preview, "ALICE")
        findings = find_keys(self.VALUE, "user text", match_values=True)
        self.assertEqual(paths(findings), ["$.other"])

    def test_match_values_covers_numbers_and_literals(self):
        findings = find_keys(self.VALUE, "7", match_values=True)
        self.assertEqual([f.kind for f in findings], ["value_match"])
        self.assertEqual(paths(findings), ["$.user_id"])
        value = parse_json('{"t":true,"f":false,"n":null,"x":1.50}').value
        self.assertEqual(paths(find_keys(value, "true", match_values=True)), ["$.t"])
        self.assertEqual(paths(find_keys(value, "false", match_values=True)), ["$.f"])
        self.assertEqual(paths(find_keys(value, "null", match_values=True)), ["$.n"])
        self.assertEqual(paths(find_keys(value, "1.50", match_values=True)), ["$.x"])

    def test_key_reported_before_value_for_same_node(self):
        value = parse_json('{"match":"match"}').value
        findings = find_keys(value, "match", match_values=True)
        self.assertEqual(kinds(findings), ["key_match", "value_match"])
        self.assertEqual(paths(findings), ["$.match", "$.match"])

    def test_containers_are_not_value_matched(self):
        findings = find_keys(self.VALUE, "list", match_values=True)
        self.assertEqual(kinds(findings), ["key_match"])

    def test_invalid_regex_reports_finding(self):
        findings = find_keys(self.VALUE, "[", regex=True)
        self.assertEqual(kinds(findings), ["invalid_pattern"])
        self.assertIn("invalid regular expression", findings[0].detail)
        self.assertEqual(findings[0].path_string, "$")

    def test_empty_pattern_reports_finding(self):
        findings = find_keys(self.VALUE, "")
        self.assertEqual(kinds(findings), ["invalid_pattern"])
        self.assertIn("empty", findings[0].detail)

    def test_max_findings_truncation(self):
        findings = find_keys(self.VALUE, "user", max_findings=1)
        self.assertEqual(kinds(findings), ["key_match", "truncated"])
        findings = find_keys(self.VALUE, "user", max_findings=2)
        self.assertEqual(kinds(findings), ["key_match", "key_match", "truncated"])
        self.assertIn("2 findings", findings[-1].detail)
        # 命中数没超过上限时不追加 truncated 标记
        self.assertEqual(kinds(find_keys(self.VALUE, "user_id", max_findings=5)),
                         ["key_match"])

    def test_unicode_pattern_and_keys(self):
        value = parsed('{"\u4e2d\u6587\u952e":"\u503c\u503c","other":"\u666e\u901a"}')
        findings = find_keys(value, "\u4e2d\u6587")
        self.assertEqual(paths(findings), ["$['\u4e2d\u6587\u952e']"])
        self.assertEqual(kinds(findings), ["key_match"])
        findings = find_keys(value, "\u503c", match_values=True)
        self.assertEqual(kinds(findings), ["value_match"])
        self.assertEqual(paths(findings), ["$['\u4e2d\u6587\u952e']"])
        self.assertEqual(find_keys(value, "\u4e0d\u5b58\u5728"), [])

    def test_regex_with_unicode_escape(self):
        value = parse_json('{"key_1":1,"key_2":2,"other":3}').value
        findings = find_keys(value, r"key_\d", regex=True)
        self.assertEqual(paths(findings), ["$.key_1", "$.key_2"])

    def test_no_match_returns_empty(self):
        self.assertEqual(find_keys(self.VALUE, "zzz"), [])
        self.assertEqual(find_keys({}, "a"), [])
        self.assertEqual(find_keys(None, "a"), [])


class TestAnalyzeSize(unittest.TestCase):
    VALUE = parse_json('{"a":[1,2,3],"b":{"c":null,"d":true},"e":"hello","f":1.5}').value

    def test_ordering_and_content(self):
        rows = analyze_size(self.VALUE)
        self.assertEqual(rows, [("$", 57, "object"), ("$.b", 19, "object"),
                                ("$.a", 7, "array")])

    def test_sizes_match_minify(self):
        rows = analyze_size(self.VALUE)
        for path_text, size, kind in rows:
            with self.subTest(path=path_text):
                node = resolve_path(self.VALUE, _path_from_text(path_text))
                self.assertEqual(size, len(minify(node).encode("utf-8")))
                self.assertEqual(kind, "object" if isinstance(node, dict) else "array")

    def test_top_n_and_tie_break(self):
        value = {"first": [1, 2], "second": [3, 4], "third": [5, 6]}
        rows = analyze_size(value, top_n=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "$")
        self.assertEqual(rows[1][0], "$.first")   # 同尺寸时文档顺序在前
        self.assertEqual(analyze_size(value, top_n=0), [])

    def test_scalars_and_empty(self):
        self.assertEqual(analyze_size("scalar"), [])
        self.assertEqual(analyze_size(None), [])
        self.assertEqual(analyze_size(12), [])
        self.assertEqual(analyze_size({}), [("$", 2, "object")])
        self.assertEqual(analyze_size([]), [("$", 2, "array")])

    def test_deep_document(self):
        depth = 800
        value = parse_json("[" * depth + "]" * depth, max_depth=5000).value
        rows = analyze_size(value, top_n=3)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0], ("$", 2 * depth, "array"))


def _path_from_text(text):
    """把 path_string 反解成路径元组（仅测试用，覆盖本文件出现的形式）。"""
    if text == "$":
        return ()
    body = text[1:]
    parts = []
    for token in re.findall(r"\[(\d+)\]|\['((?:[^'\\]|\\.)*)'\]|\.([^.\[]+)", body):
        index, quoted, dotted = token
        if index:
            parts.append(int(index))
        elif quoted:
            parts.append(quoted.replace("\\'", "'").replace("\\\\", "\\"))
        else:
            parts.append(dotted)
    return tuple(parts)


class TestResolvePath(unittest.TestCase):
    VALUE = parse_json('{"a":[10,{"b":"c"}],"d":null}').value

    def test_resolution(self):
        self.assertEqual(resolve_path(self.VALUE, ()), self.VALUE)
        self.assertEqual(resolve_path(self.VALUE, ("a", 0)), 10)
        self.assertEqual(resolve_path(self.VALUE, ("a", 1, "b")), "c")
        self.assertIsNone(resolve_path(self.VALUE, ("d",)))
        self.assertIsNone(resolve_path(self.VALUE, ("a", 9)))
        self.assertIsNone(resolve_path(self.VALUE, ("missing",)))
        self.assertIsNone(resolve_path(self.VALUE, ("a", "0")))
        self.assertIsNone(resolve_path(None, ("a",)))


class TestStructureReport(unittest.TestCase):
    def test_recursive_tree_shape(self):
        report = analyze_structure(tree(5))
        self.assertEqual(len(report.shape_signatures), 1)
        signature, count, example = report.shape_signatures[0]
        self.assertEqual(signature, "{children,id,name}")
        self.assertEqual(count, 63)               # 2^6 - 1 个同构对象
        self.assertEqual(example, "$")
        self.assertEqual(len(report.recursive_shapes), 1)
        shape_signature, depths, shape_example = report.recursive_shapes[0]
        self.assertEqual(shape_signature, "{children,id,name}")
        self.assertEqual(depths, [1, 3, 5, 7, 9, 11])
        self.assertEqual(shape_example, "$")
        self.assertTrue(any("self-similar" in s for s in report.suggestions))

    def test_non_recursive_document_has_no_recursive_shapes(self):
        report = analyze_structure(parse_json('{"a":{"b":{"c":1}}}').value)
        self.assertEqual(report.recursive_shapes, [])
        self.assertEqual([s for s, _c, _e in report.shape_signatures],
                         ["{a}", "{b}", "{c}"])

    def test_key_type_conflict(self):
        value = parsed('{"date":"2020-01-01","other":{"date":12345},'
                       '"third":{"date":true}}')
        report = analyze_structure(value)
        self.assertEqual(len(report.key_type_conflicts), 1)
        key, types, example = report.key_type_conflicts[0]
        self.assertEqual(key, "date")
        self.assertEqual(types, ["string", "number", "boolean"])
        self.assertEqual(example, "$.other.date")
        self.assertIn("consider normalizing 'date' (string vs number vs boolean)",
                      " ".join(report.suggestions))

    def test_no_conflict_when_types_agree(self):
        report = analyze_structure(parse_json('{"a":{"v":1},"b":{"v":2}}').value)
        self.assertEqual(report.key_type_conflicts, [])

    def test_array_homogeneity(self):
        value = parse_json('{"same":[1,2,3],"mixed":[1,"a",true,null,{"k":1},[2]],'
                           '"empty":[]}').value
        report = analyze_structure(value)
        by_path = {row[0]: row for row in report.array_homogeneity}
        self.assertEqual(by_path["$.same"], ("$.same", 3, ("number",), True))
        self.assertEqual(by_path["$.empty"], ("$.empty", 0, (), True))
        mixed = by_path["$.mixed"]
        self.assertEqual(mixed[1], 6)
        self.assertEqual(mixed[2], ("number", "string", "boolean", "null", "object",
                                    "array"))
        self.assertFalse(mixed[3])
        # 文档顺序（前序）：$.mixed[5] 是 $.mixed 的后代，排在根对象下一个键之前
        self.assertEqual([row[0] for row in report.array_homogeneity],
                         ["$.same", "$.mixed", "$.mixed[5]", "$.empty"])

    def test_depth_histogram(self):
        report = analyze_structure(parse_json(
            '{"a":[1,2,3],"b":{"c":null,"d":true},"e":"hello","f":1.5}').value)
        self.assertEqual(report.depth_histogram, {1: 1, 2: 4, 3: 5})
        self.assertEqual(report.max_depth, 3)
        self.assertEqual(sum(report.depth_histogram.values()), 10)

    def test_max_depth_limits_analysis(self):
        report = analyze_structure(tree(5), max_depth=3)
        self.assertTrue(report.truncated)
        self.assertEqual(max(report.depth_histogram), 3)
        self.assertTrue(any("depth limit" in s for s in report.suggestions))
        deep = analyze_structure(tree(5), max_depth=64)
        self.assertFalse(deep.truncated)

    def test_suggestions_for_repeated_shape(self):
        value = parse_json('{"list":[{"id":1,"n":"a"},{"id":2,"n":"b"},'
                           '{"id":3,"n":"c"}]}').value
        report = analyze_structure(value)
        self.assertTrue(any("appears 3 times" in s for s in report.suggestions))

    def test_suggestions_for_heterogeneous_array(self):
        report = analyze_structure(parse_json('[1,"a",true]').value)
        self.assertTrue(any("mixes element types" in s for s in report.suggestions))

    def test_clean_document_gets_positive_note(self):
        report = analyze_structure(parse_json('{"a":1}').value)
        self.assertEqual(report.suggestions,
                         ["structure looks consistent; no normalization needed"])

    def test_empty_and_scalar_roots(self):
        for value in ([], None, "x", 1, True):
            with self.subTest(value=value):
                report = analyze_structure(value)
                self.assertIsInstance(report, StructureReport)
                self.assertEqual(report.shape_signatures, [])
                self.assertEqual(report.key_type_conflicts, [])
                self.assertEqual(report.recursive_shapes, [])
                self.assertEqual(report.depth_histogram, {1: 1})
        empty = analyze_structure({})
        self.assertEqual(empty.shape_signatures, [("{}", 1, "$")])
        self.assertEqual(empty.recursive_shapes, [])
        self.assertEqual(empty.depth_histogram, {1: 1})
        self.assertEqual(empty.array_homogeneity, [])

    def test_deep_document_does_not_recurse(self):
        depth = 1000
        value = parse_json('{"a":' * depth + "1" + "}" * depth, max_depth=5000).value
        report = analyze_structure(value, max_depth=1200)
        self.assertEqual(report.max_depth, 1001)
        self.assertEqual(report.depth_histogram[1001], 1)

    def test_deterministic_repeated_calls(self):
        value = tree(3)
        first = analyze_structure(value)
        second = analyze_structure(value)
        self.assertEqual(first.shape_signatures, second.shape_signatures)
        self.assertEqual(first.recursive_shapes, second.recursive_shapes)
        self.assertEqual(first.suggestions, second.suggestions)
        self.assertEqual(first.array_homogeneity, second.array_homogeneity)

    def test_bad_max_depth_is_clamped(self):
        report = analyze_structure(tree(2), max_depth=0)
        self.assertEqual(max(report.depth_histogram), 1)
        report = analyze_structure(tree(2), max_depth=None)
        self.assertGreaterEqual(report.max_depth, 1)


class TestStructureFindings(unittest.TestCase):
    def test_large_and_deep_nodes(self):
        value = {"blob": "x" * 5000, "small": "y", "child": {"list": [1, 2, 3]}}
        findings = find_structure_findings(value, min_bytes=10, deep_threshold=100)
        self.assertEqual(kinds(findings), ["large_node", "large_node"])
        self.assertEqual(paths(findings), ["$", "$.child"])
        expected = len(minify(value).encode("utf-8"))
        self.assertIn(f"{expected} bytes", findings[0].detail)
        self.assertEqual(findings[0].path, ())
        self.assertEqual(findings[0].offset, -1)

    def test_deep_node_reported_once_at_the_threshold(self):
        value = parsed('{"a":' * 10 + "1" + "}" * 10)
        findings = find_structure_findings(value, min_bytes=10 ** 9, deep_threshold=5,
                                           detect_recursive=False)
        self.assertEqual(kinds(findings), ["deep_node"])
        self.assertEqual(paths(findings), ["$" + ".a" * 5])
        self.assertIn("depth 6", findings[0].detail)

    def test_recursive_shape_findings(self):
        findings = find_structure_findings(tree(3), min_bytes=10 ** 9, deep_threshold=99)
        self.assertEqual(kinds(findings), ["recursive_shape"])
        self.assertIn("{children,id,name}", findings[0].detail)
        self.assertEqual(findings[0].path_string, "$")

    def test_recursive_detection_can_be_disabled(self):
        self.assertEqual(find_structure_findings(tree(3), min_bytes=10 ** 9,
                                                 deep_threshold=99,
                                                 detect_recursive=False), [])

    def test_document_order_across_kinds(self):
        value = {"a": {"pad": "x" * 2000}, "b": [1, 2, 3]}
        findings = find_structure_findings(value, min_bytes=5, deep_threshold=99,
                                           detect_recursive=False)
        self.assertEqual(paths(findings), ["$", "$.a", "$.b"])
        self.assertEqual(kinds(findings), ["large_node"] * 3)

    def test_truncation_marker(self):
        value = {"a": {"pad": "x" * 2000}, "b": {"pad": "y" * 2000}}
        findings = find_structure_findings(value, min_bytes=5, max_findings=2)
        self.assertEqual(len(findings), 3)
        self.assertEqual(findings[-1].kind, "truncated")

    def test_empty_document(self):
        self.assertEqual(find_structure_findings({}), [])
        self.assertEqual(find_structure_findings(None), [])

    def test_previews_are_bounded(self):
        value = {"blob": "x" * 5000}
        findings = find_structure_findings(value, min_bytes=10)
        for finding in findings:
            self.assertLessEqual(len(finding.value_preview), PREVIEW_LIMIT)


class TestValuePreview(unittest.TestCase):
    def test_scalars(self):
        self.assertEqual(value_preview("abc"), "abc")
        self.assertEqual(value_preview(None), "null")
        self.assertEqual(value_preview(True), "true")
        self.assertEqual(value_preview(False), "false")
        self.assertEqual(value_preview(parse_json("1.230").value), "1.230")
        self.assertEqual(value_preview(parse_json("-0").value), "-0")

    def test_containers_are_summarized(self):
        value = parse_json('{"a":1,"b":2,"c":3,"d":4,"e":5,"f":6}').value
        self.assertEqual(value_preview(value),
                         "object (6 keys: a, b, c, d, e, ...)")
        self.assertEqual(value_preview([1, "a", None]),
                         "array (3 items: number, string, null)")
        self.assertEqual(value_preview({}), "object (0 keys)")
        self.assertEqual(value_preview([]), "array (0 items)")

    def test_control_characters_are_escaped(self):
        text = value_preview("a\tb\nc\rd\x01e\x7f")
        self.assertEqual(text, "a\\tb\\nc\\rd\\u0001e\\u007f")
        self.assertNotIn("\t", text)
        self.assertNotIn("\n", text)

    def test_truncation_respects_limit(self):
        text = value_preview("x" * 5000)
        self.assertEqual(len(text), PREVIEW_LIMIT)
        self.assertTrue(text.endswith("..."))
        self.assertEqual(value_preview("x" * 5000, limit=10), "xxxxxxx...")
        self.assertEqual(value_preview("abc", limit=10), "abc")
        self.assertEqual(value_preview("abc", limit=0), "")

    def test_unicode_is_preserved(self):
        self.assertEqual(value_preview("\u4e2d\u6587\u503c"), "\u4e2d\u6587\u503c")
        self.assertEqual(value_preview("\U0001f600"), "\U0001f600")


class TestWalkNodes(unittest.TestCase):
    def test_pre_order_paths_and_depths(self):
        value = parse_json('{"a":[10,20],"b":{"c":1}}').value
        walked = list(walk_nodes(value))
        self.assertEqual(walked, [
            ((), value, 1),
            (("a",), value["a"], 2),
            (("a", 0), 10, 3),
            (("a", 1), 20, 3),
            (("b",), value["b"], 2),
            (("b", "c"), 1, 3),
        ])

    def test_max_depth_stops_expansion(self):
        value = parse_json('{"a":{"b":{"c":1}}}').value
        walked = list(walk_nodes(value, max_depth=2))
        self.assertEqual([path for path, _n, _d in walked], [(), ("a",)])

    def test_scalars_and_empty_containers(self):
        self.assertEqual(list(walk_nodes(None)), [((), None, 1)])
        self.assertEqual(list(walk_nodes({})), [((), {}, 1)])
        self.assertEqual(list(walk_nodes([])), [((), [], 1)])

    def test_sequence_numbers_align_with_container_sizes(self):
        from app.core.jsonvalue import json_type
        from app.engines.statistics import iter_container_sizes

        value = parse_json('{"a":[1,2,3],"b":{"c":null,"d":true},"e":"hello"}').value
        walked = list(enumerate(walk_nodes(value)))
        for seq, path, size, kind in iter_container_sizes(value):
            with self.subTest(seq=seq):
                walked_path, walked_node, walked_depth = walked[seq][1]
                self.assertEqual(walked_path, path)
                self.assertEqual(json_type(walked_node), kind)
                self.assertEqual(walked_depth, len(path) + 1)
                self.assertEqual(size, len(minify(walked_node).encode("utf-8")))


class TestRobustness(unittest.TestCase):
    def test_input_is_never_mutated(self):
        value = parse_json('{"a":[1,2,{"b":null}],"c":"","d":{},"e":[]}').value
        before = copy.deepcopy(value)
        find_empty_values(value)
        find_keys(value, "a", match_values=True)
        analyze_size(value)
        analyze_structure(value)
        find_structure_findings(value, min_bytes=1)
        list(walk_nodes(value))
        self.assertTrue(json_equal(before, value))

    def test_non_json_objects_do_not_raise(self):
        for value in ({"a": (1, 2)}, [object()], object(), {"k": {1: "x"}}):
            with self.subTest(value=type(value).__name__):
                self.assertIsInstance(find_empty_values(value), list)
                self.assertIsInstance(find_keys(value, "a"), list)
                self.assertIsInstance(analyze_size(value), list)
                self.assertIsInstance(analyze_structure(value).suggestions, list)
                self.assertIsInstance(find_structure_findings(value), list)

    def test_repeated_calls_are_deterministic(self):
        value = parse_json('{"a":null,"b":[1,"x",{}],"c":{"a":null}}').value
        self.assertEqual(kinds(find_empty_values(value)),
                         kinds(find_empty_values(value)))
        self.assertEqual(paths(find_keys(value, "a")), paths(find_keys(value, "a")))
        self.assertEqual(analyze_size(value), analyze_size(value))
        self.assertEqual(kinds(find_structure_findings(value, min_bytes=1)),
                         kinds(find_structure_findings(value, min_bytes=1)))

    def test_all_findings_have_safe_text(self):
        value = parse_json('{"a\tb":null,"c":"x\u0001y","d":[[]],"e":{}}').value
        findings = (find_empty_values(value) + find_keys(value, "", regex=False)
                    + find_keys(value, "a", match_values=True))
        self.assertTrue(findings)
        for finding in findings:
            self.assertLessEqual(len(finding.value_preview), PREVIEW_LIMIT)
            for char in finding.value_preview + finding.detail:
                self.assertGreaterEqual(ord(char), 0x20, repr(char))
                self.assertNotEqual(ord(char), 0x7F)
            self.assertTrue(finding.path_string.startswith("$"))
            self.assertIn(finding.kind, FINDING_KINDS)

    def test_path_string_matches_statistics_helper(self):
        from app.engines.statistics import path_string as statistics_path_string

        samples = [(), ("a",), ("a", 0), ("weird key",), ("it's",), ("\u4e2d",), ("",)]
        for sample in samples:
            self.assertEqual(path_string(sample), statistics_path_string(sample))


if __name__ == "__main__":
    unittest.main(verbosity=2)

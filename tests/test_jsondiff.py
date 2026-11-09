"""结构化 JSON 对比引擎测试。

覆盖重点（对应交付要求）：

* 每一种 ``KIND_*``：added / removed / changed / type_changed / reordered；
* **键顺序变更 vs 真实数据变更**：三种选项组合（默认、``report_reordered``、
  ``ignore_key_order=False``）以及"键序不同 + 值也不同"的混合场景；
* 三种 ``array_mode``：index / set / key，含重排、中间插入、重复键、无键元素；
* ``ignore_fields`` 的三种写法：纯字段名、``*`` 通配、``$.a.b`` 路径模式；
* ``ignore_whitespace`` / ``ignore_string_case`` / ``ignore_number_format`` 双向；
* 数字字面量保真（``1.230``、``1e2``、``-0``、30 位大整数）；
* Unicode（中文键名 + emoji）出现在路径与值里；
* 空文档、完全相同的文档、``max_items`` 截断、1000 层嵌套不 ``RecursionError``；
* ``diff_text`` 单侧/双侧非法；
* 三个报表生成器都产出非空且格式正确的输出（HTML 用 ``<script>`` 载荷证明转义）；
* ``aligned_lines`` 的行号单调性与标记合法性、无损/折叠两种模式；
* ``changed_only_text`` 能重新解析且只含被差异触及的子树；
* 5 万节点的性能 sanity。
"""

from __future__ import annotations

import copy
import time
import unittest
from decimal import Decimal

from app.core.jsonparser import parse_json
from app.core.jsonvalue import JsonNumber
from app.engines import jsondiff
from app.engines.jsondiff import (
    ALIGN_MARKERS,
    ARRAY_INDEX,
    ARRAY_KEY,
    ARRAY_SET,
    DIFF_KINDS,
    DiffItem,
    DiffOptions,
    DiffResult,
    KIND_ADDED,
    KIND_CHANGED,
    KIND_REMOVED,
    KIND_REORDERED,
    KIND_TYPE_CHANGED,
    MARK_ADDED,
    MARK_CHANGED,
    MARK_CONTEXT,
    MARK_EQUAL,
    MARK_REMOVED,
    MAX_DEPTH,
    aligned_lines,
    changed_only_text,
    describe_diff_item,
    diff_json,
    diff_text,
    diff_to_html,
    diff_to_json_text,
    diff_to_markdown,
    field_ignored,
    path_string,
)


def parse(text: str):
    """测试用解析助手：深文档也放行，失败直接断言出错。"""
    result = parse_json(text, max_depth=5000)
    assert result.ok, result.error
    return result.value


def kinds(result: DiffResult):
    return [(item.kind, item.path_string) for item in result.items]


def pretty(value) -> str:
    from app.core.jsonformatter import INDENT_2, FormatOptions, dumps

    return dumps(value, FormatOptions(indent=INDENT_2, line_ending="\n"))


# --------------------------------------------------------------------------- #
# 基础：每一种 KIND_*
# --------------------------------------------------------------------------- #
class TestKinds(unittest.TestCase):
    def test_added_and_removed_and_changed(self):
        result = diff_json(parse('{"keep":1,"gone":"x"}'), parse('{"keep":1,"new":true}'))
        self.assertTrue(result.changed)
        self.assertEqual(result.error, "")
        self.assertEqual(kinds(result), [("removed", "$.gone"), ("added", "$.new")])
        removed, added = result.items
        self.assertEqual(removed.left, "x")
        self.assertIsNone(removed.right)
        self.assertEqual(removed.left_type, "string")
        self.assertEqual(removed.right_type, "")
        self.assertIsNone(added.left)
        self.assertEqual(added.right, True)
        self.assertEqual(added.left_type, "")
        self.assertEqual(added.right_type, "boolean")
        self.assertEqual(removed.depth, 1)
        self.assertEqual(added.path, ("new",))

    def test_changed_scalar_keeps_both_values(self):
        result = diff_json(parse('{"a":1}'), parse('{"a":2}'))
        self.assertEqual(kinds(result), [("changed", "$.a")])
        item = result.items[0]
        self.assertEqual((item.left, item.right), (1, 2))
        self.assertEqual((item.left_type, item.right_type), ("number", "number"))
        self.assertEqual(item.depth, 1)
        self.assertEqual(item.detail, "")

    def test_type_changed_kinds(self):
        cases = [
            ('{"a":1}', '{"a":true}', "number", "boolean"),
            ('{"a":null}', '{"a":false}', "null", "boolean"),
            ('{"a":"1"}', '{"a":1}', "string", "number"),
            ('{"a":{"x":1}}', '{"a":[1]}', "object", "array"),
            ('{"a":[1,2]}', '{"a":{"b":1}}', "array", "object"),
        ]
        for left, right, left_type, right_type in cases:
            with self.subTest(left=left, right=right):
                result = diff_json(parse(left), parse(right))
                self.assertEqual(kinds(result), [("type_changed", "$.a")])
                item = result.items[0]
                self.assertEqual(item.kind, KIND_TYPE_CHANGED)
                self.assertEqual(item.left_type, left_type)
                self.assertEqual(item.right_type, right_type)
                self.assertIn(f"{left_type} -> {right_type}", item.detail)

    def test_type_changed_emits_no_child_noise(self):
        left = parse('{"a":{"x":1,"y":2,"z":[1,2,3]}}')
        right = parse('{"a":["completely","different"]}')
        result = diff_json(left, right)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].kind, KIND_TYPE_CHANGED)
        self.assertEqual(result.items[0].path_string, "$.a")
        self.assertEqual(result.summary["added"], 0)
        self.assertEqual(result.summary["removed"], 0)
        self.assertEqual(result.summary["changed"], 0)
        self.assertEqual(result.summary["type_changed"], 1)
        # 子树整体作为值挂在条目上，UI 可以自己展开
        self.assertEqual(result.items[0].left, {"x": 1, "y": 2, "z": [1, 2, 3]})
        self.assertEqual(result.items[0].right, ["completely", "different"])

    def test_array_to_object_and_back_has_no_child_items(self):
        result = diff_json(parse('[[1,2,3]]'), parse('[{"a":1,"b":2}]'))
        self.assertEqual(kinds(result), [("type_changed", "$[0]")])

    def test_reordered_kind_carries_key_sequences(self):
        result = diff_json(parse('{"a":1,"b":2}'), parse('{"b":2,"a":1}'),
                           DiffOptions(report_reordered=True))
        self.assertEqual(kinds(result), [("reordered", "$")])
        item = result.items[0]
        self.assertEqual(item.kind, KIND_REORDERED)
        self.assertEqual(item.left, ("a", "b"))
        self.assertEqual(item.right, ("b", "a"))
        self.assertEqual(item.left_type, "object")
        self.assertEqual(item.right_type, "object")
        self.assertEqual(item.depth, 0)
        self.assertEqual(item.path, ())
        self.assertTrue(item.is_reorder)
        self.assertIn("key order", item.detail)

    def test_every_kind_constant_is_reachable(self):
        left = parse('{"gone":1,"same":0,"num":1,"obj":{"k":1}}')
        right = parse('{"new":2,"same":0,"num":"1","obj":{"k":1}}')
        result = diff_json(left, right, DiffOptions(report_reordered=True))
        seen = {item.kind for item in result.items}
        self.assertIn(KIND_ADDED, seen)
        self.assertIn(KIND_REMOVED, seen)
        self.assertIn(KIND_TYPE_CHANGED, seen)
        for kind in DIFF_KINDS:
            self.assertIn(kind, result.summary)

    def test_diff_item_is_frozen_and_describable(self):
        item = diff_json(parse('{"a":1}'), parse('{"a":2}')).items[0]
        with self.assertRaises(Exception):
            item.kind = KIND_ADDED  # type: ignore[misc]
        text = item.describe()
        self.assertIsInstance(text, str)
        self.assertIn("changed", text)
        self.assertIn("1", text)
        self.assertIn("2", text)
        self.assertEqual(text, describe_diff_item(item))

    def test_summary_and_result_helpers(self):
        result = diff_json(parse('{"a":1,"b":2}'), parse('{"a":9,"b":2,"c":3}'))
        self.assertEqual(result.summary["changed"], 1)
        self.assertEqual(result.summary["added"], 1)
        self.assertEqual(result.summary["removed"], 0)
        self.assertEqual(result.summary["type_changed"], 0)
        self.assertEqual(result.summary["reordered"], 0)
        self.assertEqual(result.summary["unchanged"], 1)
        self.assertEqual(result.summary["total"], 2)
        self.assertIs(result.summary["truncated"], False)
        self.assertFalse(result.truncated)
        self.assertEqual(len(result.by_kind(KIND_CHANGED)), 1)
        self.assertEqual(result.paths(), ["$.a", "$.c"])
        self.assertGreaterEqual(result.elapsed_ms, 0.0)
        self.assertEqual(result.left_nodes, 3)
        self.assertEqual(result.right_nodes, 4)
        self.assertIsInstance(result.options, DiffOptions)


# --------------------------------------------------------------------------- #
# 键顺序 vs 真实变更（最重要的行为）
# --------------------------------------------------------------------------- #
class TestKeyOrderSemantics(unittest.TestCase):
    LEFT = '{"a":1,"b":{"x":1,"y":2},"c":[1,2]}'
    RIGHT = '{"c":[1,2],"b":{"y":2,"x":1},"a":1}'

    def test_default_reports_nothing_for_key_order_only(self):
        result = diff_json(parse(self.LEFT), parse(self.RIGHT))
        self.assertFalse(result.changed)
        self.assertEqual(result.items, [])
        self.assertEqual(result.error, "")
        self.assertEqual(result.summary["total"], 0)
        self.assertEqual(result.summary["reordered"], 0)
        self.assertGreater(result.summary["unchanged"], 0)

    def test_report_reordered_emits_reordered_items(self):
        result = diff_json(parse(self.LEFT), parse(self.RIGHT),
                           DiffOptions(report_reordered=True))
        self.assertTrue(result.changed)
        self.assertEqual(kinds(result), [("reordered", "$"), ("reordered", "$.b")])
        self.assertEqual(result.summary["reordered"], 2)
        self.assertEqual(result.summary["changed"], 0)
        self.assertEqual(result.summary["added"], 0)
        self.assertEqual(result.summary["removed"], 0)
        self.assertEqual(result.items[0].left, ("a", "b", "c"))
        self.assertEqual(result.items[0].right, ("c", "b", "a"))
        self.assertEqual(result.items[1].left, ("x", "y"))
        self.assertEqual(result.items[1].right, ("y", "x"))

    def test_ignore_key_order_false_always_reports_reordered(self):
        result = diff_json(parse(self.LEFT), parse(self.RIGHT),
                           DiffOptions(ignore_key_order=False))
        self.assertTrue(result.changed)
        self.assertEqual(result.summary["reordered"], 2)
        self.assertEqual(kinds(result), [("reordered", "$"), ("reordered", "$.b")])

    def test_ignore_key_order_false_with_report_reordered_is_not_duplicated(self):
        result = diff_json(parse(self.LEFT), parse(self.RIGHT),
                           DiffOptions(ignore_key_order=False, report_reordered=True))
        self.assertEqual(result.summary["reordered"], 2)
        self.assertEqual(len(result.items), 2)

    def test_key_order_does_not_hide_a_real_change(self):
        result = diff_json(parse('{"a":1,"b":2}'), parse('{"b":2,"a":3}'))
        self.assertTrue(result.changed)
        self.assertEqual(kinds(result), [("changed", "$.a")])
        self.assertEqual(result.summary["reordered"], 0)

    def test_key_order_plus_real_change_with_report_reordered(self):
        result = diff_json(parse('{"a":1,"b":2}'), parse('{"b":3,"a":1}'),
                           DiffOptions(report_reordered=True))
        self.assertEqual(kinds(result), [("reordered", "$"), ("changed", "$.b")])

    def test_deep_key_order_only_difference_is_still_found(self):
        left = parse('{"o":{"p":{"q":{"r":1,"s":2}}}}')
        right = parse('{"o":{"p":{"q":{"s":2,"r":1}}}}')
        silent = diff_json(left, right)
        self.assertFalse(silent.changed)
        self.assertEqual(silent.items, [])
        loud = diff_json(left, right, DiffOptions(report_reordered=True))
        self.assertEqual(kinds(loud), [("reordered", "$.o.p.q")])

    def test_same_keys_different_values_is_not_reordered(self):
        result = diff_json(parse('{"a":1,"b":2}'), parse('{"a":2,"b":1}'),
                           DiffOptions(report_reordered=True))
        self.assertEqual(result.summary["reordered"], 0)
        self.assertEqual(result.summary["changed"], 2)

    def test_array_order_is_not_key_order(self):
        # 数组顺序属于数据（index 模式），不能被 ignore_key_order 吃掉
        result = diff_json(parse('{"a":[1,2]}'), parse('{"a":[2,1]}'))
        self.assertTrue(result.changed)
        self.assertEqual(kinds(result), [("changed", "$.a[0]"), ("changed", "$.a[1]")])
        self.assertEqual(result.summary["reordered"], 0)

    def test_identical_documents_report_no_reordered(self):
        result = diff_json(parse(self.LEFT), parse(self.LEFT),
                           DiffOptions(report_reordered=True, ignore_key_order=False))
        self.assertFalse(result.changed)
        self.assertEqual(result.items, [])


# --------------------------------------------------------------------------- #
# 嵌套结构
# --------------------------------------------------------------------------- #
class TestNestedStructures(unittest.TestCase):
    def test_deep_nested_change_path_and_depth(self):
        left = parse('{"a":{"b":{"c":[1,{"d":2}]}}}')
        right = parse('{"a":{"b":{"c":[1,{"d":3}]}}}')
        result = diff_json(left, right)
        self.assertEqual(kinds(result), [("changed", "$.a.b.c[1].d")])
        item = result.items[0]
        self.assertEqual(item.path, ("a", "b", "c", 1, "d"))
        self.assertEqual(item.depth, 5)
        self.assertEqual((item.left, item.right), (2, 3))

    def test_added_subtree_carries_whole_value(self):
        result = diff_json(parse('{"a":1}'), parse('{"a":1,"b":{"c":[1,2]}}'))
        self.assertEqual(kinds(result), [("added", "$.b")])
        self.assertEqual(result.items[0].right, {"c": [1, 2]})
        self.assertEqual(result.items[0].right_type, "object")
        # 子树内部不再产出条目
        self.assertEqual(len(result.items), 1)

    def test_removed_subtree_carries_whole_value(self):
        result = diff_json(parse('{"a":{"deep":[1,{"x":null}]}}'), parse('{}'))
        self.assertEqual(kinds(result), [("removed", "$.a")])
        self.assertEqual(result.items[0].left, {"deep": [1, {"x": None}]})
        self.assertEqual(len(result.items), 1)

    def test_mixed_object_array_changes(self):
        left = parse('{"users":[{"name":"a","tags":["x","y"]},{"name":"b","tags":[]}]}')
        right = parse('{"users":[{"name":"a","tags":["x","z"]},{"name":"c","tags":[]}]}')
        result = diff_json(left, right)
        self.assertEqual(kinds(result), [("changed", "$.users[0].tags[1]"),
                                         ("changed", "$.users[1].name")])

    def test_document_order_is_preorder(self):
        left = parse('{"a":{"x":1},"b":1,"c":1}')
        right = parse('{"a":{"x":2},"b":2,"c":1,"d":3}')
        result = diff_json(left, right)
        self.assertEqual([item.path_string for item in result.items],
                         ["$.a.x", "$.b", "$.d"])

    def test_removed_before_nested_children_of_same_object(self):
        left = parse('{"gone":1,"keep":{"inner":1}}')
        right = parse('{"keep":{"inner":2}}')
        result = diff_json(left, right)
        self.assertEqual([item.path_string for item in result.items],
                         ["$.gone", "$.keep.inner"])

    def test_root_scalar_documents(self):
        result = diff_json(parse('"a"'), parse('"b"'))
        self.assertEqual(kinds(result), [("changed", "$")])
        self.assertEqual(result.items[0].path, ())
        self.assertEqual(result.items[0].depth, 0)

    def test_root_type_change(self):
        result = diff_json(parse('{}'), parse('[]'))
        self.assertEqual(kinds(result), [("type_changed", "$")])
        self.assertEqual(result.items[0].left_type, "object")
        self.assertEqual(result.items[0].right_type, "array")


# --------------------------------------------------------------------------- #
# 数组模式
# --------------------------------------------------------------------------- #
class TestArrayModeIndex(unittest.TestCase):
    def test_append_and_truncate(self):
        appended = diff_json(parse('[1,2]'), parse('[1,2,3]'))
        self.assertEqual(kinds(appended), [("added", "$[2]")])
        truncated = diff_json(parse('[1,2,3]'), parse('[1,2]'))
        self.assertEqual(kinds(truncated), [("removed", "$[2]")])

    def test_middle_insertion_shifts_everything(self):
        left = parse('{"u":[{"id":1,"n":"a"},{"id":2,"n":"b"}]}')
        right = parse('{"u":[{"id":1,"n":"a"},{"id":3,"n":"c"},{"id":2,"n":"b"}]}')
        result = diff_json(left, right)
        # index 模式的固有语义：插入点之后的元素全部错位
        self.assertEqual(kinds(result), [("changed", "$.u[1].id"), ("changed", "$.u[1].n"),
                                         ("added", "$.u[2]")])
        self.assertEqual(result.items[0].left, 2)
        self.assertEqual(result.items[0].right, 3)

    def test_nested_arrays(self):
        result = diff_json(parse('[[1,[2,3]]]'), parse('[[1,[2,4]]]'))
        self.assertEqual(kinds(result), [("changed", "$[0][1][1]")])

    def test_default_mode_constant(self):
        self.assertEqual(DiffOptions().array_mode, ARRAY_INDEX)
        result = diff_json(parse('[1]'), parse('[2]'), DiffOptions(array_mode="nonsense"))
        self.assertEqual(kinds(result), [("changed", "$[0]")])
        self.assertTrue(any(w.startswith("bad_array_mode") for w in result.warnings))


class TestArrayModeSet(unittest.TestCase):
    def test_pure_reorder_reports_single_reordered_item(self):
        result = diff_json(parse('[1,2,3]'), parse('[3,2,1]'), DiffOptions(array_mode=ARRAY_SET))
        self.assertEqual(kinds(result), [("reordered", "$")])
        item = result.items[0]
        self.assertEqual(item.left, [1, 2, 3])
        self.assertEqual(item.right, [3, 2, 1])
        self.assertEqual(item.left_type, "array")
        self.assertEqual(item.right_type, "array")
        self.assertIn("set mode", item.detail)

    def test_same_multiset_of_objects_reports_nothing_but_reorder(self):
        left = parse('[{"id":1},{"id":2}]')
        right = parse('[{"id":2},{"id":1}]')
        silent = diff_json(left, right, DiffOptions(array_mode=ARRAY_SET))
        self.assertEqual(silent.summary["reordered"], 1)
        self.assertEqual(silent.summary["changed"], 0)
        self.assertEqual(silent.summary["added"], 0)
        self.assertEqual(silent.summary["removed"], 0)

    def test_membership_difference_reports_removed_and_added(self):
        result = diff_json(parse('[1,2,3]'), parse('[1,3]'), DiffOptions(array_mode=ARRAY_SET))
        self.assertEqual(kinds(result), [("removed", "$[1]")])
        self.assertEqual(result.items[0].left, 2)
        result = diff_json(parse('[1,3]'), parse('[1,2,3]'), DiffOptions(array_mode=ARRAY_SET))
        self.assertEqual(kinds(result), [("added", "$[1]")])
        self.assertEqual(result.items[0].right, 2)

    def test_no_child_items_for_reordered_elements(self):
        left = parse('{"a":[{"x":1},{"x":2}]}')
        right = parse('{"a":[{"x":2},{"x":1}]}')
        result = diff_json(left, right, DiffOptions(array_mode=ARRAY_SET))
        self.assertEqual(kinds(result), [("reordered", "$.a")])
        self.assertEqual(result.summary["changed"], 0)

    def test_duplicate_elements_are_a_multiset(self):
        result = diff_json(parse('[1,1,2]'), parse('[1,2,2]'), DiffOptions(array_mode=ARRAY_SET))
        self.assertEqual(kinds(result), [("removed", "$[1]"), ("added", "$[2]")])

    def test_identical_arrays_report_nothing(self):
        result = diff_json(parse('[1,2,3]'), parse('[1,2,3]'), DiffOptions(array_mode=ARRAY_SET))
        self.assertFalse(result.changed)
        self.assertEqual(result.items, [])

    def test_element_content_change_shows_as_removed_plus_added(self):
        result = diff_json(parse('[{"id":1,"v":1}]'), parse('[{"id":1,"v":2}]'),
                           DiffOptions(array_mode=ARRAY_SET))
        self.assertEqual(sorted(item.kind for item in result.items), [KIND_ADDED, KIND_REMOVED])
        self.assertEqual(result.summary["changed"], 0)

    def test_set_mode_respects_string_options(self):
        result = diff_json(parse('["a b"]'), parse('["a  b"]'),
                           DiffOptions(array_mode=ARRAY_SET, ignore_whitespace=True))
        self.assertFalse(result.changed)
        strict = diff_json(parse('["a b"]'), parse('["a  b"]'),
                           DiffOptions(array_mode=ARRAY_SET, ignore_whitespace=False))
        self.assertEqual(strict.summary["removed"], 1)
        self.assertEqual(strict.summary["added"], 1)

    def test_set_mode_respects_number_format_option(self):
        result = diff_json(parse('[1.0,2]'), parse('[2,1]'),
                           DiffOptions(array_mode=ARRAY_SET, ignore_number_format=True))
        self.assertEqual(kinds(result), [("reordered", "$")])


class TestArrayModeKey(unittest.TestCase):
    LEFT = '{"u":[{"id":1,"n":"a"},{"id":2,"n":"b"}]}'

    def test_middle_insertion_is_a_single_added_item(self):
        right = parse('{"u":[{"id":1,"n":"a"},{"id":3,"n":"c"},{"id":2,"n":"b"}]}')
        result = diff_json(parse(self.LEFT), right, DiffOptions(array_mode=ARRAY_KEY))
        self.assertEqual(kinds(result), [("added", "$.u[1]")])
        self.assertEqual(result.items[0].right, {"id": 3, "n": "c"})
        self.assertEqual(result.items[0].right_type, "object")

    def test_reorder_reports_nothing(self):
        right = parse('{"u":[{"id":2,"n":"b"},{"id":1,"n":"a"}]}')
        result = diff_json(parse(self.LEFT), right, DiffOptions(array_mode=ARRAY_KEY))
        self.assertFalse(result.changed)
        self.assertEqual(result.items, [])
        # unchanged 统计**节点**：两个 {"id","n"} 元素各 3 个节点
        self.assertEqual(result.summary["unchanged"], 6)

    def test_removal_uses_left_index(self):
        right = parse('{"u":[{"id":1,"n":"a"}]}')
        result = diff_json(parse(self.LEFT), right, DiffOptions(array_mode=ARRAY_KEY))
        self.assertEqual(kinds(result), [("removed", "$.u[1]")])
        self.assertEqual(result.items[0].left, {"id": 2, "n": "b"})

    def test_paired_element_change_descends_into_fields(self):
        right = parse('{"u":[{"id":1,"n":"a"},{"id":2,"n":"CHANGED"}]}')
        result = diff_json(parse(self.LEFT), right, DiffOptions(array_mode=ARRAY_KEY))
        self.assertEqual(kinds(result), [("changed", "$.u[1].n")])
        self.assertEqual(result.items[0].left, "b")
        self.assertEqual(result.items[0].right, "CHANGED")

    def test_scalar_elements_pair_by_key_and_report_changed(self):
        result = diff_json(parse('[1,2,3]'), parse('[1,3,2]'), DiffOptions(array_mode=ARRAY_KEY))
        self.assertEqual(kinds(result), [("changed", "$[1]"), ("changed", "$[2]")])

    def test_custom_key_field(self):
        left = parse('[{"k":"x","v":1},{"k":"y","v":2}]')
        right = parse('[{"k":"y","v":2},{"k":"x","v":9}]')
        result = diff_json(left, right, DiffOptions(array_mode=ARRAY_KEY, array_key_field="k"))
        # 路径一律用**左侧**下标：k="x" 在左侧是 0（右侧是 1）
        self.assertEqual(kinds(result), [("changed", "$[0].v")])
        self.assertEqual(result.items[0].left, 1)
        self.assertEqual(result.items[0].right, 9)

    def test_missing_key_field_falls_back_to_positional_pairing(self):
        left = parse('[{"id":1,"v":"a"},{"v":"b"},{"id":3,"v":"c"}]')
        right = parse('[{"id":1,"v":"a"},{"v":"B"},{"id":3,"v":"c"}]')
        result = diff_json(left, right, DiffOptions(array_mode=ARRAY_KEY))
        self.assertEqual(kinds(result), [("changed", "$[1].v")])

    def test_duplicate_key_values_pair_in_document_order(self):
        left = parse('[{"id":1,"v":"a"},{"id":1,"v":"b"},{"x":1}]')
        right = parse('[{"id":1,"v":"a"},{"id":1,"v":"B"},{"x":2}]')
        result = diff_json(left, right, DiffOptions(array_mode=ARRAY_KEY))
        self.assertEqual(kinds(result), [("changed", "$[1].v"), ("changed", "$[2].x")])

    def test_unmatched_on_both_sides(self):
        left = parse('[{"id":1},{"id":2},{"id":3}]')
        right = parse('[{"id":3},{"id":1},{"id":9}]')
        result = diff_json(left, right, DiffOptions(array_mode=ARRAY_KEY))
        self.assertEqual(kinds(result), [("removed", "$[1]"), ("added", "$[2]")])
        self.assertEqual(result.items[0].left, {"id": 2})
        self.assertEqual(result.items[1].right, {"id": 9})

    def test_key_mode_type_change_inside_paired_element(self):
        left = parse('[{"id":1,"v":{"a":1}}]')
        right = parse('[{"id":1,"v":[1]}]')
        result = diff_json(left, right, DiffOptions(array_mode=ARRAY_KEY))
        self.assertEqual(kinds(result), [("type_changed", "$[0].v")])

    def test_bad_key_field_falls_back_with_warning(self):
        result = diff_json(parse('[{"id":1}]'), parse('[{"id":2}]'),
                           DiffOptions(array_mode=ARRAY_KEY, array_key_field=""))
        self.assertTrue(result.changed)
        self.assertTrue(any(w.startswith("bad_array_key_field") for w in result.warnings))


# --------------------------------------------------------------------------- #
# ignore_fields
# --------------------------------------------------------------------------- #
class TestIgnoreFields(unittest.TestCase):
    LEFT = '{"id":1,"name":"a","created_at":"x","meta":{"a":1,"b":2},"items":[{"id":7,"v":1}],"keep":1}'
    RIGHT = '{"id":2,"name":"b","created_at":"y","meta":{"a":9,"b":9},"items":[{"id":8,"v":1}],"keep":1}'

    def test_no_ignore_reports_everything(self):
        result = diff_json(parse(self.LEFT), parse(self.RIGHT))
        self.assertEqual([item.path_string for item in result.items],
                         ["$.id", "$.name", "$.created_at", "$.meta.a", "$.meta.b", "$.items[0].id"])

    def test_exact_field_name_at_any_depth(self):
        result = diff_json(parse(self.LEFT), parse(self.RIGHT), DiffOptions(ignore_fields=("id",)))
        paths = [item.path_string for item in result.items]
        self.assertNotIn("$.id", paths)
        self.assertNotIn("$.items[0].id", paths)
        self.assertIn("$.name", paths)
        self.assertEqual(result.summary["changed"], 4)

    def test_wildcard_field_name(self):
        result = diff_json(parse(self.LEFT), parse(self.RIGHT),
                           DiffOptions(ignore_fields=("*_at",)))
        paths = [item.path_string for item in result.items]
        self.assertNotIn("$.created_at", paths)
        self.assertIn("$.id", paths)
        self.assertIn("$.meta.a", paths)

    def test_wildcard_question_mark_and_prefix(self):
        left = parse('{"a1":1,"a2":1,"b1":1}')
        right = parse('{"a1":2,"a2":2,"b1":2}')
        result = diff_json(left, right, DiffOptions(ignore_fields=("a?",)))
        self.assertEqual([item.path_string for item in result.items], ["$.b1"])
        result = diff_json(left, right, DiffOptions(ignore_fields=("a*",)))
        self.assertEqual([item.path_string for item in result.items], ["$.b1"])

    def test_path_pattern_with_wildcard_covers_subtree(self):
        result = diff_json(parse(self.LEFT), parse(self.RIGHT),
                           DiffOptions(ignore_fields=("$.meta.*",)))
        paths = [item.path_string for item in result.items]
        self.assertNotIn("$.meta.a", paths)
        self.assertNotIn("$.meta.b", paths)
        self.assertIn("$.id", paths)

    def test_path_pattern_exact_does_not_cover_descendants(self):
        left = parse('{"a":{"b":{"c":1}}}')
        right = parse('{"a":{"b":{"c":2}}}')
        result = diff_json(left, right, DiffOptions(ignore_fields=("$.a.b",)))
        self.assertEqual(result.items, [])
        result = diff_json(left, right, DiffOptions(ignore_fields=("$.a.b.c",)))
        self.assertEqual(result.items, [])
        result = diff_json(left, right, DiffOptions(ignore_fields=("$.a.b*",)))
        self.assertEqual(result.items, [])
        result = diff_json(left, right, DiffOptions(ignore_fields=("$.a.x",)))
        self.assertEqual(kinds(result), [("changed", "$.a.b.c")])

    def test_path_pattern_with_array_index_wildcard(self):
        result = diff_json(parse(self.LEFT), parse(self.RIGHT),
                           DiffOptions(ignore_fields=("$.items[*].id",)))
        self.assertEqual([item.path_string for item in result.items],
                         ["$.id", "$.name", "$.created_at", "$.meta.a", "$.meta.b"])

    def test_path_pattern_with_explicit_index(self):
        left = parse('{"items":[{"id":1},{"id":2}]}')
        right = parse('{"items":[{"id":9},{"id":9}]}')
        result = diff_json(left, right, DiffOptions(ignore_fields=("$.items[0].id",)))
        self.assertEqual(kinds(result), [("changed", "$.items[1].id")])

    def test_ignored_field_also_suppresses_added_and_removed(self):
        result = diff_json(parse('{"id":1,"a":1}'), parse('{"a":1}'),
                           DiffOptions(ignore_fields=("id",)))
        self.assertFalse(result.changed)
        self.assertEqual(result.items, [])
        result = diff_json(parse('{"a":1}'), parse('{"id":1,"a":1}'),
                           DiffOptions(ignore_fields=("id",)))
        self.assertFalse(result.changed)

    def test_ignore_everything_below_root(self):
        result = diff_json(parse(self.LEFT), parse(self.RIGHT), DiffOptions(ignore_fields=("*",)))
        self.assertFalse(result.changed)
        self.assertEqual(result.items, [])

    def test_ignore_fields_accepts_a_list(self):
        result = diff_json(parse(self.LEFT), parse(self.RIGHT),
                           DiffOptions(ignore_fields=["id", "*_at"]))
        paths = [item.path_string for item in result.items]
        self.assertNotIn("$.id", paths)
        self.assertNotIn("$.created_at", paths)

    def test_ignored_fields_do_not_break_reordered_detection(self):
        left = parse('{"a":1,"b":2,"skip":1}')
        right = parse('{"b":2,"skip":9,"a":1}')
        result = diff_json(left, right, DiffOptions(ignore_fields=("skip",), report_reordered=True))
        self.assertEqual(kinds(result), [("reordered", "$")])


class TestFieldIgnoredFunction(unittest.TestCase):
    OPTIONS = DiffOptions(ignore_fields=("id", "*_at", "$.meta.*", "$.a.b", "$.items[*].id", "$.x[2]"))

    def test_exact_name(self):
        self.assertTrue(field_ignored(("id",), self.OPTIONS))
        self.assertTrue(field_ignored(("deep", "deeper", "id"), self.OPTIONS))
        self.assertFalse(field_ignored(("identifier",), self.OPTIONS))

    def test_wildcard(self):
        self.assertTrue(field_ignored(("created_at",), self.OPTIONS))
        self.assertTrue(field_ignored(("a", "updated_at"), self.OPTIONS))
        self.assertFalse(field_ignored(("at",), self.OPTIONS))

    def test_path_patterns(self):
        self.assertTrue(field_ignored(("meta", "anything"), self.OPTIONS))
        self.assertTrue(field_ignored(("meta", "deep", "deeper"), self.OPTIONS))
        self.assertFalse(field_ignored(("meta",), self.OPTIONS))
        self.assertTrue(field_ignored(("a", "b"), self.OPTIONS))
        self.assertFalse(field_ignored(("a", "b", "c"), self.OPTIONS))
        self.assertTrue(field_ignored(("items", 0, "id"), self.OPTIONS))
        self.assertTrue(field_ignored(("items", 42, "id"), self.OPTIONS))
        self.assertFalse(field_ignored(("items", 0, "value"), self.OPTIONS))
        self.assertTrue(field_ignored(("x", 2), self.OPTIONS))
        self.assertFalse(field_ignored(("x", 3), self.OPTIONS))

    def test_root_is_never_ignored(self):
        self.assertFalse(field_ignored((), self.OPTIONS))
        self.assertFalse(field_ignored((), DiffOptions(ignore_fields=("*",))))
        self.assertFalse(field_ignored((), DiffOptions(ignore_fields=("$*",))))

    def test_array_index_is_not_a_field_name(self):
        self.assertFalse(field_ignored(("a", 0), self.OPTIONS))

    def test_robust_against_bad_input(self):
        self.assertFalse(field_ignored(("id",), None))
        self.assertFalse(field_ignored(("id",), object()))
        self.assertFalse(field_ignored(None, self.OPTIONS))
        self.assertFalse(field_ignored("id", self.OPTIONS))
        self.assertFalse(field_ignored(("id",), DiffOptions()))
        self.assertTrue(field_ignored(("id",), {"ignore_fields": ["id"]}))
        self.assertTrue(field_ignored(("id",), DiffOptions(ignore_fields="id")))
        self.assertFalse(field_ignored(("a",), DiffOptions(ignore_fields=None)))

    def test_weird_keys_use_bracket_path_text(self):
        options = DiffOptions(ignore_fields=("$['a.b']",))
        self.assertTrue(field_ignored(("a.b",), options))
        self.assertEqual(path_string(("a.b",)), "$['a.b']")
        self.assertEqual(path_string(("it's",)), "$['it\\'s']")
        self.assertEqual(path_string(("a", 0, "b")), "$.a[0].b")
        self.assertEqual(path_string(()), "$")


# --------------------------------------------------------------------------- #
# 字符串与数字选项
# --------------------------------------------------------------------------- #
class TestStringOptions(unittest.TestCase):
    def test_ignore_whitespace_is_on_by_default(self):
        self.assertTrue(DiffOptions().ignore_whitespace)
        result = diff_json(parse('{"s":"  padded  "}'), parse('{"s":"padded"}'))
        self.assertFalse(result.changed)

    def test_ignore_whitespace_collapses_inner_runs(self):
        result = diff_json(parse('{"s":"a b"}'), parse('{"s":"a     b"}'))
        self.assertFalse(result.changed)
        tab_left = parse('{"s":"a' + chr(92) + 'tb"}')
        self.assertEqual(tab_left["s"], "a\tb")
        self.assertFalse(diff_json(tab_left, parse('{"s":"a b"}')).changed)
        newline = parse('{"s":"a' + chr(92) + 'nb"}')
        self.assertFalse(diff_json(newline, parse('{"s":"a b"}')).changed)

    def test_ignore_whitespace_off_reports_the_difference(self):
        result = diff_json(parse('{"s":"  x  "}'), parse('{"s":"x"}'),
                           DiffOptions(ignore_whitespace=False))
        self.assertEqual(kinds(result), [("changed", "$.s")])
        self.assertEqual(result.items[0].left, "  x  ")
        self.assertEqual(result.items[0].right, "x")

    def test_ignore_string_case(self):
        self.assertFalse(DiffOptions().ignore_string_case)
        sensitive = diff_json(parse('{"s":"ABC"}'), parse('{"s":"abc"}'))
        self.assertEqual(kinds(sensitive), [("changed", "$.s")])
        insensitive = diff_json(parse('{"s":"ABC"}'), parse('{"s":"abc"}'),
                                DiffOptions(ignore_string_case=True))
        self.assertFalse(insensitive.changed)

    def test_whitespace_and_case_together(self):
        result = diff_json(parse('{"s":"  Hello   World "}'), parse('{"s":"hello world"}'),
                           DiffOptions(ignore_string_case=True))
        self.assertFalse(result.changed)
        result = diff_json(parse('{"s":"  Hello   World "}'), parse('{"s":"hello world"}'),
                           DiffOptions(ignore_string_case=True, ignore_whitespace=False))
        self.assertTrue(result.changed)

    def test_real_string_change_is_still_reported(self):
        result = diff_json(parse('{"s":"abc"}'), parse('{"s":"abd"}'),
                           DiffOptions(ignore_whitespace=True, ignore_string_case=True))
        self.assertEqual(kinds(result), [("changed", "$.s")])

    def test_string_options_apply_inside_arrays_and_keys_of_objects(self):
        left = parse('{"list":["A","B"],"o":{"k":"X"}}')
        right = parse('{"list":["a","b"],"o":{"k":"x"}}')
        self.assertFalse(diff_json(left, right, DiffOptions(ignore_string_case=True)).changed)
        self.assertEqual(diff_json(left, right).summary["changed"], 3)


class TestNumberOptions(unittest.TestCase):
    def test_literal_comparison_by_default(self):
        self.assertFalse(DiffOptions().ignore_number_format)
        for left, right in [("1.0", "1"), ("1.230", "1.23"), ("1e2", "100"), ("0.0", "0")]:
            with self.subTest(left=left, right=right):
                result = diff_json(parse('{"n":%s}' % left), parse('{"n":%s}' % right))
                self.assertEqual(kinds(result), [("changed", "$.n")])

    def test_ignore_number_format_compares_numerically(self):
        for left, right in [("1.0", "1"), ("1.230", "1.23"), ("1e2", "100"), ("0.0", "0"),
                            ("1E+5", "100000"), ("-0", "0")]:
            with self.subTest(left=left, right=right):
                result = diff_json(parse('{"n":%s}' % left), parse('{"n":%s}' % right),
                                   DiffOptions(ignore_number_format=True))
                self.assertFalse(result.changed)
                self.assertEqual(result.items, [])

    def test_ignore_number_format_still_sees_real_changes(self):
        result = diff_json(parse('{"n":1.0}'), parse('{"n":1.5}'),
                           DiffOptions(ignore_number_format=True))
        self.assertEqual(kinds(result), [("changed", "$.n")])

    def test_huge_integers_stay_exact(self):
        big_left = "123456789012345678901234567890"
        big_right = "123456789012345678901234567891"
        result = diff_json(parse('{"n":%s}' % big_left), parse('{"n":%s}' % big_right))
        item = result.items[0]
        self.assertEqual(item.kind, KIND_CHANGED)
        self.assertIsInstance(item.left, int)
        self.assertNotIsInstance(item.left, Decimal)
        self.assertEqual(item.left, int(big_left))
        self.assertEqual(item.right - item.left, 1)
        self.assertEqual(item.right, int(big_right))
        # 相同的大整数不会被 float 化后误判为相等/不等
        same = diff_json(parse('{"n":%s}' % big_left), parse('{"n":%s}' % big_left))
        self.assertFalse(same.changed)

    def test_huge_integer_vs_decimal_literal(self):
        result = diff_json(parse('{"n":1000000000000000000000000000000}'),
                           parse('{"n":1e30}'), DiffOptions(ignore_number_format=True))
        self.assertFalse(result.changed)
        strict = diff_json(parse('{"n":1000000000000000000000000000000}'), parse('{"n":1e30}'))
        self.assertTrue(strict.changed)

    def test_json_number_values_keep_their_literal(self):
        result = diff_json(parse('{"n":1.230}'), parse('{"n":1.231}'))
        item = result.items[0]
        self.assertIsInstance(item.left, JsonNumber)
        self.assertIsInstance(item.right, JsonNumber)
        self.assertEqual(item.left.raw, "1.230")
        self.assertEqual(item.right.raw, "1.231")
        self.assertEqual(item.left_type, "number")

    def test_python_floats_and_ints_are_numbers(self):
        result = diff_json({"n": 1.0}, {"n": 1})
        self.assertEqual(kinds(result), [("changed", "$.n")])
        self.assertFalse(diff_json({"n": 1.0}, {"n": 1},
                                   DiffOptions(ignore_number_format=True)).changed)

    def test_boolean_is_not_a_number(self):
        result = diff_json(parse('{"n":1}'), parse('{"n":true}'))
        self.assertEqual(kinds(result), [("type_changed", "$.n")])
        result = diff_json(parse('{"n":0}'), parse('{"n":false}'),
                           DiffOptions(ignore_number_format=True))
        self.assertEqual(kinds(result), [("type_changed", "$.n")])


# --------------------------------------------------------------------------- #
# Unicode
# --------------------------------------------------------------------------- #
class TestUnicode(unittest.TestCase):
    def test_chinese_keys_and_emoji_values(self):
        left = parse('{"中文":{"键":"值😀"},"列表":["甲","乙"]}')
        right = parse('{"中文":{"键":"值😁"},"列表":["甲","丙"]}')
        result = diff_json(left, right)
        # 非标识符形式的键一律用括号写法（与 scanners/statistics 的约定一致）
        self.assertEqual([item.path_string for item in result.items],
                         ["$['中文']['键']", "$['列表'][1]"])
        self.assertEqual(result.items[0].path, ("中文", "键"))
        self.assertEqual(result.items[0].left, "值😀")
        self.assertEqual(result.items[0].right, "值😁")
        self.assertEqual(result.items[1].path, ("列表", 1))

    def test_ascii_identifier_keys_use_the_dotted_form(self):
        result = diff_json(parse('{"users":[{"name":"a"}]}'), parse('{"users":[{"name":"b"}]}'))
        self.assertEqual(result.items[0].path_string, "$.users[0].name")

    def test_unicode_added_and_removed_keys(self):
        result = diff_json(parse('{"旧":1}'), parse('{"新":2}'))
        self.assertEqual(kinds(result), [("removed", "$['旧']"), ("added", "$['新']")])
        self.assertEqual(result.items[1].right, 2)

    def test_unicode_key_order(self):
        left = parse('{"甲":1,"乙":2}')
        right = parse('{"乙":2,"甲":1}')
        self.assertFalse(diff_json(left, right).changed)
        loud = diff_json(left, right, DiffOptions(report_reordered=True))
        self.assertEqual(loud.items[0].left, ("甲", "乙"))
        self.assertEqual(loud.items[0].right, ("乙", "甲"))

    def test_ignore_fields_with_unicode_names(self):
        left = parse('{"编号":1,"名称":"a"}')
        right = parse('{"编号":2,"名称":"b"}')
        result = diff_json(left, right, DiffOptions(ignore_fields=("编号",)))
        self.assertEqual([item.path_string for item in result.items], ["$['名称']"])
        result = diff_json(left, right, DiffOptions(ignore_fields=("$['名称']",)))
        self.assertEqual([item.path_string for item in result.items], ["$['编号']"])

    def test_emoji_only_difference(self):
        result = diff_json(parse('{"e":"👍"}'), parse('{"e":"👎"}'))
        self.assertEqual(kinds(result), [("changed", "$.e")])


# --------------------------------------------------------------------------- #
# 空文档 / 相同文档
# --------------------------------------------------------------------------- #
class TestEmptyAndIdentical(unittest.TestCase):
    IDENTICAL = [
        "{}", "[]", "null", "true", "false", "0", '"x"',
        '{"a":1}', "[1,2,3]", '{"a":[1,2,{"b":null}]}',
        '{"nested":{"deep":{"deeper":{"deepest":[]}}}}',
        '{"unicode":"中文😀","numbers":[1.230,1e5,-0,123456789012345678901234567890]}',
        '[{"id":1,"tags":["a","b"]},{"id":2,"tags":[]}]',
    ]

    def test_identical_documents_have_no_items(self):
        for text in self.IDENTICAL:
            with self.subTest(text=text):
                result = diff_json(parse(text), parse(text))
                self.assertFalse(result.changed)
                self.assertEqual(result.items, [])
                self.assertEqual(result.error, "")
                self.assertEqual(result.summary["total"], 0)
                self.assertFalse(result.truncated)

    def test_identical_with_every_option_enabled(self):
        options = DiffOptions(ignore_key_order=False, report_reordered=True,
                              ignore_whitespace=False, ignore_string_case=True,
                              ignore_number_format=True, array_mode=ARRAY_SET)
        for text in self.IDENTICAL:
            with self.subTest(text=text):
                result = diff_json(parse(text), parse(text), options)
                self.assertFalse(result.changed)
                self.assertEqual(result.items, [])

    def test_empty_containers_of_different_type(self):
        result = diff_json(parse('{}'), parse('[]'))
        self.assertTrue(result.changed)
        self.assertEqual(kinds(result), [("type_changed", "$")])
        self.assertFalse(diff_json(parse('{}'), parse('{}')).changed)
        self.assertFalse(diff_json(parse('[]'), parse('[]')).changed)

    def test_empty_versus_non_empty(self):
        self.assertEqual(kinds(diff_json(parse('{}'), parse('{"a":1}'))), [("added", "$.a")])
        self.assertEqual(kinds(diff_json(parse('{"a":1}'), parse('{}'))), [("removed", "$.a")])
        self.assertEqual(kinds(diff_json(parse('[]'), parse('[1]'))), [("added", "$[0]")])
        self.assertEqual(kinds(diff_json(parse('[1]'), parse('[]'))), [("removed", "$[0]")])

    def test_null_versus_missing_key(self):
        result = diff_json(parse('{"a":null}'), parse('{}'))
        self.assertEqual(kinds(result), [("removed", "$.a")])
        self.assertEqual(result.items[0].left_type, "null")

    def test_same_object_instance(self):
        value = parse('{"a":[1,2,3]}')
        result = diff_json(value, value)
        self.assertFalse(result.changed)
        self.assertEqual(result.items, [])


# --------------------------------------------------------------------------- #
# 截断与深度上限
# --------------------------------------------------------------------------- #
class TestTruncationAndDepth(unittest.TestCase):
    def test_max_items_truncates(self):
        left = parse('{"a":1,"b":2,"c":3,"d":4,"e":5}')
        right = parse('{"a":9,"b":9,"c":9,"d":9,"e":9}')
        result = diff_json(left, right, DiffOptions(max_items=2))
        self.assertTrue(result.truncated)
        self.assertTrue(result.changed)
        self.assertEqual(len(result.items), 2)
        self.assertEqual(result.summary["total"], 2)
        self.assertIs(result.summary["truncated"], True)
        self.assertEqual([item.path_string for item in result.items], ["$.a", "$.b"])

    def test_max_items_one(self):
        result = diff_json(parse('[1,2,3]'), parse('[9,9,9]'), DiffOptions(max_items=1))
        self.assertEqual(len(result.items), 1)
        self.assertTrue(result.truncated)

    def test_max_items_large_enough_is_not_truncated(self):
        result = diff_json(parse('[1,2,3]'), parse('[9,9,9]'), DiffOptions(max_items=100))
        self.assertEqual(len(result.items), 3)
        self.assertFalse(result.truncated)

    def test_bad_max_items_is_normalized(self):
        result = diff_json(parse('{"a":1}'), parse('{"a":2}'), DiffOptions(max_items=0))
        self.assertTrue(result.changed)
        self.assertTrue(any(w.startswith("bad_max_items") for w in result.warnings))
        result = diff_json(parse('{"a":1}'), parse('{"a":2}'), DiffOptions(max_items="nope"))
        self.assertTrue(result.changed)
        self.assertTrue(any(w.startswith("bad_max_items") for w in result.warnings))

    def test_nesting_at_the_limit_is_compared(self):
        depth = MAX_DEPTH
        left = parse('{"a":' * depth + "1" + "}" * depth)
        right = parse('{"a":' * depth + "2" + "}" * depth)
        result = diff_json(left, right)
        self.assertEqual(result.error, "")
        self.assertEqual(result.warnings, [])
        self.assertTrue(result.changed)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].kind, KIND_CHANGED)
        self.assertEqual(result.items[0].depth, depth)
        self.assertEqual(result.items[0].path, ("a",) * depth)

    def test_deep_nesting_does_not_raise_recursion_error(self):
        depth = 1200
        left = parse("[" * depth + "]" * depth)
        right = parse("[" * depth + "]" * depth)
        self.assertFalse(diff_json(left, right).changed)
        right_deep = parse("[" * (depth - 1) + "1" + "]" * (depth - 1))
        result = diff_json(left, right_deep)
        self.assertTrue(result.changed)
        self.assertEqual(result.error, "")

    def test_beyond_the_limit_reports_depth_exceeded(self):
        depth = MAX_DEPTH + 200
        left = parse('{"a":' * depth + "1" + "}" * depth)
        right = parse('{"a":' * depth + "2" + "}" * depth)
        result = diff_json(left, right)
        self.assertEqual(result.error, "")
        self.assertTrue(result.changed)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].detail, "depth_exceeded")
        self.assertTrue(any(w.startswith("depth_exceeded") for w in result.warnings))
        self.assertLess(len(result.warnings[0]), 300)

    def test_deep_object_chain_identical_is_fast_and_silent(self):
        depth = 1500
        text = '{"a":' * depth + "1" + "}" * depth
        result = diff_json(parse(text), parse(text))
        self.assertFalse(result.changed)
        self.assertEqual(result.warnings, [])


# --------------------------------------------------------------------------- #
# diff_text
# --------------------------------------------------------------------------- #
class TestDiffText(unittest.TestCase):
    def test_valid_both_sides(self):
        result = diff_text('{"a":1,"b":[1,2]}', '{"a":2,"b":[1,3]}')
        self.assertEqual(result.error, "")
        self.assertTrue(result.changed)
        self.assertEqual(kinds(result), [("changed", "$.a"), ("changed", "$.b[1]")])
        self.assertEqual(result.left_nodes, 5)
        self.assertEqual(result.right_nodes, 5)

    def test_whitespace_only_difference_is_not_a_change(self):
        result = diff_text('{ "a" : 1 , "b" : [ 1 , 2 ] }', '{"a":1,"b":[1,2]}')
        self.assertFalse(result.changed)
        self.assertEqual(result.items, [])
        self.assertEqual(result.error, "")

    def test_invalid_right_side(self):
        result = diff_text('{"a":1}', '{"a":1,}')
        self.assertFalse(result.changed)
        self.assertEqual(result.items, [])
        self.assertTrue(result.error.startswith("parse_error_right"))
        self.assertIn("trailing_comma", result.error)
        self.assertIn("line 1", result.error)

    def test_invalid_left_side(self):
        result = diff_text('{"a":}', '{"a":1}')
        self.assertFalse(result.changed)
        self.assertTrue(result.error.startswith("parse_error_left"))
        self.assertIn("left document is invalid", result.error)

    def test_both_sides_invalid_reports_the_left_one(self):
        result = diff_text("{oops", "[oops")
        self.assertFalse(result.changed)
        self.assertTrue(result.error.startswith("parse_error_left"))

    def test_empty_input_is_a_parse_error(self):
        result = diff_text("", '{"a":1}')
        self.assertFalse(result.changed)
        self.assertTrue(result.error.startswith("parse_error_left"))
        self.assertIn("empty_input", result.error)

    def test_none_input_does_not_raise(self):
        result = diff_text(None, '{"a":1}')
        self.assertIsInstance(result, DiffResult)
        self.assertFalse(result.changed)
        self.assertTrue(result.error.startswith("parse_error_left"))

    def test_jsonc_mode_allows_comments(self):
        result = diff_text('// note\n{"a":1}', '{"a":2}', mode="jsonc")
        self.assertEqual(result.error, "")
        self.assertEqual(kinds(result), [("changed", "$.a")])
        strict = diff_text('// note\n{"a":1}', '{"a":2}')
        self.assertTrue(strict.error.startswith("parse_error_left"))
        self.assertIn("comment_not_allowed", strict.error)

    def test_options_are_honoured(self):
        result = diff_text('{"b":1,"a":2}', '{"a":2,"b":1}',
                           DiffOptions(report_reordered=True))
        self.assertEqual(kinds(result), [("reordered", "$")])

    def test_bytes_input_is_decoded(self):
        result = diff_text('{"a":1}'.encode("utf-8"), '{"a":2}'.encode("utf-8"))
        self.assertEqual(result.error, "")
        self.assertEqual(kinds(result), [("changed", "$.a")])

    def test_deep_text_documents(self):
        depth = 1100
        left = '{"a":' * depth + "1" + "}" * depth
        right = '{"a":' * depth + "2" + "}" * depth
        result = diff_text(left, right)
        self.assertEqual(result.error, "")
        self.assertTrue(result.changed)


# --------------------------------------------------------------------------- #
# 报表生成器
# --------------------------------------------------------------------------- #
PAYLOAD = '{"a":1,"s":"<script>alert(\\"xss\\")</script>","b":{"c":[1,2]},"q":"a|b"}'
PAYLOAD_RIGHT = '{"a":2,"s":"safe","b":{"c":[1,3]},"q":"a|c","new":null}'


class TestReports(unittest.TestCase):
    def setUp(self):
        self.result = diff_json(parse(PAYLOAD), parse(PAYLOAD_RIGHT))
        self.assertTrue(self.result.changed)

    def test_markdown_is_well_formed(self):
        text = diff_to_markdown(self.result)
        self.assertIsInstance(text, str)
        self.assertTrue(text.startswith("# JSON Diff Report"))
        self.assertIn("| # | Path | Kind | Left | Right |", text)
        self.assertIn("| ---: | --- | --- | --- | --- |", text)
        self.assertIn("`$.a`", text)
        self.assertIn("changed", text)
        self.assertIn("added", text)
        self.assertIn("**CHANGED**", text)
        self.assertEqual(len([line for line in text.splitlines() if line.startswith("| ")]),
                         len(self.result.items) + 2)
        # 表格里的竖线必须被转义，否则表格会散架
        self.assertIn("a\\|b", text)
        self.assertTrue(text.endswith("\n") or text.splitlines())

    def test_markdown_escapes_script_payload_as_text(self):
        text = diff_to_markdown(self.result)
        self.assertIn("<script>", text)  # Markdown 里是纯文本，不需要 HTML 转义
        self.assertIn("alert(", text)

    def test_markdown_for_identical_documents(self):
        text = diff_to_markdown(diff_json(parse('{"a":1}'), parse('{"a":1}')))
        self.assertTrue(text)
        self.assertIn("**IDENTICAL**", text)
        self.assertIn("No structural difference", text)
        self.assertNotIn("| 1 |", text)

    def test_markdown_row_limit(self):
        result = diff_json(parse('{"a":1,"b":2,"c":3}'), parse('{"a":9,"b":9,"c":9}'))
        text = diff_to_markdown(result, max_rows=2)
        self.assertIn("`$.a`", text)
        self.assertIn("`$.b`", text)
        self.assertNotIn("`$.c`", text)
        self.assertIn("1 more difference(s)", text)

    def test_html_is_self_contained_and_escaped(self):
        text = diff_to_html(self.result)
        self.assertTrue(text.startswith("<!DOCTYPE html>"))
        self.assertIn('<meta charset="utf-8">', text)
        self.assertIn("<style>", text)
        self.assertIn("</style>", text)
        self.assertIn("</html>", text)
        # 自包含：没有脚本、没有外链、没有外部资源引用
        self.assertNotIn("<script", text)
        self.assertNotIn("<link", text)
        self.assertNotIn('src="', text)
        self.assertNotIn('href="', text)
        self.assertNotIn("<iframe", text)
        # 载荷被完整转义：只能看到转义后的形式，看不到可执行的形式
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("&lt;/script&gt;", text)
        self.assertIn("alert(\\&quot;xss\\&quot;)", text)
        self.assertIn("$.a", text)
        self.assertIn("kind-changed", text)
        self.assertIn("kind-added", text)
        self.assertIn("<table>", text)

    def test_html_escapes_angle_brackets_in_every_cell(self):
        result = diff_json(parse('{"k":"<img src=x onerror=alert(1)>"}'), parse('{"k":"<b>"}'))
        text = diff_to_html(result)
        self.assertNotIn("<img", text)
        self.assertNotIn("<b>", text)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", text)
        self.assertIn("&lt;b&gt;", text)

    def test_html_for_identical_and_error_results(self):
        same = diff_to_html(diff_json(parse('{"a":1}'), parse('{"a":1}')))
        self.assertIn("IDENTICAL", same)
        self.assertIn("identical under the selected options", same)
        failed = diff_to_html(diff_text('{"a":1}', "{bad"))
        self.assertIn("Error:", failed)
        self.assertIn("parse_error_right", failed)
        self.assertIn("could not be compared", failed)

    def test_json_text_report_reparses(self):
        text = diff_to_json_text(self.result)
        parsed = parse_json(text)
        self.assertTrue(parsed.ok, parsed.error)
        report = parsed.value
        self.assertEqual(report["report"], "json-diff")
        self.assertEqual(report["version"], 1)
        self.assertIs(report["changed"], True)
        self.assertEqual(report["error"], "")
        self.assertEqual(len(report["items"]), len(self.result.items))
        first = report["items"][0]
        self.assertEqual(first["path"], ["a"])
        self.assertEqual(first["path_string"], "$.a")
        self.assertEqual(first["kind"], "changed")
        self.assertEqual(first["depth"], 1)
        self.assertEqual(first["left_type"], "number")
        self.assertEqual(first["left_text"], "1")
        self.assertEqual(first["right_text"], "2")
        self.assertEqual(report["summary"]["changed"], self.result.summary["changed"])
        self.assertEqual(report["options"]["array_mode"], "index")
        self.assertEqual(report["counts"]["items"], len(self.result.items))
        self.assertIn("<script>", report["items"][1]["left_text"])

    def test_json_text_report_of_unchanged_and_failed_results(self):
        same = parse_json(diff_to_json_text(diff_json(parse('[1]'), parse('[1]')))).value
        self.assertIs(same["changed"], False)
        self.assertEqual(same["items"], [])
        self.assertEqual(same["summary"]["total"], 0)
        failed = parse_json(diff_to_json_text(diff_text("nope", "{}"))).value
        self.assertIs(failed["changed"], False)
        self.assertTrue(failed["error"].startswith("parse_error_left"))

    def test_json_text_report_without_values(self):
        report = parse_json(diff_to_json_text(self.result, include_values=False)).value
        self.assertNotIn("left_text", report["items"][0])
        self.assertEqual(report["items"][0]["kind"], "changed")

    def test_reports_survive_big_values(self):
        big = {"a": "x" * 5000, "b": [i for i in range(2000)]}
        result = diff_json(big, {"a": "y", "b": []})
        self.assertGreater(len(result.items), 1000)
        markdown = diff_to_markdown(result)
        html = diff_to_html(result, max_rows=50)
        text = diff_to_json_text(result, value_limit=64)
        self.assertTrue(markdown and html and text)
        # 表格行数被 max_rows 限制住（500 行 + 表头 + 分隔行）
        table_rows = [line for line in markdown.splitlines() if line.startswith("| ")]
        self.assertEqual(len(table_rows), 502)
        self.assertIn("more difference(s) were omitted", markdown)
        self.assertEqual(len([line for line in html.splitlines() if line.startswith("<tr")]), 50)
        self.assertIn("<thead><tr>", html)
        self.assertIn("more difference(s)", html)
        # JSON 报表是**完整**的（不截断条目），但单个值受 value_limit 限制
        self.assertNotIn("x" * 5000, text)
        self.assertNotIn("x" * 100, text)
        reparsed = parse_json(text)
        self.assertTrue(reparsed.ok, reparsed.error)
        self.assertEqual(len(reparsed.value["items"]), len(result.items))
        self.assertLess(len(reparsed.value["items"][0]["left_text"]), 100)
        self.assertNotIn("x" * 5000, markdown)

    def test_reports_accept_foreign_result_objects(self):
        bare = DiffResult(changed=False)
        self.assertIn("JSON Diff Report", diff_to_markdown(bare))
        self.assertIn("JSON Diff Report", diff_to_html(bare))
        self.assertTrue(parse_json(diff_to_json_text(bare)).ok)
        self.assertIn("no differences", diff_to_markdown(bare))


# --------------------------------------------------------------------------- #
# 并排行
# --------------------------------------------------------------------------- #
class TestAlignedLines(unittest.TestCase):
    LEFT = '{"a":1,"b":[1,2,3],"c":{"d":"x"}}'
    RIGHT = '{"a":2,"b":[1,9],"c":{"d":"x"},"e":true}'

    def setUp(self):
        self.left_text = pretty(parse(self.LEFT))
        self.right_text = pretty(parse(self.RIGHT))
        self.result = diff_json(parse(self.LEFT), parse(self.RIGHT))

    def test_row_shape_and_markers(self):
        rows = aligned_lines(self.left_text, self.right_text, self.result)
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(len(row), 5)
            left_no, left_line, right_no, right_line, marker = row
            self.assertIn(marker, ALIGN_MARKERS)
            if left_no is None:
                self.assertIsNone(left_line)
            else:
                self.assertIsInstance(left_no, int)
                self.assertIsInstance(left_line, str)
            if right_no is None:
                self.assertIsNone(right_line)
            else:
                self.assertIsInstance(right_no, int)
                self.assertIsInstance(right_line, str)
            if marker == MARK_ADDED:
                self.assertIsNone(left_no)
            if marker == MARK_REMOVED:
                self.assertIsNone(right_no)
            if marker in (MARK_EQUAL, MARK_CHANGED):
                self.assertIsNotNone(left_no)
                self.assertIsNotNone(right_no)

    def test_lossless_by_default(self):
        rows = aligned_lines(self.left_text, self.right_text, self.result)
        left_lines = self.left_text.split("\n")
        right_lines = self.right_text.split("\n")
        self.assertEqual(len([row for row in rows if row[0] is not None]), len(left_lines))
        self.assertEqual(len([row for row in rows if row[2] is not None]), len(right_lines))
        self.assertNotIn(MARK_CONTEXT, [row[4] for row in rows])
        # 每一行的文本与原文一一对应
        self.assertEqual([row[1] for row in rows if row[0] is not None], left_lines)
        self.assertEqual([row[3] for row in rows if row[2] is not None], right_lines)

    def test_line_numbers_strictly_increase(self):
        rows = aligned_lines(self.left_text, self.right_text, self.result)
        left_nos = [row[0] for row in rows if row[0] is not None and row[4] != MARK_CONTEXT]
        right_nos = [row[2] for row in rows if row[2] is not None and row[4] != MARK_CONTEXT]
        self.assertEqual(left_nos, sorted(set(left_nos)))
        self.assertEqual(right_nos, sorted(set(right_nos)))
        self.assertTrue(all(a < b for a, b in zip(left_nos, left_nos[1:])))
        self.assertTrue(all(a < b for a, b in zip(right_nos, right_nos[1:])))
        self.assertEqual(left_nos[0], 1)
        self.assertEqual(right_nos[0], 1)

    def test_markers_match_the_text(self):
        rows = aligned_lines(self.left_text, self.right_text, self.result)
        seen = {row[4] for row in rows}
        self.assertIn(MARK_EQUAL, seen)
        self.assertIn(MARK_CHANGED, seen)
        self.assertIn(MARK_REMOVED, seen)
        self.assertIn(MARK_ADDED, seen)
        for row in rows:
            if row[4] == MARK_EQUAL:
                self.assertEqual(row[1], row[3])
            if row[4] == MARK_CHANGED:
                self.assertNotEqual(row[1], row[3])

    def test_identical_texts_are_all_equal(self):
        rows = aligned_lines(self.left_text, self.left_text, self.result)
        self.assertEqual(len(rows), len(self.left_text.split("\n")))
        self.assertTrue(all(row[4] == MARK_EQUAL for row in rows))
        self.assertEqual([row[0] for row in rows], list(range(1, len(rows) + 1)))
        self.assertEqual([row[2] for row in rows], list(range(1, len(rows) + 1)))

    def test_context_collapsing(self):
        left = pretty([{"i": i, "v": i} for i in range(20)])
        right = pretty([{"i": i, "v": 99 if i == 10 else i} for i in range(20)])
        result = diff_text(left, right)
        rows = aligned_lines(left, right, result, context=1)
        markers = [row[4] for row in rows]
        self.assertIn(MARK_CONTEXT, markers)
        self.assertIn(MARK_CHANGED, markers)
        self.assertLess(len(rows), len(left.split("\n")))
        self.assertIn("unchanged line(s)", rows[0][1])
        left_nos = [row[0] for row in rows if row[0] is not None]
        self.assertTrue(all(a <= b for a, b in zip(left_nos, left_nos[1:])))
        # 折叠不会丢掉差异行
        self.assertTrue(any(row[4] == MARK_CHANGED and '"v": 99' in row[3] for row in rows))

    def test_context_zero_keeps_everything(self):
        left = pretty([{"i": i} for i in range(10)])
        right = pretty([{"i": i} for i in range(10)])
        rows = aligned_lines(left, right, None, context=0)
        self.assertEqual(len(rows), len(left.split("\n")))
        self.assertNotIn(MARK_CONTEXT, [row[4] for row in rows])

    def test_empty_and_bad_input(self):
        self.assertEqual(aligned_lines("", "", None), [])
        rows = aligned_lines(None, None, None)
        self.assertEqual(rows, [])
        rows = aligned_lines("{}", '{"a":1}', None)
        self.assertTrue(rows)
        self.assertTrue(all(row[4] in ALIGN_MARKERS for row in rows))

    def test_fallback_for_huge_inputs(self):
        original = jsondiff.MAX_ALIGN_LINES
        result = diff_json(parse('{"a":1}'), parse('{"a":2}'))
        left_text = '{"a": 1,\n"b": 2\n}'
        right_text = '{"a": 2,\n"b": 2\n}'
        try:
            jsondiff.MAX_ALIGN_LINES = 2
            rows = aligned_lines(left_text, right_text, result)
        finally:
            jsondiff.MAX_ALIGN_LINES = original
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row[4] in ALIGN_MARKERS for row in rows))
        self.assertTrue(any(w.startswith("aligned_lines_fallback") for w in result.warnings))
        self.assertEqual(rows[0][4], MARK_CHANGED)
        self.assertEqual(rows[1][4], MARK_EQUAL)
        self.assertEqual([row[0] for row in rows], [1, 2, 3])
        self.assertEqual([row[2] for row in rows], [1, 2, 3])

    def test_result_argument_is_optional(self):
        rows = aligned_lines(self.left_text, self.right_text)
        self.assertTrue(rows)
        self.assertEqual(len([row for row in rows if row[0] is not None]),
                         len(self.left_text.split("\n")))


# --------------------------------------------------------------------------- #
# 只看变更
# --------------------------------------------------------------------------- #
class TestChangedOnlyText(unittest.TestCase):
    LEFT = '{"a":1,"b":[1,2,3],"c":{"d":"x"},"gone":true}'
    RIGHT = '{"a":2,"b":[1,9],"c":{"d":"x"},"e":5}'

    def setUp(self):
        self.left = parse(self.LEFT)
        self.right = parse(self.RIGHT)
        self.result = diff_json(self.left, self.right)

    def test_left_side_contains_only_changed_subtrees(self):
        text = changed_only_text(self.left, self.result.items, side="left")
        parsed = parse_json(text)
        self.assertTrue(parsed.ok, parsed.error)
        self.assertEqual(parsed.value, {"a": 1, "b": [None, 2, 3], "gone": True})
        self.assertNotIn('"c"', text)
        self.assertNotIn('"d"', text)
        self.assertIn('"gone"', text)

    def test_right_side_contains_only_changed_subtrees(self):
        text = changed_only_text(self.right, self.result.items, side="right")
        parsed = parse_json(text)
        self.assertTrue(parsed.ok, parsed.error)
        self.assertEqual(parsed.value, {"a": 2, "b": [None, 9], "e": 5})
        self.assertNotIn('"gone"', text)

    def test_output_is_pretty_printed(self):
        text = changed_only_text(self.left, self.result.items)
        self.assertIn("\n  ", text)
        self.assertTrue(text.startswith("{"))
        self.assertTrue(text.endswith("}"))

    def test_array_padding_is_trimmed_at_the_end(self):
        left = parse('{"b":[1,2,3,4]}')
        result = diff_json(left, parse('{"b":[1,2,9,4]}'))
        text = changed_only_text(left, result.items)
        self.assertEqual(parse_json(text).value, {"b": [None, None, 3]})

    def test_root_level_change_returns_the_whole_side(self):
        result = diff_json(parse('{"a":1}'), parse('[1,2]'))
        self.assertEqual(changed_only_text(parse('{"a":1}'), result.items, side="left"),
                         '{\n  "a": 1\n}')
        self.assertEqual(changed_only_text(parse('[1,2]'), result.items, side="right"),
                         "[\n  1,\n  2\n]")

    def test_scalar_root_change(self):
        result = diff_json(parse("1"), parse("2"))
        self.assertEqual(changed_only_text(parse("1"), result.items).strip(), "1")
        self.assertEqual(changed_only_text(parse("2"), result.items, side="right").strip(), "2")

    def test_no_items_returns_empty_container_of_the_same_type(self):
        self.assertEqual(changed_only_text(parse('{"a":1}'), []), "{}")
        self.assertEqual(changed_only_text(parse('[1,2]'), []), "[]")
        self.assertEqual(changed_only_text(parse('"s"'), []), '"s"')
        result = diff_json(parse('{"a":1}'), parse('{"a":1}'))
        self.assertEqual(changed_only_text(parse('{"a":1}'), result.items), "{}")

    def test_removed_items_are_skipped_on_the_right_side(self):
        result = diff_json(parse('{"gone":1}'), parse('{}'))
        self.assertEqual(changed_only_text(parse('{"gone":1}'), result.items, side="right"), "{}")
        self.assertEqual(parse_json(changed_only_text(parse('{"gone":1}'), result.items)).value,
                         {"gone": 1})

    def test_reordered_items_are_skipped(self):
        result = diff_json(parse('{"o":{"a":1,"b":2},"z":1}'), parse('{"o":{"b":2,"a":1},"z":2}'),
                           DiffOptions(report_reordered=True))
        self.assertEqual(result.summary["reordered"], 1)
        text = changed_only_text(parse('{"o":{"a":1,"b":2},"z":1}'), result.items)
        self.assertEqual(parse_json(text).value, {"z": 1})

    def test_nested_paths_rebuild_containers(self):
        left = parse('{"deep":{"list":[{"keep":1},{"change":1}]}}')
        right = parse('{"deep":{"list":[{"keep":1},{"change":2}]}}')
        result = diff_json(left, right)
        text = changed_only_text(left, result.items)
        self.assertEqual(parse_json(text).value,
                         {"deep": {"list": [None, {"change": 1}]}})

    def test_input_is_not_mutated(self):
        snapshot = copy.deepcopy(self.left)
        changed_only_text(self.left, self.result.items)
        self.assertEqual(self.left, snapshot)
        self.assertEqual(list(self.left.keys()), list(snapshot.keys()))
        self.assertEqual(self.left["b"], snapshot["b"])

    def test_bad_input_does_not_raise(self):
        self.assertIsInstance(changed_only_text(None, None), str)
        self.assertIsInstance(changed_only_text(parse('{"a":1}'), None), str)
        self.assertEqual(changed_only_text(parse('{"a":1}'), [object()]), "{}")
        self.assertIsInstance(changed_only_text(parse('{"a":1}'), self.result.items, side="???"), str)

    def test_deep_document_pruning(self):
        depth = 900
        left = parse('{"a":' * depth + "1" + "}" * depth)
        right = parse('{"a":' * depth + "2" + "}" * depth)
        result = diff_json(left, right)
        self.assertTrue(result.changed)
        text = changed_only_text(left, result.items)
        reparsed = parse_json(text, max_depth=5000)
        self.assertTrue(reparsed.ok, reparsed.error)


# --------------------------------------------------------------------------- #
# 不变量：只读、确定性、绝不抛异常
# --------------------------------------------------------------------------- #
class TestInvariants(unittest.TestCase):
    LEFT = '{"a":[1,2,{"b":"x"}],"c":1,"d":{"e":null}}'
    RIGHT = '{"d":{"e":1},"a":[1,3,{"b":"y"}],"f":2}'

    def test_inputs_are_never_mutated(self):
        left = parse(self.LEFT)
        right = parse(self.RIGHT)
        left_snapshot = copy.deepcopy(left)
        right_snapshot = copy.deepcopy(right)
        for options in (None,
                        DiffOptions(report_reordered=True),
                        DiffOptions(ignore_key_order=False, report_reordered=True),
                        DiffOptions(array_mode=ARRAY_SET),
                        DiffOptions(array_mode=ARRAY_KEY),
                        DiffOptions(ignore_fields=("a", "*"), max_items=1)):
            with self.subTest(options=options):
                diff_json(left, right, options)
                self.assertEqual(left, left_snapshot)
                self.assertEqual(right, right_snapshot)
                self.assertEqual(list(left.keys()), list(left_snapshot.keys()))
                self.assertEqual(list(right.keys()), list(right_snapshot.keys()))
                self.assertEqual(left["a"][2], left_snapshot["a"][2])

    def test_items_reference_but_do_not_own_input_values(self):
        left = parse('{"a":{"b":1}}')
        right = parse('{"a":{"b":2}}')
        result = diff_json(left, right)
        self.assertEqual(result.items[0].left, 1)
        left["a"]["b"] = 99
        self.assertEqual(result.items[0].left, 1)

    def test_deterministic_item_order(self):
        left = parse(self.LEFT)
        right = parse(self.RIGHT)
        options = DiffOptions(report_reordered=True, ignore_fields=("c",))
        first = kinds(diff_json(left, right, options))
        for _ in range(3):
            self.assertEqual(kinds(diff_json(left, right, options)), first)
        self.assertTrue(first)

    def test_deterministic_across_array_modes(self):
        left = parse('[{"id":1,"v":1},{"id":2,"v":2},{"id":3,"v":3}]')
        right = parse('[{"id":3,"v":9},{"id":1,"v":1},{"id":4,"v":4}]')
        for mode in (ARRAY_INDEX, ARRAY_SET, ARRAY_KEY):
            with self.subTest(mode=mode):
                options = DiffOptions(array_mode=mode)
                first = kinds(diff_json(left, right, options))
                self.assertEqual(kinds(diff_json(left, right, options)), first)

    def test_symmetry_of_added_and_removed(self):
        left = parse('{"a":1,"only_left":2}')
        right = parse('{"a":1,"only_right":3}')
        forward = diff_json(left, right)
        backward = diff_json(right, left)
        self.assertEqual(forward.summary["added"], backward.summary["removed"])
        self.assertEqual(forward.summary["removed"], backward.summary["added"])
        self.assertEqual(forward.summary["changed"], backward.summary["changed"])

    def test_never_raises_on_strange_options(self):
        left = parse('{"a":1}')
        right = parse('{"a":2}')
        strange = [
            None,
            DiffOptions(array_mode=""),
            DiffOptions(array_mode=None),
            DiffOptions(array_mode="SET"),
            DiffOptions(ignore_fields=None),
            DiffOptions(ignore_fields="id"),
            DiffOptions(ignore_fields=(None, 3, "*")),
            DiffOptions(max_items=None),
            DiffOptions(max_items=-5),
            DiffOptions(array_key_field=None),
            {"array_mode": "set", "ignore_fields": ["a"], "unknown_key": 1},
            "not-an-options-object",
            42,
        ]
        for options in strange:
            with self.subTest(options=options):
                result = diff_json(left, right, options)
                self.assertIsInstance(result, DiffResult)
                self.assertIsInstance(result.items, list)
                self.assertIsInstance(result.summary, dict)
                for item in result.items:
                    self.assertIsInstance(item, DiffItem)
                    self.assertIn(item.kind, DIFF_KINDS)

    def test_unknown_option_keys_are_reported(self):
        result = diff_json(parse('{"a":1}'), parse('{"a":2}'), {"nope": 1})
        self.assertTrue(any(w.startswith("unknown_option") for w in result.warnings))
        result = diff_json(parse('{"a":1}'), parse('{"a":2}'), "junk")
        self.assertTrue(any(w.startswith("bad_options") for w in result.warnings))

    def test_upper_case_array_mode_is_normalized(self):
        result = diff_json(parse('[1,2]'), parse('[2,1]'), DiffOptions(array_mode="SET"))
        self.assertEqual(result.options.array_mode, ARRAY_SET)
        self.assertEqual(kinds(result), [("reordered", "$")])

    def test_never_raises_on_strange_values(self):
        values = [None, True, 0, -1, 1.5, "text", [], {}, [[]], {"": None},
                  {"a": object()}, object(), (1, 2), Decimal("1.5")]
        for left in values:
            for right in values:
                with self.subTest(left=type(left).__name__, right=type(right).__name__):
                    result = diff_json(left, right)
                    self.assertIsInstance(result, DiffResult)
                    self.assertIsInstance(result.items, list)

    def test_non_string_object_keys_are_compared_as_text(self):
        result = diff_json({1: "a"}, {"1": "b"})
        self.assertIsInstance(result, DiffResult)
        self.assertTrue(any(w.startswith("non_string_key") for w in result.warnings))
        # 键被当成文本 "1" 比较；"1" 不是标识符形式，所以路径用括号写法
        self.assertEqual(kinds(result), [("changed", "$['1']")])
        self.assertEqual(result.items[0].left, "a")
        self.assertEqual(result.items[0].right, "b")

    def test_equal_documents_never_report_anything(self):
        documents = ['{"a":1}', "[1,2,3]", '{"a":{"b":[1,{"c":null}]}}', "null", '"s"']
        for text in documents:
            for options in (DiffOptions(), DiffOptions(ignore_key_order=False),
                            DiffOptions(array_mode=ARRAY_SET), DiffOptions(array_mode=ARRAY_KEY),
                            DiffOptions(ignore_number_format=True, ignore_string_case=True)):
                with self.subTest(text=text, options=options):
                    result = diff_json(parse(text), parse(text), options)
                    self.assertFalse(result.changed)
                    self.assertEqual(result.items, [])
                    self.assertEqual(result.error, "")


# --------------------------------------------------------------------------- #
# 性能
# --------------------------------------------------------------------------- #
class TestPerformance(unittest.TestCase):
    RECORDS = 5000  # 每条约 8 个叶子 => 约 5 万节点

    @staticmethod
    def build(seed=0, changed=()):
        records = []
        for index in range(TestPerformance.RECORDS):
            name = f"NAME-{index}" if index in changed else f"name-{index}"
            records.append({
                "id": index,
                "name": name,
                "tags": ["a", "b", "c"],
                "meta": {"score": index * 2 + seed, "ok": True, "note": None},
            })
        return {"count": TestPerformance.RECORDS, "records": records}

    def test_fifty_thousand_nodes_with_sparse_changes(self):
        left = self.build()
        right = self.build(changed=set(range(0, self.RECORDS, 100)))
        started = time.perf_counter()
        result = diff_json(left, right)
        elapsed = time.perf_counter() - started
        self.assertEqual(result.error, "")
        self.assertTrue(result.changed)
        self.assertEqual(len(result.items), 50)
        self.assertTrue(all(item.kind == KIND_CHANGED for item in result.items))
        self.assertEqual(result.items[0].path_string, "$.records[0].name")
        self.assertLess(elapsed, 5.0)
        self.assertLess(result.elapsed_ms, 5000.0)
        self.assertGreater(result.summary["unchanged"], 30000)

    def test_fifty_thousand_identical_nodes_are_cheap(self):
        left = self.build()
        started = time.perf_counter()
        result = diff_json(left, self.build())
        elapsed = time.perf_counter() - started
        self.assertFalse(result.changed)
        self.assertEqual(result.items, [])
        self.assertLess(elapsed, 5.0)

    def test_fifty_thousand_nodes_all_different(self):
        left = self.build()
        right = self.build(seed=1, changed=set(range(self.RECORDS)))
        started = time.perf_counter()
        result = diff_json(left, right)
        elapsed = time.perf_counter() - started
        self.assertEqual(result.error, "")
        self.assertEqual(len(result.items), 2 * self.RECORDS)
        self.assertFalse(result.truncated)
        self.assertLess(elapsed, 5.0)

    def test_large_array_in_set_and_key_modes(self):
        left = self.build()
        shuffled = {"count": self.RECORDS, "records": list(reversed(left["records"]))}
        started = time.perf_counter()
        keyed = diff_json(left, shuffled, DiffOptions(array_mode=ARRAY_KEY))
        key_elapsed = time.perf_counter() - started
        self.assertEqual(keyed.error, "")
        # key 模式按下标配对，整体反转不产生任何差异
        self.assertFalse(keyed.changed)
        self.assertEqual(keyed.items, [])
        self.assertLess(key_elapsed, 5.0)

        started = time.perf_counter()
        as_set = diff_json(left, shuffled, DiffOptions(array_mode=ARRAY_SET))
        set_elapsed = time.perf_counter() - started
        self.assertEqual(as_set.error, "")
        # set 模式：同一个多重集换了顺序 => 恰好一条 reordered
        self.assertTrue(as_set.changed)
        self.assertEqual(len(as_set.items), 1)
        self.assertEqual(as_set.items[0].kind, KIND_REORDERED)
        self.assertEqual(as_set.items[0].path_string, "$.records")
        self.assertEqual(as_set.summary["added"], 0)
        self.assertEqual(as_set.summary["removed"], 0)
        self.assertLess(set_elapsed, 5.0)

    def test_large_reports_and_alignment_stay_usable(self):
        left = self.build()
        right = self.build(changed=set(range(0, self.RECORDS, 500)))
        result = diff_json(left, right)
        self.assertEqual(len(result.items), 10)
        started = time.perf_counter()
        markdown = diff_to_markdown(result)
        html = diff_to_html(result)
        text = diff_to_json_text(result)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 5.0)
        self.assertTrue(markdown and html and text)
        self.assertTrue(parse_json(text).ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)

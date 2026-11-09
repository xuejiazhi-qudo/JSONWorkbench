"""JSONPath 查询引擎单元测试。

覆盖：全部语法要点、Goessner 书店示例、过滤器与类型序、函数、正则与标志、
数字精度（超大整数 / Decimal 字面量）、Unicode、空文档、深层文档（证明不会
``RecursionError``）、``limit``/``truncated``、语法错误与位置、路径字符串往返、
``resolve_path``、顺序确定性、只读性。
"""

from __future__ import annotations

import random
import unittest
from decimal import Decimal
from pathlib import Path

from app.core.jsonparser import parse_json
from app.core.jsonvalue import JsonNumber, number_literal
from app.engines.jsonpathq import (
    DEFAULT_LIMIT,
    EXAMPLES,
    MAX_STEPS,
    CompiledPath,
    JsonPathError,
    PathMatch,
    QueryResult,
    compile_path,
    parse_path_string,
    path_to_string,
    query,
    resolve_path,
)

#: 项目根目录（tests/ 的上一级），子进程隔离测试用它设置 sys.path
ROOT = Path(__file__).resolve().parents[1]

STORE = """
{ "store": {
    "book": [
      { "category": "reference", "author": "Nigel Rees",
        "title": "Sayings of the Century", "price": 8.95 },
      { "category": "fiction", "author": "Evelyn Waugh",
        "title": "Sword of Honour", "price": 12.99 },
      { "category": "fiction", "author": "Herman Melville",
        "title": "Moby Dick", "isbn": "0-553-21311-3", "price": 8.99 },
      { "category": "fiction", "author": "J. R. R. Tolkien",
        "title": "The Lord of the Rings", "isbn": "0-395-19395-8", "price": 22.99 }
    ],
    "bicycle": { "color": "red", "price": 19.95 }
  },
  "expensive": 10
}
"""


def doc(text):
    """解析 JSON 文本，失败即测试失败。"""
    result = parse_json(text)
    assert result.ok, result.error
    return result.value


def run(text, expression, **kwargs):
    return query(doc(text) if isinstance(text, str) else text, expression, **kwargs)


def vals(text, expression, **kwargs):
    return [m.value for m in run(text, expression, **kwargs).matches]


def strs(text, expression, **kwargs):
    return [m.path_string for m in run(text, expression, **kwargs).matches]


def segs(text, expression, **kwargs):
    return [m.path for m in run(text, expression, **kwargs).matches]


def numbers(value_list):
    """把数字匹配结果统一成字面量文本，方便断言（避免 Decimal 写法差异）。"""
    return [number_literal(v) for v in value_list]


def deep_object(depth, key="a", leaf=1):
    """迭代构造深层嵌套对象，测试自身也不使用递归。"""
    node = leaf
    for _ in range(depth):
        node = {key: node}
    return node


def deep_array(depth, leaf=1):
    node = leaf
    for _ in range(depth):
        node = [node]
    return node


# --------------------------------------------------------------------------- #
class TestRootAndChild(unittest.TestCase):
    def test_root_returns_whole_document(self):
        result = run('{"a": 1}', "$")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(result.matches), 1)
        self.assertEqual(result.matches[0].path, ())
        self.assertEqual(result.matches[0].path_string, "$")
        self.assertEqual(result.matches[0].value, {"a": 1})

    def test_dot_child_chain(self):
        self.assertEqual(vals('{"a":{"b":{"c":42}}}', "$.a.b.c"), [42])
        self.assertEqual(segs('{"a":{"b":{"c":42}}}', "$.a.b.c"), [("a", "b", "c")])
        self.assertEqual(strs('{"a":{"b":{"c":42}}}', "$.a.b.c"), ["$.a.b.c"])

    def test_bracket_child_single_and_double_quotes(self):
        self.assertEqual(vals('{"a":{"b":1}}', "$['a']['b']"), [1])
        self.assertEqual(vals('{"a":{"b":1}}', '$["a"]["b"]'), [1])
        self.assertEqual(segs('{"a":1}', "$['a']"), [("a",)])

    def test_names_with_underscore_and_digits(self):
        text = '{"_x":1,"a_1":2,"B2c":3,"x9_y":4}'
        self.assertEqual(vals(text, "$._x"), [1])
        self.assertEqual(vals(text, "$.a_1"), [2])
        self.assertEqual(vals(text, "$.B2c"), [3])
        self.assertEqual(vals(text, "$.x9_y"), [4])

    def test_key_that_looks_like_a_number(self):
        self.assertEqual(vals('{"0":"zero","1":"one"}', "$['0']"), ["zero"])
        self.assertEqual(segs('{"0":"zero"}', "$['0']"), [("0",)])

    def test_missing_path_is_not_an_error(self):
        result = run('{"a":1}', "$.b.c.d")
        self.assertTrue(result.ok)
        self.assertEqual(result.matches, [])
        self.assertEqual(result.error, "")
        self.assertFalse(result.truncated)

    def test_missing_key_inside_existing_object(self):
        self.assertEqual(vals('{"a":{"b":1}}', "$.a.zzz"), [])

    def test_child_of_array_by_numeric_name(self):
        self.assertEqual(vals('[10,20,30]', "$['1']"), [20])

    def test_index_of_object_with_numeric_key(self):
        self.assertEqual(vals('{"2":"x"}', "$[2]"), ["x"])

    def test_whitespace_is_tolerated(self):
        self.assertEqual(vals('{"a": {"b": 1}}', "$ . a . b"), [1])
        self.assertEqual(vals('{"a": [1,2]}', "$.a[ 0 ]"), [1])

    def test_root_symbol_may_be_omitted(self):
        self.assertEqual(vals('{"a":{"b":7}}', "a.b"), [7])

    def test_current_symbol_at_top_level_behaves_like_root(self):
        self.assertEqual(vals('{"a":7}', "@.a"), [7])


# --------------------------------------------------------------------------- #
class TestWildcard(unittest.TestCase):
    def test_object_wildcard_keeps_insertion_order(self):
        text = '{"z":1,"a":2,"m":3}'
        self.assertEqual(vals(text, "$.*"), [1, 2, 3])
        self.assertEqual(segs(text, "$.*"), [("z",), ("a",), ("m",)])

    def test_array_wildcard(self):
        self.assertEqual(vals('[1,2,3]', "$[*]"), [1, 2, 3])
        self.assertEqual(vals('[1,2,3]', "$.*"), [1, 2, 3])

    def test_wildcard_then_child(self):
        text = '{"a":{"x":1},"b":{"x":2},"c":{"y":3}}'
        self.assertEqual(vals(text, "$.*.x"), [1, 2])

    def test_wildcard_on_scalar_yields_nothing(self):
        self.assertEqual(vals('{"a":5}', "$.a.*"), [])
        self.assertEqual(vals('"text"', "$.*"), [])


# --------------------------------------------------------------------------- #
class TestArraySelectors(unittest.TestCase):
    def test_positive_index(self):
        self.assertEqual(vals('[10,20,30]', "$[0]"), [10])
        self.assertEqual(vals('[10,20,30]', "$[2]"), [30])
        self.assertEqual(segs('[10,20,30]', "$[2]"), [(2,)])

    def test_out_of_range_index(self):
        self.assertEqual(vals('[10,20]', "$[5]"), [])
        self.assertEqual(vals('[10,20]', "$[-5]"), [])

    def test_negative_index(self):
        self.assertEqual(vals('[10,20,30]', "$[-1]"), [30])
        self.assertEqual(vals('[10,20,30]', "$[-3]"), [10])
        self.assertEqual(segs('[10,20,30]', "$[-1]"), [(2,)])

    def test_union_of_indices(self):
        self.assertEqual(vals('[10,20,30,40]', "$[0,2,3]"), [10, 30, 40])
        self.assertEqual(vals('[10,20,30,40]', "$[3,0]"), [40, 10])

    def test_union_removes_duplicates(self):
        self.assertEqual(vals('[10,20]', "$[0,0]"), [10])
        self.assertEqual(vals('{"a":1}', "$['a','a']"), [1])

    def test_mixed_union_of_names_and_indices(self):
        text = '{"a":"A","0":"zero","b":"B"}'
        self.assertEqual(vals(text, "$['a',0,'b']"), ["A", "zero", "B"])

    def test_union_with_slice_and_wildcard(self):
        self.assertEqual(vals('[1,2,3,4]', "$[0,2:]"), [1, 3, 4])
        self.assertEqual(vals('[1,2]', "$[0,*]"), [1, 2])

    def test_slice_basics(self):
        self.assertEqual(vals('[0,1,2,3,4,5]', "$[1:5]"), [1, 2, 3, 4])
        self.assertEqual(vals('[0,1,2,3,4,5]', "$[:3]"), [0, 1, 2])
        self.assertEqual(vals('[0,1,2,3,4,5]', "$[2:]"), [2, 3, 4, 5])
        self.assertEqual(vals('[0,1,2,3,4,5]', "$[:]"), [0, 1, 2, 3, 4, 5])

    def test_slice_with_step(self):
        self.assertEqual(vals('[0,1,2,3,4,5]', "$[::2]"), [0, 2, 4])
        self.assertEqual(vals('[0,1,2,3,4,5]', "$[1::2]"), [1, 3, 5])

    def test_slice_with_negative_step_reverses(self):
        self.assertEqual(vals('[0,1,2,3]', "$[::-1]"), [3, 2, 1, 0])
        self.assertEqual(segs('[0,1,2,3]', "$[::-1]"), [(3,), (2,), (1,), (0,)])

    def test_slice_bounds_are_clamped(self):
        self.assertEqual(vals('[0,1,2]', "$[-2:99]"), [1, 2])
        self.assertEqual(vals('[0,1,2]', "$[5:9]"), [])

    def test_slice_on_object_yields_nothing(self):
        self.assertEqual(vals('{"a":1,"b":2}', "$[0:2]"), [])

    def test_index_and_slice_after_recursive_descent(self):
        text = '{"x":{"list":[1,2,3]},"y":{"list":[4,5,6]}}'
        self.assertEqual(vals(text, "$..list[0]"), [1, 4])
        self.assertEqual(vals(text, "$..list[-1:]"), [3, 6])


# --------------------------------------------------------------------------- #
class TestRecursiveDescent(unittest.TestCase):
    def test_descendant_by_name(self):
        text = '{"a":{"price":1,"b":{"price":2}},"price":3}'
        self.assertEqual(vals(text, "$..price"), [1, 2, 3])

    def test_descendant_wildcard_is_preorder(self):
        text = '{"a":{"b":1},"c":2}'
        self.assertEqual(segs(text, "$..*"), [("a",), ("a", "b"), ("c",)])

    def test_descendant_bracket_name(self):
        text = '{"a":{"b":1},"b":2}'
        self.assertEqual(vals(text, "$..['b']"), [1, 2])

    def test_descendant_is_true_document_order(self):
        # 祖先顺序 != 文档顺序：$.x.y 必须排在 $.y 之前
        text = '{"x":{"y":"inner"},"y":"outer"}'
        self.assertEqual(vals(text, "$..y"), ["inner", "outer"])
        self.assertEqual(segs(text, "$..y"), [("x", "y"), ("y",)])

    def test_descendant_with_filter(self):
        text = '{"a":[{"p":5},{"p":50}],"b":{"c":[{"p":1}]}}'
        self.assertEqual(vals(text, "$..[?(@.p < 10)].p"), [5, 1])

    def test_descendant_with_index(self):
        text = '{"a":[1,2],"b":{"c":[3,4]}}'
        self.assertEqual(vals(text, "$..[0]"), [1, 3])

    def test_descendant_with_slice(self):
        text = '{"a":[1,2,3],"b":[4,5,6]}'
        self.assertEqual(vals(text, "$..[0:2]"), [1, 2, 4, 5])
        self.assertEqual(vals(text, "$..[1::2]"), [2, 5])
        # 递归下降按文档顺序判定成员，负步长不会把结果倒过来
        self.assertEqual(vals(text, "$..[::-1]"), [1, 2, 3, 4, 5, 6])
        self.assertEqual(vals(text, "$.a[::-1]"), [3, 2, 1])

    def test_chained_descendants_do_not_duplicate(self):
        text = '{"a":{"a":{"b":1}}}'
        self.assertEqual(segs(text, "$..a..b"), [("a", "a", "b")])

    def test_descendant_of_scalar_root(self):
        self.assertEqual(vals('5', "$..*"), [])
        self.assertEqual(vals('"text"', "$..a"), [])

    def test_descendant_or_self_function_form(self):
        text = '{"a":{"b":1}}'
        self.assertEqual(vals(text, "$..length()"), [1, 1])
        self.assertEqual(vals('{"a":[1,2],"b":[1,2,3]}', "$..length()"), [2, 2, 3])


# --------------------------------------------------------------------------- #
class TestFilters(unittest.TestCase):
    text = ('[{"n":1,"s":"a","ok":true,"nil":null},'
            '{"n":2,"s":"b","ok":false},'
            '{"n":3,"s":"c"}]')

    def test_comparison_operators(self):
        self.assertEqual(vals(self.text, "$[?(@.n == 2)].s"), ["b"])
        self.assertEqual(vals(self.text, "$[?(@.n != 2)].s"), ["a", "c"])
        self.assertEqual(vals(self.text, "$[?(@.n < 2)].s"), ["a"])
        self.assertEqual(vals(self.text, "$[?(@.n <= 2)].s"), ["a", "b"])
        self.assertEqual(vals(self.text, "$[?(@.n > 2)].s"), ["c"])
        self.assertEqual(vals(self.text, "$[?(@.n >= 2)].s"), ["b", "c"])

    def test_logical_operators(self):
        self.assertEqual(vals(self.text, "$[?(@.n > 1 && @.n < 3)].s"), ["b"])
        self.assertEqual(vals(self.text, "$[?(@.n == 1 || @.n == 3)].s"), ["a", "c"])
        self.assertEqual(vals(self.text, "$[?(!@.ok)].s"), ["b", "c"])
        self.assertEqual(vals(self.text, "$[?(@.n > 1 and @.n < 3)].s"), ["b"])
        self.assertEqual(vals(self.text, "$[?(@.n == 1 or @.n == 3)].s"), ["a", "c"])

    def test_parentheses_group(self):
        self.assertEqual(vals(self.text, "$[?((@.n == 1 || @.n == 3) && @.s != 'c')].s"), ["a"])

    def test_arithmetic(self):
        self.assertEqual(vals(self.text, "$[?(@.n * 2 == 4)].s"), ["b"])
        self.assertEqual(vals(self.text, "$[?(@.n + 1 == 3)].s"), ["b"])
        self.assertEqual(vals(self.text, "$[?(@.n - 1 == 1)].s"), ["b"])
        self.assertEqual(vals(self.text, "$[?(@.n % 2 == 1)].s"), ["a", "c"])
        self.assertEqual(vals(self.text, "$[?(@.n / 2 == 1)].s"), ["b"])

    def test_unary_minus_and_precedence(self):
        self.assertEqual(vals('[{"n":-5},{"n":5}]', "$[?(@.n == -5)].n"), [-5])
        self.assertEqual(vals(self.text, "$[?(@.n + @.n * 2 == 6)].s"), ["b"])

    def test_literals(self):
        self.assertEqual(vals(self.text, "$[?(@.s == 'a')].n"), [1])
        self.assertEqual(vals(self.text, '$[?(@.s == "a")].n'), [1])
        self.assertEqual(vals(self.text, "$[?(@.ok == true)].n"), [1])
        self.assertEqual(vals(self.text, "$[?(@.ok == false)].n"), [2])
        self.assertEqual(vals(self.text, "$[?(@.nil == null)].n"), [1])

    def test_existence_test(self):
        self.assertEqual(vals(self.text, "$[?(@.ok)].n"), [1])
        self.assertEqual(vals(self.text, "$[?(@.nil)].n"), [])
        self.assertEqual(vals('[{"n":0},{"m":1}]', "$[?(@.n)].n"), [0])

    def test_zero_and_empty_string_are_truthy(self):
        text = '[{"v":0},{"v":""},{"v":[]},{"v":null},{"v":false},{"v":{}}]'
        self.assertEqual(len(run(text, "$[?(@.v)]").matches), 4)

    def test_missing_property_makes_every_comparison_false(self):
        text = '[{"a":1},{"b":2}]'
        self.assertEqual(vals(text, "$[?(@.a != 1)]"), [])
        self.assertEqual(vals(text, "$[?(@.a == 1)]"), [{"a": 1}])
        self.assertEqual(vals(text, "$[?(@.a < 100)]"), [{"a": 1}])

    def test_root_reference_inside_filter(self):
        text = '{"limit":2,"items":[{"v":1},{"v":2},{"v":3}]}'
        self.assertEqual(vals(text, "$.items[?(@.v > $.limit)].v"), [3])

    def test_filter_on_non_container_yields_nothing(self):
        self.assertEqual(vals('{"a":5}', "$.a[?(@ > 1)]"), [])
        self.assertEqual(vals('{"a":"text"}', "$.a[?(@)]"), [])
        self.assertEqual(vals('{"a":null}', "$.a[?(@)]"), [])

    def test_filter_over_array_of_scalars(self):
        self.assertEqual(vals('[1,5,3,9]', "$[?(@ > 3)]"), [5, 9])
        self.assertEqual(vals('["a","bb","c"]', "$[?(length(@) > 1)]"), ["bb"])

    def test_filter_over_object_members(self):
        text = '{"x":{"v":1},"y":{"v":9},"z":3}'
        self.assertEqual(segs(text, "$[?(@.v > 5)]"), [("y",)])

    def test_filter_without_parentheses(self):
        self.assertEqual(vals('[1,5,3]', "$[?@ > 3]"), [5])

    def test_bare_property_name_is_relative_to_current_node(self):
        self.assertEqual(vals('[{"a":1},{"a":2}]', "$[?(a > 1)].a"), [2])

    def test_nested_path_inside_filter(self):
        text = '[{"m":{"k":1}},{"m":{"k":2}}]'
        self.assertEqual(vals(text, "$[?(@.m.k == 2)].m.k"), [2])

    def test_division_by_zero_is_not_an_error(self):
        text = '[{"a":0},{"a":2}]'
        self.assertEqual(vals(text, "$[?(10 / @.a > 1)].a"), [2])


# --------------------------------------------------------------------------- #
class TestTypeOrdering(unittest.TestCase):
    text = '[null,true,false,0,"a",[],[1],{},{"k":1}]'

    def test_order_null_bool_number_string_array_object(self):
        self.assertEqual(segs(self.text, "$[?(@ < 0)]"), [(0,), (1,), (2,)])
        self.assertEqual(segs(self.text, "$[?(@ > 0)]"), [(4,), (5,), (6,), (7,), (8,)])

    def test_equality_never_crosses_types(self):
        self.assertEqual(segs(self.text, "$[?(@ == 0)]"), [(3,)])
        self.assertEqual(segs(self.text, "$[?(@ == false)]"), [(2,)])
        self.assertEqual(segs(self.text, "$[?(@ == null)]"), [(0,)])

    def test_not_equal_is_the_only_true_for_incomparable_values(self):
        self.assertEqual(len(run(self.text, "$[?(@ != 0)]").matches), 8)

    def test_arrays_and_objects_have_no_order_among_themselves(self):
        text = '{"two":[1,2],"arr":[{"v":[0]},{"v":[1,2]},{"v":[9]},{"v":{}}]}'
        self.assertEqual(vals(text, "$.arr[?(@.v < $.two)].v"), [])
        self.assertEqual(vals(text, "$.arr[?(@.v > $.two)].v"), [{}])
        self.assertEqual(vals(text, "$.arr[?(@.v <= $.two)].v"), [[1, 2]])
        self.assertEqual(vals(text, "$.arr[?(@.v >= $.two)].v"), [[1, 2], {}])
        self.assertEqual(vals(text, "$.arr[?(@.v != $.two)].v"), [[0], [9], {}])

    def test_structural_equality_for_containers(self):
        text = '{"o":{"a":1},"arr":[{"v":{"a":1}},{"v":{"a":2}},{"v":{"a":1,"b":2}}]}'
        self.assertEqual(segs(text, "$.arr[?(@.v == $.o)]"), [("arr", 0)])

    def test_nested_array_equality(self):
        text = '{"o":[1,[2,3]],"arr":[{"v":[1,[2,3]]},{"v":[1,[2,4]]}]}'
        self.assertEqual(segs(text, "$.arr[?(@.v == $.o)]"), [("arr", 0)])


# --------------------------------------------------------------------------- #
class TestNumbers(unittest.TestCase):
    def test_huge_integer_is_exact(self):
        big = "123456789012345678901234567890123456789"
        text = '{"n":%s,"other":1}' % big
        self.assertEqual(vals(text, "$[?(@ == %s)]" % big), [int(big)])
        self.assertIsInstance(run(text, "$.n").matches[0].value, int)
        self.assertEqual(vals(text, "$[?(@ > 100000000000000000000)]"), [int(big)])
        self.assertEqual(vals(text, "$[?(@ == %s0)]" % big), [])

    def test_huge_integer_arithmetic_stays_exact(self):
        text = '[{"a":100000000000000000000000000000}]'
        self.assertEqual(vals(text, "$[?(@.a + 1 == 100000000000000000000000000001)].a"),
                         [10 ** 29])
        self.assertEqual(vals(text, "$[?(@.a * 2 == 200000000000000000000000000000)].a"),
                         [10 ** 29])
        self.assertEqual(vals(text, "$[?(@.a - 1 == 99999999999999999999999999999)].a"),
                         [10 ** 29])

    def test_decimal_literal_compares_numerically_with_integer(self):
        text = '{"a":1.0,"b":1,"c":2}'
        self.assertEqual(vals(text, "$[?(@ == 1)]"), [JsonNumber("1.0"), 1])

    def test_trailing_zeros_do_not_change_equality(self):
        text = '[{"v":1.10},{"v":1.1},{"v":1.11}]'
        self.assertEqual(vals(text, "$[?(@.v == 1.1)].v"),
                         [JsonNumber("1.10"), JsonNumber("1.1")])

    def test_decimal_arithmetic(self):
        text = '[{"p":0.1},{"p":0.2}]'
        self.assertEqual(vals(text, "$[?(@.p + @.p == 0.2)].p"), [JsonNumber("0.1")])
        self.assertEqual(vals(text, "$[?(@.p * 3 > 0.5)].p"), [JsonNumber("0.2")])

    def test_scientific_notation_literal(self):
        text = '{"a":1e3,"b":5}'
        self.assertEqual(vals(text, "$[?(@ == 1000)]"), [JsonNumber("1e3")])
        self.assertEqual(vals(text, "$[?(@ > 900)]"), [JsonNumber("1e3")])

    def test_decimal_filter_literal(self):
        text = '[{"v":1.5},{"v":2.5}]'
        self.assertEqual(vals(text, "$[?(@.v > 1.25)].v"), [JsonNumber("1.5"), JsonNumber("2.5")])

    def test_number_and_string_never_compare_equal(self):
        text = '[{"v":"1"},{"v":1}]'
        self.assertEqual(segs(text, "$[?(@.v == 1)]"), [(1,)])
        self.assertEqual(segs(text, "$[?(@.v == '1')]"), [(0,)])


# --------------------------------------------------------------------------- #
class TestRegex(unittest.TestCase):
    def test_match_operator_is_a_search(self):
        text = '["alpha","beta","gamma"]'
        self.assertEqual(vals(text, "$[?(@ =~ /a/)]"), ["alpha", "beta", "gamma"])
        self.assertEqual(vals(text, "$[?(@ =~ /^a/)]"), ["alpha"])

    def test_escaped_slash_inside_pattern(self):
        text = r'["a/b","x"]'
        self.assertEqual(vals(text, r"$[?(@ =~ /a\/b/)]"), ["a/b"])

    def test_flag_i(self):
        self.assertEqual(vals('["ABC"]', "$[?(@ =~ /abc/i)]"), ["ABC"])
        self.assertEqual(vals('["ABC"]', "$[?(@ =~ /abc/)]"), [])

    def test_flag_m(self):
        text = r'["a\nb"]'
        self.assertEqual(vals(text, "$[?(@ =~ /^b/m)]"), ["a\nb"])
        self.assertEqual(vals(text, "$[?(@ =~ /^b/)]"), [])

    def test_flag_s(self):
        text = r'["a\nb"]'
        self.assertEqual(vals(text, "$[?(@ =~ /a.b/s)]"), ["a\nb"])
        self.assertEqual(vals(text, "$[?(@ =~ /a.b/)]"), [])

    def test_match_function_requires_full_match(self):
        text = '["abc","abd","xyz"]'
        self.assertEqual(vals(text, "$[?match(@, /ab.*/)]"), ["abc", "abd"])
        self.assertEqual(vals(text, "$[?match(@, /^a/)]"), [])

    def test_search_function_is_partial(self):
        text = '["abc","xyz"]'
        self.assertEqual(vals(text, "$[?search(@, /^a/)]"), ["abc"])

    def test_regex_subject_coercion(self):
        text = '[{"v":1},{"v":"x1"},{"v":[1]},{"v":{"a":1}},{"v":true},{"v":null},{"w":1}]'
        # 数字 / 布尔 / null 先转成 JSON 文本再匹配
        self.assertEqual(segs(text, "$[?(@.v =~ /1/)]"), [(0,), (1,)])
        self.assertEqual(segs(text, "$[?(@.v =~ /true/)]"), [(4,)])
        self.assertEqual(segs(text, "$[?(@.v =~ /null/)]"), [(5,)])
        # 容器与缺失值永远不匹配
        self.assertEqual(segs(text, "$[?(@.v =~ /.*/)]"), [(0,), (1,), (4,), (5,)])
        self.assertEqual(segs(text, "$[?(@.v =~ /x/)]"), [(1,)])

    def test_regex_metacharacters(self):
        text = '["a1","b22","c"]'
        self.assertEqual(vals(text, r"$[?(@ =~ /^[a-z]\d+$/)]"), ["a1", "b22"])


# --------------------------------------------------------------------------- #
class TestFunctions(unittest.TestCase):
    def test_length_of_string_array_object(self):
        text = '{"s":"hello","a":[1,2,3],"o":{"x":1,"y":2},"n":5,"b":true,"z":null}'
        self.assertEqual(vals(text, "$.s.length()"), [5])
        self.assertEqual(vals(text, "$.a.length()"), [3])
        self.assertEqual(vals(text, "$.o.length()"), [2])
        self.assertEqual(vals(text, "$.s.size()"), [5])

    def test_length_of_scalar_is_nothing(self):
        text = '{"n":5,"b":true,"z":null}'
        self.assertEqual(vals(text, "$.n.length()"), [])
        self.assertEqual(vals(text, "$.b.length()"), [])
        self.assertEqual(vals(text, "$.z.length()"), [])

    def test_count_is_the_size_of_the_node_list(self):
        text = '{"a":[1,2,3],"b":{"x":1}}'
        self.assertEqual(vals(text, "$.a[*].count()"), [3])
        self.assertEqual(vals(text, "$.*.count()"), [2])
        self.assertEqual(vals(text, "$.a.count()"), [1])
        self.assertEqual(vals(text, "$.missing.count()"), [])

    def test_count_inside_filter(self):
        text = '[{"t":[1,2]},{"t":[1]},{"t":[]}]'
        self.assertEqual(segs(text, "$[?(count(@.t[*]) == 2)]"), [(0,)])
        self.assertEqual(segs(text, "$[?(count(@.missing[*]) == 0)]"), [(0,), (1,), (2,)])

    def test_first_and_last_on_arrays(self):
        text = '{"a":[1,2,3]}'
        self.assertEqual(vals(text, "$.a.first()"), [1])
        self.assertEqual(vals(text, "$.a.last()"), [3])

    def test_first_and_last_on_objects(self):
        text = '{"o":{"x":1,"y":2}}'
        self.assertEqual(vals(text, "$.o.first()"), [1])
        self.assertEqual(vals(text, "$.o.last()"), [2])

    def test_first_and_last_over_a_node_list(self):
        text = '{"list":[{"v":1},{"v":2},{"v":3}]}'
        self.assertEqual(vals(text, "$.list[*].first()"), [{"v": 1}])
        self.assertEqual(vals(text, "$.list[*].last()"), [{"v": 3}])

    def test_first_and_last_on_empty_container(self):
        self.assertEqual(vals('{"a":[],"o":{}}', "$.a.first()"), [])
        self.assertEqual(vals('{"a":[],"o":{}}', "$.o.last()"), [])

    def test_keys_and_values(self):
        text = '{"o":{"x":1,"y":2},"a":[7,8]}'
        self.assertEqual(vals(text, "$.o.keys()"), ["x", "y"])
        self.assertEqual(vals(text, "$.o.values()"), [1, 2])
        self.assertEqual(vals(text, "$.a.keys()"), [0, 1])
        self.assertEqual(vals(text, "$.a.values()"), [7, 8])

    def test_keys_of_scalar_is_nothing(self):
        self.assertEqual(vals('{"a":5}', "$.a.keys()"), [])

    def test_min_max_sum_avg(self):
        text = '{"p":[3,1,2]}'
        self.assertEqual(vals(text, "$.p.min()"), [1])
        self.assertEqual(vals(text, "$.p.max()"), [3])
        self.assertEqual(vals(text, "$.p.sum()"), [6])
        self.assertEqual(vals(text, "$.p.avg()"), [Decimal("2")])

    def test_aggregates_over_a_node_list(self):
        text = '{"b":[{"p":8.95},{"p":12.99},{"p":8.99}]}'
        self.assertEqual(numbers(vals(text, "$.b[*].p.min()")), ["8.95"])
        self.assertEqual(numbers(vals(text, "$.b[*].p.max()")), ["12.99"])
        self.assertEqual(numbers(vals(text, "$.b[*].p.sum()")), ["30.93"])

    def test_avg_of_non_divisible_integers(self):
        self.assertEqual(vals('{"a":[1,2]}', "$.a.avg()"), [Decimal("1.5")])

    def test_sum_ignores_non_numbers(self):
        self.assertEqual(vals('{"a":[1,"x",2,null,true,3]}', "$.a.sum()"), [6])
        self.assertEqual(vals('{"a":["x",null]}', "$.a.sum()"), [])

    def test_min_max_on_strings(self):
        text = '{"a":["pear","apple","zeta"]}'
        self.assertEqual(vals(text, "$.a.min()"), ["apple"])
        self.assertEqual(vals(text, "$.a.max()"), ["zeta"])

    def test_type_function(self):
        text = '{"s":"x","n":1,"f":1.5,"b":true,"z":null,"a":[],"o":{}}'
        self.assertEqual(vals(text, "$.*.type()"),
                         ["string", "number", "number", "boolean", "null", "array", "object"])

    def test_match_and_search_functions(self):
        text = '["abc","xyz"]'
        self.assertEqual(vals(text, "$[?match(@, /abc/)]"), ["abc"])
        self.assertEqual(vals(text, "$[?search(@, /b/)]"), ["abc"])

    def test_regex_function_with_string_pattern(self):
        text = '["abc","xyz"]'
        self.assertEqual(vals(text, "$[?match(@, 'a.c')]"), ["abc"])

    def test_single_argument_regex_function_applies_to_each_node(self):
        text = '{"names":["Ann","Bob","alice"]}'
        self.assertEqual(vals(text, "$.names[*].match(/^a.*/i)"), [True, False, True])
        self.assertEqual(vals(text, "$.names[*].match(/^a/)"), [False, False, False])

    def test_functions_after_wildcard(self):
        text = '{"a":[1,2],"b":[1,2,3]}'
        self.assertEqual(vals(text, "$.*.length()"), [2, 3])
        self.assertEqual(vals(text, "$..length()"), [2, 2, 3])

    def test_unknown_function_is_a_syntax_error(self):
        with self.assertRaises(JsonPathError) as caught:
            compile_path("$.a.nosuch()")
        self.assertIn("unknown_function", caught.exception.message)

    def test_bad_arity_is_a_syntax_error(self):
        with self.assertRaises(JsonPathError) as caught:
            compile_path("$.a.length(1,2)")
        self.assertIn("bad_arity", caught.exception.message)


# --------------------------------------------------------------------------- #
class TestGoessnerStore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = doc(STORE)

    def q(self, expression, **kwargs):
        return query(self.store, expression, **kwargs)

    def values(self, expression):
        return [m.value for m in self.q(expression).matches]

    def test_authors(self):
        self.assertEqual(self.values("$.store.book[*].author"),
                         ["Nigel Rees", "Evelyn Waugh", "Herman Melville", "J. R. R. Tolkien"])
        self.assertEqual(self.values("$..author"), self.values("$.store.book[*].author"))

    def test_everything_in_store(self):
        result = self.q("$.store.*")
        self.assertEqual([m.path for m in result.matches],
                         [("store", "book"), ("store", "bicycle")])

    def test_all_prices(self):
        self.assertEqual(numbers(self.values("$..price")),
                         ["8.95", "12.99", "8.99", "22.99", "19.95"])

    def test_third_book(self):
        self.assertEqual(self.values("$..book[2].title"), ["Moby Dick"])

    def test_last_book(self):
        self.assertEqual(self.values("$..book[-1:].title"), ["The Lord of the Rings"])

    def test_books_with_isbn(self):
        self.assertEqual(self.values("$..book[?(@.isbn)].title"),
                         ["Moby Dick", "The Lord of the Rings"])

    def test_books_without_isbn(self):
        self.assertEqual(self.values("$..book[?(!@.isbn)].title"),
                         ["Sayings of the Century", "Sword of Honour"])

    def test_cheap_books(self):
        self.assertEqual(self.values("$..book[?(@.price < 10)].title"),
                         ["Sayings of the Century", "Moby Dick"])

    def test_price_against_root_value(self):
        self.assertEqual(self.values("$..book[?(@.price <= $.expensive)].title"),
                         ["Sayings of the Century", "Moby Dick"])

    def test_bicycle_colour(self):
        self.assertEqual(self.values("$.store.bicycle.color"), ["red"])

    def test_first_and_last_title(self):
        self.assertEqual(self.values("$.store.book.first().title"), ["Sayings of the Century"])
        self.assertEqual(self.values("$.store.book.last().title"), ["The Lord of the Rings"])

    def test_statistics(self):
        self.assertEqual(self.values("$.store.book.length()"), [4])
        self.assertEqual(numbers(self.values("$..price.min()")), ["8.95"])
        self.assertEqual(numbers(self.values("$..price.max()")), ["22.99"])
        self.assertEqual(numbers(self.values("$.store.book[*].price.sum()")), ["53.92"])

    def test_cheapest_book_via_nested_aggregate(self):
        self.assertEqual(self.values("$..book[?(@.price == min($..price))].title"),
                         ["Sayings of the Century"])

    def test_union_of_member_names(self):
        self.assertEqual(self.values("$..book[0]['title','price']"),
                         ["Sayings of the Century", JsonNumber("8.95")])

    def test_wildcard_over_all_books(self):
        self.assertEqual(len(self.q("$.store.book[*]").matches), 4)
        self.assertEqual(len(self.q("$..*").matches), 28)


# --------------------------------------------------------------------------- #
class TestUnicode(unittest.TestCase):
    def test_chinese_keys_and_values(self):
        text = '{"中文":{"键":"值"},"emoji":"😀"}'
        self.assertEqual(vals(text, "$.中文.键"), ["值"])
        self.assertEqual(vals(text, "$['中文']['键']"), ["值"])
        self.assertEqual(vals(text, '$["中文"]["键"]'), ["值"])
        self.assertEqual(vals(text, "$.emoji"), ["😀"])

    def test_unicode_in_paths_and_filters(self):
        text = '{"数据":[{"名称":"甲","价":5},{"名称":"乙","价":50}]}'
        self.assertEqual(vals(text, "$.数据[?(@.价 < 10)].名称"), ["甲"])
        self.assertEqual(segs(text, "$.数据[0].名称"), [("数据", 0, "名称")])

    def test_unicode_path_string_uses_bracket_form(self):
        result = run('{"中文":1}', "$.中文")
        self.assertEqual(result.matches[0].path_string, "$['中文']")

    def test_unicode_escapes_in_selectors(self):
        text = '{"\\u4e2d\\u6587":1}'
        self.assertEqual(vals(text, r"$['\u4e2d\u6587']"), [1])

    def test_emoji_keys_round_trip(self):
        path = ("😀", 0, "中文")
        self.assertEqual(parse_path_string(path_to_string(path)), path)

    def test_recursive_descent_over_unicode_document(self):
        text = '{"中":{"文":{"值":1}},"emoji":"😀"}'
        self.assertEqual(vals(text, "$..值"), [1])


# --------------------------------------------------------------------------- #
class TestEmptyAndScalarDocuments(unittest.TestCase):
    def test_empty_object(self):
        self.assertEqual(strs('{}', "$"), ["$"])
        self.assertEqual(vals('{}', "$.*"), [])
        self.assertEqual(vals('{}', "$..*"), [])
        self.assertEqual(vals('{}', "$.a"), [])
        self.assertEqual(vals('{}', "$[?(@.a)]"), [])

    def test_empty_array(self):
        self.assertEqual(strs('[]', "$"), ["$"])
        self.assertEqual(vals('[]', "$[*]"), [])
        self.assertEqual(vals('[]', "$[0]"), [])
        self.assertEqual(vals('[]', "$[0:5]"), [])
        self.assertEqual(vals('[]', "$..*"), [])
        self.assertEqual(vals('[]', "$[?(@ > 1)]"), [])

    def test_scalar_documents(self):
        self.assertEqual(vals('42', "$"), [42])
        self.assertEqual(vals('"text"', "$"), ["text"])
        self.assertEqual(vals('true', "$"), [True])
        self.assertEqual(vals('null', "$"), [None])
        self.assertEqual(vals('null', "$..*"), [])

    def test_functions_on_empty_containers(self):
        self.assertEqual(vals('[]', "$.length()"), [0])
        self.assertEqual(vals('[]', "$.missing.length()"), [])
        self.assertEqual(vals('{"a":[]}', "$.a.length()"), [0])
        self.assertEqual(vals('{"a":{}}', "$.a.keys()"), [])
        self.assertEqual(vals('{"a":[]}', "$.a.sum()"), [])

    def test_none_document_is_handled(self):
        result = query(None, "$..a")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.matches, [])


# --------------------------------------------------------------------------- #
class TestLimitAndTruncation(unittest.TestCase):
    def test_limit_caps_matches_and_sets_truncated(self):
        result = run('[1,2,3,4,5]', "$[*]", limit=3)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(result.matches), 3)
        self.assertTrue(result.truncated)
        self.assertEqual([m.value for m in result.matches], [1, 2, 3])

    def test_no_truncation_when_under_the_limit(self):
        result = run('[1,2,3]', "$[*]", limit=3)
        self.assertEqual(len(result.matches), 3)
        self.assertFalse(result.truncated)
        result = run('[1,2,3]', "$[*]", limit=100)
        self.assertFalse(result.truncated)

    def test_limit_of_one(self):
        result = run('[1,2,3]', "$[*]", limit=1)
        self.assertEqual(len(result.matches), 1)
        self.assertTrue(result.truncated)

    def test_zero_or_negative_limit_means_unlimited(self):
        for limit in (0, -1):
            with self.subTest(limit=limit):
                result = run('[1,2,3]', "$[*]", limit=limit)
                self.assertEqual(len(result.matches), 3)
                self.assertFalse(result.truncated)

    def test_default_limit_is_large(self):
        self.assertEqual(DEFAULT_LIMIT, 100000)
        result = run(list(range(1000)), "$[*]")
        self.assertEqual(len(result.matches), 1000)
        self.assertFalse(result.truncated)

    def test_traversal_stops_early_on_a_huge_document(self):
        big = list(range(200000))
        result = query(big, "$[*]", limit=5)
        self.assertEqual(len(result.matches), 5)
        self.assertTrue(result.truncated)
        self.assertLess(result.elapsed_ms, 2000.0)

    def test_elapsed_ms_is_reported(self):
        result = run('{"a":1}', "$.a")
        self.assertGreaterEqual(result.elapsed_ms, 0.0)


# --------------------------------------------------------------------------- #
class TestSyntaxErrors(unittest.TestCase):
    CASES = {
        "$[": "unbalanced_bracket",
        "$.a[0": "unbalanced_bracket",
        "$['a'": "unbalanced_bracket",
        "$.a]": "unexpected_char",
        "$..": "unexpected_end",
        "$.": "unexpected_end",
        "$[?(@.a": "unexpected_end",
        "$[?(@.a >)]": "invalid_filter",
        "$[?(@.a && )]": "invalid_filter",
        "$[?()]": "invalid_filter",
        "$[?(@.a =! 1)]": "invalid_filter",
        "$.nosuch()": "unknown_function",
        "$.length(1,2)": "bad_arity",
        "$.match()": "bad_arity",
        "$[1:2:0]": "invalid_slice",
        "$[?(@ =~ /(/)]": "invalid_regex",
        "$[?(@ =~ /a/z)]": "invalid_regex_flag",
        "$[?(@.a == 'x)]": "unterminated_string",
        "$[[0]]": "invalid_selector",
        "$[?(@.a)] ]": "unexpected_char",
        "$[?((((@.a)))))]": "unbalanced_bracket",
    }

    def test_query_never_raises_and_reports_position(self):
        for expression, code in self.CASES.items():
            with self.subTest(expression=expression):
                result = query({"a": 1}, expression)
                self.assertFalse(result.ok)
                self.assertEqual(result.matches, [])
                self.assertTrue(result.error)
                self.assertTrue(result.error.startswith(code + ":"), result.error)
                self.assertGreaterEqual(result.error_position, 0)
                self.assertLessEqual(result.error_position, len(expression))
                self.assertGreaterEqual(result.elapsed_ms, 0.0)

    def test_error_message_is_english_ascii(self):
        for expression in self.CASES:
            with self.subTest(expression=expression):
                result = query({}, expression)
                self.assertTrue(result.error.isascii(), result.error)

    def test_compile_path_raises_jsonpath_error(self):
        with self.assertRaises(JsonPathError) as caught:
            compile_path("$[")
        self.assertEqual(caught.exception.code, "unbalanced_bracket")
        self.assertIn("never closed", caught.exception.message)
        self.assertEqual(caught.exception.position, 2)
        self.assertIn("position 2", str(caught.exception))

    def test_error_position_points_at_the_offending_character(self):
        result = query({}, "$.a b")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_position, 4)
        result = query({}, "$[?(@ =~ /a/z)]")
        self.assertEqual("z", "$[?(@ =~ /a/z)]"[result.error_position])

    def test_deeply_nested_filter_is_rejected_not_crashed(self):
        expression = "$[?" + "(" * 400 + "@.a" + ")" * 400 + "]"
        result = query({"a": 1}, expression)
        self.assertFalse(result.ok)
        self.assertIn("expression_too_complex", result.error)

    def test_too_many_steps_is_rejected(self):
        expression = "$" + ".a" * (MAX_STEPS + 10)
        result = query({"a": 1}, expression)
        self.assertFalse(result.ok)
        self.assertIn("expression_too_complex", result.error)

    def test_many_steps_below_the_limit_still_work(self):
        depth = 200
        value = deep_object(depth)
        result = query(value, "$" + ".a" * depth)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(result.matches), 1)
        self.assertEqual(result.matches[0].value, 1)

    def test_empty_expression_selects_the_root(self):
        self.assertEqual(strs('{"a":1}', ""), ["$"])
        self.assertEqual(strs('{"a":1}', "   "), ["$"])

    def test_non_string_expression_is_coerced(self):
        result = query({"a": 1}, None)
        self.assertIsInstance(result.ok, bool)
        self.assertTrue(result.ok or result.error)


# --------------------------------------------------------------------------- #
class TestPathStrings(unittest.TestCase):
    def test_simple_round_trip(self):
        for path in [(), ("a",), ("a", "b"), (0,), (-1,), ("a", 0, "b"), (2, "x"),
                     ("store", "book", 0, "title")]:
            with self.subTest(path=path):
                text = path_to_string(path)
                self.assertEqual(parse_path_string(text), path)

    def test_normalized_form(self):
        self.assertEqual(path_to_string(()), "$")
        self.assertEqual(path_to_string(("store", "book", 0, "title")), "$.store.book[0].title")
        self.assertEqual(path_to_string((-1,)), "$[-1]")
        self.assertEqual(path_to_string(("_x9",)), "$._x9")

    def test_keys_needing_bracket_quoting(self):
        tricky = ["a.b", "a b", "", "it's", 'say "hi"', "a[0]", "*", "\\", "\n", "\t",
                  "0", "-1", "中文", "😀", "a/b", "a:b", "?", "$", "@", "#", "a\rb"]
        for key in tricky:
            with self.subTest(key=key):
                path = ("root", key, 0)
                text = path_to_string(path)
                self.assertTrue(text.startswith("$.root["), text)
                self.assertEqual(parse_path_string(text), path)

    def test_quoted_form_is_valid_json_path(self):
        text = '{"a.b":1,"it\'s":2,"":3}'
        for key in ["a.b", "it's", ""]:
            with self.subTest(key=key):
                path_string = path_to_string((key,))
                result = query(doc(text), path_string)
                self.assertTrue(result.ok, result.error)
                self.assertEqual(len(result.matches), 1)

    def test_match_path_string_round_trips_to_the_same_node(self):
        value = doc('{"a":{"b c":[1,{"d.e":2}]},"中文":{"x":[3]}}')
        for expression in ["$..*", "$.a['b c'][1]['d.e']", '$["中文"].x[0]']:
            with self.subTest(expression=expression):
                result = query(value, expression)
                self.assertTrue(result.ok, result.error)
                for match in result.matches:
                    found, node = resolve_path(value, parse_path_string(match.path_string))
                    self.assertTrue(found, match.path_string)
                    self.assertIs(node, match.value)

    def test_parse_path_string_is_tolerant(self):
        self.assertEqual(parse_path_string("$..a[*]"), ("a",))
        self.assertEqual(parse_path_string("a.b[0]"), ("a", "b", 0))
        self.assertEqual(parse_path_string("@.x['y']"), ("x", "y"))
        self.assertEqual(parse_path_string("$[ -1 ]"), (-1,))
        self.assertEqual(parse_path_string("$"), ())
        self.assertEqual(parse_path_string(""), ())
        self.assertEqual(parse_path_string(None), ())
        self.assertIsInstance(parse_path_string("$$$...[[["), tuple)
        self.assertIsInstance(parse_path_string("][?*.@$/"), tuple)

    def test_path_to_string_accepts_any_sequence(self):
        self.assertEqual(path_to_string(["a", 0]), "$.a[0]")
        self.assertEqual(path_to_string(()), "$")


# --------------------------------------------------------------------------- #
class TestResolvePath(unittest.TestCase):
    def test_resolve_on_parsed_document(self):
        value = doc('{"store":{"book":[{"title":"T"},{"title":"U"}]}}')
        self.assertEqual(resolve_path(value, ("store", "book", 0, "title")), (True, "T"))
        self.assertEqual(resolve_path(value, ("store", "book", 1)), (True, {"title": "U"}))
        self.assertEqual(resolve_path(value, ()), (True, value))

    def test_negative_index(self):
        self.assertEqual(resolve_path([1, 2, 3], (-1,)), (True, 3))
        self.assertEqual(resolve_path([1, 2, 3], (-4,)), (False, None))

    def test_missing_paths(self):
        self.assertEqual(resolve_path({"a": 1}, ("b",)), (False, None))
        self.assertEqual(resolve_path({"a": 1}, ("a", "b")), (False, None))
        self.assertEqual(resolve_path([1], (5,)), (False, None))
        self.assertEqual(resolve_path(5, ("a",)), (False, None))
        self.assertEqual(resolve_path(None, ("a",)), (False, None))

    def test_tolerant_segment_types(self):
        self.assertEqual(resolve_path({"1": "x"}, (1,)), (True, "x"))
        self.assertEqual(resolve_path([10, 20], ("1",)), (True, 20))
        self.assertEqual(resolve_path({"a": 1}, ("a", True)), (False, None))
        self.assertEqual(resolve_path({"a": 1}, None), (False, None))

    def test_resolve_accepts_a_path_string(self):
        value = doc('{"a":{"b":[1,2,3]}}')
        self.assertEqual(resolve_path(value, "$.a.b[2]"), (True, 3))
        self.assertEqual(resolve_path(value, "$.a.zzz"), (False, None))

    def test_resolve_on_deep_document(self):
        depth = 1200
        value = deep_object(depth)
        self.assertEqual(resolve_path(value, ("a",) * depth), (True, 1))
        self.assertEqual(resolve_path(value, ("a",) * (depth + 1)), (False, None))

    def test_resolve_returns_the_live_node(self):
        value = doc('{"a":{"b":1}}')
        found, node = resolve_path(value, ("a",))
        self.assertTrue(found)
        self.assertIs(node, value["a"])


# --------------------------------------------------------------------------- #
class TestOrderingAndDeterminism(unittest.TestCase):
    def test_document_order_for_objects_and_arrays(self):
        text = '{"z":{"k":1},"a":[{"k":2},{"k":3}],"m":{"k":4}}'
        self.assertEqual(vals(text, "$..k"), [1, 2, 3, 4])

    def test_recursive_descent_is_preorder(self):
        text = '{"a":{"b":{"c":1}},"d":2}'
        self.assertEqual(segs(text, "$..*"),
                         [("a",), ("a", "b"), ("a", "b", "c"), ("d",)])

    def test_repeated_queries_are_identical(self):
        value = doc(STORE)
        first = query(value, "$..*")
        for _ in range(3):
            again = query(value, "$..*")
            self.assertEqual([m.path for m in first.matches], [m.path for m in again.matches])
            self.assertEqual([m.path_string for m in first.matches],
                             [m.path_string for m in again.matches])

    def test_union_keeps_the_written_order(self):
        self.assertEqual(segs('[1,2,3]', "$[2,0,1]"), [(2,), (0,), (1,)])

    def test_negative_slice_reverses_explicitly(self):
        self.assertEqual(segs('[1,2,3]', "$[::-1]"), [(2,), (1,), (0,)])

    def test_matches_are_in_path_order_for_nested_filters(self):
        text = '{"a":[{"v":1},{"v":5}],"b":[{"v":2},{"v":9}]}'
        self.assertEqual(segs(text, "$..[?(@.v > 3)]"), [("a", 1), ("b", 1)])


# --------------------------------------------------------------------------- #
class TestReadOnlyAndSafety(unittest.TestCase):
    def snapshot(self, value):
        stack = [((), value)]
        out = []
        while stack:
            path, node = stack.pop()
            if isinstance(node, dict):
                out.append((path, "object", tuple(node.keys())))
                for key, child in node.items():
                    stack.append((path + (key,), child))
            elif isinstance(node, list):
                out.append((path, "array", len(node)))
                for idx, child in enumerate(node):
                    stack.append((path + (idx,), child))
            else:
                out.append((path, "scalar", node if not isinstance(node, Decimal) else str(node)))
        return sorted(out, key=lambda item: (len(item[0]), item[0]))

    def test_queries_do_not_mutate_the_document(self):
        value = doc(STORE)
        before = self.snapshot(value)
        for expression, _ in EXAMPLES:
            query(value, expression)
        self.assertEqual(self.snapshot(value), before)

    def test_matches_reference_the_original_nodes(self):
        value = doc('{"a":{"b":[1,2]}}')
        match = query(value, "$.a.b").matches[0]
        self.assertIs(match.value, value["a"]["b"])

    def test_path_match_is_immutable(self):
        match = PathMatch(path=("a",), value=1, path_string="$.a")
        with self.assertRaises(Exception):
            match.value = 2
        self.assertEqual(match.path_string, "$.a")

    def test_query_survives_bizarre_documents(self):
        weird = {
            "": ["", None, True, False, 0, -0.0, {}, []],
            "deep": deep_object(60),
            "wide": {str(i): i for i in range(50)},
            "self": {"list": [[[[1]]]]},
        }
        for expression in ["$..*", "$..['']", "$[?(@)]", "$..length()", "$..[0]", "$..[::-1]"]:
            with self.subTest(expression=expression):
                result = query(weird, expression)
                self.assertIsInstance(result.ok, bool)
                if result.ok:
                    self.assertIsInstance(result.matches, list)

    def test_result_object_shape(self):
        result = run('{"a":1}', "$.a")
        self.assertIsInstance(result, QueryResult)
        self.assertIsInstance(result.matches[0], PathMatch)
        self.assertEqual(result.values, [1])
        self.assertEqual(result.paths, [("a",)])
        self.assertEqual(result.error_position, -1)

    def test_compiled_path_is_reusable_and_shared(self):
        compiled = compile_path("$.a[*]")
        self.assertIsInstance(compiled, CompiledPath)
        self.assertIs(compiled, compile_path("$.a[*]"))
        self.assertEqual(str(compiled), "$.a[*]")
        value = {"a": [1, 2]}
        self.assertEqual([m.value for m in compiled.find(value)], [1, 2])
        self.assertEqual(compiled.values(value), [1, 2])
        self.assertEqual(compiled.paths(value), [("a", 0), ("a", 1)])
        self.assertEqual(len(compiled.find(value, limit=1)), 1)

    def test_iterator_interface_is_lazy(self):
        value = list(range(100000))
        iterator = compile_path("$[*]").iter_matches(value)
        first = next(iterator)
        self.assertEqual(first.value, 0)
        self.assertEqual(first.path_string, "$[0]")
        iterator.close()

    def test_compiled_path_is_thread_safe(self):
        # UI 可能在工作线程里查询，而 compile_path 返回的是缓存里的同一个对象
        import threading

        value = doc('{"a":[{"b":1},{"b":2}],"c":{"b":3}}')
        compiled = compile_path("$..b")
        results: list = []
        lock = threading.Lock()

        def worker():
            for _ in range(50):
                found = [m.value for m in compiled.iter_matches(value)]
                with lock:
                    results.append(found)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 300)
        self.assertEqual({tuple(item) for item in results}, {(1, 2, 3)})


# --------------------------------------------------------------------------- #
class TestDeepDocuments(unittest.TestCase):
    def test_500_levels_of_objects(self):
        value = deep_object(500)
        result = query(value, "$..a", limit=10000)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(result.matches), 500)
        self.assertFalse(result.truncated)

    def test_1500_levels_do_not_hit_the_recursion_limit(self):
        value = deep_object(1500)
        for expression in ["$..a", "$..*", "$..[?(@)]"]:
            with self.subTest(expression=expression):
                result = query(value, expression, limit=20000)
                self.assertTrue(result.ok, result.error)
                self.assertEqual(len(result.matches), 1500)

    def test_deep_arrays(self):
        value = deep_array(800)
        result = query(value, "$..*", limit=5000)
        self.assertTrue(result.ok, result.error)
        # 800 层数组 + 1 个叶子标量，扣掉根节点自身
        self.assertEqual(len(result.matches), 800)
        self.assertEqual(result.matches[-1].value, 1)

    def test_deep_document_with_leaf_query(self):
        value = deep_object(600, leaf="leaf")
        result = query(value, "$..[?(@ == 'leaf')]")
        self.assertTrue(result.ok, result.error)
        self.assertEqual([m.value for m in result.matches], ["leaf"])
        self.assertEqual(len(result.matches[0].path), 600)

    def test_deep_mixed_nesting(self):
        node = {"in": 1}
        for i in range(400):
            node = [{"k%d" % i: node}, {"in": i}]
        result = query(node, "$..in", limit=5000)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(result.matches), 401)

    def test_deep_path_string_generation(self):
        path = tuple("k%d" % i for i in range(1000))
        text = path_to_string(path)
        self.assertTrue(text.startswith("$.k0.k1."))
        self.assertEqual(parse_path_string(text), path)


# --------------------------------------------------------------------------- #
class TestPublicApiSurface(unittest.TestCase):
    def test_examples_are_valid_and_unique(self):
        self.assertGreaterEqual(len(EXAMPLES), 18)
        seen = set()
        for item in EXAMPLES:
            with self.subTest(example=item):
                self.assertIsInstance(item, tuple)
                self.assertEqual(len(item), 2)
                expression, description = item
                self.assertNotIn(expression, seen)
                seen.add(expression)
                compiled = compile_path(expression)
                self.assertIsInstance(compiled, CompiledPath)
                self.assertTrue(description)
                self.assertTrue(description.isascii())

    def test_every_example_runs_on_the_store_document(self):
        value = doc(STORE)
        for expression, _ in EXAMPLES:
            with self.subTest(example=expression):
                result = query(value, expression)
                self.assertTrue(result.ok, f"{expression}: {result.error}")

    def test_module_has_no_qt_or_network_dependency(self):
        import subprocess
        import sys

        import app.engines.jsonpathq as module

        with open(module.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("PySide6", "import Qt", "urllib", "socket", "requests", "http"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, source)

        # 在全新解释器里只导入引擎，确认它不会顺带拉进 Qt / 网络库
        script = (
            "import sys, json;"
            "sys.path.insert(0, %r);"
            "import app.engines.jsonpathq;"
            "bad = [m for m in sys.modules if m.split('.')[0] in "
            "('PySide6', 'shiboken6', 'Qt', 'urllib', 'socket', 'http', 'requests')];"
            "print(json.dumps(bad))" % str(ROOT)
        )
        done = subprocess.run([sys.executable, "-c", script], capture_output=True,
                              text=True, timeout=120, cwd=str(ROOT))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "[]")

    def test_every_advertised_function_compiles_and_runs(self):
        required = ["length", "size", "count", "first", "last", "keys", "values",
                    "min", "max", "sum", "avg", "type", "match", "search"]
        value = doc('{"a":[3,1,2],"o":{"k":"v"},"s":"text"}')
        for name in required:
            with self.subTest(function=name):
                if name in ("match", "search"):
                    terminal = f"$.s.{name}(/tex.*/)"
                    in_filter = f"$[?{name}(@.s, /tex.*/)]"
                else:
                    terminal = f"$.a.{name}()"
                    in_filter = f"$[?({name}(@.a) >= 0)]"
                compiled = compile_path(terminal)
                self.assertIsInstance(compiled, CompiledPath)
                result = query(value, terminal)
                self.assertTrue(result.ok, result.error)
                self.assertGreaterEqual(len(result.matches), 1, terminal)
                self.assertTrue(compile_path(in_filter))
                filtered = query(value, in_filter)
                self.assertTrue(filtered.ok, filtered.error)

    def test_path_string_always_matches_the_normalized_form(self):
        value = doc(STORE)
        for expression in ["$..*", "$..book[0]['title','price']", "$..price"]:
            with self.subTest(expression=expression):
                for match in query(value, expression).matches:
                    self.assertEqual(match.path_string, path_to_string(match.path))


# --------------------------------------------------------------------------- #
class TestRobustness(unittest.TestCase):
    def test_random_garbage_never_raises(self):
        rng = random.Random(20260104)
        alphabet = "$@.[]*?()=!<>&|/+-%'\"0123456789abcXYZ_:, \\"
        value = doc('{"a":[1,{"b":"x"}],"c":null}')
        for _ in range(400):
            expression = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
            with self.subTest(expression=expression):
                result = query(value, expression)
                self.assertIsInstance(result.ok, bool)
                if result.ok:
                    self.assertIsInstance(result.matches, list)
                else:
                    self.assertTrue(result.error)
                    self.assertGreaterEqual(result.error_position, 0)

    def test_random_filters_never_raise(self):
        rng = random.Random(11)
        value = doc('[{"n":1,"s":"a"},{"n":"2"},{"n":null},[1,2],{"n":{"m":3}}]')
        bodies = ["@.n", "@.n > 1", "@.n == '2'", "length(@)", "@.n =~ /1/", "count(@.n[*])",
                  "@.n + 1", "-@.n", "@.n % 2", "1 / @.n", "@.n.min()", "type(@.n) == 'number'",
                  "!@.n", "@.n && @.s", "match(@.s, /a/)", "@.n == null", "@[0]"]
        for _ in range(120):
            body = rng.choice(bodies)
            result = query(value, f"$[?({body})]")
            self.assertIsInstance(result.ok, bool)
            self.assertTrue(result.ok or result.error)

    def test_huge_key_names(self):
        key = "k" * 5000
        value = {key: 1}
        result = query(value, "$['%s']" % key)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.matches[0].value, 1)

    def test_wide_document(self):
        value = {str(i): i for i in range(20000)}
        result = query(value, "$.*", limit=200000)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(result.matches), 20000)

    def test_repeated_identical_queries_are_fast_enough(self):
        value = doc(STORE)
        compiled = compile_path("$..book[?(@.price < 10)].title")
        for _ in range(200):
            self.assertEqual(len(compiled.find(value)), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

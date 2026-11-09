"""转换引擎单元测试：CSV / XML / YAML 双向、拆分、合并、去重、编码、深度与性能。

覆盖重点：

* 数字字面量保真（``1.230``、``1e5``、``-0``、30 位整数）穿过每种格式；
* Unicode（中文键名 + emoji）穿过每种格式；
* 每一个会改变行为的选项值；
* 三种格式的往返一致性（``json_equal``）；
* 恶意 XML（DOCTYPE / 实体扩展）被拒绝且不卡死；
* 500+ 层嵌套不触发 ``RecursionError``；
* 空输入（``{}`` / ``[]`` / ``""`` / ``null``）对每个公共函数都不崩；
* 2 万行 CSV 的性能 sanity。
"""

from __future__ import annotations

import os
import re
import tempfile
import time
import unittest

import yaml

from app.core.jsonformatter import json_equal, minify
from app.core.jsonparser import parse_json
from app.core.jsonvalue import JsonNumber, number_literal, to_plain
from app.engines.converters import (
    ARRAY_CELL,
    ARRAY_JOIN,
    MAX_EXPAND_ROWS,
    SUPPORTED_ENCODINGS,
    YAML_MAX_DEPTH,
    ConvertResult,
    CsvOptions,
    XmlOptions,
    YamlOptions,
    csv_to_json,
    dedupe_array,
    encoding_to_bytes,
    escape_json_string,
    json_to_csv,
    json_to_xml,
    json_to_yaml,
    merge_documents,
    split_array,
    split_to_files,
    unescape_json_string,
    warning_codes,
    xml_to_json,
    yaml_to_json,
)

BIG_INT = 123456789012345678901234567890
FLAT_JSON = ('[{"id":1,"name":"张三","price":1.230,"ok":true,"note":null},'
             '{"id":2,"name":"emoji 😀","price":1e5,"ok":false,"note":"x"}]')


def V(text):
    """解析 JSON 文本，失败直接断言失败（测试数据必须是合法的）。"""
    result = parse_json(text)
    assert result.ok, result.error
    return result.value


def codes(result):
    return warning_codes(result)


def value_of(result):
    """把 ``ConvertResult.output`` 再解析回 JSON 值。"""
    assert result.ok, result.error
    parsed = parse_json(result.output)
    assert parsed.ok, parsed.error
    return parsed.value


def read_file(path):
    """读取测试写出的文件（with 语句，避免 ResourceWarning）。"""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def nested_object(depth, leaf=1, max_depth=0):
    """构造 ``{"a":{"a":...}}`` 深嵌套对象（不用递归，避免测试自身爆栈）。"""
    text = '{"a":' * depth + str(leaf) + "}" * depth
    result = parse_json(text, max_depth=max_depth or (depth + 10))
    assert result.ok, result.error
    return result.value


def nested_array(depth):
    return V("[" * depth + "]" * depth) if depth else []


# --------------------------------------------------------------------------- #
# 结果对象
# --------------------------------------------------------------------------- #
class TestConvertResult(unittest.TestCase):
    def test_defaults(self):
        result = ConvertResult(ok=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "")
        self.assertEqual(result.error, "")
        self.assertEqual(result.warnings, [])
        self.assertEqual(result.meta, {})

    def test_warning_list_is_not_shared_between_instances(self):
        first = ConvertResult(ok=True)
        second = ConvertResult(ok=True)
        first.warnings.append("x")
        first.meta["k"] = 1
        self.assertEqual(second.warnings, [])
        self.assertEqual(second.meta, {})

    def test_warning_codes_parses_prefix(self):
        result = ConvertResult(ok=True, warnings=["a_b: detail", "c_d"])
        self.assertEqual(warning_codes(result), ["a_b", "c_d"])

    def test_error_messages_are_english_and_coded(self):
        result = json_to_csv(None)
        self.assertFalse(result.ok)
        self.assertTrue(re.match(r"^[a-z0-9_]+: ", result.error), result.error)
        self.assertTrue(result.error.isascii(), result.error)


# --------------------------------------------------------------------------- #
# JSON -> CSV
# --------------------------------------------------------------------------- #
class TestJsonToCsv(unittest.TestCase):
    def test_array_of_objects_is_the_common_case(self):
        result = json_to_csv(V('[{"a":1,"b":"x"},{"a":2,"b":"y"}]'))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.output, "a,b\r\n1,x\r\n2,y\r\n")
        self.assertEqual(result.meta["rows"], 2)
        self.assertEqual(result.meta["columns"], ["a", "b"])

    def test_single_object_becomes_one_row(self):
        result = json_to_csv(V('{"a":1,"b":2}'))
        self.assertEqual(result.output, "a,b\r\n1,2\r\n")
        self.assertEqual(result.meta["rows"], 1)

    def test_top_level_scalar_uses_value_column(self):
        self.assertEqual(json_to_csv("hi").output, "value\r\nhi\r\n")
        self.assertEqual(json_to_csv(42).output, "value\r\n42\r\n")
        self.assertEqual(json_to_csv(True).output, "value\r\ntrue\r\n")

    def test_array_of_scalars_uses_value_column(self):
        result = json_to_csv(V('[1,2,3]'))
        self.assertEqual(result.output, "value\r\n1\r\n2\r\n3\r\n")

    def test_null_root_is_an_error_not_a_crash(self):
        result = json_to_csv(None)
        self.assertFalse(result.ok)
        self.assertIn("csv_null_root", result.error)

    def test_null_elements_are_skipped_and_counted(self):
        result = json_to_csv(V('[{"a":1},null,{"a":2}]'))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.meta["skipped"], 1)
        self.assertEqual(result.meta["rows"], 2)
        self.assertIn("csv_skipped_nulls", codes(result))

    def test_empty_inputs(self):
        empty_array = json_to_csv([])
        self.assertTrue(empty_array.ok)
        self.assertEqual(empty_array.output, "")
        self.assertEqual(empty_array.meta["rows"], 0)
        empty_object = json_to_csv({})
        self.assertTrue(empty_object.ok)
        self.assertEqual(empty_object.output, "")
        self.assertIn("csv_no_columns", codes(empty_object))
        self.assertTrue(json_to_csv("").ok)
        # 整行只有一个空字段时必须加引号，否则读回来会少一行
        self.assertEqual(json_to_csv("").output, 'value\r\n""\r\n')

    def test_flatten_nested_objects_with_dotted_columns(self):
        result = json_to_csv(V('[{"a":{"b":{"c":1}},"d":2}]'))
        self.assertEqual(result.meta["columns"], ["a.b.c", "d"])
        self.assertEqual(result.output, "a.b.c,d\r\n1,2\r\n")

    def test_flatten_separator_option(self):
        result = json_to_csv(V('[{"a":{"b":1}}]'), CsvOptions(flatten_separator="/"))
        self.assertEqual(result.meta["columns"], ["a/b"])

    def test_flatten_disabled_serializes_containers_as_json(self):
        result = json_to_csv(V('[{"a":{"b":1},"c":[1,2]}]'), CsvOptions(flatten=False))
        self.assertEqual(result.meta["columns"], ["a", "c"])
        self.assertEqual(result.output, 'a,c\r\n"{""b"":1}","[1,2]"\r\n')
        self.assertIn("csv_flatten_disabled", codes(result))

    def test_empty_nested_containers_are_not_lost(self):
        result = json_to_csv(V('[{"a":{},"b":[]}]'))
        self.assertEqual(result.output, 'a,b\r\n{},[]\r\n')

    def test_existing_dotted_key_collides_first_value_wins(self):
        # 数据里本来就有 "a.b" 键，同时又有 {"a":{"b":...}}：先到者胜 + 告警
        result = json_to_csv(V('[{"a.b":1,"a":{"b":2}}]'))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.output, "a.b\r\n1\r\n")
        self.assertIn("csv_flatten_collision", codes(result))

    def test_array_handling_expand(self):
        result = json_to_csv(V('[{"id":1,"tags":["a","b"]}]'), CsvOptions(array_handling="expand"))
        self.assertEqual(result.output, "id,tags\r\n1,a\r\n1,b\r\n")
        self.assertEqual(result.meta["rows"], 2)

    def test_array_handling_expand_of_objects_uses_dotted_columns(self):
        result = json_to_csv(V('[{"id":1,"p":[{"x":1,"y":2},{"x":3,"y":4}]}]'))
        self.assertEqual(result.meta["columns"], ["id", "p.x", "p.y"])
        self.assertEqual(result.output, "id,p.x,p.y\r\n1,1,2\r\n1,3,4\r\n")

    def test_array_handling_expand_is_cartesian_across_fields(self):
        result = json_to_csv(V('[{"a":[1,2],"b":[3,4]}]'))
        self.assertEqual(result.meta["rows"], 4)
        self.assertEqual(result.output.splitlines()[1:], ["1,3", "1,4", "2,3", "2,4"])

    def test_array_handling_expand_empty_array_keeps_the_row(self):
        result = json_to_csv(V('[{"a":[],"b":1}]'))
        self.assertEqual(result.output, "a,b\r\n[],1\r\n")

    def test_array_handling_expand_overflow_falls_back_to_cell(self):
        items = ",".join(str(i) for i in range(MAX_EXPAND_ROWS + 1))
        result = json_to_csv(V('[{"a":[%s]}]' % items))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.meta["rows"], 1)
        self.assertIn("csv_expand_overflow", codes(result))
        self.assertIn("[0,1,", result.output)

    def test_array_handling_join(self):
        result = json_to_csv(V('[{"tags":["a","b","c"]}]'), CsvOptions(array_handling="join"))
        self.assertEqual(result.output, "tags\r\na; b; c\r\n")

    def test_array_handling_join_separator_option(self):
        result = json_to_csv(V('[{"tags":["a","b"]}]'),
                            CsvOptions(array_handling="join", join_separator="|"))
        self.assertEqual(result.output, "tags\r\na|b\r\n")

    def test_array_handling_join_of_objects_uses_json_text(self):
        result = json_to_csv(V('[{"a":[{"x":1},{"x":2}]}]'), CsvOptions(array_handling="join"))
        self.assertEqual(result.output, 'a\r\n"{""x"":1}; {""x"":2}"\r\n')

    def test_array_handling_cell(self):
        result = json_to_csv(V('[{"tags":["a","b"]}]'), CsvOptions(array_handling="cell"))
        self.assertEqual(result.output, 'tags\r\n"[""a"",""b""]"\r\n')

    def test_nested_array_in_expand_mode_becomes_json_text(self):
        result = json_to_csv(V('[{"a":[[1,2],[3]]}]'))
        self.assertIn("csv_nested_array", codes(result))
        self.assertEqual(result.output, 'a\r\n"[1,2]"\r\n[3]\r\n')

    def test_explicit_column_selection_and_order(self):
        result = json_to_csv(V('[{"a":1,"b":2,"c":3}]'), CsvOptions(columns=("c", "a")))
        self.assertEqual(result.output, "c,a\r\n3,1\r\n")
        self.assertEqual(result.meta["dropped"], 1)
        self.assertIn("csv_columns_dropped", codes(result))

    def test_unknown_explicit_column_is_empty(self):
        result = json_to_csv(V('[{"a":1}]'), CsvOptions(columns=("a", "zzz")))
        self.assertEqual(result.output, "a,zzz\r\n1,\r\n")
        self.assertIn("csv_unknown_column", codes(result))

    def test_missing_values_use_null_text(self):
        result = json_to_csv(V('[{"a":1,"b":2},{"a":3}]'), CsvOptions(null_text="N/A"))
        self.assertEqual(result.output, "a,b\r\n1,2\r\n3,N/A\r\n")

    def test_null_value_uses_null_text(self):
        result = json_to_csv(V('[{"a":null}]'), CsvOptions(null_text="NULL"))
        self.assertEqual(result.output, "a\r\nNULL\r\n")

    def test_rfc4180_quoting_delimiter_inside_value(self):
        result = json_to_csv(V('[{"a":"x,y"}]'))
        self.assertEqual(result.output, 'a\r\n"x,y"\r\n')

    def test_rfc4180_quoting_embedded_newline(self):
        result = json_to_csv(V('[{"a":"l1\\nl2"}]'))
        self.assertEqual(result.output, 'a\r\n"l1\nl2"\r\n')

    def test_rfc4180_quoting_embedded_quote_is_doubled(self):
        result = json_to_csv(V('[{"a":"say \\"hi\\""}]'))
        self.assertEqual(result.output, 'a\r\n"say ""hi"""\r\n')

    def test_quote_all_option(self):
        result = json_to_csv(V('[{"a":1,"b":"x"}]'), CsvOptions(quote_all=True))
        self.assertEqual(result.output, '"a","b"\r\n"1","x"\r\n')

    def test_delimiter_option_tab_and_semicolon(self):
        result = json_to_csv(V('[{"a":1,"b":2}]'), CsvOptions(delimiter="\t"))
        self.assertEqual(result.output, "a\tb\r\n1\t2\r\n")
        result = json_to_csv(V('[{"a":1,"b":2}]'), CsvOptions(delimiter="\\t"))
        self.assertEqual(result.output, "a\tb\r\n1\t2\r\n")
        result = json_to_csv(V('[{"a":1,"b":2}]'), CsvOptions(delimiter=";"))
        self.assertEqual(result.output, "a;b\r\n1;2\r\n")

    def test_bad_delimiter_is_rejected(self):
        for bad in ("", ",,", '"', "\n"):
            with self.subTest(delimiter=bad):
                result = json_to_csv(V('[{"a":1}]'), CsvOptions(delimiter=bad))
                self.assertFalse(result.ok)
                self.assertIn("csv_bad_delimiter", result.error)

    def test_include_header_option(self):
        result = json_to_csv(V('[{"a":1}]'), CsvOptions(include_header=False))
        self.assertEqual(result.output, "1\r\n")

    def test_line_ending_option(self):
        result = json_to_csv(V('[{"a":1}]'), CsvOptions(line_ending="\n"))
        self.assertEqual(result.output, "a\n1\n")
        self.assertNotIn("\r", result.output)

    def test_bad_line_ending_falls_back(self):
        result = json_to_csv(V('[{"a":1}]'), CsvOptions(line_ending="\r"))
        self.assertTrue(result.ok, result.error)
        self.assertIn("csv_bad_line_ending", codes(result))
        self.assertIn("\r\n", result.output)

    def test_bad_array_handling_falls_back_to_expand(self):
        result = json_to_csv(V('[{"a":[1,2]}]'), CsvOptions(array_handling="nope"))
        self.assertTrue(result.ok, result.error)
        self.assertIn("csv_bad_array_handling", codes(result))
        self.assertEqual(result.meta["rows"], 2)

    def test_encoding_option_is_validated_and_reported(self):
        for encoding in ("utf-8", "utf-8-sig", "gbk", "gb18030", "utf-16"):
            with self.subTest(encoding=encoding):
                result = json_to_csv(V('[{"a":"中"}]'), CsvOptions(encoding=encoding))
                self.assertTrue(result.ok, result.error)
                self.assertIn(result.meta["encoding"], SUPPORTED_ENCODINGS)
        bad = json_to_csv(V('[{"a":1}]'), CsvOptions(encoding="koi8-r"))
        self.assertFalse(bad.ok)
        self.assertIn("unsupported_encoding", bad.error)

    def test_unicode_keys_and_values(self):
        result = json_to_csv(V('[{"中文":"值","emoji":"😀"}]'))
        self.assertEqual(result.output, "中文,emoji\r\n值,😀\r\n")

    def test_number_literals_survive(self):
        result = json_to_csv(V('[{"a":1.230,"b":1e5,"c":-0,"d":%d,"e":1E+5}]' % BIG_INT))
        self.assertTrue(result.ok, result.error)
        cells = result.output.splitlines()[1].split(",")
        self.assertEqual(cells, ["1.230", "1e5", "-0", str(BIG_INT), "1E+5"])
        self.assertEqual(codes(result), [])

    def test_booleans_are_json_literals(self):
        self.assertEqual(json_to_csv(V('[{"a":true,"b":false}]')).output, "a,b\r\ntrue,false\r\n")

    def test_root_array_of_arrays(self):
        result = json_to_csv(V('[[1,2],[3]]'))
        self.assertEqual(result.meta["columns"], ["value"])
        self.assertEqual(result.meta["rows"], 3)

    def test_input_is_not_mutated(self):
        data = V('[{"a":[1,2],"b":{"c":1}}]')
        snapshot = minify(data)
        json_to_csv(data)
        json_to_csv(data, CsvOptions(flatten=False))
        json_to_csv(data, CsvOptions(array_handling="cell"))
        self.assertEqual(minify(data), snapshot)

    def test_non_options_argument_is_ignored(self):
        result = json_to_csv(V('[{"a":1}]'), "not-options")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.output, "a\r\n1\r\n")


# --------------------------------------------------------------------------- #
# CSV -> JSON
# --------------------------------------------------------------------------- #
class TestCsvToJson(unittest.TestCase):
    def test_header_row_becomes_keys(self):
        result = csv_to_json("a,b\n1,2\n3,4")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(value_of(result), [{"a": 1, "b": 2}, {"a": 3, "b": 4}])
        self.assertEqual(result.meta["rows"], 2)
        self.assertEqual(result.meta["columns"], ["a", "b"])

    def test_without_header_generates_column_names(self):
        result = csv_to_json("1,2\n3,4", has_header=False)
        self.assertEqual(value_of(result), [{"column1": 1, "column2": 2},
                                            {"column1": 3, "column2": 4}])

    def test_infer_types_on(self):
        result = csv_to_json("a,b,c,d\n1,1.5,true,false")
        parsed = value_of(result)[0]
        self.assertEqual(parsed["a"], 1)
        self.assertIsInstance(parsed["a"], int)
        self.assertIsInstance(parsed["b"], JsonNumber)
        self.assertEqual(number_literal(parsed["b"]), "1.5")
        self.assertIs(parsed["c"], True)
        self.assertIs(parsed["d"], False)

    def test_infer_types_off_keeps_strings(self):
        result = csv_to_json("a,b\n1,true", infer_types=False)
        self.assertEqual(value_of(result), [{"a": "1", "b": "true"}])

    def test_null_values(self):
        result = csv_to_json("a,b,c\n,null,")
        self.assertEqual(value_of(result), [{"a": None, "b": None, "c": None}])

    def test_custom_null_values(self):
        result = csv_to_json("a,b\nNA,x", null_values=("NA",))
        self.assertEqual(value_of(result), [{"a": None, "b": "x"}])
        result = csv_to_json("a,b\nNA,x", null_values=())
        self.assertEqual(value_of(result), [{"a": "NA", "b": "x"}])

    def test_non_canonical_numbers_stay_strings(self):
        result = csv_to_json("a,b,c,d,e\n007,1.5.6,'1',1e,0x1F")
        parsed = value_of(result)[0]
        self.assertEqual(parsed, {"a": "007", "b": "1.5.6", "c": "'1'", "d": "1e", "e": "0x1F"})

    def test_number_literals_are_preserved(self):
        result = csv_to_json("a,b,c,d\n1.230,1e5,-0,%d" % BIG_INT)
        parsed = value_of(result)[0]
        self.assertEqual(number_literal(parsed["a"]), "1.230")
        self.assertEqual(number_literal(parsed["b"]), "1e5")
        self.assertEqual(number_literal(parsed["c"]), "-0")
        self.assertEqual(parsed["d"], BIG_INT)
        self.assertIsInstance(parsed["d"], int)

    def test_empty_input_is_an_error(self):
        for text in ("", "   ", "\n\n"):
            with self.subTest(text=repr(text)):
                result = csv_to_json(text)
                self.assertFalse(result.ok)
                self.assertIn("csv_empty_input", result.error)

    def test_header_only_produces_empty_array(self):
        result = csv_to_json("a,b\n")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(value_of(result), [])
        self.assertIn("csv_header_only", codes(result))

    def test_quoted_fields_with_delimiter_newline_and_quotes(self):
        text = 'a,b\n"x,y","l1\nl2"\n"he said ""hi""",z'
        result = csv_to_json(text)
        self.assertEqual(value_of(result), [{"a": "x,y", "b": "l1\nl2"},
                                            {"a": 'he said "hi"', "b": "z"}])

    def test_custom_delimiter(self):
        self.assertEqual(value_of(csv_to_json("a;b\n1;2", delimiter=";")), [{"a": 1, "b": 2}])
        self.assertEqual(value_of(csv_to_json("a\tb\n1\t2", delimiter="\t")), [{"a": 1, "b": 2}])

    def test_root_object_keyed_by_first_column(self):
        result = csv_to_json("id,name\n1,x\n2,y", root="object")
        self.assertEqual(value_of(result), {"1": {"name": "x"}, "2": {"name": "y"}})

    def test_root_object_with_single_column(self):
        result = csv_to_json("id\n1\n2", root="object")
        self.assertEqual(value_of(result), {"1": 1, "2": 2})

    def test_root_object_duplicate_keys_last_wins(self):
        result = csv_to_json("id,name\n1,x\n1,y", root="object")
        self.assertEqual(value_of(result), {"1": {"name": "y"}})
        self.assertEqual(result.meta["duplicate_keys"], 1)
        self.assertIn("csv_duplicate_object_key", codes(result))

    def test_root_object_without_header(self):
        result = csv_to_json("1,x\n2,y", has_header=False, root="object")
        self.assertEqual(value_of(result), {"1": {"column2": "x"}, "2": {"column2": "y"}})

    def test_bad_root_value(self):
        result = csv_to_json("a\n1", root="maybe")
        self.assertFalse(result.ok)
        self.assertIn("csv_bad_root", result.error)

    def test_trim_whitespace_option(self):
        self.assertEqual(value_of(csv_to_json(" a , b \n 1 , 2 ")) , [{"a": 1, "b": 2}])
        result = csv_to_json("a,b\n 1 , x ", trim_whitespace=False)
        self.assertEqual(value_of(result), [{"a": " 1 ", "b": " x "}])

    def test_ragged_rows_are_padded_and_reported(self):
        result = csv_to_json("a,b\n1,2,3\n4")
        self.assertEqual(value_of(result), [{"a": 1, "b": 2, "column3": 3},
                                            {"a": 4, "b": None, "column3": None}])
        self.assertIn("csv_ragged_row", codes(result))

    def test_duplicate_header_names_are_renamed(self):
        result = csv_to_json("a,a,a\n1,2,3")
        self.assertEqual(result.meta["columns"], ["a", "a_2", "a_3"])
        self.assertEqual(value_of(result), [{"a": 1, "a_2": 2, "a_3": 3}])
        self.assertIn("csv_duplicate_header", codes(result))

    def test_empty_header_cell_gets_generated_name(self):
        result = csv_to_json("a,,c\n1,2,3")
        self.assertEqual(result.meta["columns"], ["a", "column2", "c"])
        self.assertIn("csv_empty_header", codes(result))

    def test_crlf_and_blank_lines(self):
        result = csv_to_json("a,b\r\n1,2\r\n\r\n3,4\r\n")
        self.assertEqual(result.meta["rows"], 2)
        self.assertIn("csv_blank_rows", codes(result))

    def test_unicode_round_trip(self):
        result = csv_to_json("中文,emoji\n值,😀")
        self.assertEqual(value_of(result), [{"中文": "值", "emoji": "😀"}])

    def test_bytes_input_is_decoded(self):
        result = csv_to_json("a\n1".encode("utf-8"))
        self.assertEqual(value_of(result), [{"a": 1}])

    def test_none_input_is_an_error(self):
        result = csv_to_json(None)
        self.assertFalse(result.ok)
        self.assertIn("csv_empty_input", result.error)


# --------------------------------------------------------------------------- #
# CSV 往返
# --------------------------------------------------------------------------- #
class TestCsvRoundTrip(unittest.TestCase):
    def test_flat_data_round_trips(self):
        data = V(FLAT_JSON)
        exported = json_to_csv(data)
        self.assertTrue(exported.ok, exported.error)
        imported = csv_to_json(exported.output)
        self.assertTrue(imported.ok, imported.error)
        self.assertTrue(json_equal(data, value_of(imported)), imported.output)

    def test_round_trip_keeps_number_literals_strictly(self):
        data = V('[{"a":1.230,"b":1e5,"c":-0,"d":%d,"e":0.000}]' % BIG_INT)
        exported = json_to_csv(data)
        imported = csv_to_json(exported.output)
        self.assertTrue(json_equal(data, value_of(imported), strict_numbers=True), imported.output)

    def test_round_trip_with_unicode_and_quotes(self):
        data = V('[{"中文":"值,带逗号","emoji":"😀\\n换行","q":"他说\\"好\\""}]')
        exported = json_to_csv(data)
        imported = csv_to_json(exported.output)
        self.assertTrue(json_equal(data, value_of(imported)), imported.output)

    def test_round_trip_with_semicolon_delimiter(self):
        data = V('[{"a":"x;y","b":1}]')
        exported = json_to_csv(data, CsvOptions(delimiter=";"))
        imported = csv_to_json(exported.output, delimiter=";")
        self.assertTrue(json_equal(data, value_of(imported)), imported.output)

    def test_round_trip_without_header_uses_generated_columns(self):
        data = V('[{"a":1,"b":2}]')
        exported = json_to_csv(data, CsvOptions(include_header=False))
        imported = csv_to_json(exported.output, has_header=False)
        self.assertEqual(value_of(imported), [{"column1": 1, "column2": 2}])


# --------------------------------------------------------------------------- #
# JSON -> XML
# --------------------------------------------------------------------------- #
class TestJsonToXml(unittest.TestCase):
    def test_object_keys_become_child_elements(self):
        result = json_to_xml(V('{"a":1,"b":"x"}'), XmlOptions(pretty=False, declaration=False))
        self.assertEqual(result.output, "<root><a>1</a><b>x</b></root>\n")

    def test_declaration_and_pretty_default(self):
        result = json_to_xml(V('{"a":1}'))
        self.assertTrue(result.output.startswith('<?xml version="1.0" encoding="UTF-8"?>\n'))
        self.assertEqual(result.output, '<?xml version="1.0" encoding="UTF-8"?>\n<root>\n  <a>1</a>\n</root>\n')

    def test_declaration_can_be_disabled(self):
        result = json_to_xml(V('{"a":1}'), XmlOptions(declaration=False, pretty=False))
        self.assertEqual(result.output, "<root><a>1</a></root>\n")
        self.assertNotIn("<?xml", result.output)

    def test_indent_option(self):
        result = json_to_xml(V('{"a":{"b":1}}'), XmlOptions(indent="\t", declaration=False))
        self.assertIn("\n\t<a>\n\t\t<b>1</b>\n\t</a>\n", result.output)

    def test_root_and_item_name_options(self):
        result = json_to_xml(V('[1,2]'), XmlOptions(root_name="doc", item_name="row",
                                                    pretty=False, declaration=False))
        self.assertTrue(result.output.startswith('<doc xmlns:json='))
        self.assertIn("<row>1</row><row>2</row>", result.output)

    def test_attribute_prefix(self):
        result = json_to_xml(V('{"a":{"@id":7,"b":1}}'), XmlOptions(pretty=False, declaration=False))
        self.assertEqual(result.output, '<root><a id="7"><b>1</b></a></root>\n')

    def test_custom_attribute_prefix(self):
        result = json_to_xml(V('{"a":{"$id":7}}'),
                            XmlOptions(attr_prefix="$", pretty=False, declaration=False))
        self.assertEqual(result.output, '<root><a id="7"/></root>\n')

    def test_text_key(self):
        result = json_to_xml(V('{"a":{"@id":7,"#text":"hi"}}'), XmlOptions(pretty=False, declaration=False))
        self.assertEqual(result.output, '<root><a id="7">hi</a></root>\n')

    def test_custom_text_key(self):
        result = json_to_xml(V('{"a":{"$":"hi","b":1}}'),
                            XmlOptions(text_key="$", pretty=False, declaration=False))
        self.assertEqual(result.output, '<root><a>hi<b>1</b></a></root>\n')

    def test_arrays_repeat_the_parent_key(self):
        result = json_to_xml(V('{"tags":["a","b","c"]}'), XmlOptions(pretty=False, declaration=False))
        self.assertEqual(result.output, "<root><tags>a</tags><tags>b</tags><tags>c</tags></root>\n")

    def test_single_element_array_needs_a_marker(self):
        result = json_to_xml(V('{"a":[7]}'), XmlOptions(pretty=False, declaration=False))
        self.assertIn('json:type="array"', result.output)
        self.assertIn("<item>7</item>", result.output)

    def test_empty_array_marker(self):
        result = json_to_xml(V('{"a":[]}'), XmlOptions(pretty=False, declaration=False))
        self.assertEqual(result.output,
                         '<root xmlns:json="https://jsonworkbench.dev/ns/json">'
                         '<a json:type="array"/></root>\n')

    def test_root_array(self):
        result = json_to_xml(V('[{"a":1},{"a":2}]'), XmlOptions(pretty=False, declaration=False))
        self.assertIn('json:type="array"', result.output)
        self.assertEqual(result.output.count("<item>"), 2)

    def test_nested_arrays(self):
        result = json_to_xml(V('{"a":[[1,2],[3]]}'))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(value_of(xml_to_json(result.output)), {"a": [[1, 2], [3]]})

    def test_null_uses_xsi_nil(self):
        result = json_to_xml(V('{"a":null}'), XmlOptions(pretty=False, declaration=False))
        self.assertIn('xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"', result.output)
        self.assertIn('<a xsi:nil="true"/>', result.output)

    def test_empty_object_marker(self):
        result = json_to_xml(V('{"a":{}}'), XmlOptions(pretty=False, declaration=False))
        self.assertIn('<a json:type="object"/>', result.output)

    def test_namespaces_are_declared_only_when_needed(self):
        plain = json_to_xml(V('{"a":1}'), XmlOptions(pretty=False, declaration=False))
        self.assertNotIn("xmlns", plain.output)
        self.assertEqual(plain.meta["namespaces"], [])

    def test_name_sanitization_leading_digit_and_illegal_chars(self):
        result = json_to_xml(V('{"1abc":1,"a b":2,"a/b":3,"a:b":4}'), XmlOptions(declaration=False))
        self.assertIn('<_abc json:name="1abc">1</_abc>', result.output)
        self.assertIn('<a_b json:name="a b">2</a_b>', result.output)
        self.assertIn('json:name="a/b"', result.output)
        self.assertIn('json:name="a:b"', result.output)
        self.assertIn("xml_name_sanitized", codes(result))

    def test_name_sanitization_conflict_gets_suffix(self):
        result = json_to_xml(V('{"a b":1,"a_b":2}'), XmlOptions(declaration=False))
        self.assertIn("xml_name_conflict", codes(result))
        self.assertIn('<a_b json:name="a b">1</a_b>', result.output)
        self.assertIn('<a_b_2 json:name="a_b">2</a_b_2>', result.output)

    def test_dash_and_dot_are_legal_name_characters(self):
        result = json_to_xml(V('{"a-b":1,"c.d":2}'), XmlOptions(pretty=False, declaration=False))
        self.assertEqual(result.output, "<root><a-b>1</a-b><c.d>2</c.d></root>\n")
        self.assertEqual(codes(result), [])

    def test_empty_key_becomes_element_with_original_name(self):
        result = json_to_xml(V('{"":1}'), XmlOptions(pretty=False, declaration=False))
        self.assertIn('<element json:name="">1</element>', result.output)

    def test_xml_prefixed_name_is_escaped(self):
        result = json_to_xml(V('{"xmlLang":1}'), XmlOptions(pretty=False, declaration=False))
        self.assertIn("<_xmlLang", result.output)

    def test_unicode_element_names_are_kept(self):
        result = json_to_xml(V('{"中文":"值"}'), XmlOptions(pretty=False, declaration=False))
        self.assertEqual(result.output, "<root><中文>值</中文></root>\n")
        self.assertEqual(codes(result), [])

    def test_text_escaping(self):
        result = json_to_xml(V('{"a":"<&>\\"\'"}'), XmlOptions(pretty=False, declaration=False))
        self.assertIn("<a>&lt;&amp;&gt;\"'</a>", result.output)

    def test_carriage_return_is_escaped_as_char_reference(self):
        result = json_to_xml(V('{"a":"x\\ry"}'), XmlOptions(pretty=False, declaration=False))
        self.assertIn("<a>x&#13;y</a>", result.output)

    def test_illegal_control_character_is_replaced(self):
        result = json_to_xml(V('{"a":"x\\u0007y"}'), XmlOptions(pretty=False, declaration=False))
        self.assertIn("xml_illegal_char", codes(result))
        self.assertIn("\ufffd", result.output)

    def test_non_scalar_attribute_becomes_json_text(self):
        result = json_to_xml(V('{"a":{"@x":{"y":1}}}'), XmlOptions(pretty=False, declaration=False))
        self.assertIn("xml_attr_not_scalar", codes(result))
        self.assertIn('x="{&quot;y&quot;:1}"', result.output)

    def test_null_attribute_becomes_empty(self):
        result = json_to_xml(V('{"a":{"@x":null}}'), XmlOptions(pretty=False, declaration=False))
        self.assertIn("xml_attr_null", codes(result))
        self.assertIn('x=""', result.output)

    def test_duplicate_attribute_after_sanitization_is_skipped(self):
        result = json_to_xml(V('{"a":{"@x y":1,"@x_y":2}}'), XmlOptions(pretty=False, declaration=False))
        self.assertIn("xml_attr_conflict", codes(result))
        self.assertEqual(result.output.count("x_y="), 1)
        self.assertIn('x_y="1"', result.output)

    def test_number_literals_survive(self):
        result = json_to_xml(V('{"a":1.230,"b":1e5,"c":-0,"d":%d}' % BIG_INT),
                            XmlOptions(pretty=False, declaration=False))
        self.assertIn("<a>1.230</a>", result.output)
        self.assertIn("<b>1e5</b>", result.output)
        self.assertIn("<c>-0</c>", result.output)
        self.assertIn("<d>%d</d>" % BIG_INT, result.output)

    def test_numeric_looking_string_gets_string_marker(self):
        result = json_to_xml(V('{"a":"123","b":"true","c":"x"}'), XmlOptions(pretty=False, declaration=False))
        self.assertIn('<a json:type="string">123</a>', result.output)
        self.assertIn('<b json:type="string">true</b>', result.output)
        self.assertIn("<c>x</c>", result.output)

    def test_empty_and_whitespace_strings_get_string_marker(self):
        result = json_to_xml(V('{"a":"","b":"  "}'), XmlOptions(pretty=False, declaration=False))
        self.assertEqual(result.output.count('json:type="string"'), 2)

    def test_empty_inputs(self):
        for data, fragment in (({}, 'json:type="object"'), ([], 'json:type="array"')):
            with self.subTest(data=data):
                result = json_to_xml(data, XmlOptions(pretty=False, declaration=False))
                self.assertTrue(result.ok, result.error)
                self.assertIn(fragment, result.output)
        self.assertTrue(json_to_xml(None).ok)
        self.assertIn('xsi:nil="true"', json_to_xml(None).output)
        self.assertTrue(json_to_xml("").ok)
        self.assertIn('json:type="string"', json_to_xml("").output)

    def test_input_is_not_mutated(self):
        data = V('{"a":[1,2],"b":{"c":null},"d":"x"}')
        snapshot = minify(data)
        json_to_xml(data)
        self.assertEqual(minify(data), snapshot)

    def test_meta_reports_element_count(self):
        result = json_to_xml(V('{"a":1,"b":[1,2]}'))
        self.assertEqual(result.meta["elements"], 4)
        self.assertEqual(result.meta["root"], "root")

    def test_root_name_is_sanitized(self):
        result = json_to_xml(V('{"a":1}'), XmlOptions(root_name="9lives", pretty=False, declaration=False))
        self.assertEqual(result.output, "<_lives><a>1</a></_lives>\n")
        self.assertEqual(result.meta["root"], "_lives")


# --------------------------------------------------------------------------- #
# XML -> JSON
# --------------------------------------------------------------------------- #
class TestXmlToJson(unittest.TestCase):
    def test_simple_elements(self):
        result = xml_to_json("<root><a>1</a><b>x</b></root>")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(value_of(result), {"a": 1, "b": "x"})

    def test_attributes_use_prefix(self):
        result = xml_to_json('<root a="1" b="x"/>')
        self.assertEqual(value_of(result), {"@a": 1, "@b": "x"})

    def test_custom_attr_prefix_and_text_key(self):
        result = xml_to_json('<root><a id="1">t</a></root>', attr_prefix="$", text_key="_")
        self.assertEqual(value_of(result), {"a": {"$id": 1, "_": "t"}})

    def test_mixed_text_and_children(self):
        result = xml_to_json('<root><a id="1">text<b>2</b></a></root>')
        self.assertEqual(value_of(result), {"a": {"@id": 1, "#text": "text", "b": 2}})

    def test_repeated_elements_collapse_into_arrays(self):
        result = xml_to_json("<root><a>1</a><a>2</a><a>3</a></root>")
        self.assertEqual(value_of(result), {"a": [1, 2, 3]})

    def test_single_element_stays_scalar(self):
        result = xml_to_json("<root><a>1</a></root>")
        self.assertEqual(value_of(result), {"a": 1})

    def test_root_as_array(self):
        result = xml_to_json("<root><a>1</a><b>2</b></root>", root_as_array=True)
        self.assertEqual(value_of(result), [1, 2])
        result = xml_to_json("<root><a>1</a></root>", root_as_array=True)
        self.assertEqual(value_of(result), [1])

    def test_root_as_array_drops_root_attributes_with_warning(self):
        result = xml_to_json('<root id="1"><a>2</a></root>', root_as_array=True)
        self.assertEqual(value_of(result), [2])
        self.assertIn("xml_root_content_dropped", codes(result))

    def test_infer_types_off_keeps_strings(self):
        result = xml_to_json("<root><a>1</a><b>true</b></root>", infer_types=False)
        self.assertEqual(value_of(result), {"a": "1", "b": "true"})

    def test_infer_types_preserves_literals(self):
        result = xml_to_json("<root><a>1.230</a><b>1e5</b><c>-0</c><d>%d</d></root>" % BIG_INT)
        parsed = value_of(result)
        self.assertEqual(number_literal(parsed["a"]), "1.230")
        self.assertEqual(number_literal(parsed["b"]), "1e5")
        self.assertEqual(number_literal(parsed["c"]), "-0")
        self.assertEqual(parsed["d"], BIG_INT)

    def test_nil_becomes_null(self):
        xml = ('<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
               '<a xsi:nil="true"/></root>')
        self.assertEqual(value_of(xml_to_json(xml)), {"a": None})

    def test_json_type_array_marker(self):
        xml = ('<root xmlns:json="https://jsonworkbench.dev/ns/json">'
               '<a json:type="array"><item>1</item></a>'
               '<b json:type="array"/></root>')
        self.assertEqual(value_of(xml_to_json(xml)), {"a": [1], "b": []})

    def test_json_name_marker_restores_key(self):
        xml = ('<root xmlns:json="https://jsonworkbench.dev/ns/json">'
               '<a_b json:name="a b">1</a_b></root>')
        self.assertEqual(value_of(xml_to_json(xml)), {"a b": 1})

    def test_json_type_string_marker_beats_inference(self):
        xml = ('<root xmlns:json="https://jsonworkbench.dev/ns/json">'
               '<a json:type="string">123</a></root>')
        self.assertEqual(value_of(xml_to_json(xml)), {"a": "123"})

    def test_json_type_object_marker(self):
        xml = ('<root xmlns:json="https://jsonworkbench.dev/ns/json">'
               '<a json:type="object"/></root>')
        self.assertEqual(value_of(xml_to_json(xml)), {"a": {}})

    def test_empty_leaf_is_empty_string(self):
        self.assertEqual(value_of(xml_to_json("<root><a/><b></b></root>")), {"a": "", "b": ""})

    def test_whitespace_only_leaf_is_folded(self):
        self.assertEqual(value_of(xml_to_json("<root><a>   </a></root>")), {"a": ""})

    def test_layout_whitespace_between_children_is_ignored(self):
        result = xml_to_json("<root>\n  <a>1</a>\n  <b>2</b>\n</root>")
        self.assertEqual(value_of(result), {"a": 1, "b": 2})

    def test_empty_root(self):
        self.assertEqual(value_of(xml_to_json("<root/>")), {})
        self.assertEqual(value_of(xml_to_json("<root/>", root_as_array=True)), [])

    def test_cdata_is_text(self):
        result = xml_to_json("<root><a><![CDATA[<b>&c</b>]]></a></root>")
        self.assertEqual(value_of(result), {"a": "<b>&c</b>"})

    def test_comments_are_ignored(self):
        result = xml_to_json("<root><!-- hi --><a>1</a></root>")
        self.assertEqual(value_of(result), {"a": 1})

    def test_namespaces_are_dropped_with_warning(self):
        result = xml_to_json('<root xmlns="http://x" xmlns:p="http://y"><a>1</a><p:b>2</p:b></root>')
        self.assertEqual(value_of(result), {"a": 1, "b": 2})
        self.assertIn("xml_namespace_dropped", codes(result))

    def test_tail_text_is_dropped_with_warning(self):
        result = xml_to_json("<root><a>1</a>tail</root>")
        self.assertEqual(value_of(result), {"a": 1})
        self.assertIn("xml_tail_text_dropped", codes(result))

    def test_carriage_return_char_reference_survives(self):
        result = xml_to_json("<root><a>x&#13;y</a></root>")
        self.assertEqual(value_of(result), {"a": "x\ry"})

    def test_doctype_is_rejected(self):
        result = xml_to_json('<!DOCTYPE r [<!ENTITY a "boom">]><r>&a;</r>')
        self.assertFalse(result.ok)
        self.assertIn("xml_doctype_not_allowed", result.error)

    def test_billion_laughs_is_rejected_quickly(self):
        payload = ('<?xml version="1.0"?>\n<!DOCTYPE lolz [\n'
                   + ''.join(' <!ENTITY lol%d "%s">\n' % (i, "&lol%d;" % (i - 1) * 10)
                             for i in range(1, 9)).replace('&lol0;', 'lol')
                   + ']>\n<lolz>&lol8;</lolz>')
        started = time.time()
        result = xml_to_json(payload)
        self.assertFalse(result.ok)
        self.assertIn("xml_doctype_not_allowed", result.error)
        self.assertLess(time.time() - started, 5.0)

    def test_external_entity_is_rejected(self):
        payload = '<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r>&x;</r>'
        result = xml_to_json(payload)
        self.assertFalse(result.ok)
        self.assertIn("xml_doctype_not_allowed", result.error)

    def test_undefined_entity_does_not_crash(self):
        result = xml_to_json("<root>&nope;</root>")
        self.assertFalse(result.ok)
        self.assertIn("xml_parse_error", result.error)

    def test_malformed_xml_reports_position(self):
        result = xml_to_json("<root><a></root>")
        self.assertFalse(result.ok)
        self.assertIn("xml_parse_error", result.error)
        self.assertIn("line 1", result.error)

    def test_empty_and_non_xml_input(self):
        for text in ("", "   ", None):
            with self.subTest(text=repr(text)):
                result = xml_to_json(text)
                self.assertFalse(result.ok)
                self.assertIn("xml_empty_input", result.error)
        self.assertIn("xml_parse_error", xml_to_json("not xml at all").error)
        self.assertFalse(xml_to_json("<root>").ok)

    def test_bytes_input_with_bom(self):
        result = xml_to_json("\ufeff<root><a>1</a></root>".encode("utf-8"))
        self.assertEqual(value_of(result), {"a": 1})

    def test_empty_attr_prefix(self):
        result = xml_to_json('<root a="1"><b>2</b></root>', attr_prefix="")
        self.assertEqual(value_of(result), {"a": 1, "b": 2})

    def test_attribute_and_child_key_conflict(self):
        result = xml_to_json('<root><a id="1"><id>2</id></a></root>', attr_prefix="")
        self.assertEqual(value_of(result), {"a": {"id": 2}})
        self.assertIn("xml_key_conflict", codes(result))

    def test_user_xmlns_key_does_not_break_the_document(self):
        exported = json_to_xml(V('{"a":{"@xmlns":"x","b":1}}'),
                              XmlOptions(pretty=False, declaration=False))
        self.assertIn("_xmlns", exported.output)
        imported = xml_to_json(exported.output)
        self.assertTrue(imported.ok, imported.error)
        self.assertEqual(value_of(imported), {"a": {"@_xmlns": "x", "b": 1}})

    def test_meta_reports_depth(self):
        result = xml_to_json("<root><a><b>1</b></a></root>")
        self.assertEqual(result.meta["elements"], 3)
        self.assertEqual(result.meta["max_depth"], 3)

    def test_random_binary_text_never_raises(self):
        import random

        rng = random.Random(20260104)
        alphabet = '<>/="&abc \n\t0123456789?!DOCTYPE'
        for _ in range(200):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
            result = xml_to_json(text)
            self.assertIsInstance(result.ok, bool)
            if not result.ok:
                self.assertTrue(result.error)


# --------------------------------------------------------------------------- #
# XML 往返
# --------------------------------------------------------------------------- #
class TestXmlRoundTrip(unittest.TestCase):
    SAMPLE = ('{"id":1,"name":"张三 😀","price":1.230,"exp":1e5,"neg":-0,"big":%d,'
              '"tags":["a","b"],"single":[7],"empty":[],"obj":{},"nil":null,'
              '"flag":true,"text":"hello","numstr":"123",'
              '"@attr":"v","mixed":{"#text":"t","b":1},'
              '"nested":{"deep":{"deeper":[1,2,{"x":null}]}}}' % BIG_INT)

    def test_round_trip_is_semantically_identical(self):
        data = V(self.SAMPLE)
        exported = json_to_xml(data)
        self.assertTrue(exported.ok, exported.error)
        imported = xml_to_json(exported.output)
        self.assertTrue(imported.ok, imported.error)
        self.assertTrue(json_equal(data, value_of(imported), strict_numbers=True), imported.output)

    def test_round_trip_of_root_array(self):
        data = V('[{"a":1},{"b":[1,2]},"x",null,true]')
        exported = json_to_xml(data)
        imported = xml_to_json(exported.output, root_as_array=True)
        self.assertTrue(json_equal(data, value_of(imported), strict_numbers=True), imported.output)

    def test_round_trip_of_root_scalars(self):
        for text in ('"hi"', "42", "1.230", "true", "null", '""'):
            with self.subTest(text=text):
                exported = json_to_xml(V(text))
                imported = xml_to_json(exported.output)
                self.assertTrue(json_equal(V(text), value_of(imported), strict_numbers=True),
                                imported.output)

    def test_round_trip_with_custom_names(self):
        data = V('{"a":[1,2],"b":{"@id":3,"#text":"t"}}')
        exported = json_to_xml(data, XmlOptions(root_name="doc", item_name="row",
                                                attr_prefix="$", text_key="_"))
        imported = xml_to_json(exported.output, attr_prefix="$", text_key="_")
        self.assertTrue(json_equal(data, value_of(imported), strict_numbers=True), imported.output)

    def test_round_trip_of_unicode_keys(self):
        data = V('{"中文":{"键":"值 😀"},"emoji":"🎉"}')
        exported = json_to_xml(data)
        imported = xml_to_json(exported.output)
        self.assertTrue(json_equal(data, value_of(imported), strict_numbers=True), imported.output)

    def test_round_trip_of_sanitized_names(self):
        data = V('{"1abc":1,"a b":2,"":3,"xml:x":4}')
        exported = json_to_xml(data)
        imported = xml_to_json(exported.output)
        self.assertTrue(json_equal(data, value_of(imported), strict_numbers=True), imported.output)

    def test_compact_and_pretty_output_parse_the_same(self):
        data = V(self.SAMPLE)
        pretty = xml_to_json(json_to_xml(data).output)
        compact = xml_to_json(json_to_xml(data, XmlOptions(pretty=False, declaration=False)).output)
        self.assertTrue(json_equal(value_of(pretty), value_of(compact), strict_numbers=True))

    def test_input_is_not_mutated(self):
        data = V(self.SAMPLE)
        snapshot = minify(data)
        json_to_xml(data)
        self.assertEqual(minify(data), snapshot)


# --------------------------------------------------------------------------- #
# JSON <-> YAML
# --------------------------------------------------------------------------- #
class TestJsonToYaml(unittest.TestCase):
    def test_basic_output(self):
        result = json_to_yaml(V('{"a":1,"b":[1,2],"c":null,"d":true}'))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.output, "a: 1\nb:\n- 1\n- 2\nc: null\nd: true\n")

    def test_number_literals_are_written_verbatim(self):
        result = json_to_yaml(V('{"a":1.230,"b":%d,"c":0.000}' % BIG_INT))
        self.assertIn("a: 1.230", result.output)
        self.assertIn("b: %d" % BIG_INT, result.output)
        self.assertIn("c: 0.000", result.output)

    def test_exponent_literal_keeps_its_text_with_explicit_tag(self):
        result = json_to_yaml(V('{"a":1e5}'))
        self.assertIn("1e5", result.output)
        self.assertIn("!!float", result.output)
        self.assertIn("yaml_literal_tagged", codes(result))

    def test_negative_zero_keeps_its_literal(self):
        result = json_to_yaml(V('{"a":-0}'))
        self.assertIn("a: -0", result.output)
        self.assertIn("yaml_negative_zero", codes(result))

    def test_precision_warning_for_high_precision_decimal(self):
        result = json_to_yaml(V('{"a":3.141592653589793238462643383279}'))
        self.assertIn("yaml_number_precision", codes(result))

    def test_precision_warning_for_overflowing_exponent(self):
        result = json_to_yaml(V('{"a":1e400}'))
        self.assertIn("yaml_number_precision", codes(result))

    def test_no_precision_warning_for_plain_values(self):
        result = json_to_yaml(V('{"a":1.230,"b":42,"c":1.5}'))
        self.assertEqual(codes(result), [])

    def test_unicode_with_allow_unicode(self):
        result = json_to_yaml(V('{"中文":"值 😀"}'), YamlOptions(allow_unicode=True))
        self.assertIn("中文: 值 😀", result.output)

    def test_unicode_without_allow_unicode_is_escaped(self):
        result = json_to_yaml(V('{"中文":"值"}'), YamlOptions(allow_unicode=False))
        self.assertNotIn("中文", result.output)
        self.assertIn("\\u", result.output)
        self.assertEqual(yaml.safe_load(result.output), {"中文": "值"})

    def test_sort_keys_option(self):
        result = json_to_yaml(V('{"z":1,"a":2}'), YamlOptions(sort_keys=True))
        self.assertEqual(result.output, "a: 2\nz: 1\n")
        result = json_to_yaml(V('{"z":1,"a":2}'), YamlOptions(sort_keys=False))
        self.assertEqual(result.output, "z: 1\na: 2\n")

    def test_indent_option(self):
        result = json_to_yaml(V('{"a":{"b":1}}'), YamlOptions(indent=4))
        self.assertIn("a:\n    b: 1\n", result.output)

    def test_bad_indent_falls_back(self):
        result = json_to_yaml(V('{"a":1}'), YamlOptions(indent=99))
        self.assertTrue(result.ok, result.error)
        self.assertIn("yaml_bad_indent", codes(result))
        self.assertEqual(result.meta["indent"], 2)

    def test_width_option_prevents_wrapping(self):
        long_text = " ".join("word%d" % i for i in range(60))
        result = json_to_yaml(V('{"a":"%s"}' % long_text), YamlOptions(width=4096))
        self.assertEqual(len(result.output.splitlines()), 1)
        narrow = json_to_yaml(V('{"a":"%s"}' % long_text), YamlOptions(width=40))
        self.assertGreater(len(narrow.output.splitlines()), 1)

    def test_bad_width_falls_back(self):
        result = json_to_yaml(V('{"a":1}'), YamlOptions(width=1))
        self.assertIn("yaml_bad_width", codes(result))
        self.assertEqual(result.meta["width"], 4096)

    def test_no_python_specific_tags(self):
        result = json_to_yaml(V('{"a":1,"b":[1,{"c":null}],"d":"x"}'))
        self.assertNotIn("python", result.output)
        self.assertNotIn("!!", result.output)

    def test_strings_that_look_like_other_types_are_quoted(self):
        result = json_to_yaml(V('{"a":"123","b":"true","c":"null","d":"1.5","e":"x"}'))
        for line in result.output.splitlines():
            if line.startswith(("a:", "b:", "c:", "d:")):
                self.assertRegex(line, r"['\"]")
        self.assertEqual(yaml.safe_load(result.output),
                         {"a": "123", "b": "true", "c": "null", "d": "1.5", "e": "x"})

    def test_empty_containers(self):
        self.assertEqual(json_to_yaml({}).output, "{}\n")
        self.assertEqual(json_to_yaml([]).output, "[]\n")
        self.assertIn("a: {}", json_to_yaml(V('{"a":{},"b":[]}')).output)

    def test_top_level_scalars(self):
        self.assertEqual(json_to_yaml(None).output, "null\n...\n")
        self.assertIn("42", json_to_yaml(42).output)
        self.assertIn("hi", json_to_yaml("hi").output)
        self.assertEqual(json_to_yaml("").output, "''\n")
        self.assertEqual(json_to_yaml(True).output, "true\n...\n")

    def test_depth_limit_reports_clear_error(self):
        result = json_to_yaml(nested_object(YAML_MAX_DEPTH + 50))
        self.assertFalse(result.ok)
        self.assertIn("yaml_depth_exceeded", result.error)
        self.assertIn(str(YAML_MAX_DEPTH), result.error)

    def test_depth_just_under_the_limit_works(self):
        result = json_to_yaml(nested_object(YAML_MAX_DEPTH - 50))
        self.assertTrue(result.ok, result.error)

    def test_input_is_not_mutated(self):
        data = V('{"a":[1.230,{"b":null}],"c":"中文"}')
        snapshot = minify(data)
        json_to_yaml(data)
        self.assertEqual(minify(data), snapshot)

    def test_float_and_decimal_inputs_are_handled(self):
        from decimal import Decimal

        result = json_to_yaml({"a": 1.5, "b": Decimal("2.250")})
        self.assertTrue(result.ok, result.error)
        self.assertIn("a: 1.5", result.output)
        self.assertIn("b: 2.250", result.output)

    def test_non_finite_float_is_written_as_null_with_warning(self):
        result = json_to_yaml({"a": float("inf")})
        self.assertTrue(result.ok, result.error)
        self.assertIn("null", result.output)
        self.assertIn("number_not_finite", codes(result))

    def test_unsupported_type_is_reported(self):
        result = json_to_yaml({"a": {1, 2}, "b": object()})
        self.assertTrue(result.ok, result.error)
        self.assertIn("yaml_unsupported_type", codes(result))


class TestYamlToJson(unittest.TestCase):
    def test_basic(self):
        result = yaml_to_json("a: 1\nb:\n  - x\n  - y\n")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(value_of(result), {"a": 1, "b": ["x", "y"]})

    def test_output_is_pretty_json(self):
        result = yaml_to_json("a: 1")
        self.assertEqual(result.output, '{\n  "a": 1\n}')

    def test_literals_are_preserved(self):
        result = yaml_to_json("a: 1.230\nb: 1.0e+5\nc: -0\nd: %d\n" % BIG_INT)
        parsed = value_of(result)
        self.assertEqual(number_literal(parsed["a"]), "1.230")
        self.assertEqual(number_literal(parsed["b"]), "1.0e+5")
        self.assertEqual(number_literal(parsed["c"]), "-0")
        self.assertEqual(parsed["d"], BIG_INT)
        self.assertIsInstance(parsed["d"], int)

    def test_yaml_booleans_and_null(self):
        parsed = value_of(yaml_to_json("a: true\nb: false\nc: null\nd: ~\ne: yes\n"))
        self.assertIs(parsed["a"], True)
        self.assertIs(parsed["b"], False)
        self.assertIsNone(parsed["c"])
        self.assertIsNone(parsed["d"])
        self.assertIs(parsed["e"], True)  # YAML 1.1 规则

    def test_python_tag_is_rejected(self):
        for payload in ("a: !!python/object/apply:os.system ['ls']",
                        "!!python/name:os.getcwd ''",
                        "a: !!python/object:x {}"):
            with self.subTest(payload=payload):
                result = yaml_to_json(payload)
                self.assertFalse(result.ok)
                self.assertIn("yaml_unsafe_tag", result.error)

    def test_python_looking_text_is_rejected_conservatively(self):
        # 预检是纯文本扫描：正文里出现 !!python/ 字样的普通字符串同样被拒
        result = yaml_to_json('a: "!!python/object:x"\n')
        self.assertFalse(result.ok)
        self.assertIn("yaml_unsafe_tag", result.error)

    def test_unsafe_tag_is_rejected_even_without_the_prefilter(self):
        # 就算绕过文本预检，SafeLoader 也不会构造 python 对象
        result = yaml_to_json("a: !SomeUnknownTag {b: 1}")
        self.assertFalse(result.ok)
        self.assertIn("yaml_error", result.error)

    def test_multiple_documents_are_rejected(self):
        result = yaml_to_json("a: 1\n---\nb: 2\n")
        self.assertFalse(result.ok)
        self.assertIn("yaml_error", result.error)

    def test_empty_input_becomes_null_with_warning(self):
        for text in ("", "   ", "\n\n", None):
            with self.subTest(text=repr(text)):
                result = yaml_to_json(text)
                if text is None:
                    self.assertFalse(result.ok)
                    continue
                self.assertTrue(result.ok, result.error)
                self.assertEqual(result.output, "null")
                self.assertIn("yaml_empty_document", codes(result))

    def test_malformed_yaml_reports_error(self):
        for text in ("a: [1, 2", "{a: 1", "a:\n\tb: 1", "a: b: c", "\t- x"):
            with self.subTest(text=text):
                result = yaml_to_json(text)
                self.assertFalse(result.ok)
                self.assertTrue(result.error.startswith("yaml_"), result.error)

    def test_non_string_keys_are_stringified(self):
        result = yaml_to_json("2: a\ntrue: b\n2020-01-02: c\n")
        parsed = value_of(result)
        self.assertEqual(set(parsed.keys()), {"2", "true", "2020-01-02"})
        self.assertEqual(parsed["true"], "b")
        self.assertIn("yaml_non_string_key", codes(result))

    def test_timestamps_become_iso_strings(self):
        result = yaml_to_json("a: 2020-01-02\nb: 2020-01-02 03:04:05\n")
        parsed = value_of(result)
        self.assertEqual(parsed["a"], "2020-01-02")
        self.assertEqual(parsed["b"], "2020-01-02T03:04:05")
        self.assertIn("yaml_timestamp_to_string", codes(result))

    def test_binary_becomes_base64(self):
        result = yaml_to_json("a: !!binary aGk=\n")
        self.assertEqual(value_of(result), {"a": "aGk="})
        self.assertIn("yaml_binary_to_base64", codes(result))

    def test_set_becomes_array(self):
        result = yaml_to_json("a: !!set {x, y}\n")
        self.assertEqual(sorted(value_of(result)["a"]), ["x", "y"])
        self.assertIn("yaml_set_to_array", codes(result))

    def test_non_finite_becomes_null(self):
        result = yaml_to_json("a: .inf\nb: .nan\n")
        self.assertEqual(value_of(result), {"a": None, "b": None})
        self.assertIn("yaml_non_finite", codes(result))

    def test_yaml_only_int_literals_are_normalized_with_warning(self):
        result = yaml_to_json("a: 0x1F\nb: 1_000\n")
        self.assertEqual(value_of(result), {"a": 31, "b": 1000})
        self.assertIn("yaml_int_literal_normalized", codes(result))

    def test_anchors_and_aliases_are_expanded(self):
        result = yaml_to_json("a: &x {k: 1}\nb: *x\n")
        self.assertEqual(value_of(result), {"a": {"k": 1}, "b": {"k": 1}})

    def test_unicode_content(self):
        result = yaml_to_json("中文: 值 😀\n")
        self.assertEqual(value_of(result), {"中文": "值 😀"})

    def test_bytes_input(self):
        result = yaml_to_json("a: 1".encode("utf-8"))
        self.assertEqual(value_of(result), {"a": 1})

    def test_deep_document_is_accepted(self):
        text = "".join("  " * i + "a:\n" for i in range(200)) + "  " * 200 + "1\n"
        result = yaml_to_json(text)
        self.assertTrue(result.ok, result.error)
        # 200 层 "a:" -> 200 个嵌套对象，最内层容器深度记为 199
        self.assertEqual(result.meta["depth"], 199)

    def test_too_deep_document_reports_error_not_recursion(self):
        depth = 2000
        text = "".join("  " * i + "a:\n" for i in range(depth)) + "  " * depth + "1\n"
        result = yaml_to_json(text)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("yaml_too_deep")
                        or result.error.startswith("depth_exceeded"), result.error)

    def test_scalar_documents(self):
        self.assertEqual(value_of(yaml_to_json("42")), 42)
        self.assertEqual(value_of(yaml_to_json("hi")), "hi")
        self.assertEqual(value_of(yaml_to_json("- 1\n- 2")), [1, 2])


class TestYamlRoundTrip(unittest.TestCase):
    SAMPLE = ('{"a":1.230,"b":1e5,"c":-0,"d":%d,"e":"中文 😀","f":null,"g":[1,2,{"h":true}],'
              '"i":{},"j":[],"k":"123","l":"true","m":"","n":"1.5","o":1E+5,'
              '"p":{"q":{"r":[null,false,"x"]}}}' % BIG_INT)

    def test_round_trip_is_strictly_identical(self):
        data = V(self.SAMPLE)
        exported = json_to_yaml(data)
        self.assertTrue(exported.ok, exported.error)
        imported = yaml_to_json(exported.output)
        self.assertTrue(imported.ok, imported.error)
        self.assertTrue(json_equal(data, value_of(imported), strict_numbers=True), imported.output)

    def test_generic_yaml_reader_still_sees_the_same_values(self):
        # 通用读取器（yaml.safe_load）把数字读成 int/float，字面量必然丢失，
        # 因此这里只能用 strict_numbers=False 比较“值”，并且要先 to_plain。
        data = V(self.SAMPLE)
        exported = json_to_yaml(data)
        plain = yaml.safe_load(exported.output)
        self.assertTrue(json_equal(to_plain(data), plain, strict_numbers=False))

    def test_round_trip_with_sort_keys_and_indent(self):
        data = V(self.SAMPLE)
        exported = json_to_yaml(data, YamlOptions(sort_keys=True, indent=4, allow_unicode=False))
        imported = yaml_to_json(exported.output)
        self.assertTrue(json_equal(data, value_of(imported), strict_numbers=True), imported.output)

    def test_round_trip_of_scalars(self):
        for text in ('"hi"', "42", "1.230", "true", "false", "null", '""', "[]", "{}"):
            with self.subTest(text=text):
                exported = json_to_yaml(V(text))
                imported = yaml_to_json(exported.output)
                self.assertTrue(json_equal(V(text), value_of(imported), strict_numbers=True),
                                f"{text} -> {exported.output!r} -> {imported.output!r}")

    def test_round_trip_of_deep_nesting(self):
        data = nested_object(120)
        exported = json_to_yaml(data)
        self.assertTrue(exported.ok, exported.error)
        imported = yaml_to_json(exported.output)
        self.assertTrue(imported.ok, imported.error)
        self.assertTrue(json_equal(data, value_of(imported), strict_numbers=True))

    def test_input_is_not_mutated(self):
        data = V(self.SAMPLE)
        snapshot = minify(data)
        json_to_yaml(data)
        self.assertEqual(minify(data), snapshot)


# --------------------------------------------------------------------------- #
# 字符串转义
# --------------------------------------------------------------------------- #
class TestStringEscaping(unittest.TestCase):
    def test_escape_basics(self):
        self.assertEqual(escape_json_string('a"b'), 'a\\"b')
        self.assertEqual(escape_json_string("a\\b"), "a\\\\b")
        self.assertEqual(escape_json_string("a\nb\rc\td"), "a\\nb\\rc\\td")
        self.assertEqual(escape_json_string(""), "")

    def test_escape_control_chars_and_unicode(self):
        self.assertEqual(escape_json_string("\x07"), "\\u0007")
        self.assertEqual(escape_json_string("中文😀"), "中文😀")

    def test_escape_does_not_add_quotes(self):
        escaped = escape_json_string("x")
        self.assertFalse(escaped.startswith('"'))
        self.assertFalse(escaped.endswith('"'))

    def test_escape_coerces_non_strings(self):
        self.assertEqual(escape_json_string(42), "42")
        self.assertEqual(escape_json_string(None), "")

    def test_unescape_basics(self):
        self.assertEqual(unescape_json_string('a\\"b').output, 'a"b')
        self.assertEqual(unescape_json_string("a\\\\b").output, "a\\b")
        self.assertEqual(unescape_json_string("a\\nb").output, "a\nb")
        self.assertEqual(unescape_json_string("\\u4e2d\\ud83d\\ude00").output, "中😀")

    def test_unescape_strips_surrounding_quotes(self):
        self.assertEqual(unescape_json_string('"a\\nb"').output, "a\nb")
        self.assertEqual(unescape_json_string('"').output, '"')  # 单个引号不成对，原样返回

    def test_unescape_invalid_escape_is_an_error(self):
        for text in (r"a\x41", r"a\q", "a\\", r"\uZZZZ", r"\u12"):
            with self.subTest(text=text):
                result = unescape_json_string(text)
                self.assertFalse(result.ok)
                self.assertIn("invalid_escape", result.error)

    def test_unescape_empty(self):
        result = unescape_json_string("")
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "")
        self.assertFalse(result.meta["changed"])
        self.assertFalse(unescape_json_string(None).ok)

    def test_round_trip(self):
        samples = ["", "plain", 'quote"inside', "back\\slash", "new\nline\r\nCRLF",
                   "tab\there", "\x00\x07\x1f", "中文 😀 混合", "a/b"]
        for text in samples:
            with self.subTest(text=repr(text)):
                result = unescape_json_string(escape_json_string(text))
                self.assertTrue(result.ok, result.error)
                self.assertEqual(result.output, text)
                self.assertTrue(result.meta["changed"] or text == result.output)

    def test_escaped_output_is_valid_json_string_body(self):
        text = 'mixed "quotes", \\backslash, \n新行 😀'
        escaped = escape_json_string(text)
        parsed = parse_json('"' + escaped + '"')
        self.assertTrue(parsed.ok, parsed.error)
        self.assertEqual(parsed.value, text)


# --------------------------------------------------------------------------- #
# 拆分
# --------------------------------------------------------------------------- #
class TestSplitArray(unittest.TestCase):
    def test_split_by_chunk_size(self):
        result = split_array(V("[1,2,3,4,5]"), chunk_size=2)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.meta["pieces"], [[1, 2], [3, 4], [5]])
        self.assertEqual(result.meta["count"], 3)
        self.assertEqual(result.output, "[1,2]\n[3,4]\n[5]\n")

    def test_output_is_json_lines(self):
        result = split_array(V('[{"a":1.230},{"b":2}]'), chunk_size=1)
        lines = result.output.splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            parsed = parse_json(line)
            self.assertTrue(parsed.ok, parsed.error)
        self.assertIn("1.230", result.output)

    def test_chunk_size_larger_than_array(self):
        result = split_array(V("[1,2]"), chunk_size=10)
        self.assertEqual(result.meta["pieces"], [[1, 2]])
        self.assertEqual(result.meta["count"], 1)

    def test_empty_array(self):
        result = split_array([], chunk_size=3)
        self.assertTrue(result.ok)
        self.assertEqual(result.meta["pieces"], [])
        self.assertEqual(result.output, "")

    def test_missing_chunk_size_is_an_error(self):
        for size in (0, -1):
            with self.subTest(size=size):
                result = split_array(V("[1]"), chunk_size=size)
                self.assertFalse(result.ok)
                self.assertIn("split_bad_chunk_size", result.error)

    def test_non_array_is_an_error(self):
        for data in ({"a": 1}, "x", 42, None, True):
            with self.subTest(data=data):
                result = split_array(data, chunk_size=2)
                self.assertFalse(result.ok)
                self.assertIn("split_not_array", result.error)

    def test_split_by_field(self):
        data = V('[{"g":"x","v":1},{"g":"y","v":2},{"g":"x","v":3}]')
        result = split_array(data, by_field="g")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.meta["count"], 2)
        self.assertEqual(result.meta["keys"], ["x", "y"])
        self.assertEqual(len(result.meta["pieces"][0]), 2)
        self.assertEqual(len(result.meta["pieces"][1]), 1)
        self.assertEqual(result.meta["mode"], "field")

    def test_split_by_nested_field(self):
        data = V('[{"a":{"b":1}},{"a":{"b":2}},{"a":{"b":1}}]')
        result = split_array(data, by_field="a.b")
        self.assertEqual(result.meta["keys"], ["1", "2"])
        self.assertEqual(len(result.meta["pieces"][0]), 2)

    def test_split_by_field_with_missing_values(self):
        data = V('[{"g":"x"},{"z":1},{"g":"x"}]')
        result = split_array(data, by_field="g")
        self.assertEqual(result.meta["unkeyed"], 1)
        self.assertIn("split_missing_field", codes(result))
        self.assertEqual(len(result.meta["pieces"]), 2)
        self.assertEqual(result.meta["pieces"][1], [{"z": 1}])

    def test_split_by_field_groups_containers(self):
        data = V('[{"g":[1,2]},{"g":[1,2]},{"g":{"k":1}}]')
        result = split_array(data, by_field="g")
        self.assertEqual(result.meta["count"], 2)
        self.assertEqual(len(result.meta["pieces"][0]), 2)

    def test_chunk_size_and_by_field_together_prefers_field(self):
        result = split_array(V('[{"g":"x"},{"g":"y"}]'), chunk_size=1, by_field="g")
        self.assertEqual(result.meta["mode"], "field")
        self.assertEqual(result.meta["count"], 2)

    def test_input_is_not_mutated(self):
        data = V('[{"g":"x"},{"g":"y"}]')
        snapshot = minify(data)
        split_array(data, chunk_size=1)
        split_array(data, by_field="g")
        self.assertEqual(minify(data), snapshot)

    def test_deep_array(self):
        data = [nested_object(300) for _ in range(3)]
        result = split_array(data, chunk_size=2)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.meta["count"], 2)


# --------------------------------------------------------------------------- #
# 合并
# --------------------------------------------------------------------------- #
class TestMergeDocuments(unittest.TestCase):
    def test_deep_merge_of_objects(self):
        result = merge_documents([V('{"a":{"b":1},"x":1}'), V('{"a":{"c":2},"y":2}')])
        self.assertTrue(result.ok, result.error)
        self.assertEqual(value_of(result), {"a": {"b": 1, "c": 2}, "x": 1, "y": 2})
        self.assertEqual(result.meta["conflicts"], 0)

    def test_deep_merge_conflict_last_wins(self):
        result = merge_documents([V('{"a":1}'), V('{"a":2}')], conflict="last")
        self.assertEqual(value_of(result), {"a": 2})
        self.assertEqual(result.meta["conflicts"], 1)

    def test_deep_merge_conflict_first_wins(self):
        result = merge_documents([V('{"a":1}'), V('{"a":2}')], conflict="first")
        self.assertEqual(value_of(result), {"a": 1})

    def test_identical_values_are_not_conflicts(self):
        result = merge_documents([V('{"a":1}'), V('{"a":1}')], conflict="error")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.meta["conflicts"], 0)

    def test_literal_difference_is_a_conflict(self):
        result = merge_documents([V('{"a":1}'), V('{"a":1.0}')], conflict="error")
        self.assertFalse(result.ok)
        self.assertIn("merge_conflict", result.error)

    def test_conflict_error_names_the_nested_path(self):
        result = merge_documents([V('{"a":{"b":{"c":1}}}'), V('{"a":{"b":{"c":2}}}')],
                                 conflict="error")
        self.assertFalse(result.ok)
        self.assertIn("$.a.b.c", result.error)

    def test_conflict_error_at_the_root(self):
        result = merge_documents([V('1'), V('2')], conflict="error")
        self.assertFalse(result.ok)
        self.assertIn("'$'", result.error)

    def test_type_mismatch_is_a_conflict(self):
        result = merge_documents([V('{"a":{"b":1}}'), V('{"a":5}')], conflict="last")
        self.assertEqual(value_of(result), {"a": 5})
        self.assertEqual(result.meta["conflicts"], 1)

    def test_array_strategy_append(self):
        result = merge_documents([V('{"l":[1]}'), V('{"l":[2]}')], array_strategy="append")
        self.assertEqual(value_of(result), {"l": [1, 2]})

    def test_array_strategy_prepend(self):
        result = merge_documents([V('{"l":[1]}'), V('{"l":[2]}')], array_strategy="prepend")
        self.assertEqual(value_of(result), {"l": [2, 1]})

    def test_array_strategy_replace(self):
        result = merge_documents([V('{"l":[1,9]}'), V('{"l":[2]}')], array_strategy="replace")
        self.assertEqual(value_of(result), {"l": [2]})

    def test_shallow_merge_replaces_whole_values(self):
        result = merge_documents([V('{"a":{"b":1}}'), V('{"a":{"c":2}}')], strategy="shallow")
        self.assertEqual(value_of(result), {"a": {"c": 2}})
        self.assertEqual(result.meta["conflicts"], 1)

    def test_shallow_merge_keeps_disjoint_keys(self):
        result = merge_documents([V('{"a":1}'), V('{"b":2}')], strategy="shallow")
        self.assertEqual(value_of(result), {"a": 1, "b": 2})

    def test_shallow_merge_requires_objects(self):
        result = merge_documents([V('[1]'), V('[2]')], strategy="shallow")
        self.assertFalse(result.ok)
        self.assertIn("merge_shallow_requires_objects", result.error)

    def test_concat_merges_top_level_arrays(self):
        result = merge_documents([V('[1,2]'), V('[3]'), V('[]')], strategy="concat")
        self.assertEqual(value_of(result), [1, 2, 3])

    def test_concat_requires_arrays(self):
        result = merge_documents([V('[1]'), V('{"a":1}')], strategy="concat")
        self.assertFalse(result.ok)
        self.assertIn("merge_concat_requires_arrays", result.error)

    def test_three_documents(self):
        result = merge_documents([V('{"a":1}'), V('{"b":2}'), V('{"c":3}')])
        self.assertEqual(value_of(result), {"a": 1, "b": 2, "c": 3})

    def test_single_document_is_copied(self):
        data = V('{"a":[1,{"b":2}]}')
        result = merge_documents([data])
        self.assertEqual(value_of(result), data)
        result.meta["value"]["a"].append("mutated")
        self.assertEqual(len(data["a"]), 2)  # 结果是副本，改它不影响输入

    def test_empty_list_is_an_error(self):
        result = merge_documents([])
        self.assertFalse(result.ok)
        self.assertIn("merge_no_documents", result.error)

    def test_non_list_input_is_an_error(self):
        result = merge_documents({"a": 1})
        self.assertFalse(result.ok)
        self.assertIn("merge_not_a_list", result.error)

    def test_bad_option_values(self):
        self.assertIn("merge_bad_strategy", merge_documents([V('{}')], strategy="x").error)
        self.assertIn("merge_bad_array_strategy",
                      merge_documents([V('{}')], array_strategy="x").error)
        self.assertIn("merge_bad_conflict", merge_documents([V('{}')], conflict="x").error)

    def test_inputs_are_not_mutated(self):
        first = V('{"a":{"b":1},"l":[1]}')
        second = V('{"a":{"c":2},"l":[2]}')
        snapshot = minify(first) + "|" + minify(second)
        for strategy in ("deep", "shallow"):
            merge_documents([first, second], strategy=strategy)
        merge_documents([V('[1]'), V('[2]')], strategy="concat")
        self.assertEqual(minify(first) + "|" + minify(second), snapshot)

    def test_scalars_and_nulls_merge(self):
        self.assertEqual(value_of(merge_documents([None, V('{"a":1}')])), {"a": 1})
        # conflict="last"：后来的 null 覆盖前面的对象；"first" 则保留对象
        self.assertEqual(value_of(merge_documents([V('{"a":1}'), None])), None)
        self.assertEqual(value_of(merge_documents([V('{"a":1}'), None], conflict="first")),
                         {"a": 1})
        self.assertEqual(value_of(merge_documents([None, None])), None)

    def test_deep_nesting(self):
        result = merge_documents([nested_object(600, 1), nested_object(600, 2)])
        self.assertTrue(result.ok, result.error)
        node = value_of(result)
        for _ in range(600):
            node = node["a"]
        self.assertEqual(node, 2)

    def test_unicode_keys(self):
        result = merge_documents([V('{"中文":{"a":1}}'), V('{"中文":{"b":2}}')])
        self.assertEqual(value_of(result), {"中文": {"a": 1, "b": 2}})


# --------------------------------------------------------------------------- #
# 去重
# --------------------------------------------------------------------------- #
class TestDedupeArray(unittest.TestCase):
    def test_whole_element_dedupe(self):
        result = dedupe_array(V('[1,1,2,"a","a",{"x":1},{"x":1},[1],[1]]'))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(value_of(result), [1, 2, "a", {"x": 1}, [1]])
        self.assertEqual(result.meta["removed"], 4)
        self.assertEqual(result.meta["total"], 9)

    def test_key_order_does_not_matter(self):
        result = dedupe_array(V('[{"a":1,"b":2},{"b":2,"a":1}]'))
        self.assertEqual(result.meta["removed"], 1)

    def test_number_literals_are_distinguished(self):
        result = dedupe_array(V('[1,1.0,1.00]'))
        self.assertEqual(result.meta["removed"], 0)

    def test_types_are_distinguished(self):
        result = dedupe_array(V('[1,"1",true,[1],{"a":1}]'))
        self.assertEqual(result.meta["removed"], 0)
        self.assertEqual(result.meta["unique"], 5)

    def test_dedupe_by_nested_key(self):
        data = V('[{"a":{"b":1},"v":1},{"a":{"b":1},"v":2},{"a":{"b":2},"v":3}]')
        result = dedupe_array(data, key="a.b")
        self.assertEqual(result.meta["removed"], 1)
        self.assertEqual(value_of(result), [{"a": {"b": 1}, "v": 1}, {"a": {"b": 2}, "v": 3}])

    def test_dedupe_by_array_index_path(self):
        data = V('[{"a":[1,9]},{"a":[1,8]},{"a":[2,9]}]')
        self.assertEqual(dedupe_array(data, key="a.0").meta["removed"], 1)
        self.assertEqual(dedupe_array(data, key="a.1").meta["removed"], 1)

    def test_keep_last(self):
        data = V('[{"k":1,"v":"first"},{"k":1,"v":"last"}]')
        result = dedupe_array(data, key="k", keep="last")
        self.assertEqual(value_of(result), [{"k": 1, "v": "last"}])
        self.assertEqual(result.meta["removed"], 1)

    def test_keep_first(self):
        data = V('[{"k":1,"v":"first"},{"k":1,"v":"last"}]')
        result = dedupe_array(data, key="k", keep="first")
        self.assertEqual(value_of(result), [{"k": 1, "v": "first"}])

    def test_case_sensitive_default(self):
        self.assertEqual(dedupe_array(V('["a","A"]')).meta["removed"], 0)

    def test_case_insensitive(self):
        result = dedupe_array(V('["a","A","b"]'), case_sensitive=False)
        self.assertEqual(value_of(result), ["a", "b"])
        self.assertEqual(result.meta["removed"], 1)

    def test_case_insensitive_by_key(self):
        data = V('[{"k":"ABC"},{"k":"abc"}]')
        self.assertEqual(dedupe_array(data, key="k", case_sensitive=False).meta["removed"], 1)
        self.assertEqual(dedupe_array(data, key="k", case_sensitive=True).meta["removed"], 0)

    def test_missing_key_elements_are_always_kept(self):
        data = V('[{"k":1},{"z":2},{"z":3}]')
        result = dedupe_array(data, key="k")
        self.assertEqual(result.meta["removed"], 0)
        self.assertEqual(result.meta["unkeyed"], 2)
        self.assertIn("dedupe_unkeyed", codes(result))

    def test_empty_array(self):
        result = dedupe_array([])
        self.assertTrue(result.ok)
        self.assertEqual(result.meta["value"], [])
        self.assertEqual(result.meta["removed"], 0)
        self.assertEqual(result.output, "[]")

    def test_non_array_is_an_error(self):
        for data in ({"a": 1}, "x", None, 42):
            with self.subTest(data=data):
                result = dedupe_array(data)
                self.assertFalse(result.ok)
                self.assertIn("dedupe_not_array", result.error)

    def test_bad_keep_is_an_error(self):
        self.assertIn("dedupe_bad_keep", dedupe_array(V("[1]"), keep="middle").error)

    def test_null_and_boolean_elements(self):
        result = dedupe_array(V('[null,null,true,true,false]'))
        self.assertEqual(value_of(result), [None, True, False])

    def test_unicode_values(self):
        result = dedupe_array(V('["中文","中文","😀"]'))
        self.assertEqual(value_of(result), ["中文", "😀"])

    def test_input_is_not_mutated(self):
        data = V('[{"a":1},{"a":1}]')
        snapshot = minify(data)
        result = dedupe_array(data)
        result.meta["value"].append("x")
        self.assertEqual(minify(data), snapshot)
        self.assertEqual(len(data), 2)

    def test_deep_elements(self):
        data = [nested_object(300), nested_object(300), nested_object(300, 2)]
        result = dedupe_array(data)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.meta["removed"], 1)


# --------------------------------------------------------------------------- #
# 写入文件
# --------------------------------------------------------------------------- #
class TestSplitToFiles(unittest.TestCase):
    def test_writes_files_and_reports_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "out")
            result = split_to_files(V("[1,2,3,4,5]"), target, chunk_size=2)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(len(result.meta["files"]), 3)
            self.assertTrue(os.path.isdir(target))
            for path in result.meta["files"]:
                self.assertTrue(os.path.isfile(path), path)
            first = parse_json(read_file(result.meta["files"][0]))
            self.assertEqual(first.value, [1, 2])
            last = parse_json(read_file(result.meta["files"][-1]))
            self.assertEqual(last.value, [5])

    def test_files_are_pretty_two_space_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = split_to_files(V('[{"a":1.230}]'), tmp, chunk_size=1)
            self.assertTrue(result.ok, result.error)
            text = read_file(result.meta["files"][0])
            self.assertEqual(text, '[\n  {\n    "a": 1.230\n  }\n]\n')


    def test_pattern_with_index_padding_and_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = split_to_files(V("[1,2,3,4]"), tmp, pattern="part_{index:03d}_of_{count}.json",
                                    chunk_size=2)
            names = [os.path.basename(path) for path in result.meta["files"]]
            self.assertEqual(names, ["part_000_of_2.json", "part_001_of_2.json"])

    def test_pattern_with_field_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = V('[{"g":"alpha"},{"g":"beta"},{"g":"alpha"}]')
            result = split_to_files(data, tmp, by_field="g", pattern="{key}.json")
            names = sorted(os.path.basename(path) for path in result.meta["files"])
            self.assertEqual(names, ["alpha.json", "beta.json"])
            parsed = parse_json(read_file(result.meta["files"][0]))
            self.assertEqual(len(parsed.value), 2)

    def test_key_is_sanitized_into_a_safe_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = V('[{"g":"a/b:c*d?"},{"g":"x"}]')
            result = split_to_files(data, tmp, by_field="g", pattern="{key}.json")
            self.assertTrue(result.ok, result.error)
            for path in result.meta["files"]:
                name = os.path.basename(path)
                self.assertNotIn("/", name)
                self.assertNotIn(":", name)
                self.assertTrue(os.path.isfile(path))

    def test_colliding_file_names_are_disambiguated(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = V('[{"g":"a/b"},{"g":"a_b"},{"g":"a b"}]')
            result = split_to_files(data, tmp, by_field="g", pattern="{key}.json")
            self.assertTrue(result.ok, result.error)
            names = sorted(os.path.basename(path) for path in result.meta["files"])
            self.assertEqual(len(names), 3)
            self.assertEqual(len(set(names)), 3, names)
            for path in result.meta["files"]:
                self.assertTrue(os.path.isfile(path), path)

    def test_empty_key_still_produces_a_json_file_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = V('[{"z":1}]')
            result = split_to_files(data, tmp, by_field="g", pattern="{key}.json")
            self.assertTrue(result.ok, result.error)
            name = os.path.basename(result.meta["files"][0])
            self.assertTrue(name.endswith(".json"), name)
            self.assertTrue(os.path.isfile(result.meta["files"][0]))

    def test_unicode_file_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = split_to_files(V('[{"g":"中文"}]'), tmp, by_field="g", pattern="{key}.json")
            self.assertTrue(result.ok, result.error)
            self.assertTrue(any("中文" in os.path.basename(p) for p in result.meta["files"]))
            text = read_file(result.meta["files"][0])
            self.assertIn("中文", text)

    def test_files_reparse_to_the_original_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = V('[{"a":1.230,"中文":"😀"},{"a":2,"中文":"x"}]')
            result = split_to_files(data, tmp, chunk_size=1)
            merged = []
            for path in result.meta["files"]:
                parsed = parse_json(read_file(path))
                self.assertTrue(parsed.ok, parsed.error)
                merged.extend(parsed.value)
            self.assertTrue(json_equal(data, merged, strict_numbers=True))

    def test_existing_directory_is_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(split_to_files(V("[1]"), tmp, chunk_size=1).ok)
            result = split_to_files(V("[2]"), tmp, chunk_size=1)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(len(os.listdir(tmp)), 1)

    def test_empty_array_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = split_to_files([], tmp, chunk_size=3)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.meta["files"], [])
            self.assertEqual(os.listdir(tmp), [])

    def test_unsafe_pattern_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            for pattern in ("../evil.json", "a/b.json", "a\\b.json", "..json", ""):
                with self.subTest(pattern=pattern):
                    result = split_to_files(V("[1]"), tmp, pattern=pattern, chunk_size=1)
                    self.assertFalse(result.ok)
                    self.assertTrue(result.error.startswith("split_"), result.error)

    def test_unknown_pattern_field_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = split_to_files(V("[1]"), tmp, pattern="{nope}.json", chunk_size=1)
            self.assertFalse(result.ok)
            self.assertIn("split_bad_pattern", result.error)

    def test_directory_that_is_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "afile.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("x")
            result = split_to_files(V("[1]"), path, chunk_size=1)
            self.assertFalse(result.ok)
            self.assertIn("split_directory_error", result.error)

    def test_unwritable_directory_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "afile.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("x")
            result = split_to_files(V("[1]"), os.path.join(path, "sub"), chunk_size=1)
            self.assertFalse(result.ok)
            self.assertIn("split_directory_error", result.error)

    def test_bad_directory_argument(self):
        for directory in ("", "   ", None, 42):
            with self.subTest(directory=directory):
                result = split_to_files(V("[1]"), directory, chunk_size=1)
                self.assertFalse(result.ok)
                self.assertIn("split_bad_directory", result.error)

    def test_split_errors_are_propagated(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = split_to_files(V('{"a":1}'), tmp, chunk_size=1)
            self.assertFalse(result.ok)
            self.assertIn("split_not_array", result.error)
            self.assertEqual(os.listdir(tmp), [])

    def test_meta_directory_is_absolute(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = split_to_files(V("[1]"), tmp, chunk_size=1)
            self.assertTrue(os.path.isabs(result.meta["directory"]))

    def test_no_temp_files_left_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            split_to_files(V("[1,2,3]"), os.path.join(tmp, "nested", "deep"), chunk_size=1)
            self.assertTrue(os.path.isdir(os.path.join(tmp, "nested", "deep")))
        self.assertFalse(os.path.exists(tmp))


# --------------------------------------------------------------------------- #
# 编码
# --------------------------------------------------------------------------- #
class TestEncodingToBytes(unittest.TestCase):
    def test_utf8(self):
        result = encoding_to_bytes("中文", "utf-8")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.meta["data"], "中文".encode("utf-8"))
        self.assertEqual(result.meta["bytes"], 6)
        self.assertFalse(result.meta["bom"])

    def test_utf8_sig_has_a_bom(self):
        result = encoding_to_bytes("中文", "utf-8-sig")
        self.assertTrue(result.meta["data"].startswith(b"\xef\xbb\xbf"))
        self.assertTrue(result.meta["bom"])
        self.assertEqual(result.meta["data"][3:].decode("utf-8"), "中文")

    def test_utf16_has_a_bom(self):
        result = encoding_to_bytes("abc", "utf-16")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.meta["data"].decode("utf-16"), "abc")
        self.assertTrue(result.meta["bom"])

    def test_gbk_handles_chinese(self):
        result = encoding_to_bytes("中文", "gbk")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.meta["data"].decode("gbk"), "中文")
        self.assertEqual(result.meta["bytes"], 4)

    def test_gbk_cannot_encode_emoji(self):
        result = encoding_to_bytes("中文😀", "gbk")
        self.assertFalse(result.ok)
        self.assertIn("encoding_unencodable", result.error)
        self.assertIn("U+1F600", result.error)

    def test_gb18030_handles_emoji(self):
        result = encoding_to_bytes("中文😀", "gb18030")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.meta["data"].decode("gb18030"), "中文😀")

    def test_ascii_rejects_non_ascii(self):
        result = encoding_to_bytes("中", "ascii")
        self.assertFalse(result.ok)
        self.assertIn("encoding_unencodable", result.error)

    def test_encoding_aliases(self):
        for name in ("utf8", "UTF-8", "cp936", "utf16"):
            with self.subTest(name=name):
                self.assertTrue(encoding_to_bytes("x", name).ok)
        self.assertEqual(encoding_to_bytes("x", "cp936").meta["encoding"], "gbk")

    def test_unsupported_encoding(self):
        for name in ("rot13", "koi8-r", "nope", "", None):
            with self.subTest(name=name):
                result = encoding_to_bytes("x", name)
                self.assertFalse(result.ok)
                self.assertIn("unsupported_encoding", result.error)

    def test_empty_text(self):
        result = encoding_to_bytes("", "utf-8")
        self.assertTrue(result.ok)
        self.assertEqual(result.meta["data"], b"")
        self.assertEqual(result.meta["bytes"], 0)

    def test_none_text_is_an_error(self):
        self.assertFalse(encoding_to_bytes(None, "utf-8").ok)

    def test_bytes_input_is_decoded_first(self):
        result = encoding_to_bytes("中".encode("utf-8"), "gbk")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.meta["data"].decode("gbk"), "中")

    def test_csv_output_can_be_encoded_with_the_requested_encoding(self):
        data = V('[{"中文":"值"}]')
        exported = json_to_csv(data, CsvOptions(encoding="gbk"))
        encoded = encoding_to_bytes(exported.output, exported.meta["encoding"])
        self.assertTrue(encoded.ok, encoded.error)
        self.assertEqual(encoded.meta["data"].decode("gbk"), exported.output)


# --------------------------------------------------------------------------- #
# 深度、健壮性、性能
# --------------------------------------------------------------------------- #
class TestDeepNesting(unittest.TestCase):
    DEPTH = 600

    def setUp(self):
        self.deep_object = nested_object(self.DEPTH)
        self.deep_array = nested_array(self.DEPTH)

    def test_json_to_csv_deep_object(self):
        result = json_to_csv([self.deep_object])
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.meta["rows"], 1)
        self.assertEqual(len(result.meta["columns"][0].split(".")), self.DEPTH)

    def test_json_to_csv_deep_array(self):
        result = json_to_csv([self.deep_array], CsvOptions(array_handling="cell"))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.meta["rows"], 1)

    def test_json_to_xml_deep(self):
        result = json_to_xml(self.deep_object)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.meta["elements"], self.DEPTH + 1)

    def test_xml_to_json_deep(self):
        exported = json_to_xml(self.deep_object, XmlOptions(pretty=False, declaration=False))
        result = xml_to_json(exported.output)
        self.assertTrue(result.ok, result.error)
        self.assertTrue(json_equal(self.deep_object, value_of(result), strict_numbers=True))

    def test_json_to_yaml_deep_reports_a_clear_error(self):
        result = json_to_yaml(self.deep_object)
        self.assertFalse(result.ok)
        self.assertIn("yaml_depth_exceeded", result.error)

    def test_yaml_to_json_deep_reports_a_clear_error(self):
        text = "".join("  " * i + "a:\n" for i in range(self.DEPTH)) + "  " * self.DEPTH + "1\n"
        result = yaml_to_json(text)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("yaml_too_deep")
                        or result.error.startswith("depth_exceeded"), result.error)

    def test_merge_deep(self):
        result = merge_documents([self.deep_object, nested_object(self.DEPTH, 2)])
        self.assertTrue(result.ok, result.error)
        self.assertIn("2", result.output)

    def test_dedupe_deep(self):
        result = dedupe_array([self.deep_object, nested_object(self.DEPTH)])
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.meta["removed"], 1)

    def test_split_deep(self):
        result = split_array([self.deep_object, self.deep_object], chunk_size=1)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.meta["count"], 2)

    def test_split_to_files_deep(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = split_to_files([self.deep_object], tmp, chunk_size=1)
            self.assertTrue(result.ok, result.error)
            parsed = parse_json(read_file(result.meta["files"][0]))
            self.assertTrue(parsed.ok, parsed.error)

    def test_beyond_max_depth_is_a_clear_error(self):
        from app.engines.converters import MAX_DEPTH

        too_deep = nested_object(MAX_DEPTH + 200, leaf=1)
        self.assertIn("depth_exceeded", json_to_csv([too_deep]).error)
        self.assertIn("depth_exceeded", json_to_xml(too_deep).error)

    def test_no_recursion_error_escapes(self):
        # 600 层已经超过 CPython 默认递归上限的一半，任何一处递归实现都会炸
        for result in (json_to_csv([self.deep_object]), json_to_xml(self.deep_object),
                       merge_documents([self.deep_object]), dedupe_array([self.deep_object]),
                       split_array([self.deep_object], chunk_size=1)):
            self.assertIsInstance(result, ConvertResult)


class TestRobustness(unittest.TestCase):
    ODD_INPUTS = [None, "", 42, -1, 0, True, False, 3.5, [], {}, [[]], [{}], "中文😀",
                  {"": ""}, {"a": None}, [None], {"中文": ["😀", {"a": []}]}]

    def test_no_public_function_raises(self):
        for data in self.ODD_INPUTS:
            with self.subTest(data=repr(data)[:40]):
                for result in (json_to_csv(data), json_to_xml(data), json_to_yaml(data),
                               split_array(data, chunk_size=1), dedupe_array(data),
                               merge_documents([data, data])):
                    self.assertIsInstance(result, ConvertResult)
                    self.assertIsInstance(result.ok, bool)
                    self.assertIsInstance(result.output, str)
                    self.assertIsInstance(result.error, str)
                    self.assertIsInstance(result.warnings, list)
                    self.assertIsInstance(result.meta, dict)
                    if not result.ok:
                        self.assertTrue(result.error)

    def test_text_functions_never_raise(self):
        for text in (None, "", "   ", "<", "<?xml", "a: ", "\x00", "中文", "[" * 50):
            with self.subTest(text=repr(text)[:30]):
                for result in (csv_to_json(text), xml_to_json(text), yaml_to_json(text),
                               unescape_json_string(text)):
                    self.assertIsInstance(result, ConvertResult)
                    self.assertIsInstance(result.ok, bool)
                self.assertIsInstance(escape_json_string(text), str)
                self.assertIsInstance(encoding_to_bytes(text, "utf-8"), ConvertResult)

    def test_odd_option_values_never_raise(self):
        data = V('[{"a":1,"b":[1,2]}]')
        options = [
            CsvOptions(delimiter="\t", flatten=False, array_handling="cell"),
            CsvOptions(flatten_separator="", columns=("a",), null_text="N", quote_all=True),
            CsvOptions(array_handling="join", join_separator="", line_ending="\n"),
            XmlOptions(root_name="", item_name="", attr_prefix="", text_key=""),
            XmlOptions(pretty=True, indent="", declaration=False),
            YamlOptions(indent=2, sort_keys=True, allow_unicode=False, width=20),
        ]
        for opts in options:
            with self.subTest(opts=opts):
                if isinstance(opts, CsvOptions):
                    self.assertIsInstance(json_to_csv(data, opts).ok, bool)
                elif isinstance(opts, XmlOptions):
                    self.assertIsInstance(json_to_xml(data, opts).ok, bool)
                else:
                    self.assertIsInstance(json_to_yaml(data, opts).ok, bool)

    def test_xml_options_with_empty_prefixes(self):
        result = json_to_xml(V('{"a":1}'), XmlOptions(attr_prefix="", text_key="",
                                                      pretty=False, declaration=False))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.output, "<root><a>1</a></root>\n")

    def test_warning_list_is_capped(self):
        from app.engines.converters import MAX_WARNINGS

        data = [{"a b": i} for i in range(MAX_WARNINGS + 50)]
        result = json_to_xml(data)
        self.assertTrue(result.ok, result.error)
        self.assertLessEqual(len(result.warnings), MAX_WARNINGS + 1)
        self.assertIn("warnings_suppressed", result.warnings[-1])

    def test_warnings_are_english_and_coded(self):
        result = json_to_xml(V('{"a b":1}'))
        for warning in result.warnings:
            self.assertTrue(warning.isascii(), warning)
            self.assertRegex(warning, r"^[a-z0-9_]+: ")

    def test_large_document_conversion(self):
        data = [{"i": i, "s": "value-%d" % i, "n": JsonNumber("1.230")} for i in range(2000)]
        for result in (json_to_csv(data), json_to_xml(data, XmlOptions(pretty=False)),
                       json_to_yaml(data[:200])):
            self.assertTrue(result.ok, result.error)


class TestPerformance(unittest.TestCase):
    def test_20k_rows_to_csv_and_back(self):
        rows = [{"id": i, "name": "name-%d" % i, "price": JsonNumber("1.230"),
                 "ok": i % 2 == 0, "note": None if i % 7 else "x, y"}
                for i in range(20000)]
        started = time.time()
        exported = json_to_csv(rows)
        elapsed_out = time.time() - started
        self.assertTrue(exported.ok, exported.error)
        self.assertEqual(exported.meta["rows"], 20000)

        started = time.time()
        imported = csv_to_json(exported.output)
        elapsed_in = time.time() - started
        self.assertTrue(imported.ok, imported.error)
        self.assertEqual(imported.meta["rows"], 20000)
        self.assertLess(elapsed_out, 5.0, "json_to_csv took %.2fs" % elapsed_out)
        self.assertLess(elapsed_in, 5.0, "csv_to_json took %.2fs" % elapsed_in)
        self.assertIn("1.230", exported.output)

    def test_20k_rows_round_trip_keeps_data(self):
        rows = [{"id": i, "v": i * 2} for i in range(20000)]
        exported = json_to_csv(rows)
        imported = csv_to_json(exported.output)
        self.assertTrue(json_equal(rows, value_of(imported), strict_numbers=True))

    def test_wide_rows(self):
        rows = [{"col%d" % c: r * c for c in range(60)} for r in range(2000)]
        started = time.time()
        result = json_to_csv(rows)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(result.meta["columns"]), 60)
        self.assertLess(time.time() - started, 5.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

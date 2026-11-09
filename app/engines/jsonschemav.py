"""JSON Schema 校验引擎：完全离线，draft-07 为基线，兼容 2019-09 / 2020-12 增补。

设计约束（与 ``app.core`` / 其它引擎一致）：

* **纯 Python、零 Qt**：不导入任何 PySide6 模块，不导入 ``urllib``/``socket``，
  因此本模块在物理上就没有联网能力。指向 ``http``/``https`` 的 ``$ref``
  一律报 ``remote_ref_not_allowed`` 错误，绝不抓取。
* **绝不修改入参**：instance 与 schema 全程只读，路径一律是新建元组。
* **绝不递归遍历用户数据**：校验主循环、规范序列化、schema 推断都用显式栈；
  1000+ 层嵌套的实例不会触发 ``RecursionError``。
* **$ref 循环必须被切断**：每条下降路径携带一个不可变的 ref 链
  ``(目标标识, id(实例节点))``；同一个目标作用在同一个实例节点上第二次出现，
  就说明是纯粹的 schema 自环（例如 ``{"allOf":[{"$ref":"#"}]}``），
  立即报 ``ref_cycle`` 并停止该分支。合法的递归树 schema（每层 ref 都作用在
  更深的子节点上）链上不会重复，因此可以正常校验任意深度。
* **公共 API 不抛异常**：:func:`validate` 捕获一切异常（含 ``RecursionError``），
  失败原因写进 ``errors``。
* **消息约定**：``keyword`` 是机器可读的关键字名，``message`` 是英文句子，
  由 UI 层翻译；``warnings`` 用 ``code: detail`` 形式（与 converters.py 一致）。

---------------------------------------------------------------------------
"integer" 的取值（重要，UI 需要照此解释）
---------------------------------------------------------------------------
遵循 draft-07 及以后版本的定义：**数值上是整数的 number 也算 integer**。
因此 ``1.0``（解析成 :class:`~app.core.jsonvalue.JsonNumber`）、``1e2``、
Python ``int`` 都满足 ``{"type": "integer"}``；``1.5`` 不满足。
``bool`` 是 Python ``int`` 的子类，但 JSON 里 ``true``/``false`` 不是数字，
所以 **布尔永远不满足 ``integer`` 或 ``number``**（draft 规范同样如此要求）。
反过来，``{"type": "number"}`` 不接受 ``true``。

---------------------------------------------------------------------------
其它约定
---------------------------------------------------------------------------
* 字符串长度（``minLength`` / ``maxLength``）按 **Unicode 码点**计数，
  与 Python ``len(str)`` 一致；不采用 ECMA-262 的 UTF-16 码元计数，
  因此 ``"😀"`` 的长度是 1（某些 JS 校验器会算成 2）。
* ``required`` / ``dependentRequired`` 的错误路径指向**缺少该属性的对象**
  （``$.users[0]``），而不是不存在的属性；缺失的键名放在 ``expected`` 字段。
  这样 UI 点击错误可以选中一个真实存在的节点。
* ``pattern`` 用 Python :mod:`re` 的 ``search`` 语义（部分匹配即通过），
  与 ECMA-262 正则并不完全等价；无法编译的 pattern 只告警 ``pattern_invalid``
  并跳过该检查，绝不抛异常。
* ``format`` 默认是注解（annotation）。本引擎对
  ``date-time`` / ``date`` / ``time`` / ``email`` / ``uri`` / ``uuid`` /
  ``ipv4`` / ``ipv6`` 做**真实断言**（失败即错误）；其它已知格式
  （``hostname`` / ``uri-reference`` / ``json-pointer`` / ``regex`` …）
  只告警 ``format_annotation``；完全不认识的格式告警 ``format_unknown``。
  ``email`` 是"基础"检查，要求域名至少两段（``a@b`` 判为不合法）。
* draft-04 的布尔型 ``exclusiveMinimum`` / ``exclusiveMaximum`` 不会被静默忽略：
  报 ``exclusive_minimum_boolean_form`` / ``exclusive_maximum_boolean_form`` 错误，
  提示改写成 draft-07 的数值形式。
* draft-07 里 ``$ref`` 会**吞掉同级关键字**（规范如此）。引擎按检测到的 draft
  执行：draft-07 及更早跳过同级关键字并告警 ``ref_siblings_ignored``，
  2019-09 / 2020-12 则同级关键字照常生效。
* ``uniqueItems`` 用"规范序列化 + 哈希集合"判重，O(n) 不退化成 O(n²)；
  规范文本把数字按数值归一化，所以 ``1.0`` 与 ``1`` 视为重复
  （与 JSON Schema 数值相等语义一致），而 ``"1"`` 与 ``1`` 不重复。
  哈希命中后还会做一次结构化成对比较兜底，若两者其实不等（理论上的碰撞），
  判为不重复并告警 ``unique_items_hash_collision``。
* ``oneOf`` / ``anyOf`` 失败时会说明**每个子 schema 为什么失败**
  （``expected`` 字段是分支原因列表，``message`` 里也有摘要），
  否则用户看到的只是一句"不匹配"，完全无法下手。
* ``max_errors`` 命中上限后停止收集并置 ``truncated=True``；另有一个全局
  工作量上限 ``MAX_STEPS``，命中即置 ``truncated`` 并报 ``budget_exceeded``
  （防御 ``anyOf`` × 巨型文档这类组合爆炸）。
* 未知关键字只告警（``unknown_keyword``，带 schema 路径），不影响校验结果；
  已识别但本引擎不支持语义的关键字（``unevaluatedItems``、``$dynamicRef`` …）
  告警 ``keyword_unsupported``。

---------------------------------------------------------------------------
限制（UI 需要提示的点）
---------------------------------------------------------------------------
* ``unevaluatedItems`` / ``unevaluatedProperties`` 需要跨分支的注解跟踪，
  本引擎不支持，遇到时告警并按"不存在"处理（结果偏宽松）。
* ``$dynamicRef`` / ``$recursiveRef`` 按普通 ``$ref`` 解析（不建模动态作用域），
  并告警 ``keyword_unsupported``。
* ``$id`` 的 base URI 作用域是**近似实现**：加载文档时会把内嵌 ``$id``
  按其字面文本与末段名字建索引，因此 ``{"$id":"address.json"}`` +
  ``{"$ref":"address.json"}`` 能解析；但严格的相对 URI 归并
  （``../``、查询串、fragment 继承）没有完整实现。
* 跨文件 ``$ref`` 只在 ``base_dir`` 与 ``search_dirs`` 里找本地文件，
  最多加载 ``MAX_REF_DOCUMENTS`` 个文档；找不到就报错，绝不联网。
* :func:`infer_schema` 的数组采样上限是 ``sample_arrays``：深层约束
  （嵌套对象里的 ``minLength`` 等）只来自被采样的元素，因此推断出的 schema
  对**未采样**的元素可能偏严。浅层信息（标量类型、数值范围、对象键集合）
  会扫描数组的全部元素，所以 ``required`` 与 ``minimum``/``maximum`` 是准确的。
"""

from __future__ import annotations

import ipaddress
import os
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..core.jsonfilemanager import read_text_file
from ..core.jsonformatter import INDENT_2, FormatOptions, dumps
from ..core.jsonparser import MODE_JSON, MODE_JSONC, parse_json
from ..core.jsonvalue import (
    TYPE_ARRAY,
    TYPE_BOOLEAN,
    TYPE_NULL,
    TYPE_NUMBER,
    TYPE_OBJECT,
    TYPE_STRING,
    JsonNumber,
    encode_json_string,
    json_type,
    number_literal,
)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
DRAFT_03 = "draft-03"
DRAFT_04 = "draft-04"
DRAFT_06 = "draft-06"
DRAFT_07 = "draft-07"
DRAFT_2019_09 = "2019-09"
DRAFT_2020_12 = "2020-12"
DRAFT_UNKNOWN = "unknown"

DRAFTS = (DRAFT_03, DRAFT_04, DRAFT_06, DRAFT_07, DRAFT_2019_09, DRAFT_2020_12, DRAFT_UNKNOWN)

#: 默认错误上限
DEFAULT_MAX_ERRORS = 500
#: 单个结果里最多保留的告警条数（与 converters.py 一致）
MAX_WARNINGS = 60
#: anyOf / oneOf 每个分支最多保留几条失败原因
MAX_BRANCH_ERRORS = 5
#: 实例最大嵌套层数（超过报 depth_exceeded，绝不 RecursionError）
MAX_INSTANCE_DEPTH = 4096
#: 单次校验允许弹出的任务数（组合爆炸防线）
MAX_STEPS = 4_000_000
#: 跨文件 $ref 最多加载的文档数
MAX_REF_DOCUMENTS = 64
#: 解析 schema 文件时允许的嵌套层数
SCHEMA_PARSE_MAX_DEPTH = 1000
#: $ref 链的最大长度（防御 id() 复用等极端情况）
MAX_REF_CHAIN = 4096

#: 根文档的内部标识（不是 URL，仅用于 $ref 循环检测）
ROOT_IDENTITY = "<root>"

#: 十进制精确运算精度（Decimal.MAX_PREC 只在部分实现上挂在类属性上）
MAX_PREC = getattr(Decimal, "MAX_PREC", None) or 10 ** 9

#: ``type`` 输出的规范顺序
TYPE_ORDER = (TYPE_OBJECT, TYPE_ARRAY, TYPE_STRING, TYPE_NUMBER, "integer", TYPE_BOOLEAN, TYPE_NULL)

#: 有真实断言实现的 format
ASSERTED_FORMATS = ("date-time", "date", "time", "email", "uri", "uuid", "ipv4", "ipv6")

#: 认识但只做注解的 format（告警 format_annotation）
ANNOTATION_FORMATS = (
    "hostname", "idn-hostname", "uri-reference", "iri", "iri-reference", "uri-template",
    "json-pointer", "relative-json-pointer", "regex", "duration", "idn-email",
)

#: 注解类关键字：出现在 $ref 同级时不算"被忽略的兄弟关键字"
_ANNOTATION_KEYWORDS = frozenset({
    "$schema", "$id", "$comment", "$anchor", "$vocabulary", "title", "description",
    "default", "examples", "deprecated", "readOnly", "writeOnly", "definitions", "$defs",
})

#: 已识别但不支持其语义的关键字（告警 keyword_unsupported，按不存在处理）
_UNSUPPORTED_KEYWORDS = frozenset({
    "unevaluatedItems", "unevaluatedProperties", "$dynamicRef", "$dynamicAnchor",
    "$recursiveRef", "$recursiveAnchor", "contentSchema", "prefixItems2019",
    "dependentContained",
})

#: 全部已知关键字（不在这里也不在 _UNSUPPORTED_KEYWORDS 里的 -> unknown_keyword 告警）
_KNOWN_KEYWORDS = frozenset({
    # 注解 / 词汇表
    "$schema", "$id", "$comment", "$anchor", "$vocabulary", "$defs", "definitions",
    "title", "description", "default", "examples", "deprecated", "readOnly", "writeOnly",
    # 断言
    "type", "const", "enum", "multipleOf", "maximum", "minimum", "exclusiveMaximum",
    "exclusiveMinimum", "maxLength", "minLength", "pattern", "format",
    "contentEncoding", "contentMediaType",
    "maxItems", "minItems", "uniqueItems", "contains", "minContains", "maxContains",
    "maxProperties", "minProperties", "required", "dependentRequired", "dependentSchemas",
    "dependencies", "propertyNames", "properties", "patternProperties", "additionalProperties",
    "items", "prefixItems", "additionalItems",
    "allOf", "anyOf", "oneOf", "not", "if", "then", "else",
    # 引用
    "$ref",
    # 已知但不支持
}) | _UNSUPPORTED_KEYWORDS

#: 断言类关键字的求值顺序（决定错误输出顺序，先本地断言后子 schema）
_CHECK_ORDER = (
    "type", "const", "enum",
    "maximum", "exclusiveMaximum", "minimum", "exclusiveMinimum", "multipleOf",
    "maxLength", "minLength", "pattern", "format", "contentEncoding", "contentMediaType",
    "maxItems", "minItems", "uniqueItems",
    "maxProperties", "minProperties", "required", "dependentRequired",
)

#: 需要子任务/子校验的关键字，按此顺序展开
_APPLICATOR_ORDER = (
    "properties", "patternProperties", "additionalProperties", "propertyNames",
    "prefixItems", "items", "additionalItems", "contains",
    "dependentSchemas", "dependencies",
    "allOf", "anyOf", "oneOf", "not", "if",
)

#: 2019-09 起才有关键字（在更早的 draft 下使用会告警）
_LATER_KEYWORDS = {
    "prefixItems": DRAFT_2020_12,
    "dependentRequired": DRAFT_2019_09,
    "dependentSchemas": DRAFT_2019_09,
    "minContains": DRAFT_2019_09,
    "maxContains": DRAFT_2019_09,
    "$defs": DRAFT_2019_09,
    "$anchor": DRAFT_2019_09,
    "$recursiveRef": DRAFT_2019_09,
    "$recursiveAnchor": DRAFT_2019_09,
    "$dynamicRef": DRAFT_2020_12,
    "$dynamicAnchor": DRAFT_2020_12,
    "$vocabulary": DRAFT_2019_09,
    "unevaluatedItems": DRAFT_2019_09,
    "unevaluatedProperties": DRAFT_2019_09,
    "contentSchema": DRAFT_2019_09,
    "deprecated": DRAFT_07,
}

_DRAFT_BY_URI = (
    ("draft/2020-12", DRAFT_2020_12),
    ("draft/2019-09", DRAFT_2019_09),
    ("draft-07", DRAFT_07),
    ("draft-06", DRAFT_06),
    ("draft-04", DRAFT_04),
    ("draft-03", DRAFT_03),
    ("draft/07", DRAFT_07),
    ("draft/06", DRAFT_06),
    ("draft/04", DRAFT_04),
    ("draft/03", DRAFT_03),
)

__all__ = [
    "ASSERTED_FORMATS",
    "DEFAULT_MAX_ERRORS",
    "DRAFTS",
    "DRAFT_04",
    "DRAFT_06",
    "DRAFT_07",
    "DRAFT_2019_09",
    "DRAFT_2020_12",
    "DRAFT_UNKNOWN",
    "LocalRefResolver",
    "MAX_INSTANCE_DEPTH",
    "MAX_WARNINGS",
    "SchemaError",
    "SchemaValidationResult",
    "detect_draft",
    "error_keywords",
    "infer_schema",
    "load_schema_file",
    "path_string",
    "schema_to_text",
    "validate",
    "warning_codes",
]


# --------------------------------------------------------------------------- #
# 路径文本（与 statistics.py / scanners.py / generator.py 完全同一份实现；
# 按项目约定不新建共享文件，各引擎模块自带一份副本，修改时必须同步四处）
# --------------------------------------------------------------------------- #
_PATH_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def path_string(path: Tuple) -> str:
    """把路径元组转成规范 JSONPath 文本。

    规则：对象键是标识符形式时写作 ``$.a.b``，否则用单引号括号形式
    ``$['weird key']``（内部的 ``\\`` 与 ``'`` 需转义）；数组下标写作 ``[0]``；
    根路径写作 ``$``。
    """
    parts = ["$"]
    try:
        items = tuple(path)
    except TypeError:
        return "$"
    for item in items:
        if isinstance(item, bool):  # bool 是 int 子类，先挡住
            parts.append("[%d]" % int(item))
        elif isinstance(item, int):
            parts.append(f"[{item}]")
        else:
            key = item if isinstance(item, str) else str(item)
            if _PATH_IDENT.match(key):
                parts.append("." + key)
            else:
                parts.append("['" + key.replace("\\", "\\\\").replace("'", "\\'") + "']")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# 结果对象
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SchemaError:
    """一条校验错误。``message`` 是英文句子，``keyword`` 供 UI 本地化。"""

    path: Tuple
    path_string: str
    keyword: str
    message: str
    expected: Any = None
    actual: Any = None
    schema_path: Tuple = ()

    def describe(self) -> str:
        """``$.a.b: keyword: message`` 形式的单行文本（日志/复制用）。"""
        return f"{self.path_string}: {self.message}"


@dataclass
class SchemaValidationResult:
    """一次 Schema 校验的完整结果。"""

    valid: bool
    errors: List[SchemaError] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    truncated: bool = False
    elapsed_ms: float = 0.0
    schema_draft: str = ""
    error_count: int = 0
    checked_nodes: int = 0

    def first_error(self) -> Optional[SchemaError]:
        return self.errors[0] if self.errors else None

    def keywords(self) -> List[str]:
        """去重后的关键字列表（保持首次出现顺序）。"""
        return error_keywords(self)


def error_keywords(result: SchemaValidationResult) -> List[str]:
    """取出 ``result.errors`` 里的关键字（去重、保序）。"""
    seen = set()
    out: List[str] = []
    for item in getattr(result, "errors", None) or ():
        keyword = getattr(item, "keyword", "")
        if keyword and keyword not in seen:
            seen.add(keyword)
            out.append(keyword)
    return out


def warning_codes(result: SchemaValidationResult) -> List[str]:
    """取出 ``result.warnings`` 里的机器可读 code（``code: detail`` 的 code 部分）。"""
    codes: List[str] = []
    for item in getattr(result, "warnings", None) or ():
        code, _, _ = str(item).partition(":")
        codes.append(code.strip())
    return codes


# --------------------------------------------------------------------------- #
# 值工具：类型、数值、规范化文本、结构相等
# --------------------------------------------------------------------------- #
def _is_integral(value: Any) -> bool:
    """数值上是否为整数（draft-07 起 ``1.0`` 也算 integer）。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, Decimal):
        if value.is_nan() or value.is_infinite():
            return False
        try:
            return value == value.to_integral_value()
        except (InvalidOperation, ValueError, OverflowError):  # pragma: no cover - 防御
            return False
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf")) and value.is_integer()
    return False


def _as_decimal(value: Any) -> Optional[Decimal]:
    """把实例数字转成 Decimal；无法转换返回 ``None``（调用方跳过检查）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return None if (value.is_nan() or value.is_infinite()) else value
    if isinstance(value, int):
        try:
            return Decimal(value)
        except (ValueError, InvalidOperation):  # pragma: no cover - 防御
            return None
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):  # pragma: no cover - 防御
            return None
    return None


def _bound_decimal(value: Any) -> Optional[Decimal]:
    """schema 里的数值边界 -> Decimal（int/float/Decimal/JsonNumber 都接受）。"""
    return _as_decimal(value)


def _canonical_number(value: Any) -> str:
    """数字的规范文本：按**数值**归一化，所以 ``1.0`` / ``1`` / ``1e0`` 相同。"""
    if isinstance(value, JsonNumber):
        decimal_value: Decimal = value
    elif isinstance(value, Decimal):
        decimal_value = value
    elif isinstance(value, int):
        decimal_value = Decimal(value)
    elif isinstance(value, float):
        try:
            decimal_value = Decimal(str(value))
        except (InvalidOperation, ValueError):  # pragma: no cover - 防御
            return "n"
    else:  # pragma: no cover - 调用方已过滤
        return "?"
    if decimal_value.is_nan():
        return "NaN"
    if decimal_value.is_infinite():
        return "-Infinity" if decimal_value < 0 else "Infinity"
    if decimal_value == 0:  # -0 与 0 数值相等，必须归一到同一个文本
        return "0"
    try:
        return str(decimal_value.normalize())
    except (InvalidOperation, ValueError):  # pragma: no cover - 防御
        return str(decimal_value)


def _scalar_text(value: Any, limit: int = 120) -> str:
    """标量的可读文本（错误消息 / actual 字段用），过长截断。"""
    kind = json_type(value)
    if kind == TYPE_STRING:
        text = encode_json_string(value)
    elif kind == TYPE_BOOLEAN:
        text = "true" if value else "false"
    elif kind == TYPE_NULL:
        text = "null"
    else:
        try:
            text = number_literal(value)
        except (TypeError, ValueError):  # pragma: no cover - 防御
            text = str(value)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _describe(value: Any, limit: int = 120) -> str:
    """实例值的简短描述：标量给出文本，容器给出类型与规模。"""
    kind = json_type(value)
    if kind == TYPE_OBJECT:
        count = len(value)
        return f"object with {count} " + ("property" if count == 1 else "properties")
    if kind == TYPE_ARRAY:
        count = len(value)
        return f"array with {count} " + ("item" if count == 1 else "items")
    return _scalar_text(value, limit)


class _Lit:
    """规范序列化栈里的字面量标记（避免与字符串值混淆）。"""

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text


def _canonical_text(value: Any) -> str:
    """迭代式生成规范 JSON 文本，用于 ``enum`` / ``const`` / ``uniqueItems``。

    * 对象按键文本排序（JSON 对象无序，``{"a":1,"b":2}`` 与 ``{"b":2,"a":1}`` 相等）
    * 数字按数值归一化（``1.0`` == ``1``，``-0`` == ``0``）
    * 字符串用 RFC 8259 转义，因此带引号的文本不可能与数字/布尔撞车
    """
    if isinstance(value, str):
        return encode_json_string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, Decimal)):
        return _canonical_number(value)
    if value is None:
        return "null"

    out: List[str] = []
    stack: List[Any] = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, _Lit):
            out.append(node.text)
            continue
        if isinstance(node, str):
            out.append(encode_json_string(node))
            continue
        if isinstance(node, bool):
            out.append("true" if node else "false")
            continue
        if node is None:
            out.append("null")
            continue
        if isinstance(node, (int, float, Decimal)):
            out.append(_canonical_number(node))
            continue
        if isinstance(node, dict):
            if not node:
                out.append("{}")
                continue
            pairs = sorted(
                ((encode_json_string(key), item) for key, item in node.items()),
                key=lambda pair: pair[0],
            )
            out.append("{")
            stack.append(_Lit("}"))
            for index in range(len(pairs) - 1, -1, -1):
                stack.append(pairs[index][1])
                stack.append(_Lit(pairs[index][0] + ":"))
                if index:
                    stack.append(_Lit(","))
            continue
        if isinstance(node, (list, tuple)):
            if not node:
                out.append("[]")
                continue
            out.append("[")
            stack.append(_Lit("]"))
            for index in range(len(node) - 1, -1, -1):
                stack.append(node[index])
                if index:
                    stack.append(_Lit(","))
            continue
        # 非 JSON 值（理论上不会出现）：退化成 repr，保证不抛异常
        out.append(encode_json_string(repr(node)))
    return "".join(out)


def _values_equal(left: Any, right: Any, max_nodes: int = 2_000_000) -> bool:
    """迭代式结构相等比较：数字按数值，对象忽略键顺序，``bool`` 不等于数字。"""
    stack: List[Tuple[Any, Any]] = [(left, right)]
    seen = 0
    while stack:
        a, b = stack.pop()
        seen += 1
        if seen > max_nodes:  # pragma: no cover - 防御
            return False
        kind_a, kind_b = json_type(a), json_type(b)
        if kind_a != kind_b:
            return False
        if kind_a == TYPE_OBJECT:
            if len(a) != len(b) or set(a.keys()) != set(b.keys()):
                return False
            for key in a:
                stack.append((a[key], b[key]))
        elif kind_a == TYPE_ARRAY:
            if len(a) != len(b):
                return False
            stack.extend(zip(a, b))
        elif kind_a == TYPE_NUMBER:
            if not _number_equal(a, b):
                return False
        elif a != b:
            return False
    return True


def _number_equal(left: Any, right: Any) -> bool:
    """数值相等（``1`` == ``1.0`` == ``1e0``），NaN 不等于任何值。"""
    try:
        if isinstance(left, Decimal) or isinstance(right, Decimal):
            left_dec = left if isinstance(left, Decimal) else _as_decimal(left)
            right_dec = right if isinstance(right, Decimal) else _as_decimal(right)
            if left_dec is None or right_dec is None:
                return False
            if left_dec.is_nan() or right_dec.is_nan():
                return False
            return left_dec == right_dec
        return bool(left == right)
    except (TypeError, ValueError, InvalidOperation):  # pragma: no cover - 防御
        return False


# --------------------------------------------------------------------------- #
# 远程引用识别（离线硬约束）
# --------------------------------------------------------------------------- #
_SCHEME_RE = re.compile(r"\A([A-Za-z][A-Za-z0-9+.\-]*):")
_NETWORK_SCHEMES = frozenset({
    "http", "https", "ftp", "ftps", "sftp", "ws", "wss", "gopher", "ldap", "ldaps",
    "mailto", "data", "git", "ssh", "svn", "jar", "netdoc",
})


def _split_scheme(ref: str) -> Tuple[str, str]:
    """拆出 URI scheme；Windows 盘符（``C:\\x``）不算 scheme。"""
    match = _SCHEME_RE.match(ref)
    if not match:
        return "", ref
    scheme = match.group(1)
    if len(scheme) == 1 and scheme.isalpha():
        rest = ref[2:]
        if not rest or rest[0] in "/\\":
            return "", ref  # 盘符路径
    return scheme.lower(), ref[len(scheme) + 1:]


def _percent_decode(text: str) -> str:
    """最小实现的百分号解码（只用字符串操作，绝不联网）。"""
    if "%" not in text:
        return text
    out: List[str] = []
    index = 0
    length = len(text)
    while index < length:
        ch = text[index]
        if ch == "%" and index + 2 < length:
            try:
                out.append(chr(int(text[index + 1:index + 3], 16)))
                index += 3
                continue
            except ValueError:
                pass
        out.append(ch)
        index += 1
    return "".join(out)


def remote_ref_error(ref: Any) -> str:
    """若是网络位置（或 UNC 共享）返回英文错误码文本，否则返回空串。"""
    if not isinstance(ref, str):
        return ""
    text = ref.strip()
    if not text:
        return ""
    if text.startswith("//"):
        return (f'remote_ref_not_allowed: "{ref}" is a network location; this tool is offline '
                f"and never downloads schemas")
    if text.startswith("\\\\"):
        return (f'remote_ref_not_allowed: "{ref}" is a UNC network share; this tool is offline '
                f"and only reads local schema files")
    scheme, rest = _split_scheme(text)
    if not scheme:
        return ""
    if scheme == "file":
        if rest.startswith("///"):
            return ""  # file:///C:/x.json 是本地文件
        if rest.startswith("//"):
            host = rest[2:].split("/", 1)[0]
            if host and host.lower() != "localhost":
                return (f'remote_ref_not_allowed: "{ref}" points at host "{host}"; this tool is '
                        f"offline and only reads local schema files")
            return ""
        return ""
    if scheme in _NETWORK_SCHEMES or ":" in ref:
        return (f'remote_ref_not_allowed: "{ref}" uses the "{scheme}" scheme; this tool is offline '
                f"and never downloads schemas. Bundle the referenced schema into $defs, "
                f"or point $ref at a local file")
    return ""


def _uri_to_local_path(uri: str) -> Optional[str]:
    """把 ``file:`` URI 或相对路径转成本地路径；网络位置返回 ``None``。"""
    if remote_ref_error(uri):
        return None
    scheme, rest = _split_scheme(uri.strip())
    if scheme == "file":
        if rest.startswith("///"):
            rest = rest[2:]
        elif rest.startswith("//"):
            rest = rest[2:].split("/", 1)
            rest = rest[1] if len(rest) > 1 else ""
        path = _percent_decode(rest)
        if os.name == "nt" and path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]
        return path.replace("/", os.sep)
    path = _percent_decode(uri.strip())
    if path.startswith("/") and os.name == "nt" and len(path) > 2 and path[2] == ":":
        path = path[1:]
    return path.replace("/", os.sep)


# --------------------------------------------------------------------------- #
# JSON Pointer
# --------------------------------------------------------------------------- #
def _unescape_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def resolve_pointer(document: Any, pointer: str) -> Tuple[Any, str]:
    """按 RFC 6901 解析 JSON Pointer（迭代实现）。返回 ``(值, 错误码文本)``。"""
    if not pointer:
        return document, ""
    if not pointer.startswith("/"):
        return None, f'pointer_invalid: JSON Pointer "{pointer}" must start with "/"'
    node = document
    walked: List[str] = []
    for raw_token in pointer.split("/")[1:]:
        token = _unescape_token(raw_token)
        walked.append(token)
        if isinstance(node, dict):
            if token not in node:
                return None, (f'ref_unresolvable: JSON Pointer "{pointer}" has no target '
                              f'("{token}" is not a property of the object at '
                              f'"{"/".join(walked[:-1]) or "/"}")')
            node = node[token]
            continue
        if isinstance(node, list):
            if token == "-":
                return None, f'ref_unresolvable: JSON Pointer "{pointer}" uses the "-" append index'
            if not re.fullmatch(r"0|[1-9][0-9]*", token):
                return None, (f'ref_unresolvable: "{token}" is not a valid array index '
                              f'in "{pointer}"')
            index = int(token)
            if index >= len(node):
                return None, (f'ref_unresolvable: JSON Pointer "{pointer}" index {index} is out of '
                              f"range (the array has {len(node)} items)")
            node = node[index]
            continue
        return None, (f'ref_unresolvable: JSON Pointer "{pointer}" cannot descend into a '
                      f"{json_type(node)} value at \"{'/'.join(walked[:-1])}\"")
    return node, ""


# --------------------------------------------------------------------------- #
# 本地文件加载
# --------------------------------------------------------------------------- #
def load_schema_file(path: str) -> Tuple[Any, str]:
    """从**本地文件**读取 schema。返回 ``(schema, 错误码文本)``；绝不联网。

    ``schema`` 通常是 ``dict``；JSON Schema 也允许 ``true`` / ``false`` 布尔 schema，
    此时原样返回。解析先按严格 JSON，失败后退回 JSONC（允许注释/尾逗号）。
    """
    if not isinstance(path, str) or not path.strip():
        return None, "schema_file_invalid: a non-empty local file path is required"
    remote = remote_ref_error(path)
    if remote:
        return None, remote.replace("remote_ref_not_allowed", "remote_schema_not_allowed", 1)
    try:
        local = os.path.abspath(os.path.expanduser(path.strip()))
    except (OSError, ValueError) as exc:  # pragma: no cover - 防御
        return None, f"schema_file_invalid: cannot resolve path \"{path}\" ({exc})"
    read = read_text_file(local)
    if not read.ok:
        return None, f"schema_file_unreadable: {read.error}"
    parsed = parse_json(read.text, MODE_JSON, max_depth=SCHEMA_PARSE_MAX_DEPTH)
    if not parsed.ok:
        parsed = parse_json(read.text, MODE_JSONC, max_depth=SCHEMA_PARSE_MAX_DEPTH)
    if not parsed.ok:
        issue = parsed.error
        detail = issue.code if issue is not None else "parse_error"
        where = f" at line {issue.line}, column {issue.column}" if issue is not None else ""
        return None, f"schema_parse_error: {detail}{where} while reading {os.path.basename(local)}"
    value = parsed.value
    if not isinstance(value, (dict, bool)):
        return None, (f"schema_invalid: a JSON Schema must be an object or a boolean, "
                      f"got {json_type(value)} in {os.path.basename(local)}")
    return value, ""


# --------------------------------------------------------------------------- #
# $ref 解析器
# --------------------------------------------------------------------------- #
class _Document:
    """一份已加载的 schema 文档：正文 + 锚点索引 + 内嵌 ``$id`` 索引。"""

    __slots__ = ("schema", "identity", "path", "anchors", "ids")

    def __init__(self, schema: Any, identity: str, path: Optional[str]) -> None:
        self.schema = schema
        self.identity = identity
        self.path = path
        self.anchors: Dict[str, Any] = {}
        self.ids: Dict[str, Any] = {}


class LocalRefResolver:
    """本地 ``$ref`` 解析器。

    支持四种写法：``#``（本文档根）、``#/a/b``（JSON Pointer）、``#name``
    （``$anchor`` 或 draft-07 的 ``$id: "#name"``）、``other.json#/a``
    （在 ``base_dir`` / ``search_dirs`` 里查找的**本地**文件）。

    ``http`` / ``https`` / ``ftp`` / ``//host`` / ``\\\\host`` 一律拒绝，
    返回 ``remote_ref_not_allowed`` 错误文本，绝不发起任何网络请求。
    """

    def __init__(self, base_schema: Any = None, base_dir: Optional[str] = None,
                 search_dirs: Sequence[str] = (), *,
                 max_documents: int = MAX_REF_DOCUMENTS) -> None:
        self.base_dir = _safe_abspath(base_dir) if base_dir else None
        self.search_dirs: Tuple[str, ...] = tuple(
            d for d in (_safe_abspath(item) for item in (search_dirs or ())) if d
        )
        self.max_documents = max(1, int(max_documents))
        self.root_schema = base_schema
        self.remote_refs: List[str] = []
        self.documents: Dict[str, _Document] = {}
        self.root_document: Optional[_Document] = None
        self._file_cache: Dict[str, Tuple[Optional[_Document], str]] = {}
        if isinstance(base_schema, (dict, list)):
            self.root_document = self._index(base_schema, ROOT_IDENTITY, None)

    # -- 公共 API ---------------------------------------------------------- #
    def resolve(self, ref: str) -> Tuple[Any, str]:
        """解析 ``ref``，返回 ``(schema, 错误码文本)``；错误文本为空表示成功。"""
        schema, _doc, error = self.resolve_ref(ref, self.root_document)
        return schema, error

    def set_root(self, schema: Any) -> None:
        """在没有 root schema 时补上（:func:`validate` 会按需调用）。"""
        if self.root_document is None and isinstance(schema, (dict, list)):
            self.root_schema = schema
            self.root_document = self._index(schema, ROOT_IDENTITY, None)

    # -- 内部 -------------------------------------------------------------- #
    def resolve_ref(self, ref: Any, base_doc: Optional[_Document]
                    ) -> Tuple[Any, Optional[_Document], str]:
        """解析 ``ref``，额外返回目标所属文档（供后续相对引用与循环检测使用）。"""
        if not isinstance(ref, str) or not ref.strip():
            return None, None, 'ref_invalid: "$ref" must be a non-empty string'
        remote = remote_ref_error(ref)
        if remote:
            if ref not in self.remote_refs:
                self.remote_refs.append(ref)
            return None, None, remote
        document = base_doc if base_doc is not None else self.root_document
        if document is None:
            return None, None, ('ref_unresolvable: there is no base schema document to '
                                'resolve against')
        uri, sep, fragment = ref.partition("#")
        uri = uri.strip()
        base_node: Any = None
        if uri:
            document, embedded, error = self._resolve_uri(uri, document)
            if error:
                return None, None, error
            base_node = embedded
        if base_node is None:
            base_node = document.schema
        if not sep:
            return base_node, document, ""
        fragment = fragment.strip()
        if not fragment:
            return base_node, document, ""
        if fragment.startswith("/"):
            target, error = resolve_pointer(base_node, fragment)
            if error:
                return None, None, error
            if not isinstance(target, (dict, bool, list)):
                return None, None, (f'ref_unresolvable: "#{fragment}" points at a '
                                    f"{json_type(target)} value, which is not a schema")
            return target, document, ""
        node = document.anchors.get(fragment)
        if node is None:
            node = document.ids.get("#" + fragment)
        if node is None:
            return None, None, (f'ref_unresolvable: anchor "#{fragment}" is not defined in '
                                f"this schema")
        return node, document, ""

    def ref_identity(self, ref: str, document: Optional[_Document]) -> Tuple[Any, Any]:
        """由 ``ref`` 与它解析到的文档推出稳定标识，供 $ref 循环检测使用。"""
        uri, _, fragment = ref.partition("#")
        uri = uri.strip()
        fragment = fragment.strip()
        return (document.identity if document is not None else ROOT_IDENTITY,
                (uri, fragment if fragment else "#"))

    def _resolve_uri(self, uri: str, base_doc: _Document
                     ) -> Tuple[Optional[_Document], Any, str]:
        """解析带文档部分的引用：先查内嵌 ``$id``，再找本地文件。

        返回 ``(文档, 内嵌节点或 None, 错误)``；内嵌节点非 ``None`` 时它就是引用目标。
        """
        for document in (base_doc, self.root_document):
            if document is None:
                continue
            node = document.ids.get(uri)
            if node is None and uri.endswith("/"):
                node = document.ids.get(uri.rstrip("/"))
            if node is not None:
                return document, node, ""
        local = _uri_to_local_path(uri)
        if local is None:
            return None, None, remote_ref_error(uri)
        document, error = self._load_file(local)
        return document, None, error

    def _load_file(self, path: str) -> Tuple[Optional[_Document], str]:
        """在 base_dir / search_dirs / 当前目录里查找并加载本地 schema 文件。"""
        candidates: List[str] = []
        if os.path.isabs(path):
            candidates.append(path)
        else:
            roots: List[Optional[str]] = [self.base_dir]
            roots.extend(self.search_dirs)
            roots.append(None)
            for root in roots:
                candidates.append(os.path.join(root, path) if root else path)
        tried: List[str] = []
        for candidate in candidates:
            normalized = _safe_abspath(candidate)
            if not normalized:
                continue
            tried.append(normalized)
            if normalized in self._file_cache:
                document, error = self._file_cache[normalized]
                return document, error
            if not os.path.isfile(normalized):
                continue
            if len(self.documents) >= self.max_documents:
                return None, (f"ref_too_many_documents: more than {self.max_documents} schema "
                              f"files were referenced; refusing to load \"{path}\"")
            schema, error = load_schema_file(normalized)
            if error:
                self._file_cache[normalized] = (None, error)
                return None, error
            document = self._index(schema, normalized, normalized)
            self._file_cache[normalized] = (document, "")
            return document, ""
        searched = ", ".join(os.path.dirname(item) or "." for item in tried) or "."
        return None, (f'ref_file_not_found: cannot find the local schema file "{path}" '
                      f"(searched: {searched})")

    def _index(self, schema: Any, identity: str, path: Optional[str]) -> _Document:
        """迭代式建立锚点与内嵌 ``$id`` 索引。"""
        document = _Document(schema, identity, path)
        self.documents[identity] = document
        if not isinstance(schema, dict):
            return document
        stack: List[Any] = [schema]
        seen = 0
        while stack:
            node = stack.pop()
            seen += 1
            if seen > 200_000:  # pragma: no cover - 防御：超大 schema
                break
            if isinstance(node, dict):
                anchor = node.get("$anchor")
                if isinstance(anchor, str) and anchor:
                    document.anchors.setdefault(anchor, node)
                ident = node.get("$id")
                if isinstance(ident, str) and ident:
                    _register_id(document, ident, node)
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        return document


def _register_id(document: _Document, ident: str, node: Any) -> None:
    """登记一个内嵌 ``$id``：原文、末段文件名、``#name`` 形式都可被引用。"""
    document.ids.setdefault(ident, node)
    if ident.startswith("#"):
        name = ident[1:]
        if name:
            document.anchors.setdefault(name, node)
        return
    tail = ident.rstrip("/").rsplit("/", 1)[-1]
    if tail:
        document.ids.setdefault(tail, node)
        base, sep, fragment = tail.partition("#")
        if sep and fragment and not fragment.startswith("/"):
            document.anchors.setdefault(fragment, node)
        if base:
            document.ids.setdefault(base, node)


def _safe_abspath(path: Any) -> str:
    try:
        return os.path.abspath(os.path.expanduser(str(path)))
    except (OSError, ValueError):  # pragma: no cover - 防御
        return ""


# --------------------------------------------------------------------------- #
# draft 识别
# --------------------------------------------------------------------------- #
def detect_draft(schema: Any) -> str:
    """识别 schema 使用的 draft 版本。

    优先看 ``$schema``；无法识别的 ``$schema`` 返回 ``"unknown"``。
    没有 ``$schema`` 时按关键字特征嗅探（``prefixItems`` -> 2020-12，
    ``$defs`` / ``dependentSchemas`` -> 2019-09，布尔型 ``exclusiveMinimum``
    -> draft-04），其余按本引擎的默认基线 ``draft-07`` 处理。
    """
    if not isinstance(schema, dict):
        return DRAFT_UNKNOWN
    declared = schema.get("$schema")
    if isinstance(declared, str) and declared.strip():
        text = declared.strip().lower()
        for needle, draft in _DRAFT_BY_URI:
            if needle in text:
                return draft
        return DRAFT_UNKNOWN
    # 关键字嗅探（迭代式，避免深层 schema 递归）
    has_2020 = has_2019 = has_04 = False
    stack: List[Any] = [schema]
    seen = 0
    while stack and seen < 200_000:
        node = stack.pop()
        seen += 1
        if isinstance(node, dict):
            if "prefixItems" in node or "$dynamicRef" in node or "unevaluatedProperties" in node:
                has_2020 = True
            if ("$defs" in node or "dependentSchemas" in node or "dependentRequired" in node
                    or "$recursiveRef" in node or "minContains" in node):
                has_2019 = True
            for key in ("exclusiveMinimum", "exclusiveMaximum"):
                if isinstance(node.get(key), bool):
                    has_04 = True
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    if has_2020:
        return DRAFT_2020_12
    if has_2019:
        return DRAFT_2019_09
    if has_04:
        return DRAFT_04
    return DRAFT_07


# --------------------------------------------------------------------------- #
# format 断言
# --------------------------------------------------------------------------- #
_RE_DATE = re.compile(r"\A(\d{4})-(\d{2})-(\d{2})\Z")
_RE_TIME = re.compile(r"\A(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?([Zz]|[+-]\d{2}:\d{2})\Z")
_RE_DATETIME = re.compile(
    r"\A(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?([Zz]|[+-]\d{2}:\d{2})\Z")
_RE_UUID = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_RE_EMAIL_LABEL = re.compile(r"\A[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\Z")
_RE_URI_SCHEME = re.compile(r"\A[A-Za-z][A-Za-z0-9+.\-]*:")
_URI_FORBIDDEN = frozenset('<>"{}|\\^`')
_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _is_leap_year(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _valid_date_parts(year: int, month: int, day: int) -> bool:
    if month < 1 or month > 12 or day < 1:
        return False
    limit = _DAYS_IN_MONTH[month - 1]
    if month == 2 and _is_leap_year(year):
        limit = 29
    return day <= limit


def _valid_time_parts(hour: int, minute: int, second: int, offset: str) -> bool:
    if hour > 23 or minute > 59:
        return False
    if second > 60:  # 60 只在闰秒时合法
        return False
    if offset and offset not in ("Z", "z"):
        sign_hour = int(offset[1:3])
        sign_minute = int(offset[4:6])
        if sign_hour > 23 or sign_minute > 59:
            return False
    return True


def _check_date(text: str) -> bool:
    match = _RE_DATE.match(text)
    if not match:
        return False
    year, month, day = (int(part) for part in match.groups())
    return _valid_date_parts(year, month, day)


def _check_time(text: str) -> bool:
    match = _RE_TIME.match(text)
    if not match:
        return False
    hour, minute, second, _frac, offset = match.groups()
    return _valid_time_parts(int(hour), int(minute), int(second), offset)


def _check_date_time(text: str) -> bool:
    match = _RE_DATETIME.match(text)
    if not match:
        return False
    year, month, day, hour, minute, second, _frac, offset = match.groups()
    return (_valid_date_parts(int(year), int(month), int(day))
            and _valid_time_parts(int(hour), int(minute), int(second), offset))


def _check_email(text: str) -> bool:
    """基础 email 检查：单个 ``@``、无空白、域名至少两段且每段是合法 label。"""
    if not text or text.count("@") != 1:
        return False
    local, _, domain = text.partition("@")
    if not local or not domain:
        return False
    if len(local) > 64 or len(domain) > 255:
        return False
    if any(ch.isspace() or ch in '<>()[]\\,;:"' for ch in local):
        return False
    if domain.startswith(".") or domain.endswith(".") or ".." in domain:
        return False
    labels = domain.split(".")
    if len(labels) < 2:
        return False
    return all(_RE_EMAIL_LABEL.match(label) for label in labels)


def _check_uri(text: str) -> bool:
    """基础 URI 检查：必须有 scheme，且只含可见 ASCII（非 ASCII 属于 IRI）。"""
    if not text or not _RE_URI_SCHEME.match(text):
        return False
    for ch in text:
        code = ord(ch)
        if code <= 0x20 or code > 0x7E or ch in _URI_FORBIDDEN:
            return False
    return True


def _check_uuid(text: str) -> bool:
    return bool(_RE_UUID.match(text))


def _check_ipv4(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text)
        return True
    except (ValueError, ipaddress.AddressValueError):
        return False


def _check_ipv6(text: str) -> bool:
    try:
        ipaddress.IPv6Address(text)
        return True
    except (ValueError, ipaddress.AddressValueError):
        return False


_FORMAT_CHECKS = {
    "date-time": (_check_date_time, "an RFC 3339 date-time, e.g. 2024-05-01T12:30:00Z"),
    "date": (_check_date, "an RFC 3339 full date, e.g. 2024-05-01"),
    "time": (_check_time, "an RFC 3339 full time, e.g. 12:30:00Z"),
    "email": (_check_email, "an email address, e.g. name@example.com"),
    "uri": (_check_uri, "an absolute URI, e.g. https://example.com/a?b=1"),
    "uuid": (_check_uuid, "an RFC 4122 UUID, e.g. 550e8400-e29b-41d4-a716-446655440000"),
    "ipv4": (_check_ipv4, "an IPv4 address, e.g. 192.168.0.1"),
    "ipv6": (_check_ipv6, "an IPv6 address, e.g. 2001:0db8::7334"),
}


# --------------------------------------------------------------------------- #
# 校验任务与执行状态
# --------------------------------------------------------------------------- #
class _Task:
    """一个待校验的 (实例节点, schema) 组合。"""

    __slots__ = ("instance", "schema", "path", "schema_path", "refs", "depth", "kind", "doc")

    def __init__(self, instance: Any, schema: Any, path: Tuple, schema_path: Tuple,
                 refs: Optional[Tuple], depth: int, kind: str, doc: Optional[_Document]) -> None:
        self.instance = instance
        self.schema = schema
        self.path = path
        self.schema_path = schema_path
        self.refs = refs
        self.depth = depth
        self.kind = kind
        self.doc = doc


class _State:
    """一次 :meth:`_Validator._walk` 的局部状态（支持嵌套的子校验）。"""

    __slots__ = ("collect", "cap", "errors", "failed")

    def __init__(self, collect: bool, cap: int) -> None:
        self.collect = collect
        self.cap = cap
        self.errors: List[SchemaError] = []
        self.failed = False


class _Plan:
    """编译好的 schema：断言列表、展开列表、结构问题、未知关键字。"""

    __slots__ = ("schema", "checks", "applicators", "problems", "unknown", "unsupported",
                 "ref", "draft_notes")

    def __init__(self, schema: dict) -> None:
        self.schema = schema
        self.checks: Tuple[Tuple[Any, Any, str], ...] = ()
        self.applicators: Tuple[Tuple[Any, Any, str], ...] = ()
        self.problems: Tuple[Tuple[str, str, Any, Any], ...] = ()
        self.unknown: Tuple[str, ...] = ()
        self.unsupported: Tuple[str, ...] = ()
        self.ref: Optional[str] = None
        self.draft_notes: Tuple[str, ...] = ()


class _Validator:
    """迭代式校验器。所有关键字处理器返回 ``True``（通过）/``False``（失败）。"""

    def __init__(self, resolver: LocalRefResolver, cap: int, draft: str,
                 max_steps: int = MAX_STEPS) -> None:
        self.resolver = resolver
        self.cap = max(1, cap)
        self.draft = draft
        self.ref_siblings = draft in (DRAFT_2019_09, DRAFT_2020_12)
        self.max_steps = max_steps
        self.steps = 0
        self.budget_exceeded = False
        self.hit_error_cap = False
        self.errors: List[SchemaError] = []
        self.warnings: List[str] = []
        self._warned: set = set()
        self._suppressed = 0
        self._problem_seen: set = set()
        self._ref_error_seen: set = set()
        self._plans: Dict[int, _Plan] = {}
        self._state: Optional[_State] = None

    # -- 错误 / 告警 ------------------------------------------------------- #
    def _fail(self, task: _Task, keyword: str, message: str, *, expected: Any = None,
              actual: Any = None, path: Optional[Tuple] = None,
              schema_path: Optional[Tuple] = None) -> bool:
        """记录一条错误并返回 ``False``（quiet 模式下只置 failed，不建对象）。"""
        state = self._state
        if state is None:  # pragma: no cover - 防御
            return False
        state.failed = True
        if not state.collect:
            return False
        if len(state.errors) >= state.cap:
            return False
        target = task.path if path is None else path
        where = task.schema_path + (keyword,) if schema_path is None else tuple(schema_path)
        state.errors.append(SchemaError(
            path=tuple(target),
            path_string=path_string(target),
            keyword=keyword,
            message=message,
            expected=expected,
            actual=actual,
            schema_path=where,
        ))
        return False

    def _warn(self, message: str) -> None:
        """去重后追加一条告警；超过上限只计数。"""
        if message in self._warned:
            self._suppressed += 1
            return
        if len(self._warned) > 20_000:  # pragma: no cover - 防御
            self._suppressed += 1
            return
        self._warned.add(message)
        if len(self.warnings) >= MAX_WARNINGS:
            self._suppressed += 1
            return
        self.warnings.append(message)

    def finish_warnings(self) -> List[str]:
        warnings = list(self.warnings)
        if self._suppressed:
            warnings.append(f"warnings_suppressed: {self._suppressed} more warnings were omitted")
        return warnings

    def _report_problem(self, task: _Task, problem: Tuple[str, str, Any, Any]) -> None:
        """schema 本身写错（例如 ``"type": 123``）：同一个 schema 只报一次。"""
        keyword = problem[0]
        marker = (id(task.schema), keyword, task.schema_path)
        if marker in self._problem_seen:
            return
        self._problem_seen.add(marker)
        self._fail(task, keyword, problem[1], expected=problem[2], actual=problem[3])

    # -- 主循环 ------------------------------------------------------------ #
    def _walk(self, instance: Any, schema: Any, path: Tuple, schema_path: Tuple,
              refs: Optional[Tuple], *, collect: bool, cap: int,
              doc: Optional[_Document] = None, main: bool = False,
              depth: int = 0) -> Tuple[bool, List[SchemaError]]:
        """迭代式校验入口。``collect=False`` 时首次失败即返回（子 schema 判定用）。"""
        state = _State(collect, cap)
        saved = self._state
        self._state = state
        stack: List[_Task] = [
            _Task(instance, schema, path, schema_path, refs, depth, json_type(instance), doc)
        ]
        try:
            while stack:
                if self.steps >= self.max_steps:
                    self.budget_exceeded = True
                    break
                task = stack.pop()
                self.steps += 1
                if task.depth > MAX_INSTANCE_DEPTH:
                    self._fail(task, "depth_exceeded",
                               f"the instance nests deeper than the {MAX_INSTANCE_DEPTH}-level "
                               f"validation limit, so it was not checked any further")
                    if not collect:
                        break
                    continue
                if not self._process(task, state, stack):
                    if not collect:
                        break
                if collect and main and len(state.errors) >= state.cap and stack:
                    self.hit_error_cap = True
                    break
        finally:
            self._state = saved
        return (not state.failed), state.errors

    def _process(self, task: _Task, state: _State, stack: List[_Task]) -> bool:
        schema = task.schema
        if schema is True:
            return True
        if schema is False:
            return self._fail(task, "false",
                              "the schema is 'false', so no value can be valid here",
                              expected="any value except this one", actual=_describe(task.instance))
        if not isinstance(schema, dict):
            return self._fail(
                task, "schema_invalid",
                f"a JSON Schema must be an object or a boolean, got {json_type(schema)}",
                expected="object or boolean", actual=_describe(schema))
        if not schema:
            return True

        plan = self._plan(schema)
        for problem in plan.problems:
            self._report_problem(task, problem)
        if plan.draft_notes:
            for note in plan.draft_notes:
                self._warn(note)
        if plan.unknown or plan.unsupported:
            where = _pointer_of(task.schema_path)
            for keyword in plan.unknown:
                self._warn(f'unknown_keyword: "{keyword}" at {where} is not a JSON Schema '
                           f"keyword and was ignored")
            for keyword in plan.unsupported:
                self._warn(f'keyword_unsupported: "{keyword}" at {where} is recognized but not '
                           f"implemented; it was ignored, so validation may be more permissive "
                           f"than the schema asks for")

        if plan.ref is not None:
            if not self._apply_ref(task, plan.ref, stack):
                return False
            if not self.ref_siblings:
                siblings = [key for key in schema
                            if key != "$ref" and key not in _ANNOTATION_KEYWORDS]
                if siblings:
                    self._warn(
                        "ref_siblings_ignored: "
                        f"{_pointer_of(task.schema_path)} has "
                        f"{', '.join(repr(s) for s in sorted(siblings))} next to \"$ref\"; "
                        f"{self.draft} ignores keywords beside \"$ref\", so they were not applied")
                return True

        ok = True
        for handler, prepared, keyword in plan.checks:
            if not handler(task, prepared):
                ok = False
                if not state.collect:
                    return False
        for handler, prepared, keyword in plan.applicators:
            if not handler(task, prepared, state, stack):
                ok = False
                if not state.collect:
                    return False
        return ok

    # -- $ref -------------------------------------------------------------- #
    def _apply_ref(self, task: _Task, ref: str, stack: List[_Task]) -> bool:
        target, document, error = self.resolver.resolve_ref(ref, task.doc)
        if error:
            return self._ref_fail(task, ref, error)
        identity = self.resolver.ref_identity(ref, document)
        key = (identity, id(task.instance))
        chain = task.refs
        depth = 0
        node = chain
        while node:  # 链表以空元组结尾
            if node[0] == key:
                return self._ref_fail(
                    task, ref,
                    f'ref_cycle: "$ref" "{ref}" resolves back to a schema that is already being '
                    f"applied to this same value, which would loop forever; the branch was stopped")
            node = node[1]
            depth += 1
            if depth > MAX_REF_CHAIN:  # pragma: no cover - 防御
                return self._ref_fail(
                    task, ref,
                    f'ref_depth_exceeded: "$ref" "{ref}" nested deeper than {MAX_REF_CHAIN} '
                    f"levels; the branch was stopped")
        stack.append(_Task(task.instance, target, task.path, task.schema_path + ("$ref",),
                           (key, chain), task.depth, task.kind, document))
        return True

    def _ref_fail(self, task: _Task, ref: str, error: str) -> bool:
        """``$ref`` 相关错误按 (ref, schema_path) 去重，避免同一问题刷满错误列表。"""
        marker = (ref, task.schema_path)
        keyword, _, detail = error.partition(":")
        keyword = keyword.strip() or "$ref"
        if keyword == "remote_ref_not_allowed":
            self._warn(error)
        if marker not in self._ref_error_seen:
            self._ref_error_seen.add(marker)
            return self._fail(task, keyword, detail.strip() or error,
                              expected=f'a $ref that resolves locally ("{ref}")',
                              actual=error)
        state = self._state
        if state is not None:
            state.failed = True
        return False

    # -- 子 schema 判定 ---------------------------------------------------- #
    def _child_path(self, task: _Task, key: Any) -> Tuple:
        state = self._state
        if state is None or not state.collect:
            return ()
        return task.path + (key,)

    def _check(self, instance: Any, schema: Any, task: _Task, keyword: str,
               extra_schema_path: Any = None) -> bool:
        """quiet 模式：只问"是否通过"，不收集错误。"""
        schema_path = task.schema_path + ((keyword,) if extra_schema_path is None
                                          else (keyword, extra_schema_path))
        ok, _errors = self._walk(instance, schema, (), schema_path, task.refs,
                                 collect=False, cap=0, doc=task.doc, depth=task.depth)
        return ok

    def _check_with_reasons(self, instance: Any, schema: Any, task: _Task, keyword: str,
                            index: int) -> Tuple[bool, List[SchemaError]]:
        """收集少量错误用于 oneOf / anyOf 的失败原因说明。"""
        collecting = bool(self._state is not None and self._state.collect)
        ok, errors = self._walk(instance, schema, task.path, task.schema_path + (keyword, index),
                                task.refs, collect=collecting,
                                cap=MAX_BRANCH_ERRORS if collecting else 0,
                                doc=task.doc, depth=task.depth)
        return ok, errors

    def _new_task(self, task: _Task, instance: Any, schema: Any, key: Any,
                  schema_path: Tuple) -> _Task:
        return _Task(instance, schema, self._child_path(task, key), schema_path, task.refs,
                     task.depth + 1, json_type(instance), task.doc)

    def _sibling_task(self, task: _Task, schema: Any, schema_path: Tuple) -> _Task:
        """子 schema 作用在**同一个**实例节点上（allOf / dependentSchemas / if-then-else）。"""
        return _Task(task.instance, schema, task.path, schema_path, task.refs,
                     task.depth, task.kind, task.doc)

    # -- schema 编译（按 id 缓存） ----------------------------------------- #
    def _plan(self, schema: dict) -> _Plan:
        marker = id(schema)
        cached = self._plans.get(marker)
        if cached is not None and cached.schema is schema:
            return cached
        plan = _Plan(schema)
        problems: List[Tuple[str, str, Any, Any]] = []
        checks: List[Tuple[Any, Any, str]] = []
        applicators: List[Tuple[Any, Any, str]] = []
        unknown: List[str] = []
        unsupported: List[str] = []
        draft_notes: List[str] = []

        for keyword in schema:
            if keyword in _KNOWN_KEYWORDS:
                if keyword in _UNSUPPORTED_KEYWORDS:
                    unsupported.append(keyword)
            else:
                unknown.append(keyword)
        unsupported.sort()
        rank = _draft_rank(self.draft)
        if self.draft != DRAFT_UNKNOWN:
            for keyword in sorted(schema.keys()):
                needs = _LATER_KEYWORDS.get(keyword)
                if needs is not None and rank < _draft_rank(needs):
                    draft_notes.append(
                        f'keyword_draft_mismatch: "{keyword}" belongs to {needs} but this schema '
                        f"is {self.draft}; it was applied anyway")

        ref = schema.get("$ref")
        if ref is not None:
            if isinstance(ref, str) and ref.strip():
                plan.ref = ref
            else:
                problems.append(("$ref", '"$ref" must be a non-empty string, got '
                                         f"{_describe(ref)}", "string", _describe(ref)))

        for keyword in _CHECK_ORDER:
            if keyword not in schema:
                continue
            prepared = getattr(self, "_prep_" + _snake(keyword))(schema, problems)
            if prepared is not None:
                checks.append((getattr(self, "_h_" + _snake(keyword)), prepared, keyword))

        self._compile_applicators(schema, problems, applicators)

        plan.checks = tuple(checks)
        plan.applicators = tuple(applicators)
        plan.problems = tuple(problems)
        plan.unknown = tuple(unknown)
        plan.unsupported = tuple(unsupported)
        plan.draft_notes = tuple(draft_notes)
        self._plans[marker] = plan
        return plan

    def _compile_applicators(self, schema: dict, problems: List, applicators: List) -> None:
        for keyword in _APPLICATOR_ORDER:
            if keyword not in schema:
                continue
            if keyword in ("prefixItems", "items", "additionalItems"):
                continue  # 三者一起处理
            prepared = getattr(self, "_prep_" + _snake(keyword))(schema, problems)
            if prepared is not None:
                applicators.append((getattr(self, "_h_" + _snake(keyword)), prepared, keyword))
        self._compile_items(schema, problems, applicators)

    def _compile_items(self, schema: dict, problems: List, applicators: List) -> None:
        """``items`` / ``prefixItems`` / ``additionalItems`` 的 draft 差异集中在这里。"""
        raw_items = schema.get("items", _MISSING)
        raw_prefix = schema.get("prefixItems", _MISSING)
        raw_additional = schema.get("additionalItems", _MISSING)
        if raw_items is _MISSING and raw_prefix is _MISSING and raw_additional is _MISSING:
            return

        if raw_prefix is not _MISSING:
            # 2020-12 语义：prefixItems 是元组形式，items 是剩余元素
            if not isinstance(raw_prefix, list) or not raw_prefix:
                problems.append(("prefixItems",
                                 '"prefixItems" must be a non-empty array of schemas, got '
                                 f"{_describe(raw_prefix)}", "array of schemas",
                                 _describe(raw_prefix)))
                prefix: Tuple[Any, ...] = ()
            else:
                prefix = tuple(raw_prefix)
            if prefix:
                applicators.append((self._h_prefix_items, prefix, "prefixItems"))
            if raw_items is not _MISSING:
                if isinstance(raw_items, list):
                    problems.append(("items",
                                     'in 2020-12 "items" must be a single schema; the array form '
                                     "moved to \"prefixItems\"", "a schema", _describe(raw_items)))
                elif isinstance(raw_items, (dict, bool)):
                    applicators.append((self._h_items_rest, (len(prefix), raw_items, "items"),
                                        "items"))
            if raw_additional is not _MISSING:
                self._warn('keyword_unsupported: "additionalItems" is obsolete in 2020-12 next to '
                           '"prefixItems" (use "items" for the remaining elements); it was ignored')
            return

        if raw_items is _MISSING:
            if raw_additional is not _MISSING:
                problems.append(("additionalItems",
                                 '"additionalItems" has no effect without the array form of '
                                 '"items"', "an \"items\" array", "missing"))
            return

        if isinstance(raw_items, list):
            # draft-07 元组形式
            if not raw_items:
                problems.append(("items",
                                 'the array form of "items" must not be empty; use "items": {} '
                                 "to allow anything", "a non-empty array of schemas", "[]"))
                return
            applicators.append((self._h_items_tuple, tuple(raw_items), "items"))
            if raw_additional is not _MISSING:
                if raw_additional is False or isinstance(raw_additional, dict):
                    applicators.append((self._h_items_rest,
                                        (len(raw_items), raw_additional, "additionalItems"),
                                        "additionalItems"))
                else:
                    problems.append(("additionalItems",
                                     '"additionalItems" must be a boolean or a schema, got '
                                     f"{_describe(raw_additional)}", "boolean or schema",
                                     _describe(raw_additional)))
            return

        if isinstance(raw_items, (dict, bool)):
            applicators.append((self._h_items_rest, (0, raw_items, "items"), "items"))
            if raw_additional is not _MISSING:
                self._warn('keyword_unsupported: "additionalItems" only applies to the array form '
                           'of "items"; it was ignored')
            return

        problems.append(("items", f'"items" must be a schema or an array of schemas, got '
                                  f"{_describe(raw_items)}", "schema or array of schemas",
                         _describe(raw_items)))


# --------------------------------------------------------------------------- #
# 编译期小工具
# --------------------------------------------------------------------------- #
class _Missing:
    """区分"关键字不存在"与"关键字的值是 None"。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "<missing>"


_MISSING = _Missing()


def _snake(keyword: str) -> str:
    """``exclusiveMinimum`` -> ``exclusive_minimum``；``$ref`` -> ``ref``。"""
    text = keyword.lstrip("$")
    out: List[str] = []
    for ch in text:
        if ch.isupper():
            out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out)


def _draft_rank(draft: str) -> int:
    order = {DRAFT_03: 3, DRAFT_04: 4, DRAFT_06: 6, DRAFT_07: 7,
             DRAFT_2019_09: 8, DRAFT_2020_12: 9, DRAFT_UNKNOWN: 99}
    return order.get(draft, 7)


def _pointer_of(schema_path: Sequence) -> str:
    """schema 路径 -> JSON Pointer 文本（根为 ``#``）。"""
    if not schema_path:
        return "#"
    return "#/" + "/".join(_escape_pointer(str(part)) for part in schema_path)


def _escape_pointer(text: str) -> str:
    return text.replace("~", "~0").replace("/", "~1")


def _type_text(allowed: Sequence[str]) -> str:
    if len(allowed) == 1:
        return allowed[0]
    return "one of [" + ", ".join(allowed) + "]"


# --------------------------------------------------------------------------- #
# 关键字：准备阶段（返回 None 表示该关键字无效，已记入 problems）
# --------------------------------------------------------------------------- #
def _prep_type(self: _Validator, schema: dict, problems: List) -> Any:
    raw = schema["type"]
    if isinstance(raw, str):
        names = [raw]
    elif isinstance(raw, list) and raw and all(isinstance(item, str) for item in raw):
        names = list(dict.fromkeys(raw))
    else:
        problems.append(("type", f'"type" must be a string or a non-empty array of strings, got '
                                 f"{_describe(raw)}", "string or array of strings", _describe(raw)))
        return None
    invalid = [name for name in names if name not in TYPE_ORDER]
    if invalid:
        problems.append(("type", f'"type" contains unknown type names: '
                                 f"{', '.join(repr(n) for n in invalid)}; allowed are "
                                 f"{', '.join(TYPE_ORDER)}",
                         list(TYPE_ORDER), invalid))
        names = [name for name in names if name in TYPE_ORDER]
        if not names:
            return None
    return (frozenset(names), tuple(names), _type_text(names), "number" in names,
            "integer" in names)


def _prep_const(self: _Validator, schema: dict, problems: List) -> Any:
    value = schema["const"]
    return (_canonical_text(value), _describe(value))


def _prep_enum(self: _Validator, schema: dict, problems: List) -> Any:
    raw = schema["enum"]
    if not isinstance(raw, list) or not raw:
        problems.append(("enum", f'"enum" must be a non-empty array, got {_describe(raw)}',
                         "non-empty array", _describe(raw)))
        return None
    texts = {}
    rendered: List[str] = []
    for value in raw:
        text = _canonical_text(value)
        texts.setdefault(text, value)
        rendered.append(_describe(value, 40))
    if len(rendered) > 8:
        rendered = rendered[:8] + [f"… ({len(raw)} values in total)"]
    return (texts, rendered, len(raw))


def _prep_number_bound(name: str, keyword: str):
    def prepare(self: _Validator, schema: dict, problems: List) -> Any:
        raw = schema[keyword]
        if isinstance(raw, bool):
            if keyword.startswith("exclusive"):
                problems.append((keyword,
                                 f'"{keyword}" is a boolean here, which is the draft-04 form; '
                                 f"draft-06 and later require a number. Rewrite it as "
                                 f'"{keyword[9:].lower()}": <number> plus this numeric form',
                                 "number", _describe(raw)))
            else:
                problems.append((keyword, f'"{keyword}" must be a number, got boolean',
                                 "number", _describe(raw)))
            return None
        bound = _bound_decimal(raw)
        if bound is None:
            problems.append((keyword, f'"{keyword}" must be a number, got {_describe(raw)}',
                             "number", _describe(raw)))
            return None
        return (bound, name, _scalar_text(raw))
    return prepare


_prep_maximum = _prep_number_bound("maximum", "maximum")
_prep_minimum = _prep_number_bound("minimum", "minimum")
_prep_exclusive_maximum = _prep_number_bound("exclusiveMaximum", "exclusiveMaximum")
_prep_exclusive_minimum = _prep_number_bound("exclusiveMinimum", "exclusiveMinimum")


def _prep_multiple_of(self: _Validator, schema: dict, problems: List) -> Any:
    raw = schema["multipleOf"]
    divisor = _bound_decimal(raw)
    if divisor is None:
        problems.append(("multipleOf", f'"multipleOf" must be a number, got {_describe(raw)}',
                         "number", _describe(raw)))
        return None
    if divisor <= 0:
        problems.append(("multipleOf",
                         f'"multipleOf" must be greater than 0, got {_scalar_text(raw)}',
                         "a number greater than 0", _scalar_text(raw)))
        return None
    return (divisor, _scalar_text(raw))


def _prep_int_bound(name: str, keyword: str):
    def prepare(self: _Validator, schema: dict, problems: List) -> Any:
        raw = schema[keyword]
        if isinstance(raw, bool) or not isinstance(raw, int):
            value = _as_decimal(raw)
            if value is None or value != value.to_integral_value() or value < 0:
                problems.append((keyword,
                                 f'"{keyword}" must be a non-negative integer, got '
                                 f"{_describe(raw)}", "non-negative integer", _describe(raw)))
                return None
            return (int(value), name)
        if raw < 0:
            problems.append((keyword, f'"{keyword}" must be non-negative, got {raw}',
                             "non-negative integer", raw))
            return None
        return (raw, name)
    return prepare


_prep_max_length = _prep_int_bound("maxLength", "maxLength")
_prep_min_length = _prep_int_bound("minLength", "minLength")
_prep_max_items = _prep_int_bound("maxItems", "maxItems")
_prep_min_items = _prep_int_bound("minItems", "minItems")
_prep_max_properties = _prep_int_bound("maxProperties", "maxProperties")
_prep_min_properties = _prep_int_bound("minProperties", "minProperties")


def _prep_pattern(self: _Validator, schema: dict, problems: List) -> Any:
    raw = schema["pattern"]
    if not isinstance(raw, str):
        problems.append(("pattern", f'"pattern" must be a string, got {_describe(raw)}',
                         "string", _describe(raw)))
        return None
    try:
        return (re.compile(raw), raw)
    except re.error as exc:
        self._warn(f'pattern_invalid: the regular expression {raw!r} cannot be compiled '
                   f"({exc}); the \"pattern\" check was skipped")
        return None


def _prep_format(self: _Validator, schema: dict, problems: List) -> Any:
    raw = schema["format"]
    if not isinstance(raw, str) or not raw:
        problems.append(("format", f'"format" must be a non-empty string, got {_describe(raw)}',
                         "string", _describe(raw)))
        return None
    if raw in _FORMAT_CHECKS:
        check, description = _FORMAT_CHECKS[raw]
        return (raw, check, description, True)
    if raw in ANNOTATION_FORMATS:
        return (raw, None, "", False)
    return (raw, None, "", False)


def _prep_content_encoding(self: _Validator, schema: dict, problems: List) -> Any:
    raw = schema["contentEncoding"]
    if not isinstance(raw, str):
        problems.append(("contentEncoding",
                         f'"contentEncoding" must be a string, got {_describe(raw)}',
                         "string", _describe(raw)))
        return None
    return raw


def _prep_content_media_type(self: _Validator, schema: dict, problems: List) -> Any:
    raw = schema["contentMediaType"]
    if not isinstance(raw, str):
        problems.append(("contentMediaType",
                         f'"contentMediaType" must be a string, got {_describe(raw)}',
                         "string", _describe(raw)))
        return None
    return raw


def _prep_unique_items(self: _Validator, schema: dict, problems: List) -> Any:
    raw = schema["uniqueItems"]
    if not isinstance(raw, bool):
        problems.append(("uniqueItems", f'"uniqueItems" must be a boolean, got {_describe(raw)}',
                         "boolean", _describe(raw)))
        return None
    return raw


def _prep_contains(self: _Validator, schema: dict, problems: List) -> Any:
    raw = schema["contains"]
    if not isinstance(raw, (dict, bool)):
        problems.append(("contains", f'"contains" must be a schema, got {_describe(raw)}',
                         "schema", _describe(raw)))
        return None
    minimum = 1
    maximum = None
    raw_min = schema.get("minContains")
    raw_max = schema.get("maxContains")
    if raw_min is not None:
        value = _as_decimal(raw_min)
        if value is None or value != value.to_integral_value() or value < 0:
            problems.append(("minContains",
                             f'"minContains" must be a non-negative integer, got '
                             f"{_describe(raw_min)}", "non-negative integer",
                             _describe(raw_min)))
        else:
            minimum = int(value)
    if raw_max is not None:
        value = _as_decimal(raw_max)
        if value is None or value != value.to_integral_value() or value < 0:
            problems.append(("maxContains",
                             f'"maxContains" must be a non-negative integer, got '
                             f"{_describe(raw_max)}", "non-negative integer",
                             _describe(raw_max)))
        else:
            maximum = int(value)
    if maximum is not None and maximum < minimum:
        problems.append(("maxContains",
                         f'"maxContains" ({maximum}) is smaller than "minContains" ({minimum}); '
                         f"no array can satisfy both", "maxContains >= minContains", maximum))
    return (raw, minimum, maximum)


def _prep_required(self: _Validator, schema: dict, problems: List) -> Any:
    raw = schema["required"]
    if not isinstance(raw, list):
        problems.append(("required", f'"required" must be an array of property names, got '
                                     f"{_describe(raw)}", "array of strings", _describe(raw)))
        return None
    names: List[str] = []
    for item in raw:
        if not isinstance(item, str):
            problems.append(("required", f'"required" must contain only strings, got '
                                         f"{_describe(item)}", "array of strings",
                             _describe(item)))
            continue
        if item not in names:
            names.append(item)
    return tuple(names)


def _prep_dependent_required(self: _Validator, schema: dict, problems: List) -> Any:
    raw = schema["dependentRequired"]
    if not isinstance(raw, dict):
        problems.append(("dependentRequired",
                         f'"dependentRequired" must be an object, got {_describe(raw)}',
                         "object", _describe(raw)))
        return None
    mapping: Dict[str, Tuple[str, ...]] = {}
    for key, value in raw.items():
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            problems.append(("dependentRequired",
                             f'"dependentRequired.{key}" must be an array of property names, got '
                             f"{_describe(value)}", "array of strings", _describe(value)))
            continue
        mapping[key] = tuple(dict.fromkeys(value))
    return mapping or None


def _prep_dependencies(self: _Validator, schema: dict, problems: List) -> Any:
    """draft-07 的 ``dependencies``：值既可以是名字数组，也可以是子 schema。"""
    raw = schema["dependencies"]
    if not isinstance(raw, dict):
        problems.append(("dependencies", f'"dependencies" must be an object, got '
                                         f"{_describe(raw)}", "object", _describe(raw)))
        return None
    required_map: Dict[str, Tuple[str, ...]] = {}
    schema_map: List[Tuple[str, Any]] = []
    for key, value in raw.items():
        if isinstance(value, (dict, bool)):
            schema_map.append((key, value))
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            required_map[key] = tuple(dict.fromkeys(value))
        else:
            problems.append(("dependencies",
                             f'"dependencies.{key}" must be an array of property names or a '
                             f"schema, got {_describe(value)}", "array of strings or schema",
                             _describe(value)))
    if not required_map and not schema_map:
        return None
    return (required_map, tuple(schema_map))


# --------------------------------------------------------------------------- #
# 关键字：对象展开类（准备阶段）
# --------------------------------------------------------------------------- #
def _prep_properties(self: _Validator, schema: dict, problems: List) -> Any:
    raw = schema["properties"]
    if not isinstance(raw, dict):
        problems.append(("properties", f'"properties" must be an object, got {_describe(raw)}',
                         "object", _describe(raw)))
        return None
    clean: Dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(value, (dict, bool)):
            problems.append(("properties",
                             f'"properties.{key}" must be a schema, got {_describe(value)}',
                             "schema", _describe(value)))
            continue
        clean[key] = value
    return clean or None


def _prep_pattern_properties(self: _Validator, schema: dict, problems: List) -> Any:
    raw = schema["patternProperties"]
    if not isinstance(raw, dict):
        problems.append(("patternProperties",
                         f'"patternProperties" must be an object, got {_describe(raw)}',
                         "object", _describe(raw)))
        return None
    entries: List[Tuple[Optional[re.Pattern], str, Any]] = []
    for pattern, value in raw.items():
        if not isinstance(value, (dict, bool)):
            problems.append(("patternProperties",
                             f'"patternProperties.{pattern}" must be a schema, got '
                             f"{_describe(value)}", "schema", _describe(value)))
            continue
        try:
            entries.append((re.compile(pattern), pattern, value))
        except re.error as exc:
            self._warn(f'pattern_invalid: the "patternProperties" regular expression '
                       f"{pattern!r} cannot be compiled ({exc}); the entry was skipped")
    return tuple(entries) or None


def _prep_additional_properties(self: _Validator, schema: dict, problems: List) -> Any:
    raw = schema["additionalProperties"]
    if not isinstance(raw, (dict, bool)):
        problems.append(("additionalProperties",
                         f'"additionalProperties" must be a boolean or a schema, got '
                         f"{_describe(raw)}", "boolean or schema", _describe(raw)))
        return None
    if raw is True:
        return None  # 空 schema 接受一切，没必要逐个键压栈
    names = frozenset(schema["properties"]) if isinstance(schema.get("properties"), dict) \
        else frozenset()
    patterns: List[re.Pattern] = []
    raw_patterns = schema.get("patternProperties")
    if isinstance(raw_patterns, dict):
        for pattern in raw_patterns:
            try:
                patterns.append(re.compile(pattern))
            except re.error:
                continue
    return (raw, names, tuple(patterns))


def _prep_property_names(self: _Validator, schema: dict, problems: List) -> Any:
    raw = schema["propertyNames"]
    if not isinstance(raw, (dict, bool)):
        problems.append(("propertyNames",
                         f'"propertyNames" must be a schema, got {_describe(raw)}',
                         "schema", _describe(raw)))
        return None
    if raw is True:
        return None  # 空 schema 接受所有键名
    return raw


def _prep_dependent_schemas(self: _Validator, schema: dict, problems: List) -> Any:
    raw = schema["dependentSchemas"]
    if not isinstance(raw, dict):
        problems.append(("dependentSchemas",
                         f'"dependentSchemas" must be an object, got {_describe(raw)}',
                         "object", _describe(raw)))
        return None
    entries: List[Tuple[str, Any]] = []
    for key, value in raw.items():
        if not isinstance(value, (dict, bool)):
            problems.append(("dependentSchemas",
                             f'"dependentSchemas.{key}" must be a schema, got {_describe(value)}',
                             "schema", _describe(value)))
            continue
        entries.append((key, value))
    return tuple(entries) or None


def _prep_all_of(self: _Validator, schema: dict, problems: List) -> Any:
    return _prep_schema_array(self, schema, problems, "allOf")


def _prep_any_of(self: _Validator, schema: dict, problems: List) -> Any:
    return _prep_schema_array(self, schema, problems, "anyOf")


def _prep_one_of(self: _Validator, schema: dict, problems: List) -> Any:
    return _prep_schema_array(self, schema, problems, "oneOf")


def _prep_schema_array(self: _Validator, schema: dict, problems: List, keyword: str) -> Any:
    raw = schema[keyword]
    if not isinstance(raw, list) or not raw:
        problems.append((keyword, f'"{keyword}" must be a non-empty array of schemas, got '
                                  f"{_describe(raw)}", "non-empty array of schemas",
                         _describe(raw)))
        return None
    for index, item in enumerate(raw):
        if not isinstance(item, (dict, bool)):
            problems.append((keyword, f'"{keyword}[{index}]" must be a schema, got '
                                      f"{_describe(item)}", "schema", _describe(item)))
            return None
    return tuple(raw)


def _prep_not(self: _Validator, schema: dict, problems: List) -> Any:
    raw = schema["not"]
    if not isinstance(raw, (dict, bool)):
        problems.append(("not", f'"not" must be a schema, got {_describe(raw)}',
                         "schema", _describe(raw)))
        return None
    return raw


def _prep_if(self: _Validator, schema: dict, problems: List) -> Any:
    raw = schema["if"]
    if not isinstance(raw, (dict, bool)):
        problems.append(("if", f'"if" must be a schema, got {_describe(raw)}',
                         "schema", _describe(raw)))
        return None
    then = schema.get("then", _MISSING)
    otherwise = schema.get("else", _MISSING)
    for keyword, value in (("then", then), ("else", otherwise)):
        if value is not _MISSING and not isinstance(value, (dict, bool)):
            problems.append((keyword, f'"{keyword}" must be a schema, got {_describe(value)}',
                             "schema", _describe(value)))
    return (raw,
            None if then is _MISSING or not isinstance(then, (dict, bool)) else then,
            None if otherwise is _MISSING or not isinstance(otherwise, (dict, bool)) else otherwise)


# --------------------------------------------------------------------------- #
# 关键字：求值阶段（断言）
# --------------------------------------------------------------------------- #
def _h_type(self: _Validator, task: _Task, prepared: Any) -> bool:
    allowed, ordered, text, has_number, has_integer = prepared
    kind = task.kind
    if kind == TYPE_NUMBER:
        if has_number:
            return True
        if has_integer and _is_integral(task.instance):
            return True
        return self._fail(task, "type", f"expected {text}, got number",
                          expected=list(ordered) if len(ordered) > 1 else text,
                          actual=_scalar_text(task.instance))
    if kind in allowed:
        return True
    return self._fail(task, "type", f"expected {text}, got {kind}",
                      expected=list(ordered) if len(ordered) > 1 else text,
                      actual=_describe(task.instance))


def _h_const(self: _Validator, task: _Task, prepared: Any) -> bool:
    text, rendered = prepared
    if _canonical_text(task.instance) == text:
        return True
    return self._fail(task, "const", f"expected exactly {rendered}, got "
                                     f"{_describe(task.instance)}",
                      expected=rendered, actual=_describe(task.instance))


def _h_enum(self: _Validator, task: _Task, prepared: Any) -> bool:
    texts, rendered, count = prepared
    if _canonical_text(task.instance) in texts:
        return True
    return self._fail(task, "enum",
                      f"value is not one of the {count} allowed values "
                      f"({_describe(task.instance)})",
                      expected=rendered, actual=_describe(task.instance))


def _h_bound(self: _Validator, task: _Task, prepared: Any, keyword: str,
             exclusive: bool, upper: bool) -> bool:
    bound, name, rendered = prepared
    if task.kind != TYPE_NUMBER:
        return True
    value = task.instance
    try:
        if upper:
            ok = value < bound if exclusive else value <= bound
        else:
            ok = value > bound if exclusive else value >= bound
    except (TypeError, ValueError, InvalidOperation):  # pragma: no cover - 防御
        return True
    if ok:
        return True
    relation = ("less than" if upper else "greater than") + (" " if exclusive else " or equal to ")
    return self._fail(task, keyword,
                      f"{_scalar_text(value)} must be {relation.strip()} {rendered}",
                      expected=f"{keyword} {rendered}", actual=_scalar_text(value))


def _h_maximum(self: _Validator, task: _Task, prepared: Any) -> bool:
    return _h_bound(self, task, prepared, "maximum", False, True)


def _h_exclusive_maximum(self: _Validator, task: _Task, prepared: Any) -> bool:
    return _h_bound(self, task, prepared, "exclusiveMaximum", True, True)


def _h_minimum(self: _Validator, task: _Task, prepared: Any) -> bool:
    return _h_bound(self, task, prepared, "minimum", False, False)


def _h_exclusive_minimum(self: _Validator, task: _Task, prepared: Any) -> bool:
    return _h_bound(self, task, prepared, "exclusiveMinimum", True, False)


def _h_multiple_of(self: _Validator, task: _Task, prepared: Any) -> bool:
    divisor, rendered = prepared
    if task.kind != TYPE_NUMBER:
        return True
    value = _as_decimal(task.instance)
    if value is None:
        return True
    try:
        if abs(value.adjusted()) > 10_000:  # 天文数字：跳过而不是卡死
            self._warn(f'multiple_of_skipped: the value {_scalar_text(task.instance, 40)} has too '
                       f"many digits to test against \"multipleOf\": {rendered}")
            return True
        with localcontext() as ctx:
            ctx.prec = MAX_PREC
            remainder = value % divisor
        ok = remainder == 0
    except (InvalidOperation, ZeroDivisionError, Overflow, ValueError,
            ArithmeticError):  # pragma: no cover - 防御
        return True
    if ok:
        return True
    return self._fail(task, "multipleOf",
                      f"{_scalar_text(task.instance)} is not a multiple of {rendered}",
                      expected=f"a multiple of {rendered}", actual=_scalar_text(task.instance))


def _h_length(self: _Validator, task: _Task, prepared: Any, keyword: str, upper: bool) -> bool:
    limit, _name = prepared
    if task.kind != TYPE_STRING:
        return True
    length = len(task.instance)
    ok = length <= limit if upper else length >= limit
    if ok:
        return True
    return self._fail(task, keyword,
                      f"the string is {length} "
                      f"{'character' if length == 1 else 'characters'} long, which is "
                      f"{'longer than' if upper else 'shorter than'} the {keyword} of {limit}",
                      expected=f"{keyword} {limit}", actual=f"length {length}")


def _h_max_length(self: _Validator, task: _Task, prepared: Any) -> bool:
    return _h_length(self, task, prepared, "maxLength", True)


def _h_min_length(self: _Validator, task: _Task, prepared: Any) -> bool:
    return _h_length(self, task, prepared, "minLength", False)


def _h_pattern(self: _Validator, task: _Task, prepared: Any) -> bool:
    regex, source = prepared
    if task.kind != TYPE_STRING:
        return True
    try:
        if regex.search(task.instance) is not None:
            return True
    except (re.error, TypeError, ValueError):  # pragma: no cover - 防御
        return True
    return self._fail(task, "pattern",
                      f"{_scalar_text(task.instance)} does not match the pattern "
                      f"{source!r}",
                      expected=f"a string matching /{source}/", actual=_scalar_text(task.instance))


def _h_format(self: _Validator, task: _Task, prepared: Any) -> bool:
    name, check, description, asserted = prepared
    if task.kind != TYPE_STRING:
        return True
    where = "#/" + "/".join(str(part) for part in task.schema_path) if task.schema_path else "#"
    if check is None:
        if name in ANNOTATION_FORMATS:
            self._warn(f'format_annotation: format "{name}" at {where} is an annotation only '
                       f"(this engine does not assert it), so the value was accepted")
        else:
            self._warn(f'format_unknown: format "{name}" at {where} is not a known format; '
                       f"it was treated as an annotation and the value was accepted")
        return True
    try:
        ok = bool(check(task.instance))
    except Exception:  # pragma: no cover - 防御：格式检查绝不抛异常
        ok = True
    if ok:
        return True
    return self._fail(task, "format",
                      f"{_scalar_text(task.instance)} is not {description}",
                      expected=f'format "{name}": {description}',
                      actual=_scalar_text(task.instance))


def _h_content_encoding(self: _Validator, task: _Task, prepared: Any) -> bool:
    where = "#/" + "/".join(str(part) for part in task.schema_path) if task.schema_path else "#"
    self._warn(f'content_annotation: contentEncoding "{prepared}" at {where} is an annotation '
               f"only; the value was not decoded or verified")
    return True


def _h_content_media_type(self: _Validator, task: _Task, prepared: Any) -> bool:
    where = "#/" + "/".join(str(part) for part in task.schema_path) if task.schema_path else "#"
    self._warn(f'content_annotation: contentMediaType "{prepared}" at {where} is an annotation '
               f"only; the value was not parsed or verified")
    return True


def _h_count(self: _Validator, task: _Task, prepared: Any, keyword: str, upper: bool,
             unit: str) -> bool:
    limit, _name = prepared
    kind = TYPE_ARRAY if unit == "item" else TYPE_OBJECT
    if task.kind != kind:
        return True
    count = len(task.instance)
    ok = count <= limit if upper else count >= limit
    if ok:
        return True
    if unit == "item":
        subject = f"the array has {count} " + ("item" if count == 1 else "items")
    else:
        subject = f"the object has {count} " + ("property" if count == 1 else "properties")
    return self._fail(task, keyword,
                      f"{subject}, but \"{keyword}\" requires "
                      f"{'at most' if upper else 'at least'} {limit}",
                      expected=f"{keyword} {limit}", actual=count)


def _h_max_items(self: _Validator, task: _Task, prepared: Any) -> bool:
    return _h_count(self, task, prepared, "maxItems", True, "item")


def _h_min_items(self: _Validator, task: _Task, prepared: Any) -> bool:
    return _h_count(self, task, prepared, "minItems", False, "item")


def _h_max_properties(self: _Validator, task: _Task, prepared: Any) -> bool:
    return _h_count(self, task, prepared, "maxProperties", True, "property")


def _h_min_properties(self: _Validator, task: _Task, prepared: Any) -> bool:
    return _h_count(self, task, prepared, "minProperties", False, "property")


def _h_unique_items(self: _Validator, task: _Task, prepared: Any) -> bool:
    if not prepared or task.kind != TYPE_ARRAY:
        return True
    items = task.instance
    if len(items) < 2:
        return True
    seen: Dict[str, Tuple[int, Any]] = {}
    duplicate: Optional[Tuple[int, int]] = None
    for index, item in enumerate(items):
        try:
            text = _canonical_text(item)
        except Exception:  # pragma: no cover - 防御
            continue
        previous = seen.get(text)
        if previous is None:
            seen[text] = (index, item)
            continue
        # 哈希命中后再做一次结构化成对比较，避免规范文本碰撞造成误报
        if _values_equal(previous[1], item):
            duplicate = (previous[0], index)
            break
        self._warn("unique_items_hash_collision: two array items produced the same canonical "
                   "text but are not equal; they were treated as different values")
    if duplicate is None:
        return True
    first, second = duplicate
    return self._fail(task, "uniqueItems",
                      f'items {first} and {second} are duplicates '
                      f"({_describe(items[second], 40)}), but the array must only contain "
                      f"unique items",
                      expected="unique items", actual=f"duplicate at index {second}")


def _h_required(self: _Validator, task: _Task, prepared: Any) -> bool:
    if task.kind != TYPE_OBJECT:
        return True
    instance = task.instance
    state = self._state
    collecting = state is not None and state.collect
    ok = True
    present = sum(1 for key in prepared if key in instance)
    for key in prepared:
        if key in instance:
            continue
        # 错误路径指向"缺少属性的对象"本身，缺失的键名放在 expected 里
        self._fail(task, "required", f'missing required property "{key}"',
                   expected=key,
                   actual=f"{present} of {len(prepared)} required properties present")
        if not collecting:
            return False
        ok = False
    return ok


def _h_dependent_required(self: _Validator, task: _Task, prepared: Any) -> bool:
    if task.kind != TYPE_OBJECT:
        return True
    instance = task.instance
    state = self._state
    collecting = state is not None and state.collect
    ok = True
    for key, deps in prepared.items():
        if key not in instance:
            continue
        for dep in deps:
            if dep in instance:
                continue
            self._fail(task, "dependentRequired",
                       f'property "{key}" requires "{dep}" to be present, but it is missing',
                       expected=dep, actual=key)
            if not collecting:
                return False
            ok = False
    return ok


# --------------------------------------------------------------------------- #
# 关键字：求值阶段（展开）
# --------------------------------------------------------------------------- #
def _h_properties(self: _Validator, task: _Task, prepared: Any, state: _State,
                  stack: List[_Task]) -> bool:
    if task.kind != TYPE_OBJECT:
        return True
    instance = task.instance
    children: List[_Task] = []
    for key in instance:  # 实例文档顺序，保证错误顺序稳定
        if key in prepared:
            children.append(self._new_task(task, instance[key], prepared[key], key,
                                           task.schema_path + ("properties", key)))
    stack.extend(reversed(children))
    return True


def _h_pattern_properties(self: _Validator, task: _Task, prepared: Any, state: _State,
                          stack: List[_Task]) -> bool:
    if task.kind != TYPE_OBJECT:
        return True
    instance = task.instance
    children: List[_Task] = []
    for key in instance:
        for regex, source, sub in prepared:
            try:
                matched = regex.search(key) is not None
            except (re.error, TypeError):  # pragma: no cover - 防御
                matched = False
            if matched:
                children.append(self._new_task(
                    task, instance[key], sub, key,
                    task.schema_path + ("patternProperties", source)))
    stack.extend(reversed(children))
    return True


def _h_additional_properties(self: _Validator, task: _Task, prepared: Any, state: _State,
                             stack: List[_Task]) -> bool:
    if task.kind != TYPE_OBJECT:
        return True
    sub, names, patterns = prepared
    instance = task.instance
    children: List[_Task] = []
    rejected: List[str] = []
    for key in instance:
        if key in names:
            continue
        matched = False
        for regex in patterns:
            try:
                if regex.search(key) is not None:
                    matched = True
                    break
            except (re.error, TypeError):  # pragma: no cover - 防御
                continue
        if matched:
            continue
        if sub is False:
            rejected.append(key)
            continue
        children.append(self._new_task(task, instance[key], sub, key,
                                       task.schema_path + ("additionalProperties",)))
    stack.extend(reversed(children))
    if not rejected:
        return True
    listing = ", ".join(encode_json_string(key) for key in rejected[:10])
    if len(rejected) > 10:
        listing += f", … ({len(rejected)} in total)"
    return self._fail(task, "additionalProperties",
                      f"additional properties are not allowed ({listing} "
                      f"{'was' if len(rejected) == 1 else 'were'} unexpected)",
                      expected="only the properties declared in \"properties\" / "
                               "\"patternProperties\"",
                      actual=rejected if len(rejected) <= 10 else f"{len(rejected)} extra keys")


def _h_property_names(self: _Validator, task: _Task, prepared: Any, state: _State,
                      stack: List[_Task]) -> bool:
    if task.kind != TYPE_OBJECT:
        return True
    if prepared is False:
        keys = list(task.instance)
        if keys:
            listing = ", ".join(encode_json_string(key) for key in keys[:10])
            if len(keys) > 10:
                listing += f", … ({len(keys)} in total)"
            return self._fail(task, "propertyNames",
                              f'"propertyNames" is false, so the object must not have any '
                              f"properties, but it has {listing}",
                              expected="an object without properties",
                              actual=f"{len(keys)} properties")
        return True
    children = [self._new_task(task, key, prepared, key,
                               task.schema_path + ("propertyNames",)) for key in task.instance]
    stack.extend(reversed(children))
    return True


def _h_prefix_items(self: _Validator, task: _Task, prepared: Any, state: _State,
                    stack: List[_Task]) -> bool:
    if task.kind != TYPE_ARRAY:
        return True
    items = task.instance
    children = [self._new_task(task, items[index], sub, index,
                               task.schema_path + ("prefixItems", index))
                for index, sub in enumerate(prepared) if index < len(items)]
    stack.extend(reversed(children))
    return True


def _h_items_tuple(self: _Validator, task: _Task, prepared: Any, state: _State,
                   stack: List[_Task]) -> bool:
    """draft-07 的数组形式 ``items``（元组校验）。"""
    if task.kind != TYPE_ARRAY:
        return True
    items = task.instance
    children = [self._new_task(task, items[index], sub, index,
                               task.schema_path + ("items", index))
                for index, sub in enumerate(prepared) if index < len(items)]
    stack.extend(reversed(children))
    return True


def _h_items_rest(self: _Validator, task: _Task, prepared: Any, state: _State,
                  stack: List[_Task]) -> bool:
    """从 ``start`` 开始的元素都按同一个 schema 校验（``items`` / ``additionalItems``）。"""
    start, sub, keyword = prepared
    if task.kind != TYPE_ARRAY:
        return True
    items = task.instance
    if len(items) <= start:
        return True
    if sub is False:
        extra = list(range(start, len(items)))
        listing = ", ".join(str(index) for index in extra[:10])
        if len(extra) > 10:
            listing += f", … ({len(extra)} in total)"
        if start == 0:
            return self._fail(task, keyword,
                              f'"{keyword}" is false, so the array must be empty, but it has '
                              f"{len(items)} "
                              f"{'item' if len(items) == 1 else 'items'}",
                              expected="an empty array", actual=f"{len(items)} items")
        return self._fail(task, keyword,
                          f'"{keyword}" is false, so the array must not have more than {start} '
                          f"{'item' if start == 1 else 'items'}, but it has {len(items)} "
                          f"(extra indices: {listing})",
                          expected=f"at most {start} items", actual=f"{len(items)} items")
    children: List[_Task] = []
    for index in range(start, len(items)):
        children.append(self._new_task(task, items[index], sub, index,
                                       task.schema_path + (keyword,)))
    stack.extend(reversed(children))
    return True


def _h_contains(self: _Validator, task: _Task, prepared: Any, state: _State,
                stack: List[_Task]) -> bool:
    sub, minimum, maximum = prepared
    if task.kind != TYPE_ARRAY:
        return True
    if maximum is None and minimum <= 0:
        return True  # minContains: 0 且没有 maxContains，永远不会失败
    items = task.instance
    matches = 0
    for index, item in enumerate(items):
        if self._check(item, sub, task, "contains", index):
            matches += 1
            if maximum is None and matches >= minimum:
                break  # 只需要知道"够不够"，不必数完（有 maxContains 时才全数）
    if matches < minimum:
        return self._fail(task, "contains",
                          f"{matches} "
                          f"{'item matches' if matches == 1 else 'items match'} the \"contains\" "
                          f"schema, but at least {minimum} "
                          f"{'is' if minimum == 1 else 'are'} required",
                          expected=f"at least {minimum} matching items",
                          actual=f"{matches} matching items")
    if maximum is not None and matches > maximum:
        return self._fail(task, "maxContains",
                          f"{matches} items match the \"contains\" schema, but at most "
                          f"{maximum} {'is' if maximum == 1 else 'are'} allowed",
                          expected=f"at most {maximum} matching items",
                          actual=f"{matches} matching items")
    return True


def _h_dependent_schemas(self: _Validator, task: _Task, prepared: Any, state: _State,
                         stack: List[_Task]) -> bool:
    if task.kind != TYPE_OBJECT:
        return True
    instance = task.instance
    children = [self._sibling_task(task, sub, task.schema_path + ("dependentSchemas", key))
                for key, sub in prepared if key in instance]
    stack.extend(reversed(children))
    return True


def _h_dependencies(self: _Validator, task: _Task, prepared: Any, state: _State,
                    stack: List[_Task]) -> bool:
    required_map, schema_map = prepared
    if task.kind != TYPE_OBJECT:
        return True
    instance = task.instance
    ok = True
    for key, deps in required_map.items():
        if key not in instance:
            continue
        for dep in deps:
            if dep in instance:
                continue
            self._fail(task, "dependencies",
                       f'property "{key}" requires "{dep}" to be present, but it is missing',
                       expected=dep, actual=key)
            if not state.collect:
                return False
            ok = False
    children = [self._sibling_task(task, sub, task.schema_path + ("dependencies", key))
                for key, sub in schema_map if key in instance]
    stack.extend(reversed(children))
    return ok


def _h_all_of(self: _Validator, task: _Task, prepared: Any, state: _State,
              stack: List[_Task]) -> bool:
    children = [self._sibling_task(task, sub, task.schema_path + ("allOf", index))
                for index, sub in enumerate(prepared)]
    stack.extend(reversed(children))
    return True


def _summarize(errors: Sequence[SchemaError], limit: int = 160) -> str:
    if not errors:
        return "the subschema rejected the value"
    text = "; ".join(error.message for error in errors[:3])
    if len(errors) > 3:
        text += f"; … ({len(errors)} problems in total)"
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _h_any_of(self: _Validator, task: _Task, prepared: Any, state: _State,
              stack: List[_Task]) -> bool:
    reasons: List[str] = []
    for index, sub in enumerate(prepared):
        ok, errors = self._check_with_reasons(task.instance, sub, task, "anyOf", index)
        if ok:
            return True
        reasons.append(f"anyOf[{index}]: {_summarize(errors)}")
    return self._fail(task, "anyOf",
                      f"the value does not match any of the {len(prepared)} \"anyOf\" subschemas "
                      f"({_describe(task.instance)}). " + " | ".join(reasons),
                      expected=reasons, actual=_describe(task.instance))


def _h_one_of(self: _Validator, task: _Task, prepared: Any, state: _State,
              stack: List[_Task]) -> bool:
    reasons: List[str] = []
    matched: List[int] = []
    for index, sub in enumerate(prepared):
        ok, errors = self._check_with_reasons(task.instance, sub, task, "oneOf", index)
        if ok:
            matched.append(index)
            if len(matched) > 1:
                break  # 已经不可能"恰好一个"了，提前结束
        else:
            reasons.append(f"oneOf[{index}]: {_summarize(errors)}")
    if len(matched) == 1:
        return True
    if matched:
        return self._fail(task, "oneOf",
                          f"the value matches {len(matched)} of the {len(prepared)} \"oneOf\" "
                          f"subschemas (indices {', '.join(str(i) for i in matched)}), but it "
                          f"must match exactly one",
                          expected="exactly one matching subschema",
                          actual=f"{len(matched)} subschemas matched")
    return self._fail(task, "oneOf",
                      f"the value does not match any of the {len(prepared)} \"oneOf\" subschemas "
                      f"({_describe(task.instance)}). " + " | ".join(reasons),
                      expected=reasons, actual=_describe(task.instance))


def _h_not(self: _Validator, task: _Task, prepared: Any, state: _State,
           stack: List[_Task]) -> bool:
    if not self._check(task.instance, prepared, task, "not"):
        return True
    return self._fail(task, "not",
                      f"the value must NOT be valid against the \"not\" subschema, but it is "
                      f"({_describe(task.instance)})",
                      expected="a value rejected by the \"not\" subschema",
                      actual=_describe(task.instance))


def _h_if(self: _Validator, task: _Task, prepared: Any, state: _State,
          stack: List[_Task]) -> bool:
    condition, then, otherwise = prepared
    matched = self._check(task.instance, condition, task, "if")
    branch = then if matched else otherwise
    if branch is None:
        return True
    keyword = "then" if matched else "else"
    stack.append(self._sibling_task(task, branch, task.schema_path + (keyword,)))
    return True


# --------------------------------------------------------------------------- #
# 把准备/求值函数挂到 _Validator 上（模块级定义，避免超长类体）
# --------------------------------------------------------------------------- #
for _name, _func in list(globals().items()):
    if _name.startswith("_prep_") and callable(_func):
        setattr(_Validator, _name, _func)
    elif _name.startswith("_h_") and callable(_func):
        setattr(_Validator, _name, _func)
del _name, _func


# --------------------------------------------------------------------------- #
# 公共入口
# --------------------------------------------------------------------------- #
def validate(instance: Any, schema: Any, *, max_errors: int = DEFAULT_MAX_ERRORS,
             resolver: Optional[LocalRefResolver] = None) -> SchemaValidationResult:
    """用 ``schema`` 校验 ``instance``（解析后的 JSON 值）。

    任何输入都不会抛异常；schema 本身有问题时也会以 :class:`SchemaError`
    （关键字如 ``type`` / ``$ref`` / ``schema_invalid``）的形式返回，
    同时 ``valid=False``。
    """
    started = time.perf_counter()
    result = SchemaValidationResult(valid=True, errors=[], warnings=[])
    try:
        result.schema_draft = detect_draft(schema)
        try:
            cap = int(max_errors)
        except (TypeError, ValueError):
            cap = DEFAULT_MAX_ERRORS
        cap = max(1, min(cap, 100_000))

        if not isinstance(schema, (dict, bool)):
            result.errors = [SchemaError(
                path=(), path_string="$", keyword="schema_invalid",
                message=f"a JSON Schema must be an object or a boolean, got {json_type(schema)}; "
                        f"paste the schema itself, not its JSON text",
                expected="object or boolean", actual=_describe(schema))]
            result.valid = False
            result.elapsed_ms = (time.perf_counter() - started) * 1000.0
            return result

        if resolver is None:
            resolver = LocalRefResolver(base_schema=schema)
        else:
            resolver.set_root(schema)

        validator = _Validator(resolver, cap, result.schema_draft, max_steps=MAX_STEPS)
        ok, errors = validator._walk(instance, schema, (), (), (), collect=True, cap=cap,
                                     doc=resolver.root_document, main=True)
        result.errors = errors
        if validator.budget_exceeded:
            result.errors.append(SchemaError(
                path=(), path_string="$", keyword="budget_exceeded",
                message=f"validation stopped after {validator.steps} steps, which is the "
                        f"{MAX_STEPS}-step safety limit; the result is incomplete",
                expected="a smaller instance or a simpler schema",
                actual=f"{validator.steps} steps"))
        result.warnings = validator.finish_warnings()
        result.truncated = bool(validator.hit_error_cap or validator.budget_exceeded)
        result.error_count = len(result.errors)
        result.checked_nodes = validator.steps
        result.valid = ok and not result.errors and not result.truncated
    except RecursionError:  # pragma: no cover - 迭代实现下不应发生
        result.errors.append(SchemaError(
            path=(), path_string="$", keyword="internal_error",
            message="validation hit Python's recursion limit; the schema or the instance nests "
                    "too deeply for a recursive pass"))
        result.valid = False
        result.truncated = True
    except MemoryError:  # pragma: no cover - 防御
        result.errors.append(SchemaError(
            path=(), path_string="$", keyword="out_of_memory",
            message="the document is too large to be validated in memory"))
        result.valid = False
        result.truncated = True
    except Exception as exc:  # pragma: no cover - 公共 API 绝不抛异常
        result.errors.append(SchemaError(
            path=(), path_string="$", keyword="internal_error",
            message=f"validation failed with an unexpected {type(exc).__name__}: {exc}",
            actual=type(exc).__name__))
        result.valid = False
    result.elapsed_ms = (time.perf_counter() - started) * 1000.0
    return result


# --------------------------------------------------------------------------- #
# schema 推断
# --------------------------------------------------------------------------- #
class _Sketch:
    """从样本里累积出来的"形状速写"，最后再一次性转成 schema。"""

    __slots__ = ("types", "count", "num_min", "num_max", "int_only", "str_min", "str_max",
                 "keys", "key_counts", "objects", "element")

    def __init__(self) -> None:
        self.types: set = set()
        self.count = 0
        self.num_min: Any = None
        self.num_max: Any = None
        self.int_only = True
        self.str_min: Optional[int] = None
        self.str_max: Optional[int] = None
        self.keys: Dict[str, "_Sketch"] = {}
        self.key_counts: Dict[str, int] = {}
        self.objects = 0
        self.element: Optional["_Sketch"] = None

    def observe_scalar(self, value: Any, kind: str) -> None:
        self.count += 1
        self.types.add(kind)
        if kind == TYPE_NUMBER:
            self._observe_number(value)
        elif kind == TYPE_STRING:
            self._observe_length(len(value))

    def _observe_number(self, value: Any) -> None:
        if self.num_min is None:
            self.num_min = value
            self.num_max = value
        else:
            try:
                if value < self.num_min:
                    self.num_min = value
                if value > self.num_max:
                    self.num_max = value
            except (TypeError, ValueError, InvalidOperation):  # pragma: no cover - 防御
                pass
        if not (isinstance(value, int) and not isinstance(value, bool)):
            self.int_only = False

    def _observe_length(self, length: int) -> None:
        if self.str_min is None or length < self.str_min:
            self.str_min = length
        if self.str_max is None or length > self.str_max:
            self.str_max = length


def _sketch_object(sketch: _Sketch, value: dict) -> None:
    """对象：登记键集合（用于 ``required``）与每个键的子速写。"""
    sketch.count += 1
    sketch.types.add(TYPE_OBJECT)
    sketch.objects += 1
    for key, item in value.items():
        sub = sketch.keys.get(key)
        if sub is None:
            sub = sketch.keys[key] = _Sketch()
            sketch.key_counts[key] = 0
        sketch.key_counts[key] += 1
        kind = json_type(item)
        if kind in (TYPE_OBJECT, TYPE_ARRAY):
            continue  # 深层结构交给主循环（受 max_depth 限制）
        sub.observe_scalar(item, kind)


def _observe_shallow(sketch: _Sketch, value: Any) -> None:
    """只吸收一层信息，不再往下递归（用于超出 ``max_depth`` / ``sample_arrays`` 的部分）。"""
    kind = json_type(value)
    if kind == TYPE_OBJECT:
        _sketch_object(sketch, value)  # _sketch_object 本身就只看一层
        return
    if kind == TYPE_ARRAY:
        sketch.count += 1
        sketch.types.add(TYPE_ARRAY)
        return
    sketch.observe_scalar(value, kind)


def infer_schema(instance: Any, *, max_depth: int = 8, sample_arrays: int = 20) -> dict:
    """从样本文档推断一个 draft-07 schema（迭代式，``JsonNumber`` 安全）。

    * ``max_depth``：超过这个深度的子树退化成 ``{}``（接受任意值）。
    * ``sample_arrays``：数组只对前 N 个元素做**深层**采样；其余元素仍然会
      贡献浅层信息（标量类型、数值范围、对象键集合），因此 ``required``
      与 ``minimum`` / ``maximum`` 覆盖整个数组，而嵌套结构的约束只来自样本。
    * 数字：所有观察值都是 Python ``int`` 时输出 ``"type": "integer"``，
      否则输出 ``"number"`` 并带上观察到的 ``minimum`` / ``maximum``。
    """
    try:
        depth_limit = int(max_depth)
    except (TypeError, ValueError):
        depth_limit = 8
    depth_limit = max(0, min(depth_limit, 64))
    try:
        sample = int(sample_arrays)
    except (TypeError, ValueError):
        sample = 20
    sample = max(1, min(sample, 100_000))

    root = _Sketch()
    stack: List[Tuple[_Sketch, Any, int]] = [(root, instance, 0)]
    seen = 0
    while stack:
        sketch, value, depth = stack.pop()
        seen += 1
        if seen > 2_000_000:  # pragma: no cover - 防御：超大文档
            break
        kind = json_type(value)
        if kind == TYPE_OBJECT:
            _sketch_object(sketch, value)  # 键集合与标量子值已在这里吸收
            if depth >= depth_limit:
                continue
            for key, item in value.items():
                if json_type(item) in (TYPE_OBJECT, TYPE_ARRAY):
                    stack.append((sketch.keys[key], item, depth + 1))
            continue
        if kind == TYPE_ARRAY:
            sketch.count += 1
            sketch.types.add(TYPE_ARRAY)
            if sketch.element is None:
                sketch.element = _Sketch()
            element = sketch.element
            deep = depth < depth_limit
            for index, item in enumerate(value):
                if deep and index < sample:
                    stack.append((element, item, depth + 1))
                else:
                    # 不深挖，但仍然吸收浅层信息，避免 min/max 与 required 失真
                    _observe_shallow(element, item)
            continue
        sketch.observe_scalar(value, kind)

    schema = _sketch_to_schema(root, depth_limit)
    schema_order = {"$schema": "https://json-schema.org/draft-07/schema#"}
    schema_order.update(schema)
    return schema_order


def _ordered_types(sketch: _Sketch) -> List[str]:
    """速写里出现过的类型 -> 规范顺序的类型名列表（number 可细化成 integer）。"""
    if not sketch.types:
        return []
    names: List[str] = []
    for name in TYPE_ORDER:
        if name == "integer":
            continue
        if name in sketch.types:
            names.append(name)
    if names == [TYPE_NUMBER] and sketch.int_only and sketch.count:
        return ["integer"]
    return names


def _sketch_to_schema(sketch: _Sketch, depth_limit: int) -> dict:
    """速写 -> schema（迭代式；深度已被 ``max_depth`` 限制住）。"""
    root: dict = {}
    stack: List[Tuple[_Sketch, dict, int]] = [(sketch, root, 0)]
    while stack:
        current, out, depth = stack.pop()
        if not current.count:
            continue
        names = _ordered_types(current)
        if not names:
            continue
        out["type"] = names[0] if len(names) == 1 else names
        if TYPE_NUMBER in current.types or "integer" in names:
            if current.num_min is not None:
                out["minimum"] = current.num_min
            if current.num_max is not None:
                out["maximum"] = current.num_max
        if TYPE_STRING in current.types and current.str_min is not None:
            out["minLength"] = current.str_min
            if current.str_max is not None:
                out["maxLength"] = current.str_max
        if TYPE_OBJECT in current.types and current.keys:
            required = [key for key in current.keys
                        if current.key_counts.get(key, 0) >= current.objects]
            if required:
                out["required"] = required
            properties: dict = {}
            if depth < depth_limit:
                out["properties"] = properties
                for key, sub in current.keys.items():
                    child: dict = {}
                    properties[key] = child
                    stack.append((sub, child, depth + 1))
        if TYPE_ARRAY in current.types and current.element is not None and current.element.count:
            items: dict = {}
            out["items"] = items
            stack.append((current.element, items, depth + 1))
    return root


# --------------------------------------------------------------------------- #
# schema 文本
# --------------------------------------------------------------------------- #
def schema_to_text(schema: Any) -> str:
    """把 schema 美化成 JSON 文本（走 :func:`app.core.jsonformatter.dumps`）。"""
    options = FormatOptions(indent=INDENT_2, sort_keys=False, ensure_ascii=False)
    try:
        if isinstance(schema, bool):
            return "true" if schema else "false"
        if isinstance(schema, (dict, list, str, int, float, Decimal)) or schema is None:
            return dumps(schema, options)
    except (TypeError, ValueError, RecursionError):  # pragma: no cover - 防御
        pass
    try:
        return str(schema)
    except Exception:  # pragma: no cover - 防御
        return "<unprintable schema>"

"""数据转换引擎：JSON <-> CSV / XML / YAML，以及拆分、合并、去重、编码。

设计约束（与 ``app.core`` 一致）：

* **绝不修改输入**：所有函数只读入参，需要改动时先做迭代式深拷贝。
* **绝不递归遍历用户数据**：容器一律用显式栈展开，1000+ 层嵌套不会触发
  ``RecursionError``；确实无法迭代实现的第三方库（PyYAML）先做深度预检，
  超限返回明确错误而不是崩溃。
* **公共 API 不抛异常**：任何输入都返回 :class:`ConvertResult`，
  失败原因写在 ``error``（英文、机器可读，形如 ``code: detail``）。
* **数字字面量优先保真**：``1.230`` 不会变成 ``1.23``，``1e5`` 不会变成
  ``100000.0``，30 位整数不会变成科学计数法。凡是格式本身无法保真的地方
  （例如通用 YAML 读取器会把高精度小数读成 double）都会给出 ``warnings``。

---------------------------------------------------------------------------
CSV 约定
---------------------------------------------------------------------------
``json_to_csv``：

* 顶层数组 -> 每个元素一行；元素不是对象时统一落到名为 ``value`` 的列。
* 顶层对象 -> 一行。顶层标量 -> 一行一列（列名 ``value``）。
  顶层 ``null`` -> 报错 ``csv_null_root``（CSV 无法表达）。
* ``flatten=True``：嵌套对象展平成 ``a.b.c`` 列（分隔符 ``flatten_separator``）。
  空对象 ``{}`` / 空数组 ``[]`` 写成紧凑 JSON 文本 ``{}`` / ``[]``，不丢键。
* ``flatten=False``：任何嵌套容器整体写成紧凑 JSON 文本。
* ``array_handling``：
  ``"join"`` 数组元素用 ``join_separator`` 连接成一个单元格；
  ``"cell"`` 整个数组写成紧凑 JSON 文本；
  ``"expand"`` 数组展开成多行——数组元素是对象时其字段以 ``路径.子键``
  展开（同一数组内多个元素按“锁步”展开，不做笛卡尔积），
  一条记录里出现多个数组字段时按笛卡尔积展开，
  单条记录展开超过 ``MAX_EXPAND_ROWS`` 行时退回 ``"cell"`` 并告警。
* 列顺序 = 首次出现顺序；``columns`` 非空时严格按它选择与排序，
  未出现在数据中的列写空值，被丢弃的数据列计入 ``meta["dropped"]``。
* 展平后路径冲突（数据里本来就有 ``"a.b"`` 键，同时又有 ``{"a":{"b":...}}``）：
  **先到者胜**，后者被丢弃并告警 ``csv_flatten_collision``。
* ``None`` 与缺失值写成 ``null_text``；数字用 :func:`number_literal` 原样输出，
  布尔输出 ``true`` / ``false``，所以 CSV 是无损的（不会有精度告警）。
* 引用规则遵循 RFC 4180（含分隔符 / 引号 / 换行的值加引号，内部引号翻倍），
  ``quote_all=True`` 时所有单元格都加引号。
* ``meta``：``rows``（数据行数）、``columns``、``records``、``skipped``
  （顶层数组里的 ``null`` 元素个数）、``dropped``、``encoding``、``delimiter``。

``csv_to_json``：

* 空输入 -> 错误 ``csv_empty_input``；只有空行同样报错。
* ``has_header=True`` 首行是键；重复表头自动改名 ``x``、``x_2``、``x_3`` 并告警；
  空表头名退化成 ``column<N>``。``has_header=False`` 时键为 ``column1..columnN``。
* 行比表头短 -> 缺失单元格按空串处理（通常推断成 ``null``）；
  行比表头长 -> 多出的单元格落到 ``column<N>``，并告警 ``csv_ragged_row``。
* ``infer_types=True``：``true``/``false``（大小写不敏感）-> 布尔；
  规范 JSON 整数字面量 -> ``int``（任意精度）；``-0`` -> ``JsonNumber("-0")``；
  规范 JSON 小数/指数字面量 -> ``JsonNumber``，**原文保留**；
  其它一律保留字符串（``007``、``1.5.6``、``1,000`` 不会被误判成数字）。
  ``null_values`` 中的文本（默认 ``""``/``null``/``NULL``/``None``）-> ``None``，
  该判断先于类型推断。
* ``root="array"`` -> 对象数组；``root="object"`` -> 以**第一列**为键的对象，
  其余列组成值对象（只有一列时值就是该列的推断值）；键重复时后者覆盖并计数。
* 输出是 2 空格缩进的 JSON 文本。

---------------------------------------------------------------------------
XML 约定
---------------------------------------------------------------------------
``json_to_xml``：

* 根元素名 = ``root_name``。对象键 -> 子元素；``@`` 前缀（``attr_prefix``）的键
  -> 父元素属性（前缀被剥掉）；``#text``（``text_key``）-> 元素文本。
  只含 ``#text`` 的对象会塌缩成纯文本元素。
* 数组：**重复父键的同名兄弟元素**（``{"a":[1,2]}`` -> ``<a>1</a><a>2</a>``）。
  长度为 0 或 1 的数组无法用“重复”表达，因此写成
  ``<a json:type="array"><item>…</item></a>``（空数组为 ``<a json:type="array"/>``）；
  数组套数组同样走 ``json:type="array"`` + ``<item>`` 包装。
* ``null`` -> ``<a xsi:nil="true"/>``，根元素按需声明 ``xmlns:xsi``。
* 元数据命名空间 ``xmlns:json="https://jsonworkbench.dev/ns/json"`` 按需声明，
  只用于三个属性：``json:type``（``array`` / ``object`` / ``string``）、
  ``json:name``（元素名被清洗时保存原始键名）。
  ``json:type="string"`` 只加在“看起来像数字/布尔/null 的字符串”以及
  空串、纯空白串上，保证 ``infer_types=True`` 往返后仍是字符串。
* 元素名清洗：非 XML Name 起始字符（数字、``-``、``.`` 等）与非法字符
  （空格、``:``、``/`` 等）替换成 ``_``；以 ``xml`` 开头的名字前缀 ``_``；
  空名字用 ``element``；清洗过的原名保存在 ``json:name``。
  同层不同键清洗后撞名时追加 ``_2``、``_3`` 并告警 ``xml_name_conflict``。
  属性名同样清洗，但**不保存原名**（XML 无处存放），属于已知损失。
* 属性值必须是标量；对象/数组属性写成紧凑 JSON 文本并告警
  ``xml_attr_not_scalar``；``null`` 属性写空串并告警 ``xml_attr_null``。
* 文本里 XML 1.0 非法的控制字符与孤立代理项替换成 U+FFFD 并告警
  ``xml_illegal_char``；``\r`` 转义成 ``&#13;``，避免 XML 行尾归一化吃掉它。
* ``pretty``/``indent``/``declaration`` 控制排版；输出始终以换行结尾。

``xml_to_json``：

* **拒绝 DOCTYPE**：输入含 ``<!DOCTYPE`` 直接报 ``xml_doctype_not_allowed``，
  因此实体扩展（billion laughs）与外部实体都无法触发；解析器本身也不解析
  外部实体。解析失败报 ``xml_parse_error``（带行列信息），绝不抛出。
* 命名空间 URI 被丢弃，只保留本地名（``{uri}tag`` -> ``tag``），并告警一次。
* 属性 -> ``attr_prefix`` + 本地名；同名兄弟元素 -> 数组（文档顺序）；
  单个元素 -> 标量/对象；``json:type="array"`` -> 其子元素组成的数组；
  ``xsi:nil="true"`` -> ``null``；``json:name`` 还原原始键名。
* 叶子元素文本按 CSV 同样的规则做类型推断（``infer_types``）；
  有子元素或属性时，非纯空白文本放进 ``text_key``，**前后空白被去掉**
  （XML 解析器会把排版缩进并进 ``text``，无法与真实空白区分）；
  纯叶子文本原样保留（只有无标记的纯空白文本被折叠成 ``""``）。
  子元素之后的 tail 文本会被丢弃并告警。
* ``root_as_array=True`` 时结果就是根元素子元素值组成的数组（文档顺序），
  根上的属性与文本被丢弃并告警；``False`` 时根元素按普通对象处理
  （重复子元素仍然塌缩成数组）。根元素为空（无子/属性/文本）时返回 ``{}``
  （``root_as_array=True`` 时返回 ``[]``）。
* 已知损失：``{"a":{"@id":1,"#text":""}}`` 的空文本无法与 ``<a id="1"/>``
  区分，往返后 ``#text`` 消失。

---------------------------------------------------------------------------
YAML 约定
---------------------------------------------------------------------------
``json_to_yaml``：

* 使用 :mod:`yaml` 的 ``SafeDumper`` 子类，**只输出标准 YAML 标签**，
  绝不输出 ``!!python/...``。
* ``JsonNumber`` 用自定义 representer 以**原文**输出：普通小数/整数直接写
  原文（``1.230``、``123456789012345678901234567890``）；YAML 1.1 解析器
  认不出的指数形式（``1e5``）加显式 ``!!float`` 标签，原文同样保留，
  并告警 ``yaml_literal_tagged``。
* 通用 YAML 读取器（``yaml.safe_load``）会把高精度小数读成 double、把 ``-0``
  读成 ``0``，这类情况告警 ``yaml_number_precision``；本模块的
  :func:`yaml_to_json` 用自定义构造器读回**原始字面量**，因此
  JSON -> YAML -> JSON 可以严格往返。
* PyYAML 的发射器是递归实现，嵌套超过 ``YAML_MAX_DEPTH`` 层直接返回
  ``yaml_depth_exceeded`` 错误（而不是 ``RecursionError``）。

``yaml_to_json``：

* 只用 SafeLoader 子类；``!!python/...`` 标签、多文档流、制表符缩进等
  都返回明确错误（``yaml_unsafe_tag`` / ``yaml_error``）。空输入 -> ``null``
  并告警 ``yaml_empty_document``。
* 日期/时间戳 -> ISO 字符串（告警）；``!!binary`` -> base64 字符串（告警）；
  ``!!set`` -> 数组（告警）；非字符串键 -> 字符串（告警）；
  ``.inf`` / ``.nan`` -> ``null``（告警 ``yaml_non_finite``）。
* 树规范化是迭代式的，深文档不会递归爆栈（解析阶段仍受 PyYAML 限制）。

---------------------------------------------------------------------------
限制（UI 需要提示的点）
---------------------------------------------------------------------------
* 嵌套深度：CSV / XML / 合并 / 去重 / 拆分统一上限 ``MAX_DEPTH``（1000 层），
  超过返回 ``depth_exceeded``；YAML 导出上限 ``YAML_MAX_DEPTH``（250 层，
  PyYAML 发射器是递归实现），YAML 导入受 PyYAML 解析器限制（约 490 层），
  超过分别返回 ``yaml_depth_exceeded`` / ``yaml_too_deep``。
* CSV 是平面格式：``flatten=False`` 或 ``array_handling="cell"/"join"`` 时
  容器被写成 JSON **文本**，读回来是字符串而不是结构（不做二次解析，
  否则无法区分“长得像 JSON 的字符串”和真的 JSON）。
* ``array_handling="expand"`` 时一条记录里的多个数组按笛卡尔积展开，
  单条记录超过 ``MAX_EXPAND_ROWS`` 行会自动退回 ``"cell"`` 并告警。
* XML 无法表达“单个元素的数组”与“标量”的区别，也无法表达空对象与空串
  的区别，因此本模块会写出 ``json:type`` / ``json:name`` / ``xsi:nil`` 元数据
  属性（命名空间 ``https://jsonworkbench.dev/ns/json`` 与标准 xsi）。
  第三方 XML 工具看不到这些语义；``infer_types=False`` 时数字/布尔会读成字符串。
* XML 属性名被清洗后**无法还原**（元素名可以，靠 ``json:name``）。
* YAML 的字面量只有本模块的 :func:`yaml_to_json` 能完整读回；
  用 ``yaml.safe_load`` 之类通用读取器会得到 double，故有精度告警。
* ``yaml_to_json`` 的不安全标签检测是文本预检 + SafeLoader 双保险，
  因此正文里出现 ``!!python/`` 字样的普通字符串也会被拒绝（保守策略）。
* 结果约定：文本类转换写 ``output``；:func:`split_array` 的分片在
  ``meta["pieces"]``（``output`` 是同样内容的 JSON Lines）；
  :func:`dedupe_array` 的结果在 ``meta["value"]``；
  :func:`split_to_files` 的文件清单在 ``meta["files"]``；
  :func:`encoding_to_bytes` 的字节在 ``meta["data"]``。
* 告警按 code 折叠，单个结果最多 ``MAX_WARNINGS`` 条，多出的只计数，
  最后一条固定是 ``warnings_suppressed: N more warnings were omitted``。
"""

from __future__ import annotations

import base64
import codecs
import csv
import datetime
import functools
import io
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

from ..core.jsonformatter import INDENT_2, FormatOptions, dumps, json_equal, minify
from ..core.jsonvalue import (
    TYPE_ARRAY,
    TYPE_OBJECT,
    JsonNumber,
    decode_json_string,
    encode_json_string,
    json_type,
    number_literal,
    to_plain,
)

#: 所有转换器统一支持的最大嵌套层数（超过返回明确错误）
MAX_DEPTH = 1000
#: PyYAML 发射器是递归实现，实测约 330 层就会 RecursionError，这里留出余量
YAML_MAX_DEPTH = 250
#: 单个结果里最多保留的告警条数，超出部分只计数
MAX_WARNINGS = 60
#: ``array_handling="expand"`` 时单条记录最多展开的行数
MAX_EXPAND_ROWS = 1000

XML_NS_JSON = "https://jsonworkbench.dev/ns/json"
XML_NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"
_XSI_NIL_ATTR = "{%s}nil" % XML_NS_XSI
_JSON_TYPE_ATTR = "{%s}type" % XML_NS_JSON
_JSON_NAME_ATTR = "{%s}name" % XML_NS_JSON

MARK_OBJECT = "object"
MARK_ARRAY = "array"
MARK_STRING = "string"

#: ``encoding_to_bytes`` / ``CsvOptions.encoding`` 支持的编码（规范化后的名字）
SUPPORTED_ENCODINGS = (
    "utf-8",
    "utf-8-sig",
    "utf-16",
    "utf-16-le",
    "utf-16-be",
    "gbk",
    "gb18030",
    "big5",
    "ascii",
    "iso8859-1",
)
_BOM_ENCODINGS = {"utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"}

ARRAY_EXPAND = "expand"
ARRAY_JOIN = "join"
ARRAY_CELL = "cell"
ARRAY_MODES = (ARRAY_EXPAND, ARRAY_JOIN, ARRAY_CELL)

MERGE_DEEP = "deep"
MERGE_SHALLOW = "shallow"
MERGE_CONCAT = "concat"
MERGE_STRATEGIES = (MERGE_DEEP, MERGE_SHALLOW, MERGE_CONCAT)

ARRAY_APPEND = "append"
ARRAY_PREPEND = "prepend"
ARRAY_REPLACE = "replace"
ARRAY_STRATEGIES = (ARRAY_APPEND, ARRAY_PREPEND, ARRAY_REPLACE)

CONFLICT_LAST = "last"
CONFLICT_FIRST = "first"
CONFLICT_ERROR = "error"
CONFLICT_MODES = (CONFLICT_LAST, CONFLICT_FIRST, CONFLICT_ERROR)

KEEP_FIRST = "first"
KEEP_LAST = "last"

#: 规范 JSON 整数 / 数字字面量（用于 CSV、XML 的类型推断，避免 "007" 被吃掉）
_JSON_INT_RE = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")
_JSON_NUM_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?\Z")
_DOCTYPE_RE = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_XML_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufdd0-\ufdef\ufffe\uffff]")
_UNSAFE_YAML_RE = re.compile(r"(?:!!\s*(?:python|ruby|perl|php)|tag:\s*yaml\.org,\s*2002:\s*python)")
_FILENAME_BAD_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

__all__ = [
    "ARRAY_CELL",
    "ARRAY_EXPAND",
    "ARRAY_JOIN",
    "ARRAY_MODES",
    "ConvertResult",
    "CsvOptions",
    "MAX_DEPTH",
    "MAX_EXPAND_ROWS",
    "SUPPORTED_ENCODINGS",
    "XmlOptions",
    "YAML_MAX_DEPTH",
    "YamlOptions",
    "csv_to_json",
    "dedupe_array",
    "encoding_to_bytes",
    "escape_json_string",
    "json_to_csv",
    "json_to_xml",
    "json_to_yaml",
    "merge_documents",
    "split_array",
    "split_to_files",
    "unescape_json_string",
    "warning_codes",
    "xml_to_json",
    "yaml_to_json",
]


# --------------------------------------------------------------------------- #
# 结果对象
# --------------------------------------------------------------------------- #
@dataclass
class ConvertResult:
    """统一的转换结果。``error`` 形如 ``code: detail``，便于 UI 本地化。"""

    ok: bool
    output: str = ""
    error: str = ""
    warnings: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CsvOptions:
    """JSON -> CSV 选项。"""

    delimiter: str = ","
    flatten: bool = True
    flatten_separator: str = "."
    array_handling: str = ARRAY_EXPAND
    join_separator: str = "; "
    columns: Tuple[str, ...] = ()
    include_header: bool = True
    encoding: str = "utf-8"
    quote_all: bool = False
    null_text: str = ""
    line_ending: str = "\r\n"


@dataclass
class XmlOptions:
    """JSON -> XML 选项。"""

    root_name: str = "root"
    item_name: str = "item"
    attr_prefix: str = "@"
    text_key: str = "#text"
    pretty: bool = True
    indent: str = "  "
    declaration: bool = True


@dataclass
class YamlOptions:
    """JSON -> YAML 选项。"""

    indent: int = 2
    sort_keys: bool = False
    allow_unicode: bool = True
    width: int = 4096


def warning_codes(result: ConvertResult) -> List[str]:
    """取出 ``result.warnings`` 里的机器可读 code 列表（``code: detail`` 的 code 部分）。"""
    codes: List[str] = []
    for item in result.warnings or ():
        code, _, _ = str(item).partition(":")
        codes.append(code.strip())
    return codes


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
class _ConvertError(Exception):
    """内部错误信号；``message`` 已是 ``code: detail`` 形式。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


class _TooDeepError(_ConvertError):
    def __init__(self, what: str, limit: int) -> None:
        super().__init__("depth_exceeded", f"{what} nests deeper than {limit} levels")


class _WarnBag:
    """告警收集器：同类告警限量，避免 2 万行数据刷屏。"""

    __slots__ = ("items", "counts", "suppressed", "limit")

    def __init__(self, limit: int = MAX_WARNINGS) -> None:
        self.items: List[str] = []
        self.counts: Dict[str, int] = {}
        self.suppressed = 0
        self.limit = limit

    def add(self, code: str, detail: str = "", *, once: bool = False) -> None:
        self.counts[code] = self.counts.get(code, 0) + 1
        if once and self.counts[code] > 1:
            return
        if len(self.items) >= self.limit:
            self.suppressed += 1
            return
        self.items.append(f"{code}: {detail}" if detail else code)

    def has(self, code: str) -> bool:
        return self.counts.get(code, 0) > 0

    def as_list(self) -> List[str]:
        items = list(self.items)
        if self.suppressed:
            items.append(f"warnings_suppressed: {self.suppressed} more warnings were omitted")
        return items


def _safe(func):
    """公共 API 保护壳：任何异常都转成 ``ConvertResult(ok=False, ...)``。"""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> ConvertResult:
        try:
            return func(*args, **kwargs)
        except _ConvertError as exc:
            return ConvertResult(ok=False, error=str(exc))
        except RecursionError:  # pragma: no cover - 迭代实现下不应发生
            return ConvertResult(ok=False, error="depth_exceeded: the document is too deeply nested")
        except MemoryError:  # pragma: no cover
            return ConvertResult(ok=False, error="out_of_memory: the document is too large to convert")
        except Exception as exc:  # noqa: BLE001 - 公共边界必须兜底
            return ConvertResult(ok=False, error=f"internal_error: {type(exc).__name__}: {exc}")

    return wrapper


def _pretty_json(value: Any) -> str:
    """2 空格缩进、``\\n`` 行尾、数字字面量原样输出。"""
    return dumps(value, FormatOptions(indent=INDENT_2, line_ending="\n"))


def _measure_depth(value: Any) -> int:
    """迭代计算嵌套深度（标量为 0，每层容器 +1）。"""
    best = 0
    stack: List[Tuple[Any, int]] = [(value, 0)]
    seen_containers = 0
    while stack:
        node, depth = stack.pop()
        kind = json_type(node)
        if kind not in (TYPE_OBJECT, TYPE_ARRAY):
            continue
        seen_containers += 1
        if seen_containers > 5_000_000:  # pragma: no cover - 防御
            break
        if depth > best:
            best = depth
        if depth >= MAX_DEPTH + 1:
            return depth
        children: Iterable[Any] = node.values() if kind == TYPE_OBJECT else node
        for child in children:
            stack.append((child, depth + 1))
    return best


def _check_depth(value: Any, what: str, limit: int = MAX_DEPTH) -> None:
    depth = _measure_depth(value)
    if depth > limit:
        raise _TooDeepError(what, limit)


def _copy_deep(value: Any) -> Any:
    """迭代式深拷贝；标量（含 ``JsonNumber``、``str``）不可变，直接共享。"""
    if not isinstance(value, (dict, list)):
        return value
    root: Any = {} if isinstance(value, dict) else []
    stack: List[Tuple[Any, Any]] = [(value, root)]
    guard = 0
    while stack:
        guard += 1
        if guard > 5_000_000:  # pragma: no cover - 防御（自引用或超大文档）
            raise _ConvertError("value_too_large",
                                "the document is too large (or self-referencing) to copy")
        src, dst = stack.pop()
        if isinstance(src, dict):
            for key, item in src.items():
                if isinstance(item, dict):
                    child: Dict[str, Any] = {}
                    dst[key] = child
                    stack.append((item, child))
                elif isinstance(item, list):
                    child_list: List[Any] = []
                    dst[key] = child_list
                    stack.append((item, child_list))
                else:
                    dst[key] = item
        else:
            for item in src:
                if isinstance(item, dict):
                    child = {}
                    dst.append(child)
                    stack.append((item, child))
                elif isinstance(item, list):
                    child_list = []
                    dst.append(child_list)
                    stack.append((item, child_list))
                else:
                    dst.append(item)
    return root


def _scalar_text(value: Any, warn: _WarnBag, path: str, fallback: str = "") -> str:
    """标量 -> 文本；数字走 :func:`number_literal` 以保留字面量。"""
    kind = json_type(value)
    if value is None:
        return fallback
    if kind == "string":
        return value
    if kind == "boolean":
        return "true" if value else "false"
    if kind == "number":
        try:
            return number_literal(value)
        except (ValueError, TypeError, InvalidOperation, ArithmeticError):
            warn.add("number_not_finite", f"{path or '$'} is NaN/Infinity; written as {fallback!r}")
            return fallback
    return minify(value)


def _infer_text(text: str, infer_types: bool, null_values: Sequence[str] = ()) -> Any:
    """CSV / XML 共用的标量推断：只认规范 JSON 字面量，避免 ``007`` 被改成 ``7``。"""
    if text is None:
        return None
    if null_values and text in null_values:
        return None
    if not infer_types:
        return text
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if _JSON_INT_RE.match(text):
        # "-0" 用 int 会丢掉负号，交给 JsonNumber 原样保存
        return JsonNumber(text) if text == "-0" else int(text)
    if _JSON_NUM_RE.match(text):
        return JsonNumber(text)
    return text


def _canonical_token(value: Any, case_sensitive: bool = True) -> Any:
    """把任意 JSON 值转成可哈希的规范形式（键排序、数字按字面量区分）。

    迭代实现：先按“父在前”的顺序收集所有节点，再逆序回填，天然满足后序。
    """
    order: List[Any] = []
    stack: List[Any] = [value]
    guard = 0
    while stack:
        node = stack.pop()
        kind = json_type(node)
        if kind not in (TYPE_OBJECT, TYPE_ARRAY):
            continue
        guard += 1
        if guard > 2_000_000:  # pragma: no cover - 防御
            raise _ConvertError("value_too_large", "cannot canonicalize such a large value")
        order.append(node)
        if kind == TYPE_OBJECT:
            stack.extend(node.values())
        else:
            stack.extend(node)

    memo: Dict[int, Any] = {}

    def leaf(node: Any) -> Any:
        kind = json_type(node)
        if kind == "string":
            return ("s", node if case_sensitive else node.lower())
        if kind == "number":
            try:
                return ("n", number_literal(node))
            except (ValueError, TypeError, InvalidOperation, ArithmeticError):
                return ("n", "nan")
        if kind == "boolean":
            return ("b", bool(node))
        return ("z", 0)

    def token_of(node: Any) -> Any:
        # 标量没有进 order/memo，就地计算；容器一定已经被逆序回填过
        return memo[id(node)] if isinstance(node, (dict, list)) else leaf(node)

    for node in reversed(order):
        if isinstance(node, dict):
            items = tuple(sorted((key, token_of(item)) for key, item in node.items()))
            memo[id(node)] = ("o", items)
        else:
            memo[id(node)] = ("a", tuple(token_of(item) for item in node))

    return token_of(value)


def _display_text(value: Any) -> str:
    """给 meta 用的人类可读文本（标量原样，容器紧凑 JSON）。"""
    kind = json_type(value)
    if kind in (TYPE_OBJECT, TYPE_ARRAY):
        return minify(value)
    return _scalar_text(value, _WarnBag(), "", "null")


def _resolve_path(value: Any, path: str) -> Tuple[bool, Any]:
    """按点号路径取值，数字段作为数组下标。返回 ``(是否存在, 值)``。"""
    if not path:
        return True, value
    current = value
    for raw in path.split("."):
        kind = json_type(current)
        if kind == TYPE_OBJECT:
            if raw in current:
                current = current[raw]
                continue
            if isinstance(current, dict):
                return False, None
        if kind == TYPE_ARRAY:
            index = _parse_index(raw, len(current))
            if index is None:
                return False, None
            current = current[index]
            continue
        return False, None
    return True, current


def _parse_index(raw: str, length: int) -> Optional[int]:
    if not _JSON_INT_RE.match(raw):
        return None
    index = int(raw)
    if index < 0:
        index += length
    if 0 <= index < length:
        return index
    return None


def _normalize_encoding(encoding: Any) -> str:
    """把编码名规范化；不支持的编码抛 :class:`_ConvertError`。"""
    if not isinstance(encoding, str) or not encoding.strip():
        raise _ConvertError("unsupported_encoding", f"empty encoding name; supported: {', '.join(SUPPORTED_ENCODINGS)}")
    try:
        info = codecs.lookup(encoding.strip())
    except LookupError:
        raise _ConvertError("unsupported_encoding",
                            f"unknown encoding {encoding!r}; supported: {', '.join(SUPPORTED_ENCODINGS)}") from None
    name = info.name.lower()
    if name not in SUPPORTED_ENCODINGS:
        raise _ConvertError("unsupported_encoding",
                            f"encoding {encoding!r} ({name}) is not supported; use one of {', '.join(SUPPORTED_ENCODINGS)}")
    return name


# --------------------------------------------------------------------------- #
# JSON -> CSV
# --------------------------------------------------------------------------- #
_VALUE_COLUMN = "value"
_EMPTY_OBJECT_TEXT = "{}"


def _normalize_delimiter(delimiter: Any) -> str:
    if isinstance(delimiter, str):
        text = delimiter
        if text == "\\t":
            text = "\t"
    else:
        text = str(delimiter)
    if len(text) != 1:
        raise _ConvertError("csv_bad_delimiter", "delimiter must be exactly one character")
    if text in ('"', "\r", "\n", "\x00"):
        raise _ConvertError("csv_bad_delimiter", f"delimiter {text!r} is not allowed")
    return text


def _flatten_record(record: Any, opts: CsvOptions, warn: _WarnBag) -> "Dict[str, Any]":
    """把一条记录展平成 ``路径 -> 值`` 的有序字典；数组原样留着交给展开阶段。

    栈里存 ``(kind, node, path, depth)``：``kind == "kv"`` 时 ``path`` 是完整列名，
    ``kind == "dict"`` 时 ``path`` 是已带分隔符的前缀。深度优先但按原始键序处理，
    因此列顺序就是首次出现顺序。
    """
    out: Dict[str, Any] = {}
    if not isinstance(record, dict):
        out[_VALUE_COLUMN] = record
        return out
    if not opts.flatten:
        for key, value in record.items():
            path = key if isinstance(key, str) else str(key)
            kind = json_type(value)
            out[path] = minify(value) if kind in (TYPE_OBJECT, TYPE_ARRAY) else value
        return out

    sep = opts.flatten_separator
    stack: List[Tuple[str, Any, str, int]] = []
    for key in reversed(list(record.keys())):
        stack.append(("kv", record[key], key if isinstance(key, str) else str(key), 0))
    while stack:
        kind, node, path, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise _TooDeepError("CSV flattening", MAX_DEPTH)
        if kind == "dict":
            for key in reversed(list(node.keys())):
                name = key if isinstance(key, str) else str(key)
                stack.append(("kv", node[key], f"{path}{name}", depth))
            continue
        if path in out:
            warn.add("csv_flatten_collision",
                     f"column {path!r} is produced twice (a literal key already contains the separator); the first value wins")
            continue
        value_kind = json_type(node)
        if value_kind == TYPE_OBJECT:
            if node:
                stack.append(("dict", node, f"{path}{sep}", depth + 1))
            else:
                out[path] = _EMPTY_OBJECT_TEXT
        elif value_kind == TYPE_ARRAY:
            out[path] = node  # 数组原样留着，交给 _rows_for_record 处理
        else:
            out[path] = node
    return out


def _render_array_cell(array: List[Any], opts: CsvOptions, warn: _WarnBag, path: str) -> str:
    if opts.array_handling == ARRAY_JOIN:
        return opts.join_separator.join(_scalar_text(item, warn, f"{path}[{i}]", opts.null_text)
                                        for i, item in enumerate(array))
    return minify(array)


def _expand_field(rows: List[Dict[str, Any]], path: str, array: List[Any],
                  opts: CsvOptions, warn: _WarnBag) -> List[Dict[str, Any]]:
    """``array_handling="expand"``：把一个数组字段摊开成多行。"""
    if not array:
        for row in rows:
            row[path] = "[]"
        return rows
    if len(rows) * len(array) > MAX_EXPAND_ROWS:
        warn.add("csv_expand_overflow",
                 f"{path or '$'} would expand to {len(rows) * len(array)} rows (limit {MAX_EXPAND_ROWS}); written as JSON text instead")
        for row in rows:
            row[path] = minify(array)
        return rows

    sep = opts.flatten_separator
    new_rows: List[Dict[str, Any]] = []
    for row in rows:
        for index, element in enumerate(array):
            clone = dict(row)
            kind = json_type(element)
            if kind == TYPE_OBJECT:
                if element:
                    _merge_flat_object(clone, element, f"{path}{sep}", warn, sep)
                else:
                    clone[path] = _EMPTY_OBJECT_TEXT
            elif kind == TYPE_ARRAY:
                warn.add("csv_nested_array",
                         f"{path}[{index}] is an array inside an array; written as JSON text")
                clone[path] = minify(element)
            else:
                clone[path] = element
            new_rows.append(clone)
    return new_rows


def _merge_flat_object(target: Dict[str, Any], node: Dict[str, Any], prefix: str,
                       warn: _WarnBag, sep: str = ".") -> None:
    """把对象展平后并入 ``target``（数组元素展开时用，内部数组不再二次展开）。

    与 :func:`_flatten_record` 一样，每个键都作为独立工作项逆序入栈，
    保证出栈顺序 == 原始键顺序，列顺序因此稳定。
    """
    stack: List[Tuple[str, Any, str, int]] = []
    for key in reversed(list(node.keys())):
        name = key if isinstance(key, str) else str(key)
        stack.append(("kv", node[key], f"{prefix}{name}", 0))
    while stack:
        kind, value, path, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise _TooDeepError("CSV flattening", MAX_DEPTH)
        if kind == "dict":
            for key in reversed(list(value.keys())):
                name = key if isinstance(key, str) else str(key)
                stack.append(("kv", value[key], f"{path}{name}", depth))
            continue
        item_kind = json_type(value)
        if item_kind == TYPE_OBJECT:
            if value:
                stack.append(("dict", value, f"{path}{sep}", depth + 1))
            else:
                target.setdefault(path, _EMPTY_OBJECT_TEXT)
        elif item_kind == TYPE_ARRAY:
            target.setdefault(path, minify(value))
        else:
            target.setdefault(path, value)


def _rows_for_record(record: Any, opts: CsvOptions, warn: _WarnBag) -> List[Dict[str, Any]]:
    flat = _flatten_record(record, opts, warn)
    if opts.array_handling != ARRAY_EXPAND:
        rendered: Dict[str, Any] = {}
        for path, value in flat.items():
            rendered[path] = _render_array_cell(value, opts, warn, path) if isinstance(value, list) else value
        return [rendered]

    rows: List[Dict[str, Any]] = [{}]
    for path, value in flat.items():
        if isinstance(value, list):
            rows = _expand_field(rows, path, value, opts, warn)
        else:
            for row in rows:
                row[path] = value
    return rows


@_safe
def json_to_csv(value: Any, options: Optional[CsvOptions] = None) -> ConvertResult:
    """JSON -> CSV 文本。约定见模块 docstring。"""
    opts = options if isinstance(options, CsvOptions) else CsvOptions()
    warn = _WarnBag()
    delimiter = _normalize_delimiter(opts.delimiter)
    line_ending = opts.line_ending if opts.line_ending in ("\n", "\r\n") else "\r\n"
    if opts.line_ending != line_ending:
        warn.add("csv_bad_line_ending", f"line_ending {opts.line_ending!r} replaced by {line_ending!r}")
    if opts.array_handling not in ARRAY_MODES:
        warn.add("csv_bad_array_handling",
                 f"array_handling {opts.array_handling!r} replaced by {ARRAY_EXPAND!r}")
        opts = replace(opts, array_handling=ARRAY_EXPAND)
    encoding = _normalize_encoding(opts.encoding)
    if not opts.flatten:
        warn.add("csv_flatten_disabled", "nested containers are written as compact JSON text")

    if value is None:
        raise _ConvertError("csv_null_root", "null cannot be represented as CSV; wrap it in an array or object")

    # ---- 收集记录 ----
    records: List[Any]
    skipped = 0
    kind = json_type(value)
    if kind == TYPE_ARRAY:
        records = []
        for item in value:
            if item is None:
                skipped += 1
                continue
            records.append(item)
    else:
        records = [value]
    _check_depth(value, "CSV conversion")

    # ---- 展平成行 ----
    rows: List[Dict[str, Any]] = []
    for record in records:
        rows.extend(_rows_for_record(record, opts, warn))

    # ---- 列顺序 ----
    seen: List[str] = []
    seen_set = set()
    for row in rows:
        for column in row:
            if column not in seen_set:
                seen_set.add(column)
                seen.append(column)
    dropped = 0
    if opts.columns:
        wanted = [str(c) for c in opts.columns]
        for column in wanted:
            if column not in seen_set:
                warn.add("csv_unknown_column", f"column {column!r} does not exist in the data")
        dropped = len([c for c in seen if c not in set(wanted)])
        if dropped:
            warn.add("csv_columns_dropped", f"{dropped} column(s) are not in CsvOptions.columns and were dropped", once=True)
        columns = wanted
    else:
        columns = seen

    if not columns:
        if rows:
            warn.add("csv_no_columns", "no scalar field was found, so nothing was written")
        return ConvertResult(ok=True, output="", warnings=warn.as_list(),
                             meta={"rows": 0, "columns": [], "records": len(records),
                                   "skipped": skipped, "dropped": 0, "encoding": encoding,
                                   "delimiter": delimiter})

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=delimiter, quotechar='"',
                        quoting=csv.QUOTE_ALL if opts.quote_all else csv.QUOTE_MINIMAL,
                        lineterminator=line_ending)
    if opts.include_header:
        writer.writerow(columns)
    written = 0
    for row in rows:
        cells = []
        for column in columns:
            if column in row:
                cells.append(_csv_cell(row[column], opts, warn, column))
            else:
                cells.append(opts.null_text)
        writer.writerow(cells)
        written += 1
    if skipped:
        warn.add("csv_skipped_nulls", f"{skipped} null element(s) in the root array produced no row")

    return ConvertResult(ok=True, output=buffer.getvalue(), warnings=warn.as_list(),
                         meta={"rows": written, "columns": columns, "records": len(records),
                               "skipped": skipped, "dropped": dropped, "encoding": encoding,
                               "delimiter": delimiter})


def _csv_cell(value: Any, opts: CsvOptions, warn: _WarnBag, column: str) -> str:
    if value is None:
        return opts.null_text
    kind = json_type(value)
    if kind in (TYPE_OBJECT, TYPE_ARRAY):
        return minify(value)
    text = _scalar_text(value, warn, column, opts.null_text)
    return text


# --------------------------------------------------------------------------- #
# CSV -> JSON
# --------------------------------------------------------------------------- #
@_safe
def csv_to_json(text: Any, *, delimiter: str = ",", has_header: bool = True,
                infer_types: bool = True,
                null_values: Sequence[str] = ("", "null", "NULL", "None"),
                root: str = "array", trim_whitespace: bool = True) -> ConvertResult:
    """CSV 文本 -> JSON。约定见模块 docstring。"""
    warn = _WarnBag()
    if text is None:
        raise _ConvertError("csv_empty_input", "no CSV text was provided")
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8-sig", errors="replace")
    if not isinstance(text, str):
        text = str(text)
    if not text.strip():
        raise _ConvertError("csv_empty_input", "the CSV text is empty")
    if root not in ("array", "object"):
        raise _ConvertError("csv_bad_root", f"root must be 'array' or 'object', got {root!r}")
    delim = _normalize_delimiter(delimiter)
    nulls = tuple(str(item) for item in (null_values or ()))

    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delim, quotechar='"')
    raw_rows: List[List[str]] = []
    blank = 0
    for raw in reader:
        if not raw or (len(raw) == 1 and raw[0] == ""):
            blank += 1
            continue
        raw_rows.append(raw)
    if blank:
        warn.add("csv_blank_rows", f"{blank} empty line(s) were skipped")
    if not raw_rows:
        raise _ConvertError("csv_empty_input", "the CSV text has no data rows")

    def clean(cell: str) -> str:
        return cell.strip() if trim_whitespace else cell

    width = max(len(row) for row in raw_rows)
    header_width = width
    if has_header:
        header = [clean(cell) for cell in raw_rows[0]]
        body = raw_rows[1:]
        header_width = len(header)
        keys: List[str] = []
        taken: set = set()
        for index, name in enumerate(header):
            base = name if name else f"column{index + 1}"
            if not name:
                warn.add("csv_empty_header", f"header cell {index + 1} is empty; renamed to {base!r}")
            candidate = base
            if candidate in taken:
                suffix = 2
                while f"{base}_{suffix}" in taken:
                    suffix += 1
                candidate = f"{base}_{suffix}"
                warn.add("csv_duplicate_header",
                         f"header {base!r} appears more than once; renamed to {candidate!r}")
            taken.add(candidate)
            keys.append(candidate)
        if not body:
            warn.add("csv_header_only", "the CSV text has a header row but no data rows")
    else:
        keys = [f"column{index + 1}" for index in range(width)]
        body = raw_rows

    for index in range(len(keys), width):
        keys.append(f"column{index + 1}")

    missing = _infer_text("", infer_types, nulls)
    records: List[Dict[str, Any]] = []
    for row in body:
        record: Dict[str, Any] = {}
        if has_header and len(row) > header_width:
            warn.add("csv_ragged_row",
                     f"a row has {len(row)} cells but the header only has {header_width}; extra cells were appended as column<N>")
        for index, cell in enumerate(row):
            key = keys[index] if index < len(keys) else f"column{index + 1}"
            value = _infer_text(clean(cell), infer_types, nulls)
            if key in record:
                warn.add("csv_duplicate_key", f"key {key!r} appears twice in one row; the last value wins")
            record[key] = value
        for index in range(len(row), len(keys)):
            record[keys[index]] = missing
        records.append(record)

    if root == "array":
        result_value: Any = records
        duplicate_keys = 0
    else:
        if not keys:
            raise _ConvertError("csv_object_needs_key", "root='object' needs at least one column")
        result_value = {}
        duplicate_keys = 0
        key_column = keys[0]
        for record in records:
            name = record.get(key_column)
            name = "" if name is None else _display_text(name)
            if len(keys) == 1:
                payload: Any = record.get(key_column)
            else:
                payload = {k: v for k, v in record.items() if k != key_column}
            if name in result_value:
                duplicate_keys += 1
            result_value[name] = payload
        if duplicate_keys:
            warn.add("csv_duplicate_object_key",
                     f"{duplicate_keys} duplicated key(s) in the first column; the last row wins")

    return ConvertResult(ok=True, output=_pretty_json(result_value), warnings=warn.as_list(),
                         meta={"rows": len(records), "columns": list(keys), "skipped": blank,
                                "root": root, "duplicate_keys": duplicate_keys})


# --------------------------------------------------------------------------- #
# JSON -> XML
# --------------------------------------------------------------------------- #
class _XmlNode:
    __slots__ = ("name", "original", "attrs", "text", "children", "marker", "nil")

    def __init__(self, name: str) -> None:
        self.name = name
        self.original: Optional[str] = None
        self.attrs: List[Tuple[str, str]] = []
        self.text: str = ""
        self.children: List["_XmlNode"] = []
        self.marker: str = ""
        self.nil = False

    def add(self, name: str) -> "_XmlNode":
        node = _XmlNode(name)
        self.children.append(node)
        return node


class _XmlFlags:
    __slots__ = ("json_ns", "xsi_ns")

    def __init__(self) -> None:
        self.json_ns = False
        self.xsi_ns = False


def _is_name_start(ch: str) -> bool:
    return ch == "_" or ch.isalpha()


def _is_name_char(ch: str) -> bool:
    return ch == "_" or ch == "." or ch == "-" or ch.isalpha() or ("0" <= ch <= "9")


def sanitize_xml_name(name: Any, fallback: str = "element") -> Tuple[str, bool]:
    """把任意键名清洗成合法 XML Name；返回 ``(名字, 是否被改动)``。"""
    text = name if isinstance(name, str) else str(name)
    if not text:
        return fallback, True
    chars: List[str] = []
    for index, ch in enumerate(text):
        ok = _is_name_start(ch) if index == 0 else _is_name_char(ch)
        chars.append(ch if ok else "_")
    cleaned = "".join(chars)
    if cleaned[:3].lower() == "xml":
        cleaned = "_" + cleaned
    return cleaned, cleaned != text


def _xml_escape_text(text: str, warn: _WarnBag, path: str) -> str:
    illegal = _XML_ILLEGAL_RE.search(text)
    if illegal:
        warn.add("xml_illegal_char",
                 f"{path or '$'} contains character U+{ord(illegal.group(0)):04X} which is illegal in XML 1.0; replaced by U+FFFD")
        text = _XML_ILLEGAL_RE.sub("\ufffd", text)
    out = []
    for ch in text:
        if ch == "&":
            out.append("&amp;")
        elif ch == "<":
            out.append("&lt;")
        elif ch == ">":
            out.append("&gt;")
        elif ch == "\r":
            out.append("&#13;")  # 避免 XML 行尾归一化把 \r 变成 \n
        else:
            out.append(ch)
    return "".join(out)


def _xml_escape_attr(text: str, warn: _WarnBag, path: str) -> str:
    escaped = _xml_escape_text(text, warn, path)
    return escaped.replace('"', "&quot;")


def _needs_string_marker(text: str) -> bool:
    """字符串在 ``infer_types=True`` 的读取端会不会被误判成别的类型？"""
    if text == "" or text.strip() == "":
        return True
    lowered = text.lower()
    if lowered in ("true", "false"):
        return True
    return bool(_JSON_NUM_RE.match(text))


def _xml_scalar_text(value: Any, warn: _WarnBag, path: str) -> Tuple[str, str]:
    """标量 -> (文本, 需要的 json:type 标记)。"""
    kind = json_type(value)
    if kind == "string":
        return value, (MARK_STRING if _needs_string_marker(value) else "")
    if kind == "boolean":
        return ("true" if value else "false"), ""
    if kind == "number":
        try:
            return number_literal(value), ""
        except (ValueError, TypeError, InvalidOperation, ArithmeticError):
            warn.add("xml_non_finite", f"{path or '$'} is NaN/Infinity which XML cannot express; written as xsi:nil")
            return "", "nil"
    if value is None:
        return "", "nil"
    return minify(value), MARK_STRING if _needs_string_marker(minify(value)) else ""


def _build_xml_tree(value: Any, opts: XmlOptions, warn: _WarnBag) -> Tuple[_XmlNode, _XmlFlags]:
    flags = _XmlFlags()
    root_name, root_changed = sanitize_xml_name(opts.root_name, "root")
    if not root_name:
        root_name = "root"
    root = _XmlNode(root_name)
    item_name, _ = sanitize_xml_name(opts.item_name, "item")
    if not item_name:
        item_name = "item"
    attr_prefix = opts.attr_prefix if isinstance(opts.attr_prefix, str) else "@"
    text_key = opts.text_key if isinstance(opts.text_key, str) else "#text"

    # stack item: (value, node, path)
    stack: List[Tuple[Any, _XmlNode, str, int]] = [(value, root, "", 0)]
    while stack:
        current, node, path, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise _TooDeepError("XML conversion", MAX_DEPTH)
        kind = json_type(current)

        if current is None:
            node.nil = True
            flags.xsi_ns = True
            continue

        if kind == "string":
            marker = MARK_STRING if _needs_string_marker(current) else ""
            if marker:
                node.marker = marker
                flags.json_ns = True
            node.text = current
            continue

        if kind == "boolean":
            node.text = "true" if current else "false"
            continue

        if kind == "number":
            text, marker = _xml_scalar_text(current, warn, path)
            if marker == "nil":
                node.nil = True
                flags.xsi_ns = True
            else:
                node.text = text
            continue

        if kind == TYPE_ARRAY:
            node.marker = MARK_ARRAY
            flags.json_ns = True
            items = list(current)
            children = [node.add(item_name) for _ in items]  # 正序建节点 = 文档顺序
            for index in reversed(range(len(items))):
                stack.append((items[index], children[index], f"{path}[{index}]", depth + 1))
            continue

        # ---- 对象 ----
        if not current:
            node.marker = MARK_OBJECT
            flags.json_ns = True
            continue

        used_names: Dict[str, int] = {}
        pending: List[Tuple[Any, _XmlNode, str]] = []
        # 只有 #text / @属性、没有子元素时，才能安全地给元素打 json:type="string"
        has_child_keys = any(
            (key if isinstance(key, str) else str(key)) != text_key
            and not (attr_prefix and (key if isinstance(key, str) else str(key)).startswith(attr_prefix))
            for key in current
        )
        for key in current:
            raw_key = key if isinstance(key, str) else str(key)
            child_path = f"{path}.{raw_key}" if path else raw_key
            item = current[key]
            if raw_key == text_key:
                text, marker = _xml_scalar_text(item, warn, child_path)
                if marker == "nil":
                    warn.add("xml_text_null", f"{child_path} is null; the element text was left empty")
                elif marker == MARK_STRING and not has_child_keys:
                    node.marker = MARK_STRING
                    flags.json_ns = True
                node.text = text
                continue
            if attr_prefix and raw_key.startswith(attr_prefix):
                attr_name_raw = raw_key[len(attr_prefix):]
                attr_name, changed = sanitize_xml_name(attr_name_raw, "attr")
                existing = {name for name, _ in node.attrs}
                if attr_name in existing:
                    warn.add("xml_attr_conflict",
                             f"attribute {attr_name!r} (from key {raw_key!r}) is already present; skipped")
                    continue
                if json_type(item) in (TYPE_OBJECT, TYPE_ARRAY):
                    warn.add("xml_attr_not_scalar",
                             f"{child_path} is not a scalar; written as compact JSON text in attribute {attr_name!r}")
                    attr_value = minify(item)
                elif item is None:
                    warn.add("xml_attr_null", f"{child_path} is null; attribute {attr_name!r} was left empty")
                    attr_value = ""
                else:
                    attr_value, _marker = _xml_scalar_text(item, warn, child_path)
                node.attrs.append((attr_name, attr_value))
                if changed:
                    warn.add("xml_name_sanitized",
                             f"attribute name {attr_name_raw!r} was sanitized to {attr_name!r}; the original name is lost")
                continue

            if json_type(item) == TYPE_ARRAY and len(item) >= 2:
                # 数组 -> 重复父键的同名兄弟元素（名字只解析一次，避免被当成撞名）
                array = list(item)
                name, original = _resolve_element_name(raw_key, used_names, warn, child_path)
                if original is not None:
                    flags.json_ns = True
                children = []
                for _element in array:
                    child = node.add(name)
                    child.original = original
                    children.append(child)
                for index in reversed(range(len(array))):
                    pending.append((array[index], children[index], f"{child_path}[{index}]"))
                continue

            name, original = _resolve_element_name(raw_key, used_names, warn, child_path)
            if original is not None:
                flags.json_ns = True
            child = node.add(name)
            child.original = original
            pending.append((item, child, child_path))

        for entry in reversed(pending):
            stack.append((entry[0], entry[1], entry[2], depth + 1))

    return root, flags


def _resolve_element_name(raw_key: str, used_names: Dict[str, int], warn: _WarnBag,
                          path: str) -> Tuple[str, Optional[str]]:
    """把键名变成合法且不撞名的 XML 元素名；返回 ``(名字, 需要写进 json:name 的原名)``。"""
    name, changed = sanitize_xml_name(raw_key, "element")
    original: Optional[str] = raw_key if changed else None
    if name in used_names:
        used_names[name] += 1
        candidate = f"{name}_{used_names[name]}"
        while candidate in used_names:
            used_names[name] += 1
            candidate = f"{name}_{used_names[name]}"
        warn.add("xml_name_conflict",
                 f"key {path!r} and an earlier key both sanitize to {name!r}; renamed to {candidate!r}")
        name = candidate
        original = raw_key
        used_names[name] = 1
    else:
        used_names[name] = 1
    if changed:
        warn.add("xml_name_sanitized",
                 f"element name {raw_key!r} was sanitized to {name!r}; the original is kept in json:name")
    if original == name:
        original = None
    return name, original


def _serialize_xml(root: _XmlNode, opts: XmlOptions, flags: _XmlFlags, warn: _WarnBag) -> str:
    pretty = bool(opts.pretty)
    nl = "\n" if pretty else ""
    indent = opts.indent if pretty else ""
    if indent and set(indent) - {" ", "\t"}:
        warn.add("xml_bad_indent", f"indent {indent!r} contains characters other than space/tab; replaced by two spaces")
        indent = "  "

    out: List[str] = []
    if opts.declaration:
        out.append('<?xml version="1.0" encoding="UTF-8"?>')
        out.append(nl)

    stack: List[List[Any]] = []
    node = root
    depth = 0
    prefix = ""
    while True:
        attrs = _node_attributes(node, node is root, flags, warn)
        text = _xml_escape_text(node.text, warn, node.name) if node.text else ""
        if node.children:
            out.append(f"{prefix}<{node.name}{attrs}>")
            if text:
                out.append(text)
            stack.append([node, depth, 0])
            node = node.children[0]
            depth += 1
            prefix = nl + indent * depth
            continue
        if text:
            out.append(f"{prefix}<{node.name}{attrs}>{text}</{node.name}>")
        else:
            out.append(f"{prefix}<{node.name}{attrs}/>")
        while True:
            if not stack:
                out.append("\n")
                return "".join(out)
            frame = stack[-1]
            frame[2] += 1
            parent, parent_depth, index = frame[0], frame[1], frame[2]
            if index < len(parent.children):
                node = parent.children[index]
                depth = parent_depth + 1
                prefix = nl + indent * depth
                break
            out.append(nl + indent * parent_depth)
            out.append(f"</{parent.name}>")
            stack.pop()


def _node_attributes(node: _XmlNode, is_root: bool, flags: _XmlFlags,
                     warn: Optional[_WarnBag] = None) -> str:
    if warn is None:
        warn = _WarnBag()
    parts: List[str] = []
    if is_root:
        if flags.xsi_ns:
            parts.append(f' xmlns:xsi="{XML_NS_XSI}"')
        if flags.json_ns:
            parts.append(f' xmlns:json="{XML_NS_JSON}"')
    if node.nil:
        parts.append(' xsi:nil="true"')
    if node.marker:
        parts.append(f' json:type="{node.marker}"')
    if node.original is not None:
        parts.append(' json:name="%s"' % _xml_escape_attr(node.original, warn, node.name))
    for name, value in node.attrs:
        parts.append(' %s="%s"' % (name, _xml_escape_attr(value, warn, name)))
    return "".join(parts)


@_safe
def json_to_xml(value: Any, options: Optional[XmlOptions] = None) -> ConvertResult:
    """JSON -> XML 文本。约定见模块 docstring。"""
    opts = options if isinstance(options, XmlOptions) else XmlOptions()
    warn = _WarnBag()
    _check_depth(value, "XML conversion")
    root, flags = _build_xml_tree(value, opts, warn)
    text = _serialize_xml(root, opts, flags, warn)
    return ConvertResult(ok=True, output=text, warnings=warn.as_list(),
                         meta={"root": root.name, "elements": _count_elements(root),
                               "namespaces": _namespace_list(flags),
                               "sanitized": warn.has("xml_name_sanitized")})


def _count_elements(root: _XmlNode) -> int:
    count = 0
    stack = [root]
    while stack:
        node = stack.pop()
        count += 1
        stack.extend(node.children)
    return count


def _namespace_list(flags: _XmlFlags) -> List[str]:
    names = []
    if flags.xsi_ns:
        names.append("xsi")
    if flags.json_ns:
        names.append("json")
    return names


# --------------------------------------------------------------------------- #
# XML -> JSON
# --------------------------------------------------------------------------- #
def _local_name(tag: str) -> str:
    if isinstance(tag, str) and tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag if isinstance(tag, str) else str(tag)


@_safe
def xml_to_json(text: Any, *, attr_prefix: str = "@", text_key: str = "#text",
                infer_types: bool = True, root_as_array: bool = False) -> ConvertResult:
    """XML 文本 -> JSON。约定见模块 docstring；恶意 XML 一律拒绝，绝不抛出。"""
    warn = _WarnBag()
    if text is None:
        raise _ConvertError("xml_empty_input", "no XML text was provided")
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise _ConvertError("xml_decode_error", f"the input is not valid UTF-8 ({exc})") from None
    if not isinstance(text, str):
        text = str(text)
    if text.startswith("\ufeff"):
        text = text[1:]
    if not text.strip():
        raise _ConvertError("xml_empty_input", "the XML text is empty")
    if _DOCTYPE_RE.search(text):
        raise _ConvertError("xml_doctype_not_allowed",
                            "DOCTYPE/ENTITY declarations are rejected to block entity-expansion and external-entity attacks")
    attr_prefix = attr_prefix if isinstance(attr_prefix, str) else "@"
    text_key = text_key if isinstance(text_key, str) else "#text"

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise _ConvertError("xml_parse_error", str(exc)) from None
    except ValueError as exc:
        raise _ConvertError("xml_parse_error", str(exc)) from None

    # ---- 迭代式文档序遍历（父在前），再逆序回填 -> 等价后序 ----
    order: List[Any] = []
    depths: Dict[int, int] = {}
    stack: List[Tuple[Any, int]] = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise _TooDeepError("XML conversion", MAX_DEPTH)
        order.append(element)
        depths[id(element)] = depth
        for child in list(element):
            stack.append((child, depth + 1))

    values: Dict[int, Any] = {}
    for element in reversed(order):
        children = list(element)
        pairs: List[Tuple[str, Any]] = []
        for child in children:
            override = child.attrib.get(_JSON_NAME_ATTR)
            # json:name="" 也是有效覆盖（原始键名就是空串），所以只判断 None
            name = override if isinstance(override, str) else _local_name(child.tag)
            pairs.append((name, values[id(child)]))
            if child.tail and child.tail.strip():
                warn.add("xml_tail_text_dropped",
                         f"text after <{_local_name(child.tag)}> has no JSON representation and was dropped")
            if child.tag.startswith("{") or any(k.startswith("{") for k in child.attrib):
                warn.add("xml_namespace_dropped", "XML namespace URIs were dropped; only local names are kept", once=True)
        values[id(element)] = _element_value(element, pairs, warn,
                                            attr_prefix=attr_prefix, text_key=text_key,
                                            infer_types=infer_types,
                                            is_root=element is root,
                                            root_as_array=root_as_array)

    result_value = values[id(root)]
    return ConvertResult(ok=True, output=_pretty_json(result_value), warnings=warn.as_list(),
                         meta={"root": _local_name(root.tag), "elements": len(order),
                               "max_depth": max(depths.values()) if depths else 0})


def _element_value(element: Any, pairs: List[Tuple[str, Any]], warn: _WarnBag, *,
                   attr_prefix: str, text_key: str, infer_types: bool,
                   is_root: bool, root_as_array: bool) -> Any:
    attrib = element.attrib
    if attrib.get(_XSI_NIL_ATTR) in ("true", "1", "yes"):
        return None
    marker = attrib.get(_JSON_TYPE_ATTR, "")

    child_values = [value for _name, value in pairs]
    if marker == MARK_ARRAY:
        return child_values

    attrs: Dict[str, Any] = {}
    for name, raw in attrib.items():
        if name in (_JSON_TYPE_ATTR, _JSON_NAME_ATTR, _XSI_NIL_ATTR):
            continue
        local = _local_name(name)
        key = f"{attr_prefix}{local}"
        if key in attrs:
            warn.add("xml_attr_conflict", f"attribute {key!r} appears twice; the last value wins")
        attrs[key] = _infer_text(raw, infer_types)

    if is_root and root_as_array:
        if attrs or (element.text or "").strip():
            warn.add("xml_root_content_dropped",
                     "root_as_array keeps only the child elements; attributes and text on the root element were dropped")
        return child_values

    text = element.text
    if marker == MARK_OBJECT:
        merged = dict(attrs)
        if text is not None and text.strip():
            merged[text_key] = _infer_text(text.strip(), infer_types)
        merged.update(_group_pairs(pairs, warn, attr_prefix, text_key))
        return merged

    if pairs:
        merged = dict(attrs)
        if text is not None and text.strip():
            # 混合内容里，文本前后的空白是排版缩进（XML 解析器会把它并进 text）
            merged[text_key] = _infer_text(text.strip(), infer_types)
        for key, value in _group_pairs(pairs, warn, attr_prefix, text_key).items():
            if key in merged:
                warn.add("xml_key_conflict", f"key {key!r} comes from both an attribute and a child element")
            merged[key] = value
        return merged

    if attrs:
        merged = dict(attrs)
        if text is not None and text != "":
            merged[text_key] = text if marker == MARK_STRING else _infer_text(text, infer_types)
        return merged

    # 纯叶子元素
    if text is None:
        if is_root and marker == "":
            return [] if root_as_array else {}
        return ""
    if marker == MARK_STRING:
        return text
    if marker == "number":
        return _infer_text(text, True)
    if marker == "boolean":
        return text.strip().lower() == "true"
    if marker == "null":
        return None
    if text.strip() == "":
        # 没有标记的纯空白文本视为排版缩进；写出的空白串一定带 json:type="string"
        return ""
    return _infer_text(text, infer_types)


def _group_pairs(pairs: List[Tuple[str, Any]], warn: _WarnBag,
                 attr_prefix: str, text_key: str) -> Dict[str, Any]:
    groups: Dict[str, List[Any]] = {}
    order: List[str] = []
    for name, value in pairs:
        if name not in groups:
            groups[name] = []
            order.append(name)
        groups[name].append(value)
    out: Dict[str, Any] = {}
    for name in order:
        if name == text_key:
            warn.add("xml_text_key_element", f"a child element is named {text_key!r}; it may clash with element text")
        values = groups[name]
        out[name] = values[0] if len(values) == 1 else values
    return out


# --------------------------------------------------------------------------- #
# JSON <-> YAML
# --------------------------------------------------------------------------- #
class _JsonYamlDumper(yaml.SafeDumper):
    """只输出标准 YAML 标签的 Dumper；``JsonNumber`` 按原文输出。"""


class _JsonYamlLoader(yaml.SafeLoader):
    """SafeLoader 子类：int/float 构造器保留原始字面量，并把告警带回调用方。"""

    def __init__(self, stream: Any) -> None:
        super().__init__(stream)
        self.json_warnings: List[Tuple[str, str]] = []


def _yaml_tag_for(raw: str) -> str:
    lowered = raw.lower()
    if "e" in lowered or "." in raw:
        return "tag:yaml.org,2002:float"
    return "tag:yaml.org,2002:int"


def _represent_json_number(dumper: yaml.Dumper, data: JsonNumber) -> Any:
    raw = data.raw
    return dumper.represent_scalar(_yaml_tag_for(raw), raw)


def _represent_decimal(dumper: yaml.Dumper, data: Decimal) -> Any:
    from ..core.jsonvalue import _decimal_literal  # 复用 core 的字面量规则

    raw = _decimal_literal(data)
    return dumper.represent_scalar(_yaml_tag_for(raw), raw)


def _represent_float(dumper: yaml.Dumper, data: float) -> Any:
    if data != data or data in (float("inf"), float("-inf")):
        return dumper.represent_none(None)
    return dumper.represent_scalar("tag:yaml.org,2002:float", repr(data))


_JsonYamlDumper.add_representer(JsonNumber, _represent_json_number)
_JsonYamlDumper.add_representer(Decimal, _represent_decimal)
_JsonYamlDumper.add_representer(float, _represent_float)


def _construct_yaml_int(loader: _JsonYamlLoader, node: Any) -> Any:
    raw = node.value if isinstance(node.value, str) else str(node.value)
    if raw == "-0":
        # int("-0") == 0 会丢掉负号，交给 JsonNumber 原样保存
        return JsonNumber(raw)
    if _JSON_INT_RE.match(raw):
        return int(raw)
    if _JSON_NUM_RE.match(raw):
        return JsonNumber(raw)
    try:
        value = int(raw.replace("_", ""), 0)
    except (ValueError, TypeError):
        value = yaml.constructor.SafeConstructor.construct_yaml_int(loader, node)
    loader.json_warnings.append(("yaml_int_literal_normalized",
                                 f"YAML integer {raw!r} is not a JSON literal; written as {value}"))
    return value


def _construct_yaml_float(loader: _JsonYamlLoader, node: Any) -> Any:
    raw = (node.value if isinstance(node.value, str) else str(node.value)).strip()
    lowered = raw.lower().replace("_", "")
    if lowered in (".inf", "+.inf", "-.inf", ".nan"):
        loader.json_warnings.append(("yaml_non_finite", f"YAML value {raw!r} has no JSON equivalent; converted to null"))
        return None
    if _JSON_NUM_RE.match(raw):
        return JsonNumber(raw)
    try:
        value = float(lowered)
    except (ValueError, TypeError):
        value = yaml.constructor.SafeConstructor.construct_yaml_float(loader, node)
        if value is None:
            return None
    if value != value or value in (float("inf"), float("-inf")):
        loader.json_warnings.append(("yaml_non_finite", f"YAML value {raw!r} has no JSON equivalent; converted to null"))
        return None
    literal = repr(value)
    loader.json_warnings.append(("yaml_float_literal_normalized",
                                 f"YAML float {raw!r} is not a JSON literal; written as {literal}"))
    return JsonNumber(literal)


_JsonYamlLoader.add_constructor("tag:yaml.org,2002:int", _construct_yaml_int)
_JsonYamlLoader.add_constructor("tag:yaml.org,2002:float", _construct_yaml_float)


def _number_warnings(value: Any, warn: _WarnBag, path: str) -> None:
    """模拟“通用 YAML 读取器”会看到什么，并据此给出精度/字面量告警。

    * :func:`to_plain` 给出的就是外部库（float/int）能表达的值；
    * PyYAML 的隐式解析器决定原文要不要加显式 ``!!float`` 标签。
    本模块自己的 :func:`yaml_to_json` 会读回原始字面量，所以这些都是
    “换个工具读就丢东西”的提示，而不是本模块的输出缺陷。
    """
    if not isinstance(value, JsonNumber):
        return
    raw = value.raw
    where = path or "$"
    try:
        plain = to_plain(value)
    except (OverflowError, ValueError, InvalidOperation, ArithmeticError):
        warn.add("yaml_number_precision",
                 f"{where} = {raw} overflows an IEEE double; a generic YAML reader sees Infinity")
        return
    if isinstance(plain, float):
        if plain != plain or plain in (float("inf"), float("-inf")):
            warn.add("yaml_number_precision",
                     f"{where} = {raw} overflows an IEEE double; a generic YAML reader sees Infinity")
            return
        if Decimal(repr(plain)) != value:
            warn.add("yaml_number_precision",
                     f"{where} = {raw} cannot be represented exactly as a double; the literal is kept in the YAML text but a generic reader loses precision")
            return
    if raw == "-0":
        # 数值没变，但负号对 int/float 读取端不可见
        warn.add("yaml_negative_zero", f"{where} = -0 is read back as 0 by a generic YAML reader")
        return
    expected = _yaml_tag_for(raw)
    try:
        detected = yaml.resolver.Resolver().resolve(yaml.ScalarNode, raw, (True, False))
    except Exception:  # pragma: no cover - 解析器不会抛，纯防御
        detected = expected
    if detected != expected:
        warn.add("yaml_literal_tagged",
                 f"{where} = {raw} is written as !!{expected.rsplit(':', 1)[-1]} {raw} because YAML 1.1 does not read this literal as a number")


def _collect_number_warnings(value: Any, warn: _WarnBag) -> None:
    stack: List[Tuple[Any, str]] = [(value, "")]
    guard = 0
    while stack:
        node, path = stack.pop()
        kind = json_type(node)
        if kind == "number":
            _number_warnings(node, warn, path)
            continue
        guard += 1
        if guard > 1_000_000:  # pragma: no cover - 防御
            return
        if kind == TYPE_OBJECT:
            for key in reversed(list(node.keys())):
                stack.append((node[key], f"{path}.{key}" if path else str(key)))
        elif kind == TYPE_ARRAY:
            for index in reversed(range(len(node))):
                stack.append((node[index], f"{path}[{index}]"))


def _plainify_for_yaml(value: Any, warn: _WarnBag, path: str) -> Any:
    """把非 JSON 原生类型（``float``、``Decimal``、``tuple`` 等）转成可安全 dump 的形式。

    ``JsonNumber`` 与 ``Decimal`` 保留原对象（自定义 representer 按字面量输出），
    ``tuple`` 转成 ``list``，其它奇异类型退化成字符串并告警。
    """
    if isinstance(value, (dict, list)):
        return _plainify_containers(value, warn)
    if isinstance(value, JsonNumber):
        return value
    if isinstance(value, (str, bool)) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            warn.add("number_not_finite", f"{path or '$'} is NaN/Infinity; written as null")
            return None
        return value
    if isinstance(value, Decimal):
        if value.is_nan() or value.is_infinite():
            warn.add("number_not_finite", f"{path or '$'} is not a finite number; written as null")
            return None
        return value  # 交给 _represent_decimal，2.250 不会退化成 2.25
    if isinstance(value, tuple):
        return list(value)
    warn.add("yaml_unsupported_type", f"{path or '$'} has type {type(value).__name__}; written as a string")
    return str(value)


def _plainify_containers(value: Any, warn: _WarnBag) -> Any:
    """迭代式地把容器内的奇异标量替换成 YAML 可表达的值；不修改输入。"""
    root_is_dict = isinstance(value, dict)
    root: Any = {} if root_is_dict else []
    stack: List[Tuple[Any, Any, str]] = [(value, root, "")]
    guard = 0
    while stack:
        src, dst, path = stack.pop()
        guard += 1
        if guard > 2_000_000:  # pragma: no cover - 防御
            raise _ConvertError("value_too_large", "the document is too large to convert")
        if isinstance(src, dict):
            for key, item in src.items():
                child_path = f"{path}.{key}" if path else str(key)
                if isinstance(item, dict):
                    child: Dict[str, Any] = {}
                    dst[key] = child
                    stack.append((item, child, child_path))
                elif isinstance(item, list):
                    child_list: List[Any] = []
                    dst[key] = child_list
                    stack.append((item, child_list, child_path))
                else:
                    dst[key] = _plainify_for_yaml(item, warn, child_path)
        else:
            for index, item in enumerate(src):
                child_path = f"{path}[{index}]"
                if isinstance(item, dict):
                    child = {}
                    dst.append(child)
                    stack.append((item, child, child_path))
                elif isinstance(item, list):
                    child_list = []
                    dst.append(child_list)
                    stack.append((item, child_list, child_path))
                else:
                    dst.append(_plainify_for_yaml(item, warn, child_path))
    return root


@_safe
def json_to_yaml(value: Any, options: Optional[YamlOptions] = None) -> ConvertResult:
    """JSON -> YAML 文本（标准标签、数字字面量原样保留）。"""
    opts = options if isinstance(options, YamlOptions) else YamlOptions()
    warn = _WarnBag()
    indent = int(opts.indent) if isinstance(opts.indent, int) else 2
    if not 2 <= indent <= 9:
        warn.add("yaml_bad_indent", f"indent {opts.indent!r} is out of range 2..9; using 2")
        indent = 2
    width = int(opts.width) if isinstance(opts.width, int) else 4096
    if width < 20:
        warn.add("yaml_bad_width", f"width {opts.width!r} is too small; using 4096")
        width = 4096

    depth = _measure_depth(value)
    if depth > YAML_MAX_DEPTH:
        raise _ConvertError("yaml_depth_exceeded",
                            f"the document nests {depth} levels deep but the YAML emitter supports at most {YAML_MAX_DEPTH}; export JSON/CSV/XML instead or flatten the data")

    _collect_number_warnings(value, warn)
    prepared = _plainify_containers(value, warn) if isinstance(value, (dict, list)) else _plainify_for_yaml(value, warn, "")

    try:
        text = yaml.dump(prepared, Dumper=_JsonYamlDumper, sort_keys=bool(opts.sort_keys),
                         allow_unicode=bool(opts.allow_unicode), indent=indent, width=width,
                         default_flow_style=False)
    except yaml.YAMLError as exc:
        raise _ConvertError("yaml_dump_error", str(exc).splitlines()[0]) from None
    except RecursionError:
        raise _ConvertError("yaml_depth_exceeded",
                            f"the YAML emitter ran out of stack; the document nests deeper than {YAML_MAX_DEPTH} levels") from None
    except TypeError as exc:
        raise _ConvertError("yaml_unsupported_type", str(exc)) from None
    if not text:
        text = "null\n"
    return ConvertResult(ok=True, output=text, warnings=warn.as_list(),
                         meta={"depth": depth, "indent": indent, "width": width,
                               "sort_keys": bool(opts.sort_keys),
                               "allow_unicode": bool(opts.allow_unicode)})


def _normalize_yaml_value(value: Any, warn: _WarnBag, path: str) -> Any:
    """把 ``yaml.safe_load`` 的结果迭代式转成 JSON 值模型。"""
    if isinstance(value, (dict, list, set, tuple)):
        return _normalize_yaml_containers(value, warn)
    return _normalize_yaml_scalar(value, warn, path)


def _normalize_yaml_scalar(value: Any, warn: _WarnBag, path: str) -> Any:
    if value is None or isinstance(value, (bool, str, int)):
        return value  # 已经是合法 JSON 值（JsonNumber 是 Decimal，不会落进这个分支）
    if isinstance(value, JsonNumber):
        return value
    if isinstance(value, Decimal):
        try:
            return JsonNumber(number_literal(value))
        except (ValueError, TypeError, InvalidOperation, ArithmeticError):
            warn.add("yaml_non_finite", f"{path or '$'} is not a finite number; converted to null")
            return None
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            warn.add("yaml_non_finite", f"{path or '$'} is not a finite number; converted to null")
            return None
        return JsonNumber(repr(value))
    if isinstance(value, (bytes, bytearray)):
        encoded = base64.b64encode(bytes(value)).decode("ascii")
        warn.add("yaml_binary_to_base64", f"{path or '$'} is binary data; converted to a base64 string")
        return encoded
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        warn.add("yaml_timestamp_to_string", f"{path or '$'} is a timestamp; converted to an ISO string")
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        warn.add("yaml_timestamp_to_string", f"{path or '$'} is a duration; converted to seconds")
        return JsonNumber(repr(value.total_seconds()))
    warn.add("yaml_unsupported_type", f"{path or '$'} has type {type(value).__name__}; converted to a string")
    return str(value)


def _yaml_key_text(key: Any, warn: _WarnBag, path: str) -> str:
    if isinstance(key, str):
        return key
    if isinstance(key, bool):
        warn.add("yaml_non_string_key", f"{path or '$'} has a boolean key; converted to text")
        return "true" if key else "false"
    if key is None:
        warn.add("yaml_non_string_key", f"{path or '$'} has a null key; converted to 'null'")
        return "null"
    if isinstance(key, (int, float, Decimal, JsonNumber)):
        warn.add("yaml_non_string_key", f"{path or '$'} has a numeric key; converted to text")
        try:
            return number_literal(key)
        except (ValueError, TypeError, InvalidOperation, ArithmeticError):
            return str(key)
    if isinstance(key, (bytes, bytearray)):
        warn.add("yaml_non_string_key", f"{path or '$'} has a binary key; converted to base64 text")
        return base64.b64encode(bytes(key)).decode("ascii")
    if isinstance(key, (datetime.datetime, datetime.date, datetime.time)):
        warn.add("yaml_non_string_key", f"{path or '$'} has a timestamp key; converted to an ISO string")
        return key.isoformat()
    if isinstance(key, (tuple, list, set)):
        warn.add("yaml_non_string_key", f"{path or '$'} has a complex key; converted to JSON text")
        return minify(_normalize_yaml_value(list(key) if isinstance(key, (list, set)) else list(key), warn, path))
    warn.add("yaml_non_string_key", f"{path or '$'} has a {type(key).__name__} key; converted to text")
    return str(key)


def _normalize_yaml_containers(value: Any, warn: _WarnBag) -> Any:
    if isinstance(value, dict):
        root: Any = {}
    elif isinstance(value, set):
        root = []
        warn.add("yaml_set_to_array", "a YAML set was converted to an array")
    else:
        root = []
    stack: List[Tuple[Any, Any, str]] = [(value, root, "")]
    guard = 0
    while stack:
        src, dst, path = stack.pop()
        guard += 1
        if guard > 2_000_000:  # pragma: no cover - 防御
            raise _ConvertError("value_too_large", "the YAML document is too large to convert")
        if isinstance(src, dict):
            for key, item in src.items():
                name = _yaml_key_text(key, warn, path)
                child_path = f"{path}.{name}" if path else name
                if isinstance(item, (dict, list, set, tuple)):
                    if isinstance(item, set):
                        warn.add("yaml_set_to_array", f"{child_path} is a YAML set; converted to an array")
                        child: List[Any] = []
                        dst[name] = child
                        stack.append((list(item), child, child_path))
                    elif isinstance(item, dict):
                        child_map: Dict[str, Any] = {}
                        dst[name] = child_map
                        stack.append((item, child_map, child_path))
                    else:
                        child_list: List[Any] = []
                        dst[name] = child_list
                        stack.append((item, child_list, child_path))
                else:
                    dst[name] = _normalize_yaml_scalar(item, warn, child_path)
        else:
            items = list(src)
            for index, item in enumerate(items):
                child_path = f"{path}[{index}]"
                if isinstance(item, dict):
                    child_map = {}
                    dst.append(child_map)
                    stack.append((item, child_map, child_path))
                elif isinstance(item, (list, set, tuple)):
                    if isinstance(item, set):
                        warn.add("yaml_set_to_array", f"{child_path} is a YAML set; converted to an array")
                    child_list = []
                    dst.append(child_list)
                    stack.append((list(item), child_list, child_path))
                else:
                    dst.append(_normalize_yaml_scalar(item, warn, child_path))
    return root


@_safe
def yaml_to_json(text: Any) -> ConvertResult:
    """YAML 文本 -> JSON 文本（只用 SafeLoader，拒绝任何 ``!!python`` 标签）。"""
    warn = _WarnBag()
    if text is None:
        raise _ConvertError("yaml_empty_input", "no YAML text was provided")
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise _ConvertError("yaml_decode_error", f"the input is not valid UTF-8 ({exc})") from None
    if not isinstance(text, str):
        text = str(text)
    if not text.strip():
        warn.add("yaml_empty_document", "the YAML text is empty; the JSON result is null")
        return ConvertResult(ok=True, output="null", warnings=warn.as_list(), meta={"empty": True})
    if _UNSAFE_YAML_RE.search(text):
        raise _ConvertError("yaml_unsafe_tag",
                            "the document contains a language-specific tag (!!python/... ); only safe YAML is accepted")

    loader = _JsonYamlLoader(text)
    try:
        data = loader.get_single_data()
    except yaml.YAMLError as exc:
        message = str(exc).strip().splitlines()
        raise _ConvertError("yaml_error", message[0] if message else type(exc).__name__) from None
    except RecursionError:
        raise _ConvertError("yaml_too_deep", "the YAML document nests too deeply for the parser") from None
    finally:
        try:
            loader.dispose()
        except Exception:  # pragma: no cover - dispose 不应失败
            pass

    for code, detail in getattr(loader, "json_warnings", ()):
        warn.add(code, detail)

    value = _normalize_yaml_value(data, warn, "")
    _check_depth(value, "YAML conversion")
    return ConvertResult(ok=True, output=_pretty_json(value), warnings=warn.as_list(),
                         meta={"depth": _measure_depth(value)})


# --------------------------------------------------------------------------- #
# 字符串转义
# --------------------------------------------------------------------------- #
def escape_json_string(text: Any) -> str:
    """按 RFC 8259 转义字符串**内部**（不加首尾引号）。非字符串会先转成文本。"""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    return encode_json_string(text)[1:-1]


@_safe
def unescape_json_string(text: Any) -> ConvertResult:
    """解码 JSON 转义序列；如果整体被一对双引号包住，会先去掉引号。"""
    if text is None:
        raise _ConvertError("empty_input", "no text was provided")
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", errors="replace")
    if not isinstance(text, str):
        text = str(text)
    body = text
    if len(body) >= 2 and body.startswith('"') and body.endswith('"'):
        body = body[1:-1]
    try:
        decoded = decode_json_string(body)
    except ValueError as exc:
        raise _ConvertError("invalid_escape", str(exc)) from None
    if decoded != body:
        meta = {"changed": True}
    else:
        meta = {"changed": False}
    meta["length"] = len(decoded)
    return ConvertResult(ok=True, output=decoded, meta=meta)


# --------------------------------------------------------------------------- #
# 拆分 / 合并 / 去重
# --------------------------------------------------------------------------- #
@_safe
def split_array(value: Any, *, chunk_size: int = 0, by_field: str = "") -> ConvertResult:
    """把数组拆成若干片：按 ``chunk_size`` 等分，或按 ``by_field`` 分组。

    ``output`` 是 JSON Lines（每行一个紧凑 JSON 文档），``meta["pieces"]`` 是片列表。
    """
    if not isinstance(value, list):
        raise _ConvertError("split_not_array", f"the value is {json_type(value)}, but split_array needs an array")
    if by_field:
        if not isinstance(by_field, str):
            raise _ConvertError("split_bad_field", "by_field must be a dotted path string")
        groups: Dict[Any, List[Any]] = {}
        keys: Dict[Any, str] = {}
        order: List[Any] = []
        unkeyed = 0
        for index, item in enumerate(value):
            found, field_value = _resolve_path(item, by_field)
            if not found:
                unkeyed += 1
                token: Any = ("__missing__",)
                display = ""
            else:
                token = _canonical_token(field_value)
                display = _display_text(field_value)
            if token not in groups:
                groups[token] = []
                keys[token] = display
                order.append(token)
            groups[token].append(item)
        pieces = [groups[token] for token in order]
        labels = [keys[token] for token in order]
        if unkeyed:
            warn_text = f"{unkeyed} element(s) have no field {by_field!r}; they were grouped under the empty key"
        else:
            warn_text = ""
        warnings = [f"split_missing_field: {warn_text}"] if warn_text else []
        lines = [minify(piece) for piece in pieces]
        return ConvertResult(ok=True, output="".join(line + "\n" for line in lines),
                             warnings=warnings,
                             meta={"pieces": pieces, "count": len(pieces), "mode": "field",
                                   "field": by_field, "keys": labels, "total": len(value),
                                   "unkeyed": unkeyed})

    try:
        size = int(chunk_size)
    except (TypeError, ValueError):
        raise _ConvertError("split_bad_chunk_size", f"chunk_size must be an integer, got {chunk_size!r}") from None
    if size <= 0:
        raise _ConvertError("split_bad_chunk_size",
                            "chunk_size must be a positive integer when by_field is empty")
    pieces = [value[start:start + size] for start in range(0, len(value), size)]
    lines = [minify(piece) for piece in pieces]
    return ConvertResult(ok=True, output="".join(line + "\n" for line in lines),
                         meta={"pieces": pieces, "count": len(pieces), "mode": "chunk",
                               "chunk_size": size, "total": len(value)})


@_safe
def merge_documents(values: Any, *, strategy: str = MERGE_DEEP,
                    array_strategy: str = ARRAY_APPEND,
                    conflict: str = CONFLICT_LAST) -> ConvertResult:
    """合并多个 JSON 文档。约定见模块 docstring；输入不会被修改。"""
    if not isinstance(values, (list, tuple)):
        raise _ConvertError("merge_not_a_list", "values must be a list of JSON documents")
    docs = list(values)
    if not docs:
        raise _ConvertError("merge_no_documents", "no document was provided")
    if strategy not in MERGE_STRATEGIES:
        raise _ConvertError("merge_bad_strategy",
                            f"strategy must be one of {', '.join(MERGE_STRATEGIES)}, got {strategy!r}")
    if array_strategy not in ARRAY_STRATEGIES:
        raise _ConvertError("merge_bad_array_strategy",
                            f"array_strategy must be one of {', '.join(ARRAY_STRATEGIES)}, got {array_strategy!r}")
    if conflict not in CONFLICT_MODES:
        raise _ConvertError("merge_bad_conflict",
                            f"conflict must be one of {', '.join(CONFLICT_MODES)}, got {conflict!r}")
    for index, doc in enumerate(docs):
        _check_depth(doc, f"merge (document {index})")

    state = {"conflicts": 0}
    if len(docs) == 1:
        merged = _copy_deep(docs[0])
    elif strategy == MERGE_CONCAT:
        merged = []
        for index, doc in enumerate(docs):
            if not isinstance(doc, list):
                raise _ConvertError("merge_concat_requires_arrays",
                                    f"document {index} is {json_type(doc)}, but strategy 'concat' only merges top-level arrays")
            merged.extend(_copy_deep(doc))
    elif strategy == MERGE_SHALLOW:
        merged = _copy_deep(docs[0])
        if not isinstance(merged, dict):
            raise _ConvertError("merge_shallow_requires_objects",
                                f"strategy 'shallow' needs objects, but document 0 is {json_type(merged)}")
        for index, doc in enumerate(docs[1:], start=1):
            if not isinstance(doc, dict):
                raise _ConvertError("merge_shallow_requires_objects",
                                    f"strategy 'shallow' needs objects, but document {index} is {json_type(doc)}")
            for key, item in doc.items():
                if key in merged:
                    if json_equal(merged[key], item):
                        continue
                    state["conflicts"] += 1
                    if conflict == CONFLICT_ERROR:
                        raise _ConvertError("merge_conflict", f"documents disagree at '$.{key}'")
                    if conflict == CONFLICT_LAST:
                        merged[key] = _copy_deep(item)
                else:
                    merged[key] = _copy_deep(item)
    else:
        merged = _copy_deep(docs[0])
        slot: List[Any] = [merged]
        for index, doc in enumerate(docs[1:], start=1):
            _deep_merge(slot, doc, (), array_strategy, conflict, state)
            merged = slot[0]

    return ConvertResult(ok=True, output=_pretty_json(merged),
                         meta={"documents": len(docs), "strategy": strategy,
                               "array_strategy": array_strategy, "conflict": conflict,
                               "conflicts": state["conflicts"], "value": merged})


def _deep_merge(slot: List[Any], other: Any, path: Tuple[str, ...],
                array_strategy: str, conflict: str, state: Dict[str, int]) -> None:
    """迭代式深合并：``slot[0]`` 是私有副本，可原地修改。"""
    stack: List[Tuple[List[Any], Any, Any, Tuple[str, ...]]] = [(slot, 0, other, path)]
    guard = 0
    while stack:
        container, key, incoming, current_path = stack.pop()
        guard += 1
        if guard > 2_000_000:  # pragma: no cover - 防御
            raise _ConvertError("value_too_large", "the documents are too large to merge")
        existing = container[key]
        if isinstance(existing, dict) and isinstance(incoming, dict):
            for name in incoming:
                if name in existing:
                    stack.append((existing, name, incoming[name], current_path + (str(name),)))
                else:
                    existing[name] = _copy_deep(incoming[name])
            continue
        if isinstance(existing, list) and isinstance(incoming, list):
            copied = _copy_deep(incoming)
            if array_strategy == ARRAY_APPEND:
                existing.extend(copied)
            elif array_strategy == ARRAY_PREPEND:
                existing[0:0] = copied
            else:
                container[key] = copied
            continue
        if json_equal(existing, incoming):
            continue
        state["conflicts"] += 1
        label = "$" + "".join(f".{part}" for part in current_path)
        if conflict == CONFLICT_ERROR:
            raise _ConvertError("merge_conflict",
                                f"documents disagree at '{label}' ({json_type(existing)} vs {json_type(incoming)})")
        if conflict == CONFLICT_LAST:
            container[key] = _copy_deep(incoming)


@_safe
def dedupe_array(items: Any, *, key: str = "", case_sensitive: bool = True,
                 keep: str = KEEP_FIRST) -> ConvertResult:
    """数组去重：``key=""`` 比整个元素，``key="a.b"`` 比嵌套字段（点号路径）。"""
    if not isinstance(items, list):
        raise _ConvertError("dedupe_not_array", f"the value is {json_type(items)}, but dedupe_array needs an array")
    if keep not in (KEEP_FIRST, KEEP_LAST):
        raise _ConvertError("dedupe_bad_keep", f"keep must be 'first' or 'last', got {keep!r}")
    if key and not isinstance(key, str):
        raise _ConvertError("dedupe_bad_key", "key must be a dotted path string")

    tokens: List[Any] = []
    unkeyed = 0
    for index, item in enumerate(items):
        if key:
            found, field_value = _resolve_path(item, key)
            if not found:
                unkeyed += 1
                tokens.append(("__unkeyed__", index))
                continue
            tokens.append(_canonical_token(field_value, case_sensitive))
        else:
            tokens.append(_canonical_token(item, case_sensitive))

    counts: Dict[Any, int] = {}
    unique = 0
    for token in tokens:
        if key and token[0] == "__unkeyed__":
            continue
        if token not in counts:
            unique += 1
        counts[token] = counts.get(token, 0) + 1
    seen: Dict[Any, int] = {}
    result: List[Any] = []
    removed = 0
    for index, item in enumerate(items):
        token = tokens[index]
        if key and token[0] == "__unkeyed__":
            result.append(item)
            continue
        seen[token] = seen.get(token, 0) + 1
        if keep == KEEP_FIRST:
            if seen[token] == 1:
                result.append(item)
            else:
                removed += 1
        else:
            if seen[token] == counts[token]:
                result.append(item)
            else:
                removed += 1

    warnings: List[str] = []
    if unkeyed:
        warnings.append(f"dedupe_unkeyed: {unkeyed} element(s) have no field {key!r} and were always kept")
    return ConvertResult(ok=True, output=_pretty_json(result), warnings=warnings,
                         meta={"value": result, "removed": removed, "total": len(items),
                               "kept": len(result), "unique": unique, "unkeyed": unkeyed,
                               "key": key, "keep": keep, "case_sensitive": case_sensitive})


# --------------------------------------------------------------------------- #
# 写入文件
# --------------------------------------------------------------------------- #
def _safe_filename(text: str, max_len: int = 80) -> str:
    cleaned = _FILENAME_BAD_RE.sub("_", text)
    cleaned = cleaned.replace("..", "_").rstrip(" .")  # Windows 不允许结尾的点/空格
    if not cleaned or cleaned.startswith("."):
        cleaned = "part" + cleaned  # "{key}.json" 且 key 为空时不能丢掉扩展名
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
    return cleaned


@_safe
def split_to_files(value: Any, directory: Any, *, pattern: str = "part_{index}.json",
                   chunk_size: int = 0, by_field: str = "") -> ConvertResult:
    """把数组拆片并**真实写盘**（utf-8、2 空格缩进）；返回文件清单。"""
    if not isinstance(directory, str) or not directory.strip():
        raise _ConvertError("split_bad_directory", "directory must be a non-empty path")
    if not isinstance(pattern, str) or not pattern.strip():
        raise _ConvertError("split_bad_pattern", "pattern must be a non-empty file name template")
    if "/" in pattern or "\\" in pattern or os.sep in pattern or ".." in pattern:
        raise _ConvertError("split_unsafe_pattern",
                            "pattern must be a plain file name without path separators or '..'")

    pieces_result = split_array(value, chunk_size=chunk_size, by_field=by_field)
    if not pieces_result.ok:
        return ConvertResult(ok=False, error=pieces_result.error, warnings=list(pieces_result.warnings))
    pieces: List[Any] = pieces_result.meta.get("pieces", [])
    labels: List[str] = pieces_result.meta.get("keys") or [""] * len(pieces)
    warnings = list(pieces_result.warnings)

    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        raise _ConvertError("split_directory_error", f"cannot create {directory!r}: {exc}") from None
    if not os.path.isdir(directory):
        raise _ConvertError("split_directory_error", f"{directory!r} exists but is not a directory")
    if not os.access(directory, os.W_OK):
        raise _ConvertError("split_directory_not_writable", f"{directory!r} is not writable")

    files: List[str] = []
    used: set = set()
    total = len(pieces)
    for index, piece in enumerate(pieces):
        label = labels[index] if index < len(labels) else ""
        name = _render_pattern(pattern, index=index, count=total, total=pieces_result.meta.get("total", total),
                               key=_safe_filename(str(label)), field=by_field or "")
        if name in used:
            stem, ext = os.path.splitext(name)
            name = f"{stem}_{index}{ext}"
        used.add(name)
        path = os.path.join(directory, name)
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(_pretty_json(piece))
                handle.write("\n")
        except OSError as exc:
            raise _ConvertError("split_write_error", f"cannot write {path!r}: {exc}") from None
        files.append(path)

    return ConvertResult(ok=True, output="\n".join(files), warnings=warnings,
                         meta={"files": files, "count": len(files), "directory": os.path.abspath(directory),
                               "pattern": pattern, "mode": pieces_result.meta.get("mode", "")})


def _render_pattern(pattern: str, **values: Any) -> str:
    try:
        name = pattern.format(**values)
    except (KeyError, IndexError, ValueError) as exc:
        raise _ConvertError("split_bad_pattern",
                            f"pattern {pattern!r} could not be formatted ({exc}); available fields: {', '.join(sorted(values))}") from None
    name = _safe_filename(str(name))
    if not name:
        raise _ConvertError("split_bad_pattern", f"pattern {pattern!r} produced an empty file name")
    return name


# --------------------------------------------------------------------------- #
# 编码
# --------------------------------------------------------------------------- #
@_safe
def encoding_to_bytes(text: Any, encoding: Any) -> ConvertResult:
    """文本 -> 字节；``meta["data"]`` 是结果字节串。不支持的编码/字符报明确错误。"""
    name = _normalize_encoding(encoding)
    if text is None:
        raise _ConvertError("empty_input", "no text was provided")
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _ConvertError("encoding_decode_error", f"the input is not valid UTF-8 ({exc})") from None
    if not isinstance(text, str):
        text = str(text)
    try:
        data = text.encode(name)
    except UnicodeEncodeError as exc:
        bad = text[exc.start:exc.end] if exc.start < len(text) else ""
        codepoints = ", ".join(f"U+{ord(ch):04X}" for ch in bad[:4])
        raise _ConvertError("encoding_unencodable",
                            f"{name} cannot encode {codepoints or 'this character'} at offset {exc.start}; "
                            f"use utf-8 or gb18030 instead") from None
    except (LookupError, ValueError) as exc:
        raise _ConvertError("unsupported_encoding", str(exc)) from None
    return ConvertResult(ok=True, output="", meta={"data": data, "encoding": name,
                                                   "bytes": len(data), "characters": len(text),
                                                   "bom": name in _BOM_ENCODINGS})

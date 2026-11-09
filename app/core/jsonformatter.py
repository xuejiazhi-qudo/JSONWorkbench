"""JSON 格式化 / 压缩 / 排序。

三条硬性保证：

1. **格式化不改变数据**：默认不排序，键顺序与数组元素顺序原样保留；
   排序是独立开关，且作用于副本，绝不原地修改。
2. **数字字面量原样输出**：``1.230``、``1e5``、超大整数都不会被改写。
3. **迭代式序列化**：显式工作栈，深层嵌套不会触发 Python 递归上限。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, List, Optional, Tuple

from .jsonparser import DEFAULT_MAX_DEPTH, MODE_JSON, ParseResult, parse_json
from .jsonvalue import (
    TYPE_ARRAY,
    TYPE_OBJECT,
    encode_json_string,
    json_type,
    number_literal,
)

INDENT_2 = "  "
INDENT_4 = "    "
INDENT_8 = "        "
INDENT_TAB = "\t"

SORT_NONE = "none"
SORT_ALPHA = "alpha"      # 大小写不敏感的字母序
SORT_UNICODE = "unicode"  # 按 Unicode 码点

SORT_MODES = (SORT_NONE, SORT_ALPHA, SORT_UNICODE)

INDENT_CHOICES = {
    "2": INDENT_2,
    "4": INDENT_4,
    "8": INDENT_8,
    "tab": INDENT_TAB,
    "none": "",
}

__all__ = [
    "FormatOptions",
    "FormatResult",
    "INDENT_2",
    "INDENT_4",
    "INDENT_8",
    "INDENT_CHOICES",
    "INDENT_TAB",
    "SORT_ALPHA",
    "SORT_MODES",
    "SORT_NONE",
    "SORT_UNICODE",
    "dumps",
    "format_text",
    "indent_from_choice",
    "json_equal",
    "minify",
    "minify_text",
    "sort_value",
]


@dataclass
class FormatOptions:
    """格式化参数。默认值对应"标准美化、不改变数据"。"""

    indent: str = INDENT_4
    sort_keys: bool = False
    sort_mode: str = SORT_ALPHA
    sort_arrays: bool = False
    sort_recursive: bool = True
    ensure_ascii: bool = False
    space_after_colon: bool = True
    trailing_newline: bool = False
    line_ending: str = "\n"
    max_depth: int = DEFAULT_MAX_DEPTH

    def validated(self) -> "FormatOptions":
        opts = replace(self)
        if opts.sort_mode not in SORT_MODES:
            opts.sort_mode = SORT_ALPHA
        if opts.indent and set(opts.indent) - {" ", "\t"}:
            opts.indent = INDENT_4
        if opts.line_ending not in ("\n", "\r\n"):
            opts.line_ending = "\n"
        if opts.max_depth < 1:
            opts.max_depth = DEFAULT_MAX_DEPTH
        return opts


def indent_from_choice(choice: str) -> str:
    return INDENT_CHOICES.get(str(choice).lower(), INDENT_4)


@dataclass
class FormatResult:
    ok: bool
    output: str = ""
    parse: Optional[ParseResult] = None
    error_message: str = ""
    semantics_preserved: bool = True
    sorted_keys: bool = False
    sorted_arrays: bool = False


# --------------------------------------------------------------------------- #
# 排序
# --------------------------------------------------------------------------- #
def _sort_key(key: str, mode: str):
    if mode == SORT_UNICODE:
        return key
    return (key.lower(), key)


def sort_value(value: Any, mode: str = SORT_ALPHA, *, recursive: bool = True,
               sort_arrays: bool = False, max_depth: int = 1000) -> Any:
    """返回排序后的**副本**；原对象不被修改。

    数组排序会改变元素顺序（属于数据变更），必须显式开启。
    """
    if mode not in SORT_MODES or mode == SORT_NONE:
        return value

    # 用显式栈构造副本，排序和格式化采用同一套顺序规则。
    root = [None]
    stack = [(value, root, 0, 0)]
    while stack:
        node, parent, key, depth = stack.pop()
        if depth > max_depth or not isinstance(node, (dict, list)):
            parent[key] = node
            continue
        if isinstance(node, dict):
            keys = sorted(node, key=lambda k: _sort_key(k, mode))
            target = {k: node[k] for k in keys}
            children = [(node[k], target, k, depth + 1) for k in keys]
        else:
            target = sorted(node, key=_scalar_sort_key) if sort_arrays else list(node)
            children = [(v, target, i, depth + 1) for i, v in enumerate(target)]
        parent[key] = target
        if recursive:
            stack.extend(reversed(children))
    return root[0]


def _scalar_sort_key(value: Any) -> Tuple[int, Any]:
    """数组排序用的稳定键：先按类型分组，再按可比较的值排序。"""
    kind = json_type(value)
    order = {
        "null": 0,
        "boolean": 1,
        "number": 2,
        "string": 3,
        "array": 4,
        "object": 5,
    }[kind]
    if kind == TYPE_OBJECT:
        return (order, sorted(value.keys()))
    if kind == TYPE_ARRAY:
        return (order, len(value))
    if value is None:
        return (order, 0)
    try:
        return (order, value)
    except TypeError:  # pragma: no cover - 防御
        return (order, str(value))


# --------------------------------------------------------------------------- #
# 序列化（迭代式，内存 O(嵌套深度)）
# --------------------------------------------------------------------------- #
class _SerializeFrame:
    __slots__ = ("kind", "keys", "node", "index", "depth")

    def __init__(self, kind: str, node: Any, keys: Optional[List[str]], depth: int) -> None:
        self.kind = kind
        self.node = node
        self.keys = keys
        self.index = 0
        self.depth = depth


def dumps(value: Any, options: Optional[FormatOptions] = None) -> str:
    """把解析结果序列化成 JSON 文本。"""
    opts = (options or FormatOptions()).validated()
    indent = opts.indent
    pretty = bool(indent)
    newline = opts.line_ending if pretty else ""
    colon_sep = ":" + (" " if (pretty and opts.space_after_colon) else "")
    ensure_ascii = opts.ensure_ascii
    do_sort = opts.sort_keys and opts.sort_mode != SORT_NONE

    out: List[str] = []
    stack: List[_SerializeFrame] = []
    node = value

    while True:
        # ---------------- 下降：打开一个容器，或直接输出标量 ----------------
        kind = json_type(node)
        if kind == TYPE_OBJECT:
            keys = list(node.keys())
            if do_sort and (opts.sort_recursive or not stack):
                keys.sort(key=lambda k: _sort_key(k, opts.sort_mode))
            out.append("{")
            if not keys:
                out.append("}")
            else:
                stack.append(_SerializeFrame(TYPE_OBJECT, node, keys, len(stack)))
                out.append(_prefix(newline, indent, len(stack), colon_sep,
                                   keys[0], ensure_ascii))
                node = node[keys[0]]
                continue
        elif kind == TYPE_ARRAY:
            items = node
            if do_sort and opts.sort_arrays and (opts.sort_recursive or not stack):
                items = sorted(items, key=_scalar_sort_key)
            out.append("[")
            if not items:
                out.append("]")
            else:
                stack.append(_SerializeFrame(TYPE_ARRAY, items, None, len(stack)))
                out.append(_prefix(newline, indent, len(stack), colon_sep,
                                   None, ensure_ascii))
                node = items[0]
                continue
        else:
            out.append(_scalar_text(node, kind, ensure_ascii))

        # ---------------- 回溯：推进兄弟节点或关闭容器 ----------------
        while True:
            if not stack:
                text = "".join(out)
                if opts.trailing_newline and not text.endswith(opts.line_ending):
                    text += opts.line_ending
                return text
            frame = stack[-1]
            frame.index += 1
            if frame.index < _frame_len(frame):
                is_object = frame.kind == TYPE_OBJECT
                key = frame.keys[frame.index] if is_object else None
                out.append(",")
                out.append(_prefix(newline, indent, len(stack), colon_sep, key,
                                   ensure_ascii))
                node = frame.node[key] if is_object else frame.node[frame.index]
                break
            out.append(_closer(newline, indent, frame.depth, frame.kind))
            stack.pop()


def _frame_len(frame: _SerializeFrame) -> int:
    return len(frame.keys) if frame.kind == TYPE_OBJECT else len(frame.node)


def _prefix(newline: str, indent: str, depth: int, colon_sep: str,
            key: Optional[str], ensure_ascii: bool) -> str:
    """容器内某个子节点的前缀：换行 + 缩进 + （对象时的）"key":。"""
    if not newline:  # 紧凑模式
        return "" if key is None else encode_json_string(key, ensure_ascii) + colon_sep
    pad = indent * depth
    if key is None:
        return newline + pad
    return newline + pad + encode_json_string(key, ensure_ascii) + colon_sep


def _closer(newline: str, indent: str, depth: int, kind: str) -> str:
    bracket = "}" if kind == TYPE_OBJECT else "]"
    if not newline:
        return bracket
    return newline + indent * depth + bracket


def _scalar_text(value: Any, value_type: str, ensure_ascii: bool) -> str:
    if value_type == "string":
        return encode_json_string(value, ensure_ascii)
    if value_type == "boolean":
        return "true" if value else "false"
    if value_type == "null":
        return "null"
    return number_literal(value)


def minify(value: Any, options: Optional[FormatOptions] = None) -> str:
    opts = FormatOptions(indent="")
    if options is not None:
        opts = replace(
            opts,
            sort_keys=options.sort_keys,
            sort_mode=options.sort_mode,
            sort_arrays=options.sort_arrays,
            sort_recursive=options.sort_recursive,
            ensure_ascii=options.ensure_ascii,
            trailing_newline=options.trailing_newline,
            line_ending=options.line_ending,
        )
    return dumps(value, opts)


# --------------------------------------------------------------------------- #
# 文本入口
# --------------------------------------------------------------------------- #
def format_text(text: str, options: Optional[FormatOptions] = None,
                mode: str = MODE_JSON, *, verify: bool = True,
                progress=None, cancel=None) -> FormatResult:
    """解析 + 格式化一步完成；``verify`` 会回读结果并比对语义一致性。"""
    opts = (options or FormatOptions()).validated()
    parsed = parse_json(text, mode, max_depth=opts.max_depth, progress=progress, cancel=cancel)
    if not parsed.ok:
        return FormatResult(ok=False, parse=parsed,
                            error_message=parsed.error.code if parsed.error else "parse_error")
    source = parsed.value
    if parsed.stats.duplicate_key_count:
        return FormatResult(ok=False, parse=parsed, semantics_preserved=False,
                            error_message="duplicate_key")
    output = dumps(source, opts)
    preserved = True
    if verify:
        check = parse_json(output, mode, max_depth=opts.max_depth)
        expected = source
        if opts.sort_keys and opts.sort_arrays and opts.sort_mode != SORT_NONE:
            expected = sort_value(source, opts.sort_mode, recursive=opts.sort_recursive,
                                  sort_arrays=True, max_depth=opts.max_depth)
        preserved = check.ok and json_equal(expected, check.value)
    return FormatResult(
        ok=True,
        output=output,
        parse=parsed,
        semantics_preserved=preserved,
        sorted_keys=bool(opts.sort_keys and opts.sort_mode != SORT_NONE),
        sorted_arrays=bool(opts.sort_keys and opts.sort_arrays),
    )


def minify_text(text: str, options: Optional[FormatOptions] = None,
                mode: str = MODE_JSON, *, verify: bool = True,
                progress=None, cancel=None) -> FormatResult:
    opts = options or FormatOptions()
    compact = replace(opts, indent="")
    return format_text(text, compact, mode, verify=verify, progress=progress, cancel=cancel)


# --------------------------------------------------------------------------- #
# 语义等价比较
# --------------------------------------------------------------------------- #
def json_equal(left: Any, right: Any, *, strict_numbers: bool = True,
               max_nodes: int = 5_000_000) -> bool:
    """迭代式深度比较；``strict_numbers`` 时数字按原始字面量比较。"""
    stack: List[Tuple[Any, Any]] = [(left, right)]
    seen = 0
    while stack:
        a, b = stack.pop()
        seen += 1
        if seen > max_nodes:  # pragma: no cover - 防御
            return False
        type_a, type_b = json_type(a), json_type(b)
        if type_a != type_b:
            return False
        if type_a == TYPE_OBJECT:
            if len(a) != len(b) or a.keys() != b.keys():
                return False
            for key in a:
                stack.append((a[key], b[key]))
        elif type_a == TYPE_ARRAY:
            if len(a) != len(b):
                return False
            stack.extend(zip(a, b))
        elif type_a == "number":
            if strict_numbers:
                if number_literal(a) != number_literal(b):
                    return False
            elif a != b:
                return False
        elif a != b:
            # 字符串/布尔/null 按值比较；不能用 is，两次解析出的等值字符串并非同一对象
            return False
    return True

"""文档统计引擎：一次迭代遍历算出全部计数、深度直方图与体积。

设计要点：

* **单遍迭代**：显式栈展开，绝不递归；1000 层以上嵌套同样安全（递归会直接
  ``RecursionError``，那是产品缺陷）。
* **自底向上算体积**：每个节点的紧凑字节数由子节点累加得到，绝不对每个子树
  重新序列化（否则 10 万节点文档会退化成 O(n²)）。
* **数字精确**：``number_min`` / ``number_max`` / ``number_sum`` 全程使用
  ``int`` / ``Decimal``（在 ``MAX_PREC`` 上下文中相加），绝不转 ``float``。
* **只读**：不修改传入的数据，也不长期持有其内部引用（路径一律是新建元组）。
* **绝不抛异常**：任何输入都返回一个完整的 :class:`Statistics`。

两个"深度"的定义（务必区分）：

* ``depth_histogram`` 统计**每个节点**所在的层级，根节点为第 1 层，标量也算，
  因此 ``sum(depth_histogram.values()) == node_count``。
* ``max_depth`` 统计**容器嵌套层数**，与 :class:`app.core.jsonparser.ParseStats`
  的 ``max_depth`` 完全一致（纯标量根文档为 0）。

``duplicate_key_count`` 的语义也与解析器不同：Python ``dict`` 已经吞掉了同一
对象内的真重复键，所以这里统计的是"同名键在全文中被重复使用的次数"
（``key_count - unique_key_count``）。真重复键请用 :func:`merge_parser_stats`
从 :class:`~app.core.jsonparser.ParseResult` 取权威值。
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation, Overflow, localcontext
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..core.jsonvalue import (
    TYPE_ARRAY,
    TYPE_BOOLEAN,
    TYPE_NUMBER,
    TYPE_OBJECT,
    TYPE_STRING,
    encode_json_string,
    json_type,
    number_literal,
)

#: 单次统计的节点上限，超过后停止展开（防御性保护，正常文档到不了）
MAX_NODES = 20_000_000

#: 十进制精确运算精度（decimal.MAX_PREC 在 C 实现与纯 Python 实现中都存在），
#: 默认精度只有 28 位，超大整数相加会丢精度。
MAX_PREC = Decimal.MAX_PREC if hasattr(Decimal, "MAX_PREC") else 10 ** 9

#: 美化体积使用的缩进（两个空格），与 UI 的"2 空格美化"保持一致
PRETTY_INDENT_WIDTH = 2

_BYTE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")

__all__ = [
    "MAX_NODES",
    "ROW_LABELS",
    "Statistics",
    "compact_size",
    "compute_statistics",
    "format_bytes",
    "iter_container_sizes",
    "merge_parser_stats",
    "path_string",
    "push_top_n",
    "statistics_as_rows",
]


# --------------------------------------------------------------------------- #
# 路径文本（三个引擎模块共用同一份实现；因约定不新建共享文件而各自复制一份，
# 修改时必须同步 statistics.py / scanners.py / generator.py 三处）
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
                if "'" in key:
                    parts.append('["' + key.replace("\\", "\\\\").replace('"', '\\"') + '"]')
                else:
                    parts.append("['" + key.replace("\\", "\\\\") + "']")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# 序列化字节数（紧凑形式，与 jsonformatter.minify 的输出逐字节一致）
# --------------------------------------------------------------------------- #
_NEEDS_ESCAPE = re.compile(r'["\\\x00-\x1f]')


def _text_bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _scalar_bytes(value: Any, kind: str) -> int:
    """标量节点在紧凑 JSON 中占用的字节数。"""
    if kind == TYPE_STRING:
        text = value if isinstance(value, str) else str(value)
        if _NEEDS_ESCAPE.search(text) is None:
            return _text_bytes(text) + 2  # 两个引号
        return _text_bytes(encode_json_string(text))
    if kind == TYPE_NUMBER:
        return len(_number_text(value))
    if kind == TYPE_BOOLEAN:
        return 4 if value else 5
    return 4  # null


def _number_text(value: Any) -> str:
    """数字字面量文本；NaN/Infinity 之类非法值退化成 ``repr``，绝不抛异常。"""
    try:
        return number_literal(value)
    except (ValueError, TypeError, ArithmeticError):
        try:
            return repr(value)
        except Exception:  # pragma: no cover - 防御
            return "null"


def _is_finite(value: Any) -> bool:
    """数字能否参与 min/max/sum：NaN / Infinity 一律排除。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, Decimal):
        return value.is_finite()
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf"))
    return False


def _key_bytes(key: Any) -> int:
    text = key if isinstance(key, str) else str(key)
    return _text_bytes(encode_json_string(text))


def _exact_sum(int_part: int, dec_part: Optional[Decimal]) -> Any:
    """把整数累加值与小数累加值合并成精确结果（int 或 Decimal）。

    必须在 ``MAX_PREC`` 上下文中相加：默认 28 位精度下
    ``Decimal("1.5") + 10**30`` 会被悄悄舍入。
    """
    if dec_part is None:
        return int_part
    if not int_part:
        return dec_part
    with localcontext() as ctx:
        ctx.prec = MAX_PREC
        return dec_part + Decimal(int_part)


class _SizeFrame:
    """体积遍历栈帧。"""

    __slots__ = ("kind", "items", "index", "total", "seq", "path_len")

    def __init__(self, kind: str, items: Any, seq: int, path_len: int) -> None:
        self.kind = kind
        self.items = items
        self.index = 0
        self.total = 0
        self.seq = seq
        self.path_len = path_len


def compact_size(value: Any) -> int:
    """``value`` 紧凑序列化后的 UTF-8 字节数，等价 ``len(minify(value).encode())``。"""
    frames: List[_SizeFrame] = []
    node = value
    size = 0
    while True:
        kind = json_type(node)
        if kind == TYPE_OBJECT:
            items = list(node.items())
            if not items:
                size = 2
            else:
                frames.append(_SizeFrame(TYPE_OBJECT, items, 0, 0))
                node = items[0][1]
                continue
        elif kind == TYPE_ARRAY:
            items = list(node)
            if not items:
                size = 2
            else:
                frames.append(_SizeFrame(TYPE_ARRAY, items, 0, 0))
                node = items[0]
                continue
        else:
            items = ()
            size = _scalar_bytes(node, kind)

        while True:
            if not frames:
                return size
            frame = frames[-1]
            if frame.kind == TYPE_OBJECT:
                frame.total += size + _key_bytes(frame.items[frame.index][0]) + 1
            else:
                frame.total += size
            frame.index += 1
            if frame.index < len(frame.items):
                node = (frame.items[frame.index][1] if frame.kind == TYPE_OBJECT
                        else frame.items[frame.index])
                break
            frames.pop()
            size = frame.total + 2 + (len(frame.items) - 1)


def iter_container_sizes(value: Any) -> Iterator[Tuple[int, Tuple, int, str]]:
    """产出 ``(前序序号, 路径, 紧凑字节数, 类型名)``，只含 object/array 节点。

    产出顺序是**后序**（子树先于父树），``序号`` 是前序遍历的递增编号，调用方
    可用它恢复文档顺序。整棵文档只遍历一次，不会为每个子树重复序列化。
    """
    frames: List[_SizeFrame] = []
    path: List[Any] = []
    node = value
    seq = 0
    finished = False
    while not finished:
        kind = json_type(node)
        own_seq = seq
        seq += 1
        items: Any = ()
        if kind == TYPE_OBJECT:
            items = list(node.items())
            if items:
                frames.append(_SizeFrame(TYPE_OBJECT, items, own_seq, len(path)))
                path.append(items[0][0])
                node = items[0][1]
                continue
            size = 2
        elif kind == TYPE_ARRAY:
            items = list(node)
            if items:
                frames.append(_SizeFrame(TYPE_ARRAY, items, own_seq, len(path)))
                path.append(0)
                node = items[0]
                continue
            size = 2
        else:
            size = _scalar_bytes(node, kind)

        while True:
            if kind in (TYPE_OBJECT, TYPE_ARRAY):
                yield (own_seq, tuple(path), size, kind)
            if not frames:
                finished = True
                break
            frame = frames[-1]
            if frame.kind == TYPE_OBJECT:
                frame.total += size + _key_bytes(frame.items[frame.index][0]) + 1
            else:
                frame.total += size
            frame.index += 1
            if frame.index < len(frame.items):
                path[-1] = (frame.items[frame.index][0] if frame.kind == TYPE_OBJECT
                            else frame.index)
                node = (frame.items[frame.index][1] if frame.kind == TYPE_OBJECT
                        else frame.items[frame.index])
                break
            frames.pop()
            del path[frame.path_len:]
            size = frame.total + 2 + (len(frame.items) - 1)
            kind = frame.kind
            own_seq = frame.seq
            node = None


def push_top_n(items: List[Tuple[int, int, Tuple, str]], item: Tuple[int, int, Tuple, str],
               limit: int) -> None:
    """有界 Top-N：``items`` 保存最大的 ``limit`` 个，``items[0]`` 始终是"最差"的那个。

    ``item`` 形如 ``(size, seq, path, type_name)``，``seq`` 是文档顺序编号。
    排序键是 ``(size, -seq)``：尺寸小的更差，同尺寸时 seq 大（文档中靠后）的更差，
    因此淘汰时优先丢掉靠后的，保留靠前的，输出完全确定。
    scanners.analyze_size 复用同一份实现，保证两处排序口径一致。
    """
    if limit <= 0:
        return
    key = (item[0], -item[1])
    if len(items) < limit:
        items.append(item)
        items.sort(key=lambda entry: (entry[0], -entry[1]))
        return
    worst = items[0]
    if key > (worst[0], -worst[1]):
        items[0] = item
        items.sort(key=lambda entry: (entry[0], -entry[1]))


# --------------------------------------------------------------------------- #
# 统计结果
# --------------------------------------------------------------------------- #
@dataclass
class Statistics:
    """一次统计的全部结果。所有字段都有默认值，便于 UI 构造"空统计"。"""

    node_count: int = 0
    object_count: int = 0
    array_count: int = 0
    string_count: int = 0
    number_count: int = 0
    boolean_count: int = 0
    null_count: int = 0
    key_count: int = 0
    unique_key_count: int = 0
    duplicate_key_count: int = 0
    max_depth: int = 0
    depth_histogram: Dict[int, int] = field(default_factory=dict)
    total_string_chars: int = 0
    longest_string_chars: int = 0
    longest_string_path: Tuple = ()
    max_array_length: int = 0
    max_array_path: Tuple = ()
    total_array_items: int = 0
    byte_size: int = 0
    pretty_size: int = 0
    top_keys: List[Tuple[str, int]] = field(default_factory=list)
    largest_nodes: List[Tuple[str, int, str]] = field(default_factory=list)
    number_min: Any = None
    number_max: Any = None
    number_sum: Any = None
    integer_count: int = 0
    float_count: int = 0
    leaf_ratio: float = 0.0
    elapsed_ms: float = 0.0


class _StatFrame:
    """统计遍历栈帧。"""

    __slots__ = ("kind", "items", "index", "depth", "path_len", "bytes", "pretty", "seq")

    def __init__(self, kind: str, items: Any, depth: int, path_len: int, seq: int) -> None:
        self.kind = kind
        self.items = items
        self.index = 0
        self.depth = depth        # 容器自身所在层（根为 1）
        self.path_len = path_len  # 容器自身路径在 path 栈中的长度
        self.bytes = 0            # 子节点紧凑字节累加（含 "key":）
        self.pretty = 0           # 子节点美化字节累加（含换行/缩进/"key": ）
        self.seq = seq            # 前序序号，用于同尺寸时的确定性排序


def compute_statistics(value: Any, *, top_n: int = 10, largest_n: int = 10,
                       collect_largest: bool = True) -> Statistics:
    """单遍迭代统计 ``value``；任何输入都不会抛异常。

    :param top_n: ``top_keys`` 保留的键名个数（<=0 表示不统计）。
    :param largest_n: ``largest_nodes`` 保留的子树个数（<=0 表示不统计）。
    :param collect_largest: ``False`` 时跳过最大子树收集（超大文档可省内存）。
    """
    started = time.perf_counter()
    top_n = max(0, int(top_n or 0))
    largest_n = max(0, int(largest_n or 0)) if collect_largest else 0

    stats = Statistics()
    histogram: Dict[int, int] = {}
    key_counter: Counter = Counter()
    largest: List[Tuple[int, int, Tuple, str]] = []

    node_count = 0
    seq = 0
    key_count = 0
    total_string_chars = 0
    longest_chars = -1
    longest_path: Tuple = ()
    max_array_length = -1
    max_array_path: Tuple = ()
    total_array_items = 0
    max_depth = 0
    integer_count = 0
    float_count = 0
    int_sum = 0
    dec_sum: Optional[Decimal] = None
    number_min: Any = None
    number_max: Any = None
    has_number = False
    truncated = False

    frames: List[_StatFrame] = []
    path: List[Any] = []
    node: Any = value
    size = 0
    pretty = 0
    done_kind = ""
    finished = False

    with localcontext() as ctx:
        # 十进制默认精度只有 28 位，超大整数相加会丢精度；这里放开到精确。
        ctx.prec = MAX_PREC
        # NaN/Infinity 的比较与加法默认会抛 InvalidOperation；手工构造的输入
        # 可能带这类值，公共 API 不允许抛异常，所以关掉陷阱。
        ctx.traps[InvalidOperation] = False
        ctx.traps[Overflow] = False
        while not finished:
            kind = json_type(node)
            node_count += 1
            own_seq = seq
            seq += 1
            if node_count > MAX_NODES:  # pragma: no cover - 防御
                truncated = True
                finished = True
                break
            depth = len(frames) + 1
            histogram[depth] = histogram.get(depth, 0) + 1
            items: Any = ()

            # ---------------- 下降：容器入栈，标量直接算体积 ----------------
            if kind == TYPE_OBJECT:
                stats.object_count += 1
                max_depth = depth if depth > max_depth else max_depth
                items = list(node.items())
                key_count += len(items)
                if top_n:
                    for key, _ in items:
                        key_counter[key if isinstance(key, str) else str(key)] += 1
                if items:
                    frames.append(_StatFrame(TYPE_OBJECT, items, depth, len(path), own_seq))
                    path.append(items[0][0])
                    node = items[0][1]
                    continue
                size = pretty = 2
            elif kind == TYPE_ARRAY:
                stats.array_count += 1
                max_depth = depth if depth > max_depth else max_depth
                items = list(node)
                length = len(items)
                total_array_items += length
                if length > max_array_length:
                    max_array_length = length
                    max_array_path = tuple(path)
                if items:
                    frames.append(_StatFrame(TYPE_ARRAY, items, depth, len(path), own_seq))
                    path.append(0)
                    node = items[0]
                    continue
                size = pretty = 2
            elif kind == TYPE_STRING:
                stats.string_count += 1
                text = node if isinstance(node, str) else str(node)
                total_string_chars += len(text)
                if len(text) > longest_chars:
                    longest_chars = len(text)
                    longest_path = tuple(path)
                size = pretty = _scalar_bytes(text, TYPE_STRING)
            elif kind == TYPE_NUMBER:
                stats.number_count += 1
                if isinstance(node, int):  # json_type 已排除 bool
                    integer_count += 1
                else:
                    float_count += 1
                # NaN/Infinity 不参与 min/max/sum（否则结果没有意义），但仍然计数
                if _is_finite(node):
                    has_number = True
                    if isinstance(node, int):
                        int_sum += node
                    else:
                        decimal_value = (node if isinstance(node, Decimal)
                                         else Decimal(str(node)))
                        dec_sum = (decimal_value if dec_sum is None
                                   else dec_sum + decimal_value)
                    if number_min is None or node < number_min:
                        number_min = node
                    if number_max is None or node > number_max:
                        number_max = node
                size = pretty = len(_number_text(node))
            elif kind == TYPE_BOOLEAN:
                stats.boolean_count += 1
                size = pretty = 4 if node else 5
            else:
                stats.null_count += 1
                size = pretty = 4
            done_kind = kind

            # ---------------- 回溯：把刚完成节点的体积交给父级 ----------------
            while True:
                if largest_n and done_kind in (TYPE_OBJECT, TYPE_ARRAY):
                    push_top_n(largest, (size, own_seq, tuple(path), done_kind), largest_n)
                if not frames:
                    finished = True
                    break
                frame = frames[-1]
                if frame.kind == TYPE_OBJECT:
                    # 注意：这里累加的是"刚完成的子节点"，所以要在 index 前进之前取键
                    key_size = _key_bytes(frame.items[frame.index][0])
                    frame.bytes += size + key_size + 1                  # + ':'
                    frame.pretty += (pretty + 1 + PRETTY_INDENT_WIDTH * frame.depth
                                     + key_size + 2)                    # 换行+缩进+"key":
                else:
                    frame.bytes += size
                    frame.pretty += pretty + 1 + PRETTY_INDENT_WIDTH * frame.depth
                frame.index += 1
                if frame.index < len(frame.items):
                    if frame.kind == TYPE_OBJECT:
                        path[-1] = frame.items[frame.index][0]
                        node = frame.items[frame.index][1]
                    else:
                        path[-1] = frame.index
                        node = frame.items[frame.index]
                    done_kind = ""
                    break
                frames.pop()
                del path[frame.path_len:]
                count = len(frame.items)
                size = frame.bytes + 2 + (count - 1)
                pretty = frame.pretty + (count - 1) + 3 + PRETTY_INDENT_WIDTH * (frame.depth - 1)
                done_kind = frame.kind
                own_seq = frame.seq
                node = None

    stats.node_count = node_count
    stats.key_count = key_count
    stats.unique_key_count = len(key_counter)
    stats.duplicate_key_count = max(0, key_count - len(key_counter))
    stats.max_depth = max_depth
    stats.depth_histogram = {d: histogram[d] for d in sorted(histogram)}
    stats.total_string_chars = total_string_chars
    stats.longest_string_chars = max(0, longest_chars)
    stats.longest_string_path = longest_path
    stats.max_array_length = max(0, max_array_length)
    stats.max_array_path = max_array_path
    stats.total_array_items = total_array_items
    stats.byte_size = size
    stats.pretty_size = pretty
    stats.integer_count = integer_count
    stats.float_count = float_count
    scalars = stats.string_count + stats.number_count + stats.boolean_count + stats.null_count
    stats.leaf_ratio = (scalars / node_count) if node_count else 0.0
    if top_n and key_counter:
        stats.top_keys = [
            (key, count)
            for key, count in sorted(key_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]
        ]
    if largest:
        largest.sort(key=lambda item: (-item[0], item[1]))
        stats.largest_nodes = [(path_string(p), b, k) for b, _seq, p, k in largest]
    if has_number:
        stats.number_min = number_min
        stats.number_max = number_max
        stats.number_sum = _exact_sum(int_sum, dec_sum)
    stats.elapsed_ms = (time.perf_counter() - started) * 1000.0
    if truncated:  # pragma: no cover - 防御，正常文档触发不到
        stats.depth_histogram[-1] = node_count
    return stats


def merge_parser_stats(stats: Statistics, parse_result: Any) -> Statistics:
    """用解析器的权威计数补全统计结果（返回副本，不修改入参）。

    目前只覆盖 ``duplicate_key_count``：同一对象内的真重复键在 ``dict`` 里已被
    后者覆盖，只有解析器知道原始个数。``parse_result`` 可以是
    :class:`~app.core.jsonparser.ParseResult`，也可以直接是它的 ``.stats``。
    """
    source = getattr(parse_result, "stats", parse_result)
    duplicates = getattr(source, "duplicate_key_count", None)
    if not isinstance(duplicates, int):
        return stats
    return replace(stats, duplicate_key_count=duplicates)


# --------------------------------------------------------------------------- #
# 展示辅助
# --------------------------------------------------------------------------- #
def format_bytes(n: int) -> str:
    """字节数转可读文本：``1023 B`` / ``1.5 KB`` / ``2.3 MB``，一位小数。"""
    try:
        value = float(n)
    except (TypeError, ValueError):
        return "n/a"
    if value != value or value in (float("inf"), float("-inf")):  # NaN / Inf
        return "n/a"
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value < 1024:
        return f"{sign}{int(value)} B"
    index = 0
    while value >= 1024 and index < len(_BYTE_UNITS) - 1:
        value /= 1024.0
        index += 1
    text = f"{value:.1f}"
    if text == "1024.0" and index < len(_BYTE_UNITS) - 1:
        # 1048575 B 四舍五入会得到 "1024.0 KB"，进位到下一档更自然
        value /= 1024.0
        index += 1
        text = f"{value:.1f}"
    return f"{sign}{text} {_BYTE_UNITS[index]}"


#: statistics_as_rows 产出的稳定标签 id（UI 用它们做翻译键，切勿随意改名）
ROW_LABELS: Tuple[str, ...] = (
    "node.count",
    "node.objects",
    "node.arrays",
    "node.strings",
    "node.numbers",
    "node.booleans",
    "node.nulls",
    "node.leaf_ratio",
    "key.total",
    "key.unique",
    "key.reused",
    "key.top",
    "depth.max",
    "depth.histogram",
    "string.chars_total",
    "string.chars_longest",
    "string.longest_path",
    "array.items_total",
    "array.length_max",
    "array.longest_path",
    "number.integer_count",
    "number.float_count",
    "number.min",
    "number.max",
    "number.sum",
    "size.compact",
    "size.pretty",
    "nodes.largest",
    "timing.elapsed_ms",
)

_NA = "n/a"


def _number_text_or_na(value: Any) -> str:
    return _NA if value is None else _number_text(value)


def _pairs_text(pairs: List[Tuple[str, int]]) -> str:
    return ", ".join(f"{key} ({count})" for key, count in pairs) if pairs else _NA


def _largest_text(rows: List[Tuple[str, int, str]]) -> str:
    return ", ".join(f"{p} ({format_bytes(b)}, {k})" for p, b, k in rows) if rows else _NA


def statistics_as_rows(stats: Statistics) -> List[Tuple[str, str]]:
    """把统计结果摊平成 ``[(label_key, value_text)]``，供表格控件直接渲染。

    ``label_key`` 取自 :data:`ROW_LABELS`，是稳定的英文 id（UI 负责翻译）；
    ``value_text`` 是已经格式化好的文本，可直接显示；无值的项统一是 ``n/a``。
    """
    if not isinstance(stats, Statistics):  # pragma: no cover - 防御
        stats = Statistics()
    histogram_text = ", ".join(f"{d}:{c}" for d, c in sorted(stats.depth_histogram.items()))
    return [
        ("node.count", str(stats.node_count)),
        ("node.objects", str(stats.object_count)),
        ("node.arrays", str(stats.array_count)),
        ("node.strings", str(stats.string_count)),
        ("node.numbers", str(stats.number_count)),
        ("node.booleans", str(stats.boolean_count)),
        ("node.nulls", str(stats.null_count)),
        ("node.leaf_ratio", f"{stats.leaf_ratio:.3f}"),
        ("key.total", str(stats.key_count)),
        ("key.unique", str(stats.unique_key_count)),
        ("key.reused", str(stats.duplicate_key_count)),
        ("key.top", _pairs_text(stats.top_keys)),
        ("depth.max", str(stats.max_depth)),
        ("depth.histogram", histogram_text or _NA),
        ("string.chars_total", str(stats.total_string_chars)),
        ("string.chars_longest", str(stats.longest_string_chars)),
        ("string.longest_path",
         path_string(stats.longest_string_path) if stats.longest_string_chars else _NA),
        ("array.items_total", str(stats.total_array_items)),
        ("array.length_max", str(stats.max_array_length)),
        ("array.longest_path",
         path_string(stats.max_array_path) if stats.max_array_length else _NA),
        ("number.integer_count", str(stats.integer_count)),
        ("number.float_count", str(stats.float_count)),
        ("number.min", _number_text_or_na(stats.number_min)),
        ("number.max", _number_text_or_na(stats.number_max)),
        ("number.sum", _number_text_or_na(stats.number_sum)),
        ("size.compact", format_bytes(stats.byte_size)),
        ("size.pretty", format_bytes(stats.pretty_size)),
        ("nodes.largest", _largest_text(stats.largest_nodes)),
        ("timing.elapsed_ms", f"{stats.elapsed_ms:.1f} ms"),
    ]

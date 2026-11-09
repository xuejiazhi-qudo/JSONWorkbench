"""JSONPath 查询引擎（纯 Python，只读，迭代式）。

设计约束（与 ``app.core`` / ``app.engines`` 一致）：

* **只读**：查询过程绝不修改输入文档，也不会拷贝它。
* **不递归遍历用户数据**：递归下降 ``..`` 用显式帧栈展开，1000+ 层嵌套不会
  触发 ``RecursionError``；过滤器表达式求值的递归深度由**表达式自身**的嵌套
  层数决定，编译期就用 ``MAX_EXPR_DEPTH`` 卡死。
* **公共入口不抛异常**：:func:`query` 捕获一切异常并返回
  :class:`QueryResult`；编译期语法错误允许抛 :class:`JsonPathError`，
  但 :func:`query` 会把它转换成 ``ok=False``。
* **惰性 + 限额**：内部全程用生成器串成流水线，命中 ``limit`` 条即停止遍历，
  因此百万节点文档上的 ``$..*`` 也不会把结果一次性堆进内存。
* **错误文本是英文**：形如 ``code: detail``，中文本地化交给 UI 层。

---------------------------------------------------------------------------
语法
---------------------------------------------------------------------------
``$`` 根节点，``@`` 当前节点（仅过滤器内），``.name`` / ``['name']`` /
``["name"]`` 子成员，``.*`` / ``[*]`` 通配，``..`` 递归下降，``[0]`` /
``[-1]`` 下标，``[0,2,'a']`` 并集，``[1:5]`` / ``[::2]`` / ``[::-1]`` 切片，
``[?(...)]`` 过滤器（比较 ``== != < <= > >=``、逻辑 ``&& || !``、括号、
算术 ``+ - * / %``、字符串/数字/``true``/``false``/``null`` 字面量、
正则 ``=~ /re/ims``、函数调用），以及 ``.length()`` 这类函数终止步。

---------------------------------------------------------------------------
语义约定（都是明确定义的，不会抛异常）
---------------------------------------------------------------------------
* **顺序**：结果严格按文档前序（父在子前，对象按插入顺序，数组按下标）。
  递归下降采用"边下降边判定"的走法，因此 ``$..y`` 在
  ``{"x":{"y":1},"y":2}`` 上先给出 ``$.x.y`` 再给出 ``$.y``。
  例外：普通（非递归下降的）负步长切片 ``[::-1]`` 与并集 ``[2,0]`` 按用户写下的
  顺序输出；``$..[::-1]`` 这类递归下降里的切片只做成员判定，因此仍是文档顺序。
* **类型序**：``null < boolean < number < string < array < object``。
  跨类型比较按该序给出确定结果；**同类型**的数组/对象之间没有定义顺序，
  ``<``/``>`` 一律 ``False``，``<=``/``>=`` 只在两者结构相等时成立，
  ``==``/``!=`` 走结构化深比较。任何一侧缺失（nothing）时所有比较都为
  ``False``。
* **数字**：``int`` / :class:`~app.core.jsonvalue.JsonNumber` / ``Decimal``
  按数值比较，``1.0 == 1`` 成立，超大整数保持精确；整数之间用原生 ``int``
  运算，涉及小数时按操作数位数动态提升 ``decimal`` 精度，避免大整数被
  28 位默认精度截断。除零、溢出等一律得到"缺失"，不会抛异常。
* **真值**：空节点列表、``null``、``false`` 为假；其余（含 ``0``、``""``、
  空数组）为真。因此 ``?(@.isbn)`` 是纯粹的"存在性"判断。
* **函数**：作用在"节点列表"上。``length``/``size``/``keys``/``values``/
  ``first``/``last``/``type``/``match``/``search`` 逐节点求值；
  ``count``/``sum``/``avg``/``min``/``max`` 是聚合函数，整个节点列表只产出
  一个值。``count()`` 返回**节点列表长度**（``$.store.book.length()`` 才是
  数组元素个数）。``first()``/``last()`` 在"恰好命中一个容器节点"时返回该
  容器的首/末元素，否则返回节点列表的首/末节点。
  函数产出的 :class:`PathMatch` 复用被作用节点的 ``path``（合成值在文档里
  没有真实位置），因此 ``keys()`` 的多个结果会共享同一个 path。
* **``=~``** 用 ``re.search``（部分匹配），``match()`` 用 ``re.fullmatch``
  （整体匹配），``search()`` 用 ``re.search``。左侧是字符串时直接匹配；是数字 /
  布尔 / ``null`` 时先转成对应的 JSON 文本（``1`` -> ``"1"``）再匹配；容器与
  缺失值一律 ``False``。正则字面量支持 ``i m s x u`` 标志。
* 过滤器作用在标量节点上不产生匹配（不是错误）；路径不存在返回空列表且
  ``ok=True``（也不是错误）。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from functools import lru_cache
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from ..core.jsonvalue import (
    TYPE_ARRAY,
    TYPE_BOOLEAN,
    TYPE_NULL,
    TYPE_NUMBER,
    TYPE_OBJECT,
    TYPE_STRING,
    JsonNumber,
    encode_json_string,
    is_number,
    json_type,
    number_literal,
)

#: :func:`query` 默认最多返回的匹配数
DEFAULT_LIMIT = 100_000
#: 单条路径最多允许的选择器步数（防御性上限，避免生成器链过深）
MAX_STEPS = 256
#: 过滤器表达式最大嵌套深度（括号 / 运算层级）。每一层在解析与求值时都要
#: 占用若干 Python 栈帧，因此这个上限同时挡住了递归下降式的栈溢出。
MAX_EXPR_DEPTH = 40
#: 递归下降去重用的祖先路径集合上限，超出后不再登记（最坏情况出现重复匹配）
ANCESTRY_DEDUP_CAP = 100_000
#: 运行期字符串正则的编译缓存上限
REGEX_CACHE_SIZE = 256

__all__ = [
    "ANCESTRY_DEDUP_CAP",
    "CompiledPath",
    "DEFAULT_LIMIT",
    "EXAMPLES",
    "FUNCTION_NAMES",
    "JsonPathError",
    "MAX_EXPR_DEPTH",
    "MAX_STEPS",
    "MISSING",
    "PathMatch",
    "QueryResult",
    "REGEX_CACHE_SIZE",
    "compare_values",
    "compile_path",
    "compile_regex",
    "deep_equal",
    "parse_path_string",
    "path_to_string",
    "query",
    "resolve_path",
    "truthy",
    "values_equal",
]


# --------------------------------------------------------------------------- #
# 异常与结果对象
# --------------------------------------------------------------------------- #
class JsonPathError(Exception):
    """JSONPath 编译期错误。``message`` 形如 ``code: detail``（英文）。"""

    def __init__(self, message: str, position: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.position = max(0, int(position))
        self.code = code or (message.split(":", 1)[0] if ":" in message else "syntax_error")

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"{self.message} (at position {self.position})"


@dataclass(frozen=True)
class PathMatch:
    """一条命中结果。

    ``path`` 是从根开始的分段元组，例如 ``('store', 'book', 0, 'title')``，
    根本身是 ``()``；``path_string`` 是规范化后的 ``$.store.book[0].title``。
    """

    path: Tuple[Any, ...]
    value: Any
    path_string: str = "$"


@dataclass
class QueryResult:
    """一次查询的结果。``error`` 形如 ``code: detail``，便于 UI 本地化。"""

    ok: bool
    matches: List[PathMatch] = field(default_factory=list)
    error: str = ""
    error_position: int = -1
    truncated: bool = False
    elapsed_ms: float = 0.0

    @property
    def values(self) -> List[Any]:
        return [m.value for m in self.matches]

    @property
    def paths(self) -> List[Tuple[Any, ...]]:
        return [m.path for m in self.matches]


# --------------------------------------------------------------------------- #
# 基础工具：类型序、比较、算术
# --------------------------------------------------------------------------- #
_TYPE_RANK = {
    TYPE_NULL: 0,
    TYPE_BOOLEAN: 1,
    TYPE_NUMBER: 2,
    TYPE_STRING: 3,
    TYPE_ARRAY: 4,
    TYPE_OBJECT: 5,
}


class _Missing:
    """"nothing"——路径没有命中、或运算无定义时的哨兵值。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "<nothing>"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_ASCII_DIGITS = frozenset("0123456789")
_SENTINEL = object()


def _rank(value: Any) -> int:
    return _TYPE_RANK.get(json_type(value), 3)


def _numeric_cmp(left: Any, right: Any) -> Optional[int]:
    """数字比较，返回 -1/0/1；无法比较返回 ``None``（不抛异常）。"""
    try:
        if left < right:
            return -1
        if left > right:
            return 1
        return 0
    except (TypeError, ValueError, ArithmeticError):
        return None


def deep_equal(left: Any, right: Any) -> bool:
    """迭代式结构相等比较；数字按数值比（``1.0 == 1``），字面量写法无关。"""
    stack: List[Tuple[Any, Any]] = [(left, right)]
    while stack:
        a, b = stack.pop()
        ta, tb = json_type(a), json_type(b)
        if _TYPE_RANK.get(ta, 3) != _TYPE_RANK.get(tb, 3):
            return False
        if ta == TYPE_OBJECT:
            if len(a) != len(b):
                return False
            for key in a:
                if key not in b:
                    return False
                stack.append((a[key], b[key]))
        elif ta == TYPE_ARRAY:
            if len(a) != len(b):
                return False
            stack.extend(zip(a, b))
        elif ta == TYPE_NUMBER:
            if _numeric_cmp(a, b) != 0:
                return False
        elif ta == TYPE_NULL:
            continue
        else:
            try:
                if a != b:
                    return False
            except Exception:  # pragma: no cover - 防御
                return False
    return True


def values_equal(left: Any, right: Any) -> bool:
    """过滤器 ``==`` 的语义：跨类型恒为 ``False``，同类型按类型规则比较。"""
    if isinstance(left, _Missing) or isinstance(right, _Missing):
        return False
    if _rank(left) != _rank(right):
        return False
    kind = json_type(left)
    if kind == TYPE_NULL:
        return True
    if kind == TYPE_NUMBER:
        return _numeric_cmp(left, right) == 0
    if kind in (TYPE_ARRAY, TYPE_OBJECT):
        return deep_equal(left, right)
    try:
        return bool(left == right)
    except Exception:  # pragma: no cover - 防御
        return False


def compare_values(left: Any, right: Any) -> Optional[int]:
    """全序比较：跨类型按类型序，同类型的数组/对象无定义 -> ``None``。"""
    if isinstance(left, _Missing) or isinstance(right, _Missing):
        return None
    rl, rr = _rank(left), _rank(right)
    if rl != rr:
        return -1 if rl < rr else 1
    kind = json_type(left)
    if kind == TYPE_NULL:
        return 0
    if kind == TYPE_NUMBER:
        return _numeric_cmp(left, right)
    if kind == TYPE_BOOLEAN:
        return int(bool(left) > bool(right)) - int(bool(left) < bool(right))
    if kind == TYPE_STRING:
        try:
            return (left > right) - (left < right)
        except TypeError:  # pragma: no cover - 防御
            return None
    return None


def _digits(value: Any) -> int:
    """估算十进制位数；刻意不用 ``str()``——超大整数会触发 4300 位转换限制。"""
    if isinstance(value, Decimal):
        try:
            sign, digits, exponent = value.as_tuple()
            return len(digits) + abs(int(exponent))
        except Exception:  # pragma: no cover - 防御
            return 34
    if isinstance(value, int):
        return int(value.bit_length() * 0.30103) + 3
    if isinstance(value, float):
        return 34
    return 34


def _precision_for(left: Any, right: Any) -> int:
    """按操作数位数估算所需的 decimal 精度，避免大整数被默认 28 位截断。"""
    return min(1_000_000, max(34, _digits(left) + _digits(right) + 16))


def arithmetic(op: str, left: Any, right: Any) -> Any:
    """``+ - * / %``；操作数不是数字（或除零/溢出）时返回 :data:`MISSING`。

    ``+`` 额外支持两个字符串拼接。整数之间走原生 ``int`` 运算，保证
    ``10**5000 + 1`` 精确；只要有一侧是小数就切到 ``Decimal`` 并动态提精度。
    """
    if isinstance(left, _Missing) or isinstance(right, _Missing):
        return MISSING
    if op == "+" and isinstance(left, str) and isinstance(right, str):
        return left + right
    if isinstance(left, bool) or isinstance(right, bool):
        return MISSING
    if not is_number(left) or not is_number(right):
        return MISSING
    try:
        if isinstance(left, int) and isinstance(right, int):
            if op == "+":
                return left + right
            if op == "-":
                return left - right
            if op == "*":
                return left * right
            if op == "%":
                return MISSING if right == 0 else left % right
            if op == "/":
                if right == 0:
                    return MISSING
                if left % right == 0:
                    return left // right
                with localcontext() as ctx:
                    ctx.prec = _precision_for(left, right)
                    return ctx.divide(Decimal(left), Decimal(right))
            return MISSING
        dl = left if isinstance(left, Decimal) else Decimal(left)
        dr = right if isinstance(right, Decimal) else Decimal(right)
        with localcontext() as ctx:
            ctx.prec = _precision_for(dl, dr)
            if op == "+":
                return ctx.add(dl, dr)
            if op == "-":
                return ctx.subtract(dl, dr)
            if op == "*":
                return ctx.multiply(dl, dr)
            if op == "/":
                return MISSING if dr == 0 else ctx.divide(dl, dr)
            if op == "%":
                return MISSING if dr == 0 else ctx.remainder(dl, dr)
        return MISSING
    except (ArithmeticError, ValueError, TypeError, OverflowError):
        return MISSING


def unary_minus(value: Any) -> Any:
    if isinstance(value, _Missing) or isinstance(value, bool) or not is_number(value):
        return MISSING
    try:
        return -value
    except (ArithmeticError, ValueError, TypeError):  # pragma: no cover - 防御
        return MISSING


def truthy(value: Any) -> bool:
    """过滤器真值：空节点列表 / ``null`` / ``false`` 为假，其余为真。"""
    if isinstance(value, _Missing):
        return False
    if isinstance(value, _Nodes):
        it = iter(value)
        first = next(it, _SENTINEL)
        if first is _SENTINEL:
            return False
        if next(it, _SENTINEL) is _SENTINEL:
            return truthy(first[1])
        return True
    if value is None or value is False:
        return False
    return True


def as_text(value: Any) -> Optional[str]:
    """把标量转成文本，供正则与 ``type()`` 之外的字符串场景使用。"""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if is_number(value):
        try:
            return number_literal(value)
        except (ValueError, TypeError, ArithmeticError):
            return None
    return None


# --------------------------------------------------------------------------- #
# 正则
# --------------------------------------------------------------------------- #
_REGEX_FLAGS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL,
                "x": re.VERBOSE, "u": re.UNICODE}
_regex_cache: Dict[str, Optional[re.Pattern]] = {}


def compile_regex(pattern: str, flags: int = 0) -> Optional[re.Pattern]:
    """编译正则；失败返回 ``None``（绝不抛异常）。字符串形式带缓存。"""
    if flags:
        key = f"{flags}\x00{pattern}"
    else:
        key = pattern
    if key in _regex_cache:
        return _regex_cache[key]
    try:
        compiled: Optional[re.Pattern] = re.compile(pattern, flags)
    except (re.error, ValueError, TypeError):
        compiled = None
    if len(_regex_cache) >= REGEX_CACHE_SIZE:
        _regex_cache.clear()
    _regex_cache[key] = compiled
    return compiled


def _as_pattern(value: Any) -> Optional[re.Pattern]:
    if isinstance(value, re.Pattern):
        return value
    if isinstance(value, str):
        return compile_regex(value)
    return None


def regex_hit(subject: Any, pattern: Optional[re.Pattern], full: bool) -> bool:
    """正则匹配；``full=True`` 要求整体匹配（``match()``），否则部分匹配。"""
    if pattern is None:
        return False
    text = as_text(subject)
    if text is None:
        return False
    try:
        return bool(pattern.fullmatch(text) if full else pattern.search(text))
    except (TypeError, ValueError):  # pragma: no cover - 防御
        return False


# --------------------------------------------------------------------------- #
# 节点列表包装
# --------------------------------------------------------------------------- #
class _Nodes:
    """惰性的 ``(path, value)`` 序列，用来和普通 JSON 数组值区分开。"""

    __slots__ = ("source",)

    def __init__(self, source: Iterable[Tuple[Tuple, Any]]) -> None:
        self.source = source

    def __iter__(self) -> Iterator[Tuple[Tuple, Any]]:
        return iter(self.source)


def collapse(result: Any) -> Any:
    """把表达式结果收敛成单个值：0 个 -> MISSING，1 个 -> 值，多个 -> list。"""
    if not isinstance(result, _Nodes):
        return result
    values: List[Any] = []
    for _, value in result:
        values.append(value)
    if not values:
        return MISSING
    if len(values) == 1:
        return values[0]
    return values


def _scalar(result: Any) -> Any:
    return collapse(result)


# --------------------------------------------------------------------------- #
# 求值上下文
# --------------------------------------------------------------------------- #
class _Ctx:
    """一次查询的上下文：根节点 + 过滤器里的 ``@`` 绑定。"""

    __slots__ = ("root", "current_path", "current_value")

    def __init__(self, root: Any) -> None:
        self.root = root
        self.current_path: Tuple[Any, ...] = ()
        self.current_value: Any = root


# --------------------------------------------------------------------------- #
# 选择器 / 步骤
# --------------------------------------------------------------------------- #
class _Segment:
    """一个路径步骤。``apply`` 把节点流映射成新的节点流（保持顺序）。

    子类直接实现 :meth:`apply`（每个节点一个 ``for`` 循环），这样整条流水线
    每步只占一个生成器帧；:meth:`select_one` 是给 :class:`_Union` /
    :class:`_Descendant` 用的单节点包装。
    """

    __slots__ = ()

    def apply(self, nodes: Iterable[Tuple[Tuple, Any]],
              ctx: _Ctx) -> Iterator[Tuple[Tuple, Any]]:
        raise NotImplementedError  # pragma: no cover - 抽象方法

    def select_one(self, path: Tuple, node: Any,
                   ctx: _Ctx) -> Iterator[Tuple[Tuple, Any]]:
        return self.apply(((path, node),), ctx)

    def prepare(self, parent: Any) -> Any:
        """递归下降时按父节点预计算的状态（例如切片选中的下标集合）。"""
        return None

    def matches(self, parent: Any, key: Any, child: Any, child_path: Tuple,
                state: Any, ctx: _Ctx) -> bool:
        raise NotImplementedError  # pragma: no cover - 抽象方法


def _as_index(name: str) -> Optional[int]:
    """``"12"`` / ``"-3"`` -> int，其余 ``None``。"""
    if not name:
        return None
    body = name[1:] if name[0] in "+-" else name
    if not body or not all(ch in _ASCII_DIGITS for ch in body):
        return None
    try:
        return int(name)
    except ValueError:  # pragma: no cover - 防御
        return None


class _Child(_Segment):
    """对象成员 ``.name`` / ``['name']``。"""

    __slots__ = ("name", "index")

    def __init__(self, name: str) -> None:
        self.name = name
        self.index = _as_index(name)

    def apply(self, nodes, ctx):
        name, index = self.name, self.index
        for path, node in nodes:
            if isinstance(node, dict):
                if name in node:
                    yield (path + (name,), node[name])
            elif isinstance(node, (list, tuple)) and index is not None:
                total = len(node)
                idx = index if index >= 0 else total + index
                if 0 <= idx < total:
                    yield (path + (idx,), node[idx])

    def prepare(self, parent):
        return self.name

    def matches(self, parent, key, child, child_path, state, ctx):
        if isinstance(parent, dict):
            return key == state
        return self.index is not None and key == (
            self.index if self.index >= 0 else len(parent) + self.index)


class _Index(_Segment):
    """数组下标 ``[0]`` / ``[-1]``；对对象则容忍数字形式的键。"""

    __slots__ = ("index",)

    def __init__(self, index: int) -> None:
        self.index = index

    def apply(self, nodes, ctx):
        index = self.index
        for path, node in nodes:
            if isinstance(node, (list, tuple)):
                total = len(node)
                idx = index if index >= 0 else total + index
                if 0 <= idx < total:
                    yield (path + (idx,), node[idx])
            elif isinstance(node, dict):
                key = str(index)
                if key in node:
                    yield (path + (key,), node[key])

    def prepare(self, parent):
        if isinstance(parent, (list, tuple)):
            total = len(parent)
            idx = self.index if self.index >= 0 else total + self.index
            return idx if 0 <= idx < total else None
        key = str(self.index)
        return key if key in parent else None

    def matches(self, parent, key, child, child_path, state, ctx):
        return state is not None and key == state


def slice_indices(length: int, start: Optional[int], stop: Optional[int],
                  step: Optional[int]) -> range:
    """Python 切片语义（含负步长与越界归一化）。

    返回惰性的 ``range``，因此 ``$[0:1000000]`` 不会先分配一百万个整数。
    """
    return range(*slice(start, stop, step or 1).indices(max(0, length)))


def _slice_bounds(length: int, start: Optional[int], stop: Optional[int],
                  step: Optional[int]) -> Tuple[int, int, int]:
    """归一化后的 ``(start, stop, step)``，用于 O(1) 判定某个下标是否被选中。"""
    return slice(start, stop, step or 1).indices(max(0, length))


def _in_slice(key: int, bounds: Tuple[int, int, int]) -> bool:
    start, stop, step = bounds
    if step > 0:
        return start <= key < stop and (key - start) % step == 0
    return stop < key <= start and (start - key) % (-step) == 0


class _Slice(_Segment):
    """数组切片 ``[1:5]`` / ``[:3]`` / ``[2:]`` / ``[::2]`` / ``[::-1]``。"""

    __slots__ = ("start", "stop", "step")

    def __init__(self, start: Optional[int], stop: Optional[int],
                 step: Optional[int]) -> None:
        self.start = start
        self.stop = stop
        self.step = step

    def apply(self, nodes, ctx):
        start, stop, step = self.start, self.stop, self.step
        for path, node in nodes:
            if isinstance(node, (list, tuple)):
                for idx in slice_indices(len(node), start, stop, step):
                    yield (path + (idx,), node[idx])

    def prepare(self, parent):
        if isinstance(parent, (list, tuple)):
            return _slice_bounds(len(parent), self.start, self.stop, self.step)
        return None

    def matches(self, parent, key, child, child_path, state, ctx):
        return state is not None and isinstance(key, int) and _in_slice(key, state)

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"{self.start}:{self.stop}:{self.step}"


class _Wildcard(_Segment):
    """``*`` / ``[*]``：对象的全部成员值 + 数组的全部元素。"""

    __slots__ = ()

    def apply(self, nodes, ctx):
        for path, node in nodes:
            if isinstance(node, dict):
                for key, child in node.items():
                    yield (path + (key,), child)
            elif isinstance(node, (list, tuple)):
                for idx, child in enumerate(node):
                    yield (path + (idx,), child)

    def matches(self, parent, key, child, child_path, state, ctx):
        return True


class _Union(_Segment):
    """``[0,2,'a']``：按书写顺序拼接各选择器的结果，并按 path 去重。"""

    __slots__ = ("selectors",)

    def __init__(self, selectors: Sequence[_Segment]) -> None:
        self.selectors = tuple(selectors)

    def apply(self, nodes, ctx):
        selectors = self.selectors
        for path, node in nodes:
            seen = set()
            for selector in selectors:
                for item in selector.select_one(path, node, ctx):
                    if item[0] in seen:
                        continue
                    seen.add(item[0])
                    yield item

    def prepare(self, parent):
        return tuple(selector.prepare(parent) for selector in self.selectors)

    def matches(self, parent, key, child, child_path, state, ctx):
        for selector, sub_state in zip(self.selectors, state):
            if selector.matches(parent, key, child, child_path, sub_state, ctx):
                return True
        return False


class _Filter(_Segment):
    """``[?(...)]``：对容器的每个子节点求值谓词。作用在标量上 => 无匹配。"""

    __slots__ = ("expr",)

    def __init__(self, expr: "_Expr") -> None:
        self.expr = expr

    def apply(self, nodes, ctx):
        for path, node in nodes:
            if isinstance(node, dict):
                for key, child in node.items():
                    child_path = path + (key,)
                    if self.test(child, child_path, ctx):
                        yield (child_path, child)
            elif isinstance(node, (list, tuple)):
                for idx, child in enumerate(node):
                    child_path = path + (idx,)
                    if self.test(child, child_path, ctx):
                        yield (child_path, child)

    def test(self, child: Any, child_path: Tuple, ctx: _Ctx) -> bool:
        saved_path, saved_value = ctx.current_path, ctx.current_value
        ctx.current_path, ctx.current_value = child_path, child
        try:
            return truthy(self.expr.eval(ctx))
        except Exception:
            # 过滤器里的任何运行期意外都按"不匹配"处理，绝不打断整次查询。
            return False
        finally:
            ctx.current_path, ctx.current_value = saved_path, saved_value

    def matches(self, parent, key, child, child_path, state, ctx):
        return self.test(child, child_path, ctx)


class _DescendFrame:
    __slots__ = ("node", "path", "it", "state")

    def __init__(self, node: Any, path: Tuple, it: Iterator, state: Any) -> None:
        self.node = node
        self.path = path
        self.it = it
        self.state = state


def _child_iter(node: Any) -> Iterator[Tuple[Any, Any]]:
    if isinstance(node, dict):
        return iter(node.items())
    return iter(enumerate(node))


class _Descendant(_Segment):
    """递归下降 ``..``。

    ``selector`` 为 ``None`` 时是"自身 + 全部后代"；否则对**每个子节点**判定
    选择器，命中即产出，然后继续深入——这样输出天然是文档前序。
    整个下降过程用显式帧栈，不用 Python 递归。
    """

    __slots__ = ("selector",)

    def __init__(self, selector: Optional[_Segment]) -> None:
        self.selector = selector

    def apply(self, nodes, ctx):
        seen: set = set()
        for path, value in nodes:
            if not self._covered(path, seen):
                if len(seen) < ANCESTRY_DEDUP_CAP:
                    seen.add(path)
                yield from self._walk(path, value, ctx)

    def _covered(self, path: Tuple, seen: set) -> bool:
        """输入节点若是已处理节点的后代，则其子树已被覆盖，跳过以免重复。"""
        if not seen:
            return False
        if path in seen:
            return True
        for cut in range(0, len(path)):
            if path[:cut] in seen:
                return True
        return False

    def _walk(self, root_path: Tuple, root_node: Any,
              ctx: _Ctx) -> Iterator[Tuple[Tuple, Any]]:
        selector = self.selector
        if selector is None:
            yield (root_path, root_node)
        if not isinstance(root_node, (dict, list, tuple)) or not len(root_node):
            return
        stack = [_DescendFrame(root_node, root_path, _child_iter(root_node),
                               selector.prepare(root_node) if selector else None)]
        while stack:
            frame = stack[-1]
            item = next(frame.it, _SENTINEL)
            if item is _SENTINEL:
                stack.pop()
                continue
            key, child = item
            child_path = frame.path + (key,)
            if selector is None or selector.matches(
                    frame.node, key, child, child_path, frame.state, ctx):
                yield (child_path, child)
            if isinstance(child, (dict, list, tuple)) and len(child):
                stack.append(_DescendFrame(
                    child, child_path, _child_iter(child),
                    selector.prepare(child) if selector else None))


# --------------------------------------------------------------------------- #
# 过滤器表达式 AST
# --------------------------------------------------------------------------- #
class _Expr:
    __slots__ = ()

    def eval(self, ctx: _Ctx) -> Any:
        raise NotImplementedError  # pragma: no cover - 抽象方法


class _Literal(_Expr):
    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value

    def eval(self, ctx):
        return self.value


class _Regex(_Expr):
    """/pattern/flags 字面量；求值结果是已编译的 ``re.Pattern``。"""

    __slots__ = ("pattern", "source")

    def __init__(self, pattern: Optional[re.Pattern], source: str) -> None:
        self.pattern = pattern
        self.source = source

    def eval(self, ctx):
        return self.pattern if self.pattern is not None else MISSING


class _PathExpr(_Expr):
    """``@.a.b`` / ``$.x[0]``：求值结果是惰性节点列表 :class:`_Nodes`。"""

    __slots__ = ("relative", "steps")

    def __init__(self, relative: bool, steps: Sequence["_Step"]) -> None:
        self.relative = relative
        self.steps = tuple(steps)

    def eval(self, ctx):
        if self.relative:
            nodes: Iterable[Tuple[Tuple, Any]] = ((ctx.current_path, ctx.current_value),)
        else:
            nodes = (((), ctx.root),)
        for step in self.steps:
            nodes = step.apply(nodes, ctx)
        return _Nodes(nodes)


class _Not(_Expr):
    __slots__ = ("operand",)

    def __init__(self, operand: _Expr) -> None:
        self.operand = operand

    def eval(self, ctx):
        return not truthy(self.operand.eval(ctx))


class _Logical(_Expr):
    __slots__ = ("op", "left", "right")

    def __init__(self, op: str, left: _Expr, right: _Expr) -> None:
        self.op = op
        self.left = left
        self.right = right

    def eval(self, ctx):
        if self.op == "&&":
            if not truthy(self.left.eval(ctx)):
                return False
            return truthy(self.right.eval(ctx))
        if truthy(self.left.eval(ctx)):
            return True
        return truthy(self.right.eval(ctx))


class _Compare(_Expr):
    __slots__ = ("op", "left", "right")

    def __init__(self, op: str, left: _Expr, right: _Expr) -> None:
        self.op = op
        self.left = left
        self.right = right

    def eval(self, ctx):
        op = self.op
        if op == "=~":
            return regex_hit(_scalar(self.left.eval(ctx)),
                             _as_pattern(_scalar(self.right.eval(ctx))), full=False)
        left = _scalar(self.left.eval(ctx))
        right = _scalar(self.right.eval(ctx))
        if isinstance(left, _Missing) or isinstance(right, _Missing):
            return False
        if op == "==":
            return values_equal(left, right)
        if op == "!=":
            return not values_equal(left, right)
        order = compare_values(left, right)
        if order is None:
            # 同类型的数组/对象之间没有定义顺序：只有相等时 <= / >= 才成立
            return values_equal(left, right) if op in ("<=", ">=") else False
        if op == "<":
            return order < 0
        if op == "<=":
            return order <= 0
        if op == ">":
            return order > 0
        if op == ">=":
            return order >= 0
        return False  # pragma: no cover - 编译期已拦下未知运算符


class _Binary(_Expr):
    __slots__ = ("op", "left", "right")

    def __init__(self, op: str, left: _Expr, right: _Expr) -> None:
        self.op = op
        self.left = left
        self.right = right

    def eval(self, ctx):
        return arithmetic(self.op, _scalar(self.left.eval(ctx)),
                          _scalar(self.right.eval(ctx)))


class _Neg(_Expr):
    __slots__ = ("operand",)

    def __init__(self, operand: _Expr) -> None:
        self.operand = operand

    def eval(self, ctx):
        return unary_minus(_scalar(self.operand.eval(ctx)))


# --------------------------------------------------------------------------- #
# 函数
# --------------------------------------------------------------------------- #
#: 函数名 -> (最少参数, 最多参数, 实现)。实现是生成器，产出 ``(path, value)``。
_FUNCTIONS: Dict[str, Tuple[int, int, Any]] = {}

#: ``size`` 是 ``length`` 的别名
_ALIASES = {"size": "length"}


def _function(name: str, min_args: int, max_args: int):
    def decorator(func):
        _FUNCTIONS[name] = (min_args, max_args, func)
        return func
    return decorator


def _subject_nodes(call: "_Call", ctx: _Ctx,
                   default_nodes: Optional[Iterable]) -> Iterable[Tuple[Tuple, Any]]:
    """取函数的作用对象：有参数用参数，没参数用"当前节点列表"。"""
    if not call.args:
        if default_nodes is not None:
            return default_nodes
        return ((ctx.current_path, ctx.current_value),)
    result = call.args[0].eval(ctx)
    if isinstance(result, _Nodes):
        return result
    if isinstance(result, _Missing):
        return ()
    return ((ctx.current_path, result),)


def _default_nodes(ctx: _Ctx,
                   default_nodes: Optional[Iterable]) -> Iterable[Tuple[Tuple, Any]]:
    if default_nodes is not None:
        return default_nodes
    return ((ctx.current_path, ctx.current_value),)


def _numeric_items(value: Any) -> Iterator[Any]:
    """聚合函数的候选数字：标量数字本身，或数组里的数字元素。"""
    if is_number(value) and not isinstance(value, bool):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            if is_number(item) and not isinstance(item, bool):
                yield item


def _ordered_items(value: Any) -> Iterator[Any]:
    """``min``/``max`` 的候选：数字与字符串。"""
    if (is_number(value) and not isinstance(value, bool)) or isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            if (is_number(item) and not isinstance(item, bool)) or isinstance(item, str):
                yield item


def _length_of(value: Any) -> Any:
    if isinstance(value, (str, list, tuple, dict)):
        return len(value)
    return MISSING


@_function("length", 0, 1)
def _fn_length(call, ctx, default_nodes):
    for path, value in _subject_nodes(call, ctx, default_nodes):
        size = _length_of(value)
        if not isinstance(size, _Missing):
            yield (path, size)


@_function("count", 0, 1)
def _fn_count(call, ctx, default_nodes):
    """节点列表长度（RFC 9742 语义）。

    过滤器/标量上下文里，空列表给出 ``0``（这样 ``count(@.missing) == 0``
    成立）；作为终止步骤时，输入为空说明前面的路径没有命中，不产出任何结果。
    """
    total = 0
    anchor: Optional[Tuple] = None
    for path, _value in _subject_nodes(call, ctx, default_nodes):
        if total == 0:
            anchor = path
        total += 1
    if anchor is None:
        if default_nodes is None:
            yield (ctx.current_path, 0)
        return
    yield (anchor, total)


@_function("first", 0, 1)
def _fn_first(call, ctx, default_nodes):
    nodes = iter(_subject_nodes(call, ctx, default_nodes))
    head = next(nodes, _SENTINEL)
    if head is _SENTINEL:
        return
    if next(nodes, _SENTINEL) is _SENTINEL:
        path, value = head
        if isinstance(value, (list, tuple)):
            if value:
                yield (path, value[0])
        elif isinstance(value, dict):
            if value:
                yield (path, next(iter(value.values())))
        else:
            yield head
        return
    yield head


@_function("last", 0, 1)
def _fn_last(call, ctx, default_nodes):
    tail = _SENTINEL
    count = 0
    for item in _subject_nodes(call, ctx, default_nodes):
        tail = item
        count += 1
    if tail is _SENTINEL:
        return
    if count == 1:
        path, value = tail
        if isinstance(value, (list, tuple)):
            if value:
                yield (path, value[-1])
        elif isinstance(value, dict):
            tail_value = _SENTINEL
            for child in value.values():
                tail_value = child
            if tail_value is not _SENTINEL:
                yield (path, tail_value)
        else:
            yield tail
        return
    yield tail


@_function("keys", 0, 1)
def _fn_keys(call, ctx, default_nodes):
    for path, value in _subject_nodes(call, ctx, default_nodes):
        if isinstance(value, dict):
            for key in value:
                yield (path, key)
        elif isinstance(value, (list, tuple)):
            for idx in range(len(value)):
                yield (path, idx)


@_function("values", 0, 1)
def _fn_values(call, ctx, default_nodes):
    for path, value in _subject_nodes(call, ctx, default_nodes):
        if isinstance(value, dict):
            for child in value.values():
                yield (path, child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                yield (path, child)


@_function("type", 0, 1)
def _fn_type(call, ctx, default_nodes):
    for path, value in _subject_nodes(call, ctx, default_nodes):
        yield (path, json_type(value))


@_function("min", 0, 1)
def _fn_min(call, ctx, default_nodes):
    best = MISSING
    anchor = ctx.current_path
    for path, value in _subject_nodes(call, ctx, default_nodes):
        for item in _ordered_items(value):
            if isinstance(best, _Missing):
                best, anchor = item, path
                continue
            order = compare_values(item, best)
            if order is not None and order < 0:
                best, anchor = item, path
    if not isinstance(best, _Missing):
        yield (anchor, best)


@_function("max", 0, 1)
def _fn_max(call, ctx, default_nodes):
    best = MISSING
    anchor = ctx.current_path
    for path, value in _subject_nodes(call, ctx, default_nodes):
        for item in _ordered_items(value):
            if isinstance(best, _Missing):
                best, anchor = item, path
                continue
            order = compare_values(item, best)
            if order is not None and order > 0:
                best, anchor = item, path
    if not isinstance(best, _Missing):
        yield (anchor, best)


@_function("sum", 0, 1)
def _fn_sum(call, ctx, default_nodes):
    total = MISSING
    anchor = ctx.current_path
    for path, value in _subject_nodes(call, ctx, default_nodes):
        for item in _numeric_items(value):
            if isinstance(total, _Missing):
                total, anchor = item, path
                continue
            added = arithmetic("+", total, item)
            if not isinstance(added, _Missing):
                total = added
    if not isinstance(total, _Missing):
        yield (anchor, total)


@_function("avg", 0, 1)
def _fn_avg(call, ctx, default_nodes):
    total = MISSING
    count = 0
    anchor = ctx.current_path
    for path, value in _subject_nodes(call, ctx, default_nodes):
        for item in _numeric_items(value):
            if isinstance(total, _Missing):
                total, anchor = item, path
            else:
                added = arithmetic("+", total, item)
                if not isinstance(added, _Missing):
                    total = added
            count += 1
    if isinstance(total, _Missing) or count == 0:
        return
    average = arithmetic("/", total, count)
    if not isinstance(average, _Missing):
        yield (anchor, average)


def _regex_function(full: bool):
    def implementation(call, ctx, default_nodes):
        if len(call.args) >= 2:
            subject = _scalar(call.args[0].eval(ctx))
            pattern = _as_pattern(_scalar(call.args[1].eval(ctx)))
            yield (ctx.current_path, regex_hit(subject, pattern, full))
            return
        pattern = _as_pattern(_scalar(call.args[0].eval(ctx)))
        if pattern is None:
            return
        for path, value in _default_nodes(ctx, default_nodes):
            yield (path, regex_hit(value, pattern, full))
    return implementation


_FUNCTIONS["match"] = (1, 2, _regex_function(True))
_FUNCTIONS["search"] = (1, 2, _regex_function(False))

FUNCTION_NAMES = tuple(sorted(_FUNCTIONS))


class _Call(_Expr):
    """函数调用。求值成节点列表；在标量上下文里再收敛成单个值。"""

    __slots__ = ("name", "args", "position")

    def __init__(self, name: str, args: Sequence[_Expr], position: int) -> None:
        self.name = name
        self.args = tuple(args)
        self.position = position

    def eval_nodes(self, ctx: _Ctx,
                   default_nodes: Optional[Iterable] = None) -> Iterator[Tuple[Tuple, Any]]:
        entry = _FUNCTIONS.get(self.name)
        if entry is None:  # pragma: no cover - 编译期已校验
            return iter(())
        return entry[2](self, ctx, default_nodes)

    def eval(self, ctx):
        return _Nodes(self.eval_nodes(ctx, None))


class _Function(_Segment):
    """函数作为路径的终止步骤，例如 ``$..book.length()``。"""

    __slots__ = ("call",)

    def __init__(self, call: _Call) -> None:
        self.call = call

    def apply(self, nodes, ctx):
        yield from self.call.eval_nodes(ctx, nodes)


_Step = _Segment  # 语义别名：路径里的"一步"


# --------------------------------------------------------------------------- #
# 编译后的路径
# --------------------------------------------------------------------------- #
class CompiledPath:
    """编译好的 JSONPath。无状态、可复用、可跨线程共享。"""

    __slots__ = ("expression", "steps")

    def __init__(self, expression: str, steps: Sequence[_Step]) -> None:
        self.expression = expression
        self.steps = tuple(steps)

    def iter_matches(self, value: Any) -> Iterator[PathMatch]:
        """惰性产出 :class:`PathMatch`，调用方随时可以提前中断。"""
        ctx = _Ctx(value)
        nodes: Iterable[Tuple[Tuple, Any]] = (((), value),)
        for step in self.steps:
            nodes = step.apply(nodes, ctx)
        for path, node in nodes:
            yield PathMatch(path=path, value=node, path_string=path_to_string(path))

    def find(self, value: Any, *, limit: int = DEFAULT_LIMIT) -> List[PathMatch]:
        out: List[PathMatch] = []
        for match in self.iter_matches(value):
            out.append(match)
            if limit and len(out) >= limit:
                break
        return out

    def values(self, value: Any, *, limit: int = DEFAULT_LIMIT) -> List[Any]:
        return [match.value for match in self.find(value, limit=limit)]

    def paths(self, value: Any, *, limit: int = DEFAULT_LIMIT) -> List[Tuple]:
        return [match.path for match in self.find(value, limit=limit)]

    def __str__(self) -> str:
        return self.expression

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"CompiledPath({self.expression!r})"


# --------------------------------------------------------------------------- #
# 表达式解析器
# --------------------------------------------------------------------------- #
_NUMBER_RE = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?")
_INT_RE = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")
_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}
_COMPARE_OPS = ("==", "!=", "<=", ">=", "=~", "<", ">", "=")
_KEYWORDS = {"true": True, "false": False, "null": None}


def _is_name_first(ch: str) -> bool:
    return ch == "_" or ch.isalpha()


def _is_name_char(ch: str) -> bool:
    return ch == "_" or ch.isalnum()


class _Parser:
    """把 JSONPath 文本编译成步骤序列。所有错误都带精确位置。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.n = len(text)
        self.pos = 0
        self.depth = 0

    # ---------------- 基础 ---------------- #
    def error(self, code: str, detail: str, position: Optional[int] = None) -> JsonPathError:
        at = self.pos if position is None else position
        return JsonPathError(f"{code}: {detail}", max(0, min(at, self.n)), code)

    def skip_ws(self) -> None:
        text, n = self.text, self.n
        i = self.pos
        while i < n and text[i] in " \t\r\n":
            i += 1
        self.pos = i

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < self.n else ""

    def read_name(self) -> str:
        text, n = self.text, self.n
        start = self.pos
        i = start
        if i < n and _is_name_first(text[i]):
            i += 1
            while i < n and _is_name_char(text[i]):
                i += 1
        self.pos = i
        return text[start:i]

    # ---------------- 字符串 / 正则 / 数字 ---------------- #
    def read_quoted(self) -> str:
        """读取单引号或双引号字符串（支持反斜杠转义与 ``\\uXXXX``）。"""
        text, n = self.text, self.n
        start = self.pos
        quote = text[start]
        i = start + 1
        parts: List[str] = []
        while True:
            if i >= n:
                raise self.error("unterminated_string",
                                 f"the {quote}-quoted text is never closed", start)
            ch = text[i]
            if ch == quote:
                self.pos = i + 1
                return "".join(parts)
            if ch != "\\":
                parts.append(ch)
                i += 1
                continue
            i += 1
            if i >= n:
                raise self.error("unterminated_string",
                                 f"the {quote}-quoted text is never closed", start)
            esc = text[i]
            if esc in _SIMPLE_ESCAPES:
                parts.append(_SIMPLE_ESCAPES[esc])
                i += 1
                continue
            if esc == "u":
                code, used = self._read_hex4(i + 1, start)
                parts.append(code)
                i = used
                continue
            parts.append(esc)
            i += 1

    def _read_hex4(self, at: int, start: int) -> Tuple[str, int]:
        text, n = self.text, self.n
        hex4 = text[at:at + 4]
        if len(hex4) != 4 or any(c not in "0123456789abcdefABCDEF" for c in hex4):
            raise self.error("invalid_escape",
                             f"\\u must be followed by 4 hex digits, got '\\u{hex4}'", at - 2)
        code = int(hex4, 16)
        at += 4
        if 0xD800 <= code <= 0xDBFF and text[at:at + 2] == "\\u":
            tail = text[at + 2:at + 6]
            if len(tail) == 4 and all(c in "0123456789abcdefABCDEF" for c in tail):
                low = int(tail, 16)
                if 0xDC00 <= low <= 0xDFFF:
                    return chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)), at + 6
            return "\ufffd", at
        if 0xD800 <= code <= 0xDFFF:
            return "\ufffd", at
        return chr(code), at

    def read_regex(self) -> _Regex:
        """/pattern/flags —— 只在主值位置与 ``=~`` 右侧出现。"""
        text, n = self.text, self.n
        start = self.pos
        i = start + 1
        parts: List[str] = []
        while True:
            if i >= n:
                raise self.error("unterminated_regex",
                                 "the regular expression is missing its closing '/'", start)
            ch = text[i]
            if ch == "\n":
                raise self.error("unterminated_regex",
                                 "a regular expression may not span lines", start)
            if ch == "\\":
                parts.append(ch)
                i += 1
                if i >= n:
                    raise self.error("unterminated_regex",
                                     "the regular expression is missing its closing '/'",
                                     start)
                parts.append(text[i])
                i += 1
                continue
            if ch == "/":
                i += 1
                break
            parts.append(ch)
            i += 1
        flags = 0
        flag_chars = ""
        while i < n and text[i].isalpha():
            flag = _REGEX_FLAGS.get(text[i])
            if flag is None:
                raise self.error("invalid_regex_flag",
                                 f"unsupported regex flag '{text[i]}' (use i, m, s, x, u)", i)
            flags |= flag
            flag_chars += text[i]
            i += 1
        self.pos = i
        pattern_text = "".join(parts)
        compiled = compile_regex(pattern_text, flags)
        if compiled is None:
            raise self.error("invalid_regex",
                             f"'/{pattern_text}/{flag_chars}' is not a valid regular "
                             f"expression", start)
        return _Regex(compiled, f"/{pattern_text}/{flag_chars}")

    def read_number(self) -> Any:
        match = _NUMBER_RE.match(self.text, self.pos)
        if not match:
            found = self.peek() or "end of input"
            raise self.error("invalid_number", f"expected a number, found '{found}'")
        raw = match.group(0)
        self.pos = match.end()
        if _INT_RE.match(raw):
            return int(raw)
        try:
            return JsonNumber(raw)
        except (ValueError, ArithmeticError):  # pragma: no cover - 正则已保证合法
            raise self.error("invalid_number", f"'{raw}' is not a valid number")

    def read_optional_int(self) -> Optional[int]:
        self.skip_ws()
        text, n = self.text, self.n
        start = self.pos
        i = start
        if i < n and text[i] in "+-":
            i += 1
        digits = i
        while i < n and text[i] in _ASCII_DIGITS:
            i += 1
        if i == digits:
            self.pos = start
            return None
        self.pos = i
        return int(text[start:i])

    # ---------------- 路径 ---------------- #
    def parse_path(self) -> List[_Step]:
        """解析整条路径（顶层入口）。"""
        self.skip_ws()
        if self.pos < self.n and self.text[self.pos] in "$@":
            self.pos += 1
        steps = self.parse_steps(allow_leading_name=True)
        self.skip_ws()
        if self.pos < self.n:
            raise self.error("unexpected_char",
                             f"unexpected '{self.text[self.pos]}' after a complete path")
        return steps

    def parse_steps(self, allow_leading_name: bool = False) -> List[_Step]:
        steps: List[_Step] = []
        while True:
            self.skip_ws()
            if self.pos >= self.n:
                break
            ch = self.text[self.pos]
            if ch == ".":
                steps.extend(self.parse_dot_step())
            elif ch == "[":
                steps.append(self.parse_bracket_selector())
            elif allow_leading_name and not steps and _is_name_first(ch):
                # 容忍省略根符号的写法，例如 "store.book[0]"
                steps.append(self.parse_name_segment())
            else:
                break
            if len(steps) > MAX_STEPS:
                raise self.error("expression_too_complex",
                                 f"a path may not contain more than {MAX_STEPS} steps", 0)
        return steps

    def parse_dot_step(self) -> List[_Step]:
        if self.text[self.pos + 1: self.pos + 2] == ".":
            self.pos += 2
            return self.parse_descendant()
        self.pos += 1
        self.skip_ws()
        if self.pos >= self.n:
            raise self.error("unexpected_end", "the path ends right after '.'")
        ch = self.text[self.pos]
        if ch == "*":
            self.pos += 1
            return [_Wildcard()]
        if ch == "[":
            return [self.parse_bracket_selector()]
        if ch in "\"'":
            return [_Child(self.read_quoted())]
        if _is_name_first(ch):
            return [self.parse_name_segment()]
        raise self.error("expected_name",
                         f"expected a property name after '.', found '{ch}'", self.pos)

    def parse_name_segment(self) -> _Step:
        at = self.pos
        name = self.read_name()
        if not name:
            raise self.error("expected_name", "expected a property name", at)
        saved = self.pos
        self.skip_ws()
        if self.pos < self.n and self.text[self.pos] == "(":
            return _Function(self.parse_call(name, at))
        self.pos = saved
        return _Child(name)

    def parse_call(self, name: str, at: int) -> _Call:
        canonical = _ALIASES.get(name, name)
        entry = _FUNCTIONS.get(canonical)
        if entry is None:
            known = ", ".join(FUNCTION_NAMES)
            raise self.error("unknown_function",
                             f"unknown function '{name}'; available: {known}", at)
        self.pos += 1  # '('
        args: List[_Expr] = []
        self.skip_ws()
        if self.peek() == ")":
            self.pos += 1
        else:
            while True:
                args.append(self.parse_or())
                self.skip_ws()
                ch = self.peek()
                if ch == ",":
                    self.pos += 1
                    continue
                if ch == ")":
                    self.pos += 1
                    break
                raise self.error("unbalanced_paren",
                                 "expected ',' or ')' inside the argument list of "
                                 f"'{name}()'")
        low, high, _impl = entry
        if not low <= len(args) <= high:
            want = f"{low}" if low == high else f"{low} to {high}"
            raise self.error("bad_arity",
                             f"{name}() takes {want} argument(s) but got {len(args)}", at)
        return _Call(canonical, args, at)

    def parse_descendant(self) -> List[_Step]:
        self.skip_ws()
        if self.pos >= self.n:
            raise self.error("unexpected_end", "the path ends right after '..'")
        ch = self.text[self.pos]
        if ch == "*":
            self.pos += 1
            return [_Descendant(_Wildcard())]
        if ch == "[":
            return [_Descendant(self.parse_bracket_selector())]
        if ch in "\"'":
            return [_Descendant(_Child(self.read_quoted()))]
        if _is_name_first(ch):
            at = self.pos
            name = self.read_name()
            saved = self.pos
            self.skip_ws()
            if self.pos < self.n and self.text[self.pos] == "(":
                # "$..length()"：先取"自身 + 全部后代"，再套函数
                return [_Descendant(None), _Function(self.parse_call(name, at))]
            self.pos = saved
            return [_Descendant(_Child(name))]
        raise self.error("expected_name",
                         f"expected a property name, '*' or '[' after '..', found '{ch}'",
                         self.pos)

    def parse_bracket_selector(self) -> _Segment:
        open_at = self.pos
        self.pos += 1  # '['
        selectors: List[_Segment] = []
        while True:
            selectors.append(self.parse_one_selector())
            self.skip_ws()
            ch = self.peek()
            if ch == ",":
                self.pos += 1
                if self.peek() == "":
                    raise self.error("unbalanced_bracket",
                                     "the '[' is never closed", open_at)
                continue
            break
        if self.pos >= self.n:
            raise self.error("unbalanced_bracket", "the '[' is never closed", open_at)
        if self.text[self.pos] != "]":
            raise self.error("unbalanced_bracket",
                             f"expected ']' but found '{self.text[self.pos]}'", self.pos)
        self.pos += 1
        if len(selectors) == 1:
            return selectors[0]
        return _Union(selectors)

    def parse_one_selector(self) -> _Segment:
        self.skip_ws()
        if self.pos >= self.n:
            raise self.error("unbalanced_bracket", "the '[' is never closed", self.pos)
        ch = self.text[self.pos]
        if ch == "*":
            self.pos += 1
            return _Wildcard()
        if ch == "?":
            at = self.pos
            self.pos += 1
            self.skip_ws()
            paren = self.peek() == "("
            if paren:
                self.pos += 1
            expr = self.parse_or()
            self.skip_ws()
            if paren:
                if self.peek() != ")":
                    raise self.error("unbalanced_paren",
                                     "the filter expression is missing its closing ')'", at)
                self.pos += 1
            return _Filter(expr)
        if ch in "\"'":
            return _Child(self.read_quoted())
        if ch == ":" or ch in "+-" or ch in _ASCII_DIGITS:
            return self.parse_index_or_slice()
        raise self.error("invalid_selector",
                         f"unexpected '{ch}' inside '['; expected '*', '?', a quoted "
                         f"name, an index or a slice", self.pos)

    def parse_index_or_slice(self) -> _Segment:
        at = self.pos
        first = self.read_optional_int()
        self.skip_ws()
        if self.peek() != ":":
            if first is None:
                raise self.error("invalid_selector",
                                 "expected an array index", at)
            return _Index(first)
        self.pos += 1
        stop = self.read_optional_int()
        self.skip_ws()
        step: Optional[int] = None
        if self.peek() == ":":
            self.pos += 1
            step = self.read_optional_int()
        if step == 0:
            raise self.error("invalid_slice", "a slice step may not be 0", at)
        return _Slice(first, stop, step)

    # ---------------- 过滤器表达式 ---------------- #
    def parse_or(self) -> _Expr:
        self.depth += 1
        if self.depth > MAX_EXPR_DEPTH:
            raise self.error("expression_too_complex",
                             f"the filter expression nests deeper than {MAX_EXPR_DEPTH}",
                             self.pos)
        try:
            left = self.parse_and()
            while True:
                self.skip_ws()
                if self.text[self.pos: self.pos + 2] == "||":
                    self.pos += 2
                    left = _Logical("||", left, self.parse_and())
                elif self.text[self.pos: self.pos + 2] == "or" and not _is_name_char(
                        self.text[self.pos + 2: self.pos + 3] or " "):
                    self.pos += 2
                    left = _Logical("||", left, self.parse_and())
                else:
                    return left
        finally:
            self.depth -= 1

    def parse_and(self) -> _Expr:
        left = self.parse_not()
        while True:
            self.skip_ws()
            if self.text[self.pos: self.pos + 2] == "&&":
                self.pos += 2
                left = _Logical("&&", left, self.parse_not())
            elif self.text[self.pos: self.pos + 3] == "and" and not _is_name_char(
                    self.text[self.pos + 3: self.pos + 4] or " "):
                self.pos += 3
                left = _Logical("&&", left, self.parse_not())
            else:
                return left

    def parse_not(self) -> _Expr:
        self.skip_ws()
        if self.peek() == "!" and self.text[self.pos + 1: self.pos + 2] != "=":
            self.pos += 1
            return _Not(self.parse_not())
        return self.parse_comparison()

    def parse_comparison(self) -> _Expr:
        left = self.parse_additive()
        self.skip_ws()
        for op in _COMPARE_OPS:
            if not self.text.startswith(op, self.pos):
                continue
            self.pos += len(op)
            right = self.parse_additive()
            return _Compare("==" if op == "=" else op, left, right)
        return left

    def parse_additive(self) -> _Expr:
        left = self.parse_multiplicative()
        while True:
            self.skip_ws()
            ch = self.peek()
            if ch in "+-":
                self.pos += 1
                left = _Binary(ch, left, self.parse_multiplicative())
            else:
                return left

    def parse_multiplicative(self) -> _Expr:
        left = self.parse_unary()
        while True:
            self.skip_ws()
            ch = self.peek()
            if ch in "*/%":
                self.pos += 1
                left = _Binary(ch, left, self.parse_unary())
            else:
                return left

    def parse_unary(self) -> _Expr:
        self.skip_ws()
        ch = self.peek()
        if ch == "-":
            self.pos += 1
            return _Neg(self.parse_unary())
        if ch == "+":
            self.pos += 1
            return self.parse_unary()
        return self.parse_primary()

    def parse_primary(self) -> _Expr:
        self.skip_ws()
        if self.pos >= self.n:
            raise self.error("unexpected_end", "the filter expression ended too early")
        ch = self.text[self.pos]
        if ch == "(":
            self.pos += 1
            expr = self.parse_or()
            self.skip_ws()
            if self.peek() != ")":
                raise self.error("unbalanced_paren",
                                 "expected ')' to close the group", self.pos)
            self.pos += 1
            return expr
        if ch == "/":
            return self.read_regex()
        if ch in "\"'":
            return _Literal(self.read_quoted())
        if ch in _ASCII_DIGITS:
            return _Literal(self.read_number())
        if ch in "$@":
            relative = ch == "@"
            self.pos += 1
            return _PathExpr(relative, self.parse_steps())
        if _is_name_first(ch):
            at = self.pos
            name = self.read_name()
            if name in _KEYWORDS and not (self.pos < self.n and _is_name_char(self.text[self.pos])):
                return _Literal(_KEYWORDS[name])
            saved = self.pos
            self.skip_ws()
            if self.peek() == "(":
                return self.parse_call(name, at)
            self.pos = saved
            # 裸名字视作 "@.name"（Jayway 风格的容错写法）
            return _PathExpr(True, [_Child(name)])
        raise self.error("invalid_filter",
                         f"unexpected '{ch}' in the filter expression", self.pos)


@lru_cache(maxsize=512)
def _compile_uncached(expression: str) -> CompiledPath:
    parser = _Parser(expression)
    steps = parser.parse_path()
    return CompiledPath(expression, steps)


def compile_path(expression: str) -> CompiledPath:
    """编译 JSONPath 表达式；语法错误抛 :class:`JsonPathError`。

    结果带 LRU 缓存（``CompiledPath`` 无状态，可跨线程复用），UI 每次按键
    重新编译也不会成为瓶颈。
    """
    if not isinstance(expression, str):
        expression = str(expression)
    if not expression.strip():
        return _compile_uncached("$")
    try:
        return _compile_uncached(expression)
    except RecursionError:  # pragma: no cover - 深度已由 MAX_EXPR_DEPTH 限制
        raise JsonPathError("expression_too_complex: the expression nests too deeply",
                            0, "expression_too_complex") from None


# --------------------------------------------------------------------------- #
# 路径字符串
# --------------------------------------------------------------------------- #
def _quote_key(key: str) -> str:
    """规范化键段：默认单引号（RFC 9535）；键本身含单引号时改用双引号形式，
    因为本模块的路径解析器不支持单引号串内的反斜杠转义。"""
    if "'" in key:
        return '["' + key.replace("\\", "\\\\").replace('"', '\\"') + '"]'
    return "['" + key.replace("\\", "\\\\") + "']"


def path_to_string(path: Sequence[Any]) -> str:
    """把路径元组转成规范文本，例如 ``('a', 0)`` -> ``$.a[0]``。

    只有 ASCII 标识符形式的键走 ``.name`` 简写，其余（含空串、带点/空格/引号、
    非 ASCII）一律用 ``$["..."]``，转义规则与 JSON 字符串一致，因此
    :func:`parse_path_string` 可以无损还原。
    """
    if not path:
        return "$"
    out: List[str] = ["$"]
    for segment in path:
        if isinstance(segment, bool):  # bool 是 int 子类，先挡掉
            out.append(_quote_key(str(segment)))
        elif isinstance(segment, int):
            out.append(f"[{segment}]")
        elif isinstance(segment, str):
            if _IDENT_RE.match(segment):
                out.append("." + segment)
            else:
                # 单引号是 RFC 9535 的规范化形式，与 statistics/scanners/generator 保持一致
                out.append(_quote_key(segment))
        else:
            out.append("[" + encode_json_string(str(segment)) + "]")
    return "".join(out)


_STRING_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}


def parse_path_string(text: str) -> Tuple[Any, ...]:
    """:func:`path_to_string` 的逆运算，宽容处理各种写法，绝不抛异常。

    接受 ``$`` / ``@`` / 省略根符号；``.name``、``['name']``、``["name"]``、
    ``[0]``、``[-1]``；``..`` 与 ``*`` 无法用路径元组表达，直接跳过。
    """
    if not isinstance(text, str):
        return ()
    segments: List[Any] = []
    i, n = 0, len(text)
    while i < n and text[i] in " \t\r\n":
        i += 1
    if i < n and text[i] in "$@":
        i += 1
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == ".":
            i += 1
            if i < n and text[i] == ".":
                i += 1
            j = i
            while j < n and text[j] not in ".[":
                j += 1
            name = text[i:j].strip()
            if name and name != "*":
                segments.append(name)
            i = j
            continue
        if ch == "[":
            i += 1
            while i < n and text[i] in " \t\r\n":
                i += 1
            if i < n and text[i] in "\"'":
                quote = text[i]
                i += 1
                parts: List[str] = []
                while i < n:
                    cur = text[i]
                    if cur == "\\" and i + 1 < n:
                        esc = text[i + 1]
                        if esc == "u":
                            hex4 = text[i + 2: i + 6]
                            if len(hex4) == 4 and all(
                                    c in "0123456789abcdefABCDEF" for c in hex4):
                                code = int(hex4, 16)
                                parts.append(chr(code) if code < 0xD800 or code > 0xDFFF
                                             else "\ufffd")
                                i += 6
                                continue
                            parts.append("\ufffd")
                            i += 2
                            continue
                        parts.append(_STRING_ESCAPES.get(esc, esc))
                        i += 2
                        continue
                    if cur == quote:
                        i += 1
                        break
                    parts.append(cur)
                    i += 1
                segments.append("".join(parts))
            else:
                j = i
                while j < n and text[j] != "]":
                    j += 1
                token = text[i:j].strip()
                i = j
                if token and token != "*":
                    try:
                        segments.append(int(token))
                    except ValueError:
                        segments.append(token)
            while i < n and text[i] != "]":
                i += 1
            if i < n:
                i += 1
            continue
        # 裸名字（例如 "a.b" 或者 "$" 后面直接跟名字）
        j = i
        while j < n and text[j] not in ".[":
            j += 1
        name = text[i:j].strip()
        if name and name != "*":
            segments.append(name)
        i = j if j > i else i + 1
    return tuple(segments)


def resolve_path(value: Any, path: Sequence[Any]) -> Tuple[bool, Any]:
    """沿路径取值，返回 ``(found, node)``；迭代实现，深层文档也安全。

    找不到时返回 ``(False, None)``，不抛异常。``path`` 也可以直接传规范化的
    路径文本（内部走 :func:`parse_path_string`）。
    """
    if isinstance(path, str):
        path = parse_path_string(path)
    if not isinstance(path, (tuple, list)):
        return (False, None)
    node = value
    for segment in path:
        if isinstance(segment, bool):
            return (False, None)
        if isinstance(segment, int):
            if isinstance(node, (list, tuple)):
                total = len(node)
                idx = segment if segment >= 0 else total + segment
                if 0 <= idx < total:
                    node = node[idx]
                    continue
                return (False, None)
            if isinstance(node, dict):
                key = str(segment)
                if key in node:
                    node = node[key]
                    continue
            return (False, None)
        if isinstance(segment, str):
            if isinstance(node, dict):
                if segment in node:
                    node = node[segment]
                    continue
                return (False, None)
            if isinstance(node, (list, tuple)):
                idx = _as_index(segment)
                if idx is None:
                    return (False, None)
                total = len(node)
                real = idx if idx >= 0 else total + idx
                if 0 <= real < total:
                    node = node[real]
                    continue
            return (False, None)
        return (False, None)
    return (True, node)


# --------------------------------------------------------------------------- #
# 查询入口
# --------------------------------------------------------------------------- #
def query(value: Any, expression: str, *, limit: int = DEFAULT_LIMIT) -> QueryResult:
    """执行查询。**任何输入都不会抛异常**，失败信息写在 ``result.error``。

    ``limit`` 限制返回的匹配数（默认 100000）；命中上限时 ``truncated=True``。
    ``limit <= 0`` 表示不限额——调用方需要自行确保文档规模可控。
    """
    started = time.perf_counter()
    result = QueryResult(ok=False)

    def finish() -> QueryResult:
        result.elapsed_ms = (time.perf_counter() - started) * 1000.0
        return result

    try:
        compiled = compile_path(expression)
    except JsonPathError as exc:
        result.error = exc.message
        result.error_position = exc.position
        return finish()
    except RecursionError:  # pragma: no cover - 编译期已有深度上限
        result.error = "expression_too_complex: the expression nests too deeply"
        result.error_position = 0
        return finish()
    except Exception as exc:  # pragma: no cover - 防御
        result.error = f"invalid_expression: {type(exc).__name__}"
        result.error_position = 0
        return finish()

    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = DEFAULT_LIMIT

    matches: List[PathMatch] = []
    try:
        if cap > 0:
            for match in compiled.iter_matches(value):
                matches.append(match)
                if len(matches) > cap:
                    result.truncated = True
                    del matches[cap:]
                    break
        else:
            matches.extend(compiled.iter_matches(value))
        result.ok = True
        result.matches = matches
    except RecursionError:
        result.ok = False
        result.matches = []
        result.error = ("document_too_deep: the document nests deeper than the "
                        "query engine can walk")
        result.error_position = -1
    except MemoryError:
        result.ok = False
        result.matches = []
        result.error = "out_of_memory: the query produced too many results"
        result.error_position = -1
    except Exception as exc:
        result.ok = False
        result.matches = []
        # 异常文本里可能夹带用户数据（例如 KeyError 带着中文键名），统一过滤成 ASCII
        detail = "".join(ch if ch.isascii() else "?" for ch in str(exc))
        detail = detail.replace("\n", " ").strip()[:200]
        result.error = f"query_failed: {type(exc).__name__}" + (f": {detail}" if detail else "")
        result.error_position = -1
    return finish()


# --------------------------------------------------------------------------- #
# UI 示例（表达式, 英文说明）
# --------------------------------------------------------------------------- #
EXAMPLES: List[Tuple[str, str]] = [
    ("$", "The whole document (the root node)"),
    ("$.store.book[*].author", "The author of every book in the store"),
    ("$..author", "Every author, at any depth"),
    ("$.store.*", "Everything directly inside store"),
    ("$.store.book[0].title", "The title of the first book"),
    ("$..book[2]", "The third book"),
    ("$..book[-1:]", "The last book"),
    ("$..book[0,1]", "The first two books (union)"),
    ("$..book[:2]", "Books from the start up to index 2"),
    ("$..book[1:3]", "Books 1 and 2"),
    ("$..book[::2]", "Every second book"),
    ("$..book[::-1]", "All books in reverse order"),
    ("$..book[?(@.isbn)]", "Every book that has an ISBN"),
    ("$..book[?(!@.isbn)]", "Every book without an ISBN"),
    ("$..book[?(@.price < 10)]", "Every book cheaper than 10"),
    ("$..book[?(@.price <= $.expensive)]", "Every book that is not expensive"),
    ("$..book[?(@.category == 'fiction' && @.price < 10)]", "Cheap fiction"),
    ("$..book[?(@.category == 'reference' || @.author =~ /Evelyn/)]",
     "Reference books, or any book by Evelyn"),
    ("$..book[?(@.price * 2 < 20 && @.price - 5 > 3)]", "Arithmetic inside a filter"),
    ("$..book[?(@.title =~ /^[A-Z]/i)]", "Books whose title starts with a letter"),
    ("$..book[?(length(@.title) > 20)]", "Books with a long title"),
    ("$..book[?(count(@.reviews[*]) > 1)]", "Books with more than one review"),
    ("$..*", "Every node in the document, in document order"),
    ("$..price", "Every price, at any depth"),
    ("$..['category','price']", "Every category and price member"),
    ("$..book.length()", "How many books there are"),
    ("$..book.count()", "How many nodes the previous step selected"),
    ("$.store.book[0].keys()", "The keys of the first book"),
    ("$.store.book[0].values()", "The values of the first book"),
    ("$..price.min()", "The lowest price"),
    ("$..price.max()", "The highest price"),
    ("$..price.sum()", "The total of all prices"),
    ("$..price.avg()", "The average price"),
    ("$..book.first()", "The first book"),
    ("$..book.last()", "The last book"),
    ("$..book.type()", "The JSON type of every book"),
    ("$..book[?(@.price == min($..price))]", "The cheapest book(s)"),
    ("$..bicycle.color", "The colour of the bicycle"),
]

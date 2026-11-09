"""结构化 JSON 对比引擎（差异树、忽略选项、并排行、报表导出）。

这是"JSON Diff"页面的算法核心：两个解析后的文档 -> 一组带路径的差异项
(:class:`DiffItem`)，UI 用它做差异高亮、忽略选项、"只看变更"视图、复制差异
与报表导出。

设计约束（与 ``app.core`` / ``app.engines.converters`` 一致）：

* **纯 Python**：只用标准库（``difflib`` / ``fnmatch`` / ``decimal`` / ``time``），
  不导入 Qt，不联网。
* **绝不修改输入**：全程只读；:class:`DiffItem` 是 frozen dataclass，
  :func:`changed_only_text` 只新建容器。
* **绝不递归遍历用户数据**：对比、尺寸统计、规范 token、剪枝全部用显式
  队列/栈展开，1000+ 层嵌套不会 ``RecursionError``。
* **公共 API 不抛异常**：任何输入都返回 :class:`DiffResult`，失败原因写在
  ``error``（英文、机器可读，形如 ``code: detail``），可恢复的降级写在
  ``warnings``。
* **确定性**：条目顺序 = 文档顺序（对象按左侧键序、其次右侧新增键序；
  数组按下标序）。同样的输入 + 同样的选项永远得到同样的列表。
* **数字字面量保真**：默认按**字面量**比较（``1.0 != 1``），
  ``ignore_number_format=True`` 时按数值比较（``1.0 == 1``）；
  30 位大整数全程走 ``int`` / ``Decimal``，绝不转 ``float``。

---------------------------------------------------------------------------
比较语义
---------------------------------------------------------------------------
* **对象按键比较**（JSON 对象本身无序）：

  * 左侧有、右侧无 -> ``KIND_REMOVED``；右侧有、左侧无 -> ``KIND_ADDED``；
    两侧都有 -> 递归比较（可能是 ``KIND_CHANGED`` / ``KIND_TYPE_CHANGED``
    或更深层的条目）。
  * **键顺序**：``ignore_key_order=True``（默认）时，"仅键顺序不同"不是数据
    变更；``report_reordered=True`` 时才额外产出 ``KIND_REORDERED`` 条目
    （``left`` / ``right`` 是键序列，``detail`` 说明是哪种容器）。
    ``ignore_key_order=False`` 时顺序被视为有意义，键序不同**总是**产出
    ``KIND_REORDERED``（此时 ``changed=True``），与 ``report_reordered`` 无关。
  * 深层键序差异同样能被找到：只有当子树**严格相等**（含键序、含数组顺序、
    含数字字面量）时才会被快速路径整棵跳过，因此"快速路径命中"意味着
    "不可能有更深的键序差异"。

* **数组**由 ``array_mode`` 决定：

  * ``"index"``（默认）：按下标配对。长度不同时，多出的尾部元素报
    ``KIND_ADDED`` / ``KIND_REMOVED``；配对元素递归比较。
    **因此"中间插入一个元素"会把它后面的所有元素都报成变更**——这是
    下标模式的固有语义，需要"识别插入"时请用 ``"key"`` 或 ``"set"``。
  * ``"key"``：元素按 ``array_key_field``（默认 ``id``）配对。
    左侧独有 -> ``KIND_REMOVED``；右侧独有 -> ``KIND_ADDED``；
    配对成功但内容不同 -> **递归比较**，因此标量元素得到 ``KIND_CHANGED``，
    对象元素得到其内部字段的精确条目（UI 的"变更树"更有用）。
    **路径一律使用左侧下标**（右侧独有的元素没有左侧下标，用右侧下标）。
    键值重复时按出现顺序依次配对；没有该字段（或字段不是标量）的元素
    按"无键元素"在文档顺序内位置配对。
  * ``"set"``：数组当作**多重集**（顺序无关）。只报成员差异：
    左侧多出的出现 -> ``KIND_REMOVED``，右侧多出的出现 -> ``KIND_ADDED``；
    多重集完全相同但顺序不同 -> 一条 ``KIND_REORDERED``（``left``/``right``
    是整个数组）。多重集相同则**不会**产出任何子级条目。

* **类型变更**（string -> number、object -> array 等）报
  ``KIND_TYPE_CHANGED``，并且**绝不**再为该子树产出 added/removed 噪声。

* **字符串**：``ignore_whitespace=True``（默认）时先 ``strip()`` 再把连续
  空白（空格/制表/换行等）折叠成单个空格后比较；``ignore_string_case=True``
  时再做大小写折叠（``casefold()``）。两者都只影响**比较**，
  ``left`` / ``right`` 里保存的仍是原文。

* **数字**：默认按 :func:`~app.core.jsonvalue.number_literal` 的字面量比较；
  ``ignore_number_format=True`` 时按 ``Decimal`` 数值比较，
  所以 ``1.0 == 1``、``1e2 == 100``，而大整数依然精确（``10**30 != 10**30+1``）。

* **``ignore_fields``**（:func:`field_ignored`）支持三种写法，命中即跳过该
  节点**及其整棵子树**：

  1. **纯字段名**：``"id"``、``"secret"`` —— 任意深度的同名键。
  2. **字段名通配**：``"*_at"``、``"tmp*"``、``"?"`` —— 用 :func:`fnmatch.fnmatchcase`
     匹配**路径最后一段**（键名），大小写敏感。
  3. **路径模式**（含 ``$`` 或以 ``.`` 开头）：``"$.meta.*"``、``"$.a.b"``、
     ``"$.items[*].id"`` —— 匹配规范 JSONPath 文本（对象键是标识符时写
     ``$.a.b``，否则写 ``$['weird key']``；数组下标写 ``[0]``）。
     ``*`` 可跨 ``.`` 与 ``[]``（所以 ``$.meta.*`` 覆盖 meta 的全部子孙），
     ``[*]`` 匹配任意下标。
     路径模式**不自动包含子孙**（``$.a.b`` 不会忽略 ``$.a.b.c``），
     需要整棵子树时写 ``$.a.b*``。
     只作用于**字段**（子节点），根文档 ``$`` 本身永不被忽略。

---------------------------------------------------------------------------
条目顺序、汇总与截断
---------------------------------------------------------------------------
* ``items`` 是**文档顺序**（前序遍历，父在子前）；``summary`` 里是
  每种 ``KIND_*`` 的计数 + ``"unchanged"``（被判定相等的**节点**数量，
  快速路径整棵跳过的子树按其节点数一次性计入）+ ``"total"`` + ``"truncated"``。
* ``changed == bool(items)``：没有任何条目就是"无差异"。
* ``max_items`` 生效后停止遍历并置 ``truncated=True``（已产出的条目保留）。
* 嵌套超过 ``MAX_DEPTH``（1000 层）时不再深入，在该处产出一条
  ``KIND_CHANGED`` + ``detail="depth_exceeded"`` 的条目并告警，
  而不是抛 ``RecursionError``。

---------------------------------------------------------------------------
并排行 / 报表 / 只看变更
---------------------------------------------------------------------------
* :func:`aligned_lines` 把两侧美化文本按行对齐（``difflib.SequenceMatcher``），
  产出 ``(left_no, left_text, right_no, right_text, marker)``；
  ``context=0``（默认）**不折叠**任何相等区域，每一行都带真实行号，
  因此对 UI 是无损的（同步滚动与行号映射都成立）；``context>0`` 时把超过
  ``2*context`` 行的相等区折叠成一条 ``marker="context"`` 行（行号指向该区
  首行，两侧行号依然单调不减，但被折叠的行不再出现，**是有损的**）。
* :func:`diff_to_markdown` / :func:`diff_to_html` / :func:`diff_to_json_text`
  是完整的报表生成器（HTML 自包含、内联样式、**所有值都转义**）。
* :func:`changed_only_text` 把文档剪枝成"只含被差异触及的子树"，再美化输出；
  数组下标用 ``null`` 补齐以保持原下标，尾部补位 ``null`` 会被裁掉。

---------------------------------------------------------------------------
限制（UI 需要提示的点）
---------------------------------------------------------------------------
* 报表里的 ``left_text`` / ``right_text`` 对**容器**是紧凑 JSON 文本，
  对**超长字符串**是前若干字符；两者都受 ``VALUE_TEXT_LIMIT``（1200 字符，
  报表函数可用 ``value_limit`` 覆盖）限制并加省略号。
  :attr:`DiffItem.left` / :attr:`DiffItem.right` 始终保存完整值。
* ``array_mode="set"`` 只报多重集成员差异，**不会**报元素内部的变化
  （元素内部变化本身就改变了它的 token，因此表现为一条 added + 一条 removed）。
* ``array_mode="set"`` 里同一 token 出现多次时，只报**多出**的那些出现，
  ``left`` / ``right`` 取该 token 的首次出现值（多重集语义下无法更精确）。
* ``array_mode="key"`` 假定元素是对象；标量元素一律按"无键元素"位置配对。
* ``ignore_whitespace`` 会把 ``"a  b"`` 与 ``"a b"`` 视为相等（连续空白折叠），
  这是有意的；需要逐字符比较时关掉它。
* 路径模式匹配的是规范 JSONPath 文本，因此键名里含 ``.`` 时
  （``{"a.b": 1}`` -> ``$['a.b']``）``$.a.b`` 不会命中，要写 ``$['a.b']``。
* :func:`aligned_lines` 的行标记来自**文本**比较（美化后的两侧文本），
  与语义条目不是一一对应：被 ``ignore_whitespace`` /
  ``ignore_number_format`` 判定相等的差异在文本上仍然不同，会显示成
  ``changed``；反之，两侧文本相同的行一定是 ``equal``。
* :func:`aligned_lines` 单侧超过 ``MAX_ALIGN_LINES`` 行时不再做 LCS 对齐，
  退化成"按行号并排 + 逐行比较"，并给出告警（避免超大文档卡死 UI）。
* 深度上限 ``MAX_DEPTH``（1000 层）与 ``app.engines.converters`` 一致；
  更深的文档会被解析器先挡下（默认 ``max_depth=1000``），
  :func:`diff_text` 内部用 5000 层解析，因此对比深度才是真实上限。
"""

from __future__ import annotations

import difflib
import fnmatch
import html as html_module
import re
import time
from collections import deque
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

from ..core.jsonformatter import INDENT_2, FormatOptions, dumps, minify
from ..core.jsonparser import MODE_JSON, MODE_JSONC, parse_json
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

#: 所有引擎统一支持的最大对比深度（与 converters / statistics 保持一致）
MAX_DEPTH = 1000
#: :func:`diff_text` 解析时放宽的深度上限（对比阶段仍受 ``MAX_DEPTH`` 限制）
PARSE_MAX_DEPTH = 5000
#: 报表里单个值的文本上限，超出后容器写成摘要
VALUE_TEXT_LIMIT = 1200
#: :func:`aligned_lines` 单侧行数上限，超出后退化成"按行号并排"
MAX_ALIGN_LINES = 400_000
#: 内部防御：单次遍历最多访问的节点数（自引用或超大文档时给出明确错误）
MAX_NODES = 5_000_000

KIND_ADDED = "added"
KIND_REMOVED = "removed"
KIND_CHANGED = "changed"
KIND_TYPE_CHANGED = "type_changed"
KIND_REORDERED = "reordered"

#: UI 可以按这些 kind 分组着色
DIFF_KINDS: Tuple[str, ...] = (
    KIND_ADDED,
    KIND_REMOVED,
    KIND_CHANGED,
    KIND_TYPE_CHANGED,
    KIND_REORDERED,
)

#: 数组对比模式
ARRAY_INDEX = "index"
ARRAY_SET = "set"
ARRAY_KEY = "key"
ARRAY_MODES: Tuple[str, ...] = (ARRAY_INDEX, ARRAY_SET, ARRAY_KEY)

#: :func:`aligned_lines` 的行标记
MARK_EQUAL = "equal"
MARK_ADDED = "added"
MARK_REMOVED = "removed"
MARK_CHANGED = "changed"
MARK_CONTEXT = "context"
ALIGN_MARKERS: Tuple[str, ...] = (MARK_EQUAL, MARK_ADDED, MARK_REMOVED, MARK_CHANGED, MARK_CONTEXT)

#: :func:`changed_only_text` 接受的 side
SIDE_LEFT = "left"
SIDE_RIGHT = "right"

_PATH_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_WHITESPACE_RUN = re.compile(r"\s+")

__all__ = [
    "ALIGN_MARKERS",
    "ARRAY_INDEX",
    "ARRAY_KEY",
    "ARRAY_MODES",
    "ARRAY_SET",
    "DIFF_KINDS",
    "DiffItem",
    "DiffOptions",
    "DiffResult",
    "KIND_ADDED",
    "KIND_CHANGED",
    "KIND_REMOVED",
    "KIND_REORDERED",
    "KIND_TYPE_CHANGED",
    "MARK_ADDED",
    "MARK_CHANGED",
    "MARK_CONTEXT",
    "MARK_EQUAL",
    "MARK_REMOVED",
    "MAX_ALIGN_LINES",
    "MAX_DEPTH",
    "MAX_NODES",
    "SIDE_LEFT",
    "SIDE_RIGHT",
    "VALUE_TEXT_LIMIT",
    "aligned_lines",
    "changed_only_text",
    "describe_diff_item",
    "diff_json",
    "diff_text",
    "diff_to_html",
    "diff_to_json_text",
    "diff_to_markdown",
    "field_ignored",
    "path_string",
]


# --------------------------------------------------------------------------- #
# 结果对象
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DiffItem:
    """一条差异。``path`` 是元组（对象键为 ``str``，数组下标为 ``int``）。"""

    path: Tuple
    path_string: str
    kind: str
    left: Any = None
    right: Any = None
    left_type: str = ""
    right_type: str = ""
    depth: int = 0
    detail: str = ""

    def describe(self) -> str:
        """人类可读的英文描述（UI 可直接显示或本地化）。"""
        return describe_diff_item(self)

    @property
    def is_reorder(self) -> bool:
        """``True`` 表示"仅顺序不同"，UI 可以把它渲染成更弱的提示色。"""
        return self.kind == KIND_REORDERED


@dataclass
class DiffOptions:
    """对比选项。默认值对应"忽略键顺序、忽略字符串空白、按字面量比数字"。"""

    ignore_key_order: bool = True
    report_reordered: bool = False
    ignore_fields: Tuple[str, ...] = ()
    ignore_whitespace: bool = True
    ignore_string_case: bool = False
    ignore_number_format: bool = False
    array_mode: str = ARRAY_INDEX
    array_key_field: str = "id"
    max_items: int = 100000


@dataclass
class DiffResult:
    """对比结果。``error`` 形如 ``code: detail``；``warnings`` 是降级提示。"""

    changed: bool
    items: List[DiffItem] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)
    truncated: bool = False
    error: str = ""
    elapsed_ms: float = 0.0
    warnings: List[str] = field(default_factory=list)
    left_nodes: int = 0
    right_nodes: int = 0
    options: Optional[DiffOptions] = None

    def by_kind(self, kind: str) -> List[DiffItem]:
        return [item for item in self.items if item.kind == kind]

    def paths(self) -> List[str]:
        return [item.path_string for item in self.items]


# --------------------------------------------------------------------------- #
# 错误信号与描述表
# --------------------------------------------------------------------------- #
class _DiffError(Exception):
    """内部错误信号；``str(exc)`` 已是 ``code: detail`` 形式。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


_DESCRIBE_TEMPLATES = {
    KIND_ADDED: "added ({type}): {value}",
    KIND_REMOVED: "removed ({type}): {value}",
    KIND_CHANGED: "changed ({type}): {left} -> {right}",
    KIND_TYPE_CHANGED: "type changed: {left_type} {left} -> {right_type} {right}",
    KIND_REORDERED: "reordered ({type}): {left} -> {right}",
}


def describe_diff_item(item: Any) -> str:
    """把 :class:`DiffItem` 渲染成一句英文描述；任何输入都不抛异常。"""
    try:
        kind = getattr(item, "kind", "")
        left = getattr(item, "left", None)
        right = getattr(item, "right", None)
        detail = getattr(item, "detail", "")
        text = _DESCRIBE_TEMPLATES.get(kind, "{kind}: {left} -> {right}").format(
            kind=kind or "difference",
            type=getattr(item, "left_type", "") or getattr(item, "right_type", "") or "value",
            left_type=getattr(item, "left_type", "") or "?",
            right_type=getattr(item, "right_type", "") or "?",
            left=_short_text(left),
            right=_short_text(right),
            value=_short_text(right if kind == KIND_ADDED else left),
        )
        if detail:
            text = f"{text} [{detail}]"
        return text
    except Exception:  # noqa: BLE001 - 描述文本永远不该把异常抛给 UI
        return "difference"


# --------------------------------------------------------------------------- #
# 路径文本（与 scanners / statistics / generator 的约定一致；因约定不新建共享
# 文件而各自复制一份，修改时必须同步四处）
# --------------------------------------------------------------------------- #
def path_string(path: Any) -> str:
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
# 选项规范化 / 忽略字段
# --------------------------------------------------------------------------- #
def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    try:
        return bool(value)
    except Exception:  # noqa: BLE001 - 防御
        return default


def _coerce_options(options: Any) -> Tuple[DiffOptions, List[str]]:
    """把任意 ``options`` 规范化成 :class:`DiffOptions`，并返回降级告警。"""
    warnings: List[str] = []
    if options is None:
        return DiffOptions(), warnings
    if isinstance(options, DiffOptions):
        base = options
    elif isinstance(options, dict):
        known = {f for f in DiffOptions.__dataclass_fields__}
        kwargs = {k: v for k, v in options.items() if k in known}
        unknown = sorted(set(options) - known)
        if unknown:
            warnings.append("unknown_option: " + ", ".join(unknown))
        base = DiffOptions(**kwargs)
    else:
        warnings.append("bad_options: expected DiffOptions or a mapping; defaults were used")
        return DiffOptions(), warnings

    opts = replace(base)
    opts.ignore_key_order = _as_bool(base.ignore_key_order, True)
    opts.report_reordered = _as_bool(base.report_reordered, False)
    opts.ignore_whitespace = _as_bool(base.ignore_whitespace, True)
    opts.ignore_string_case = _as_bool(base.ignore_string_case, False)
    opts.ignore_number_format = _as_bool(base.ignore_number_format, False)
    if isinstance(base.ignore_fields, str):
        opts.ignore_fields = (base.ignore_fields,)
    else:
        try:
            opts.ignore_fields = tuple(str(p) for p in (base.ignore_fields or ()))
        except TypeError:
            warnings.append("bad_ignore_fields: the value is not iterable; it was ignored")
            opts.ignore_fields = ()
    mode = str(base.array_mode or ARRAY_INDEX).strip().lower()
    if mode not in ARRAY_MODES:
        warnings.append(f"bad_array_mode: {base.array_mode!r} is not one of "
                        f"{', '.join(ARRAY_MODES)}; {ARRAY_INDEX!r} was used")
        mode = ARRAY_INDEX
    opts.array_mode = mode
    if not isinstance(base.array_key_field, str) or not base.array_key_field:
        warnings.append("bad_array_key_field: it must be a non-empty field name; 'id' was used")
        opts.array_key_field = "id"
    else:
        opts.array_key_field = base.array_key_field
    try:
        max_items = int(base.max_items)
    except (TypeError, ValueError):
        warnings.append(f"bad_max_items: {base.max_items!r} is not an integer; 100000 was used")
        max_items = 100000
    if max_items <= 0:
        warnings.append(f"bad_max_items: {max_items} is not positive; 1 was used")
        max_items = 1
    opts.max_items = max_items
    return opts, warnings


_PATTERN_CACHE: Dict[str, Optional[re.Pattern]] = {}
_WILDCARD_CHARS = "*?["


def _compiled(pattern: str) -> Optional[re.Pattern]:
    """把 ``fnmatch`` 模式编译成正则并缓存；``None`` 表示无法编译（永不命中）。

    只用于**字段名**模式（``*_at``）；路径模式见 :func:`_path_compiled`。
    """
    if pattern in _PATTERN_CACHE:
        return _PATTERN_CACHE[pattern]
    compiled: Optional[re.Pattern]
    try:
        compiled = re.compile(fnmatch.translate(pattern))
    except (re.error, ValueError, TypeError):  # pragma: no cover - fnmatch 极少失败
        compiled = None
    _cache_put(pattern, compiled)
    return compiled


def _path_compiled(pattern: str) -> Optional[re.Pattern]:
    """把**路径模式**编译成正则并缓存；``None`` 表示无法编译（永不命中）。

    不能直接用 :func:`fnmatch.translate`：``fnmatch`` 把 ``[*]`` 当成"只含 ``*``
    这一个字符的字符类"，于是 ``$.items[*].id`` 永远匹配不到 ``$.items[0].id``。
    这里的规则是：``*`` 匹配任意长度字符（可跨 ``.`` 与 ``[]``）、``?`` 匹配单个
    字符、``[*]`` 匹配任意数组下标、其它字符一律按字面量匹配。
    """
    cache_key = "path:" + pattern
    if cache_key in _PATTERN_CACHE:
        return _PATTERN_CACHE[cache_key]
    out: List[str] = []
    index = 0
    length = len(pattern)
    try:
        while index < length:
            ch = pattern[index]
            if pattern.startswith("[*]", index):
                out.append(r"\[\d+\]")
                index += 3
                continue
            if ch == "*":
                out.append(".*")
            elif ch == "?":
                out.append(".")
            else:
                out.append(re.escape(ch))
            index += 1
        compiled: Optional[re.Pattern] = re.compile("".join(out) + r"\Z")
    except (re.error, ValueError, TypeError):  # pragma: no cover - 防御
        compiled = None
    _cache_put(cache_key, compiled)
    return compiled


def _cache_put(key: str, compiled: Optional[re.Pattern]) -> None:
    if len(_PATTERN_CACHE) > 4096:  # 防御：模式来自用户输入，别无限增长
        _PATTERN_CACHE.clear()
    _PATTERN_CACHE[key] = compiled


def _is_path_pattern(pattern: str) -> bool:
    return pattern.startswith("$") or pattern.startswith(".")


def _matches_name(pattern: str, name: str) -> bool:
    """字段名匹配：纯名字走快速路径，含通配符走 :func:`fnmatch.fnmatchcase`。"""
    if not any(ch in pattern for ch in _WILDCARD_CHARS):
        return pattern == name
    compiled = _compiled(pattern)
    if compiled is None:
        return False
    return compiled.match(name) is not None


def field_ignored(path: Any, options: Any) -> bool:
    """``path`` 指向的字段是否被 ``options.ignore_fields`` 忽略。

    支持三种模式（详见模块 docstring）：纯字段名（任意深度）、字段名通配
    （``*_at``）、路径模式（``$.meta.*`` / ``$.a.b`` / ``$.items[*].id``）。
    根路径 ``()`` / ``$`` 永不被忽略。任何输入都不抛异常。
    """
    try:
        parts = tuple(path)
    except TypeError:
        return False
    if not parts:
        return False
    patterns: Tuple[str, ...]
    if isinstance(options, DiffOptions):
        raw = options.ignore_fields
    elif isinstance(options, dict):
        raw = options.get("ignore_fields", ())
    else:
        raw = getattr(options, "ignore_fields", ()) or ()
    if isinstance(raw, str):
        patterns = (raw,)
    else:
        try:
            patterns = tuple(str(p) for p in (raw or ()))
        except TypeError:
            return False
    if not patterns:
        return False

    text = path_string(parts)
    for pattern in patterns:
        if not pattern:
            continue
        if _is_path_pattern(pattern):
            compiled = _path_compiled(pattern)
            if compiled is not None and compiled.match(text) is not None:
                return True
            continue
        last = parts[-1]
        if isinstance(last, str) and _matches_name(pattern, last):
            return True
    return False


# --------------------------------------------------------------------------- #
# 值文本 / 规范化
# --------------------------------------------------------------------------- #
def _scalar_text(value: Any) -> str:
    """标量 -> JSON 文本（数字走 :func:`number_literal`，字面量原样保留）。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, (int, float, Decimal)):
        try:
            return number_literal(value)
        except (ValueError, TypeError, InvalidOperation, ArithmeticError):
            return "null"  # NaN/Infinity 不是合法 JSON
    return _quote(str(value))


def _quote(text: str) -> str:
    """按 RFC 8259 转义并加双引号（复用 core 的实现，保证与格式化器一致）。"""
    return encode_json_string(text)


def _container_text(value: Any, limit: int = VALUE_TEXT_LIMIT) -> str:
    """容器 -> 紧凑 JSON 文本，超过 ``limit`` 时退化成摘要（绝不抛异常）。"""
    kind = json_type(value)
    try:
        text = minify(value)
    except (RecursionError, ValueError, TypeError, InvalidOperation, ArithmeticError):
        text = ""
    if text:
        if len(text) <= limit:
            return text
        return f"<{kind} {text[:limit]}…>"
    size = 0
    if kind == TYPE_OBJECT:
        size = len(value)
        unit = "keys"
    elif kind == TYPE_ARRAY:
        size = len(value)
        unit = "items"
    else:  # pragma: no cover - 标量不会走到这里
        return _scalar_text(value)
    return f"<{kind}: {size} {unit}>"


def _value_text(value: Any, limit: int = VALUE_TEXT_LIMIT) -> str:
    """任意值 -> 报表用文本（容器紧凑 JSON；超长一律截断）。

    截断只影响**报表文本**；:attr:`DiffItem.left` / :attr:`DiffItem.right`
    里始终是完整的值，UI 要展开细节请用它。
    """
    kind = json_type(value)
    if kind in (TYPE_OBJECT, TYPE_ARRAY):
        return _container_text(value, limit)
    text = _scalar_text(value)
    if limit > 0 and len(text) > limit:
        return text[:limit] + "…"
    return text


def _short_text(value: Any, limit: int = 160) -> str:
    """描述/摘要用的短文本。"""
    text = _value_text(value, limit * 4)
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _path_label(path: Tuple, limit: int = 100) -> str:
    """告警里用的路径文本；深文档的路径可能有几千字符，必须截断。"""
    text = path_string(path)
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2:]
    return f"{head}…{tail}"


def _normalize_string(value: str, opts: DiffOptions) -> str:
    """按选项规范化字符串：去首尾空白 + 折叠连续空白，再按需大小写折叠。"""
    text = value
    if opts.ignore_whitespace:
        # ``\s`` 覆盖空格/制表/换行/回车/换页/垂直制表以及 Unicode 空白
        text = _WHITESPACE_RUN.sub(" ", text).strip()
    if opts.ignore_string_case:
        text = text.casefold()
    return text


def _numeric_token(value: Any) -> str:
    """数值规范化 token：``1.0`` 与 ``1`` 得到同一个串，大整数依然精确。"""
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
    except (ValueError, InvalidOperation, ArithmeticError, TypeError):
        return "?"
    if dec.is_nan():
        return "nan"
    if dec.is_infinite():
        return "-inf" if dec < 0 else "inf"
    try:
        return str(dec.normalize())
    except (InvalidOperation, ArithmeticError):  # pragma: no cover - 防御
        return str(dec)


def _number_equal(left: Any, right: Any, opts: DiffOptions) -> bool:
    if not opts.ignore_number_format:
        try:
            return number_literal(left) == number_literal(right)
        except (ValueError, TypeError, InvalidOperation, ArithmeticError):
            pass
    try:
        a = left if isinstance(left, Decimal) else Decimal(str(left))
        b = right if isinstance(right, Decimal) else Decimal(str(right))
    except (ValueError, InvalidOperation, ArithmeticError, TypeError):
        return _numeric_token(left) == _numeric_token(right)
    try:
        return bool(a == b)
    except (InvalidOperation, ArithmeticError):  # pragma: no cover - 防御
        return _numeric_token(left) == _numeric_token(right)


# --------------------------------------------------------------------------- #
# 尺寸表：一次遍历得到每个节点的子树节点数，供快速路径与集合配对使用
# --------------------------------------------------------------------------- #
def _measure_sizes(value: Any) -> Dict[int, int]:
    """迭代计算 ``id(节点) -> 子树节点总数``（标量为 1，容器为 1 + 子节点之和）。

    两个用途：

    1. :meth:`_Engine._equal_fast` 的**廉价预检**（节点数不同 => 一定不等）
       与遍历预算（一次严格比较恰好弹出"节点数"次，预算必须按节点算，
       只按叶子算会让"1200 层空数组"这种 1 叶子/1200 节点的结构误判为不等）；
    2. ``summary["unchanged"]`` 的计数。
    """
    sizes: Dict[int, int] = {}
    order: List[Any] = []
    stack: List[Any] = [value]
    guard = 0
    while stack:
        node = stack.pop()
        kind = json_type(node)
        if kind not in (TYPE_OBJECT, TYPE_ARRAY):
            continue
        guard += 1
        if guard > MAX_NODES:
            raise _DiffError("value_too_large",
                             f"the document has more than {MAX_NODES} container nodes")
        if id(node) in sizes:  # 手工构造的共享/自引用子树：只登记一次
            continue
        sizes[id(node)] = 0  # 占位，逆序回填时覆盖
        order.append(node)
        if kind == TYPE_OBJECT:
            stack.extend(node.values())
        else:
            stack.extend(node)
    for node in reversed(order):
        total = 1  # 自己也是一个节点
        children: Iterable[Any] = node.values() if isinstance(node, dict) else node
        for child in children:
            total += sizes.get(id(child), 1)
        sizes[id(node)] = total
    return sizes


def _node_count(node: Any, sizes: Dict[int, int]) -> int:
    return sizes.get(id(node), 1)


# --------------------------------------------------------------------------- #
# 规范 token（set / key 模式用）：可哈希、遵循忽略选项、迭代实现
# --------------------------------------------------------------------------- #
def _token(value: Any, opts: DiffOptions, path: Tuple = (),
           sizes: Optional[Dict[int, int]] = None) -> Any:
    """把任意 JSON 值转成可哈希的规范形式。

    * 对象键**排序**（键顺序无关），数组保持顺序（``set`` 模式再由调用方
      按多重集比较）；
    * 字符串按 ``ignore_whitespace`` / ``ignore_string_case`` 规范化；
    * 数字按 ``ignore_number_format`` 选择"字面量"或"数值"；
    * 命中 ``ignore_fields`` 的键**整个丢掉**（含子树）。
    """
    sizes = sizes if sizes is not None else {}
    order: List[Tuple[Any, Tuple]] = []
    memo: Dict[Tuple[int, Tuple], Any] = {}
    stack: List[Tuple[Any, Tuple]] = [(value, path)]
    guard = 0
    while stack:
        node, node_path = stack.pop()
        kind = json_type(node)
        if kind not in (TYPE_OBJECT, TYPE_ARRAY):
            continue
        guard += 1
        if guard > MAX_NODES:
            raise _DiffError("value_too_large", "cannot canonicalize such a large value")
        key = (id(node), node_path)
        if key in memo:
            continue
        memo[key] = None  # 占位
        order.append((node, node_path))
        if kind == TYPE_OBJECT:
            for name in node:
                if not _keep_key(name, node_path, opts):
                    continue
                child_path = node_path + (name if isinstance(name, str) else str(name),)
                stack.append((node[name], child_path))
        else:
            for index, child in enumerate(node):
                stack.append((child, node_path + (index,)))

    for node, node_path in reversed(order):
        key = (id(node), node_path)
        if isinstance(node, dict):
            pairs: List[Tuple[str, Any]] = []
            for name, child in node.items():
                if not _keep_key(name, node_path, opts):
                    continue
                child_path = node_path + (name if isinstance(name, str) else str(name),)
                pairs.append((name if isinstance(name, str) else str(name),
                              _token_of(child, child_path, memo, opts)))
            pairs.sort()
            memo[key] = ("o", tuple(pairs))
        else:
            memo[key] = ("a", tuple(
                _token_of(child, node_path + (index,), memo, opts)
                for index, child in enumerate(node)
            ))
    return _token_of(value, path, memo, opts)


def _token_of(node: Any, node_path: Tuple, memo: Dict[Tuple[int, Tuple], Any],
              opts: DiffOptions) -> Any:
    if isinstance(node, (dict, list)):
        return memo[(id(node), node_path)]
    kind = json_type(node)
    if kind == TYPE_STRING:
        return ("s", _normalize_string(node, opts))
    if kind == TYPE_NUMBER:
        if opts.ignore_number_format:
            return ("n", _numeric_token(node))
        try:
            return ("n", number_literal(node))
        except (ValueError, TypeError, InvalidOperation, ArithmeticError):
            return ("n", "?")
    if kind == TYPE_BOOLEAN:
        return ("b", bool(node))
    return ("z", 0)


def _keep_key(name: Any, parent_path: Tuple, opts: DiffOptions) -> bool:
    key = name if isinstance(name, str) else str(name)
    return not field_ignored(parent_path + (key,), opts)


# --------------------------------------------------------------------------- #
# 对比引擎
# --------------------------------------------------------------------------- #
class _Work:
    """一个待比较的节点对；左右路径分开保存（``key`` 模式下会不同）。"""

    __slots__ = ("left", "right", "path", "right_path", "depth")

    def __init__(self, left: Any, right: Any, path: Tuple, right_path: Tuple, depth: int) -> None:
        self.left = left
        self.right = right
        self.path = path
        self.right_path = right_path
        self.depth = depth

    def run(self, engine: "_Engine", queue: Deque[Any]) -> None:
        engine._compare(self, queue)


class _Pending:
    """一条"已经决定要报、但要按文档顺序排队"的差异。

    容器的 removed / added 子节点如果立刻产出，会插到同层其它子节点的深层
    条目之前，破坏文档顺序；因此它们和递归任务一起进队列。
    """

    __slots__ = ("path", "kind", "left", "right", "left_type", "right_type", "depth", "detail")

    def __init__(self, path: Tuple, kind: str, left: Any, right: Any, left_type: str,
                 right_type: str, depth: int, detail: str = "") -> None:
        self.path = path
        self.kind = kind
        self.left = left
        self.right = right
        self.left_type = left_type
        self.right_type = right_type
        self.depth = depth
        self.detail = detail

    def run(self, engine: "_Engine", queue: Deque[Any]) -> None:
        engine._emit(self.path, self.kind, self.left, self.right, self.left_type,
                     self.right_type, self.depth, self.detail)


class _Engine:
    """一次对比的全部状态；迭代式主循环，绝不递归用户数据。"""

    def __init__(self, opts: DiffOptions, warnings: List[str]) -> None:
        self.opts = opts
        self.warnings = warnings
        self.items: List[DiffItem] = []
        self.counts: Dict[str, int] = {kind: 0 for kind in DIFF_KINDS}
        self.unchanged = 0
        self.truncated = False
        self.sizes_left: Dict[int, int] = {}
        self.sizes_right: Dict[int, int] = {}
        self.budget = max(1, int(opts.max_items))

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def run(self, left: Any, right: Any) -> DiffResult:
        self.sizes_left = _measure_sizes(left)
        self.sizes_right = _measure_sizes(right)
        queue: Deque[Any] = deque()
        queue.append(_Work(left, right, (), (), 0))
        steps = 0
        while queue:
            if len(self.items) >= self.budget:
                self.truncated = True
                break
            steps += 1
            if steps > MAX_NODES:
                # 解析器不会产生自引用数据；只有手工构造的循环结构才会走到这里。
                # 与其无限循环，不如停下来把情况说清楚。
                self.truncated = True
                self._warn_once("value_too_large",
                                f"more than {MAX_NODES} comparison steps were needed; the value "
                                f"may be self-referencing, so the comparison was stopped")
                break
            queue.popleft().run(self, queue)
        return self._build_result()

    def _build_result(self) -> DiffResult:
        summary: Dict[str, Any] = {kind: self.counts.get(kind, 0) for kind in DIFF_KINDS}
        summary["unchanged"] = self.unchanged
        summary["total"] = len(self.items)
        summary["truncated"] = self.truncated
        return DiffResult(
            changed=bool(self.items),
            items=self.items,
            summary=summary,
            truncated=self.truncated,
            elapsed_ms=0.0,
            warnings=list(self.warnings),
            # 节点数由公共入口在 run() 之后回填（那里才知道根对象）
            left_nodes=0,
            right_nodes=0,
            options=self.opts,
        )

    # ------------------------------------------------------------------ #
    # 单对节点
    # ------------------------------------------------------------------ #
    def _compare(self, work: _Work, queue: Deque[Any]) -> None:
        left, right = work.left, work.right
        path, depth = work.path, work.depth
        # 严格相等 => 整棵跳过。放在深度上限之前：完全相同的深文档不该被报成
        # depth_exceeded（相同的子树根本不需要展开）。
        if self._equal_fast(left, right):
            self.unchanged += _node_count(left, self.sizes_left)
            return
        if depth > MAX_DEPTH:
            self._warn_once("depth_exceeded",
                            f"{_path_label(path)} nests deeper than {MAX_DEPTH} levels; "
                            f"the subtree was not compared")
            self._emit(path, KIND_CHANGED, None, None, "", "", depth, detail="depth_exceeded")
            return

        kind_left, kind_right = json_type(left), json_type(right)
        if kind_left != kind_right:
            self._emit(path, KIND_TYPE_CHANGED, left, right, kind_left, kind_right, depth,
                       detail=f"{kind_left} -> {kind_right}")
            return

        if kind_left == TYPE_OBJECT:
            self._compare_object(work, queue)
            return
        if kind_left == TYPE_ARRAY:
            self._compare_array(work, queue)
            return

        # ---- 标量 ----
        if kind_left == TYPE_STRING:
            if _normalize_string(left, self.opts) == _normalize_string(right, self.opts):
                self.unchanged += 1
                return
        elif kind_left == TYPE_NUMBER:
            if _number_equal(left, right, self.opts):
                self.unchanged += 1
                return
        elif left == right:
            self.unchanged += 1
            return
        self._emit(path, KIND_CHANGED, left, right, kind_left, kind_right, depth)

    def _equal_fast(self, left: Any, right: Any) -> bool:
        """**严格**相等快速路径：命中即可整棵跳过（严格相等 => 任何选项下都相等）。

        严格 = 同结构 + **同键序** + 同数组序 + 数字按字面量 + 字符串逐字符。
        因此"仅键顺序不同"的子树**不会**被跳过，深层键序差异仍能被发现。

        这里不能直接用 :func:`app.core.jsonformatter.json_equal`：它对对象比较的是
        ``a.keys() != b.keys()``，而 dict 的 ``KeysView`` 是**集合**视图，
        键顺序不同也会判等，``report_reordered`` 就会失效。
        """
        kind = json_type(left)
        if kind not in (TYPE_OBJECT, TYPE_ARRAY):
            return False  # 标量由 _compare 直接处理
        size = self.sizes_left.get(id(left))
        if size is not None and self.sizes_right.get(id(right)) != size:
            return False  # 节点数不同 => 一定不等（最便宜的预检）
        budget = 4 * (size if size is not None else MAX_NODES) + 64
        stack: List[Tuple[Any, Any]] = [(left, right)]
        seen = 0
        while stack:
            a, b = stack.pop()
            seen += 1
            if seen > budget:  # pragma: no cover - 尺寸表已保证到不了这里
                return False
            kind_a = json_type(a)
            if kind_a != json_type(b):
                return False
            if kind_a == TYPE_OBJECT:
                if len(a) != len(b):
                    return False
                keys_a = list(a.keys())
                if keys_a != list(b.keys()):  # 键序敏感
                    return False
                for key in keys_a:
                    stack.append((a[key], b[key]))
            elif kind_a == TYPE_ARRAY:
                if len(a) != len(b):
                    return False
                stack.extend(zip(a, b))
            elif kind_a == TYPE_NUMBER:
                try:
                    if number_literal(a) != number_literal(b):
                        return False
                except (ValueError, TypeError, InvalidOperation, ArithmeticError):
                    if a != b:  # NaN/Infinity 之类的非法数字：退回值比较
                        return False
            elif a != b:
                return False
        return True

    def _equal_loose(self, left: Any, right: Any, path: Tuple) -> bool:
        """**选项感知**的相等判断（``set`` / ``key`` 模式用）。"""
        size = self.sizes_left.get(id(left))
        if size is not None and self.sizes_right.get(id(right)) != size:
            return False
        try:
            return _token(left, self.opts, path, self.sizes_left) == \
                _token(right, self.opts, path, self.sizes_right)
        except _DiffError:
            raise
        except (RecursionError, ValueError, TypeError, InvalidOperation, ArithmeticError):
            return False  # pragma: no cover - 防御

    # ------------------------------------------------------------------ #
    # 对象
    # ------------------------------------------------------------------ #
    def _compare_object(self, work: _Work, queue: Deque[Any]) -> None:
        left: Dict[Any, Any] = work.left
        right: Dict[Any, Any] = work.right
        path, depth = work.path, work.depth
        left_keys = list(left.keys())
        right_keys = list(right.keys())
        left_names = [k if isinstance(k, str) else str(k) for k in left_keys]
        right_names = [k if isinstance(k, str) else str(k) for k in right_keys]
        if any(not isinstance(k, str) for k in left_keys) or \
                any(not isinstance(k, str) for k in right_keys):
            self._warn_once("non_string_key",
                            f"{_path_label(path)} has non-string object keys; they were compared as text")

        # 右侧键名 -> 原始键对象（同名取首个）
        right_index: Dict[str, Any] = {}
        for original, name in zip(right_keys, right_names):
            if name not in right_index:
                right_index[name] = original
        left_name_set = set(left_names)
        children: List[Any] = []

        # 前序：先报"这个对象自己"的键序差异，再报它的子节点
        order_matters = not self.opts.ignore_key_order
        if (self.opts.report_reordered or order_matters) and left_names != right_names \
                and sorted(left_names) == sorted(right_names):
            self._emit(path, KIND_REORDERED, tuple(left_names), tuple(right_names),
                       TYPE_OBJECT, TYPE_OBJECT, depth, detail="object key order")

        for name, original in zip(left_names, left_keys):
            child_path = path + (name,)
            if field_ignored(child_path, self.opts):
                continue
            if name not in right_index:
                value = left[original]
                children.append(_Pending(child_path, KIND_REMOVED, value, None,
                                         json_type(value), "", depth + 1))
                continue
            right_original = right_index[name]
            children.append(_Work(left[original], right[right_original], child_path,
                                  work.right_path + (name,), depth + 1))

        for name, original in zip(right_names, right_keys):
            if name in left_name_set:
                continue
            if right_index.get(name) is not original:
                continue  # 重名键（手工构造的数据）：只报一次
            child_path = path + (name,)
            if field_ignored(child_path, self.opts):
                continue
            value = right[original]
            children.append(_Pending(child_path, KIND_ADDED, None, value, "",
                                     json_type(value), depth + 1))
        # 推到队首 => 严格前序（父节点的条目后面紧跟它自己的子树）
        queue.extendleft(reversed(children))

    # ------------------------------------------------------------------ #
    # 数组
    # ------------------------------------------------------------------ #
    def _compare_array(self, work: _Work, queue: Deque[Any]) -> None:
        mode = self.opts.array_mode
        if mode == ARRAY_SET:
            self._compare_array_set(work)
        elif mode == ARRAY_KEY:
            self._compare_array_key(work, queue)
        else:
            self._compare_array_index(work, queue)

    def _compare_array_index(self, work: _Work, queue: Deque[Any]) -> None:
        left, right = work.left, work.right
        path, right_path, depth = work.path, work.right_path, work.depth
        shared = min(len(left), len(right))
        children: List[Any] = []
        for index in range(shared):
            child_path = path + (index,)
            if field_ignored(child_path, self.opts):
                continue
            children.append(_Work(left[index], right[index], child_path,
                                  right_path + (index,), depth + 1))
        for index in range(shared, len(left)):
            child_path = path + (index,)
            if field_ignored(child_path, self.opts):
                continue
            value = left[index]
            children.append(_Pending(child_path, KIND_REMOVED, value, None,
                                     json_type(value), "", depth + 1))
        for index in range(shared, len(right)):
            child_path = path + (index,)
            right_child = right_path + (index,)
            if field_ignored(child_path, self.opts) or field_ignored(right_child, self.opts):
                continue
            value = right[index]
            children.append(_Pending(child_path, KIND_ADDED, None, value, "",
                                     json_type(value), depth + 1))
        queue.extendleft(reversed(children))

    def _compare_array_set(self, work: _Work) -> None:
        """``set`` 模式：数组当作多重集，只报成员差异与整体重排。"""
        left, right = work.left, work.right
        path, depth = work.path, work.depth
        left_tokens = [_token(item, self.opts, path + (index,), self.sizes_left)
                       for index, item in enumerate(left)]
        right_tokens = [_token(item, self.opts, path + (index,), self.sizes_right)
                        for index, item in enumerate(right)]

        # 一次遍历同时得到：出现次数、出现下标列表、首次出现顺序
        left_counts, left_positions, left_order = _multiset(left_tokens)
        right_counts, right_positions, right_order = _multiset(right_tokens)

        if left_counts == right_counts:
            # 成员完全相同：严格快速路径没命中就说明只是顺序不同
            if left_tokens != right_tokens:
                self._emit(path, KIND_REORDERED, list(left), list(right),
                           TYPE_ARRAY, TYPE_ARRAY, depth, detail="array order (set mode)")
            else:  # pragma: no cover - 严格相等时不会走到这里
                self.unchanged += _node_count(left, self.sizes_left)
            return

        # 多重集语义下"配对"是任意的：按出现顺序把前 min(左,右) 个视为已配对，
        # 多出来的出现才报 removed / added（确定性且与文档顺序一致）。
        for token in left_order:
            matched = min(left_counts[token], right_counts.get(token, 0))
            for occurrence in left_positions[token][matched:]:
                child_path = path + (occurrence,)
                if field_ignored(child_path, self.opts):
                    continue
                value = left[occurrence]
                self._emit(child_path, KIND_REMOVED, value, None, json_type(value), "", depth + 1)

        for token in right_order:
            matched = min(right_counts[token], left_counts.get(token, 0))
            for occurrence in right_positions[token][matched:]:
                child_path = path + (occurrence,)
                if field_ignored(child_path, self.opts):
                    continue
                value = right[occurrence]
                self._emit(child_path, KIND_ADDED, None, value, "", json_type(value), depth + 1)

    def _compare_array_key(self, work: _Work, queue: Deque[Any]) -> None:
        """``key`` 模式：按 ``array_key_field`` 配对，路径使用**左侧下标**。"""
        left, right = work.left, work.right
        path, right_path, depth = work.path, work.right_path, work.depth
        field_name = self.opts.array_key_field

        left_keys: List[Any] = [None] * len(left)
        right_keys: List[Any] = [None] * len(right)
        left_by_key: Dict[Any, List[int]] = {}
        unkeyed_left: List[int] = []
        unkeyed_right: List[int] = []

        for index, item in enumerate(left):
            token = _key_token(item, field_name, self.opts, path + (index,), self.sizes_left)
            if token is None:
                unkeyed_left.append(index)
            else:
                left_keys[index] = token
                left_by_key.setdefault(token, []).append(index)
        for index, item in enumerate(right):
            token = _key_token(item, field_name, self.opts, right_path + (index,), self.sizes_right)
            if token is None:
                unkeyed_right.append(index)
            else:
                right_keys[index] = token

        # ---- 配对：按键值出现顺序依次配对，键值相同的第 n 个配第 n 个 ----
        pairs: List[Tuple[int, int]] = []
        added: List[int] = []
        cursor: Dict[Any, int] = {}
        for index in range(len(right)):
            token = right_keys[index]
            if token is None:
                continue
            used = cursor.get(token, 0)
            candidates = left_by_key.get(token, ())
            if used < len(candidates):
                cursor[token] = used + 1
                pairs.append((candidates[used], index))
            else:
                added.append(index)
        # 没有键字段的元素退回"按出现顺序位置配对"
        for offset, index in enumerate(unkeyed_right):
            if offset < len(unkeyed_left):
                pairs.append((unkeyed_left[offset], index))
            else:
                added.append(index)
        paired_left = {left_index for left_index, _right_index in pairs}
        pair_of_left = dict(pairs)
        added.sort()

        # ---- 产出：左侧按下标序（removed / 配对递归），再报右侧独有的 added ----
        children: List[Any] = []
        for index in range(len(left)):
            child_path = path + (index,)
            if field_ignored(child_path, self.opts):
                continue
            if index not in paired_left:
                value = left[index]
                children.append(_Pending(child_path, KIND_REMOVED, value, None,
                                         json_type(value), "", depth + 1))
                continue
            right_index = pair_of_left[index]
            a, b = left[index], right[right_index]
            if self._equal_loose(a, b, child_path):
                self.unchanged += _node_count(a, self.sizes_left)
                continue
            children.append(_Work(a, b, child_path, right_path + (right_index,), depth + 1))
        for index in added:
            child_path = path + (index,)
            right_child = right_path + (index,)
            if field_ignored(child_path, self.opts) or field_ignored(right_child, self.opts):
                continue
            value = right[index]
            children.append(_Pending(child_path, KIND_ADDED, None, value, "",
                                     json_type(value), depth + 1))
        queue.extendleft(reversed(children))

    # ------------------------------------------------------------------ #
    # 产出条目
    # ------------------------------------------------------------------ #
    def _emit(self, path: Tuple, kind: str, left: Any, right: Any, left_type: str,
              right_type: str, depth: int, detail: str = "") -> None:
        if len(self.items) >= self.budget:
            self.truncated = True
            return
        self.items.append(DiffItem(
            path=tuple(path),
            path_string=path_string(path),
            kind=kind,
            left=left,
            right=right,
            left_type=left_type,
            right_type=right_type,
            depth=depth,
            detail=detail,
        ))
        self.counts[kind] = self.counts.get(kind, 0) + 1

    def _warn_once(self, code: str, detail: str) -> None:
        message = f"{code}: {detail}" if detail else code
        if message not in self.warnings:
            self.warnings.append(message)


def _multiset(tokens: Sequence[Any]) -> Tuple[Dict[Any, int], Dict[Any, List[int]], List[Any]]:
    """一次遍历得到：``token -> 出现次数``、``token -> 出现下标列表``、首次出现顺序。"""
    counts: Dict[Any, int] = {}
    positions: Dict[Any, List[int]] = {}
    order: List[Any] = []
    for index, token in enumerate(tokens):
        if token in counts:
            counts[token] += 1
            positions[token].append(index)
        else:
            counts[token] = 1
            positions[token] = [index]
            order.append(token)
    return counts, positions, order


def _key_token(item: Any, field_name: str, opts: DiffOptions, path: Tuple,
               sizes: Dict[int, int]) -> Any:
    """取元素的配对键 token；元素不是对象、没有该字段或字段是容器时返回 ``None``。"""
    if not isinstance(item, dict) or field_name not in item:
        return None
    value = item[field_name]
    kind = json_type(value)
    if kind in (TYPE_OBJECT, TYPE_ARRAY):
        return None
    if field_ignored(path + (field_name,), opts):
        return None
    return _token(value, opts, path + (field_name,), sizes)


# --------------------------------------------------------------------------- #
# 公共入口
# --------------------------------------------------------------------------- #
def diff_json(left: Any, right: Any, options: Optional[DiffOptions] = None) -> DiffResult:
    """对比两个已解析的 JSON 值。任何输入都不抛异常，问题写在 ``error`` 里。"""
    started = time.perf_counter()
    try:
        opts, warnings = _coerce_options(options)
        engine = _Engine(opts, warnings)
        result = engine.run(left, right)
        result.elapsed_ms = (time.perf_counter() - started) * 1000.0
        result.left_nodes = engine.sizes_left.get(id(left), 1)
        result.right_nodes = engine.sizes_right.get(id(right), 1)
        return result
    except _DiffError as exc:
        return _failed(started, str(exc))
    except RecursionError:  # pragma: no cover - 迭代实现下不应发生
        return _failed(started, "depth_exceeded: the documents are too deeply nested to compare")
    except MemoryError:  # pragma: no cover
        return _failed(started, "out_of_memory: the documents are too large to compare")
    except Exception as exc:  # noqa: BLE001 - 公共边界必须兜底
        return _failed(started, f"internal_error: {type(exc).__name__}: {exc}")


def diff_text(left_text: str, right_text: str,
              options: Optional[DiffOptions] = None, mode: str = MODE_JSON) -> DiffResult:
    """解析两侧文本再对比；解析失败时 ``changed=False`` 并给出可读的 ``error``。"""
    started = time.perf_counter()
    try:
        parse_mode = MODE_JSONC if str(mode).strip().lower() == MODE_JSONC else MODE_JSON
        opts, warnings = _coerce_options(options)
        left_parsed = _parse_side(left_text, parse_mode, "left")
        right_parsed = _parse_side(right_text, parse_mode, "right")
        engine = _Engine(opts, warnings)
        result = engine.run(left_parsed, right_parsed)
        result.elapsed_ms = (time.perf_counter() - started) * 1000.0
        result.left_nodes = engine.sizes_left.get(id(left_parsed), 1)
        result.right_nodes = engine.sizes_right.get(id(right_parsed), 1)
        return result
    except _DiffError as exc:
        return _failed(started, str(exc))
    except RecursionError:  # pragma: no cover
        return _failed(started, "depth_exceeded: the documents are too deeply nested to parse")
    except MemoryError:  # pragma: no cover
        return _failed(started, "out_of_memory: the documents are too large to parse")
    except Exception as exc:  # noqa: BLE001 - 公共边界必须兜底
        return _failed(started, f"internal_error: {type(exc).__name__}: {exc}")


def _parse_side(text: Any, mode: str, side: str) -> Any:
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise _DiffError(f"parse_error_{side}", f"the text is not valid UTF-8 ({exc})") from None
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    parsed = parse_json(text, mode, max_depth=PARSE_MAX_DEPTH)
    if not parsed.ok:
        issue = parsed.error
        if issue is None:  # pragma: no cover - 解析器总会给出 issue
            raise _DiffError(f"parse_error_{side}", "the document could not be parsed")
        title, message, _suggestion = issue.describe()
        where = f"line {issue.line}, column {issue.column}"
        raise _DiffError(f"parse_error_{side}",
                         f"{side} document is invalid at {where} ({issue.code}): {message}")
    return parsed.value


def _failed(started: float, error: str) -> DiffResult:
    return DiffResult(
        changed=False,
        items=[],
        summary={kind: 0 for kind in DIFF_KINDS} | {"unchanged": 0, "total": 0, "truncated": False},
        truncated=False,
        error=error,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
    )


# --------------------------------------------------------------------------- #
# 报表：Markdown
# --------------------------------------------------------------------------- #
def _md_cell(text: str) -> str:
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _summary_line(summary: Dict[str, Any]) -> str:
    parts = [f"{summary.get(kind, 0)} {kind}" for kind in DIFF_KINDS if summary.get(kind, 0)]
    unchanged = summary.get("unchanged", 0)
    if not parts:
        return f"no differences ({unchanged} unchanged leaf value(s))"
    parts.append(f"{unchanged} unchanged")
    return ", ".join(parts)


def diff_to_markdown(result: Any, *, max_rows: int = 500, value_limit: int = 200) -> str:
    """差异 -> Markdown 报表（GitHub 风格表格，单元格里的 ``|`` 与换行被转义）。"""
    items, summary, error, warnings, truncated = _report_parts(result)
    out: List[str] = ["# JSON Diff Report", ""]
    if error:
        out += [f"**Error:** `{_md_cell(error)}`", ""]
    state = "CHANGED" if _result_changed(result) else "IDENTICAL"
    out += [f"- Result: **{state}**",
            f"- Differences: **{len(items)}**",
            f"- Summary: {_md_cell(_summary_line(summary))}",
            f"- Truncated: {'yes' if truncated else 'no'}"]
    if warnings:
        out.append(f"- Warnings: {len(warnings)}")
    out.append("")
    if warnings:
        out += ["## Warnings", ""]
        out += [f"- `{_md_cell(str(w))}`" for w in warnings]
        out.append("")
    if not items:
        out += ["## Differences", "",
                "No structural difference was found under the selected options." if not error
                else "The documents could not be compared.", ""]
        return "\n".join(out)

    out += ["## Differences", "",
            "| # | Path | Kind | Left | Right |",
            "| ---: | --- | --- | --- | --- |"]
    shown = items if max_rows <= 0 else items[:max_rows]
    for number, item in enumerate(shown, start=1):
        left_text = _item_side_text(item, "left", value_limit)
        right_text = _item_side_text(item, "right", value_limit)
        out.append("| {n} | `{path}` | {kind} | {left} | {right} |".format(
            n=number,
            path=_md_cell(_item_path(item)),
            kind=_md_cell(str(getattr(item, "kind", ""))),
            left=_md_cell(left_text) or "&nbsp;",
            right=_md_cell(right_text) or "&nbsp;",
        ))
    if max_rows > 0 and len(items) > len(shown):
        out += ["", f"_… {len(items) - len(shown)} more difference(s) were omitted from this report._"]
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# 报表：HTML（自包含、内联样式、所有值都转义）
# --------------------------------------------------------------------------- #
_HTML_CSS = """
:root { color-scheme: light dark; }
body { margin: 0; padding: 24px; background: #ffffff; color: #1f2328;
       font: 14px/1.55 -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial,
             "Microsoft YaHei", sans-serif; }
h1 { font-size: 20px; margin: 0 0 12px; }
h2 { font-size: 16px; margin: 24px 0 8px; }
ul.meta { margin: 0 0 8px; padding-left: 20px; }
ul.meta li { margin: 2px 0; }
.error { background: #ffebe9; border: 1px solid #ff818266; color: #82071e;
         padding: 8px 12px; border-radius: 6px; margin: 8px 0; }
.warn { background: #fff8c5; border: 1px solid #d4a72c66; color: #4d2d00;
        padding: 8px 12px; border-radius: 6px; margin: 8px 0; }
.ok { background: #dafbe1; border: 1px solid #4ac26b66; color: #0a3622;
      padding: 8px 12px; border-radius: 6px; margin: 8px 0; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #d0d7de; padding: 5px 8px; text-align: left; vertical-align: top; }
th { background: #f6f8fa; font-weight: 600; position: sticky; top: 0; }
td.num { text-align: right; color: #57606a; width: 1%; white-space: nowrap; }
td.path { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12.5px;
          word-break: break-all; }
td.val { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12.5px;
         white-space: pre-wrap; word-break: break-word; max-width: 420px; }
.kind { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 12px;
        font-weight: 600; white-space: nowrap; }
.kind-added { background: #dafbe1; color: #0a3622; }
.kind-removed { background: #ffebe9; color: #82071e; }
.kind-changed { background: #fff8c5; color: #4d2d00; }
.kind-type_changed { background: #ddf4ff; color: #0550ae; }
.kind-reordered { background: #fbefff; color: #6639ba; }
tr.row-added td.val.right { background: #f0fff4; }
tr.row-removed td.val.left { background: #fff5f5; }
tr.row-changed td.val { background: #fffdf2; }
.note { color: #57606a; font-style: italic; margin-top: 8px; }
code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
@media (prefers-color-scheme: dark) {
  body { background: #0d1117; color: #e6edf3; }
  th { background: #161b22; }
  th, td { border-color: #30363d; }
  td.num, .note { color: #8b949e; }
  .error { background: #490202; border-color: #f8514966; color: #ffdcd7; }
  .warn { background: #341a04; border-color: #d4a72c66; color: #ffe9b3; }
  .ok { background: #032b1a; border-color: #2ea04366; color: #d3f8dd; }
  .kind-added { background: #032b1a; color: #7ee2a8; }
  .kind-removed { background: #490202; color: #ffb3ab; }
  .kind-changed { background: #341a04; color: #ffd88a; }
  .kind-type_changed { background: #04182e; color: #9cd4ff; }
  .kind-reordered { background: #211039; color: #d8b9ff; }
  tr.row-added td.val.right { background: #0f2417; }
  tr.row-removed td.val.left { background: #2b0f10; }
  tr.row-changed td.val { background: #26200d; }
}
""".strip()


def diff_to_html(result: Any, *, max_rows: int = 1000, value_limit: int = 400) -> str:
    """差异 -> 自包含 HTML 报表（内联样式、无外部资源、**所有值都转义**）。"""
    items, summary, error, warnings, truncated = _report_parts(result)
    esc = html_module.escape
    out: List[str] = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>JSON Diff Report</title>",
        "<style>" + _HTML_CSS + "</style>",
        "</head>",
        "<body>",
        "<h1>JSON Diff Report</h1>",
    ]
    if error:
        out.append(f'<div class="error"><strong>Error:</strong> {esc(error)}</div>')
    out.append("<ul class=\"meta\">")
    out.append(f"<li>Result: <strong>{'CHANGED' if _result_changed(result) else 'IDENTICAL'}</strong></li>")
    out.append(f"<li>Differences: <strong>{len(items)}</strong></li>")
    out.append(f"<li>Summary: {esc(_summary_line(summary))}</li>")
    out.append(f"<li>Truncated: {'yes' if truncated else 'no'}</li>")
    out.append(f"<li>Elapsed: {esc(_elapsed_text(result))}</li>")
    out.append("</ul>")
    for warning in warnings:
        out.append(f'<div class="warn">{esc(str(warning))}</div>')
    if not error and not _result_changed(result):
        out.append('<div class="ok">The two documents are identical under the selected options.</div>')

    out.append("<h2>Differences</h2>")
    if not items:
        out.append('<p class="note">No structural difference was found under the selected options.</p>'
                   if not error else
                   '<p class="note">The documents could not be compared.</p>')
    else:
        shown = items if max_rows <= 0 else items[:max_rows]
        out.append("<table>")
        out.append("<thead><tr><th>#</th><th>Path</th><th>Kind</th>"
                   "<th>Left</th><th>Right</th></tr></thead>")
        out.append("<tbody>")
        for number, item in enumerate(shown, start=1):
            kind = str(getattr(item, "kind", ""))
            safe_kind = esc(kind)
            out.append(f'<tr class="row-{safe_kind}">')
            out.append(f'<td class="num">{number}</td>')
            out.append(f'<td class="path">{esc(_item_path(item))}</td>')
            out.append(f'<td><span class="kind kind-{safe_kind}">{safe_kind}</span></td>')
            out.append(f'<td class="val left">{esc(_item_side_text(item, "left", value_limit))}</td>')
            out.append(f'<td class="val right">{esc(_item_side_text(item, "right", value_limit))}</td>')
            out.append("</tr>")
        out.append("</tbody>")
        out.append("</table>")
        if max_rows > 0 and len(items) > len(shown):
            out.append(f'<p class="note">… {len(items) - len(shown)} more difference(s) '
                       f"were omitted from this report.</p>")
    out.append("</body>")
    out.append("</html>")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# 报表：机器可读 JSON 文本
# --------------------------------------------------------------------------- #
def diff_to_json_text(result: Any, *, indent: int = 2, include_values: bool = True,
                      value_limit: int = 2000) -> str:
    """差异 -> 机器可读的 JSON 文本报表（不依赖 stdlib ``json``，数字字面量保真）。"""
    items, summary, error, warnings, truncated = _report_parts(result)
    payload: Dict[str, Any] = {
        "report": "json-diff",
        "version": 1,
        "changed": bool(_result_changed(result)),
        "error": error,
        "truncated": bool(truncated),
        "elapsed_ms": round(_elapsed_value(result), 3),
        "summary": dict(summary),
        "warnings": [str(w) for w in warnings],
        "options": _options_payload(getattr(result, "options", None)),
        "counts": {
            "left_nodes": int(getattr(result, "left_nodes", 0) or 0),
            "right_nodes": int(getattr(result, "right_nodes", 0) or 0),
            "items": len(items),
        },
        "items": [],
    }
    rows: List[Dict[str, Any]] = payload["items"]  # type: ignore[assignment]
    for item in items:
        row: Dict[str, Any] = {
            "path": [_path_part(part) for part in _item_path_tuple(item)],
            "path_string": _item_path(item),
            "kind": str(getattr(item, "kind", "")),
            "depth": int(getattr(item, "depth", 0) or 0),
            "left_type": str(getattr(item, "left_type", "") or ""),
            "right_type": str(getattr(item, "right_type", "") or ""),
            "detail": str(getattr(item, "detail", "") or ""),
        }
        if include_values:
            row["left_text"] = _item_side_text(item, "left", value_limit)
            row["right_text"] = _item_side_text(item, "right", value_limit)
        rows.append(row)
    return _dump_json(payload, indent)


def _path_part(part: Any) -> Any:
    if isinstance(part, bool):
        return str(part).lower()
    if isinstance(part, int):
        return part
    return part if isinstance(part, str) else str(part)


def _options_payload(options: Any) -> Dict[str, Any]:
    if not isinstance(options, DiffOptions):
        return {}
    return {
        "ignore_key_order": bool(options.ignore_key_order),
        "report_reordered": bool(options.report_reordered),
        "ignore_fields": [str(p) for p in (options.ignore_fields or ())],
        "ignore_whitespace": bool(options.ignore_whitespace),
        "ignore_string_case": bool(options.ignore_string_case),
        "ignore_number_format": bool(options.ignore_number_format),
        "array_mode": str(options.array_mode),
        "array_key_field": str(options.array_key_field),
        "max_items": int(options.max_items),
    }


def _dump_json(value: Any, indent: int = 2) -> str:
    """极简 JSON 序列化（迭代式）；只处理报表用到的类型，其它退化成字符串。"""
    pad = " " * max(0, min(int(indent), 8))
    out: List[str] = []
    # 栈元素：("value", v) / ("items", iterator, first_flag_holder) / ("lit", text)
    stack: List[Any] = [("value", value, 0)]
    while stack:
        entry = stack.pop()
        tag = entry[0]
        if tag == "lit":
            out.append(entry[1])
            continue
        depth = entry[2]
        node = entry[1]
        newline = "\n" if pad else ""
        child_pad = pad * (depth + 1)
        if isinstance(node, dict):
            if not node:
                out.append("{}")
                continue
            out.append("{" + newline)
            keys = list(node.keys())
            # 逆序压栈 => 正序输出
            stack.append(("lit", newline + pad * depth + "}"))
            for index in reversed(range(len(keys))):
                key = keys[index]
                if index + 1 < len(keys):
                    stack.append(("lit", "," + newline))
                stack.append(("value", node[key], depth + 1))
                stack.append(("lit", child_pad + _dump_key(key) + ":" + (" " if pad else "")))
            continue
        if isinstance(node, (list, tuple)):
            items = list(node)
            if not items:
                out.append("[]")
                continue
            out.append("[" + newline)
            stack.append(("lit", newline + pad * depth + "]"))
            for index in reversed(range(len(items))):
                if index + 1 < len(items):
                    stack.append(("lit", "," + newline))
                stack.append(("value", items[index], depth + 1))
                stack.append(("lit", child_pad))
            continue
        out.append(_dump_scalar(node))
    return "".join(out)


def _dump_key(key: Any) -> str:
    return _dump_scalar(key if isinstance(key, str) else str(key))


def _dump_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (float, Decimal)):
        try:
            return number_literal(value)
        except (ValueError, TypeError, InvalidOperation, ArithmeticError):
            return "null"
    return _quote(str(value))


# --------------------------------------------------------------------------- #
# 报表公共工具
# --------------------------------------------------------------------------- #
def _report_parts(result: Any) -> Tuple[List[Any], Dict[str, Any], str, List[str], bool]:
    items = list(getattr(result, "items", ()) or ())
    summary = dict(getattr(result, "summary", {}) or {})
    error = str(getattr(result, "error", "") or "")
    warnings = [str(w) for w in (getattr(result, "warnings", ()) or ())]
    truncated = bool(getattr(result, "truncated", False))
    return items, summary, error, warnings, truncated


def _result_changed(result: Any) -> bool:
    if getattr(result, "error", ""):
        return False
    return bool(getattr(result, "changed", False))


def _elapsed_value(result: Any) -> float:
    try:
        return float(getattr(result, "elapsed_ms", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _elapsed_text(result: Any) -> str:
    return f"{_elapsed_value(result):.2f} ms"


def _item_path_tuple(item: Any) -> Tuple:
    path = getattr(item, "path", ())
    try:
        return tuple(path)
    except TypeError:
        return ()


def _item_path(item: Any) -> str:
    text = getattr(item, "path_string", "")
    if isinstance(text, str) and text:
        return text
    return path_string(_item_path_tuple(item))


def _item_side_text(item: Any, side: str, limit: int) -> str:
    """某一侧的展示文本；缺失的一侧是空串（区别于 ``null`` 值）。"""
    value_type = getattr(item, f"{side}_type", "") or ""
    value = getattr(item, side, None)
    if not value_type and value is None:
        return ""
    return _value_text(value, limit)


# --------------------------------------------------------------------------- #
# 并排行（供左右双编辑器同步滚动 + 差异高亮）
# --------------------------------------------------------------------------- #
def _as_text(text: Any) -> str:
    if isinstance(text, (bytes, bytearray)):
        try:
            return bytes(text).decode("utf-8-sig")
        except UnicodeDecodeError:
            return bytes(text).decode("utf-8", errors="replace")
    if isinstance(text, str):
        return text
    return "" if text is None else str(text)


def aligned_lines(left_text: str, right_text: str, result: Any = None, *,
                  context: int = 0) -> List[Tuple[Optional[int], Optional[str],
                                                  Optional[int], Optional[str], str]]:
    """把两侧文本按行对齐，产出并排视图需要的行。

    每行是 ``(left_no, left_text, right_no, right_text, marker)``，行号 1-based，
    ``marker`` 取 :data:`ALIGN_MARKERS` 之一：

    * ``"equal"``   —— 两侧都有且文本相同；
    * ``"changed"`` —— 两侧都有但文本不同（成对出现，左右行号都存在）；
    * ``"removed"`` —— 只有左侧有（右侧为 ``(None, None)``）；
    * ``"added"``    —— 只有右侧有（左侧为 ``(None, None)``）；
    * ``"context"`` —— 被折叠的相等区域（仅 ``context > 0`` 时出现），
      行号指向该区**首行**，文本是 ``… N unchanged line(s) …``。

    **无损约定**：``context=0``（默认）不折叠任何区域，两侧每一行都出现且带
    真实行号，因此 UI 的同步滚动、行号映射与"跳到第 N 行"都成立；
    非 context 行上左右行号各自**严格递增**，context 行上单调不减。
    ``context>0`` 会把超过 ``2*context`` 行的相等区折叠成一条 context 行
    （**有损**：被折叠的行不再出现），适合超长文档。

    ``result`` 只用于在无法对齐时附带告警，不影响行的内容。标记来自**文本**
    比较（美化后的两侧文本），与语义条目不是一一对应（见模块 docstring）。
    单侧超过 :data:`MAX_ALIGN_LINES` 行时退化成"按行号并排 + 逐行比较"。
    """
    warnings: List[str] = []
    try:
        left_lines = _as_text(left_text).split("\n")
        right_lines = _as_text(right_text).split("\n")
        if left_lines and left_lines[-1] == "":
            left_lines.pop()
        if right_lines and right_lines[-1] == "":
            right_lines.pop()
        try:
            context = int(context)
        except (TypeError, ValueError):
            context = 0
        context = max(0, context)

        rows: List[Tuple[Optional[int], Optional[str], Optional[int], Optional[str], str]] = []
        if len(left_lines) > MAX_ALIGN_LINES or len(right_lines) > MAX_ALIGN_LINES:
            warnings.append(f"aligned_lines_fallback: a side has more than {MAX_ALIGN_LINES} lines; "
                            f"rows were paired by line number instead of LCS alignment")
            rows = _pair_by_number(left_lines, right_lines)
        else:
            matcher = difflib.SequenceMatcher(None, left_lines, right_lines, autojunk=False)
            for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                rows.extend(_opcode_rows(tag, left_lines, right_lines, i1, i2, j1, j2))
        if context > 0:
            rows = _collapse_equal_runs(rows, context)
        if warnings and result is not None:
            existing = getattr(result, "warnings", None)
            if isinstance(existing, list):
                for message in warnings:
                    if message not in existing:
                        existing.append(message)
        return rows
    except Exception:  # noqa: BLE001 - 公共边界必须兜底
        return []


def _opcode_rows(tag: str, left_lines: Sequence[str], right_lines: Sequence[str],
                 i1: int, i2: int, j1: int, j2: int
                 ) -> List[Tuple[Optional[int], Optional[str], Optional[int], Optional[str], str]]:
    rows: List[Tuple[Optional[int], Optional[str], Optional[int], Optional[str], str]] = []
    if tag == "equal":
        for offset in range(i2 - i1):
            rows.append((i1 + offset + 1, left_lines[i1 + offset],
                         j1 + offset + 1, right_lines[j1 + offset], MARK_EQUAL))
        return rows
    if tag == "delete":
        for index in range(i1, i2):
            rows.append((index + 1, left_lines[index], None, None, MARK_REMOVED))
        return rows
    if tag == "insert":
        for index in range(j1, j2):
            rows.append((None, None, index + 1, right_lines[index], MARK_ADDED))
        return rows
    # replace：先成对（changed），多出来的一侧按 removed / added 收尾
    left_span = list(range(i1, i2))
    right_span = list(range(j1, j2))
    shared = min(len(left_span), len(right_span))
    for offset in range(shared):
        rows.append((left_span[offset] + 1, left_lines[left_span[offset]],
                     right_span[offset] + 1, right_lines[right_span[offset]], MARK_CHANGED))
    for index in left_span[shared:]:
        rows.append((index + 1, left_lines[index], None, None, MARK_REMOVED))
    for index in right_span[shared:]:
        rows.append((None, None, index + 1, right_lines[index], MARK_ADDED))
    return rows


def _pair_by_number(left_lines: Sequence[str], right_lines: Sequence[str]
                    ) -> List[Tuple[Optional[int], Optional[str], Optional[int], Optional[str], str]]:
    rows: List[Tuple[Optional[int], Optional[str], Optional[int], Optional[str], str]] = []
    total = max(len(left_lines), len(right_lines))
    for index in range(total):
        has_left = index < len(left_lines)
        has_right = index < len(right_lines)
        if has_left and has_right:
            marker = MARK_EQUAL if left_lines[index] == right_lines[index] else MARK_CHANGED
            rows.append((index + 1, left_lines[index], index + 1, right_lines[index], marker))
        elif has_left:
            rows.append((index + 1, left_lines[index], None, None, MARK_REMOVED))
        else:
            rows.append((None, None, index + 1, right_lines[index], MARK_ADDED))
    return rows


def _collapse_equal_runs(rows: List[Tuple[Optional[int], Optional[str], Optional[int],
                                          Optional[str], str]], context: int
                         ) -> List[Tuple[Optional[int], Optional[str], Optional[int],
                                         Optional[str], str]]:
    """把过长的相等区折叠成一条 context 行；保留紧邻差异的 ``context`` 行。"""
    keep = context
    out: List[Tuple[Optional[int], Optional[str], Optional[int], Optional[str], str]] = []
    index = 0
    total = len(rows)
    while index < total:
        row = rows[index]
        if row[4] != MARK_EQUAL:
            out.append(row)
            index += 1
            continue
        start = index
        while index < total and rows[index][4] == MARK_EQUAL:
            index += 1
        run = index - start
        at_start = start == 0
        at_end = index == total
        head = 0 if at_start else keep
        tail = 0 if at_end else keep
        if run <= head + tail:
            out.extend(rows[start:index])
            continue
        out.extend(rows[start:start + head])
        first = rows[start + head]
        hidden = run - head - tail
        out.append((first[0], f"… {hidden} unchanged line(s) …", first[2],
                    f"… {hidden} unchanged line(s) …", MARK_CONTEXT))
        out.extend(rows[index - tail:index])
    return out


# --------------------------------------------------------------------------- #
# 只看变更（剪枝）
# --------------------------------------------------------------------------- #
def changed_only_text(value: Any, items: Any, *, side: str = SIDE_LEFT) -> str:
    """把文档剪枝成"只含被差异触及的子树"，再美化输出（2 空格缩进）。

    * ``side="left"`` 取每条差异的 :attr:`DiffItem.left`，``"right"`` 取
      :attr:`DiffItem.right`；某一侧缺失（``added`` 的左侧 / ``removed`` 的
      右侧，即 ``*_type == ""``）的条目会被跳过。
    * 数组下标用 ``null`` 补齐以保持原下标；仅为补位而产生的**尾部** ``null``
      会被裁掉（中间的真实 ``null`` 不受影响）。
    * ``reordered`` 条目的 ``left`` / ``right`` 是键序列而不是文档子树，
      因此被跳过（它的子节点本来就没有变化）。
    * 没有可保留的条目时，返回与原文档同类型的空容器（``{}`` / ``[]``）
      或原标量的美化文本。
    * **绝不修改** ``value``；返回文本永远是合法 JSON（除非原文档深到无法
      序列化，此时返回 ``null``）。
    """
    try:
        want = SIDE_RIGHT if str(side).strip().lower() == SIDE_RIGHT else SIDE_LEFT
        pruned = _prune_to_items(value, items, want)
        if pruned is _MISSING:
            if isinstance(value, dict):
                return "{}"
            if isinstance(value, list):
                return "[]"
            return _pretty(value)
        return _pretty(pruned)
    except Exception:  # noqa: BLE001 - 公共边界必须兜底
        return "null"


class _Missing:
    """``changed_only_text`` 内部哨兵：没有任何条目被保留。"""


_MISSING = _Missing()


def _pretty(value: Any) -> str:
    try:
        return dumps(value, FormatOptions(indent=INDENT_2, line_ending="\n"))
    except (RecursionError, ValueError, TypeError, InvalidOperation, ArithmeticError):
        return "null"


def _prune_to_items(value: Any, items: Any, side: str) -> Any:
    kept = 0
    if isinstance(value, dict):
        root: Any = {}
    elif isinstance(value, list):
        root = []
    else:
        root = None  # 标量根：路径只能是 ()，整份文档就是变更本身
    for item in (items or ()):
        kind = str(getattr(item, "kind", ""))
        if kind == KIND_REORDERED:
            continue
        present_type = getattr(item, f"{side}_type", "") or ""
        payload = getattr(item, side, None)
        if not present_type:
            continue  # 这一侧没有值（added 的左侧 / removed 的右侧）
        path = _item_path_tuple(item)
        if not path:
            return payload  # 根级差异：整份文档就是它
        if _set_at_path(root, value, path, payload):
            kept += 1
    if kept == 0 and root is not None and not root:
        return _MISSING
    if root is None:
        return _MISSING
    _trim_padding(root)
    return root


def _set_at_path(root: Any, original: Any, path: Tuple, payload: Any) -> bool:
    """在 ``root``（剪枝副本）里按 ``path`` 放下 ``payload``，中间层按需新建。"""
    holder = root
    for depth, part in enumerate(path):
        last = depth == len(path) - 1
        if isinstance(part, bool) or not isinstance(part, int):
            # 对象键
            if not isinstance(holder, dict):
                return False
            key = part if isinstance(part, str) else str(part)
            if last:
                holder[key] = payload
                return True
            child = holder.get(key)
            if not isinstance(child, (dict, list)):
                child = _empty_for(_peek(original, path[:depth + 1]))
                holder[key] = child
            holder = child
            continue
        # 数组下标
        if not isinstance(holder, list):
            return False
        index = int(part)
        if index < 0:
            return False
        while len(holder) <= index:
            holder.append(None)
        if last:
            holder[index] = payload
            return True
        child = holder[index]
        if not isinstance(child, (dict, list)):
            child = _empty_for(_peek(original, path[:depth + 1]))
            holder[index] = child
        holder = child
    return False


def _peek(original: Any, path: Tuple) -> Any:
    """按路径看原文档里该节点的类型（用于决定中间层建 ``{}`` 还是 ``[]``）。"""
    current = original
    for part in path:
        if isinstance(current, dict):
            key = part if isinstance(part, str) else str(part)
            if key not in current:
                return None
            current = current[key]
        elif isinstance(current, list):
            if isinstance(part, bool) or not isinstance(part, int) or not (0 <= part < len(current)):
                return None
            current = current[part]
        else:
            return None
    return current


def _empty_for(sample: Any) -> Any:
    if isinstance(sample, list):
        return []
    return {}


def _trim_padding(root: Any) -> None:
    """裁掉数组尾部仅为补位而产生的 ``None``（迭代实现，深层安全）。"""
    stack: List[Any] = [root]
    guard = 0
    while stack:
        node = stack.pop()
        guard += 1
        if guard > MAX_NODES:  # pragma: no cover - 防御
            return
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            while node and node[-1] is None:
                node.pop()
            stack.extend(node)

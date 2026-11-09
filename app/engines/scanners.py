"""扫描引擎：重复键、空值、键/值查找、体积热点与结构报告。

设计要点：

* **全部迭代**：显式栈遍历，1000 层以上嵌套不会 ``RecursionError``。
* **只读**：绝不修改传入的数据；:class:`Finding` 是 frozen dataclass。
* **确定性**：所有输出都按**文档顺序**（对象按键的插入顺序，数组按下标）；
  同尺寸/同分数的排序一律用"先出现者在前"作为二次判据。
* **有上限**：``max_findings`` 生效后立刻停止扫描，并追加一条
  ``kind='truncated'`` 的 :class:`Finding`，UI 可以直接把它渲染成提示行
  （因此返回列表长度可能是 ``max_findings + 1``）。
* **绝不抛异常**：非法正则、非法路径、畸形输入都退化成空结果或一条说明性
  Finding，不会把异常抛给 UI 线程。

``Finding.kind`` 词表见 :data:`FINDING_KINDS`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from ..core.jsonparser import parse_json
from ..core.jsonvalue import (
    TYPE_ARRAY,
    TYPE_BOOLEAN,
    TYPE_NULL,
    TYPE_NUMBER,
    TYPE_OBJECT,
    TYPE_STRING,
    json_type,
    number_literal,
)
from .statistics import MAX_NODES, iter_container_sizes, push_top_n

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


#: 预览文本的最大长度（含省略号）
PREVIEW_LIMIT = 120

KIND_DUPLICATE_KEY = "duplicate_key"
KIND_NULL_VALUE = "null_value"
KIND_EMPTY_STRING = "empty_string"
KIND_WHITESPACE_STRING = "whitespace_string"
KIND_EMPTY_ARRAY = "empty_array"
KIND_EMPTY_OBJECT = "empty_object"
KIND_KEY_MATCH = "key_match"
KIND_VALUE_MATCH = "value_match"
KIND_LARGE_NODE = "large_node"
KIND_DEEP_NODE = "deep_node"
KIND_RECURSIVE_SHAPE = "recursive_shape"
KIND_INVALID_PATTERN = "invalid_pattern"
KIND_TRUNCATED = "truncated"

#: UI 可以按这些 kind 分组着色
FINDING_KINDS: Tuple[str, ...] = (
    KIND_DUPLICATE_KEY,
    KIND_NULL_VALUE,
    KIND_EMPTY_STRING,
    KIND_WHITESPACE_STRING,
    KIND_EMPTY_ARRAY,
    KIND_EMPTY_OBJECT,
    KIND_KEY_MATCH,
    KIND_VALUE_MATCH,
    KIND_LARGE_NODE,
    KIND_DEEP_NODE,
    KIND_RECURSIVE_SHAPE,
    KIND_INVALID_PATTERN,
    KIND_TRUNCATED,
)

__all__ = [
    "FINDING_KINDS",
    "Finding",
    "PREVIEW_LIMIT",
    "StructureReport",
    "analyze_size",
    "analyze_structure",
    "find_duplicate_keys",
    "find_empty_values",
    "find_keys",
    "find_structure_findings",
    "path_string",
    "resolve_path",
    "value_preview",
    "walk_nodes",
]


# --------------------------------------------------------------------------- #
# 结果对象
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Finding:
    """一条扫描结果。``path`` 是元组，``path_string`` 是它的 JSONPath 文本。"""

    kind: str
    path: Tuple = ()
    path_string: str = "$"
    detail: str = ""
    value_preview: str = ""
    offset: int = -1
    line: int = -1
    column: int = -1


@dataclass
class StructureReport:
    """:func:`analyze_structure` 的结果，全部字段都是确定性顺序（文档顺序）。

    * ``shape_signatures``：``[(签名, 出现次数, 首次出现的 path_string)]``，签名是
      排序后的键集合，形如 ``{children,id,name}``。
    * ``array_homogeneity``：``[(path_string, 长度, 元素类型元组, 是否同质)]``，
      元素类型按首次出现顺序去重；空数组算同质。
    * ``depth_histogram``：``{层级: 节点数}``，根为第 1 层（与 statistics 同口径）。
    * ``recursive_shapes``：``[(签名, 出现过的层级列表, 首次出现的 path_string)]``，
      只包含出现在 2 个以上不同层级的签名。
    * ``key_type_conflicts``：``[(键名, 类型列表, 首个冲突值的 path_string)]``。
    * ``max_depth``：本次分析实际到达的最深**节点**层级（根为 1，与
      ``depth_histogram`` 同口径；注意它不是 :class:`Statistics.max_depth` 的容器
      嵌套层数口径）。
    """

    shape_signatures: List[Tuple[str, int, str]] = field(default_factory=list)
    array_homogeneity: List[Tuple[str, int, Tuple[str, ...], bool]] = field(default_factory=list)
    depth_histogram: Dict[int, int] = field(default_factory=dict)
    recursive_shapes: List[Tuple[str, List[int], str]] = field(default_factory=list)
    key_type_conflicts: List[Tuple[str, List[str], str]] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    max_depth: int = 0
    truncated: bool = False


# --------------------------------------------------------------------------- #
# 预览文本
# --------------------------------------------------------------------------- #
_CONTROL_TEXT = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _sanitize(text: str) -> str:
    """去掉控制字符，保证文本可以安全地放进表格/日志。"""
    out: List[str] = []
    for ch in text:
        mapped = _CONTROL_TEXT.get(ch)
        if mapped is not None:
            out.append(mapped)
            continue
        code = ord(ch)
        if code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
            out.append(f"\\u{code:04x}")
            continue
        out.append(ch)
    return "".join(out)


def _clip(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def value_preview(value: Any, limit: int = PREVIEW_LIMIT) -> str:
    """把任意值转成 <= ``limit`` 字符、不含控制字符的展示文本。

    标量给出字面量（字符串不加引号，便于阅读）；容器给出结构摘要，绝不整体
    序列化（否则一个大子树会让预览变得极慢）。
    """
    limit = max(0, int(limit or 0))
    if limit == 0:
        return ""
    try:
        kind = json_type(value)
        if kind == TYPE_STRING:
            text = value if isinstance(value, str) else str(value)
        elif kind == TYPE_NUMBER:
            try:
                text = number_literal(value)
            except (ValueError, TypeError, ArithmeticError):
                text = repr(value)
        elif kind == TYPE_BOOLEAN:
            text = "true" if value else "false"
        elif kind == TYPE_NULL:
            text = "null"
        elif kind == TYPE_OBJECT:
            keys = list(value.keys())
            head = ", ".join(str(k) for k in keys[:5])
            more = ", ..." if len(keys) > 5 else ""
            text = f"object ({len(keys)} keys: {head}{more})" if keys else "object (0 keys)"
        else:
            items = list(value)
            head = ", ".join(json_type(item) for item in items[:5])
            more = ", ..." if len(items) > 5 else ""
            text = f"array ({len(items)} items: {head}{more})" if items else "array (0 items)"
    except Exception:  # pragma: no cover - 防御
        text = repr(value)
    # 先裁剪再转义：转义只会让文本变长，两次裁剪保证结果不超过 limit
    return _clip(_sanitize(_clip(text, limit)), limit)


# --------------------------------------------------------------------------- #
# 迭代遍历（前序 = 文档顺序，内存只与嵌套深度相关）
# --------------------------------------------------------------------------- #
class _WalkFrame:
    __slots__ = ("kind", "items", "index")

    def __init__(self, kind: str, items: Any) -> None:
        self.kind = kind
        self.items = items
        self.index = 0


def walk_nodes(value: Any, *, max_depth: Optional[int] = None,
               max_nodes: int = MAX_NODES) -> Iterator[Tuple[Tuple, Any, int]]:
    """前序（文档顺序）迭代遍历，产出 ``(path, node, depth)``，根深度为 1。

    ``max_depth`` 给定时不再展开更深的子树（该层节点本身仍会产出）。
    迭代器不递归，也不会修改数据。
    """
    frames: List[_WalkFrame] = []
    path: List[Any] = []
    node = value
    seen = 0
    while True:
        yield (tuple(path), node, len(path) + 1)
        seen += 1
        if seen >= max_nodes:
            return
        kind = json_type(node)
        if max_depth is not None and len(path) + 1 >= max_depth:
            kind = ""  # 到达深度上限，不再展开
        if kind == TYPE_OBJECT:
            items = list(node.items())
            if items:
                frames.append(_WalkFrame(TYPE_OBJECT, items))
                path.append(items[0][0])
                node = items[0][1]
                continue
        elif kind == TYPE_ARRAY:
            items = list(node)
            if items:
                frames.append(_WalkFrame(TYPE_ARRAY, items))
                path.append(0)
                node = items[0]
                continue
        # 回溯到下一个兄弟节点
        while True:
            if not frames:
                return
            frame = frames[-1]
            frame.index += 1
            if frame.index < len(frame.items):
                if frame.kind == TYPE_OBJECT:
                    path[-1] = frame.items[frame.index][0]
                    node = frame.items[frame.index][1]
                else:
                    path[-1] = frame.index
                    node = frame.items[frame.index]
                break
            frames.pop()
            path.pop()


_MISSING = object()


def resolve_path(value: Any, path: Sequence) -> Any:
    """按路径取值；取不到时返回 ``None``（绝不抛异常）。"""
    node = value
    try:
        for part in tuple(path):
            if isinstance(part, bool):
                return None
            if isinstance(part, int):
                if not isinstance(node, (list, tuple)) or not -len(node) <= part < len(node):
                    return None
                node = node[part]
            elif isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return None
    except Exception:  # pragma: no cover - 防御
        return None
    return node


def _make_finding(kind: str, path: Tuple, detail: str, value: Any = _MISSING,
                  offset: int = -1, line: int = -1, column: int = -1) -> Finding:
    preview = "" if value is _MISSING else value_preview(value)
    return Finding(kind=kind, path=tuple(path), path_string=path_string(path),
                   detail=detail, value_preview=preview,
                   offset=offset, line=line, column=column)


def _truncated_finding(limit: int, scope: str) -> Finding:
    return _make_finding(
        KIND_TRUNCATED, (),
        f"stopped after {limit} findings ({scope}); narrow the scan or raise the limit")


# --------------------------------------------------------------------------- #
# 重复键（借助解析器的告警，因此带精确 line/column/offset）
# --------------------------------------------------------------------------- #
def find_duplicate_keys(text: str, *, mode: str = "json") -> List[Finding]:
    """找出同一对象内重复出现的键，位置信息来自解析器，精确到行列与偏移。

    解析失败时返回失败之前已经收集到的重复键（可能为空列表）。``value_preview``
    是**最终生效**的那个值（JSON 语义下后者覆盖前者）。
    """
    try:
        result = parse_json(text if isinstance(text, str) else str(text), mode)
    except Exception:  # pragma: no cover - parse_json 本身不抛异常
        return []
    findings: List[Finding] = []
    for issue in getattr(result, "warnings", ()) or ():
        if getattr(issue, "code", "") != KIND_DUPLICATE_KEY:
            continue
        try:
            details = issue.details or {}
            key = str(details.get("key", ""))
            raw_path = details.get("path", ())
            path = tuple(raw_path) if isinstance(raw_path, (tuple, list)) else ()
            first = details.get("first_offset", -1)
            detail = f"key '{key}' appears more than once in the same object"
            if isinstance(first, int) and first >= 0:
                detail += f"; first occurrence at offset {first}"
            detail += "; the last value wins"
            value = resolve_path(result.value, path) if result.ok else _MISSING
            findings.append(_make_finding(KIND_DUPLICATE_KEY, path, detail, value,
                                          offset=issue.offset, line=issue.line,
                                          column=issue.column))
        except Exception:  # pragma: no cover - 防御
            continue
    return findings


# --------------------------------------------------------------------------- #
# 空值扫描
# --------------------------------------------------------------------------- #
def find_empty_values(value: Any, *, include_null: bool = True,
                      include_empty_string: bool = True,
                      include_empty_array: bool = True,
                      include_empty_object: bool = True,
                      include_whitespace_only_string: bool = False,
                      max_findings: int = 10000) -> List[Finding]:
    """按文档顺序找出"空"的节点：null / 空串 / 空数组 / 空对象 / 纯空白串。

    根节点本身也参与判断（例如整个文档就是 ``null``）。达到 ``max_findings``
    后立即停止，并追加一条 ``kind='truncated'`` 的结果。
    """
    limit = max(0, int(max_findings or 0))
    findings: List[Finding] = []
    if not any((include_null, include_empty_string, include_empty_array,
                include_empty_object, include_whitespace_only_string)):
        return findings
    try:
        for path, node, _depth in walk_nodes(value):
            kind = json_type(node)
            matched = ""
            detail = ""
            if kind == TYPE_NULL and include_null:
                matched, detail = KIND_NULL_VALUE, "value is null"
            elif kind == TYPE_STRING and include_empty_string and node == "":
                matched, detail = KIND_EMPTY_STRING, "string is empty"
            elif (kind == TYPE_STRING and include_whitespace_only_string and node != ""
                  and isinstance(node, str) and node.strip() == ""):
                matched = KIND_WHITESPACE_STRING
                detail = f"string contains only whitespace ({len(node)} chars)"
            elif kind == TYPE_ARRAY and include_empty_array and not node:
                matched, detail = KIND_EMPTY_ARRAY, "array has no items"
            elif kind == TYPE_OBJECT and include_empty_object and not node:
                matched, detail = KIND_EMPTY_OBJECT, "object has no keys"
            if not matched:
                continue
            if len(findings) >= limit:
                findings.append(_truncated_finding(limit, "empty values"))
                return findings
            findings.append(_make_finding(matched, path, detail, node))
    except Exception:  # pragma: no cover - 防御
        return findings
    return findings


# --------------------------------------------------------------------------- #
# 键 / 值查找
# --------------------------------------------------------------------------- #
def _compile_matcher(pattern: str, *, case_sensitive: bool, regex: bool):
    """返回 ``(matcher, error_detail)``；``matcher(text) -> bool``。"""
    if regex:
        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            return None, f"invalid regular expression: {exc}"
        except Exception as exc:  # pragma: no cover - 防御
            return None, f"invalid regular expression: {exc}"
        return (lambda text: compiled.search(text) is not None), ""
    if not case_sensitive:
        needle = pattern.lower()
        return (lambda text: needle in text.lower()), ""
    return (lambda text: pattern in text), ""


def _scalar_text(value: Any, kind: str) -> Optional[str]:
    """标量的可搜索文本；容器返回 ``None``（容器不参与值匹配）。"""
    if kind == TYPE_STRING:
        return value if isinstance(value, str) else str(value)
    if kind == TYPE_NUMBER:
        try:
            return number_literal(value)
        except (ValueError, TypeError, ArithmeticError):
            return str(value)
    if kind == TYPE_BOOLEAN:
        return "true" if value else "false"
    if kind == TYPE_NULL:
        return "null"
    return None


def find_keys(value: Any, pattern: str, *, case_sensitive: bool = False,
              regex: bool = False, match_values: bool = False,
              max_findings: int = 10000) -> List[Finding]:
    """按文档顺序查找键名（可选同时查找值）。

    * ``regex=False`` 时是**子串**匹配（不是全等）；``regex=True`` 时用
      ``re.search``，非法正则会返回一条 ``kind='invalid_pattern'`` 的结果。
    * ``match_values=True`` 时字符串值按原文匹配，数字按字面量文本匹配，
      ``true``/``false``/``null`` 按关键字文本匹配；容器不参与值匹配。
    * 命中键时 ``kind='key_match'``，命中值时 ``kind='value_match'``；同一节点
      两者都命中时先报键（键在文本中更靠前）。
    """
    limit = max(0, int(max_findings or 0))
    findings: List[Finding] = []
    if not isinstance(pattern, str) or pattern == "":
        findings.append(_make_finding(KIND_INVALID_PATTERN, (),
                                      "search pattern is empty; nothing to match"))
        return findings
    matcher, error = _compile_matcher(pattern, case_sensitive=case_sensitive, regex=regex)
    if matcher is None:
        findings.append(_make_finding(KIND_INVALID_PATTERN, (), error))
        return findings
    try:
        for path, node, _depth in walk_nodes(value):
            if path:
                last = path[-1]
                if isinstance(last, str) and _safe_match(matcher, last):
                    if len(findings) >= limit:
                        findings.append(_truncated_finding(limit, "key search"))
                        return findings
                    findings.append(_make_finding(
                        KIND_KEY_MATCH, path,
                        f"key '{last}' matches '{pattern}'", node))
            if match_values:
                text = _scalar_text(node, json_type(node))
                if text is not None and _safe_match(matcher, text):
                    if len(findings) >= limit:
                        findings.append(_truncated_finding(limit, "value search"))
                        return findings
                    findings.append(_make_finding(
                        KIND_VALUE_MATCH, path,
                        f"value matches '{pattern}'", node))
    except Exception:  # pragma: no cover - 防御
        return findings
    return findings


def _safe_match(matcher, text: str) -> bool:
    """正则可能因灾难性回溯抛错，这里一律吞掉，绝不打断扫描。"""
    try:
        return bool(matcher(text))
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 体积分析
# --------------------------------------------------------------------------- #
def analyze_size(value: Any, *, top_n: int = 20) -> List[Tuple[str, int, str]]:
    """返回最大的 ``top_n`` 个子树：``[(path_string, 字节数, 类型名)]``，降序。

    只统计容器（object/array）子树，标量不单独列出；根节点通常是最大项。
    排序规则：字节数降序，同尺寸按文档顺序（先出现者在前）。
    字节数是紧凑序列化后的 UTF-8 大小，由子树自底向上累加得到，不会为每个
    子树重复序列化。
    """
    top_n = max(0, int(top_n or 0))
    if top_n == 0:
        return []
    top: List[Tuple[int, int, Tuple, str]] = []
    try:
        for seq, path, size, kind in iter_container_sizes(value):
            push_top_n(top, (size, seq, path, kind), top_n)
    except Exception:  # pragma: no cover - 防御
        return [(path_string(p), s, k) for s, _q, p, k in sorted(
            top, key=lambda item: (-item[0], item[1]))]
    top.sort(key=lambda item: (-item[0], item[1]))
    return [(path_string(path), size, kind) for size, _seq, path, kind in top]


# --------------------------------------------------------------------------- #
# 结构分析
# --------------------------------------------------------------------------- #
def _shape_signature(keys: Sequence[str]) -> str:
    """对象键集合指纹：排序后拼接，保证同构对象得到同一个签名。"""
    return "{" + ",".join(sorted(keys)) + "}"


def analyze_structure(value: Any, *, max_depth: int = 64) -> StructureReport:
    """分析文档结构：形状指纹、数组同质性、深度直方图、递归形状与类型冲突。

    ``max_depth`` 限制展开层数（超过该深度的子树不再分析），既能防止病态文档
    拖慢 UI，也避免超深递归；被截断时 ``StructureReport.truncated`` 为 ``True``。
    任何输入都不会抛异常。
    """
    report = StructureReport()
    try:
        depth_limit = max(1, int(max_depth or 1))
    except (TypeError, ValueError):
        depth_limit = 64
    histogram: Dict[int, int] = {}
    shapes: Dict[str, List[Any]] = {}      # signature -> [count, example_path, depths:set]
    key_types: Dict[str, List[Any]] = {}   # key -> [types:list, first_path, conflict_path]
    conflicts_seen: Dict[str, bool] = {}
    reached = 0
    try:
        for path, node, depth in walk_nodes(value, max_depth=depth_limit):
            histogram[depth] = histogram.get(depth, 0) + 1
            reached = depth if depth > reached else reached
            kind = json_type(node)
            if kind == TYPE_OBJECT:
                keys = [k if isinstance(k, str) else str(k) for k in node.keys()]
                signature = _shape_signature(keys)
                entry = shapes.get(signature)
                if entry is None:
                    shapes[signature] = [1, path, {depth}]
                else:
                    entry[0] += 1
                    entry[2].add(depth)
                for key in keys:
                    child_kind = json_type(node[key])
                    child_path = path + (key,)
                    record = key_types.get(key)
                    if record is None:
                        key_types[key] = [[child_kind], child_path, None]
                    elif child_kind not in record[0]:
                        record[0].append(child_kind)
                        if record[2] is None:
                            record[2] = child_path
            elif kind == TYPE_ARRAY:
                types: List[str] = []
                for item in node:
                    item_type = json_type(item)
                    if item_type not in types:
                        types.append(item_type)
                report.array_homogeneity.append(
                    (path_string(path), len(node), tuple(types), len(types) <= 1))
        report.truncated = reached >= depth_limit
    except Exception:  # pragma: no cover - 防御
        return report

    report.depth_histogram = {d: histogram[d] for d in sorted(histogram)}
    report.max_depth = reached
    for signature, (count, example, depths) in shapes.items():
        report.shape_signatures.append((signature, count, path_string(example)))
    for signature, (count, example, depths) in shapes.items():
        if len(depths) >= 2:
            report.recursive_shapes.append(
                (signature, sorted(depths), path_string(example)))
    for key, (types, _first, conflict_path) in key_types.items():
        if len(types) > 1:
            report.key_type_conflicts.append(
                (key, list(types), path_string(conflict_path if conflict_path else ())))

    report.suggestions = _build_suggestions(report)
    return report


def _build_suggestions(report: StructureReport) -> List[str]:
    """根据结构分析结果生成可执行的英文建议，顺序确定。"""
    suggestions: List[str] = []
    for key, types, example in report.key_type_conflicts:
        suggestions.append(
            f"consider normalizing '{key}' ({' vs '.join(types)}); see {example}")
    for signature, depths, example in report.recursive_shapes[:5]:
        suggestions.append(
            f"shape {signature} repeats at depths {', '.join(str(d) for d in depths)}"
            f" ({example}); the document looks self-similar, model it as a recursive"
            " $defs entry instead of copy-pasting the structure")
    shown = 0
    for signature, count, example in report.shape_signatures:
        if count < 3 or signature == "{}":
            continue
        suggestions.append(
            f"object shape {signature} appears {count} times ({example} and others);"
            " consider extracting it into a reusable schema definition")
        shown += 1
        if shown >= 5:
            break
    shown = 0
    for path, length, types, homogeneous in report.array_homogeneity:
        if homogeneous or length == 0:
            continue
        suggestions.append(
            f"array at {path} mixes element types ({', '.join(types)});"
            " consider splitting it or declaring a union type")
        shown += 1
        if shown >= 5:
            break
    if report.max_depth >= 12:
        suggestions.append(
            f"the document nests {report.max_depth} levels deep; consider flattening"
            " deeply nested objects to keep paths readable")
    if report.truncated:
        suggestions.append(
            "analysis stopped at the configured depth limit; deeper levels were not"
            " inspected (raise max_depth to see them)")
    if not suggestions:
        suggestions.append("structure looks consistent; no normalization needed")
    return suggestions


# --------------------------------------------------------------------------- #
# 体积 / 深度 / 递归形状 -> Finding 列表
# --------------------------------------------------------------------------- #
def find_structure_findings(value: Any, *, min_bytes: int = 1024,
                            deep_threshold: int = 16, detect_recursive: bool = True,
                            max_findings: int = 10000) -> List[Finding]:
    """找出体积热点、超深节点与自相似（递归）形状，统一成 :class:`Finding` 列表。

    * ``large_node``：紧凑字节数 >= ``min_bytes`` 的容器。
    * ``deep_node``：节点深度 > ``deep_threshold``（只在**跨过阈值的那一层**报一
      次，不会把整棵深子树的每个节点都报出来）。
    * ``recursive_shape``：同一键集合指纹出现在两个以上不同深度。

    输出按文档顺序排列（``recursive_shape`` 因为需要整棵树的信息，统一排在后面）；
    达到 ``max_findings`` 时停止并追加 ``truncated``。
    """
    limit = max(0, int(max_findings or 0))
    try:
        min_bytes = max(0, int(min_bytes or 0))
        deep_threshold = max(1, int(deep_threshold or 1))
    except (TypeError, ValueError):
        min_bytes, deep_threshold = 1024, 16
    collected: List[Tuple[int, Finding]] = []
    truncated = False
    try:
        # enumerate 的序号与 iter_container_sizes 的前序序号口径一致，可以直接混排
        for seq, (path, node, depth) in enumerate(walk_nodes(value)):
            if depth == deep_threshold + 1:
                # 只报"跨过阈值的那一层"，深子树内部不再逐节点刷屏
                collected.append((seq, _make_finding(
                    KIND_DEEP_NODE, path,
                    f"node sits at depth {depth}, deeper than the threshold "
                    f"{deep_threshold}", node)))
                if len(collected) > limit:
                    # 多收一条再停：只有真的还有内容被丢弃时才报 truncated
                    truncated = True
                    break
    except Exception:  # pragma: no cover - 防御
        pass
    if not truncated:
        try:
            for seq, path, size, kind in iter_container_sizes(value):
                if size < min_bytes:
                    continue
                node = resolve_path(value, path)
                collected.append((seq, _make_finding(
                    KIND_LARGE_NODE, path,
                    f"{kind} subtree serializes to {size} bytes", node)))
                if len(collected) > limit:
                    truncated = True
                    break
        except Exception:  # pragma: no cover - 防御
            pass
    collected.sort(key=lambda item: item[0])
    findings = [finding for _seq, finding in collected[:limit]]
    if truncated:
        findings.append(_truncated_finding(limit, "size and depth hotspots"))
        return findings
    if detect_recursive and len(findings) < limit:
        try:
            report = analyze_structure(value)
            for signature, depths, example in report.recursive_shapes:
                if len(findings) >= limit:
                    findings.append(_truncated_finding(limit, "recursive shapes"))
                    break
                findings.append(Finding(
                    kind=KIND_RECURSIVE_SHAPE, path=(), path_string=example,
                    detail=(f"object shape {signature} repeats at depths "
                            f"{', '.join(str(d) for d in depths)}; likely a recursive "
                            "or self-similar structure"),
                    value_preview=example))
        except Exception:  # pragma: no cover - 防御
            pass
    return findings

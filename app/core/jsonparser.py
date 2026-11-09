"""标准 JSON / JSONC 解析器。

设计要点：

* **迭代式解析**：容器用显式栈展开，深层嵌套不会触发 Python 递归上限，
  也不会段错误；超过 ``max_depth`` 时返回明确错误。
* **位置精确**：每个问题都带 line / column / offset / length，可直接用于
  编辑器跳转与行内红标。
* **数字保真**：整数字面量 -> ``int``（任意精度），小数/指数 ->
  :class:`~app.core.jsonvalue.JsonNumber`（携带原文）。
* **严格区分 JSON 与 JSONC**：注释与尾逗号只在 ``mode="jsonc"`` 下合法，
  标准模式下报 ``comment_not_allowed`` / ``trailing_comma``。
* **绝不修改数据**：解析器只读；修复建议以文本形式给出，是否应用由用户决定。
* **可取消 + 可报告进度**：供后台线程处理大文件。
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .jsonvalue import JsonNumber

MODE_JSON = "json"
MODE_JSONC = "jsonc"

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

DEFAULT_MAX_DEPTH = 1000
#: 超过该字节数不再记录节点偏移表（避免大文件内存爆炸）
OFFSET_TRACK_LIMIT = 8 * 1024 * 1024
_PROGRESS_STEP = 256 * 1024

KIND_OBJECT = "object"
KIND_ARRAY = "array"

__all__ = [
    "DEFAULT_MAX_DEPTH",
    "Issue",
    "JsonParseError",
    "JsonParser",
    "MODE_JSON",
    "MODE_JSONC",
    "OffsetInfo",
    "ParseCancelled",
    "ParseResult",
    "ParseStats",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "describe_issue",
    "parse_json",
]


# --------------------------------------------------------------------------- #
# 问题描述表（英文源串，UI 层通过 code 做中文本地化）
# --------------------------------------------------------------------------- #
ISSUE_MESSAGES: Dict[str, Tuple[str, str, str]] = {
    # code: (title, message template, suggestion template)
    "empty_input": ("Empty input", "The document is empty; there is no JSON value to parse.",
                    "Paste or open a JSON document first."),
    "unexpected_eof": ("Unexpected end of input", "The document ended while a {expected} was expected.",
                       "The file looks truncated; add the missing content."),
    "unclosed_string": ("Unclosed string", "A string literal is not closed by a double quote.",
                        "Add the missing \" before the end of the line."),
    "invalid_escape": ("Invalid escape sequence", "Escape sequence {found} is not allowed in JSON.",
                       "Use one of \\\" \\\\ \\/ \\b \\f \\n \\r \\t \\uXXXX."),
    "invalid_unicode_escape": ("Invalid \\u escape", "\\u must be followed by exactly 4 hexadecimal digits.",
                               "Fix the \\u escape, e.g. \\u00e9."),
    "lone_surrogate": ("Lone surrogate", "An unpaired UTF-16 surrogate was found; it was replaced by U+FFFD.",
                       "Use a surrogate pair (\\uD83D\\uDE00) for characters outside the BMP."),
    "control_char_in_string": ("Control character in string",
                               "Raw control character U+{char_code:04X} must be escaped inside a string.",
                               "Replace it with its escape sequence."),
    "single_quote_string": ("Single-quoted string", "JSON strings must use double quotes.",
                            "Replace the single quotes with double quotes."),
    "unquoted_key": ("Unquoted object key", "Object keys must be double-quoted strings.",
                     "Wrap the key in double quotes."),
    "expected_key": ("Object key expected", "A double-quoted object key was expected here.",
                     "Add \"key\": or remove the trailing comma."),
    "missing_colon": ("Missing colon", "Expected ':' between the object key and its value.",
                      "Insert ':' after the key."),
    "missing_comma": ("Missing comma", "Expected ',' between two JSON values.",
                      "Insert ',' between the values."),
    "trailing_comma": ("Trailing comma", "Trailing commas are not allowed in standard JSON.",
                       "Remove the comma before the closing bracket, or switch to JSONC mode."),
    "bracket_mismatch": ("Bracket mismatch", "Found '{found}' but '{expected}' was expected.",
                         "Check the bracket nesting around this position."),
    "unclosed_container": ("Unclosed {container}", "This {container} is never closed.",
                           "Add the missing closing bracket."),
    "comment_not_allowed": ("Comment not allowed", "Standard JSON does not support comments.",
                            "Remove the comment, or switch the document type to JSONC."),
    "unclosed_comment": ("Unclosed block comment", "A /* comment is missing its closing */.",
                         "Add */ to close the comment."),
    "invalid_number": ("Invalid number", "Malformed number literal ({reason}).",
                       "JSON numbers look like -12, 1.5, 1e10; no leading zeros or plus sign."),
    "invalid_literal": ("Invalid literal", "'{found}' is not a valid JSON value.",
                        "Use true / false / null (lowercase), or quote it as a string."),
    "invalid_value": ("Invalid value", "Unexpected character '{found}' where a JSON value was expected.",
                      "Values are objects, arrays, strings, numbers, true, false or null."),
    "missing_value": ("Missing value", "A JSON value was expected here but '{found}' was found.",
                      "Add the missing value, or remove the trailing key/comma."),
    "extra_data": ("Extra data after top-level value",
                   "Only one top-level JSON value is allowed per document.",
                   "Remove the trailing content, or wrap the values in an array."),
    "depth_exceeded": ("Nesting too deep", "Nesting depth exceeds the limit of {limit} levels.",
                       "Raise the depth limit in Settings, or flatten the document."),
    "duplicate_key": ("Duplicate key", "Key '{key}' appears more than once in the same object.",
                      "The last occurrence wins; merge or rename the duplicates."),
    "precision_loss": ("Number precision", "The literal {raw} cannot be represented exactly as a double.",
                       "The original literal is preserved on output; be careful when converting to CSV/YAML."),
    "offsets_disabled": ("Node offsets unavailable",
                         "The document is larger than {limit} MB, so editor jump offsets were not collected.",
                         "Use the tree view to inspect large documents."),
    "out_of_memory": ("Out of memory", "The document is too large to be held in memory.",
                      "Close other documents, or process the file in smaller chunks."),
    "cancelled": ("Cancelled", "Parsing was cancelled.", ""),
}


def describe_issue(issue: "Issue") -> Tuple[str, str, str]:
    """返回 (标题, 描述, 修复建议) 的英文默认文本，供测试与回退使用。"""
    entry = ISSUE_MESSAGES.get(issue.code)
    if entry is None:
        return (issue.code, issue.code, "")
    title, message, suggestion = entry
    try:
        message = message.format(**issue.details)
    except (KeyError, IndexError, ValueError):
        pass
    try:
        suggestion = suggestion.format(**issue.details)
    except (KeyError, IndexError, ValueError):
        pass
    return title, message, suggestion


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Issue:
    """一个解析问题（错误或警告）。"""

    code: str
    severity: str
    offset: int
    length: int
    line: int
    column: int
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.severity == SEVERITY_ERROR

    def describe(self) -> Tuple[str, str, str]:
        return describe_issue(self)


@dataclass
class OffsetInfo:
    """节点在原文中的位置，用于"点击树节点跳到编辑器"。"""

    key_offset: int = -1
    key_length: int = 0
    value_offset: int = -1
    value_length: int = 0


@dataclass
class ParseStats:
    """单次解析顺带统计出的计数器（一遍扫描，零额外开销）。"""

    object_count: int = 0
    array_count: int = 0
    string_count: int = 0
    number_count: int = 0
    boolean_count: int = 0
    null_count: int = 0
    key_count: int = 0
    duplicate_key_count: int = 0
    string_chars: int = 0
    max_depth: int = 0
    max_array_length: int = 0
    comment_count: int = 0

    @property
    def node_count(self) -> int:
        return (
            self.object_count
            + self.array_count
            + self.string_count
            + self.number_count
            + self.boolean_count
            + self.null_count
        )


@dataclass
class ParseResult:
    ok: bool
    value: Any = None
    error: Optional[Issue] = None
    warnings: Tuple[Issue, ...] = ()
    stats: ParseStats = field(default_factory=ParseStats)
    offsets: Optional[Dict[Tuple, OffsetInfo]] = None
    text_length: int = 0
    cancelled: bool = False

    @property
    def issues(self) -> Tuple[Issue, ...]:
        if self.error is None:
            return self.warnings
        return (self.error,) + self.warnings


class JsonParseError(Exception):
    """内部使用的语法错误信号。"""

    def __init__(self, code: str, offset: int, length: int = 1, **details: Any) -> None:
        super().__init__(code)
        self.code = code
        self.offset = max(0, offset)
        self.length = max(0, length)
        self.details = details


class ParseCancelled(Exception):
    """用户取消解析。"""


_SIMPLE_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}

_HEX_DIGITS = set("0123456789abcdefABCDEF")
_DIGITS = set("0123456789")
_WHITESPACE = " \t\n\r"
# C 层扫描：字符串体内一直读到引号、反斜杠或控制字符为止
_STRING_BODY = re.compile(r'[^"\\\x00-\x1f]*')
_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")

_LITERAL_WORDS = {
    "true": True,
    "false": False,
    "null": None,
}

_COMMON_BAD_LITERALS = {
    "True": "Python's True is written as true in JSON.",
    "False": "Python's False is written as false in JSON.",
    "None": "Python's None is written as null in JSON.",
    "NULL": "null must be lower-case in JSON.",
    "Null": "null must be lower-case in JSON.",
    "undefined": "undefined is JavaScript, not JSON; use null.",
    "NaN": "NaN is not valid JSON; use null or a string.",
    "Infinity": "Infinity is not valid JSON; use a string or 1e999 style number.",
    "-Infinity": "Infinity is not valid JSON; use a string.",
}


class _Frame:
    __slots__ = ("kind", "container", "key", "key_offset", "key_length", "start",
                 "self_path", "key_offsets")

    def __init__(self, kind: str, container: Any, start: int, self_path: Tuple) -> None:
        self.kind = kind
        self.container = container
        self.key: Optional[str] = None
        self.key_offset = -1
        self.key_length = 0
        self.start = start
        self.self_path = self_path
        self.key_offsets: Optional[Dict[str, int]] = {} if kind == KIND_OBJECT else None

    @property
    def closer(self) -> str:
        return "}" if self.kind == KIND_OBJECT else "]"


class JsonParser:
    """把 JSON 文本解析成 Python 对象，并收集问题与统计。"""

    def __init__(
        self,
        text: str,
        mode: str = MODE_JSON,
        *,
        track_offsets: bool = False,
        max_depth: int = DEFAULT_MAX_DEPTH,
        detect_duplicate_keys: bool = True,
        progress: Optional[Callable[[int, int], None]] = None,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.text = text
        self.n = len(text)
        self.mode = MODE_JSONC if mode == MODE_JSONC else MODE_JSON
        self.max_depth = max(1, int(max_depth))
        self.detect_duplicate_keys = detect_duplicate_keys
        self.track_offsets = track_offsets and self.n <= OFFSET_TRACK_LIMIT
        self.pos = 0
        self.stats = ParseStats()
        self.warnings: List[Tuple[str, int, int, Dict[str, Any]]] = []
        self.offsets: Optional[Dict[Tuple, OffsetInfo]] = {} if self.track_offsets else None
        self.max_depth_seen = 0
        self._progress = progress
        self._cancel = cancel
        self._next_tick = _PROGRESS_STEP
        self._line_starts = [0]
        self._line_scanned = 0

    # ------------------------------------------------------------------ #
    # 位置工具
    # ------------------------------------------------------------------ #
    def line_col(self, offset: int) -> Tuple[int, int]:
        """offset -> (1-based line, 1-based column)，行索引惰性且单调构建。"""
        offset = max(0, min(offset, self.n))
        starts = self._line_starts
        i = self._line_scanned
        if i < offset:
            # 只扫描 [i, offset) 区间内的换行；扫不到也要把游标推到 offset，
            # 否则单行大文件上反复调用会退化成 O(n²)。
            while True:
                j = self.text.find("\n", i, offset)
                if j < 0:
                    i = offset
                    break
                starts.append(j + 1)
                i = j + 1
            self._line_scanned = i
        idx = bisect_right(starts, offset) - 1
        if idx < 0:
            idx = 0
        return idx + 1, offset - starts[idx] + 1

    def make_issue(self, code: str, severity: str, offset: int, length: int,
                   details: Optional[Dict[str, Any]] = None) -> Issue:
        offset = max(0, min(offset, self.n))
        line, column = self.line_col(offset)
        return Issue(code=code, severity=severity, offset=offset, length=max(0, length),
                     line=line, column=column, details=details or {})

    def _warn(self, code: str, offset: int, length: int = 1, **details: Any) -> None:
        self.warnings.append((code, offset, length, details))

    def _tick(self) -> None:
        if self.pos < self._next_tick:
            return
        self._next_tick = self.pos + _PROGRESS_STEP
        if self._cancel is not None and self._cancel():
            raise ParseCancelled()
        if self._progress is not None:
            self._progress(self.pos, self.n)

    # ------------------------------------------------------------------ #
    # 词法
    # ------------------------------------------------------------------ #
    def _skip_ws(self) -> None:
        text = self.text
        n = self.n
        i = self.pos
        while i < n:
            ch = text[i]
            if ch in _WHITESPACE:
                i += 1
                continue
            if ch == "/":
                nxt = text[i + 1 : i + 2]
                if nxt == "/":
                    if self.mode != MODE_JSONC:
                        self.pos = i
                        raise JsonParseError("comment_not_allowed", i, 2)
                    end = text.find("\n", i + 2)
                    self.stats.comment_count += 1
                    i = n if end < 0 else end + 1
                    continue
                if nxt == "*":
                    if self.mode != MODE_JSONC:
                        self.pos = i
                        raise JsonParseError("comment_not_allowed", i, 2)
                    end = text.find("*/", i + 2)
                    if end < 0:
                        self.pos = i
                        raise JsonParseError("unclosed_comment", i, n - i)
                    i = end + 2
                    self.stats.comment_count += 1
                    continue
            break
        self.pos = i
        if i >= self._next_tick:
            self._tick()

    def _parse_string(self) -> Tuple[str, int]:
        """解析字符串，返回 (值, 起始 offset)。调用时 ``self.pos`` 指向引号。"""
        text = self.text
        n = self.n
        start = self.pos
        i = start + 1
        parts: List[str] = []
        while True:
            if i >= n:
                raise JsonParseError("unclosed_string", start, n - start)
            match = _STRING_BODY.match(text, i)
            j = match.end()
            if j > i:
                parts.append(text[i:j])
            if j >= n:
                raise JsonParseError("unclosed_string", start, n - start)
            ch = text[j]
            if ch == '"':
                self.pos = j + 1
                return "".join(parts), start
            if ch == "\n" or ch == "\r":
                raise JsonParseError("unclosed_string", start, j - start + 1, reason="newline")
            if ch != "\\":
                raise JsonParseError("control_char_in_string", j, 1, char_code=ord(ch))
            # 转义
            if j + 1 >= n:
                raise JsonParseError("unclosed_string", start, n - start)
            esc = text[j + 1]
            if esc in _SIMPLE_ESCAPES:
                parts.append(_SIMPLE_ESCAPES[esc])
                i = j + 2
                continue
            if esc != "u":
                raise JsonParseError("invalid_escape", j, 2, found="\\" + esc)
            hex4 = text[j + 2 : j + 6]
            if len(hex4) != 4 or any(c not in _HEX_DIGITS for c in hex4):
                raise JsonParseError("invalid_unicode_escape", j, 6, found="\\u" + hex4)
            code = int(hex4, 16)
            i = j + 6
            if 0xD800 <= code <= 0xDBFF:
                if text[i : i + 2] == "\\u":
                    hex4b = text[i + 2 : i + 6]
                    low = int(hex4b, 16) if len(hex4b) == 4 and all(c in _HEX_DIGITS for c in hex4b) else -1
                    if 0xDC00 <= low <= 0xDFFF:
                        parts.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
                        i += 6
                        continue
                self._warn("lone_surrogate", j, 6)
                parts.append("\ufffd")
                continue
            if 0xDC00 <= code <= 0xDFFF:
                self._warn("lone_surrogate", j, 6)
                parts.append("\ufffd")
                continue
            parts.append(chr(code))

    def _parse_number(self) -> Any:
        text = self.text
        n = self.n
        start = self.pos
        i = start
        if i < n and text[i] == "-":
            i += 1
        if i >= n:
            raise JsonParseError("invalid_number", start, n - start, reason="number is truncated")
        if i < n and text[i] == "+":
            raise JsonParseError("invalid_number", start, i - start + 1,
                                 reason="a leading '+' is not allowed")
        ch = text[i]
        if ch == "0":
            i += 1
            if i < n and text[i] in _DIGITS:
                raise JsonParseError("invalid_number", start, i - start + 1,
                                     reason="leading zeros are not allowed")
        elif ch in _DIGITS:
            while i < n and text[i] in _DIGITS:
                i += 1
        else:
            reason = "missing integer part before '.'" if ch == "." else "unexpected character"
            raise JsonParseError("invalid_number", start, max(1, i - start + 1), reason=reason)
        is_float = False
        if i < n and text[i] == ".":
            is_float = True
            i += 1
            if i >= n or text[i] not in _DIGITS:
                raise JsonParseError("invalid_number", start, i - start,
                                     reason="missing fraction digits after '.'")
            while i < n and text[i] in _DIGITS:
                i += 1
        if i < n and text[i] in "eE":
            is_float = True
            j = i + 1
            if j < n and text[j] in "+-":
                j += 1
            if j >= n or text[j] not in _DIGITS:
                raise JsonParseError("invalid_number", start, j - start,
                                     reason="missing exponent digits")
            while j < n and text[j] in _DIGITS:
                j += 1
            i = j
        raw = text[start:i]
        self.pos = i
        self.stats.number_count += 1
        if not is_float:
            as_int = int(raw)
            # "-0" 这类字面量用 int 会丢掉负号，交给 JsonNumber 原样保存
            return as_int if str(as_int) == raw else JsonNumber(raw)
        number = JsonNumber(raw)
        try:
            as_float = float(raw)
        except OverflowError:
            self._warn("precision_loss", start, i - start, raw=raw, reason="overflow")
        else:
            if Decimal(repr(as_float)) != number:
                self._warn("precision_loss", start, i - start, raw=raw, reason="digits")
        return number

    def _parse_object_key(self) -> Tuple[str, int]:
        self._skip_ws()
        if self.pos >= self.n:
            raise JsonParseError("unexpected_eof", self.pos, 0, expected="object key")
        ch = self.text[self.pos]
        if ch == '"':
            return self._parse_string()
        if ch == "'":
            raise JsonParseError("single_quote_string", self.pos, 1)
        if ch in ("}", ","):
            raise JsonParseError("expected_key", self.pos, 1, found=ch)
        match = _IDENTIFIER.match(self.text, self.pos)
        if match:
            raise JsonParseError("unquoted_key", self.pos, match.end() - self.pos, key=match.group(0))
        raise JsonParseError("expected_key", self.pos, 1, found=ch)

    def _expect_colon(self) -> None:
        self._skip_ws()
        if self.pos >= self.n:
            raise JsonParseError("unexpected_eof", self.pos, 0, expected="':'")
        if self.text[self.pos] != ":":
            raise JsonParseError("missing_colon", self.pos, 1, found=self.text[self.pos])
        self.pos += 1

    def _parse_scalar(self) -> Any:
        ch = self.text[self.pos]
        if ch == '"':
            value, _ = self._parse_string()
            self.stats.string_count += 1
            self.stats.string_chars += len(value)
            return value
        if ch in "-+." or ch in _DIGITS:
            return self._parse_number()
        if ch in "tfn":
            for word, literal in _LITERAL_WORDS.items():
                if self.text.startswith(word, self.pos):
                    end = self.pos + len(word)
                    # 必须是完整单词，"nullable" 不是 null
                    if end >= self.n or not (self.text[end].isalnum() or self.text[end] == "_"):
                        self.pos = end
                        if literal is None:
                            self.stats.null_count += 1
                        else:
                            self.stats.boolean_count += 1
                        return literal
            match = _IDENTIFIER.match(self.text, self.pos)
            found = match.group(0) if match else ch
            raise JsonParseError("invalid_literal", self.pos, len(found), found=found,
                                 hint=_COMMON_BAD_LITERALS.get(found, ""))
        if ch == "'":
            raise JsonParseError("single_quote_string", self.pos, 1)
        if ch in "}]":
            raise JsonParseError("missing_value", self.pos, 1, found=ch)
        match = _IDENTIFIER.match(self.text, self.pos)
        if match:
            found = match.group(0)
            raise JsonParseError("invalid_literal", self.pos, len(found), found=found,
                                 hint=_COMMON_BAD_LITERALS.get(found, ""))
        raise JsonParseError("invalid_value", self.pos, 1, found=ch)

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def parse(self) -> Any:
        self._skip_ws()
        if self.pos >= self.n:
            raise JsonParseError("empty_input", 0, 0)

        stack: List[_Frame] = []
        path_stack: List[Any] = []
        pending: List[Tuple[Tuple, int]] = []
        value: Any = None

        while True:
            self._skip_ws()
            if self.pos >= self.n:
                raise JsonParseError("unexpected_eof", self.pos, 0, expected="value")
            value_start = self.pos
            path = tuple(path_stack)
            if self.offsets is not None:
                pending.append((path, value_start))
            ch = self.text[value_start]

            if ch == "{" or ch == "[":
                kind = KIND_OBJECT if ch == "{" else KIND_ARRAY
                if len(stack) + 1 > self.max_depth:
                    raise JsonParseError("depth_exceeded", value_start, 1, limit=self.max_depth)
                container: Any = {} if kind == KIND_OBJECT else []
                self.pos = value_start + 1
                self.max_depth_seen = max(self.max_depth_seen, len(stack) + 1)
                frame = _Frame(kind, container, value_start, path)
                self._skip_ws()
                closer = frame.closer
                if self.pos < self.n and self.text[self.pos] == closer:
                    self.pos += 1
                    value = frame.container
                    if kind == KIND_OBJECT:
                        self.stats.object_count += 1
                    else:
                        self.stats.array_count += 1
                else:
                    if kind == KIND_OBJECT:
                        key, key_offset = self._parse_object_key()
                        key_length = self.pos - key_offset
                        self._expect_colon()
                        frame.key = key
                        frame.key_offset = key_offset
                        frame.key_length = key_length
                        path_stack.append(key)
                    else:
                        path_stack.append(0)
                    stack.append(frame)
                    continue
            else:
                value = self._parse_scalar()

            # ---- 回溯：记录刚完成的值，并放进父容器 ----
            while True:
                if pending:
                    done_path, done_start = pending.pop()
                    self._record_offset(done_path, done_start, self.pos, stack)
                if not stack:
                    self._skip_ws()
                    if self.pos < self.n:
                        raise JsonParseError("extra_data", self.pos, min(32, self.n - self.pos))
                    return value

                frame = stack[-1]
                if frame.kind == KIND_OBJECT:
                    key = frame.key
                    if key in frame.container:
                        self.stats.duplicate_key_count += 1
                        if self.detect_duplicate_keys:
                            first = frame.key_offsets.get(key, -1) if frame.key_offsets else -1
                            self._warn("duplicate_key", frame.key_offset, frame.key_length,
                                       key=key, first_offset=first, path=frame.self_path + (key,))
                    if frame.key_offsets is not None:
                        frame.key_offsets[key] = frame.key_offset
                    frame.container[key] = value
                    self.stats.key_count += 1
                else:
                    frame.container.append(value)

                self._skip_ws()
                if self.pos >= self.n:
                    raise JsonParseError("unclosed_container", frame.start, 1,
                                         container=frame.kind, expected=frame.closer)
                ch = self.text[self.pos]

                if ch == ",":
                    comma_offset = self.pos
                    self.pos += 1
                    self._skip_ws()
                    if self.pos >= self.n:
                        raise JsonParseError("unexpected_eof", self.pos, 0, expected="value")
                    nxt = self.text[self.pos]
                    if nxt == frame.closer:
                        if self.mode == MODE_JSONC:
                            self.pos += 1
                            value = self._close_frame(frame, stack, path_stack)
                            continue
                        raise JsonParseError("trailing_comma", comma_offset, 1, container=frame.kind)
                    if frame.kind == KIND_OBJECT:
                        key, key_offset = self._parse_object_key()
                        key_length = self.pos - key_offset
                        self._expect_colon()
                        frame.key = key
                        frame.key_offset = key_offset
                        frame.key_length = key_length
                        path_stack[-1] = key
                    else:
                        path_stack[-1] = len(frame.container)
                    break

                if ch == frame.closer:
                    self.pos += 1
                    value = self._close_frame(frame, stack, path_stack)
                    continue

                if ch in "}]":
                    raise JsonParseError("bracket_mismatch", self.pos, 1, found=ch,
                                         expected=frame.closer)
                raise JsonParseError("missing_comma", self.pos, 1, container=frame.kind, found=ch)

    def _record_offset(self, path: Tuple, value_start: int, value_end: int,
                       stack: List[_Frame]) -> None:
        """记录节点在原文中的位置，供"点击树节点跳到编辑器"使用。"""
        if self.offsets is None:
            return
        parent = stack[-1] if stack else None
        if parent is not None and parent.kind == KIND_OBJECT:
            key_offset = parent.key_offset
            key_length = parent.key_length
        else:
            key_offset = -1
            key_length = 0
        self.offsets[path] = OffsetInfo(
            key_offset=key_offset,
            key_length=key_length,
            value_offset=value_start,
            value_length=max(0, value_end - value_start),
        )

    def _close_frame(self, frame: _Frame, stack: List[_Frame], path_stack: List[Any]) -> Any:
        stack.pop()
        path_stack.pop()
        if frame.kind == KIND_OBJECT:
            self.stats.object_count += 1
        else:
            self.stats.array_count += 1
            self.stats.max_array_length = max(self.stats.max_array_length, len(frame.container))
        return frame.container


def parse_json(
    text: str,
    mode: str = MODE_JSON,
    *,
    track_offsets: bool = False,
    max_depth: int = DEFAULT_MAX_DEPTH,
    detect_duplicate_keys: bool = True,
    progress: Optional[Callable[[int, int], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> ParseResult:
    """解析 JSON 文本。任何输入都不会抛出异常，问题通过 :class:`ParseResult` 返回。"""
    if not isinstance(text, str):
        text = str(text)
    parser = JsonParser(
        text,
        mode,
        track_offsets=track_offsets,
        max_depth=max_depth,
        detect_duplicate_keys=detect_duplicate_keys,
        progress=progress,
        cancel=cancel,
    )
    result = ParseResult(ok=False, text_length=len(text))
    try:
        result.value = parser.parse()
        result.ok = True
    except ParseCancelled:
        result.cancelled = True
        result.error = parser.make_issue("cancelled", SEVERITY_ERROR, parser.pos, 0)
    except JsonParseError as exc:
        details = dict(exc.details)
        hint = details.pop("hint", "")
        if hint:
            details["suggestion"] = hint
        result.error = parser.make_issue(exc.code, SEVERITY_ERROR, exc.offset, exc.length, details)
    except RecursionError:  # pragma: no cover - 迭代解析下不应发生
        result.error = parser.make_issue("depth_exceeded", SEVERITY_ERROR, parser.pos, 1,
                                         {"limit": max_depth})
    except MemoryError:  # pragma: no cover
        result.error = parser.make_issue("out_of_memory", SEVERITY_ERROR, parser.pos, 0, {})

    warnings: List[Issue] = [
        parser.make_issue(code, SEVERITY_WARNING, offset, length, details)
        for code, offset, length, details in parser.warnings
    ]
    if parser.track_offsets and parser.offsets is not None:
        result.offsets = parser.offsets
    elif track_offsets:
        warnings.append(
            parser.make_issue("offsets_disabled", SEVERITY_WARNING, 0, 0,
                              {"limit": OFFSET_TRACK_LIMIT // (1024 * 1024)})
        )
    result.warnings = tuple(warnings)
    result.stats = parser.stats
    result.stats.max_depth = parser.max_depth_seen
    return result

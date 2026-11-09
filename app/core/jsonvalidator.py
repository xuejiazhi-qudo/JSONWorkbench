"""JSON 校验：把解析器的问题整理成可展示、可跳转、可修复的结果。

修复建议遵循一条铁律：**绝不擅自改动用户数据**。:func:`propose_fix` 只生成
建议，:func:`apply_fix` 只在 UI 明确确认后调用。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .jsonparser import (
    ISSUE_MESSAGES,
    MODE_JSON,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    Issue,
    ParseResult,
    ParseStats,
    describe_issue,
    parse_json,
)
from .jsonvalue import encode_json_string

__all__ = [
    "Fix",
    "FixResult",
    "ValidationResult",
    "apply_fix",
    "auto_fix",
    "issue_codes",
    "propose_fix",
    "quick_check",
    "validate_text",
]

_LITERAL_FIXES = {
    "True": "true",
    "TRUE": "true",
    "False": "false",
    "FALSE": "false",
    "None": "null",
    "NULL": "null",
    "Null": "null",
    "undefined": "null",
}

_CONTROL_ESCAPES = {
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}

#: 没有可靠自动修复方式的错误码（只给建议，不动数据）
NO_FIX_CODES = frozenset({
    "empty_input",
    "extra_data",
    "depth_exceeded",
    "invalid_escape",
    "invalid_unicode_escape",
    "invalid_number",
    "precision_loss",
    "duplicate_key",
    "lone_surrogate",
    "offsets_disabled",
    "out_of_memory",
    "cancelled",
})


@dataclass
class ValidationResult:
    """一次校验的完整结果。"""

    valid: bool
    value: Any = None
    errors: Tuple[Issue, ...] = ()
    warnings: Tuple[Issue, ...] = ()
    parse: Optional[ParseResult] = None
    stats: ParseStats = field(default_factory=ParseStats)
    mode: str = MODE_JSON
    text_length: int = 0
    elapsed_ms: float = 0.0
    cancelled: bool = False

    @property
    def issues(self) -> Tuple[Issue, ...]:
        return self.errors + self.warnings

    @property
    def error(self) -> Optional[Issue]:
        return self.errors[0] if self.errors else None

    @property
    def duplicate_key_count(self) -> int:
        return sum(1 for w in self.warnings if w.code == "duplicate_key")

    def describe(self, issue: Issue) -> Tuple[str, str, str]:
        return describe_issue(issue)


def validate_text(text: str, mode: str = MODE_JSON, *,
                  max_depth: int = 1000,
                  detect_duplicate_keys: bool = True,
                  track_offsets: bool = False,
                  progress=None,
                  cancel=None) -> ValidationResult:
    """校验文本。任何输入都不会抛异常。"""
    started = time.perf_counter()
    parsed = parse_json(text, mode, max_depth=max_depth,
                        detect_duplicate_keys=detect_duplicate_keys,
                        track_offsets=track_offsets,
                        progress=progress, cancel=cancel)
    errors = (parsed.error,) if parsed.error is not None else ()
    result = ValidationResult(
        valid=parsed.ok,
        value=parsed.value,
        errors=errors,
        warnings=parsed.warnings,
        parse=parsed,
        stats=parsed.stats,
        mode=mode,
        text_length=len(text),
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
        cancelled=parsed.cancelled,
    )
    return result


def quick_check(text: str, mode: str = MODE_JSON) -> Tuple[bool, Optional[str], int, int]:
    """状态栏用的快速检查，返回 (是否合法, 错误码, 行, 列)。"""
    result = validate_text(text, mode)
    issue = result.error
    if issue is None:
        return True, None, 0, 0
    return False, issue.code, issue.line, issue.column


def issue_codes() -> Tuple[str, ...]:
    """所有已知问题码，供 i18n 完整性测试使用。"""
    return tuple(sorted(ISSUE_MESSAGES))


# --------------------------------------------------------------------------- #
# 修复建议
# --------------------------------------------------------------------------- #
@dataclass
class Fix:
    """一处可预览、可确认的文本修改。"""

    code: str
    offset: int
    length: int          # 被替换掉的原文长度
    insert: str          # 替换进去的文本
    label: str           # 英文动作描述，UI 负责翻译
    detail: str = ""     # 补充说明

    @property
    def is_noop(self) -> bool:
        return self.length == 0 and not self.insert


def apply_fix(text: str, fix: Fix) -> str:
    """把 :class:`Fix` 应用到文本上，返回新文本（原字符串不可变，天然安全）。"""
    if fix.is_noop:
        return text
    start = max(0, min(fix.offset, len(text)))
    end = max(start, min(start + fix.length, len(text)))
    return text[:start] + fix.insert + text[end:]


def propose_fix(text: str, issue: Issue) -> Optional[Fix]:
    """为可安全修复的问题生成建议；无法可靠修复时返回 ``None``。"""
    if issue.code in NO_FIX_CODES:
        return None
    handler = _FIX_HANDLERS.get(issue.code)
    if handler is None:
        return None
    try:
        fix = handler(text, issue)
    except (IndexError, ValueError):  # pragma: no cover - 防御
        return None
    if fix is None or fix.is_noop:
        return None
    return fix


def _fix_trailing_comma(text: str, issue: Issue) -> Optional[Fix]:
    if text[issue.offset : issue.offset + 1] != ",":
        return None
    return Fix(issue.code, issue.offset, 1, "", "Remove the trailing comma")


def _fix_unquoted_key(text: str, issue: Issue) -> Optional[Fix]:
    key = issue.details.get("key")
    if not key:
        return None
    return Fix(issue.code, issue.offset, issue.length, encode_json_string(key),
               "Wrap the object key in double quotes", f"{key} -> \"{key}\"")


def _fix_single_quote(text: str, issue: Issue) -> Optional[Fix]:
    """把单引号字符串改成双引号字符串，并处理内部转义。"""
    if text[issue.offset : issue.offset + 1] != "'":
        return None
    i = issue.offset + 1
    body: List[str] = []
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "'":
                body.append("'")       # \' -> '
            else:
                body.append("\\" + nxt)
            i += 2
            continue
        if ch == "'":
            replacement = '"' + "".join(body).replace('"', '\\"') + '"'
            return Fix(issue.code, issue.offset, i - issue.offset + 1, replacement,
                       "Convert the single-quoted string to double quotes")
        if ch == "\n":
            return None
        body.append(ch)
        i += 1
    return None


def _fix_comment(text: str, issue: Issue) -> Optional[Fix]:
    marker = text[issue.offset : issue.offset + 2]
    if marker == "//":
        end = text.find("\n", issue.offset)
        stop = len(text) if end < 0 else end
        return Fix(issue.code, issue.offset, stop - issue.offset, "",
                   "Delete the line comment")
    if marker == "/*":
        end = text.find("*/", issue.offset + 2)
        stop = len(text) if end < 0 else end + 2
        return Fix(issue.code, issue.offset, stop - issue.offset, "",
                   "Delete the block comment")
    return None


def _fix_unclosed_comment(text: str, issue: Issue) -> Optional[Fix]:
    return Fix(issue.code, len(text), 0, "*/", "Close the block comment with */")


def _fix_invalid_literal(text: str, issue: Issue) -> Optional[Fix]:
    found = text[issue.offset : issue.offset + issue.length]
    replacement = _LITERAL_FIXES.get(found)
    if replacement is None:
        return None
    return Fix(issue.code, issue.offset, issue.length, replacement,
               f"Replace '{found}' with JSON's '{replacement}'",
               issue.details.get("suggestion", ""))


def _fix_missing_colon(text: str, issue: Issue) -> Optional[Fix]:
    return Fix(issue.code, issue.offset, 0, ":", "Insert ':' after the object key")


def _fix_missing_comma(text: str, issue: Issue) -> Optional[Fix]:
    return Fix(issue.code, issue.offset, 0, ",", "Insert ',' between the two values")


def _fix_bracket_mismatch(text: str, issue: Issue) -> Optional[Fix]:
    expected = issue.details.get("expected")
    if expected not in ("}", "]"):
        return None
    return Fix(issue.code, issue.offset, 1, expected,
               f"Replace '{text[issue.offset]}' with '{expected}'")


def _fix_unclosed_string(text: str, issue: Issue) -> Optional[Fix]:
    end = issue.offset + issue.length
    if issue.details.get("reason") == "newline":
        # 引号缺失在换行前：把结束引号补在换行处
        return Fix(issue.code, end - 1, 0, '"', "Close the string with a double quote")
    if end >= len(text):
        return Fix(issue.code, len(text), 0, '"', "Close the string with a double quote")
    return None


def _fix_control_char(text: str, issue: Issue) -> Optional[Fix]:
    ch = text[issue.offset : issue.offset + 1]
    if not ch or ord(ch) >= 0x20:
        return None
    escaped = _CONTROL_ESCAPES.get(ch) or f"\\u{ord(ch):04x}"
    return Fix(issue.code, issue.offset, 1, escaped,
               f"Escape the control character as {escaped}")


def _fix_missing_value(text: str, issue: Issue) -> Optional[Fix]:
    return Fix(issue.code, issue.offset, 0, "null",
               "Insert null as the missing value")


_FIX_HANDLERS = {
    "trailing_comma": _fix_trailing_comma,
    "unquoted_key": _fix_unquoted_key,
    "single_quote_string": _fix_single_quote,
    "comment_not_allowed": _fix_comment,
    "unclosed_comment": _fix_unclosed_comment,
    "invalid_literal": _fix_invalid_literal,
    "missing_colon": _fix_missing_colon,
    "missing_comma": _fix_missing_comma,
    "bracket_mismatch": _fix_bracket_mismatch,
    "unclosed_string": _fix_unclosed_string,
    "control_char_in_string": _fix_control_char,
    "missing_value": _fix_missing_value,
}


@dataclass
class FixResult:
    """批量修复的结果（UI 必须先展示 diff 再让用户确认）。"""

    ok: bool
    text: str
    applied: Tuple[Fix, ...] = ()
    remaining: Optional[Issue] = None
    unchanged: bool = True
    rounds: int = 0


def auto_fix(text: str, mode: str = MODE_JSON, *, max_rounds: int = 50,
             max_depth: int = 1000) -> FixResult:
    """反复应用可安全修复的建议，直到文档合法或无建议可用。

    只处理语法类问题；重复键、精度警告这类"会改变数据含义"的问题不动。
    """
    applied: List[Fix] = []
    current = text
    rounds = 0
    while rounds < max_rounds:
        rounds += 1
        result = validate_text(current, mode, max_depth=max_depth)
        if result.valid:
            return FixResult(ok=True, text=current, applied=tuple(applied),
                             unchanged=current == text, rounds=rounds)
        issue = result.error
        fix = propose_fix(current, issue) if issue is not None else None
        if fix is None:
            return FixResult(ok=False, text=current, applied=tuple(applied),
                             remaining=issue, unchanged=current == text, rounds=rounds)
        candidate = apply_fix(current, fix)
        if candidate == current:  # pragma: no cover - 防御：修复没有效果
            return FixResult(ok=False, text=current, applied=tuple(applied),
                             remaining=issue, unchanged=current == text, rounds=rounds)
        current = candidate
        applied.append(fix)
    result = validate_text(current, mode, max_depth=max_depth)
    return FixResult(ok=result.valid, text=current, applied=tuple(applied),
                     remaining=result.error, unchanged=current == text, rounds=rounds)


def summarize_warnings(warnings: Tuple[Issue, ...]) -> Dict[str, int]:
    """按问题码统计警告数量，供状态栏/校验页汇总。"""
    counts: Dict[str, int] = {}
    for warning in warnings:
        counts[warning.code] = counts.get(warning.code, 0) + 1
    return counts


def severity_of(issue: Issue) -> str:
    return SEVERITY_ERROR if issue.is_error else SEVERITY_WARNING

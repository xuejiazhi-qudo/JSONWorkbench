"""核心层：纯 Python 业务逻辑，不依赖任何 Qt 部件，可独立单测。"""

from .jsonparser import (
    Issue,
    MODE_JSON,
    MODE_JSONC,
    ParseResult,
    ParseStats,
    describe_issue,
    parse_json,
)
from .jsonvalue import JsonNumber, json_type, number_literal

__all__ = [
    "Issue",
    "JsonNumber",
    "MODE_JSON",
    "MODE_JSONC",
    "ParseResult",
    "ParseStats",
    "describe_issue",
    "json_type",
    "number_literal",
    "parse_json",
]

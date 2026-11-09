"""JSON 值模型与数字精度保真。

解析结果使用原生 Python 容器：``dict``（保持插入顺序）、``list``、``str``、
``bool``、``None``、``int``（整数字面量，任意精度）以及 :class:`JsonNumber`
（含小数/指数的字面量，继承 ``Decimal`` 并携带原始文本）。

这样设计的目的是：格式化、压缩、排序往返之后，``1.230``、``1e5``、
``12345678901234567890123456789`` 等字面量必须原样输出，绝不出现
``0.30000000000000004`` 或 ``1.0e+25`` 之类的精度损坏。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

TYPE_OBJECT = "object"
TYPE_ARRAY = "array"
TYPE_STRING = "string"
TYPE_NUMBER = "number"
TYPE_BOOLEAN = "boolean"
TYPE_NULL = "null"

ALL_TYPES = (
    TYPE_OBJECT,
    TYPE_ARRAY,
    TYPE_STRING,
    TYPE_NUMBER,
    TYPE_BOOLEAN,
    TYPE_NULL,
)

__all__ = [
    "JsonNumber",
    "ALL_TYPES",
    "TYPE_ARRAY",
    "TYPE_BOOLEAN",
    "TYPE_NULL",
    "TYPE_NUMBER",
    "TYPE_OBJECT",
    "TYPE_STRING",
    "decode_json_string",
    "encode_json_string",
    "is_number",
    "json_type",
    "number_literal",
    "to_plain",
    "type_name",
]


class JsonNumber(Decimal):
    """保留原始字面量的小数/科学计数法数字。

    继承 ``Decimal`` 因此可直接参与比较、排序与算术；``raw`` 保存解析时的
    原文，序列化时优先输出 ``raw``。
    """

    __slots__ = ("raw",)

    def __new__(cls, raw: str) -> "JsonNumber":
        obj = super().__new__(cls, raw)
        obj.raw = raw
        return obj

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"JsonNumber({self.raw!r})"


def is_number(value: Any) -> bool:
    """``True`` 表示 JSON number（bool 不算）。"""
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def json_type(value: Any) -> str:
    """返回 JSON 类型名，顺序很重要：bool 是 int 的子类，必须先判断。"""
    if value is None:
        return TYPE_NULL
    if isinstance(value, bool):
        return TYPE_BOOLEAN
    if isinstance(value, str):
        return TYPE_STRING
    if isinstance(value, (int, float, Decimal)):
        return TYPE_NUMBER
    if isinstance(value, dict):
        return TYPE_OBJECT
    if isinstance(value, (list, tuple)):
        return TYPE_ARRAY
    return TYPE_STRING


_TYPE_LABELS = {
    TYPE_OBJECT: "Object",
    TYPE_ARRAY: "Array",
    TYPE_STRING: "String",
    TYPE_NUMBER: "Number",
    TYPE_BOOLEAN: "Boolean",
    TYPE_NULL: "Null",
}


def type_name(value: Any) -> str:
    return _TYPE_LABELS[json_type(value)]


def number_literal(value: Any) -> str:
    """把数字转成 JSON 合法字面量，尽量保持原文。"""
    if isinstance(value, JsonNumber):
        return value.raw
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return _decimal_literal(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("JSON does not support NaN/Infinity")
        return repr(value)
    raise TypeError(f"not a number: {value!r}")


def _decimal_literal(value: Decimal) -> str:
    """Decimal -> JSON 合法字面量（Decimal 默认会输出 ``1E+5`` 这种形式）。"""
    if value.is_nan() or value.is_infinite():
        raise ValueError("JSON does not support NaN/Infinity")
    text = str(value)
    if "e" not in text and "E" not in text:
        return text
    # 统一成小写 e，并去掉指数里的 "+"，保证是合法 JSON 数字。
    mantissa, _, exp = text.replace("e", "E").partition("E")
    return f"{mantissa}e{int(exp)}"


def encode_json_string(text: str, ensure_ascii: bool = False) -> str:
    """按 RFC 8259 转义字符串并加上双引号。"""
    out = ['"']
    for ch in text:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        elif ensure_ascii and code > 0x7E:
            if code > 0xFFFF:
                code -= 0x10000
                out.append(f"\\u{0xD800 + (code >> 10):04x}\\u{0xDC00 + (code & 0x3FF):04x}")
            else:
                out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


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


def decode_json_string(body: str) -> str:
    """解码字符串内部（不含首尾引号）的转义序列。

    仅用于已经通过校验的文本；非法转义会抛 ``ValueError``。
    """
    if "\\" not in body:
        return body
    out = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= n:
            raise ValueError("truncated escape")
        esc = body[i]
        if esc in _SIMPLE_ESCAPES:
            out.append(_SIMPLE_ESCAPES[esc])
            i += 1
            continue
        if esc != "u":
            raise ValueError(f"invalid escape: \\{esc}")
        hex4 = body[i + 1 : i + 5]
        if len(hex4) != 4:
            raise ValueError("truncated unicode escape")
        try:
            code = int(hex4, 16)
        except ValueError as exc:
            raise ValueError(f"invalid unicode escape: \\u{hex4}") from exc
        i += 5
        if 0xD800 <= code <= 0xDBFF:
            if body[i : i + 2] == "\\u":
                hex4b = body[i + 2 : i + 6]
                try:
                    low = int(hex4b, 16)
                except ValueError:
                    low = -1
                if 0xDC00 <= low <= 0xDFFF:
                    code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                    i += 6
                else:
                    code = 0xFFFD
            else:
                code = 0xFFFD
        elif 0xDC00 <= code <= 0xDFFF:
            code = 0xFFFD
        out.append(chr(code))
    return "".join(out)


def to_plain(value: Any) -> Any:
    """把 :class:`JsonNumber` 递归替换成 ``int``/``float``，供外部库使用。

    转换可能损失精度，调用方（如导出 CSV/YAML）应自行提示。
    """
    if isinstance(value, JsonNumber):
        try:
            as_int = value.to_integral_value()
        except (InvalidOperation, ValueError):
            return float(value)
        if as_int == value and "e" not in value.raw.lower() and "." not in value.raw:
            return int(as_int)
        return float(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: to_plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_plain(v) for v in value]
    return value

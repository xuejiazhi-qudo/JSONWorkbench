"""解包"被字符串包裹的 JSON"（双重/多重编码）。

典型输入（网关、日志、消息队列里非常常见）::

    "{\\"data\\":{\\"id\\":1},\\"code\\":0}"

它本身是**合法 JSON**（一个字符串），但用户真正想要的是里面那层 JSON。
本模块负责识别并逐层解包：

* 只接受"外层解析成功且根是字符串、字符串内容又能解析成 JSON"的情况；
* 保留内层**原始文本**（不重新序列化），因此数字字面量、键顺序都不变；
* 支持多层包裹（字符串里还是字符串里才是 JSON），层数有上限；
* 解包是显式动作：UI 必须先询问用户，绝不自动改写文档。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .jsonparser import MODE_JSON, ParseResult, parse_json
from .jsonvalue import json_type

DEFAULT_MAX_LAYERS = 8

__all__ = ["DEFAULT_MAX_LAYERS", "UnwrapResult", "detect_wrapped", "unwrap"]


@dataclass
class UnwrapResult:
    ok: bool
    text: str = ""            # 解包后的原始文本（未格式化）
    layers: int = 0           # 实际剥掉的字符串层数
    root_type: str = ""       # 解包后根节点的 JSON 类型
    wrapped: bool = False     # 原文是否确实是"字符串包裹 JSON"
    error: str = ""           # 失败原因（机器可读 code）
    outer: Optional[ParseResult] = None


def detect_wrapped(text: str, mode: str = MODE_JSON) -> bool:
    """快速判断：文档是否是"字符串包裹的 JSON"。"""
    return unwrap(text, mode=mode, max_layers=1).wrapped


def unwrap(text: str, mode: str = MODE_JSON, *,
           max_layers: int = DEFAULT_MAX_LAYERS) -> UnwrapResult:
    """逐层解包；``max_layers`` 防止恶意/异常输入无限循环。"""
    if not isinstance(text, str):
        return UnwrapResult(ok=False, error="empty_input")
    current = text
    outer = parse_json(current, mode)
    if not outer.ok:
        return UnwrapResult(ok=False, error=outer.error.code if outer.error else "parse_error",
                            outer=outer)
    layers = 0
    value = outer.value
    while layers < max_layers and isinstance(value, str):
        inner = parse_json(value, mode)
        if not inner.ok:
            break
        current = value          # 保留内层原始文本，字面量不丢
        value = inner.value
        layers += 1
    if layers == 0:
        return UnwrapResult(ok=True, text=current, layers=0,
                            root_type=json_type(value), wrapped=False, outer=outer)
    return UnwrapResult(ok=True, text=current, layers=layers,
                        root_type=json_type(value), wrapped=True, outer=outer)

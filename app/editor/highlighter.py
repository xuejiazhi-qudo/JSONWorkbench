"""JSON 语法高亮。

性能约束：单个超长行（压缩过的 JSON 常常是一整行几 MB）会让逐 token 上色卡死界面，
因此超过 :attr:`MAX_BLOCK_CHARS` 的块直接跳过上色；整份文档超过阈值时由编辑器
调用 :meth:`set_enabled` 关闭高亮，并在状态栏提示用户。
"""

from __future__ import annotations

import re
from typing import Optional

from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat

#: 超过该长度的单行不做 token 级高亮
MAX_BLOCK_CHARS = 60_000

_PATTERN = re.compile(
    r"(?P<comment>//[^\n]*|/\*.*?\*/|/\*.*$)"
    r"|(?P<key>\"(?:[^\"\\]|\\.)*\"(?=\s*:))"
    r"|(?P<string>\"(?:[^\"\\]|\\.)*\")"
    r"|(?P<number>-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)"
    r"|(?P<bool>\b(?:true|false)\b)"
    r"|(?P<null>\bnull\b)"
    r"|(?P<punct>[{}\[\],:])"
)

_GROUPS = ("comment", "key", "string", "number", "bool", "null", "punct")

_STATE_COMMENT = 1


class JsonHighlighter(QSyntaxHighlighter):
    """按 token 类型着色的高亮器，颜色跟随主题。"""

    def __init__(self, document, colors: Optional[dict] = None) -> None:
        super().__init__(document)
        self._formats = {name: QTextCharFormat() for name in _GROUPS}
        self._enabled = True
        self._max_block_chars = MAX_BLOCK_CHARS
        if colors:
            self.set_colors(colors)

    # ------------------------------------------------------------------ 配置
    def set_colors(self, colors: dict, font: Optional[QFont] = None) -> None:
        """colors 来自 :meth:`Palette.as_tokens`（大写键）。"""
        token_map = {
            "comment": "SYN_COMMENT",
            "key": "SYN_KEY",
            "string": "SYN_STRING",
            "number": "SYN_NUMBER",
            "bool": "SYN_BOOL",
            "null": "SYN_NULL",
            "punct": "SYN_PUNCT",
        }

        def make(group: str, *, bold: bool = False, italic: bool = False) -> QTextCharFormat:
            fmt = QTextCharFormat()
            value = colors.get(token_map[group])
            if value:
                fmt.setForeground(QColor(value))
            if bold:
                fmt.setFontWeight(QFont.DemiBold)
            if italic:
                fmt.setFontItalic(True)
            return fmt

        self._formats["comment"] = make("comment", italic=True)
        self._formats["key"] = make("key", bold=True)
        self._formats["string"] = make("string")
        self._formats["number"] = make("number")
        self._formats["bool"] = make("bool")
        self._formats["null"] = make("null")
        self._formats["punct"] = make("punct")
        self.rehighlight()

    def set_enabled(self, enabled: bool) -> None:
        if self._enabled == enabled:
            return
        self._enabled = enabled
        self.rehighlight()

    def is_enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------ 上色
    def highlightBlock(self, text: str) -> None:  # noqa: N802 (Qt 命名)
        self.setCurrentBlockState(0)
        if not self._enabled or not text:
            return
        if len(text) > self._max_block_chars:
            return

        start = 0
        if self.previousBlockState() == _STATE_COMMENT:
            end = text.find("*/")
            if end < 0:
                self.setFormat(0, len(text), self._formats["comment"])
                self.setCurrentBlockState(_STATE_COMMENT)
                return
            self.setFormat(0, end + 2, self._formats["comment"])
            start = end + 2

        for match in _PATTERN.finditer(text, start):
            for name in _GROUPS:
                span = match.span(name)
                if span[0] == -1:
                    continue
                self.setFormat(span[0], span[1] - span[0], self._formats[name])
                if name == "comment":
                    token = match.group(name)
                    if token.startswith("/*") and not token.endswith("*/"):
                        self.setCurrentBlockState(_STATE_COMMENT)
                break

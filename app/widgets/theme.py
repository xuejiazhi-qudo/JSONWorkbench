"""主题管理：深色/浅色双主题，QSS 由同一份模板 + 调色板生成。

设计参考 VS Code / JetBrains 的现代开发者工具观感：扁平、细边框、
圆角卡片、蓝紫色主色、克制的留白。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from app.utils.resources import read_style_template

THEME_DARK = "dark"
THEME_LIGHT = "light"
THEMES = (THEME_DARK, THEME_LIGHT)

ACCENT = "#4C8DFF"

__all__ = [
    "ACCENT",
    "Palette",
    "THEME_DARK",
    "THEME_LIGHT",
    "THEMES",
    "ThemeManager",
    "theme",
]


@dataclass(frozen=True)
class Palette:
    """一套配色。所有 QSS 令牌都来自这里。"""

    name: str
    bg: str                # 窗口背景
    panel: str             # 卡片/面板背景
    panel_alt: str         # 次级面板（侧边栏、表头）
    raised: str            # 输入框等凹陷控件背景
    border: str
    border_strong: str
    text: str
    text_muted: str
    text_disabled: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_text: str
    accent_soft: str       # 选中项的柔和底色
    danger: str
    warning: str
    success: str
    selection_bg: str
    selection_text: str
    hover: str
    gutter_bg: str
    gutter_text: str
    current_line: str
    error_bg: str
    diff_add: str
    diff_del: str
    diff_mod: str
    scroll: str
    scroll_hover: str
    shadow: str
    # 语法高亮
    syn_key: str
    syn_string: str
    syn_number: str
    syn_bool: str
    syn_null: str
    syn_punct: str
    syn_comment: str

    def as_tokens(self) -> Dict[str, str]:
        return {
            "BG": self.bg,
            "PANEL": self.panel,
            "PANEL_ALT": self.panel_alt,
            "RAISED": self.raised,
            "BORDER": self.border,
            "BORDER_STRONG": self.border_strong,
            "TEXT": self.text,
            "MUTED": self.text_muted,
            "DISABLED": self.text_disabled,
            "ACCENT": self.accent,
            "ACCENT_HOVER": self.accent_hover,
            "ACCENT_PRESSED": self.accent_pressed,
            "ACCENT_TEXT": self.accent_text,
            "ACCENT_SOFT": self.accent_soft,
            "DANGER": self.danger,
            "WARNING": self.warning,
            "SUCCESS": self.success,
            "SELECTION_BG": self.selection_bg,
            "SELECTION_TEXT": self.selection_text,
            "HOVER": self.hover,
            "GUTTER_BG": self.gutter_bg,
            "GUTTER_TEXT": self.gutter_text,
            "CURRENT_LINE": self.current_line,
            "ERROR_BG": self.error_bg,
            "DIFF_ADD": self.diff_add,
            "DIFF_DEL": self.diff_del,
            "DIFF_MOD": self.diff_mod,
            "SCROLL": self.scroll,
            "SCROLL_HOVER": self.scroll_hover,
            "SHADOW": self.shadow,
            "SYN_KEY": self.syn_key,
            "SYN_STRING": self.syn_string,
            "SYN_NUMBER": self.syn_number,
            "SYN_BOOL": self.syn_bool,
            "SYN_NULL": self.syn_null,
            "SYN_PUNCT": self.syn_punct,
            "SYN_COMMENT": self.syn_comment,
        }

    def qcolor(self, token: str) -> QColor:
        return QColor(self.as_tokens().get(token, self.text))


DARK_PALETTE = Palette(
    name=THEME_DARK,
    bg="#1B1C21",
    panel="#22242A",
    panel_alt="#1F2126",
    raised="#191A1F",
    border="#31343C",
    border_strong="#3D414B",
    text="#E6E8EC",
    text_muted="#9AA1AC",
    text_disabled="#5C626C",
    accent="#4C8DFF",
    accent_hover="#5F9BFF",
    accent_pressed="#3B7BE8",
    accent_text="#FFFFFF",
    accent_soft="#24344F",
    danger="#F2555A",
    warning="#E8A33D",
    success="#3FBF7F",
    selection_bg="#2C4A78",
    selection_text="#FFFFFF",
    hover="#2A2D35",
    gutter_bg="#1F2126",
    gutter_text="#5F6672",
    current_line="#26282F",
    error_bg="#3A2226",
    diff_add="#1F3A2A",
    diff_del="#3E2326",
    diff_mod="#3A3320",
    scroll="#3A3E48",
    scroll_hover="#4C515D",
    shadow="#000000",
    syn_key="#7FB4FF",
    syn_string="#9CDC8B",
    syn_number="#D7A05F",
    syn_bool="#C586C0",
    syn_null="#8A8F98",
    syn_punct="#B9BEC7",
    syn_comment="#6A7079",
)

LIGHT_PALETTE = Palette(
    name=THEME_LIGHT,
    bg="#F5F6F8",
    panel="#FFFFFF",
    panel_alt="#EEF0F4",
    raised="#FFFFFF",
    border="#DCE0E6",
    border_strong="#C4C9D2",
    text="#1F2328",
    text_muted="#5C636E",
    text_disabled="#9AA1AC",
    accent="#2F6FE4",
    accent_hover="#3B7BE8",
    accent_pressed="#2559BE",
    accent_text="#FFFFFF",
    accent_soft="#DCE7FB",
    danger="#D33A3F",
    warning="#B87514",
    success="#1E8A55",
    selection_bg="#B9D2F7",
    selection_text="#10233F",
    hover="#E8EBF0",
    gutter_bg="#F2F3F6",
    gutter_text="#9AA1AC",
    current_line="#F1F4F9",
    error_bg="#FBE3E4",
    diff_add="#DFF3E4",
    diff_del="#FBE0E1",
    diff_mod="#FDF2D8",
    scroll="#C7CCD4",
    scroll_hover="#AEB4BE",
    shadow="#8A9099",
    syn_key="#1F5FD0",
    syn_string="#1E7A3C",
    syn_number="#A2560A",
    syn_bool="#8A2BE2",
    syn_null="#6B7280",
    syn_punct="#4A4F58",
    syn_comment="#8A9099",
)

_PALETTES = {THEME_DARK: DARK_PALETTE, THEME_LIGHT: LIGHT_PALETTE}

_UI_FONT = "Segoe UI, Microsoft YaHei UI, Microsoft YaHei, PingFang SC, Noto Sans CJK SC, sans-serif"
_MONO_FONT = ("Cascadia Mono, Consolas, JetBrains Mono, SF Mono, Menlo, "
              "Microsoft YaHei Mono, monospace")


class ThemeManager(QObject):
    """全局主题状态；切换时发信号，界面自行刷新（图标需要重新着色）。"""

    theme_changed = Signal(str)

    def __init__(self, initial: str = THEME_DARK) -> None:
        super().__init__()
        self._theme = initial if initial in _PALETTES else THEME_DARK
        self._cache: Dict[str, str] = {}
        self._template: Optional[str] = None

    @property
    def name(self) -> str:
        return self._theme

    def palette(self) -> Palette:
        return _PALETTES[self._theme]

    def is_dark(self) -> bool:
        return self._theme == THEME_DARK

    def icon_color(self, kind: str = "text") -> str:
        """图标着色，供 :mod:`app.widgets.icons` 使用。"""
        palette = self.palette()
        return {
            "text": palette.text,
            "muted": palette.text_muted,
            "accent": palette.accent,
            "danger": palette.danger,
            "success": palette.success,
            "warning": palette.warning,
            "on_accent": palette.accent_text,
        }.get(kind, palette.text)

    def stylesheet(self) -> str:
        if self._theme in self._cache:
            return self._cache[self._theme]
        if self._template is None:
            self._template = read_style_template()
        tokens = self.palette().as_tokens()
        tokens["UI_FONT"] = _UI_FONT
        tokens["MONO_FONT"] = _MONO_FONT
        css = self._template
        for key, value in tokens.items():
            css = css.replace(f"@{key}@", value)
        self._cache[self._theme] = css
        return css

    def set_theme(self, name: str) -> None:
        if name not in _PALETTES or name == self._theme:
            return
        self._theme = name
        self.apply()
        self.theme_changed.emit(name)

    def toggle(self) -> str:
        self.set_theme(THEME_LIGHT if self._theme == THEME_DARK else THEME_DARK)
        return self._theme

    def apply(self) -> None:
        """把样式表与 QPalette 应用到整个应用。"""
        app = QApplication.instance()
        if app is None:  # pragma: no cover - 仅测试环境
            return
        app.setStyleSheet(self.stylesheet())
        palette = self.palette()
        qp = QPalette()
        qp.setColor(QPalette.Window, QColor(palette.bg))
        qp.setColor(QPalette.WindowText, QColor(palette.text))
        qp.setColor(QPalette.Base, QColor(palette.raised))
        qp.setColor(QPalette.AlternateBase, QColor(palette.panel_alt))
        qp.setColor(QPalette.Text, QColor(palette.text))
        qp.setColor(QPalette.Button, QColor(palette.panel))
        qp.setColor(QPalette.ButtonText, QColor(palette.text))
        qp.setColor(QPalette.Highlight, QColor(palette.selection_bg))
        qp.setColor(QPalette.HighlightedText, QColor(palette.selection_text))
        qp.setColor(QPalette.ToolTipBase, QColor(palette.panel))
        qp.setColor(QPalette.ToolTipText, QColor(palette.text))
        qp.setColor(QPalette.PlaceholderText, QColor(palette.text_muted))
        qp.setColor(QPalette.Mid, QColor(palette.border))
        qp.setColor(QPalette.Dark, QColor(palette.border))
        qp.setColor(QPalette.Link, QColor(palette.accent))
        app.setPalette(qp)


theme = ThemeManager()

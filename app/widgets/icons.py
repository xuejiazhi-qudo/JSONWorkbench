"""SVG 图标：内嵌矢量路径 + 运行时按主题着色。

图标是 24x24 描边风格（与 VS Code / Feather 一致的观感），
按需渲染成 QIcon 并缓存；主题切换时调用 :func:`clear_cache`。
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from PySide6.QtCore import QByteArray, QRect
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from app.widgets.theme import theme

#: 图标名 -> SVG 内部标记（24x24 视口，描边风格）
ICONS: Dict[str, str] = {
    "logo": '<path d="M9 4H8a3 3 0 0 0-3 3v3a2 2 0 0 1-2 2 2 2 0 0 1 2 2v3a3 3 0 0 0 3 3h1"/>'
            '<path d="M15 4h1a3 3 0 0 1 3 3v3a2 2 0 0 0 2 2 2 2 0 0 0-2 2v3a3 3 0 0 1-3 3h-1"/>',
    "braces": '<path d="M9 4H8a3 3 0 0 0-3 3v3a2 2 0 0 1-2 2 2 2 0 0 1 2 2v3a3 3 0 0 0 3 3h1"/>'
              '<path d="M15 4h1a3 3 0 0 1 3 3v3a2 2 0 0 0 2 2 2 2 0 0 0-2 2v3a3 3 0 0 1-3 3h-1"/>'
              '<line x1="10" y1="12" x2="14" y2="12"/>',
    "code": '<polyline points="9 6 4 12 9 18"/><polyline points="15 6 20 12 15 18"/>',
    "file": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>'
            '<polyline points="14 3 14 8 19 8"/>',
    "file-plus": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>'
                 '<polyline points="14 3 14 8 19 8"/>'
                 '<line x1="12" y1="12" x2="12" y2="18"/><line x1="9" y1="15" x2="15" y2="15"/>',
    "file-text": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>'
                 '<polyline points="14 3 14 8 19 8"/>'
                 '<line x1="9" y1="13" x2="15" y2="13"/><line x1="9" y1="17" x2="13" y2="17"/>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h3.5l2 2H19a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "folder-open": '<path d="M3 7a2 2 0 0 1 2-2h3.5l2 2H19a2 2 0 0 1 2 2v1H3z"/>'
                   '<path d="M3 10h18l-2 8a2 2 0 0 1-2 1.5H5a2 2 0 0 1-2-1.5z"/>',
    "save": '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>'
            '<polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/>',
    "save-as": '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v3"/>'
               '<polyline points="17 21 17 13 7 13 7 21"/>'
               '<path d="M21.5 16.5l-4.5 4.5-2-.5.5-2 4.5-4.5z"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
                '<polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
    "upload": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
              '<polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>',
    "copy": '<rect x="9" y="9" width="12" height="12" rx="2"/>'
            '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "clipboard": '<rect x="8" y="3" width="8" height="4" rx="1"/>'
                 '<path d="M16 5h2a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h2"/>',
    "trash": '<polyline points="3 6 5 6 21 6"/>'
             '<path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>'
             '<line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/>'
             '<path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>',
    "close": '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
    "search": '<circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.5" y2="16.5"/>',
    "replace": '<path d="M4 8h11"/><polyline points="12 5 15 8 12 11"/>'
               '<path d="M20 16H9"/><polyline points="12 13 9 16 12 19"/>',
    "undo": '<path d="M9 14L4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-4"/>',
    "redo": '<path d="M15 14l5-5-5-5"/><path d="M20 9H9a5 5 0 0 0 0 10h4"/>',
    "format": '<line x1="4" y1="6" x2="20" y2="6"/><line x1="9" y1="12" x2="20" y2="12"/>'
              '<line x1="9" y1="18" x2="20" y2="18"/>'
              '<polyline points="6 9 4 12 6 15"/>',
    "compress": '<polyline points="4 14 10 14 10 20"/><polyline points="20 10 14 10 14 4"/>'
                '<line x1="14" y1="10" x2="21" y2="3"/><line x1="3" y1="21" x2="10" y2="14"/>',
    "check-circle": '<circle cx="12" cy="12" r="9"/><polyline points="8 12 11 15 16 9"/>',
    "check": '<polyline points="4 12 9 17 20 6"/>',
    "shield-check": '<path d="M12 3l8 3v6c0 4.5-3.4 8.3-8 9.5-4.6-1.2-8-5-8-9.5V6z"/>'
                    '<polyline points="9 12 11 14 15 10"/>',
    "tree": '<rect x="9" y="3" width="6" height="4" rx="1"/>'
            '<rect x="3" y="17" width="6" height="4" rx="1"/>'
            '<rect x="15" y="17" width="6" height="4" rx="1"/>'
            '<path d="M12 7v4M6 17v-2a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v2"/>',
    "columns": '<rect x="3" y="4" width="7" height="16" rx="1.5"/>'
               '<rect x="14" y="4" width="7" height="16" rx="1.5"/>'
               '<line x1="5.5" y1="8" x2="7.5" y2="8"/><line x1="5.5" y1="12" x2="7.5" y2="12"/>',
    "split": '<rect x="3" y="4" width="18" height="16" rx="2"/><line x1="12" y1="4" x2="12" y2="20"/>',
    "branch": '<line x1="6" y1="4" x2="6" y2="15"/><circle cx="18" cy="6" r="2.5"/>'
              '<circle cx="6" cy="18" r="2.5"/><path d="M18 8.5a9.5 9.5 0 0 1-9.5 9.5"/>',
    "convert": '<polyline points="17 2 21 6 17 10"/><path d="M3 12v-2a4 4 0 0 1 4-4h14"/>'
               '<polyline points="7 22 3 18 7 14"/><path d="M21 12v2a4 4 0 0 1-4 4H3"/>',
    "history": '<polyline points="2 4 2 10 8 10"/><path d="M4.5 15a8.5 8.5 0 1 0 1.8-8.7L2 10"/>'
               '<polyline points="12 8 12 12 15 14"/>',
    "bar-chart": '<line x1="3" y1="21" x2="21" y2="21"/>'
                 '<line x1="6" y1="21" x2="6" y2="13"/><line x1="11" y1="21" x2="11" y2="7"/>'
                 '<line x1="16" y1="21" x2="16" y2="11"/><line x1="20" y1="21" x2="20" y2="16"/>',
    "sparkles": '<path d="M11 3l1.7 4.8L17.5 9.5l-4.8 1.7L11 16l-1.7-4.8L4.5 9.5l4.8-1.7z"/>'
                '<path d="M18 14l.9 2.6 2.6.9-2.6.9-.9 2.6-.9-2.6-2.6-.9 2.6-.9z"/>',
    "wrench": '<path d="M20.6 5.6a5 5 0 0 1-6.6 6.6L6 20.2 3.8 18l8-8a5 5 0 0 1 6.6-6.6l-3 3 2.6 2.6z"/>',
    "settings": '<circle cx="12" cy="12" r="3.2"/>'
                '<path d="M12 2.5v2.6M12 18.9v2.6M2.5 12h2.6M18.9 12h2.6"/>'
                '<path d="M5.3 5.3l1.9 1.9M16.8 16.8l1.9 1.9M18.7 5.3l-1.9 1.9M7.2 16.8l-1.9 1.9"/>',
    "moon": '<path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z"/>',
    "sun": '<circle cx="12" cy="12" r="4"/>'
          '<path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8'
          'M19.1 4.9l-1.8 1.8M6.7 17.3l-1.8 1.8"/>',
    "expand-all": '<polyline points="7 9 12 4 17 9"/><polyline points="7 15 12 20 17 15"/>',
    "collapse-all": '<polyline points="7 4 12 9 17 4"/><polyline points="7 20 12 15 17 20"/>',
    "chevron-right": '<polyline points="9 6 15 12 9 18"/>',
    "chevron-down": '<polyline points="6 9 12 15 18 9"/>',
    "plus": '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
    "minus": '<line x1="5" y1="12" x2="19" y2="12"/>',
    "play": '<polygon points="7 4 20 12 7 20"/>',
    "refresh": '<polyline points="21 3 21 9 15 9"/><path d="M20.5 14.5a9 9 0 1 1-2-9.5L21 9"/>',
    "alert": '<path d="M10.3 3.9L1.9 18a2 2 0 0 0 1.7 3h16.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>'
             '<line x1="12" y1="9" x2="12" y2="14"/><line x1="12" y1="17.5" x2="12" y2="17.6"/>',
    "x-circle": '<circle cx="12" cy="12" r="9"/><line x1="15" y1="9" x2="9" y2="15"/>'
                '<line x1="9" y1="9" x2="15" y2="15"/>',
    "info": '<circle cx="12" cy="12" r="9"/><line x1="12" y1="11" x2="12" y2="16.5"/>'
            '<line x1="12" y1="7.8" x2="12" y2="7.9"/>',
    "help": '<circle cx="12" cy="12" r="9"/><path d="M9.4 9.2a2.7 2.7 0 1 1 3.6 2.6c-.6.2-1 .8-1 1.5v.4"/>'
            '<line x1="12" y1="17.2" x2="12" y2="17.3"/>',
    "globe": '<circle cx="12" cy="12" r="9"/><line x1="3" y1="12" x2="21" y2="12"/>'
             '<path d="M12 3a14 14 0 0 1 0 18 14 14 0 0 1 0-18z"/>',
    "filter": '<polygon points="3 4.5 21 4.5 14 12.5 14 19.5 10 21.5 10 12.5"/>',
    "list": '<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/>'
            '<line x1="8" y1="18" x2="21" y2="18"/>'
            '<line x1="4" y1="6" x2="4.1" y2="6"/><line x1="4" y1="12" x2="4.1" y2="12"/>'
            '<line x1="4" y1="18" x2="4.1" y2="18"/>',
    "database": '<ellipse cx="12" cy="5.5" rx="8" ry="2.8"/>'
                '<path d="M4 5.5v6c0 1.5 3.6 2.8 8 2.8s8-1.3 8-2.8v-6"/>'
                '<path d="M4 11.5v6c0 1.5 3.6 2.8 8 2.8s8-1.3 8-2.8v-6"/>',
    "scissors": '<circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/>'
                '<line x1="8.2" y1="7.3" x2="20" y2="18"/><line x1="8.2" y1="16.7" x2="20" y2="6"/>',
    "table": '<rect x="3" y="4" width="18" height="16" rx="2"/><line x1="3" y1="10" x2="21" y2="10"/>'
             '<line x1="9.5" y1="10" x2="9.5" y2="20"/><line x1="15.5" y1="10" x2="15.5" y2="20"/>',
    "quote": '<path d="M9.5 6.5H6a2 2 0 0 0-2 2v3a2 2 0 0 0 2 2h2v1.5a2.5 2.5 0 0 1-2.5 2.5"/>'
             '<path d="M20 6.5h-3.5a2 2 0 0 0-2 2v3a2 2 0 0 0 2 2h2V15a2.5 2.5 0 0 1-2.5 2.5"/>',
    "target": '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/>'
              '<line x1="12" y1="1" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="23"/>'
              '<line x1="1" y1="12" x2="4" y2="12"/><line x1="20" y1="12" x2="23" y2="12"/>',
    "layers": '<polygon points="12 2.5 21.5 8 12 13.5 2.5 8"/><polyline points="2.5 14 12 19.5 21.5 14"/>',
    "lock": '<rect x="4" y="10" width="16" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
    "sort": '<path d="M7 4v16"/><polyline points="4 17 7 20 10 17"/>'
            '<line x1="13" y1="7" x2="21" y2="7"/><line x1="13" y1="12" x2="19" y2="12"/>'
            '<line x1="13" y1="17" x2="17" y2="17"/>',
    "arrow-right": '<line x1="4" y1="12" x2="19" y2="12"/><polyline points="14 7 19 12 14 17"/>',
    "eye": '<path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12z"/>'
           '<circle cx="12" cy="12" r="2.6"/>',
    "wand": '<path d="M4 20L15 9"/><path d="M17 3l1 2.5L20.5 6.5 18 7.5 17 10l-1-2.5L13.5 6.5 16 5.5z"/>'
            '<path d="M19.5 13l.6 1.4 1.4.6-1.4.6-.6 1.4-.6-1.4-1.4-.6 1.4-.6z"/>',
    "merge": '<path d="M6 3v6a4 4 0 0 0 4 4h5"/><polyline points="12 10 15 13 12 16"/>'
             '<path d="M6 21v-4a4 4 0 0 1 4-4"/>',
    "bug": '<rect x="8" y="6" width="8" height="13" rx="4"/>'
           '<path d="M8 10H4M20 10h-4M8 15H4M20 15h-4M9 6l-1.5-2.5M15 6l1.5-2.5M12 19v2"/>',
}

_SVG_TEMPLATE = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
    'fill="none" stroke="{color}" stroke-width="{width}" stroke-linecap="round" '
    'stroke-linejoin="round">{body}</svg>'
)

_FILLED = {"play"}

_cache: Dict[Tuple[str, str, int, float], QIcon] = {}


def icon_names() -> Tuple[str, ...]:
    return tuple(sorted(ICONS))


def svg_text(name: str, color: str = "#E6E8EC", stroke_width: float = 1.8) -> str:
    """返回完整的 SVG 文本，可用于导出 .svg 文件或调试。"""
    body = ICONS.get(name, ICONS["info"])
    template = _SVG_TEMPLATE
    if name in _FILLED:
        template = template.replace('fill="none"', f'fill="{color}"')
    return template.format(color=color, width=stroke_width, body=body)


def render_pixmap(name: str, color: str, size: int, stroke_width: float = 1.8) -> QPixmap:
    """按设备像素比渲染，保证高 DPI 下清晰。"""
    from PySide6.QtGui import QGuiApplication

    screen = QGuiApplication.primaryScreen()
    ratio = screen.devicePixelRatio() if screen is not None else 1.0
    side = max(1, int(size * ratio))
    pixmap = QPixmap(side, side)
    pixmap.fill(QColor(0, 0, 0, 0))
    renderer = QSvgRenderer(QByteArray(svg_text(name, color, stroke_width).encode("utf-8")))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    renderer.render(painter, QRect(0, 0, side, side))
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def icon(name: str, color: Optional[str] = None, size: int = 18,
         kind: str = "text", stroke_width: float = 1.8) -> QIcon:
    """取图标（带缓存）。``color`` 优先，否则用主题的 kind 颜色。"""
    resolved = color or theme.icon_color(kind)
    key = (name, resolved, size, stroke_width)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    pixmap = render_pixmap(name, resolved, size, stroke_width)
    result = QIcon(pixmap)
    _cache[key] = result
    return result


def clear_cache() -> None:
    _cache.clear()


def export_svgs(directory: str, color: str = "#E6E8EC") -> int:
    """把全部图标导出成 .svg 文件（打包/文档用），返回导出数量。"""
    import os

    os.makedirs(directory, exist_ok=True)
    count = 0
    for name in ICONS:
        path = os.path.join(directory, f"{name}.svg")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(svg_text(name, color))
        count += 1
    return count


def _init_theme_hook() -> None:
    """主题切换时清空图标缓存，否则图标还是旧颜色。"""
    theme.theme_changed.connect(lambda _name: clear_cache())


_init_theme_hook()

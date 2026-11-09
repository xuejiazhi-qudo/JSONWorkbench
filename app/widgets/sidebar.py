"""左侧导航栏。

用 QListWidget 实现：分组标题是不可选中的普通行（字体/颜色通过 item data 设置），
条目行是图标 + 文本，选中态由 QSS 的 #SidebarList::item:selected 控制。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.i18n import register_retranslate, tr
from app.widgets.icons import icon
from app.widgets.theme import theme


class NavItem:
    """一个导航条目。"""

    __slots__ = ("item_id", "label", "icon_name", "group", "shortcut")

    def __init__(self, item_id: str, label: str, icon_name: str, group: str,
                 shortcut: str = "") -> None:
        self.item_id = item_id
        self.label = label          # 英文原文，同时是 tr() 的 key
        self.icon_name = icon_name
        self.group = group
        self.shortcut = shortcut


GROUP_WORKSPACE = "workspace"
GROUP_TOOLS = "tools"
GROUP_DATA = "data"

NAV_ITEMS: List[NavItem] = [
    NavItem("formatter", "JSON Formatter", "format", GROUP_WORKSPACE, "Ctrl+Shift+F"),
    NavItem("validator", "JSON Validator", "check-circle", GROUP_WORKSPACE, "F5"),
    NavItem("compressor", "JSON Minifier", "compress", GROUP_WORKSPACE, "Ctrl+Shift+M"),
    NavItem("tree", "Tree Viewer", "tree", GROUP_WORKSPACE),
    NavItem("diff", "JSON Diff", "columns", GROUP_TOOLS, "Ctrl+Shift+D"),
    NavItem("jsonpath", "JSONPath Query", "branch", GROUP_TOOLS),
    NavItem("converter", "Data Converter", "convert", GROUP_TOOLS),
    NavItem("schema", "JSON Schema", "shield-check", GROUP_TOOLS),
    NavItem("generator", "Data Generator", "sparkles", GROUP_DATA),
    NavItem("tools", "Utilities", "wrench", GROUP_DATA),
    NavItem("stats", "Statistics", "bar-chart", GROUP_DATA),
    NavItem("history", "History", "history", GROUP_DATA),
    NavItem("settings", "Settings", "settings", GROUP_DATA),
]

_GROUP_LABELS = {
    GROUP_WORKSPACE: "Workspace",
    GROUP_TOOLS: "Tools",
    GROUP_DATA: "Data & Settings",
}

_ROLE_ID = Qt.ItemDataRole.UserRole


class Sidebar(QWidget):
    """分组导航，点击条目发出 :attr:`item_selected`。"""

    item_selected = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None, width: int = 208,
                 items: Optional[List[NavItem]] = None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(max(160, width))
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self._items: List[NavItem] = list(items) if items else list(NAV_ITEMS)

        self.list = QListWidget(self)
        self.list.setObjectName("SidebarList")
        self.list.setFrameShape(QListWidget.NoFrame)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.list.setIconSize(QSize(17, 17))
        self.list.currentItemChanged.connect(self._on_current_changed)

        self.footer = QLabel(tr("100% offline · no upload"), self)
        self.footer.setObjectName("SidebarBadge")
        self.footer.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.footer)

        self._rows: Dict[str, QListWidgetItem] = {}
        self._headers: Dict[str, QListWidgetItem] = {}
        self._build()
        register_retranslate(self)

    # ------------------------------------------------------------------ 构建
    def _build(self) -> None:
        current_group = None
        for nav in self._items:
            if nav.group != current_group:
                current_group = nav.group
                header = QListWidgetItem(tr(_GROUP_LABELS[current_group]))
                header.setFlags(Qt.NoItemFlags)
                font = QFont(header.font())
                font.setPointSizeF(max(7.0, font.pointSizeF() - 1.5))
                font.setBold(True)
                header.setFont(font)
                header.setForeground(QColor(theme.palette().text_muted))
                header.setSizeHint(QSize(0, 30))
                header.setTextAlignment(Qt.AlignLeft | Qt.AlignBottom)
                self.list.addItem(header)
                self._headers[current_group] = header

            row = QListWidgetItem(icon(nav.icon_name, size=17, kind="muted"), tr(nav.label))
            row.setData(_ROLE_ID, nav.item_id)
            tooltip = tr(nav.label) + (f"  ({nav.shortcut})" if nav.shortcut else "")
            row.setToolTip(tooltip)
            row.setSizeHint(QSize(0, 34))
            self.list.addItem(row)
            self._rows[nav.item_id] = row

    def _on_current_changed(self, current: QListWidgetItem, _previous) -> None:
        """点击与方向键都会走到这里，因此只在这一个入口发信号。"""
        if current is None:
            return
        item_id = current.data(_ROLE_ID)
        if item_id:
            self.item_selected.emit(str(item_id))

    # ------------------------------------------------------------------ 对外接口
    def select(self, item_id: str, *, emit: bool = False) -> None:
        row = self._rows.get(item_id)
        if row is None:
            return
        if not emit:
            self.list.blockSignals(True)
        self.list.setCurrentItem(row)
        if not emit:
            self.list.blockSignals(False)

    def current_id(self) -> str:
        item = self.list.currentItem()
        if item is not None and item.data(_ROLE_ID):
            return str(item.data(_ROLE_ID))
        return self._items[0].item_id if self._items else ""

    def set_badge(self, item_id: str, text: str) -> None:
        row = self._rows.get(item_id)
        if row is None:
            return
        nav = next((n for n in self._items if n.item_id == item_id), None)
        base = tr(nav.label) if nav else ""
        row.setText(f"{base}   {text}" if text else base)

    def set_width(self, width: int) -> None:
        self.setFixedWidth(max(160, width))

    def refresh_icons(self) -> None:
        for nav in self._items:
            self._rows[nav.item_id].setIcon(icon(nav.icon_name, size=17, kind="muted"))

    def retranslate(self) -> None:
        for group_id, header in self._headers.items():
            header.setText(tr(_GROUP_LABELS[group_id]))
            header.setForeground(QColor(theme.palette().text_muted))
        for nav in self._items:
            row = self._rows[nav.item_id]
            row.setText(tr(nav.label))
            row.setToolTip(tr(nav.label) + (f"  ({nav.shortcut})" if nav.shortcut else ""))
        self.footer.setText(tr("100% offline · no upload"))

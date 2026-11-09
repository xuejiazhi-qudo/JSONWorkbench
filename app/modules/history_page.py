"""历史记录页：查看 / 恢复 / 删除 / 清空，隐私说明常驻。"""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.history.store import (
    KIND_FILE,
    KIND_FORMAT,
    KIND_QUERY,
    KIND_SCHEMA,
    HistoryEntry,
    HistoryStore,
)
from app.i18n import register_retranslate, tr
from app.settings.settings import settings
from app.utils import dialogs
from app.widgets.icons import icon
from app.widgets.page import PageShell

KIND_LABELS = {
    KIND_FILE: "Opened file",
    KIND_FORMAT: "Format run",
    KIND_QUERY: "JSONPath query",
    KIND_SCHEMA: "Schema validation",
}


class HistoryPage(PageShell):
    """侧边栏"历史记录"页。"""

    status_message = Signal(str, int)
    restore_content = Signal(str)     # 用存储的原文新建标签页
    restore_path = Signal(str)        # 重新打开文件

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("History",
                         "Recent files, format runs, queries and schemas. By default only "
                         "metadata is stored — never the JSON content.")
        self._store: Optional[HistoryStore] = None

        self.filter_combo = QComboBox()
        self.filter_combo.addItem(tr("All kinds"), "")
        for kind, label in KIND_LABELS.items():
            self.filter_combo.addItem(tr(label), kind)
        self.filter_combo.currentIndexChanged.connect(self.refresh)

        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.setIcon(icon("refresh", size=15, kind="muted"))
        self.refresh_button.clicked.connect(self.refresh)

        self.restore_button = QPushButton(tr("Restore"))
        self.restore_button.setProperty("class", "primary")
        self.restore_button.setIcon(icon("history", size=15, kind="on_accent"))
        self.restore_button.clicked.connect(self.restore_selected)

        self.delete_button = QPushButton(tr("Delete"))
        self.delete_button.setIcon(icon("trash", size=15, kind="muted"))
        self.delete_button.clicked.connect(self.delete_selected)

        self.clear_button = QPushButton(tr("Clear all"))
        self.clear_button.setProperty("class", "danger")
        self.clear_button.setIcon(icon("trash", size=15, kind="danger"))
        self.clear_button.clicked.connect(self.clear_all)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("Time"), tr("Kind"), tr("Label"), tr("Detail"), tr("Path")])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        self.tree.setColumnWidth(3, 220)
        self.tree.setColumnWidth(4, 260)
        self.tree.itemActivated.connect(lambda *_: self.restore_selected())

        self.note_label = QLabel("")
        self.note_label.setObjectName("HintLabel")
        self.note_label.setWordWrap(True)

        top = QHBoxLayout()
        top.addWidget(QLabel(tr("Kind:")))
        top.addWidget(self.filter_combo)
        top.addStretch(1)
        top.addWidget(self.refresh_button)
        top.addWidget(self.restore_button)
        top.addWidget(self.delete_button)
        top.addWidget(self.clear_button)

        layout = QVBoxLayout()
        layout.addLayout(top)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.note_label)
        self.add_layout(layout)
        register_retranslate(self)

    # ------------------------------------------------------------------ 数据
    def _store_or_none(self) -> Optional[HistoryStore]:
        if self._store is None or not self._store.available:
            self._store = HistoryStore()
        return self._store if self._store.available else None

    def showEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        super().showEvent(event)
        self.refresh()

    def refresh(self) -> None:
        self.tree.clear()
        store = self._store_or_none()
        if store is None:
            self.note_label.setText(tr("History is unavailable: {error}").format(
                error=getattr(store, "error", "") or tr("cannot open the local database")))
            return
        kind = self.filter_combo.currentData() or None
        entries = store.list(kind=kind, limit=settings.history_limit)
        for entry in entries:
            item = QTreeWidgetItem([
                entry.display_time(),
                tr(KIND_LABELS.get(entry.kind, entry.kind)),
                entry.label,
                entry.detail,
                entry.path,
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, entry.id)
            if entry.has_content:
                item.setIcon(2, icon("file-text", size=14, kind="success"))
            self.tree.addTopLevelItem(item)
        self.note_label.setText(
            tr("{n} entries stored locally. Content storage is {state}.").format(
                n=store.count(),
                state=tr("ON") if settings.history_store_content else tr("OFF")))

    def _selected_entry(self) -> Optional[HistoryEntry]:
        item = self.tree.currentItem()
        if item is None:
            return None
        store = self._store_or_none()
        if store is None:
            return None
        return store.get(item.data(0, Qt.ItemDataRole.UserRole))

    # ------------------------------------------------------------------ 动作
    def restore_selected(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        if entry.has_content:
            self.restore_content.emit(entry.content)
            self.status_message.emit(tr("Restored from history."), 4000)
            return
        if entry.path and os.path.isfile(entry.path):
            self.restore_path.emit(entry.path)
            return
        dialogs.warn(self, tr("History"),
                     tr("Nothing to restore: no stored content and the file is gone."))

    def delete_selected(self) -> None:
        item = self.tree.currentItem()
        if item is None:
            return
        store = self._store_or_none()
        if store is not None and store.delete(item.data(0, Qt.ItemDataRole.UserRole)):
            self.refresh()
            self.status_message.emit(tr("Entry deleted."), 3000)

    def clear_all(self) -> None:
        if not dialogs.confirm(self, tr("Clear all history"),
                               tr("This permanently deletes every history entry on this machine."),
                               default=False):
            return
        store = self._store_or_none()
        if store is None:
            return
        count = store.clear_all()
        self.refresh()
        self.status_message.emit(tr("Removed {n} history entries.").format(n=count), 5000)

    # ------------------------------------------------------------------ 翻译
    def retranslate(self) -> None:
        super().retranslate()
        self.filter_combo.setItemText(0, tr("All kinds"))
        for index, (kind, label) in enumerate(KIND_LABELS.items(), start=1):
            self.filter_combo.setItemText(index, tr(label))
        self.refresh_button.setText(tr("Refresh"))
        self.restore_button.setText(tr("Restore"))
        self.delete_button.setText(tr("Delete"))
        self.clear_button.setText(tr("Clear all"))
        self.tree.setHeaderLabels([tr("Time"), tr("Kind"), tr("Label"), tr("Detail"), tr("Path")])
        self.refresh()

"""功能页通用外壳：标题 + 副标题 + 内容区。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from app.i18n import register_retranslate, tr


class PageShell(QWidget):
    """所有侧边栏功能页的统一外壳。"""

    def __init__(self, title: str, subtitle: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._title_key = title
        self._subtitle_key = subtitle
        self.title_label = QLabel(tr(title))
        self.title_label.setObjectName("PageTitle")
        self.subtitle_label = QLabel(tr(subtitle))
        self.subtitle_label.setObjectName("PageSubtitle")
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setVisible(bool(subtitle))

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(14, 12, 14, 10)
        self._layout.setSpacing(8)
        self._layout.addWidget(self.title_label)
        self._layout.addWidget(self.subtitle_label)
        register_retranslate(self)

    def add(self, widget: QWidget, stretch: int = 0) -> None:
        self._layout.addWidget(widget, stretch)

    def add_layout(self, layout) -> None:
        self._layout.addLayout(layout)

    def retranslate(self) -> None:
        self.title_label.setText(tr(self._title_key))
        if self._subtitle_key:
            self.subtitle_label.setText(tr(self._subtitle_key))

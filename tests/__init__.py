"""单元测试与离屏 UI 测试。

测试进程一律：
* headless（不弹模态框，确认类问题按默认值返回），保证离屏/CI 不挂死；
* 使用独立的临时 APPDATA / 数据目录，避免测试之间通过 QSettings/SQLite 互相污染。

运行方式（在 JSONWorkbench 目录下）::

    python -m unittest discover -t . -s tests -v
"""

import os
import tempfile

_PROFILE = tempfile.mkdtemp(prefix="jsonwb-test-")
os.environ["APPDATA"] = _PROFILE
os.environ["JSONWB_DATA_DIR"] = os.path.join(_PROFILE, "data")
os.environ.setdefault("JSONWB_HEADLESS", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _shutdown_qt() -> None:
    """解释器退出前先销毁顶层控件，避免 PySide6 析构顺序导致的段错误。"""
    import gc

    try:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QCoreApplication, QEvent
    except ImportError:  # pragma: no cover - Qt 未安装时测试根本跑不起来
        return
    app = QApplication.instance()
    if app is None:
        return
    from app.utils.worker import shutdown_tasks
    shutdown_tasks()
    for widget in list(app.topLevelWidgets()):
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    gc.collect()


import atexit  # noqa: E402
from PySide6 import QtWidgets  # noqa: E402,F401 - 先注册 Qt 清理，再注册我们的清理

atexit.register(_shutdown_qt)

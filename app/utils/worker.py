"""后台任务：QThread 封装，带进度上报与取消。

大文件解析/格式化/对比/转换都必须走这里，避免卡住界面（产品硬要求）。
"""

from __future__ import annotations

import traceback
from typing import Any, Callable, Optional, Set

from PySide6.QtCore import QObject, QThread, Signal

__all__ = ["JobContext", "Task", "start_task"]


class JobContext:
    """传给任务函数的上下文：上报进度、查询取消状态。"""

    def __init__(self, signals: "TaskSignals", total: int = 0) -> None:
        self._signals = signals
        self._cancelled = False
        self.total = total

    def progress(self, done: int, total: Optional[int] = None) -> None:
        if total is not None:
            self.total = total
        self._signals.progress.emit(int(done), int(self.total))

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise TaskCancelled()


class TaskCancelled(Exception):
    """任务被用户取消。"""


class TaskSignals(QObject):
    progress = Signal(int, int)
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()


class Task(QThread):
    """一次性后台任务。``fn(ctx, *args, **kwargs)`` 的返回值通过 succeeded 发出。"""

    def __init__(self, fn: Callable, *args: Any, parent: Optional[QObject] = None,
                 **kwargs: Any) -> None:
        super().__init__(parent)
        self.signals = TaskSignals()
        self.ctx = JobContext(self.signals)
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self) -> None:  # noqa: D102 (Qt 覆盖)
        try:
            result = self._fn(self.ctx, *self._args, **self._kwargs)
        except TaskCancelled:
            self.signals.cancelled.emit()
        except Exception as exc:  # noqa: BLE001 - 后台任务的任何异常都要回到 UI 层展示
            self.signals.failed.emit(f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=6)}")
        else:
            if self.ctx.cancelled:
                self.signals.cancelled.emit()
            else:
                self.signals.succeeded.emit(result)

    def cancel(self) -> None:
        self.ctx.cancel()


_live_tasks: Set[Task] = set()


def start_task(fn: Callable, *args: Any,
               on_done: Optional[Callable[[Any], None]] = None,
               on_error: Optional[Callable[[str], None]] = None,
               on_progress: Optional[Callable[[int, int], None]] = None,
               on_cancelled: Optional[Callable[[], None]] = None,
               parent: Optional[QObject] = None, **kwargs: Any) -> Task:
    """启动后台任务并保持强引用（否则 QThread 会被 GC 掉导致崩溃）。"""
    task = Task(fn, *args, parent=parent, **kwargs)
    _live_tasks.add(task)
    if on_done is not None:
        task.signals.succeeded.connect(on_done)
    if on_error is not None:
        task.signals.failed.connect(on_error)
    if on_progress is not None:
        task.signals.progress.connect(on_progress)
    if on_cancelled is not None:
        task.signals.cancelled.connect(on_cancelled)

    def _cleanup() -> None:
        _live_tasks.discard(task)
        task.deleteLater()

    task.finished.connect(_cleanup)
    task.start()
    return task


def shutdown_tasks() -> None:
    """退出 Qt 前回收工作线程，避免 QThread 仍运行时被析构。"""
    tasks = list(_live_tasks)
    for task in tasks:
        task.cancel()
    for task in tasks:
        task.wait()

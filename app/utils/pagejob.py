"""功能页的最新任务：只在线程里计算，在 GUI 线程验证输入并展示结果。"""

from PySide6.QtCore import QObject, Slot

from app.i18n import tr
from app.utils.worker import start_task


class PageJob(QObject):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.generation = 0
        self.task = None
        self.done = None
        self.is_current = None

    def run(self, work, done, is_current, background):
        self.generation += 1
        generation = self.generation
        if self.task is not None:
            self.task.cancel()
            self.task = None
        self.done, self.is_current = done, is_current
        if not background:
            done(work())
            return
        self.owner.status_message.emit(tr("Processing in the background…"), 3000)

        def compute(ctx):
            try:
                return generation, work(), ""
            except Exception as exc:
                return generation, None, f"{type(exc).__name__}: {exc}"

        self.task = start_task(compute, on_done=self._complete)

    @Slot(object)
    def _complete(self, payload):
        generation, result, error = payload
        if generation != self.generation:
            return
        self.task = None
        if not self.is_current():
            self.owner.status_message.emit(tr("Inputs changed; the result was discarded."), 5000)
            return
        if error:
            self.owner.status_message.emit(error, 8000)
            return
        self.done(result)


def run_latest(owner, work, done, is_current, *, background):
    job = getattr(owner, "_page_job", None)
    if job is None:
        job = owner._page_job = PageJob(owner)
    job.run(work, done, is_current, background)


def is_large_value(value, limit=20000):
    """有界预检：最多访问 limit 个节点，不序列化整份文档。"""
    stack = [iter((value,))]
    count = 0
    while stack:
        try:
            node = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        count += 1
        if count > limit or isinstance(node, str) and len(node) > 800_000:
            return True
        if isinstance(node, dict):
            stack.append(iter(node.values()))
        elif isinstance(node, list):
            stack.append(iter(node))
    return False

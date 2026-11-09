#!/usr/bin/env python3
"""JSON Workbench 启动入口。

用法::

    python run.py                 # 正常启动
    python run.py file.json       # 启动并打开文件
    JSONWB_SELFTEST=1500 python run.py   # 自检模式：跑完冒烟检查后退出

打包后的 exe 行为一致（见 JsonWorkbench.spec）。
"""

from __future__ import annotations

import os
import sys


def _bootstrap() -> str:
    """把项目根目录加进 sys.path，源码运行与打包运行都能 import app.*。"""
    if getattr(sys, "frozen", False):
        root = os.path.dirname(sys.executable)
    else:
        root = os.path.dirname(os.path.abspath(__file__))
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


def _write_selftest_report(report: str) -> None:
    """打包版没有控制台，把自检报告落到 exe 旁边的文件里便于验收。"""
    try:
        if getattr(sys, "frozen", False):
            directory = os.path.dirname(sys.executable)
        else:
            directory = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(directory, "selftest_report.txt"), "w",
                  encoding="utf-8") as handle:
            handle.write(report + "\n")
    except OSError:
        pass


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    _bootstrap()

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    from app import APP_NAME, APP_ORG, APP_VERSION
    from app.i18n import set_language
    from app.settings.settings import settings
    from app.widgets.theme import theme

    # 高分屏：Qt 6 默认开启缩放，这里只把圆整策略设为精确跟随，避免 125%/150% 下发虚
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    app = QApplication(argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(APP_ORG)
    # Fusion 保证 QSS 在 Windows / macOS / Linux 上表现一致
    app.setStyle("Fusion")

    set_language(settings.language)
    theme.set_theme(settings.theme)
    theme.apply()

    base_font = QFont(settings.font_family or "Segoe UI")
    base_font.setPointSize(10)
    app.setFont(base_font)

    from app.widgets.mainwindow import MainWindow
    from app.utils.worker import shutdown_tasks

    app.aboutToQuit.connect(shutdown_tasks)

    window = MainWindow()
    window.show()

    for argument in argv[1:]:
        if argument and os.path.isfile(argument):
            window.open_path(os.path.abspath(argument))

    selftest = os.environ.get("JSONWB_SELFTEST")
    if selftest:
        try:
            delay = max(200, int(selftest))
        except ValueError:
            delay = 1500

        def _run_selftest() -> None:
            from app.selftest import run_selftest

            report, ok = run_selftest(window)
            print(report, flush=True)
            _write_selftest_report(report)
            app.exit(0 if ok else 1)

        QTimer.singleShot(delay, _run_selftest)

        def _watchdog() -> None:
            # 硬看门狗：自检被模态框或死循环卡住时强制退出，避免自动化流程无限挂起
            print("SELFTEST TIMEOUT", flush=True)
            os._exit(3)

        QTimer.singleShot(delay + 60_000, _watchdog)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

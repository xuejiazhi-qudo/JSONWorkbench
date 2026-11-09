# -*- mode: python ; coding: utf-8 -*-
"""JSON Workbench 打包配置。

用法（在项目根目录）::

    pyinstaller JsonWorkbench.spec --noconfirm

产物为绿色免安装目录 ``dist/JSONWorkbench/``，直接整目录分发即可；
压缩成 zip 就是可携带版。选择 onedir 而非 onefile 是为了启动速度
（onefile 解包会被杀软逐文件扫描，表现为启动挂起）。
"""

import os
import re
import sys

from PyInstaller.utils.hooks import collect_submodules

BLOCK_CIPHER = None

hiddenimports = collect_submodules("app")

# 这些 Qt 模块本应用完全用不到，排除以显著减小体积
EXCLUDES = [
    "PySide6.QtWebEngine",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtLocation",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtWebSockets",
    "PySide6.QtWebView",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtNetwork",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtXml",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtConcurrent",
    "matplotlib",
    "numpy",
    "pandas",
    "scipy",
    "pytest",
]

a = Analysis(
    ["run.py"],
    pathex=[os.path.abspath(".")],
    binaries=[],
    datas=[("resources", "resources")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=BLOCK_CIPHER,
    noarchive=False,
)

# Windows 版 Qt 使用系统 ICU 的无版本后缀导出。PATH 中的 Poppler/Conda
# 可能提供同名、但仅导出 ucnv_open_78 等符号的 DLL，PyInstaller 会误收集它，
# 导致 QtCore 启动时报“找不到指定的程序”。使用 Windows 10/11 自带的 ICU，
# 同时移除误收集 ICU 的数据依赖；不随应用分发另一套系统 ICU。
if sys.platform == "win32":
    a.binaries = [entry for entry in a.binaries
                  if entry[0].lower() != "icuuc.dll"
                  and not re.fullmatch(r"icudt\d+\.dll", entry[0], re.IGNORECASE)]

pyz = PYZ(a.pure, a.zipped_data, cipher=BLOCK_CIPHER)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="JSONWorkbench",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # GUI 应用不弹控制台；自检报告会写到 exe 旁边的文件
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="JSONWorkbench",
)

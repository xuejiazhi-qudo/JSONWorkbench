<p align="center">
  <img src="resources/icons/logo.svg" alt="JSON Workbench" width="80" height="80">
</p>

<h1 align="center">JSON Workbench</h1>

<p align="center">
  完全离线的 JSON 桌面工具箱，集编辑、校验、对比、查询与转换于一体。
</p>

<p align="center">
  <a href="README.md">English</a> · <strong>简体中文</strong>
</p>

JSON Workbench 将常用 JSON 工具整合到一个桌面应用中。你可以同时处理多份文档、浏览嵌套结构、对比接口响应、执行 JSONPath 查询、校验 Schema，以及生成测试数据，所有处理均在本机完成。

**无需账号、不上传数据、无遥测。** 历史记录默认不保存 JSON 原文。

项目使用 Python 和 PySide6 构建，当前版本为 **1.0.0**。打包目标为 Windows 10/11；源码按跨平台方式设计，但本次版本尚未验证 macOS 和 Linux。

## 目录

- [功能特性](#功能特性)
- [快速开始](#快速开始)
- [数据处理与隐私](#数据处理与隐私)
- [限制与兼容性](#限制与兼容性)
- [快捷键](#快捷键)
- [开发与测试](#开发与测试)
- [构建 Windows 便携版](#构建-windows-便携版)
- [项目结构](#项目结构)
- [依赖与许可](#依赖与许可)

## 功能特性

| 工具 | 主要能力 |
| --- | --- |
| **编辑器** | 多标签拖动、语法高亮、行号、括号匹配、自动缩进、括号补全及未保存状态提示。 |
| **格式化与压缩** | 支持 2、4、8 空格、Tab 或紧凑输出，保留 `1.230`、`1e5`、`-0` 和大整数等数字字面量。 |
| **键排序** | 支持不区分大小写的字母序或 Unicode 顺序，可选递归排序，并显式开启数组排序。 |
| **语法校验** | 按行、列和偏移定位错误，确认后应用修复建议，检测重复键和数字精度风险。 |
| **树形查看器** | 按需创建节点，搜索键值、复制 JSONPath、查看节点属性，并编辑符合条件的文档。 |
| **JSON 对比** | 对比结构与类型，区分键顺序和数据变化；数组按下标、集合或键字段配对；导出 Markdown、HTML、JSON 报告。 |
| **JSONPath** | 支持子节点、通配符、递归下降、负下标、并集、切片、过滤器、正则及函数；偏移可用时可定位到树或编辑器。 |
| **数据转换** | JSON 与 CSV、XML、YAML 互转，字符串转义与反转义、字符串化 JSON 解包、数组拆分、多文档合并及去重。 |
| **JSON Schema** | 导入或粘贴本地 Schema、从文档推断 Schema、校验 draft-07 和 2020-12 常用关键字，并导出报告。 |
| **统计分析** | 查看节点与类型计数、嵌套深度、键复用、字符串和数组大小、数字汇总、体积热点及结构特征。 |
| **实用工具** | 查找重复键、空值及匹配的键值，按文件拆分数组，分析文档结构。 |
| **数据生成** | 根据字段定义或 Schema 生成对象，生成姓名、邮箱、日期、UUID 等模拟数据，支持种子复现。 |
| **历史记录** | 查看最近文件、格式化操作、查询和 Schema 校验记录，恢复仍可访问的文件或显式保存的原文。 |

界面支持**简体中文和英语**、**深浅双主题**、高 DPI 显示，以及窗口和分栏位置记忆。默认显示语言为简体中文。

### 编辑与文件支持

- 点击行号旁的箭头，按 JSON 对象和数组结构折叠或展开。右键菜单支持“折叠全部 / 展开全部”；查找或跳转到隐藏内容时自动展开，折叠不会修改文档原文。
- 拖拽文件到窗口或标签栏，支持打开、保存和另存为。
- 自动识别 UTF-8、UTF-8 BOM、UTF-16、GB18030，并保留 LF 或 CRLF 换行。
- 普通文本或正则查找替换，支持捕获组替换。
- 识别 JSON 字符串中包裹的 JSON 文档，并解包多层编码。
- 格式化、排序和树节点修改等文本替换支持撤销。

## 快速开始

### Windows 便携版

1. 完整解压便携 ZIP 包。
2. 打开解压后的 `JSONWorkbench` 文件夹。
3. 运行 `JSONWorkbench.exe`。

请将 `_internal` 文件夹保留在 exe 旁边。便携包已包含 Python 和 Qt 运行环境，无需单独安装 Python。设置和历史记录保存在用户目录中，不保存在 exe 旁边。

### 从源码运行

| 环境 | 支持版本 | 开发验证版本 |
| --- | --- | --- |
| Python | 3.10+ | 3.13 |
| PySide6 | `>=6.5,<7` | 6.12.0 |
| PyYAML | `>=6.0,<7` | 6.0.3 |

在项目目录中执行，建议使用虚拟环境：

```bash
python -m pip install -r requirements.txt
python run.py
```

启动时打开指定文件：

```bash
python run.py data.json
```

## 数据处理与隐私

- 文档处理均在本机完成，无账号系统、无遥测。
- 拒绝远程 Schema 引用，校验时不会下载外部 Schema。
- 历史记录默认开启，但**默认不保存 JSON 原文**；仍可能记录文件路径和操作元数据。
- 可在设置中关闭历史和最近文件、调整条数、删除记录，或主动开启原文保存。
- Windows 历史数据库路径为 `%APPDATA%\Qudo\JSONWorkbench\history.sqlite`。
- 保存文件时先写入目标目录中的临时文件，再替换目标文件。
- XML 输入拒绝 DOCTYPE 和外部实体，YAML 使用安全加载器。

### 文档内容保护

普通格式化保留键顺序和数组顺序，并重新解析输出，检查结果与输入数据是否一致。排序是独立操作，其中数组排序会有意改变元素顺序。

含重复键的文档禁止格式化、压缩和排序，避免解析成字典后丢失前面的值。这类文档也禁止树形编辑。

JSONC 格式化会移除注释，替换编辑器文本前需要确认。含 JSONC 注释的文档允许树形查看，但禁止树形编辑，避免静默移除注释。上述文本修改可通过 `Ctrl+Z` 撤销。

后台格式化回写前检查请求、文本版本、文档模式和标签是否仍然打开。其他后台功能页在输入或参数变化后丢弃旧结果。

## 限制与兼容性

| 范围 | 实际行为 |
| --- | --- |
| **JSON 与 JSONC** | 仅 JSONC 模式接受注释和尾逗号。`.json5` 扩展名会选择 JSONC 模式，不支持单引号、未加引号的键等完整 JSON5 语法。 |
| **JSON Schema** | 支持 draft-07 常用关键字及部分 2019-09、2020-12 扩展，并非完整的标准一致性实现。不支持的关键字会告警，部分检查可能被跳过或近似处理。 |
| **Schema 引用** | 支持本地 JSON 指针及相对本地文件，拒绝远程引用；动态引用作用域尚未完整实现。 |
| **Schema 正则** | 使用 Python 正则语义，与 JavaScript / ECMA-262 可能存在差异。 |
| **格式转换** | 目标格式不一定能完整表达 JSON 类型、数字写法和嵌套结构，请检查转换警告及参数。 |
| **大文件** | 超过 20 MB 打开前确认，默认加载上限为 200 MB。数据完整载入内存，并非流式编辑器。 |
| **后台计算** | 自动及手动校验、格式化、对比、转换、统计在适用的路径中超过 80 万字符后转为后台计算；查询和 Schema 校验还会检查节点规模。文件读取、部分输入解析及结果绘制仍在界面线程，并非所有计算都能立即中止。 |
| **编辑器保护** | 超过配置阈值关闭语法高亮，默认阈值为 2 MB；括号匹配也有大文档限制。 |
| **嵌套与偏移** | 解析器默认限制为 1,000 层，可在设置中调整。超过解析器的 8,388,608 字符阈值后不再收集节点偏移，编辑器跳转能力会受限。 |

解析、序列化、对比和统计引擎采用迭代遍历处理深层数据。性能测试包含调高深度上限后的 5,000 层嵌套，但不代表所有功能都支持任意深度。

## 快捷键

以下为 Windows 快捷键：

| 快捷键 | 功能 | 快捷键 | 功能 |
| --- | --- | --- | --- |
| `Ctrl+N` | 新建标签 | `Ctrl+Shift+F` | 格式化 |
| `Ctrl+O` | 打开文件 | `Ctrl+Shift+M` | 压缩 |
| `Ctrl+S` | 保存 | `F5` | 校验 |
| `Ctrl+Shift+S` | 另存为 | `Ctrl+Shift+D` | JSON 对比 |
| `Ctrl+W` | 关闭标签 | `Ctrl+Shift+P` | JSONPath 查询 |
| `Ctrl+F` / `Ctrl+H` | 查找 / 替换 | `Ctrl+Shift+V` | JSON Schema |
| `Ctrl+G` | 跳转行 | `Ctrl+B` / `Ctrl+I` | 显示或隐藏侧边栏 / 侧面板 |
| `Ctrl+Shift+K` | 删除当前行 | `Ctrl++` / `Ctrl+-` / `Ctrl+0` | 放大 / 缩小 / 重置 |
| `Ctrl+Z` / `Ctrl+Y` | 撤销 / 重做 | `Ctrl+,` | 设置 |
| `Ctrl+A` | 全选 | `F1` | 关于 |

## 开发与测试

### 自动化测试

```bash
# 按模块在独立子进程运行全量测试
python tests/run_all.py

# 运行指定模块
python tests/run_all.py test_parser test_regressions

# 运行单个 unittest 模块
python -m unittest tests.test_parser -v
```

测试使用 Qt 的 `offscreen` 平台和无弹窗对话框。测试脚本同时检查 unittest 结果和进程退出码，单模块超过 180 秒判为失败。Qt 清理会等待后台任务，并在解释器退出前销毁控件。

测试覆盖解析、格式化、转换、查询、Schema、生成、统计、文件管理、编辑器、功能页、集成、多语言、性能及数据丢失回归场景。

### 应用自检

PowerShell：

```powershell
$env:JSONWB_SELFTEST = "1500"
python run.py
Remove-Item Env:JSONWB_SELFTEST
```

macOS / Linux shell：

```bash
JSONWB_SELFTEST=1500 python run.py
```

变量值表示启动后等待的毫秒数。应用执行冒烟检查，成功后以退出码 `0` 退出；输出 `SELFTEST n/n passed`，并将报告写入启动脚本或打包 exe 旁边的 `selftest_report.txt`。

Windows 打包版同样支持：

```powershell
$env:JSONWB_SELFTEST = "1500"
Start-Process -FilePath ".\dist\JSONWorkbench\JSONWorkbench.exe" -Wait
Remove-Item Env:JSONWB_SELFTEST
```

## 构建 Windows 便携版

在 Windows 上进入项目目录后执行：

```powershell
python -m pip install pyinstaller
python -m PyInstaller JsonWorkbench.spec --noconfirm
```

输出结构：

```text
dist/
└── JSONWorkbench/
    ├── JSONWorkbench.exe
    └── _internal/
```

采用 PyInstaller **onedir** 布局，避免每次启动都解包运行环境。分发时请打包整个目录，通常压缩为 ZIP；目标电脑不需要安装构建工具。

打包配置排除了不使用的 Qt 模块，并防止 Poppler、Conda 等工具中不兼容的同名 `icuuc.dll` 替代 Windows 系统 ICU 依赖。

## 项目结构

```text
JSONWorkbench/
├── run.py                    # 应用入口与自检模式
├── JsonWorkbench.spec        # PyInstaller 配置
├── requirements.txt
├── README.md                 # 英文文档
├── README.zh-CN.md           # 简体中文文档
├── resources/
│   ├── icons/                # 按主题着色的 SVG 图标
│   ├── styles/base.qss       # 样式模板
│   └── samples/              # 正常、错误及 JSONC 示例
├── app/
│   ├── core/                 # 值模型、解析、格式化、校验、文件读写
│   ├── engines/              # 对比、查询、Schema、转换、分析及生成
│   ├── editor/               # 编辑器、高亮、行号、查找替换
│   ├── models/               # 懒加载 JSON 树模型
│   ├── modules/              # 功能页面
│   ├── widgets/              # 主窗口、标签、导航、主题及状态栏
│   ├── settings/             # QSettings 封装
│   ├── history/              # SQLite 历史记录
│   ├── utils/                # 资源、后台任务及对话框
│   ├── i18n.py
│   ├── i18n_zh.py
│   └── selftest.py
└── tests/                    # 单元、界面、集成、性能及回归测试
```

`core` 和 `engines` 不依赖 Qt，可独立测试。功能页与控件通过结果对象、回调和信号，将算法引擎连接到桌面界面。

## 依赖与许可

| 组件 | 用途 | 许可信息 |
| --- | --- | --- |
| Python | 运行环境与标准库 | PSF |
| PySide6 / Qt | 桌面界面、SVG 渲染及设置 | LGPLv3，详见相应发行包的许可说明 |
| PyYAML | YAML 转换 | MIT |
| PyInstaller | 构建期打包 | GPL，附带 bootloader 例外 |

应用当前未包含独立的 `LICENSE` 文件。上表仅描述第三方依赖的许可，不代表应用代码已获得相同许可授权。

相关网页工具：[Qudo JSON 格式化](https://www.qudo.cc/zh/json-formatter)。

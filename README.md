<p align="center">
  <img src="resources/icons/logo.svg" alt="JSON Workbench" width="80" height="80">
</p>

<h1 align="center">JSON Workbench</h1>

<p align="center">
  An offline desktop toolkit for editing, validating, comparing, querying, and converting JSON.
</p>

<p align="center">
  <strong>English</strong> · <a href="README.zh-CN.md">简体中文</a>
</p>

JSON Workbench brings everyday JSON tools into one desktop application. Work with multiple documents, inspect nested data, compare API responses, run JSONPath queries, validate schemas, and generate test data—all on your own computer.

**No account. No uploads. No telemetry.** JSON content is excluded from saved history by default.

Built with Python and PySide6. The current version is **1.0.0**. Windows 10/11 is the packaged target; the source is designed for cross-platform use, but macOS and Linux have not been verified in this release.

## Contents

- [Features](#features)
- [Getting started](#getting-started)
- [Data handling and privacy](#data-handling-and-privacy)
- [Limits and compatibility](#limits-and-compatibility)
- [Keyboard shortcuts](#keyboard-shortcuts)
- [Development and testing](#development-and-testing)
- [Building a Windows package](#building-a-windows-package)
- [Project structure](#project-structure)
- [Dependencies and licensing](#dependencies-and-licensing)

## Features

| Tool | What you can do |
| --- | --- |
| **Editor** | Work in movable tabs with syntax highlighting, line numbers, bracket matching, auto-indent, bracket completion, and unsaved-change indicators. |
| **Format and minify** | Use 2, 4, or 8 spaces, tabs, or compact output. Preserve number literals such as `1.230`, `1e5`, `-0`, and large integers. |
| **Sort keys** | Choose case-insensitive alphabetical or Unicode order, with optional recursive sorting and explicit array sorting. |
| **Validator** | Locate syntax errors by line, column, and offset. Review suggested fixes before applying them. Detect duplicate keys and precision risks. |
| **Tree viewer** | Browse nodes created on demand, search keys and values, copy JSONPath expressions, inspect node properties, and edit supported documents. |
| **JSON Diff** | Compare structure and types, distinguish key reordering from data changes, match arrays by index, set, or key field, and export Markdown, HTML, or JSON reports. |
| **JSONPath** | Query with child selectors, wildcards, recursive descent, negative indexes, unions, slices, filters, regular expressions, and functions. Locate matches in the tree or editor when offsets are available. |
| **Converter** | Convert between JSON and CSV, XML, or YAML. Escape or unescape strings, unwrap encoded JSON, split arrays, merge documents, and remove duplicates. |
| **JSON Schema** | Load or paste local schemas, infer schemas from documents, validate common draft-07 and 2020-12 keywords, and export reports. |
| **Statistics** | Inspect node and type counts, nesting depth, key reuse, string and array sizes, numeric summaries, size hotspots, and structural patterns. |
| **Utilities** | Find duplicate keys, empty values, and matching keys or values; split arrays into files and analyze document structure. |
| **Data generator** | Generate objects from field definitions or schemas. Create mock names, email addresses, dates, UUIDs, and more, with reproducible seeded output. |
| **History** | Review recent files, formatting actions, queries, and schema checks. Restore available files or explicitly saved content. |

The interface supports **English and Simplified Chinese**, **light and dark themes**, high-DPI displays, and saved window and splitter positions. Simplified Chinese is the default language.

### Editing and file support

- Drag files into the window or tab bar; open, save, and use Save As.
- Detect UTF-8, UTF-8 with BOM, UTF-16, and GB18030; preserve LF or CRLF line endings.
- Find and replace plain text or regular expressions, including capture-group replacements.
- Recognize JSON strings that wrap another JSON document and unwrap multiple layers.
- Keep text replacements undoable, including formatting, sorting, and tree edits.

## Getting started

### Windows portable package

1. Extract the entire portable ZIP archive.
2. Open the extracted `JSONWorkbench` folder.
3. Run `JSONWorkbench.exe`.

Keep the `_internal` folder beside the executable. The portable package includes its Python and Qt runtime; a separate Python installation is not required. Settings and history are stored in your user profile, rather than beside the executable.

### Run from source

| Requirement | Supported version | Development environment |
| --- | --- | --- |
| Python | 3.10+ | 3.13 |
| PySide6 | `>=6.5,<7` | 6.12.0 |
| PyYAML | `>=6.0,<7` | 6.0.3 |

From the project directory, preferably in a virtual environment:

```bash
python -m pip install -r requirements.txt
python run.py
```

To open a file at startup:

```bash
python run.py data.json
```

## Data handling and privacy

- Document processing happens locally. The application has no account system or telemetry.
- Remote schema references are rejected; validation does not download external schemas.
- History is enabled by default, but **JSON document content is not stored by default**. File paths and operation metadata may still be recorded.
- You can disable history and recent files, adjust history limits, delete entries, or enable content storage in Settings.
- On Windows, the history database is stored at `%APPDATA%\Qudo\JSONWorkbench\history.sqlite`.
- File saves use a temporary file in the destination directory followed by replacement of the target file.
- XML input rejects DOCTYPE declarations and external entities. YAML loading uses safe loaders.

### Protecting document content

Ordinary formatting preserves key order and array order, then parses the output again to check that it matches the input data. Sorting is an explicit operation; sorting array elements intentionally changes their order.

Documents with duplicate keys cannot be formatted, minified, or sorted, because converting them to a dictionary would discard earlier values. Tree editing is also disabled for these documents.

Formatting JSONC removes comments and requires confirmation before replacing the editor text. Documents containing JSONC comments can be viewed in the tree, but tree editing is disabled to avoid silently removing those comments. These text changes can be undone with `Ctrl+Z`.

Background formatting checks the request, document revision, document mode, and whether the tab is still open before writing results. Other background pages discard results when their inputs or options have changed.

## Limits and compatibility

| Area | Behavior |
| --- | --- |
| **JSON and JSONC** | Comments and trailing commas are accepted only in JSONC mode. The `.json5` extension selects JSONC mode; full JSON5 syntax, including single-quoted strings and unquoted keys, is not supported. |
| **JSON Schema** | Common draft-07 keywords and selected 2019-09 / 2020-12 additions are supported. This is not a complete standards-conformance implementation. Unsupported keywords produce warnings; some checks may therefore be skipped or approximated. |
| **Schema references** | Local JSON pointers and relative local files are supported. Remote references are rejected. Dynamic reference scope is not fully implemented. |
| **Schema regular expressions** | Patterns use Python regular-expression semantics, which can differ from JavaScript / ECMA-262. |
| **Conversions** | Target formats cannot always represent JSON types, number formatting, or nested structures exactly. Review conversion warnings and options. |
| **Large files** | Files over 20 MB require confirmation. The default load limit is 200 MB. Data is loaded into memory; this is not a streaming editor. |
| **Background work** | Automatic validation, manual validation, formatting, comparison, conversion, and statistics use background computation above 800,000 characters where applicable. Queries and schema validation also use a node-count check. File loading, some input parsing, and result rendering still run on the GUI thread. Not every calculation can be stopped immediately. |
| **Editor protection** | Syntax highlighting is disabled above the configured size threshold, which defaults to 2 MB. Bracket matching also has a large-document limit. |
| **Nesting and offsets** | The default parser nesting limit is 1,000 and can be changed in Settings. Node offsets are not collected above the parser's 8,388,608-character threshold, limiting editor jumps. |

The parser, serializer, diff, and statistics engines use iterative traversal for deeply nested data. Performance tests include 5,000-level nesting with an increased depth limit; that does not mean every feature supports arbitrary nesting.

## Keyboard shortcuts

Windows shortcuts:

| Shortcut | Action | Shortcut | Action |
| --- | --- | --- | --- |
| `Ctrl+N` | New tab | `Ctrl+Shift+F` | Format |
| `Ctrl+O` | Open file | `Ctrl+Shift+M` | Minify |
| `Ctrl+S` | Save | `F5` | Validate |
| `Ctrl+Shift+S` | Save As | `Ctrl+Shift+D` | JSON Diff |
| `Ctrl+W` | Close tab | `Ctrl+Shift+P` | JSONPath query |
| `Ctrl+F` / `Ctrl+H` | Find / Replace | `Ctrl+Shift+V` | JSON Schema |
| `Ctrl+G` | Go to line | `Ctrl+B` / `Ctrl+I` | Toggle sidebar / side panel |
| `Ctrl+Shift+K` | Delete line | `Ctrl++` / `Ctrl+-` / `Ctrl+0` | Zoom in / out / reset |
| `Ctrl+Z` / `Ctrl+Y` | Undo / Redo | `Ctrl+,` | Settings |
| `Ctrl+A` | Select all | `F1` | About |

## Development and testing

### Automated tests

```bash
# Run all modules in isolated subprocesses
python tests/run_all.py

# Run selected modules
python tests/run_all.py test_parser test_regressions

# Run a single unittest module
python -m unittest tests.test_parser -v
```

Tests use Qt's `offscreen` platform and headless dialogs. The runner checks both unittest results and process exit codes, and fails a module after 180 seconds. Qt cleanup waits for background tasks and destroys widgets before interpreter shutdown.

The suite covers parsing, formatting, converters, queries, schemas, generation, statistics, file handling, editor behavior, pages, integration, translations, performance, and data-loss regressions.

### Application self-test

PowerShell:

```powershell
$env:JSONWB_SELFTEST = "1500"
python run.py
Remove-Item Env:JSONWB_SELFTEST
```

macOS / Linux shell:

```bash
JSONWB_SELFTEST=1500 python run.py
```

The value sets the startup delay in milliseconds. The application runs its smoke checks and exits with code `0` on success. It prints `SELFTEST n/n passed` and writes `selftest_report.txt` beside the launcher or packaged executable.

The packaged Windows executable supports the same mode:

```powershell
$env:JSONWB_SELFTEST = "1500"
Start-Process -FilePath ".\dist\JSONWorkbench\JSONWorkbench.exe" -Wait
Remove-Item Env:JSONWB_SELFTEST
```

## Building a Windows package

Run on Windows from the project directory:

```powershell
python -m pip install pyinstaller
python -m PyInstaller JsonWorkbench.spec --noconfirm
```

Output:

```text
dist/
└── JSONWorkbench/
    ├── JSONWorkbench.exe
    └── _internal/
```

The build uses PyInstaller's **onedir** layout to avoid unpacking the runtime at each launch. Distribute the entire folder, usually as a ZIP archive. Build tools are not required on the destination computer.

The packaging configuration excludes unused Qt modules and prevents an incompatible `icuuc.dll` from tools such as Poppler or Conda from replacing Windows' system ICU dependency.

## Project structure

```text
JSONWorkbench/
├── run.py                    # Application entry point and self-test mode
├── JsonWorkbench.spec        # PyInstaller configuration
├── requirements.txt
├── README.md                 # English documentation
├── README.zh-CN.md           # Simplified Chinese documentation
├── resources/
│   ├── icons/                # Theme-aware SVG icons
│   ├── styles/base.qss       # Stylesheet template
│   └── samples/              # Valid, invalid, and JSONC samples
├── app/
│   ├── core/                 # Value model, parser, formatter, validator, file I/O
│   ├── engines/              # Diff, JSONPath, schema, conversions, analysis, generation
│   ├── editor/               # Editor, highlighting, line numbers, find/replace
│   ├── models/               # Lazy JSON tree model
│   ├── modules/              # Feature pages
│   ├── widgets/              # Main window, tabs, navigation, themes, status bar
│   ├── settings/             # QSettings wrapper
│   ├── history/              # SQLite history storage
│   ├── utils/                # Resources, background jobs, dialogs
│   ├── i18n.py
│   ├── i18n_zh.py
│   └── selftest.py
└── tests/                    # Unit, UI, integration, performance, and regression tests
```

`core` and `engines` do not depend on Qt and can be tested independently. Pages and widgets connect those engines to the desktop interface through result objects, callbacks, and signals.

## Dependencies and licensing

| Component | Purpose | License information |
| --- | --- | --- |
| Python | Runtime and standard library | PSF |
| PySide6 / Qt | Desktop interface, SVG rendering, settings | LGPLv3; see the distribution's license notices |
| PyYAML | YAML conversion | MIT |
| PyInstaller | Build-time packaging | GPL with a bootloader exception |

The application currently does not include a standalone `LICENSE` file. Third-party license information above describes the dependencies, not a license grant for the application code.

Related web tool: [Qudo JSON Formatter](https://www.qudo.cc/zh/json-formatter).

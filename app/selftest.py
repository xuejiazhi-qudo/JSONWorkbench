"""应用自检：离屏/带界面都能跑，用来验证核心链路没有被改坏。

用法::

    JSONWB_SELFTEST=1500 python run.py

每一项检查都真实执行（新建标签、格式化、压缩、校验、切主题、切语言…），
失败会打印异常类型与消息，退出码非 0。
"""

from __future__ import annotations

import traceback
from typing import Callable, List, Tuple

SAMPLE_TEXT = """{"name":"示例","version":2,"ok":true,"ratio":1.230,
 "tags":["a","b"],"nested":{"deep":{"value":null}},"big":12345678901234567890123456789}"""

INVALID_TEXT = '{"a":1,}'


def _checks() -> List[Tuple[str, Callable[[object], object]]]:
    from app.core.jsonformatter import FormatOptions, format_text, json_equal, minify_text
    from app.core.jsonparser import parse_json
    from app.i18n import LANG_EN, LANG_ZH

    def core_roundtrip(_window):
        pretty = format_text(SAMPLE_TEXT, FormatOptions(indent="  "))
        assert pretty.ok, pretty.error_message
        assert pretty.semantics_preserved, "semantics changed by formatting"
        compact = minify_text(SAMPLE_TEXT)
        assert compact.ok, compact.error_message
        assert "1.230" in compact.output, "number literal was rewritten"
        assert "12345678901234567890123456789" in compact.output, "big integer was rewritten"
        assert json_equal(parse_json(pretty.output).value, parse_json(SAMPLE_TEXT).value)
        return f"{len(SAMPLE_TEXT)}→{len(compact.output)} chars"

    def core_error_detection(_window):
        result = parse_json(INVALID_TEXT)
        assert not result.ok
        assert result.error.code == "trailing_comma", result.error.code
        return f"{result.error.code}@{result.error.line}:{result.error.column}"

    def new_tab_and_format(window):
        tab = window.new_tab(SAMPLE_TEXT)
        window.workspace.tabs.setCurrentWidget(tab)
        window.workspace.format_document()
        text = tab.text()
        assert "\n" in text, "format did not produce multi-line output"
        assert '"name"' in text
        return f"{len(text)} chars"

    def minify_document(window):
        tab = window.workspace.current_tab()
        assert tab is not None
        window.workspace.minify_document()
        text = tab.text()
        assert "\n" not in text, "minify left newlines"
        assert text.startswith("{"), text[:20]
        return f"{len(text)} chars"

    def validate_invalid(window):
        tab = window.new_tab(INVALID_TEXT)
        window.workspace.tabs.setCurrentWidget(tab)
        result = window.workspace.validate_now()
        assert result is not None and not result.valid, "invalid document passed validation"
        assert window.status.valid_label.objectName() == "StatusInvalid"
        return result.error.code

    def validate_valid(window):
        tab = window.new_tab(SAMPLE_TEXT)
        window.workspace.tabs.setCurrentWidget(tab)
        result = window.workspace.validate_now()
        assert result is not None and result.valid, "valid document failed validation"
        assert window.status.valid_label.objectName() == "StatusValid"
        return f"{result.stats.node_count} nodes"

    def find_bar(window):
        tab = window.workspace.current_tab()
        tab.show_find(False)
        assert tab.find_bar.isVisible()
        tab.find_bar.find_edit.setText("name")
        found = tab.find_bar.find_next()
        tab.find_bar.hide_bar()
        assert found, "find did not locate an existing substring"
        return "ok"

    def theme_toggle(window):
        before = window.theme_action is not None
        assert before
        from app.widgets.theme import theme

        original = theme.name
        window.toggle_theme()
        assert theme.name != original
        window.toggle_theme()
        assert theme.name == original
        return theme.name

    def language_toggle(window):
        original = settings_language()
        window.change_language(LANG_EN)
        assert window._actions["Format"].text() == "Format"
        window.change_language(LANG_ZH)
        window.change_language(original)
        return "ok"

    def sidebar_navigation(window):
        window._on_nav("validator")
        assert window.stack.currentWidget() is window.workspace
        window._on_nav("formatter")
        return window.sidebar.current_id()

    def converter_page(window):
        page = window.converter_page
        page.input_editor.set_text('{"users":[{"id":1,"name":"张三"}]}')
        page.conversion_combo.setCurrentIndex(0)
        page.run_conversion()
        assert page._last_result.ok, page._last_result.error
        assert "users.id" in page.output_editor.text()
        page.conversion_combo.setCurrentIndex(4)
        page.run_conversion()
        assert "张三" in page.output_editor.text()
        return "csv+yaml ok"

    def tools_page(window):
        page = window.tools_page
        page.set_document_provider(lambda: ('{"a":1,"a":2}', "json"))
        page.run_duplicates()
        assert page.dup_tree.topLevelItemCount() == 1
        return "1 duplicate"

    def stats_page(window):
        page = window.stats_page
        page.set_document(SAMPLE_TEXT, "selftest")
        assert page.rows_tree.topLevelItemCount() >= 20
        return f"{page.rows_tree.topLevelItemCount()} rows"

    def generator_page(window):
        page = window.generator_page
        page.seed_edit.setText("42")
        page.run_generate()
        first = page.output_editor.text()
        page.run_generate()
        assert first == page.output_editor.text(), "generator is not deterministic with a seed"
        assert first.strip().startswith("{")
        return "deterministic"

    def tree_panel(window):
        panel = window.workspace.tree_panel
        tab = window.workspace.current_tab()
        assert tab is not None
        result = tab.validation(force=True)
        assert result.valid
        panel.set_document(result.value, result.parse.offsets)
        assert panel.locate_path(("name",)) or panel.model.has_document()
        return "ok"

    def history_page(window):
        from app.history.store import KIND_FORMAT, HistoryStore

        store = HistoryStore()
        try:
            store.record(KIND_FORMAT, "selftest-entry", detail="selftest")
            entries = store.list(kind=KIND_FORMAT, limit=5)
            assert any(entry.label == "selftest-entry" for entry in entries)
            window.history_page.refresh()
            assert window.history_page.tree.topLevelItemCount() >= 1
            return f"{store.count()} entries"
        finally:
            store.close()

    def jsonpath_page(window):
        page = window.jsonpath_page
        if page is None:
            return "skipped (engine not built)"
        from app.core.jsonparser import parse_json

        parsed = parse_json('{"users":[{"name":"张三"},{"name":"Alice"}]}')
        assert parsed.ok
        page.set_document(parsed.value, "selftest")
        page.set_offsets(parsed.offsets)
        page.expression_edit.setText("$.users[*].name")
        page.run_query()
        assert page.result_tree.topLevelItemCount() == 2, page.summary_label.text()
        return "2 matches"

    def schema_page(window):
        page = window.schema_page
        if page is None:
            return "skipped (engine not built)"
        tab = window.new_tab('{"name":"selftest","age":3}')
        window.workspace.tabs.setCurrentWidget(tab)
        page.load_instance()
        page.schema_editor.set_text(
            '{"type":"object","required":["name"],"properties":{"name":{"type":"string"}}}')
        page.run_validation()
        assert page._last_result is not None
        assert not any(e.keyword == "internal_error" for e in page._last_result.errors), \
            "schema engine raised internally"
        assert page._last_result.valid, page._last_result.errors
        page.schema_editor.set_text('{"type":"object","required":["missing"]}')
        page.run_validation()
        assert not page._last_result.valid
        assert page._last_result.errors[0].keyword == "required"
        return "required detected"

    def diff_page(window):
        page = window.diff_page
        if page is None:
            return "skipped (engine not built)"
        page.set_documents('{"a":1,"b":[1,2]}', '{"a":1,"b":[1,2]}')
        page.run_diff()
        assert page._result is not None and not page._result.changed
        page.set_documents('{"a":1,"b":[1,2]}', '{"a":2,"b":[1,2]}')
        page.run_diff()
        assert page._result.changed
        kinds = {item.kind for item in page._result.items}
        assert kinds == {"changed"}, kinds
        return "equal + changed"

    def unwrap_double_encoded(window):
        import json as _json

        wrapped = _json.dumps(_json.dumps('{"data":{"id":1},"code":0}'))
        tab = window.new_tab(wrapped)
        window.workspace.tabs.setCurrentWidget(tab)
        tab.validation(force=True)
        bar = window.workspace.unwrap_bar
        assert bar.isVisible(), "unwrap banner did not appear for a double-encoded document"
        window.workspace.unwrap_button.click()
        from app.core.jsonparser import parse_json

        assert parse_json(tab.text()).value == {"data": {"id": 1}, "code": 0}
        assert not bar.isVisible(), "unwrap banner stayed visible after unwrapping"
        return "unwrapped"

    def tab_close(window):
        count = window.workspace.tabs.count()
        window.close_tab_at(count - 1)
        assert window.workspace.tabs.count() >= 1
        return f"{count}→{window.workspace.tabs.count()}"

    return [
        ("core: format/minify round-trip", core_roundtrip),
        ("core: syntax error detection", core_error_detection),
        ("ui: new tab + format", new_tab_and_format),
        ("ui: minify", minify_document),
        ("ui: invalid document is reported", validate_invalid),
        ("ui: valid document is reported", validate_valid),
        ("ui: find bar locates text", find_bar),
        ("ui: theme toggle", theme_toggle),
        ("ui: language toggle", language_toggle),
        ("ui: sidebar navigation", sidebar_navigation),
        ("ui: converter page", converter_page),
        ("ui: tools page", tools_page),
        ("ui: stats page", stats_page),
        ("ui: generator page", generator_page),
        ("ui: tree panel", tree_panel),
        ("ui: unwrap double-encoded JSON", unwrap_double_encoded),
        ("ui: history page", history_page),
        ("ui: jsonpath page", jsonpath_page),
        ("ui: schema page", schema_page),
        ("ui: diff page", diff_page),
        ("ui: close tab", tab_close),
    ]


def settings_language() -> str:
    from app.i18n import current_language

    return current_language()


def run_selftest(window) -> Tuple[str, bool]:
    """执行全部自检，返回 (报告文本, 是否全部通过)。"""
    lines: List[str] = []
    passed = 0
    for name, fn in _checks():
        try:
            detail = fn(window)
        except AssertionError as exc:
            lines.append(f"[FAIL] {name}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - 自检要抓住任何异常
            lines.append(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
            lines.append(traceback.format_exc(limit=3).rstrip())
            continue
        passed += 1
        lines.append(f"[ ok ] {name}" + (f" ({detail})" if detail else ""))
    total = len(_checks())
    ok = passed == total
    header = f"SELFTEST {passed}/{total} passed"
    return header + "\n" + "\n".join(lines) + ("\nOK" if ok else "\nFAILED"), ok

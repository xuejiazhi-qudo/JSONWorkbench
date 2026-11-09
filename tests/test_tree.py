"""树模型与树面板测试：懒加载、增删改、搜索、路径、定位。"""

from __future__ import annotations

import unittest

from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtWidgets import QApplication, QDialog

from app.core.jsonparser import parse_json
from app.core.jsonvalue import JsonNumber
from app.models.jsontreemodel import (
    COL_KEY,
    COL_TYPE,
    COL_VALUE,
    JsonNode,
    JsonTreeModel,
    path_to_string,
    value_preview,
)
from app.modules.tree_view import NodeEditDialog, parse_scalar_text

_app = QApplication.instance() or QApplication([])

SAMPLE = '{"users":[{"id":1,"name":"张三"},{"id":2,"name":"Alice"}],"meta":{"n":null},"t":true}'


def make_model(text: str = SAMPLE, offsets: bool = False):
    result = parse_json(text, track_offsets=offsets)
    assert result.ok, result.error
    model = JsonTreeModel()
    model.set_document(result.value, result.offsets)
    return model, result


def child(model, parent, row):
    return model.index(row, 0, parent)


def vindex(model, path):
    """取路径对应节点的"值列"索引（index_for_path 默认给第 0 列）。"""
    idx = model.index_for_path(path)
    return model.index(idx.row(), COL_VALUE, idx.parent())


class TestModelStructure(unittest.TestCase):
    def test_root_and_columns(self):
        model, _ = make_model()
        root = QModelIndex()
        self.assertEqual(model.rowCount(root), 3)      # users, meta, t
        self.assertEqual(model.columnCount(root), 3)
        self.assertEqual(model.data(model.index(0, COL_KEY, root)), "users")
        self.assertEqual(model.data(model.index(0, COL_TYPE, root)), "Array")
        self.assertEqual(model.data(model.index(2, COL_TYPE, root)), "Boolean")

    def test_nested_paths(self):
        model, _ = make_model()
        root = QModelIndex()
        users = child(model, root, 0)
        first = child(model, users, 0)
        name = child(model, first, 1)
        self.assertEqual(model.path_at(name), ("users", 0, "name"))
        value_index = model.index(name.row(), COL_VALUE, first)
        self.assertEqual(model.data(value_index), '"张三"')
        self.assertEqual(model.parent(name), first)
        self.assertEqual(model.parent(users), root)

    def test_lazy_child_creation(self):
        model, _ = make_model()
        root_node = model.node_at(QModelIndex())
        self.assertEqual(root_node._children, {})
        child(model, QModelIndex(), 0)
        self.assertEqual(len(root_node._children), 1)

    def test_scalar_root(self):
        model, _ = make_model("42")
        self.assertEqual(model.rowCount(QModelIndex()), 0)
        self.assertEqual(model.data(QModelIndex()), "42")

    def test_empty_containers(self):
        model, _ = make_model('{"a":{},"b":[]}')
        root = QModelIndex()
        self.assertEqual(model.rowCount(child(model, root, 0)), 0)
        self.assertEqual(model.data(model.index(0, COL_VALUE, root)), "{…} 0 keys")
        self.assertEqual(model.data(model.index(1, COL_VALUE, root)), "[…] 0 items")

    def test_index_for_path_roundtrip(self):
        model, _ = make_model()
        path = ("users", 1, "name")
        index = model.index_for_path(path)
        self.assertTrue(index.isValid())
        self.assertEqual(model.path_at(index), path)
        self.assertEqual(model.data(vindex(model, path)), '"Alice"')

    def test_index_for_path_missing(self):
        model, _ = make_model()
        self.assertFalse(model.index_for_path(("nope",)).isValid())
        self.assertFalse(model.index_for_path(("users", 9)).isValid())

    def test_deep_path_does_not_recurse(self):
        depth = 800
        text = '{"a":' * depth + "1" + "}" * depth
        model, _ = make_model(text)
        path = tuple(["a"] * depth)
        index = model.index_for_path(path)
        self.assertTrue(index.isValid())
        self.assertEqual(model.data(vindex(model, path)), "1")

    def test_offsets_are_exposed(self):
        model, _ = make_model('{"a": 1}', offsets=True)
        info = model.offset_for_path(("a",))
        self.assertIsNotNone(info)
        self.assertEqual(info.value_offset, 6)  # '{"a": 1}' 里 1 的偏移


class TestModelEditing(unittest.TestCase):
    def test_set_node_value(self):
        model, _ = make_model()
        index = vindex(model, ("users", 0, "name"))
        changed = []
        model.value_changed.connect(lambda: changed.append(1))
        self.assertTrue(model.set_node_value(index, "李四"))
        self.assertEqual(model.data(index), '"李四"')
        self.assertEqual(model.root_value()["users"][0]["name"], "李四")
        self.assertEqual(changed, [1])

    def test_set_value_on_container_is_rejected(self):
        model, _ = make_model()
        index = vindex(model, ("users",))
        self.assertFalse(model.set_node_value(index, []))

    def test_add_child_to_object(self):
        model, _ = make_model('{"a":1}')
        root = QModelIndex()
        self.assertTrue(model.add_child(root, "b", 2))
        self.assertEqual(model.rowCount(root), 2)
        self.assertEqual(model.root_value(), {"a": 1, "b": 2})

    def test_add_duplicate_key_is_rejected(self):
        model, _ = make_model('{"a":1}')
        self.assertFalse(model.add_child(QModelIndex(), "a", 2))
        self.assertEqual(model.rowCount(QModelIndex()), 1)

    def test_add_child_to_array(self):
        model, _ = make_model("[1]")
        root = QModelIndex()
        self.assertTrue(model.add_child(root, None, 2))
        self.assertEqual(model.root_value(), [1, 2])

    def test_remove_node(self):
        model, _ = make_model('{"a":1,"b":2}')
        index = model.index_for_path(("a",))
        self.assertTrue(model.remove_node(index))
        self.assertEqual(model.root_value(), {"b": 2})
        self.assertEqual(model.rowCount(QModelIndex()), 1)

    def test_remove_array_element_shifts(self):
        model, _ = make_model("[10,20,30]")
        index = model.index_for_path((1,))
        self.assertTrue(model.remove_node(index))
        self.assertEqual(model.root_value(), [10, 30])

    def test_remove_root_is_rejected(self):
        model, _ = make_model('{"a":1}')
        self.assertFalse(model.remove_node(QModelIndex()))


class TestSearchAndInfo(unittest.TestCase):
    def test_search_keys_and_values(self):
        model, _ = make_model()
        paths = model.search("name")
        self.assertEqual(paths, [("users", 0, "name"), ("users", 1, "name")])
        paths = model.search("alice")
        self.assertEqual(paths, [("users", 1, "name")])

    def test_search_case_sensitive(self):
        model, _ = make_model()
        self.assertEqual(model.search("ALICE", case_sensitive=True), [])
        self.assertEqual(len(model.search("ALICE")), 1)

    def test_search_limit(self):
        model, _ = make_model("[" + ",".join("1" for _ in range(50)) + "]")
        paths = model.search("1", limit=5)
        self.assertLessEqual(len(paths), 5)
        self.assertEqual(len(paths), 5)

    def test_search_empty_query(self):
        model, _ = make_model()
        self.assertEqual(model.search(""), [])

    def test_search_hits_are_highlighted(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor

        model, _ = make_model()
        model.set_highlight_color(QColor("#24344F"))
        model.set_search_hits([("users", 0, "name")])
        hit = model.index_for_path(("users", 0, "name"))
        miss = model.index_for_path(("users", 1, "name"))
        self.assertEqual(model.data(hit, Qt.ItemDataRole.BackgroundRole).name(), "#24344f")
        self.assertIsNone(model.data(miss, Qt.ItemDataRole.BackgroundRole))
        model.set_search_hits([])
        self.assertIsNone(model.data(hit, Qt.ItemDataRole.BackgroundRole))

    def test_subtree_text(self):
        model, _ = make_model()
        index = model.index_for_path(("users", 0))
        expected = '{\n    "id": 1,\n    "name": "张三"\n}'
        self.assertEqual(model.subtree_text(index), expected)

    def test_statistics_for(self):
        model, _ = make_model(offsets=True)
        index = model.index_for_path(("users",))
        info = model.statistics_for(index)
        self.assertEqual(info["path"], "$.users")
        self.assertEqual(info["type"], "Array")
        self.assertEqual(info["children"], 2)
        self.assertIn("offset", info)
        scalar = model.statistics_for(model.index_for_path(("t",)))
        self.assertEqual(scalar["type"], "Boolean")
        self.assertNotIn("length", scalar)

    def test_statistics_for_string_length(self):
        model, _ = make_model('{"s":"abcdef"}')
        info = model.statistics_for(model.index_for_path(("s",)))
        self.assertEqual(info["length"], 6)


class TestHelpers(unittest.TestCase):
    def test_path_to_string_quoting(self):
        self.assertEqual(path_to_string(()), "$")
        self.assertEqual(path_to_string(("a", 0, "b")), "$.a[0].b")
        self.assertEqual(path_to_string(("weird key",)), "$['weird key']")
        self.assertEqual(path_to_string(("",)), "$['']")
        self.assertEqual(path_to_string(("dot.key",)), "$['dot.key']")
        self.assertEqual(path_to_string(("quote'key",)), '$["quote\'key"]')

    def test_value_preview_truncation(self):
        self.assertEqual(value_preview("x" * 500, 10), '"xxxxxxxxxx…"')
        self.assertEqual(value_preview(None), "null")
        self.assertEqual(value_preview(True), "true")
        self.assertEqual(value_preview(JsonNumber("1.230")), "1.230")

    def test_parse_scalar_text(self):
        self.assertEqual(parse_scalar_text("string", "hi"), (True, "hi", ""))
        self.assertEqual(parse_scalar_text("null", ""), (True, None, ""))
        ok, value, _ = parse_scalar_text("number", "1.230")
        self.assertTrue(ok)
        self.assertEqual(value_preview(value), "1.230")
        ok, _value, error = parse_scalar_text("number", "abc")
        self.assertFalse(ok)
        self.assertTrue(error)
        ok, value, _ = parse_scalar_text("object", '{"a":1}')
        self.assertTrue(ok and value == {"a": 1})
        ok, _v, error = parse_scalar_text("object", "[1]")
        self.assertFalse(ok)
        ok, _v, error = parse_scalar_text("array", "{")
        self.assertFalse(ok)


class TestDialog(unittest.TestCase):
    def test_edit_dialog_prefills(self):
        dialog = NodeEditDialog(mode="edit", kind="string", value="hi")
        self.assertEqual(dialog.text_edit.text(), "hi")
        self.assertEqual(dialog.stack.currentIndex(), 0)

    def test_add_dialog_requires_key_for_objects(self):
        dialog = NodeEditDialog(mode="add", kind="number", value="", allow_key=True)
        dialog.key_edit.setText("")
        dialog.text_edit.setText("5")
        dialog._accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertFalse(dialog.error_label.isHidden())

    def test_add_dialog_accepts_valid_number(self):
        dialog = NodeEditDialog(mode="add", kind="number", value="", allow_key=True)
        dialog.key_edit.setText("k")
        dialog.text_edit.setText("1.5")
        dialog._accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.result_key, "k")


class TestView(unittest.TestCase):
    def test_expand_limited_refuses_huge_documents(self):
        from app.modules.tree_view import TreePanel

        panel = TreePanel()
        big = "[" + ",".join("1" for _ in range(30000)) + "]"
        model, _ = make_model(big)
        panel.model = model
        panel.tree.setModel(model)
        self.assertFalse(panel.tree.expand_limited())

    def test_locate_path_selects_node(self):
        from app.modules.tree_view import TreePanel

        panel = TreePanel()
        model, _ = make_model()
        panel.set_document(model.root_value(), None)
        self.assertTrue(panel.locate_path(("users", 0, "name")))
        self.assertEqual(panel.tree.currentIndex().isValid(), True)
        self.assertFalse(panel.locate_path(("nope",)))

    def test_flags_mark_scalars_editable(self):
        model, _ = make_model()
        scalar = vindex(model, ("t",))
        container = vindex(model, ("users",))
        self.assertTrue(model.flags(scalar) & Qt.ItemFlag.ItemIsEditable)
        self.assertFalse(model.flags(container) & Qt.ItemFlag.ItemIsEditable)


if __name__ == "__main__":
    unittest.main(verbosity=2)

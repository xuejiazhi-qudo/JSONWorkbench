"""JSON 树模型：懒加载节点，支持编辑/增删/搜索/定位。

节点按需创建（只有展开过的容器才会生成子节点对象），
因此百万级数组也不会一次性吃掉内存。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt, Signal

from app.core.jsonformatter import dumps
from app.core.jsonparser import OffsetInfo
from app.core.jsonvalue import (
    TYPE_ARRAY,
    TYPE_OBJECT,
    json_type,
    number_literal,
    encode_json_string,
)

COL_KEY = 0
COL_VALUE = 1
COL_TYPE = 2
COLUMN_COUNT = 3

#: 搜索结果上限，避免在超大文档上无限收集
SEARCH_LIMIT = 500
#: 值预览截断长度
PREVIEW_LIMIT = 160

_TYPE_LABELS = {
    TYPE_OBJECT: "Object",
    TYPE_ARRAY: "Array",
    "string": "String",
    "number": "Number",
    "boolean": "Boolean",
    "null": "Null",
}


class JsonNode:
    """树中的一个节点；子节点懒加载。"""

    __slots__ = ("value", "parent", "key", "_children", "_keys")

    def __init__(self, value: Any, parent: Optional["JsonNode"] = None,
                 key: Any = None) -> None:
        self.value = value
        self.parent = parent
        self.key = key
        self._children: Dict[int, "JsonNode"] = {}
        self._keys: Optional[List[str]] = None

    # ------------------------------------------------------------------ 基本
    @property
    def kind(self) -> str:
        return json_type(self.value)

    @property
    def is_container(self) -> bool:
        return self.kind in (TYPE_OBJECT, TYPE_ARRAY)

    def child_count(self) -> int:
        if self.kind == TYPE_OBJECT:
            return len(self.value)
        if self.kind == TYPE_ARRAY:
            return len(self.value)
        return 0

    def _object_keys(self) -> List[str]:
        if self._keys is None or len(self._keys) != len(self.value):
            self._keys = list(self.value.keys())
        return self._keys

    def child_at(self, row: int) -> "JsonNode":
        node = self._children.get(row)
        if node is None:
            if self.kind == TYPE_OBJECT:
                key = self._object_keys()[row]
                node = JsonNode(self.value[key], self, key)
            else:
                node = JsonNode(self.value[row], self, row)
            self._children[row] = node
        return node

    def row_of(self, child: "JsonNode") -> int:
        if self.kind == TYPE_OBJECT:
            keys = self._object_keys()
            return keys.index(child.key) if child.key in keys else -1
        return child.key if isinstance(child.key, int) else -1

    def path(self) -> Tuple:
        segments = []
        node = self
        while node.parent is not None:
            segments.append(node.key)
            node = node.parent
        return tuple(reversed(segments))

    def drop_children_cache(self) -> None:
        self._children.clear()
        self._keys = None


def path_to_string(path: Tuple) -> str:
    """把路径元组转成 JSONPath 文本。

    需要引号的键使用单引号（RFC 9535 规范化形式），与 engines 层的
    ``path_string`` 保持一致，避免同一路径在不同界面显示成两种写法。
    """
    out = ["$"]
    for segment in path:
        if isinstance(segment, int):
            out.append(f"[{segment}]")
        elif _is_plain_key(segment):
            out.append(f".{segment}")
        elif "'" in segment:
            escaped = segment.replace("\\", "\\\\").replace('"', '\\"')
            out.append(f'["{escaped}"]')
        else:
            escaped = segment.replace("\\", "\\\\")
            out.append(f"['{escaped}']")
    return "".join(out)


def _is_plain_key(key: str) -> bool:
    if not key:
        return False
    if not (key[0].isalpha() or key[0] in "_$"):
        return False
    return all(ch.isalnum() or ch in "_$" for ch in key)


def value_preview(value: Any, limit: int = PREVIEW_LIMIT) -> str:
    """标量的可读预览；容器给出规模摘要。"""
    kind = json_type(value)
    if kind == TYPE_OBJECT:
        return f"{{…}} {len(value)} keys"
    if kind == TYPE_ARRAY:
        return f"[…] {len(value)} items"
    if kind == "string":
        text = value if len(value) <= limit else value[:limit] + "…"
        return encode_json_string(text)
    if kind == "number":
        return number_literal(value)
    if kind == "boolean":
        return "true" if value else "false"
    return "null"


class JsonTreeModel(QAbstractItemModel):
    """把解析结果暴露成 Qt 树。"""

    read_only = False

    #: 值被编辑/增删后发出，视图据此把整棵树回写到编辑器
    value_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._root: Optional[JsonNode] = None
        self._offsets: Optional[Dict[Tuple, OffsetInfo]] = None
        self._has_document = False
        self._type_colors: Dict[str, Any] = {}
        self._highlight_color: Any = None
        self._search_hits: set = set()

    def set_type_colors(self, colors: Dict[str, Any]) -> None:
        """colors: {json 类型: QColor}，主题切换时由视图刷新。"""
        self._type_colors = dict(colors)
        if self._has_document:
            top = self.index(0, 0)
            bottom = self.index(max(0, self.rowCount() - 1), COLUMN_COUNT - 1)
            self.dataChanged.emit(top, bottom)

    def set_highlight_color(self, color: Any) -> None:
        self._highlight_color = color

    def set_search_hits(self, paths) -> None:
        """标记搜索命中的节点路径（用于整树高亮）。"""
        self._search_hits = {tuple(path) for path in paths}
        if self._has_document:
            top = self.index(0, 0)
            bottom = self.index(max(0, self.rowCount() - 1), COLUMN_COUNT - 1)
            self.dataChanged.emit(top, bottom)

    # ------------------------------------------------------------------ 文档
    def set_document(self, value: Any, offsets: Optional[Dict[Tuple, OffsetInfo]] = None) -> None:
        self.beginResetModel()
        self._root = JsonNode(value)
        self._offsets = offsets
        self._has_document = True
        self.endResetModel()

    def clear(self) -> None:
        self.beginResetModel()
        self._root = None
        self._offsets = None
        self._has_document = False
        self.endResetModel()

    def has_document(self) -> bool:
        return self._has_document

    def root_value(self) -> Any:
        return self._root.value if self._root is not None else None

    def offset_for_path(self, path: Tuple) -> Optional[OffsetInfo]:
        if self._offsets is None:
            return None
        return self._offsets.get(path)

    # ------------------------------------------------------------------ 模型接口
    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return COLUMN_COUNT

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        node = self.node_at(parent)
        if node is None:
            return 0
        return node.child_count()

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        node = self.node_at(parent)
        if node is None or row < 0 or row >= node.child_count():
            return QModelIndex()
        return self.createIndex(row, column, node.child_at(row))

    def parent(self, index: QModelIndex) -> QModelIndex:  # noqa: A003 (Qt 命名)
        node = self.node_at(index)
        if node is None or node.parent is None:
            return QModelIndex()
        parent_node = node.parent
        if parent_node is self._root:
            return QModelIndex()
        grand = parent_node.parent
        if grand is None:
            return QModelIndex()
        row = grand.row_of(parent_node)
        return self.createIndex(row, 0, parent_node) if row >= 0 else QModelIndex()

    def node_at(self, index: QModelIndex) -> Optional[JsonNode]:
        if not index.isValid():
            return self._root
        node = index.internalPointer()
        return node if isinstance(node, JsonNode) else None

    def path_at(self, index: QModelIndex) -> Tuple:
        node = self.node_at(index)
        return node.path() if node is not None else ()

    def index_for_path(self, path: Tuple) -> QModelIndex:
        """按路径取 QModelIndex（不展开视图，仅构造索引链）。"""
        if not self._has_document:
            return QModelIndex()
        parent = QModelIndex()
        node = self._root
        for segment in path:
            if node is None:
                return QModelIndex()
            if isinstance(segment, int):
                if node.kind != TYPE_ARRAY or not (0 <= segment < len(node.value)):
                    return QModelIndex()
                row = segment
            else:
                if node.kind != TYPE_OBJECT or segment not in node.value:
                    return QModelIndex()
                row = node._object_keys().index(segment)
            parent = self.createIndex(row, 0, node.child_at(row))
            node = parent.internalPointer()
        return parent

    # ------------------------------------------------------------------ 展示
    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        node = self.node_at(index)
        if node is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            column = index.column() if index.isValid() else COL_VALUE
            if column == COL_KEY:
                if node.parent is None:
                    return "$"
                return node.key if isinstance(node.key, str) else f"[{node.key}]"
            if column == COL_VALUE:
                return value_preview(node.value)
            return _TYPE_LABELS[json_type(node.value)]
        if role == Qt.ItemDataRole.ToolTipRole:
            path = path_to_string(node.path())
            preview = value_preview(node.value, 400)
            return f"{path}\n{preview}"
        if role == Qt.ItemDataRole.BackgroundRole and self._search_hits:
            if node.path() in self._search_hits and self._highlight_color is not None:
                return self._highlight_color
            return None
        if role == Qt.ItemDataRole.ForegroundRole and index.column() in (COL_VALUE, COL_TYPE):
            color = self._type_colors.get(json_type(node.value))
            if color is not None:
                return color
            return None
        if role == Qt.ItemDataRole.UserRole:
            return node
        if role == Qt.ItemDataRole.UserRole + 1:
            return path_to_string(node.path())
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        base = super().flags(index)
        node = self.node_at(index)
        if node is None:
            return base
        if not self.read_only and index.column() == COL_VALUE and not node.is_container:
            base |= Qt.ItemFlag.ItemIsEditable
        return base

    def headerData(self, section: int, orientation: Qt.Orientation,  # noqa: N802
                   role: int = Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return ("Key", "Value", "Type")[section]
        return None

    # ------------------------------------------------------------------ 编辑
    def set_node_value(self, index: QModelIndex, value: Any) -> bool:
        if self.read_only:
            return False
        node = self.node_at(index)
        if node is None or node.parent is None or node.is_container:
            return False
        parent = node.parent
        if parent.kind == TYPE_OBJECT:
            parent.value[node.key] = value
        else:
            parent.value[node.key] = value
        node.value = value
        node.drop_children_cache()
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.DisplayRole])
        self.value_changed.emit()
        return True

    def remove_node(self, index: QModelIndex) -> bool:
        if self.read_only:
            return False
        node = self.node_at(index)
        if node is None or node.parent is None:
            return False
        parent = node.parent
        row = parent.row_of(node)
        if row < 0:
            return False
        self.beginRemoveRows(self.parent(index), row, row)
        if parent.kind == TYPE_OBJECT:
            del parent.value[node.key]
            parent._keys = None
        else:
            del parent.value[row]
        parent.drop_children_cache()
        self.endRemoveRows()
        self.value_changed.emit()
        return True

    def add_child(self, parent_index: QModelIndex, key: Any, value: Any) -> bool:
        if self.read_only:
            return False
        parent = self.node_at(parent_index)
        if parent is None or not parent.is_container:
            return False
        if parent.kind == TYPE_OBJECT:
            if not isinstance(key, str) or key in parent.value:
                return False
            row = len(parent.value)
            self.beginInsertRows(parent_index, row, row)
            parent.value[key] = value
            parent._keys = None
        else:
            row = len(parent.value)
            self.beginInsertRows(parent_index, row, row)
            parent.value.append(value)
        parent.drop_children_cache()
        self.endInsertRows()
        self.value_changed.emit()
        return True

    # ------------------------------------------------------------------ 查询
    def search(self, query: str, *, case_sensitive: bool = False,
               match_values: bool = True, limit: int = SEARCH_LIMIT) -> List[Tuple]:
        """返回匹配节点的路径列表（广度优先，带上限）。"""
        if not query or not self._has_document:
            return []
        needle = query if case_sensitive else query.lower()
        found: List[Tuple] = []
        stack: List[JsonNode] = [self._root]
        visited = 0
        while stack and len(found) < limit:
            node = stack.pop()
            visited += 1
            if visited > 200000:
                break
            if node.parent is not None:
                haystack_key = node.key if isinstance(node.key, str) else ""
                if not case_sensitive:
                    haystack_key = haystack_key.lower()
                if needle in haystack_key:
                    found.append(node.path())
                elif match_values and not node.is_container:
                    haystack = value_preview(node.value, 4096)
                    if not case_sensitive:
                        haystack = haystack.lower()
                    if needle in haystack:
                        found.append(node.path())
            if node.is_container and len(found) < limit:
                count = min(node.child_count(), 5000)
                for row in range(count - 1, -1, -1):
                    stack.append(node.child_at(row))
        return found

    def subtree_text(self, index: QModelIndex) -> str:
        node = self.node_at(index)
        if node is None:
            return ""
        return dumps(node.value)

    def statistics_for(self, index: QModelIndex) -> Dict[str, Any]:
        """选中节点的属性信息，供属性面板展示。"""
        node = self.node_at(index)
        if node is None:
            return {}
        value = node.value
        kind = json_type(value)
        info: Dict[str, Any] = {
            "path": path_to_string(node.path()),
            "type": _TYPE_LABELS[kind],
            "children": node.child_count(),
            "serialized_bytes": (
                len(dumps(value).encode("utf-8", "ignore")) if node.child_count() <= 2000 else -1),
            "preview": value_preview(value, 400),
        }
        if kind == "string":
            info["length"] = len(value)
        if kind == TYPE_ARRAY:
            types = {json_type(item) for item in value[:200]}
            info["element_types"] = ", ".join(sorted(_TYPE_LABELS.get(t, t) for t in types)) or "-"
        if kind == TYPE_OBJECT:
            info["keys"] = ", ".join(list(value.keys())[:20]) or "-"
        offset = self.offset_for_path(node.path())
        if offset is not None:
            info["offset"] = offset.value_offset
            info["key_offset"] = offset.key_offset
        return info

"""i18n 完整性测试：源码里每个 tr("...") 的 key 都必须有中文翻译。

用 AST 扫描而不是正则，避免字符串拼接/转义带来的漏报。
"""

from __future__ import annotations

import ast
import os
import unittest
from typing import Set

from app import i18n
from app.core.jsonparser import ISSUE_MESSAGES

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def collect_tr_keys(root: str = PROJECT_ROOT) -> Set[str]:
    """扫描 app/ 与 run.py 里所有 tr("字面量") 调用，返回 key 集合。"""
    keys: Set[str] = set()
    targets = [os.path.join(root, "run.py")]
    for dirpath, dirnames, filenames in os.walk(os.path.join(root, "app")):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if name.endswith(".py"):
                targets.append(os.path.join(dirpath, name))
    for path in targets:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_tr = (isinstance(func, ast.Name) and func.id == "tr") or (
                isinstance(func, ast.Attribute) and func.attr == "tr")
            if not is_tr or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                keys.add(first.value)
    return keys


class TestCatalogCompleteness(unittest.TestCase):
    def test_every_tr_key_has_chinese_translation(self):
        keys = collect_tr_keys() | set(i18n.EXTRA_LABEL_KEYS)
        self.assertGreater(len(keys), 50, "扫描结果过少，提取器可能失效")
        missing = i18n.missing_translations(keys)
        self.assertEqual(missing, (), f"缺少中文翻译的界面文案: {missing}")

    def test_every_issue_code_has_chinese_translation(self):
        self.assertEqual(i18n.issue_codes_missing_zh(), ())
        for code in ISSUE_MESSAGES:
            title, message, _ = i18n.issue_text(
                i18n_issue(code))
            self.assertTrue(title, code)
            self.assertTrue(message, code)

    def test_issue_placeholders_match_english_templates(self):
        """中文模板里的占位符必须与英文模板一致，否则 format 会留下 {xxx}。"""
        import re

        placeholder = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*[^}]*\}")
        for code, (en_title, en_message, en_suggestion) in ISSUE_MESSAGES.items():
            zh = i18n._ISSUE_ZH.get(code)
            if zh is None:
                continue
            for en_part, zh_part in zip((en_message, en_suggestion), (zh[1], zh[2])):
                self.assertEqual(
                    sorted(placeholder.findall(en_part)),
                    sorted(placeholder.findall(zh_part)),
                    f"{code}: 中英文占位符不一致")

    def test_translation_roundtrip_language_switch(self):
        original = i18n.current_language()
        try:
            i18n.set_language(i18n.LANG_EN)
            self.assertEqual(i18n.tr("Format"), "Format")
            i18n.set_language(i18n.LANG_ZH)
            self.assertNotEqual(i18n.tr("Format"), "Format")
        finally:
            i18n.set_language(original)


def i18n_issue(code: str):
    from app.core.jsonparser import Issue, SEVERITY_ERROR

    return Issue(code=code, severity=SEVERITY_ERROR, offset=0, length=0, line=1, column=1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

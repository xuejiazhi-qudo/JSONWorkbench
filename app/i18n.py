"""多语言支持。

约定：**英文原文即 key**（``tr("Format")``），中文是翻译目标。默认显示中文，
缺失翻译时回退到英文原文，因此永远不会出现空白按钮。

解析器的问题码走 ``issue.<code>.title/message/suggestion`` 命名空间，
占位符必须与 :data:`app.core.jsonparser.ISSUE_MESSAGES` 保持一致。
"""

from __future__ import annotations

import weakref
from typing import Dict, Tuple

from app.core.jsonparser import ISSUE_MESSAGES, Issue, describe_issue

LANG_ZH = "zh"
LANG_EN = "en"
LANGUAGES = ((LANG_ZH, "简体中文"), (LANG_EN, "English"))

_current_language = LANG_ZH
_retranslatable: "weakref.WeakSet" = weakref.WeakSet()

__all__ = [
    "LANG_EN",
    "LANG_ZH",
    "LANGUAGES",
    "current_language",
    "issue_text",
    "issue_codes_missing_zh",
    "missing_translations",
    "register_retranslate",
    "retranslate_all",
    "set_language",
    "tr",
    "translation_keys",
    "warning_text",
]


# --------------------------------------------------------------------------- #
# 解析器问题码的中文翻译
# --------------------------------------------------------------------------- #
_ISSUE_ZH: Dict[str, Tuple[str, str, str]] = {
    "empty_input": ("空输入", "文档为空，没有可解析的 JSON 值。", "请先粘贴或打开一个 JSON 文档。"),
    "unexpected_eof": ("输入意外结束", "文档在需要{expected}的位置就结束了。", "文件可能被截断，请补全缺失内容。"),
    "unclosed_string": ("字符串未闭合", "字符串字面量缺少结尾的双引号。", "在行尾补上缺失的 \"。"),
    "invalid_escape": ("非法转义序列", "JSON 中不允许转义序列 {found}。",
                       "只能使用 \\\" \\\\ \\/ \\b \\f \\n \\r \\t \\uXXXX。"),
    "invalid_unicode_escape": ("非法 \\u 转义", "\\u 后面必须正好是 4 个十六进制字符。",
                               "请修正 \\u 转义，例如 \\u00e9。"),
    "lone_surrogate": ("孤立代理项", "发现未配对的 UTF-16 代理项，已替换为 U+FFFD。",
                       "BMP 之外的字符请使用代理对，例如 \\uD83D\\uDE00。"),
    "control_char_in_string": ("字符串内含控制字符",
                               "字符串中的裸控制字符 U+{char_code:04X} 必须转义。",
                               "请改用对应的转义序列。"),
    "single_quote_string": ("使用了单引号", "JSON 字符串必须使用双引号。", "请把单引号换成双引号。"),
    "unquoted_key": ("对象键未加引号", "对象的键必须是带双引号的字符串。", "给键加上双引号。"),
    "expected_key": ("缺少对象键", "此处需要一个带双引号的对象键。", "补上 \"key\":，或删掉多余的逗号。"),
    "missing_colon": ("缺少冒号", "对象键与值之间需要 ':'。", "在键后面插入 ':'。"),
    "missing_comma": ("缺少逗号", "两个 JSON 值之间需要 ','。", "在两个值之间插入 ','。"),
    "trailing_comma": ("多余的逗号", "标准 JSON 不允许结尾逗号。",
                       "删掉右括号前的逗号，或把文档类型切换为 JSONC。"),
    "bracket_mismatch": ("括号不匹配", "此处是 '{found}'，但需要 '{expected}'。",
                         "检查这一带括号的嵌套关系。"),
    "unclosed_container": ("{container}未闭合", "这个{container}始终没有闭合。", "补上缺失的右括号。"),
    "comment_not_allowed": ("不允许注释", "标准 JSON 不支持注释。",
                            "删除注释，或把文档类型切换为 JSONC。"),
    "unclosed_comment": ("块注释未闭合", "/* 注释缺少结尾的 */。", "补上 */ 关闭注释。"),
    "invalid_number": ("非法数字", "数字字面量格式错误（{reason}）。",
                       "JSON 数字形如 -12、1.5、1e10；不能有前导零或正号。"),
    "invalid_literal": ("非法字面量", "'{found}' 不是合法的 JSON 值。",
                        "请使用小写的 true / false / null，或把它写成字符串。"),
    "invalid_value": ("非法值", "在需要 JSON 值的位置出现了 '{found}'。",
                      "值只能是对象、数组、字符串、数字、true、false 或 null。"),
    "missing_value": ("缺少值", "此处需要一个 JSON 值，却找到了 '{found}'。",
                      "补上缺失的值，或删掉多余的键/逗号。"),
    "extra_data": ("顶层值之后有多余内容", "一个文档只允许有一个顶层 JSON 值。",
                   "删除多余内容，或把多个值包进一个数组。"),
    "depth_exceeded": ("嵌套层数过深", "嵌套层数超过上限 {limit} 层。",
                       "在设置里调高深度上限，或把文档结构扁平化。"),
    "duplicate_key": ("重复的键", "同一个对象里键 '{key}' 出现了多次。",
                      "解析时后者会覆盖前者，建议合并或重命名。"),
    "precision_loss": ("数字精度", "字面量 {raw} 无法用双精度浮点数精确表示。",
                       "输出时会保留原始字面量；转成 CSV/YAML 等格式时请注意精度。"),
    "offsets_disabled": ("节点偏移不可用", "文档超过 {limit} MB，因此未收集编辑器跳转偏移。",
                         "大文件建议用树形视图查看。"),
    "out_of_memory": ("内存不足", "文档太大，无法完整载入内存。",
                      "关闭其它文档，或把文件拆成更小的块处理。"),
    "cancelled": ("已取消", "解析已被取消。", ""),
}

#: 容器名的中文（用于 unclosed_container 的 {container} 占位符）
_CONTAINER_ZH = {"object": "对象", "array": "数组"}


def _format(template: str, details: Dict) -> str:
    if not template:
        return ""
    try:
        return template.format(**details)
    except (KeyError, IndexError, ValueError):
        return template


def issue_text(issue: Issue) -> Tuple[str, str, str]:
    """返回本地化的 (标题, 描述, 修复建议)。"""
    details = dict(issue.details)
    if issue.code == "unclosed_container" and "container" in details:
        details["container"] = _CONTAINER_ZH.get(details["container"], details["container"])
    entry = _ISSUE_ZH.get(issue.code)
    if entry is None or _current_language != LANG_ZH:
        return describe_issue(issue)
    title, message, suggestion = entry
    if issue.code == "invalid_literal" and details.get("suggestion"):
        suggestion = f"{suggestion}（{details['suggestion']}）"
    return title, _format(message, details), _format(suggestion, details)


def issue_codes_missing_zh() -> Tuple[str, ...]:
    """缺少中文翻译的问题码，供完整性测试使用。"""
    return tuple(sorted(set(ISSUE_MESSAGES) - set(_ISSUE_ZH)))


# --------------------------------------------------------------------------- #
# 界面文案
# --------------------------------------------------------------------------- #
from app.i18n_zh import EXTRA_LABEL_KEYS, ZH_UI, ZH_WARNINGS

_UI_ZH: Dict[str, str] = {}
_UI_ZH.update(ZH_UI)


def tr(text: str) -> str:
    """翻译界面文案；中文模式下缺失翻译时回退英文原文。"""
    if _current_language == LANG_ZH:
        return _UI_ZH.get(text, text)
    return text


def set_language(language: str) -> None:
    global _current_language
    target = LANG_EN if str(language).lower().startswith("en") else LANG_ZH
    if target == _current_language:
        return
    _current_language = target
    retranslate_all()


def register_retranslate(widget) -> None:
    """登记一个实现了 ``retranslate()`` 的部件，切语言时自动刷新文案。"""
    _retranslatable.add(widget)


def retranslate_all() -> None:
    for widget in list(_retranslatable):
        try:
            widget.retranslate()
        except RuntimeError:
            # 底层 C++ 对象已销毁，WeakSet 还没来得及清理
            continue
        except AttributeError:
            continue


def current_language() -> str:
    return _current_language


def translation_keys() -> Tuple[str, ...]:
    """当前已登记的中文翻译 key（含问题码命名空间），供测试比对。"""
    return tuple(sorted(_UI_ZH))


def missing_translations(keys) -> Tuple[str, ...]:
    """返回给定 key 中尚未翻译成中文的部分。"""
    return tuple(sorted(k for k in keys if k not in _UI_ZH))


def warning_text(code: str, detail: str = "") -> str:
    """引擎警告码的本地化文本；未知码原样返回。"""
    entry = ZH_WARNINGS.get(code)
    if entry is None or _current_language != LANG_ZH:
        return f"{code}: {detail}" if detail else code
    return f"{entry}（{detail}）" if detail else entry


def register_translations(mapping: Dict[str, str]) -> None:
    """批量登记中文翻译（供 catalog 模块调用）。"""
    _UI_ZH.update(mapping)

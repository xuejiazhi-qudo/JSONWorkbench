"""文件读写：编码识别、BOM 处理、大文件保护、原子保存。

离线优先：本模块只做本地文件 IO，绝不联网。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .jsonparser import MODE_JSON, MODE_JSONC

#: 超过该大小视为"大文件"，UI 需要提示并考虑关闭语法高亮
LARGE_FILE_BYTES = 10 * 1024 * 1024
#: 超过该大小直接拒绝一次性载入（避免把内存打爆）
MAX_LOAD_BYTES = 200 * 1024 * 1024

JSON_EXTENSIONS = (".json",)
JSONC_EXTENSIONS = (".jsonc", ".json5")
SUPPORTED_EXTENSIONS = JSON_EXTENSIONS + JSONC_EXTENSIONS + (".txt", ".log", ".geojson", ".har")

_BOMS: Tuple[Tuple[bytes, str, int], ...] = (
    (b"\xff\xfe\x00\x00", "utf-32-le", 4),
    (b"\x00\x00\xfe\xff", "utf-32-be", 4),
    (b"\xef\xbb\xbf", "utf-8-sig", 3),
    (b"\xff\xfe", "utf-16-le", 2),
    (b"\xfe\xff", "utf-16-be", 2),
)

__all__ = [
    "FileReadResult",
    "LARGE_FILE_BYTES",
    "MAX_LOAD_BYTES",
    "SUPPORTED_EXTENSIONS",
    "WriteResult",
    "detect_encoding",
    "detect_line_ending",
    "file_dialog_filter",
    "format_size",
    "guess_mode_from_path",
    "is_large_file",
    "read_text_file",
    "write_text_file",
]


@dataclass
class FileReadResult:
    ok: bool
    text: str = ""
    path: str = ""
    encoding: str = "utf-8"
    had_bom: bool = False
    size: int = 0
    line_ending: str = "\n"
    truncated: bool = False
    error: str = ""
    warnings: List[str] = field(default_factory=list)

    @property
    def is_large(self) -> bool:
        return self.size >= LARGE_FILE_BYTES


@dataclass
class WriteResult:
    ok: bool
    path: str = ""
    bytes_written: int = 0
    encoding: str = "utf-8"
    error: str = ""


def detect_encoding(data: bytes) -> Tuple[str, bool, str]:
    """识别编码，返回 (编码名, 是否带 BOM, 说明)。

    顺序：BOM -> 严格 UTF-8 -> GB18030（覆盖 GBK/GB2312）-> latin-1 兜底。
    """
    for bom, encoding, length in _BOMS:
        if data.startswith(bom):
            return encoding, True, f"BOM detected ({encoding})"
    if not data:
        return "utf-8", False, ""
    try:
        data.decode("utf-8")
        return "utf-8", False, ""
    except UnicodeDecodeError:
        pass
    # 无 BOM 的 UTF-16 启发式：ASCII 文本在 UTF-16 里会出现大量 0x00
    if len(data) >= 2:
        zeros_even = sum(1 for i in range(0, min(len(data), 512), 2) if data[i] == 0)
        zeros_odd = sum(1 for i in range(1, min(len(data), 512), 2) if data[i] == 0)
        sample = min(len(data), 512) // 2
        if sample and zeros_even > sample * 0.6:
            return "utf-16-be", False, "Heuristic: looks like UTF-16 BE without BOM"
        if sample and zeros_odd > sample * 0.6:
            return "utf-16-le", False, "Heuristic: looks like UTF-16 LE without BOM"
    try:
        data.decode("gb18030")
        return "gb18030", False, "Not valid UTF-8; decoded as GB18030 (GBK compatible)"
    except UnicodeDecodeError:
        pass
    return "latin-1", False, "Unknown encoding; decoded as latin-1 (bytes preserved)"


def detect_line_ending(text: str) -> str:
    """判断主导换行符。"""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def guess_mode_from_path(path: str) -> str:
    """按扩展名区分标准 JSON 与 JSONC（绝不把注释误判成标准 JSON）。"""
    ext = os.path.splitext(path or "")[1].lower()
    return MODE_JSONC if ext in JSONC_EXTENSIONS else MODE_JSON


def is_large_file(path: str) -> bool:
    try:
        return os.path.getsize(path) >= LARGE_FILE_BYTES
    except OSError:
        return False


def format_size(size: float) -> str:
    """字节数转人类可读文本。"""
    size = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"  # pragma: no cover


def read_text_file(path: str, *, max_bytes: Optional[int] = None) -> FileReadResult:
    """读取文本文件。失败时返回带 error 的结果，不抛异常。"""
    limit = max_bytes if max_bytes is not None else MAX_LOAD_BYTES
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return FileReadResult(ok=False, path=path, error=f"Cannot stat file: {exc}")
    if size > limit:
        return FileReadResult(
            ok=False, path=path, size=size,
            error=f"File is {format_size(size)}, larger than the {format_size(limit)} load limit")
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except PermissionError:
        return FileReadResult(ok=False, path=path, size=size,
                              error="Permission denied: the file is locked or read-only")
    except OSError as exc:
        return FileReadResult(ok=False, path=path, size=size, error=f"Cannot read file: {exc}")

    encoding, had_bom, note = detect_encoding(data)
    warnings: List[str] = [note] if note else []
    try:
        text = data.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        text = data.decode("latin-1")
        encoding = "latin-1"
        warnings.append("Decoding failed; fell back to latin-1 so every byte stays visible")
    if had_bom and text and text[0] == "\ufeff":
        text = text[1:]
    elif encoding == "utf-8-sig":
        encoding = "utf-8"
    if text.count("\ufffd"):
        warnings.append("Some characters could not be decoded and were replaced by U+FFFD")
    return FileReadResult(
        ok=True,
        text=text,
        path=os.path.abspath(path),
        encoding=encoding,
        had_bom=had_bom,
        size=size,
        line_ending=detect_line_ending(text),
        warnings=warnings,
    )


def write_text_file(path: str, text: str, *, encoding: str = "utf-8",
                    with_bom: bool = False, line_ending: Optional[str] = None,
                    atomic: bool = True) -> WriteResult:
    """写文件。默认原子写入（同目录临时文件 + replace），避免写一半损坏原文件。"""
    body = text
    if line_ending == "\r\n":
        body = body.replace("\r\n", "\n").replace("\n", "\r\n")
    elif line_ending == "\n":
        body = body.replace("\r\n", "\n")
    codec = "utf-8-sig" if (with_bom and encoding == "utf-8") else encoding
    try:
        data = body.encode(codec)
    except (UnicodeEncodeError, LookupError) as exc:
        return WriteResult(ok=False, path=path, encoding=codec,
                           error=f"Cannot encode text as {codec}: {exc}")
    directory = os.path.dirname(os.path.abspath(path)) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        if not atomic:
            with open(path, "wb") as handle:
                handle.write(data)
            return WriteResult(ok=True, path=os.path.abspath(path),
                               bytes_written=len(data), encoding=codec)
        handle = tempfile.NamedTemporaryFile(prefix=".jsonwb-", suffix=".tmp",
                                             dir=directory, delete=False)
        temp_path = handle.name
        try:
            with handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        except BaseException:
            _silent_remove(temp_path)
            raise
    except PermissionError:
        return WriteResult(ok=False, path=path, encoding=codec,
                           error="Permission denied: the file is locked or read-only")
    except OSError as exc:
        return WriteResult(ok=False, path=path, encoding=codec, error=f"Cannot write file: {exc}")
    return WriteResult(ok=True, path=os.path.abspath(path), bytes_written=len(data),
                       encoding=codec)


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def file_dialog_filter(mode: str = MODE_JSON) -> str:
    """文件对话框过滤器（英文，UI 层负责翻译标签）。"""
    json_pattern = " ".join(f"*{ext}" for ext in JSON_EXTENSIONS)
    jsonc_pattern = " ".join(f"*{ext}" for ext in JSONC_EXTENSIONS)
    all_pattern = " ".join(f"*{ext}" for ext in SUPPORTED_EXTENSIONS)
    parts = [f"JSON ({json_pattern})", f"JSONC ({jsonc_pattern})", f"All supported ({all_pattern})"]
    if mode == MODE_JSONC:
        parts = [parts[1], parts[0], parts[2]]
    parts.append("All files (*)")
    return ";;".join(parts)

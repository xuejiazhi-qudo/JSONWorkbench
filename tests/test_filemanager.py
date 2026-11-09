"""文件管理测试：编码识别、BOM、换行、大文件保护、原子写入与错误处理。"""

from __future__ import annotations

import os
import tempfile
import unittest

from app.core import jsonfilemanager as fm
from app.core.jsonfilemanager import (
    detect_encoding,
    detect_line_ending,
    file_dialog_filter,
    format_size,
    guess_mode_from_path,
    read_text_file,
    write_text_file,
)
from app.core.jsonparser import MODE_JSON, MODE_JSONC


class TestEncodingDetection(unittest.TestCase):
    def test_utf8_bom(self):
        self.assertEqual(detect_encoding(b"\xef\xbb\xbf{}"), ("utf-8-sig", True, "BOM detected (utf-8-sig)"))

    def test_utf16_boms(self):
        self.assertEqual(detect_encoding(b"\xff\xfe{\x00"), ("utf-16-le", True, "BOM detected (utf-16-le)"))
        self.assertEqual(detect_encoding(b"\xfe\xff\x00{"), ("utf-16-be", True, "BOM detected (utf-16-be)"))

    def test_utf32_boms_take_priority_over_utf16(self):
        encoding, bom, _ = detect_encoding(b"\xff\xfe\x00\x00{\x00\x00\x00")
        self.assertEqual((encoding, bom), ("utf-32-le", True))

    def test_plain_utf8(self):
        encoding, bom, note = detect_encoding('{"中":"文"}'.encode("utf-8"))
        self.assertEqual((encoding, bom, note), ("utf-8", False, ""))

    def test_gbk_falls_back_to_gb18030(self):
        encoding, bom, note = detect_encoding('{"名称":"值"}'.encode("gbk"))
        self.assertEqual(encoding, "gb18030")
        self.assertFalse(bom)
        self.assertIn("GB18030", note)

    def test_undecodable_bytes_fall_back_to_latin1(self):
        encoding, _, note = detect_encoding(b"\xff\xfe\xfd not json")
        self.assertIn(encoding, ("latin-1", "utf-16-le"))

    def test_empty_data(self):
        self.assertEqual(detect_encoding(b"")[0], "utf-8")


class TestReadFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_read_utf8_json(self):
        path = self.write("a.json", '{"中文":"值"}'.encode("utf-8"))
        result = read_text_file(path)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.text, '{"中文":"值"}')
        self.assertEqual(result.encoding, "utf-8")
        self.assertFalse(result.had_bom)
        self.assertEqual(result.size, os.path.getsize(path))
        self.assertEqual(result.path, os.path.abspath(path))

    def test_bom_is_stripped_from_text(self):
        path = self.write("b.json", b'\xef\xbb\xbf{"a":1}')
        result = read_text_file(path)
        self.assertTrue(result.ok)
        self.assertTrue(result.had_bom)
        self.assertEqual(result.text, '{"a":1}')
        self.assertFalse(result.text.startswith("\ufeff"))

    def test_utf16_file(self):
        path = self.write("c.json", '{"a":"中"}'.encode("utf-16"))
        result = read_text_file(path)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.text, '{"a":"中"}')

    def test_gbk_file(self):
        path = self.write("d.json", '{"名称":"值"}'.encode("gbk"))
        result = read_text_file(path)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, '{"名称":"值"}')
        self.assertTrue(result.warnings)

    def test_crlf_detection(self):
        path = self.write("e.json", b'{\r\n"a":1\r\n}')
        result = read_text_file(path)
        self.assertEqual(result.line_ending, "\r\n")

    def test_lf_detection(self):
        path = self.write("f.json", b'{\n"a":1\n}')
        self.assertEqual(read_text_file(path).line_ending, "\n")

    def test_empty_file(self):
        path = self.write("g.json", b"")
        result = read_text_file(path)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "")
        self.assertEqual(result.size, 0)

    def test_missing_file(self):
        result = read_text_file(os.path.join(self.dir, "nope.json"))
        self.assertFalse(result.ok)
        self.assertIn("Cannot stat file", result.error)

    def test_directory_is_not_readable(self):
        result = read_text_file(self.dir)
        self.assertFalse(result.ok)
        self.assertTrue(result.error)

    def test_size_limit_is_enforced(self):
        path = self.write("big.json", b"{" + b" " * 200 + b"}")
        result = read_text_file(path, max_bytes=100)
        self.assertFalse(result.ok)
        self.assertIn("load limit", result.error)

    def test_large_file_flag(self):
        original = fm.LARGE_FILE_BYTES
        fm.LARGE_FILE_BYTES = 10
        try:
            path = self.write("h.json", b"0123456789abcdef")
            self.assertTrue(fm.is_large_file(path))
            self.assertTrue(read_text_file(path).is_large)
        finally:
            fm.LARGE_FILE_BYTES = original

    def test_invalid_bytes_produce_replacement_warning(self):
        path = self.write("i.json", b'{"a":"\xc3\x28"}')
        result = read_text_file(path)
        self.assertTrue(result.ok)
        self.assertTrue(result.text)


class TestWriteFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_roundtrip_utf8(self):
        path = os.path.join(self.dir, "out.json")
        result = write_text_file(path, '{"中":"文"}')
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.bytes_written, len('{"中":"文"}'.encode("utf-8")))
        with open(path, "rb") as handle:
            self.assertEqual(handle.read().decode("utf-8"), '{"中":"文"}')

    def test_bom_written_when_requested(self):
        path = os.path.join(self.dir, "bom.json")
        write_text_file(path, "{}", with_bom=True)
        with open(path, "rb") as handle:
            self.assertTrue(handle.read().startswith(b"\xef\xbb\xbf"))

    def test_line_ending_conversion(self):
        path = os.path.join(self.dir, "crlf.json")
        write_text_file(path, "{\n\"a\":1\n}", line_ending="\r\n")
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b'{\r\n"a":1\r\n}')

    def test_lf_normalizes_crlf(self):
        path = os.path.join(self.dir, "lf.json")
        write_text_file(path, "{\r\n\"a\":1\r\n}", line_ending="\n")
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b'{\n"a":1\n}')

    def test_atomic_write_leaves_no_temp_files(self):
        path = os.path.join(self.dir, "atomic.json")
        write_text_file(path, "{}")
        leftovers = [n for n in os.listdir(self.dir) if n != "atomic.json"]
        self.assertEqual(leftovers, [])

    def test_creates_missing_directories(self):
        path = os.path.join(self.dir, "sub", "deep", "out.json")
        result = write_text_file(path, "{}")
        self.assertTrue(result.ok, result.error)
        self.assertTrue(os.path.exists(path))

    def test_gbk_encoding(self):
        path = os.path.join(self.dir, "gbk.json")
        result = write_text_file(path, '{"名称":"值"}', encoding="gbk")
        self.assertTrue(result.ok, result.error)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read().decode("gbk"), '{"名称":"值"}')

    def test_unencodable_character_reports_error(self):
        path = os.path.join(self.dir, "bad.json")
        result = write_text_file(path, '{"emoji":"😀"}', encoding="gbk")
        self.assertFalse(result.ok)
        self.assertIn("Cannot encode", result.error)
        self.assertFalse(os.path.exists(path))

    def test_write_to_invalid_path_reports_error(self):
        blocker = os.path.join(self.dir, "file.json")
        write_text_file(blocker, "{}")
        result = write_text_file(os.path.join(blocker, "nested.json"), "{}")
        self.assertFalse(result.ok)
        self.assertTrue(result.error)

    def test_overwrite_existing_file(self):
        path = os.path.join(self.dir, "over.json")
        write_text_file(path, '{"old":true}')
        write_text_file(path, '{"new":true}')
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), '{"new":true}')

    def test_non_atomic_write(self):
        path = os.path.join(self.dir, "plain.json")
        result = write_text_file(path, "{}", atomic=False)
        self.assertTrue(result.ok)
        self.assertEqual(os.path.getsize(path), 2)


class TestHelpers(unittest.TestCase):
    def test_format_size(self):
        self.assertEqual(format_size(0), "0 B")
        self.assertEqual(format_size(1023), "1023 B")
        self.assertEqual(format_size(1024), "1.0 KB")
        self.assertEqual(format_size(1536), "1.5 KB")
        self.assertEqual(format_size(1024 * 1024), "1.0 MB")
        self.assertEqual(format_size(3.5 * 1024 * 1024 * 1024), "3.5 GB")

    def test_detect_line_ending(self):
        self.assertEqual(detect_line_ending("a\r\nb\r\nc"), "\r\n")
        self.assertEqual(detect_line_ending("a\nb\nc"), "\n")
        self.assertEqual(detect_line_ending("abc"), "\n")
        self.assertEqual(detect_line_ending("a\r\nb\nc\nd"), "\n")

    def test_guess_mode_from_path(self):
        self.assertEqual(guess_mode_from_path("a.json"), MODE_JSON)
        self.assertEqual(guess_mode_from_path("a.JSON"), MODE_JSON)
        self.assertEqual(guess_mode_from_path("a.jsonc"), MODE_JSONC)
        self.assertEqual(guess_mode_from_path("a.json5"), MODE_JSONC)
        self.assertEqual(guess_mode_from_path(""), MODE_JSON)

    def test_file_dialog_filter(self):
        text = file_dialog_filter()
        self.assertIn("*.json", text)
        self.assertIn("*.jsonc", text)
        self.assertTrue(text.startswith("JSON ("))
        self.assertTrue(file_dialog_filter(MODE_JSONC).startswith("JSONC ("))
        self.assertTrue(text.endswith("All files (*)"))

    def test_supported_extensions_include_json(self):
        self.assertIn(".json", fm.SUPPORTED_EXTENSIONS)
        self.assertIn(".jsonc", fm.SUPPORTED_EXTENSIONS)


if __name__ == "__main__":
    unittest.main(verbosity=2)

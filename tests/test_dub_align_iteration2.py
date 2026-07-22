"""第二轮迭代测试：xlsx 文案读取 / 设置与组件根 / 字幕位置 / 音色包 / 目录浏览与解析端点。"""

import io
import json
import shutil
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
import wave
import zipfile
from pathlib import Path

from dub_align_studio import settings as studio_settings
from dub_align_studio import voice_library, xlsx_reader
from dub_align_studio.subtitles import SUBTITLE_POSITIONS, SubtitleEntry, SubtitleStyle, drawtext_filters


def _make_xlsx(cells: dict[str, str]) -> bytes:
    """构造最小合法 xlsx：cells 形如 {"B1": "第一句", "B2": "第二句", "C1": "忽略"}。"""
    shared: list[str] = []
    refs = []
    for ref, text in cells.items():
        if text not in shared:
            shared.append(text)
        refs.append((ref, shared.index(text)))
    rows: dict[int, list[tuple[str, int]]] = {}
    for ref, idx in refs:
        row = int("".join(c for c in ref if c.isdigit()))
        rows.setdefault(row, []).append((ref, idx))
    sheet_cells = ""
    for row in sorted(rows):
        cell_xml = "".join(f'<c r="{ref}" t="s"><v>{idx}</v></c>' for ref, idx in rows[row])
        sheet_cells += f'<row r="{row}">{cell_xml}</row>'
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("xl/sharedStrings.xml",
                        f'<?xml version="1.0"?><sst {ns}>' +
                        "".join(f"<si><t>{t}</t></si>" for t in shared) + "</sst>")
        bundle.writestr("xl/worksheets/sheet1.xml",
                        f'<?xml version="1.0"?><worksheet {ns}><sheetData>{sheet_cells}</sheetData></worksheet>')
    return buffer.getvalue()


class XlsxReaderTests(unittest.TestCase):
    def test_reads_default_column_b_in_row_order(self):
        data = _make_xlsx({"B2": "第二句", "B1": "第一句", "A1": "序号", "C1": "备注"})
        self.assertEqual(xlsx_reader.read_column(data), ["第一句", "第二句"])

    def test_reads_custom_column(self):
        data = _make_xlsx({"C1": "丙一", "C3": "丙三", "B1": "乙"})
        self.assertEqual(xlsx_reader.read_column(data, "c"), ["丙一", "丙三"])

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            xlsx_reader.read_column(b"not a zip")
        with self.assertRaises(ValueError):
            xlsx_reader.read_column(_make_xlsx({"B1": "x"}), "1B")


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self._backup = studio_settings.SETTINGS_FILE
        self.temp = Path(tempfile.mkdtemp(prefix="settings_"))
        studio_settings.SETTINGS_FILE = self.temp / "settings.json"

    def tearDown(self):
        studio_settings.SETTINGS_FILE = self._backup
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_component_root_defaults_then_custom(self):
        self.assertEqual(studio_settings.component_root(), studio_settings.default_component_root())
        custom = self.temp / "D盘组件"
        studio_settings.set_component_root(custom)
        self.assertEqual(studio_settings.component_root(), custom)
        self.assertTrue(custom.exists())
        self.assertEqual(studio_settings.whisper_models_dir(), custom / "whisper.cpp" / "models")


class SubtitlePositionTests(unittest.TestCase):
    def test_five_positions_available(self):
        self.assertEqual(list(SUBTITLE_POSITIONS), ["顶部", "中上", "中部", "中下", "底部"])

    def test_position_lands_in_filter(self):
        workdir = Path(tempfile.mkdtemp(prefix="subpos_"))
        try:
            font = workdir / "f.ttf"
            font.write_bytes(b"\x00")
            entries = [SubtitleEntry(1, 0.0, 6.0, "台词")]
            top = drawtext_filters(entries, SubtitleStyle(position="顶部"), font, workdir)[0]
            self.assertIn("y=(h-text_h)*0.060", top)
            bottom = drawtext_filters(entries, SubtitleStyle(position="底部"), font, workdir)[0]
            self.assertIn("y=h-text_h-180", bottom)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


class VoicePackTests(unittest.TestCase):
    def test_export_then_import_roundtrip(self):
        root_a = Path(tempfile.mkdtemp(prefix="pack_a_"))
        root_b = Path(tempfile.mkdtemp(prefix="pack_b_"))
        try:
            sample = root_a / "s.wav"
            with wave.open(str(sample), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(8000)
                handle.writeframes(b"\x00\x00" * 800)
            voice_library.register_voice(root_a, "晚棠", sample, transcript="参考句")
            payload = voice_library.export_voices_zip(root_a)
            imported = voice_library.import_voices_zip(root_b, payload)
            self.assertEqual(len(imported), 1)
            entry = voice_library.get_voice(root_b, imported[0])
            self.assertEqual(entry.name, "晚棠")
            self.assertEqual(entry.transcript, "参考句")
        finally:
            shutil.rmtree(root_a, ignore_errors=True)
            shutil.rmtree(root_b, ignore_errors=True)

    def test_import_bad_zip_raises(self):
        root = Path(tempfile.mkdtemp(prefix="pack_bad_"))
        try:
            with self.assertRaises(Exception):
                voice_library.import_voices_zip(root, b"not a zip")
        finally:
            shutil.rmtree(root, ignore_errors=True)


class NewEndpointTests(unittest.TestCase):
    port = 8772

    @classmethod
    def setUpClass(cls):
        from dub_align_studio import web_server

        cls.ws = web_server
        cls._home_backup = web_server.STUDIO_HOME
        cls._temp_home = Path(tempfile.mkdtemp(prefix="ep_home_"))
        web_server.STUDIO_HOME = cls._temp_home
        cls.server = web_server.serve(port=cls.port, open_browser=False)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.ws.STUDIO_HOME = cls._home_backup
        shutil.rmtree(cls._temp_home, ignore_errors=True)

    def test_browse_lists_directories(self):
        temp = Path(tempfile.mkdtemp(prefix="browse_"))
        try:
            (temp / "子目录甲").mkdir()
            (temp / "子目录乙").mkdir()
            (temp / "文件.txt").write_text("x")
            data = json.loads(urllib.request.urlopen(
                self.base + "/api/browse?path=" + urllib.parse.quote(str(temp))).read())
            self.assertEqual(set(data["dirs"]), {"子目录甲", "子目录乙"})
            self.assertEqual(data["path"], str(temp))
        finally:
            shutil.rmtree(temp, ignore_errors=True)

    def test_script_parse_xlsx_and_txt(self):
        data = _make_xlsx({"B1": "一", "B2": "二"})
        req = urllib.request.Request(self.base + "/api/script/parse?kind=xlsx&column=B",
                                     data=data, method="POST")
        parsed = json.loads(urllib.request.urlopen(req).read())
        self.assertEqual(parsed["lines"], ["一", "二"])
        req = urllib.request.Request(self.base + "/api/script/parse?kind=txt",
                                     data="甲\n\n乙\n".encode("utf-8"), method="POST")
        parsed = json.loads(urllib.request.urlopen(req).read())
        self.assertEqual(parsed["lines"], ["甲", "乙"])

    def test_settings_endpoint(self):
        data = json.loads(urllib.request.urlopen(self.base + "/api/settings").read())
        self.assertIn("component_root", data)

    def test_voice_export_import_endpoints(self):
        import io as _io

        buf = _io.BytesIO()
        with wave.open(buf, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 400)
        q = urllib.parse.urlencode({"name": "包测试", "filename": "a.wav"})
        urllib.request.urlopen(urllib.request.Request(
            self.base + "/api/voices?" + q, data=buf.getvalue(), method="POST"))
        exported = urllib.request.urlopen(self.base + "/api/voices/export").read()
        self.assertGreater(len(exported), 100)
        imported = json.loads(urllib.request.urlopen(urllib.request.Request(
            self.base + "/api/voices/import", data=exported, method="POST")).read())
        self.assertEqual(len(imported["imported"]), 1)


if __name__ == "__main__":
    unittest.main()

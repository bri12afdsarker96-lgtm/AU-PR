"""第三轮迭代测试：总目录/字体库/字幕字体/克隆存档/镜像源/环境自检端点。"""

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

from dub_align_studio import fonts, settings as studio_settings
from dub_align_studio.components import _gh_mirror_urls
from dub_align_studio.subtitles import SubtitleStyle, pick_font


def _fake_ttf() -> bytes:
    return b"\x00\x01\x00\x00" + b"F" * 64


class TempRootMixin:
    def setUp(self):
        self._backup = studio_settings.SETTINGS_FILE
        self.temp = Path(tempfile.mkdtemp(prefix="it3_"))
        studio_settings.SETTINGS_FILE = self.temp / "settings.json"
        studio_settings.set_data_root(self.temp / "总目录")

    def tearDown(self):
        studio_settings.SETTINGS_FILE = self._backup
        shutil.rmtree(self.temp, ignore_errors=True)


class FontLibraryTests(TempRootMixin, unittest.TestCase):
    def test_pack_catalog_shape(self):
        keys = [f["key"] for f in fonts.FONT_PACK]
        self.assertGreaterEqual(len(keys), 12)  # 十几种自媒体常用字体
        self.assertEqual(len(keys), len(set(keys)))
        for item in fonts.FONT_PACK:
            self.assertTrue(item["name"] and item["url"] and item["file"], item["key"])

    def test_upload_ttf_then_list_and_resolve(self):
        saved = fonts.save_uploaded_font("我的字体.ttf", _fake_ttf())
        self.assertEqual(saved, ["我的字体.ttf"])
        listed = fonts.list_fonts()
        self.assertEqual(len(listed), 1)
        self.assertEqual(fonts.resolve_font("我的字体").name, "我的字体.ttf")
        self.assertEqual(fonts.resolve_font("我的字体.ttf").name, "我的字体.ttf")
        self.assertIsNone(fonts.resolve_font("不存在"))
        self.assertIsNone(fonts.resolve_font(fonts.DEFAULT_FONT_LABEL))

    def test_upload_zip_extracts_fonts(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as bundle:
            bundle.writestr("fonts/甲.ttf", _fake_ttf())
            bundle.writestr("fonts/说明.txt", "x")
            bundle.writestr("乙.otf", _fake_ttf())
        saved = fonts.save_uploaded_font("字体包.zip", buf.getvalue())
        self.assertEqual(sorted(saved), ["乙.otf", "甲.ttf"])
        with self.assertRaises(ValueError):
            fonts.save_uploaded_font("坏.zip", zipfile_bytes_without_fonts())

    def test_reject_unknown_format(self):
        with self.assertRaises(ValueError):
            fonts.save_uploaded_font("x.exe", b"MZ")

    def test_pick_font_prefers_library_then_falls_back(self):
        fonts.save_uploaded_font("库字体.ttf", _fake_ttf())
        self.assertEqual(pick_font("库字体").name, "库字体.ttf")
        # 未指定/找不到 → 系统字体兜底（本容器有文泉驿）
        fallback = pick_font("")
        self.assertTrue(fallback is None or fallback.exists())

    def test_mirror_urls_wrap_github(self):
        urls = fonts.mirror_urls("https://github.com/a/b/releases/x.ttf")
        self.assertEqual(len(urls), 7)  # 2026-07 镜像池扩容：6 家镜像 + 直连
        self.assertTrue(urls[0].startswith("https://ghproxy.net/https://github.com/"))
        self.assertEqual(urls[-1], "https://github.com/a/b/releases/x.ttf")
        self.assertEqual(fonts.mirror_urls("https://example.com/f.ttf"), ["https://example.com/f.ttf"])
        # 仓库内文件（/raw/ 形态）额外生成 jsDelivr CDN 首选
        raw = fonts.mirror_urls("https://github.com/google/fonts/raw/main/ofl/x/Y.ttf")
        self.assertEqual(raw[0], "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/x/Y.ttf")
        self.assertEqual(len(raw), 8)


def zipfile_bytes_without_fonts() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as bundle:
        bundle.writestr("说明.txt", "x")
    return buf.getvalue()


class ComponentMirrorTests(unittest.TestCase):
    def test_component_mirror_urls(self):
        urls = _gh_mirror_urls(["https://github.com/ggml-org/whisper.cpp/releases/a.zip",
                                "https://huggingface.co/x/y.bin"])
        self.assertEqual(len(urls), 8)  # github×7（镜像6 + 直连1）+ hf×1
        self.assertIn("https://huggingface.co/x/y.bin", urls)


class CloneArchiveTests(TempRootMixin, unittest.TestCase):
    def test_step_dub_archives_master_copy(self):
        from dub_align_studio import studio_pipeline as pipeline

        out = self.temp / "输出"
        master = pipeline.step_dub("第一句台词。", "mock", out)
        self.assertTrue(master.path.exists())
        clones = list(studio_settings.clones_dir().glob("*_mock_默认声线.wav"))
        self.assertEqual(len(clones), 1)
        self.assertEqual(clones[0].stat().st_size, master.path.stat().st_size)
        self.assertTrue(clones[0].with_suffix(".json").exists())


class SubtitleFontStyleTests(TempRootMixin, unittest.TestCase):
    def test_subtitle_style_carries_font(self):
        style = SubtitleStyle(font_name="库字体")
        self.assertEqual(style.font_name, "库字体")


class EnvcheckEndpointTests(unittest.TestCase):
    port = 8774

    @classmethod
    def setUpClass(cls):
        from dub_align_studio import web_server

        cls.ws = web_server
        cls._settings_backup = studio_settings.SETTINGS_FILE
        cls.temp = Path(tempfile.mkdtemp(prefix="env_"))
        studio_settings.SETTINGS_FILE = cls.temp / "settings.json"
        studio_settings.set_data_root(cls.temp / "总目录")
        cls.server = web_server.serve(port=cls.port, open_browser=False)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        studio_settings.SETTINGS_FILE = cls._settings_backup
        shutil.rmtree(cls.temp, ignore_errors=True)

    def _run_action(self, payload: dict) -> dict:
        import time

        req = urllib.request.Request(self.base + "/api/run", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req)
        end = time.time() + 60
        while time.time() < end:
            job = json.loads(urllib.request.urlopen(self.base + "/api/job").read())
            if job["done"]:
                return job
            time.sleep(0.2)
        raise AssertionError("超时")

    def test_envcheck_reports_layout_and_skips_existing(self):
        job = self._run_action({"action": "envcheck"})
        self.assertTrue(job["ok"], job["log"])
        text = "\n".join(job["log"])
        self.assertIn("总目录", text)
        self.assertIn("不会重复下载", text)
        self.assertIn("字体库", text)

    def test_fonts_endpoints(self):
        data = json.loads(urllib.request.urlopen(self.base + "/api/fonts").read())
        self.assertGreaterEqual(len(data["pack"]), 12)
        q = urllib.parse.urlencode({"filename": "上传.ttf"})
        req = urllib.request.Request(self.base + "/api/fonts?" + q, data=_fake_ttf(), method="POST")
        saved = json.loads(urllib.request.urlopen(req).read())
        self.assertEqual(saved["saved"], ["上传.ttf"])
        body = urllib.request.urlopen(self.base + "/fonts/" + urllib.parse.quote("上传.ttf")).read()
        self.assertEqual(body, _fake_ttf())

    def test_settings_returns_data_root(self):
        data = json.loads(urllib.request.urlopen(self.base + "/api/settings").read())
        self.assertIn("data_root", data)
        self.assertIn("总目录", data["data_root"])


if __name__ == "__main__":
    unittest.main()

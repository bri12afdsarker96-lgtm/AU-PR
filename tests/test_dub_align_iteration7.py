"""第七轮迭代测试：音色可选引擎+可调参数+合成试听 / 字体分类扩充 / 字体库独立入口。"""

import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
import wave
from pathlib import Path

from dub_align_studio import fonts, settings as studio_settings, voice_library, web_server


def _wav(path: Path) -> None:
    with wave.open(str(path), "wb") as h:
        h.setnchannels(1)
        h.setsampwidth(2)
        h.setframerate(8000)
        h.writeframes(b"\x01\x02" * 800)


class VoiceDesignTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="vdesign_"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_register_stores_engine_and_params(self):
        ref = self.root / "r.wav"
        _wav(ref)
        entry = voice_library.register_voice(
            self.root, "晚棠", ref, transcript="你好", engine="fish_local",
            params={"num_steps": 20, "guidance_scale": 1.8, "speed": 1.1, "seed": 7})
        self.assertEqual(entry.engine, "fish_local")
        self.assertEqual(entry.params["num_steps"], 20)
        self.assertEqual(entry.params["guidance_scale"], 1.8)
        got = voice_library.get_voice(self.root, entry.voice_id)
        self.assertEqual(got.engine, "fish_local")
        self.assertEqual(got.params["seed"], 7)
        # 元数据落盘含 engine/params
        meta = json.loads((voice_library.voices_root(self.root) / entry.voice_id / "音色.json").read_text("utf-8"))
        self.assertEqual(meta["engine"], "fish_local")
        self.assertEqual(meta["params"]["num_steps"], 20)

    def test_legacy_voice_without_params_defaults(self):
        # 模拟旧版音色目录（音色.json 无 engine/params）
        vdir = voice_library.voices_root(self.root) / "旧音色"
        vdir.mkdir(parents=True)
        _wav(vdir / "参考音频.wav")
        (vdir / "音色.json").write_text(json.dumps({"name": "旧"}, ensure_ascii=False), encoding="utf-8")
        got = voice_library.get_voice(self.root, "旧音色")
        self.assertEqual(got.engine, voice_library.DEFAULT_ENGINE)
        self.assertEqual(got.params, voice_library.DEFAULT_PARAMS)

    def test_merge_params_fills_and_coerces(self):
        p = voice_library.merge_params({"num_steps": "16", "guidance_scale": "2.0", "unknown": 9})
        self.assertEqual(p["num_steps"], 16)
        self.assertEqual(p["guidance_scale"], 2.0)
        self.assertEqual(p["speed"], voice_library.DEFAULT_PARAMS["speed"])  # 缺项补默认
        self.assertNotIn("unknown", p)  # 未知键剔除

    def test_export_import_preserves_engine_params(self):
        ref = self.root / "r.wav"
        _wav(ref)
        voice_library.register_voice(self.root, "青梧", ref, engine="fish_local",
                                     params={"num_steps": 24})
        blob = voice_library.export_voices_zip(self.root)
        dest = Path(tempfile.mkdtemp(prefix="vimp_"))
        try:
            ids = voice_library.import_voices_zip(dest, blob)
            got = voice_library.get_voice(dest, ids[0])
            self.assertEqual(got.engine, "fish_local")
            self.assertEqual(got.params["num_steps"], 24)
        finally:
            shutil.rmtree(dest, ignore_errors=True)


class FontExpansionTests(unittest.TestCase):
    def test_new_verified_fonts_present(self):
        keys = {f["key"] for f in fonts.FONT_PACK}
        for k in ("lxgw_wenkai_light", "huninn", "cactus_serif"):
            self.assertIn(k, keys)

    def test_all_fonts_have_category(self):
        for f in fonts.FONT_PACK:
            self.assertIn(f["category"], {"黑体标题", "楷宋文艺", "圆体可爱", "手写书法"}, f["key"])
        self.assertGreaterEqual(len(fonts.FONT_PACK), 16)

    def test_no_dead_single_repo_paths(self):
        dead = ["googlefonts/zcoolxiaowei", "googlefonts/ma-shan-zheng", "googlefonts/long-cang",
                "googlefonts/zhi-mang-xing", "googlefonts/liu-jian-mao-cao",
                "googlefonts/zcool-qingke-huangyou"]
        for f in fonts.FONT_PACK:
            for marker in dead:
                self.assertNotIn(marker, f["url"], f["key"])

    def test_statuses_carry_category(self):
        for s in fonts.font_statuses():
            self.assertIn("category", s)


class VoiceEndpointTests(unittest.TestCase):
    port = 8784

    @classmethod
    def setUpClass(cls):
        cls._settings_backup = studio_settings.SETTINGS_FILE
        cls.temp = Path(tempfile.mkdtemp(prefix="v7_"))
        studio_settings.SETTINGS_FILE = cls.temp / "settings.json"
        studio_settings.set_data_root(cls.temp / "数据")
        cls._home_backup = web_server.STUDIO_HOME
        web_server.STUDIO_HOME = cls.temp
        cls.server = web_server.serve(port=cls.port, open_browser=False)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        web_server.STUDIO_HOME = cls._home_backup
        studio_settings.SETTINGS_FILE = cls._settings_backup
        shutil.rmtree(cls.temp, ignore_errors=True)

    def _register(self) -> str:
        src = self.temp / "ref.wav"
        _wav(src)
        q = urllib.parse.urlencode({"name": "试听音色", "filename": "ref.wav",
                                    "engine": "mock", "num_steps": 12, "speed": 1.1, "seed": 5})
        req = urllib.request.Request(self.base + "/api/voices?" + q,
                                     data=src.read_bytes(), method="POST")
        return json.loads(urllib.request.urlopen(req).read())["voice_id"]

    def test_state_exposes_engine_params(self):
        vid = self._register()
        state = json.loads(urllib.request.urlopen(self.base + "/api/state").read())
        v = next(x for x in state["voices"] if x["voice_id"] == vid)
        self.assertEqual(v["engine"], "mock")
        self.assertEqual(v["params"]["num_steps"], 12)
        self.assertEqual(v["params"]["speed"], 1.1)

    def test_voice_try_synthesizes_playable_audio(self):
        vid = self._register()
        req = urllib.request.Request(self.base + "/api/run",
                                     data=json.dumps({"action": "voice_try", "voice_id": vid,
                                                      "engine": "mock", "text": "试听一句话。"}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req)
        deadline = time.time() + 30
        job = {}
        while time.time() < deadline:
            job = json.loads(urllib.request.urlopen(self.base + "/api/job").read())
            if job["done"]:
                break
            time.sleep(0.2)
        self.assertTrue(job.get("ok"), job.get("log"))
        self.assertTrue(job.get("try_audio"), "应返回可播放的试听音频路径")
        # 试听音频可经 /api/audio 播放
        with urllib.request.urlopen(self.base + "/api/audio?path=" +
                                    urllib.parse.quote(job["try_audio"])) as r:
            self.assertEqual(r.status, 200)


class UiContractV7Tests(unittest.TestCase):
    def test_index_wires_voice_design_and_font_tab(self):
        html = (Path(web_server.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        for marker in ('data-p="p5"', 'id="p5"', 'id="vEngine"', 'id="pSteps"', 'id="pGuide"',
                       "tryVoice", "applyVoiceDefaults", "fillGrouped", "合成试听"):
            self.assertIn(marker, html, marker)


if __name__ == "__main__":
    unittest.main()

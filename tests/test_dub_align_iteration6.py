"""第六轮迭代测试：音频混流(BGM循环/音效定点/音量) 纯逻辑 + 素材与配置端点 + UI 契约。"""

import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path

from dub_align_studio import audio_mix, settings as studio_settings, web_server
from dub_align_studio.audio_mix import AudioMix, BgmTrack, SfxCue


def _wav(path: Path, secs: float = 1.0) -> bytes:
    with wave.open(str(path), "wb") as h:
        h.setnchannels(1)
        h.setsampwidth(2)
        h.setframerate(8000)
        h.writeframes(b"\x01\x02" * int(8000 * secs))
    return path.read_bytes()


class AudioFiltergraphTests(unittest.TestCase):
    def test_trivial_mix_detected(self):
        self.assertTrue(AudioMix().is_trivial())
        self.assertFalse(AudioMix(master_volume=0.5).is_trivial())
        self.assertFalse(AudioMix(bgm=BgmTrack(Path("b.wav"))).is_trivial())
        self.assertFalse(AudioMix(sfx=[SfxCue(Path("s.wav"))]).is_trivial())

    def test_master_only_volume_graph(self):
        graph, out = audio_mix.build_audio_filtergraph(AudioMix(master_volume=0.5))
        self.assertEqual(out, "[aout]")
        self.assertIn("[1:a]volume=0.500", graph)
        self.assertIn("[aout]", graph)
        self.assertNotIn("amix", graph)  # 只有一路不需要 amix

    def test_bgm_loop_input_and_graph(self):
        mix = AudioMix(bgm=BgmTrack(Path("bgm.wav"), volume=0.3, loop=True))
        inputs = audio_mix.build_audio_inputs(mix)
        self.assertEqual(inputs, [["-stream_loop", "-1", "-i", "bgm.wav"]])  # 循环
        graph, _ = audio_mix.build_audio_filtergraph(mix)
        self.assertIn("[2:a]volume=0.300", graph)   # BGM 是输入 2
        self.assertIn("amix=inputs=2:duration=first:normalize=0", graph)  # 对齐 master、不压音量

    def test_bgm_no_loop(self):
        inputs = audio_mix.build_audio_inputs(AudioMix(bgm=BgmTrack(Path("b.wav"), loop=False)))
        self.assertEqual(inputs, [["-i", "b.wav"]])

    def test_sfx_delay_and_order(self):
        mix = AudioMix(bgm=BgmTrack(Path("b.wav")),
                       sfx=[SfxCue(Path("s1.wav"), at_seconds=2.5, volume=1.2),
                            SfxCue(Path("s2.wav"), at_seconds=0.0)])
        graph, _ = audio_mix.build_audio_filtergraph(mix)
        self.assertIn("[3:a]adelay=2500|2500,volume=1.200", graph)  # 音效在 BGM 之后（输入3）
        self.assertIn("[4:a]adelay=0|0", graph)
        self.assertIn("amix=inputs=4", graph)  # master+bgm+2音效

    def test_volume_clamped(self):
        graph, _ = audio_mix.build_audio_filtergraph(AudioMix(master_volume=99))
        self.assertIn("volume=4.000", graph)  # 上限 4

    def test_mix_from_payload_resolves_and_skips_missing(self):
        got = {}

        def resolve(name):
            return Path("/real/" + name) if name != "缺失.wav" else None

        mix = audio_mix.mix_from_payload({
            "master_volume": 0.8,
            "bgm": {"file": "bgm.mp3", "volume": 0.4, "loop": False},
            "sfx": [{"file": "ding.wav", "at": 3.0, "volume": 1.5},
                    {"file": "缺失.wav", "at": 1.0}],
        }, resolve)
        self.assertEqual(mix.master_volume, 0.8)
        self.assertEqual(mix.bgm.path, Path("/real/bgm.mp3"))
        self.assertFalse(mix.bgm.loop)
        self.assertEqual(len(mix.sfx), 1)   # 缺失音效被跳过，不阻塞成片
        self.assertEqual(mix.sfx[0].at_seconds, 3.0)


class AssetAndConfigEndpointTests(unittest.TestCase):
    port = 8782

    @classmethod
    def setUpClass(cls):
        cls._settings_backup = studio_settings.SETTINGS_FILE
        cls.temp = Path(tempfile.mkdtemp(prefix="assets_"))
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

    def test_asset_upload_list_serve_delete(self):
        source = self.temp / "up.wav"
        payload = _wav(source)
        req = urllib.request.Request(self.base + "/api/assets?filename=" + urllib.parse.quote("鼓点.wav"),
                                     data=payload, method="POST")
        self.assertTrue(json.loads(urllib.request.urlopen(req).read())["ok"])
        listed = json.loads(urllib.request.urlopen(self.base + "/api/assets").read())["assets"]
        self.assertIn("鼓点.wav", [a["file"] for a in listed])
        # 试听（Range）
        url = self.base + "/api/asset?file=" + urllib.parse.quote("鼓点.wav")
        with urllib.request.urlopen(url) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.headers["Content-Type"], "audio/wav")
        # 删除
        d = urllib.request.Request(self.base + "/api/assets/" + urllib.parse.quote("鼓点.wav"), method="DELETE")
        urllib.request.urlopen(d)
        listed = json.loads(urllib.request.urlopen(self.base + "/api/assets").read())["assets"]
        self.assertNotIn("鼓点.wav", [a["file"] for a in listed])
        source.unlink(missing_ok=True)

    def test_asset_rejects_non_audio(self):
        req = urllib.request.Request(self.base + "/api/assets?filename=x.txt", data=b"hi", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 400)

    def test_config_preset_save_list_delete(self):
        body = json.dumps({"name": "甜宠竖屏", "config": {"aspect": "9:16 竖屏", "audio": {"master_volume": 0.9}}})
        saved = json.loads(urllib.request.urlopen(urllib.request.Request(
            self.base + "/api/config", data=body.encode(),
            headers={"Content-Type": "application/json"}, method="POST")).read())
        self.assertIn("甜宠竖屏", saved["presets"])
        got = json.loads(urllib.request.urlopen(self.base + "/api/config").read())
        self.assertEqual(got["presets"]["甜宠竖屏"]["aspect"], "9:16 竖屏")
        d = urllib.request.Request(self.base + "/api/config/" + urllib.parse.quote("甜宠竖屏"), method="DELETE")
        after = json.loads(urllib.request.urlopen(d).read())
        self.assertNotIn("甜宠竖屏", after["presets"])

    def test_config_preset_survives_on_disk(self):
        body = json.dumps({"name": "落盘验证", "config": {"seed": 7}})
        urllib.request.urlopen(urllib.request.Request(
            self.base + "/api/config", data=body.encode(),
            headers={"Content-Type": "application/json"}, method="POST"))
        preset_file = studio_settings.data_root() / "作品参数预设.json"
        self.assertTrue(preset_file.exists())
        self.assertIn("落盘验证", json.loads(preset_file.read_text(encoding="utf-8")))


class UiAudioContractTests(unittest.TestCase):
    def test_index_wires_audio_and_config(self):
        html = (Path(web_server.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        for marker in ('id="mVol"', 'id="bgmSel"', 'id="sfxTrack"', "toggleMixPreview",
                       "collectAudio", "AudioContext", "saveColor", 'id="cfgSel"',
                       "saveConfig", "togglePaths", "strokeShadow", "renderSfxPins"):
            self.assertIn(marker, html, marker)


if __name__ == "__main__":
    unittest.main()

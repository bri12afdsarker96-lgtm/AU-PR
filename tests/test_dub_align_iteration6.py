"""第六轮迭代测试：音频混流(BGM循环/音效定点/音量) 纯逻辑 + 素材与配置端点 + UI 契约。

v0.7.71 P0-2 增补：音量专项锁死——master/BGM/SFX/原视频音效的 0/默认/极值都必须
被前端 collectAudio 与后端 mix_from_payload 忠实保留、进入 ffmpeg 命令。"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path
from unittest import mock

from dub_align_studio import audio_mix, render_b, settings as studio_settings, web_server
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
        # 原视频音效 > 0 也要视为非平凡：必须走 filter_complex 才能把 [0:a] 混入
        # （旧版没这一路 → -an 剥音 → 用户抱怨「原视频音效被剪辑掉、音量控件失效」）
        self.assertFalse(AudioMix(orig_video_volume=1.0).is_trivial())
        self.assertFalse(AudioMix(orig_video_volume=0.35).is_trivial())

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

    def test_orig_video_audio_participates_when_volume_positive(self):
        """orig_video_volume>0 时把 [0:a] 拉进 amix，volume 由该字段决定；
        video_input=None 或 orig_video_volume=0 时**不**接入这一路（避免误引用不存在的音轨）。"""
        graph, out = audio_mix.build_audio_filtergraph(
            AudioMix(master_volume=1.0, orig_video_volume=0.4), master_input=1, video_input=0)
        self.assertEqual(out, "[aout]")
        self.assertIn("[0:a]volume=0.400", graph)
        self.assertIn("amix=inputs=2", graph)  # master + 原视频音效
        # video_input=None → 忽略这一路
        graph2, _ = audio_mix.build_audio_filtergraph(
            AudioMix(orig_video_volume=1.0), master_input=1, video_input=None)
        self.assertNotIn("[0:a]", graph2)
        # orig_video_volume<=0 → 忽略这一路（即使指定了 video_input）
        graph3, _ = audio_mix.build_audio_filtergraph(
            AudioMix(orig_video_volume=0.0), master_input=1, video_input=0)
        self.assertNotIn("[0:a]", graph3)

    def test_orig_video_audio_coexists_with_bgm_and_sfx(self):
        mix = AudioMix(orig_video_volume=0.5, bgm=BgmTrack(Path("b.wav"), volume=0.3),
                       sfx=[SfxCue(Path("s.wav"), at_seconds=1.0, volume=0.8)])
        graph, _ = audio_mix.build_audio_filtergraph(mix, master_input=1, video_input=0)
        # 原音是"master 之后紧接"，BGM 输入号仍是 master+1=2、音效仍是 3（不受原音路影响，
        # 因为原音复用 video_input，不占额外的 -i 输入号）
        self.assertIn("[0:a]volume=0.500", graph)
        self.assertIn("[2:a]volume=0.300", graph)
        self.assertIn("[3:a]adelay=1000|1000,volume=0.800", graph)
        self.assertIn("amix=inputs=4", graph)  # master + orig + bgm + sfx

    def test_mix_from_payload_reads_orig_video_volume(self):
        mix = audio_mix.mix_from_payload({"orig_video_volume": 0.7}, lambda _: None)
        self.assertAlmostEqual(mix.orig_video_volume, 0.7)
        # 未下发时兜底 0（保持旧行为，未升级过前端的老 payload 不会突然带原音）
        mix2 = audio_mix.mix_from_payload({"master_volume": 1.0}, lambda _: None)
        self.assertEqual(mix2.orig_video_volume, 0.0)

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


class ShotAudioFilterTests(unittest.TestCase):
    """画面变速 → 原音永不变速；音频长度按裁剪/变速两条路径分派。"""

    def test_no_speed_uses_target_length(self):
        af = render_b.shot_audio_filter(speed=1.0, src_seconds=8.0, target_seconds=5.0)
        self.assertIn("atrim=0:5.000", af)
        self.assertNotIn("atempo", af)      # 关键约定：音频永不变速
        self.assertNotIn("PTS*", af)
        self.assertIn("aformat=sample_rates=44100:channel_layouts=stereo", af)
        self.assertIn("apad", af)           # 短原音兜底静音

    def test_slowdown_keeps_src_length_pads_to_end(self):
        """放慢：原 3s → 段 6s。原音只有 3s，用 apad 补静音到 6s；atrim 取 min=3s。"""
        af = render_b.shot_audio_filter(speed=0.5, src_seconds=3.0, target_seconds=6.0)
        self.assertIn("atrim=0:3.000", af)
        self.assertNotIn("atempo", af)      # 放慢时也不给音频降速

    def test_speedup_truncates_at_segment_end(self):
        """加速：原 6s → 段 3s。原音保原速率但只保留能进段内的 3s，不外溢下段。"""
        af = render_b.shot_audio_filter(speed=2.0, src_seconds=6.0, target_seconds=3.0)
        self.assertIn("atrim=0:3.000", af)
        self.assertNotIn("atempo", af)

    def test_src_unknown_falls_back_to_target(self):
        """src_seconds<=0（探测失败）时按 target 保底，避免 atrim=0:0.000 空段。"""
        af = render_b.shot_audio_filter(speed=2.0, src_seconds=0.0, target_seconds=4.0)
        self.assertIn("atrim=0:4.000", af)

    def test_shot_speed_matches_video_filter_setpts(self):
        """shot_speed 与 shot_video_filter 内 match_video_to_audio 得到同一 speed——
        主循环预取 speed 给音频侧决策，与画面 setpts 完全一致。"""
        # 变速匹配模式：src=10, target=5 → speed=2.0；shot_video_filter 会用 setpts/2
        speed = render_b.shot_speed(10.0, 5.0, "变速匹配")
        self.assertAlmostEqual(speed, 2.0)
        vf, _ = render_b.shot_video_filter(10.0, 5.0, 720, 1280, 30, "变速匹配")
        self.assertTrue(any("setpts=(PTS-STARTPTS)/2.000000" in p for p in vf))
        # 裁剪模式：speed=1.0
        self.assertEqual(render_b.shot_speed(10.0, 5.0, "裁剪多余画面"), 1.0)


class RenderConfigFfmpegResolutionTests(unittest.TestCase):
    """RenderConfig 归一化 ffmpeg 路径：无论调用方传裸名还是忘了走 make_render_config，
    只要机器上装了 ffmpeg，任务过程中就不该因 PATH/CWD 变化报「找不到 ffmpeg」。"""

    def test_post_init_resolves_bare_names_to_absolute(self):
        tmp = Path(tempfile.mkdtemp(prefix="ffmpeg_stub_"))
        try:
            fake_ffmpeg = tmp / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
            fake_ffprobe = tmp / ("ffprobe.exe" if sys.platform == "win32" else "ffprobe")
            fake_ffmpeg.write_text("stub")
            fake_ffprobe.write_text("stub")
            os.chmod(fake_ffmpeg, 0o755)
            os.chmod(fake_ffprobe, 0o755)
            # 让 settings.ffmpeg_tool 命中我们的假 ffmpeg（把 tmp 塞进已知目录之首）
            with mock.patch.object(studio_settings, "ffmpeg_tool",
                                    side_effect=lambda name="ffmpeg":
                                        str(tmp / (name + (".exe" if sys.platform == "win32" else "")))):
                cfg = render_b.RenderConfig()  # 默认 ffmpeg="ffmpeg" / ffprobe="ffprobe"
            self.assertEqual(cfg.ffmpeg, str(fake_ffmpeg))
            self.assertEqual(cfg.ffprobe, str(fake_ffprobe))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_post_init_keeps_working_absolute_path(self):
        """已给绝对可执行路径时不覆盖——避免每次实例化都跑一遍全盘搜。"""
        tmp = Path(tempfile.mkdtemp(prefix="ffmpeg_keep_"))
        try:
            good = tmp / ("ffmpeg" + (".exe" if sys.platform == "win32" else ""))
            good.write_text("x")
            os.chmod(good, 0o755)
            called = {"n": 0}

            def _spy(name="ffmpeg"):
                called["n"] += 1
                return "/nowhere"

            with mock.patch.object(studio_settings, "ffmpeg_tool", side_effect=_spy):
                cfg = render_b.RenderConfig(ffmpeg=str(good), ffprobe=str(good))
            self.assertEqual(cfg.ffmpeg, str(good))
            self.assertEqual(called["n"], 0)   # 有效绝对路径 → 不再问 settings
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_last_ditch_resolver_recognizes_variants(self):
        """_resolve_exe_last_ditch：ffmpeg.exe / ffmpeg / 相对路径 都要能识别成 ffmpeg。"""
        tmp = Path(tempfile.mkdtemp(prefix="ffmpeg_last_"))
        try:
            real = tmp / ("ffmpeg" + (".exe" if sys.platform == "win32" else ""))
            real.write_text("x")
            os.chmod(real, 0o755)
            with mock.patch.object(studio_settings, "ffmpeg_tool",
                                    side_effect=lambda name="ffmpeg": str(real)):
                self.assertEqual(render_b._resolve_exe_last_ditch("ffmpeg"), str(real))
                self.assertEqual(render_b._resolve_exe_last_ditch("ffmpeg.exe"), str(real))
                self.assertEqual(render_b._resolve_exe_last_ditch("./ffmpeg"), str(real))
            # 非 ffmpeg 系列返回 None，不越界解析（比如 whisper-cli）
            self.assertIsNone(render_b._resolve_exe_last_ditch("whisper-cli"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class UiAudioContractTests(unittest.TestCase):
    def test_mvol_default_is_120_percent(self):
        """配音总音量滑杆默认 120%（用户 2026-08 反馈：100% 感觉太闷，把新基准调到 120%）。
        用正则捕获 mVol 那一行，确认 value="120"——纯搜 'value="120"' 会误命中别处。"""
        import re
        html = (Path(web_server.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        match = re.search(r'<input[^>]*id="mVol"[^>]*>', html)
        self.assertIsNotNone(match, "缺失 mVol 滑杆")
        self.assertIn('value="120"', match.group(0))
        # 显示文本也应为 120%
        self.assertRegex(html, r'id="mVolV">120%</span>')

    def test_index_wires_audio_and_config(self):
        html = (Path(web_server.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        for marker in ('id="mVol"', 'id="bgmSel"', 'id="sfxTrack"', "toggleMixPreview",
                       "collectAudio", "AudioContext", "saveColor", 'id="cfgSel"',
                       "saveConfig", "togglePaths", "strokeShadow", "renderSfxPins",
                       # 原视频音效滑杆——必须存在，否则用户没法在成片里保留分镜自带音效
                       'id="oVol"', "orig_video_volume"):
            self.assertIn(marker, html, marker)


class VolumeChainLockdownTests(unittest.TestCase):
    """v0.7.71 P0-2 音量专项：从 payload → AudioMix → ffmpeg filter_complex 全链路
    锁死每一档合法值（含 0），杜绝"UI 有值但没进 ffmpeg"或"0 被恢复成默认"。"""

    def _resolve(self, name: str):
        # 通过测试用的 resolver：只要文件名非空就当"存在"，返回一个 Path
        if not name:
            return None
        return Path("/tmp") / name

    # ---- master 音量 0/120/200% ----
    def test_master_volume_0_reaches_filtergraph(self):
        mix = audio_mix.mix_from_payload({"master_volume": 0}, self._resolve)
        self.assertEqual(mix.master_volume, 0.0)
        graph, _ = audio_mix.build_audio_filtergraph(mix)
        self.assertIn("volume=0.000", graph)

    def test_master_volume_120_reaches_filtergraph(self):
        mix = audio_mix.mix_from_payload({"master_volume": 1.2}, self._resolve)
        self.assertAlmostEqual(mix.master_volume, 1.2)
        graph, _ = audio_mix.build_audio_filtergraph(mix)
        self.assertIn("volume=1.200", graph)

    def test_master_volume_200_reaches_filtergraph(self):
        mix = audio_mix.mix_from_payload({"master_volume": 2.0}, self._resolve)
        self.assertEqual(mix.master_volume, 2.0)
        graph, _ = audio_mix.build_audio_filtergraph(mix)
        self.assertIn("volume=2.000", graph)

    # ---- 原视频音效 0/100/200% ----
    def test_orig_video_volume_zero_not_in_amix(self):
        """orig_video_volume=0 → is_trivial 视 mix 为无原音；filtergraph 不会加 [0:a]。"""
        mix = audio_mix.mix_from_payload({"orig_video_volume": 0}, self._resolve)
        self.assertEqual(mix.orig_video_volume, 0.0)
        graph, _ = audio_mix.build_audio_filtergraph(mix, video_input=0)
        self.assertNotIn("[0:a]", graph)

    def test_orig_video_volume_100_in_amix_at_1(self):
        mix = audio_mix.mix_from_payload({"orig_video_volume": 1.0}, self._resolve)
        graph, _ = audio_mix.build_audio_filtergraph(mix, video_input=0)
        self.assertIn("[0:a]volume=1.000", graph)

    def test_orig_video_volume_200_in_amix_at_2(self):
        mix = audio_mix.mix_from_payload({"orig_video_volume": 2.0}, self._resolve)
        graph, _ = audio_mix.build_audio_filtergraph(mix, video_input=0)
        self.assertIn("[0:a]volume=2.000", graph)

    # ---- BGM 0% 必须保留 ----
    def test_bgm_volume_zero_preserved(self):
        mix = audio_mix.mix_from_payload({
            "bgm": {"file": "x.mp3", "volume": 0, "loop": True},
        }, self._resolve)
        self.assertIsNotNone(mix.bgm)
        self.assertEqual(mix.bgm.volume, 0.0)   # 0 不被回默 0.35
        graph, _ = audio_mix.build_audio_filtergraph(mix)
        self.assertIn("volume=0.000", graph)

    def test_bgm_loop_false_preserved(self):
        mix = audio_mix.mix_from_payload({
            "bgm": {"file": "x.mp3", "volume": 0.3, "loop": False},
        }, self._resolve)
        self.assertFalse(mix.bgm.loop)

    # ---- SFX 0% 与位置 0 秒必须保留 ----
    def test_sfx_volume_zero_preserved(self):
        mix = audio_mix.mix_from_payload({
            "sfx": [{"file": "s.wav", "at": 2.5, "volume": 0}],
        }, self._resolve)
        self.assertEqual(len(mix.sfx), 1)
        self.assertEqual(mix.sfx[0].volume, 0.0)   # 0 不被回默 1.0
        graph, _ = audio_mix.build_audio_filtergraph(mix)
        self.assertIn("volume=0.000", graph)

    def test_sfx_position_zero_preserved(self):
        mix = audio_mix.mix_from_payload({
            "sfx": [{"file": "s.wav", "at": 0, "volume": 1}],
        }, self._resolve)
        self.assertEqual(mix.sfx[0].at_seconds, 0.0)
        graph, _ = audio_mix.build_audio_filtergraph(mix)
        self.assertIn("adelay=0|0", graph)

    # ---- mix_from_payload 缺失素材 warnings 回调 ----
    def test_missing_bgm_warning_captured(self):
        warns: list[str] = []
        mix = audio_mix.mix_from_payload({
            "bgm": {"file": "无.mp3", "volume": 0.3},
        }, lambda _: None, warnings=warns)
        self.assertIsNone(mix.bgm)   # 素材缺失，bgm 跳过
        self.assertTrue(any("BGM 素材找不到" in w and "无.mp3" in w for w in warns))

    def test_missing_sfx_warning_captured(self):
        warns: list[str] = []
        mix = audio_mix.mix_from_payload({
            "sfx": [{"file": "缺.wav", "at": 0, "volume": 0}],
        }, lambda _: None, warnings=warns)
        self.assertEqual(mix.sfx, [])
        self.assertTrue(any("音效素材找不到" in w and "缺.wav" in w for w in warns))

    def test_missing_asset_no_warnings_when_no_callback(self):
        """warnings=None 时保持旧无回显行为（对现有调用方向后兼容）"""
        mix = audio_mix.mix_from_payload({
            "bgm": {"file": "无.mp3", "volume": 0.3},
        }, lambda _: None)  # 不传 warnings
        self.assertIsNone(mix.bgm)  # 仍静默跳过


class FrontendZeroValuesRoundtripTests(unittest.TestCase):
    """v0.7.71 P0-2：前端 restoreAudio 必须严格用 null/undefined 判断，
    保留合法的 0（音量=0=静音；位置=0 秒）。"""

    def test_restoreAudio_uses_null_check_for_sfx_volume(self):
        """定位 restoreAudio 里的 sfx map；必须**不出现** `+s.volume||1` 之类
        会把 0 吞成默认的表达式。"""
        html = (Path(web_server.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        # 反面断言：不能出现 `+s.volume||1` / `+s.at||0` 这种 falsy-or 语法
        self.assertNotIn("volume:+s.volume||1", html)
        self.assertNotIn("at:+s.at||0", html.replace(",at:+c.at||0", ""))
        # 正面：必须用 s.volume==null / s.at==null 的显式 null 判断
        self.assertIn("s.volume==null", html)
        self.assertIn("s.at==null", html)


class MixWarningSummaryTests(unittest.TestCase):
    """v0.7.71 P1-2：_run_job 在成片结束前必须给出**汇总条**——
    避免逐条 ⚠ 被最后一句"✅ 成片完成"淹没。
    N 必须与真实缺失数量一致。"""

    def test_run_job_appends_summary_when_assets_missing(self):
        """故意提交 payload 里两个不存在的 SFX，末尾必须出现『共有 2 个音频素材被跳过』。"""
        from unittest import mock as _m
        import tempfile as _tf
        from dub_align_studio import studio_pipeline as _pl

        with _tf.TemporaryDirectory() as td:
            (Path(td) / "shot.mp4").write_bytes(b"MP4")
            payload = {
                "action": "run_all",
                "engine": "mock",
                "text": "第一行\n第二行",
                "output_dir": td,
                "shots_dir": td,
                "material_mode": "flat",
                "reuse_dub": False,
                "audio": {
                    "sfx": [
                        {"file": "不存在1.wav", "at": 0, "volume": 1},
                        {"file": "不存在2.wav", "at": 1, "volume": 0.5},
                    ],
                },
            }
            fake_master = _m.MagicMock()
            fake_master.seconds = 2.0; fake_master.engine = "mock"; fake_master.path = Path(td) / "master.wav"
            with _m.patch.object(_pl, "step_dub", return_value=fake_master), \
                 _m.patch.object(_pl, "step_timing", return_value=([], [])), \
                 _m.patch.object(_pl, "step_render") as _sr, \
                 _m.patch.object(_pl, "select_shot_videos", return_value=[Path(td) / "shot.mp4"]):
                _sr.return_value = type("R", (), {"ok": True, "subtitle_note": "",
                                                   "output_path": str(Path(td) / "成片.mp4")})()
                job = web_server.JobState(slot="test", action="run_all")
                web_server._run_job(job, "run_all", payload)
            log_text = "\n".join(job.log)
            self.assertIn("本次共有 2 个音频素材被跳过", log_text,
                           f"缺少 P1-2 汇总条；日志：\n{log_text}")

    def test_no_summary_when_all_assets_present(self):
        """无缺失素材时不应出现汇总条（避免刷屏干扰）。"""
        from unittest import mock as _m
        import tempfile as _tf
        from dub_align_studio import studio_pipeline as _pl
        with _tf.TemporaryDirectory() as td:
            payload = {
                "action": "dub",
                "engine": "mock",
                "text": "行",
                "output_dir": td,
                "audio": {},
            }
            fake_master = _m.MagicMock()
            fake_master.seconds = 1.0; fake_master.engine = "mock"; fake_master.path = Path(td) / "master.wav"
            with _m.patch.object(_pl, "step_dub", return_value=fake_master):
                job = web_server.JobState(slot="test", action="dub")
                web_server._run_job(job, "dub", payload)
            log_text = "\n".join(job.log)
            self.assertNotIn("音频素材被跳过", log_text, log_text)

    def test_no_summary_when_render_fails(self):
        """v0.7.71 P1：成片渲染失败时**绝不**输出'成片中不包含'汇总——
        因为根本没有成片，那句话是假的。"""
        from unittest import mock as _m
        import tempfile as _tf
        from dub_align_studio import studio_pipeline as _pl
        with _tf.TemporaryDirectory() as td:
            (Path(td) / "shot.mp4").write_bytes(b"MP4")
            payload = {
                "action": "run_all",
                "engine": "mock",
                "text": "行",
                "output_dir": td,
                "shots_dir": td,
                "material_mode": "flat",
                "reuse_dub": False,
                "audio": {"sfx": [{"file": "缺失.wav", "at": 0, "volume": 1}]},
            }
            fake_master = _m.MagicMock()
            fake_master.seconds = 1.0; fake_master.engine = "mock"
            fake_master.path = Path(td) / "master.wav"
            fake_result = type("R", (), {"ok": False, "subtitle_note": "",
                                          "output_path": str(Path(td) / "成片.mp4")})()
            with _m.patch.object(_pl, "step_dub", return_value=fake_master), \
                 _m.patch.object(_pl, "step_timing", return_value=([], [])), \
                 _m.patch.object(_pl, "step_render", return_value=fake_result), \
                 _m.patch.object(_pl, "select_shot_videos", return_value=[Path(td) / "shot.mp4"]):
                job = web_server.JobState(slot="test", action="run_all")
                web_server._run_job(job, "run_all", payload)
            log_text = "\n".join(job.log)
            # 逐条 ⚠ 依然出现（用户看到有素材缺失）；但汇总"成片中不包含"绝不出现
            self.assertIn("音效素材找不到：缺失.wav", log_text)
            self.assertNotIn("成片中不包含", log_text,
                              "渲染失败却打了'成片中不包含'汇总——用户会以为有成片")

    def test_no_summary_for_voice_try_action(self):
        """voice_try 根本不产生成片；即便有缺失素材字段也不能打汇总。"""
        from unittest import mock as _m
        import tempfile as _tf
        from dub_align_studio import settings as _st
        with _tf.TemporaryDirectory() as td:
            payload = {
                "action": "voice_try",
                "engine": "mock",
                "text": "试听",
                # 故意塞缺失素材（虽然 voice_try 不用；但_run_job 头部会解析）
                "audio": {"sfx": [{"file": "no.wav", "at": 0, "volume": 1}]},
            }
            with _m.patch.object(_st, "clones_dir", return_value=Path(td)):
                job = web_server.JobState(slot="test", action="voice_try")
                web_server._run_job(job, "voice_try", payload)
            log_text = "\n".join(job.log)
            self.assertNotIn("成片中不包含", log_text)

    def test_summary_when_run_all_render_succeeds(self):
        """run_all + result.ok=True 时正常输出汇总条。"""
        from unittest import mock as _m
        import tempfile as _tf
        from dub_align_studio import studio_pipeline as _pl
        with _tf.TemporaryDirectory() as td:
            (Path(td) / "shot.mp4").write_bytes(b"MP4")
            payload = {
                "action": "run_all",
                "engine": "mock",
                "text": "行",
                "output_dir": td,
                "shots_dir": td,
                "material_mode": "flat",
                "reuse_dub": False,
                "audio": {"sfx": [{"file": "缺失.wav", "at": 0, "volume": 1}]},
            }
            fake_master = _m.MagicMock()
            fake_master.seconds = 1.0; fake_master.engine = "mock"
            fake_master.path = Path(td) / "master.wav"
            fake_result = type("R", (), {"ok": True, "subtitle_note": "",
                                          "output_path": str(Path(td) / "成片.mp4")})()
            with _m.patch.object(_pl, "step_dub", return_value=fake_master), \
                 _m.patch.object(_pl, "step_timing", return_value=([], [])), \
                 _m.patch.object(_pl, "step_render", return_value=fake_result), \
                 _m.patch.object(_pl, "select_shot_videos", return_value=[Path(td) / "shot.mp4"]):
                job = web_server.JobState(slot="test", action="run_all")
                web_server._run_job(job, "run_all", payload)
            log_text = "\n".join(job.log)
            self.assertIn("本次共有 1 个音频素材被跳过", log_text)


if __name__ == "__main__":
    unittest.main()

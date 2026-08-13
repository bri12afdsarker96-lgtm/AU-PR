"""Edge TTS 免费云端预设音色引擎的确定性测试。

网络与 ffmpeg 全部 mock，保证测试离线可跑、无外部依赖。覆盖：
    · 请求 URL/JSON 字段（不发 model / response_format）
    · Endpoint 归一化（服务根 + 用户误填完整接口都能拼对）
    · 空 endpoint 时 probe 与 synthesize_full 给友好中文错
    · MP3→WAV 调用参数（44100 / stereo / pcm_s16le）与 MasterAudio 契约
    · 错误分类：HTTP 429 / JSON 包装 500 / 超时 / 非音频 / 空响应
    · engines 曝光：正式版与 cloud_only 都能看到 edge_tts
    · voice_id 分派：选 edge_tts 不查 voice_library；不触发 cloud_gpu.mark_active
    · UI 契约：Edge 相关 id / api / 引擎切换钩子存在
    · 保留旧引擎行为（synthesize_long / SynthesisOptions 向后兼容）
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from dub_align_studio import settings as studio_settings
from dub_align_studio import studio_pipeline as pipeline
from dub_align_studio import web_server
from dub_align_studio.engines import (
    EdgeTtsEngine,
    EngineUnavailable,
    SynthesisOptions,
    edge_tts,
)


def _wav_bytes(seconds: float = 0.5, sr: int = 44100, ch: int = 2) -> bytes:
    """构造一段固定格式的 PCM16 WAV（供 ffmpeg mock 返回）。"""
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * int(sr * ch * seconds))
    return buf.getvalue()


def _fake_ffmpeg_ok(cmd, **_kwargs):
    """把 -i in.mp3 转 out.wav 的调用当成成功：直接写一段占位 WAV 到目标路径。"""
    # cmd 末位是输出路径
    out = Path(cmd[-1])
    out.write_bytes(_wav_bytes())
    return mock.MagicMock(returncode=0, stdout="", stderr="")


class EndpointNormalizationTests(unittest.TestCase):
    def test_root_kept_as_is(self):
        self.assertEqual(edge_tts._normalize_endpoint_root("https://x.workers.dev"),
                          "https://x.workers.dev")
        self.assertEqual(edge_tts._normalize_endpoint_root("https://x.workers.dev/"),
                          "https://x.workers.dev")

    def test_full_endpoint_stripped(self):
        for url in ("https://x.workers.dev/v1/audio/speech",
                     "https://x.workers.dev/v1/audio/speech/",
                     "HTTPS://x.workers.dev/V1/Audio/Speech/"):
            self.assertEqual(
                edge_tts._normalize_endpoint_root(url).lower().rstrip("/"),
                "https://x.workers.dev",
                url,
            )

    def test_endpoint_url_no_double_slash(self):
        for root in ("https://x.workers.dev", "https://x.workers.dev/",
                      "https://x.workers.dev/v1/audio/speech"):
            self.assertEqual(edge_tts._endpoint_url(root),
                              "https://x.workers.dev/v1/audio/speech")

    def test_empty_root_returns_empty_url(self):
        self.assertEqual(edge_tts._endpoint_url(""), "")


class DefaultsAndProbeTests(unittest.TestCase):
    """未配置 endpoint 时的 probe 与 synthesize_full 行为。"""

    def setUp(self):
        self._backup = studio_settings.SETTINGS_FILE
        self.tmp = Path(tempfile.mkdtemp(prefix="edge_defaults_"))
        studio_settings.SETTINGS_FILE = self.tmp / "settings.json"

    def tearDown(self):
        studio_settings.SETTINGS_FILE = self._backup
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_endpoint_empty(self):
        """任何默认路径都不能预置作者公共 Worker 地址。"""
        with mock.patch.dict("os.environ", {}, clear=False):
            for var in ("EDGE_TTS_ENDPOINT",):
                if var in __import__("os").environ:
                    del __import__("os").environ[var]
            self.assertEqual(edge_tts.edge_tts_endpoint(), "")
        eng = EdgeTtsEngine()
        self.assertEqual(eng.endpoint_root, "")
        status = eng.probe()
        self.assertFalse(status.available)
        self.assertIn("未配置", status.detail)

    def test_probe_live_check_false_never_calls_worker(self):
        """probe(live_check=False) 只做配置检查，不能触发任何 HTTP。"""
        eng = EdgeTtsEngine(endpoint="https://x.workers.dev")
        with mock.patch.object(eng, "_request_mp3",
                                side_effect=AssertionError("不应被调用")):
            status = eng.probe(live_check=False)
        self.assertTrue(status.available)
        self.assertIn("未即时探活", status.detail)

    def test_probe_live_check_true_does_request(self):
        """probe(live_check=True) 才真的合成一次短话验证连通。"""
        eng = EdgeTtsEngine(endpoint="https://x.workers.dev")
        with mock.patch.object(eng, "_request_mp3", return_value=b"\xff\xfb\x00") as m:
            status = eng.probe(live_check=True)
        self.assertTrue(status.available)
        m.assert_called_once()

    def test_synthesize_without_endpoint_raises_friendly(self):
        with self.assertRaises(EngineUnavailable) as ctx:
            EdgeTtsEngine().synthesize_full("你好", None, self.tmp / "m.wav",
                                              SynthesisOptions())
        self.assertIn("未配置", str(ctx.exception))


class RequestPayloadTests(unittest.TestCase):
    """请求 URL / JSON 字段严格符合 Worker 契约。"""

    def _capture_request(self):
        """返回一个假 urlopen，把首次请求的 URL/body 塞进 captured。"""
        captured: dict = {}

        class _FakeResp:
            status = 200
            headers = {"Content-Type": "audio/mpeg"}

            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *args): return False

            def read(self_inner): return b"\xff\xfb\x00\x11FAKEMP3"

        def _fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["headers"] = dict(req.headers)
            return _FakeResp()

        return captured, _fake_urlopen

    def test_post_hits_v1_audio_speech(self):
        captured, fake = self._capture_request()
        eng = EdgeTtsEngine(endpoint="https://x.workers.dev")
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            eng._request_mp3("你好", "zh-CN-XiaoxiaoNeural", speed=1.1, pitch=5, style="cheerful")
        self.assertEqual(captured["url"], "https://x.workers.dev/v1/audio/speech")

    def test_payload_only_worker_supported_fields(self):
        """必须只发 input/voice/speed/pitch/style；绝不发 model / response_format。"""
        captured, fake = self._capture_request()
        eng = EdgeTtsEngine(endpoint="https://x.workers.dev/v1/audio/speech/")  # 误填完整
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            eng._request_mp3("你好", "zh-CN-YunyangNeural", speed=1.0, pitch=0, style="general")
        body = captured["body"]
        self.assertEqual(set(body.keys()), {"input", "voice", "speed", "pitch", "style"})
        self.assertEqual(body["input"], "你好")
        self.assertEqual(body["voice"], "zh-CN-YunyangNeural")
        self.assertEqual(body["style"], "general")
        self.assertNotIn("model", body)
        self.assertNotIn("response_format", body)


class ErrorClassificationTests(unittest.TestCase):
    """错误分类：429 / JSON 包装 500 / 非音频 / 空响应。"""

    def _make_engine(self):
        return EdgeTtsEngine(endpoint="https://x.workers.dev")

    def test_json_wrapped_upstream_429_is_retryable(self):
        """Worker 把上游 429 包成 HTTP 200 + application/json → retryable=True。"""
        payload = json.dumps({"error": {"code": "429", "message": "rate limit exceeded"}}).encode()
        with self.assertRaises(EngineUnavailable) as ctx:
            edge_tts._validate_audio_response("application/json", payload)
        self.assertTrue(getattr(ctx.exception, "retryable", False))

    def test_json_wrapped_fatal_not_retryable(self):
        payload = json.dumps({"error": {"code": "voice_not_supported",
                                          "message": "the voice does not exist"}}).encode()
        with self.assertRaises(EngineUnavailable) as ctx:
            edge_tts._validate_audio_response("application/json", payload)
        self.assertFalse(getattr(ctx.exception, "retryable", False))
        self.assertIn("voice_not_supported", str(ctx.exception))

    def test_non_audio_content_type_rejected(self):
        with self.assertRaises(EngineUnavailable) as ctx:
            edge_tts._validate_audio_response("text/html", b"<html>oops</html>")
        self.assertIn("非音频响应", str(ctx.exception))

    def test_empty_body_rejected(self):
        with self.assertRaises(EngineUnavailable) as ctx:
            edge_tts._validate_audio_response("audio/mpeg", b"")
        self.assertIn("空响应", str(ctx.exception))

    def test_audio_mpeg_ok(self):
        data = b"\xff\xfb\x00" * 10
        self.assertEqual(edge_tts._validate_audio_response("audio/mpeg", data), data)

    def test_http_429_retries_then_gives_hint(self):
        eng = self._make_engine()
        err = urllib.error.HTTPError("http://x", 429, "Too Many Requests", {}, io.BytesIO(b"busy"))
        call = {"n": 0}

        def _raise(*_a, **_k):
            call["n"] += 1
            raise err

        with mock.patch("urllib.request.urlopen", side_effect=_raise), \
             mock.patch.object(edge_tts, "_sleep_backoff"):
            with self.assertRaises(EngineUnavailable) as ctx:
                eng._request_mp3("hi", "v", 1.0, 0, "general")
        # 至少重试满 _MAX_RETRIES 次
        self.assertGreaterEqual(call["n"], edge_tts._MAX_RETRIES)
        self.assertIn("HTTP 429", str(ctx.exception))

    def test_http_500_retries(self):
        eng = self._make_engine()
        err = urllib.error.HTTPError("http://x", 500, "Server Error", {}, io.BytesIO(b"oops"))
        call = {"n": 0}

        def _raise(*_a, **_k):
            call["n"] += 1
            raise err

        with mock.patch("urllib.request.urlopen", side_effect=_raise), \
             mock.patch.object(edge_tts, "_sleep_backoff"):
            with self.assertRaises(EngineUnavailable):
                eng._request_mp3("hi", "v", 1.0, 0, "general")
        self.assertGreaterEqual(call["n"], edge_tts._MAX_RETRIES)

    def test_timeout_reports_seconds(self):
        eng = EdgeTtsEngine(endpoint="https://x.workers.dev", timeout=3.5)
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError()), \
             mock.patch.object(edge_tts, "_sleep_backoff"):
            with self.assertRaises(EngineUnavailable) as ctx:
                eng._request_mp3("hi", "v", 1.0, 0, "general")
        self.assertIn("3 秒", str(ctx.exception))  # int(3.5)=3

    def test_urlerror_retries_then_fails(self):
        eng = self._make_engine()
        with mock.patch("urllib.request.urlopen",
                          side_effect=urllib.error.URLError("dns fail")), \
             mock.patch.object(edge_tts, "_sleep_backoff"):
            with self.assertRaises(EngineUnavailable) as ctx:
                eng._request_mp3("hi", "v", 1.0, 0, "general")
        self.assertIn("连接失败", str(ctx.exception))


class SynthesizeFullContractTests(unittest.TestCase):
    """synthesize_full 端到端：MP3 → ffmpeg → PCM16 WAV → MasterAudio。"""

    def setUp(self):
        self._backup = studio_settings.SETTINGS_FILE
        self.tmp = Path(tempfile.mkdtemp(prefix="edge_full_"))
        studio_settings.SETTINGS_FILE = self.tmp / "settings.json"

    def tearDown(self):
        studio_settings.SETTINGS_FILE = self._backup
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _prepare_engine(self):
        eng = EdgeTtsEngine(endpoint="https://x.workers.dev")
        return eng

    def _patch_ffmpeg(self):
        # 拦截 ffmpeg 调用（edge_tts._convert_mp3_to_wav → integrated_workbench.proc.run_silent）
        return mock.patch("dub_align_studio.engines.edge_tts.run_silent",
                           side_effect=_fake_ffmpeg_ok)

    def test_writes_master_and_metadata(self):
        eng = self._prepare_engine()
        out = self.tmp / "master.wav"
        opts = SynthesisOptions(edge_voice="zh-CN-YunxiNeural", edge_pitch=3, edge_style="chat")
        with mock.patch.object(eng, "_request_mp3", return_value=b"\xff\xfbMP3"), \
             self._patch_ffmpeg():
            master = eng.synthesize_full("你好世界。", None, out, opts)
        self.assertTrue(out.is_file())
        self.assertEqual(master.engine, "edge_tts")
        self.assertEqual(master.voice_id, "zh-CN-YunxiNeural")
        self.assertEqual(master.sample_rate, 44100)
        self.assertIn("edge-worker", master.model)
        # master.json 落盘
        self.assertTrue(master.metadata_path().is_file())

    def test_ffmpeg_called_with_fixed_params(self):
        eng = self._prepare_engine()
        out = self.tmp / "m.wav"
        with mock.patch.object(eng, "_request_mp3", return_value=b"MP3"), \
             mock.patch("dub_align_studio.engines.edge_tts.run_silent",
                          side_effect=_fake_ffmpeg_ok) as m:
            eng.synthesize_full("hi", None, out, SynthesisOptions())
        cmd = m.call_args_list[0].args[0]
        self.assertIn("-ar", cmd); self.assertIn("44100", cmd)
        self.assertIn("-ac", cmd); self.assertIn("2", cmd)
        self.assertIn("-acodec", cmd); self.assertIn("pcm_s16le", cmd)

    def test_tmp_mp3_is_cleaned(self):
        eng = self._prepare_engine()
        out = self.tmp / "m.wav"
        with mock.patch.object(eng, "_request_mp3", return_value=b"MP3"), \
             self._patch_ffmpeg():
            eng.synthesize_full("hi", None, out, SynthesisOptions())
        # 不能残留 .part.mp3
        for p in self.tmp.iterdir():
            self.assertFalse(p.name.endswith(".part.mp3"), p)

    def test_failure_cleans_half_baked_output(self):
        eng = self._prepare_engine()
        out = self.tmp / "m.wav"
        # 先建一个"目标已存在" — 失败时应删掉这次生成的、不留脏
        with mock.patch.object(eng, "_request_mp3",
                                 side_effect=EngineUnavailable("boom")):
            with self.assertRaises(EngineUnavailable):
                eng.synthesize_full("hi", None, out, SynthesisOptions())
        # 不留 .part.mp3 / .part.wav
        for p in self.tmp.iterdir():
            self.assertFalse(p.name.endswith(".part.mp3"), p)
            self.assertFalse(p.name.endswith(".part.wav"), p)

    def test_failure_preserves_prior_output(self):
        """契约：用户重跑同一 chunk / 覆盖 master 时，若本次失败**不能删掉旧 output**——
        双临时策略（tmp_mp3 + tmp_wav → 原子 replace）保证旧文件在最后 replace 之前不动。"""
        eng = self._prepare_engine()
        out = self.tmp / "m.wav"
        out.write_bytes(b"OLD-USER-DATA-DO-NOT-DELETE")
        # 场景 A：网络阶段失败，output 应完整保留
        with mock.patch.object(eng, "_request_mp3",
                                 side_effect=EngineUnavailable("net down")):
            with self.assertRaises(EngineUnavailable):
                eng.synthesize_full("hi", None, out, SynthesisOptions())
        self.assertTrue(out.is_file())
        self.assertEqual(out.read_bytes(), b"OLD-USER-DATA-DO-NOT-DELETE")
        # 场景 B：ffmpeg 转码阶段失败，output 仍应保留旧内容
        with mock.patch.object(eng, "_request_mp3", return_value=b"MP3"), \
             mock.patch("dub_align_studio.engines.edge_tts.run_silent",
                          return_value=mock.MagicMock(returncode=1,
                                                       stdout="", stderr="ffmpeg error")):
            with self.assertRaises(EngineUnavailable):
                eng.synthesize_full("hi", None, out, SynthesisOptions())
        self.assertTrue(out.is_file())
        self.assertEqual(out.read_bytes(), b"OLD-USER-DATA-DO-NOT-DELETE")
        # 不留任何 .part.*
        for p in self.tmp.iterdir():
            self.assertFalse(p.name.endswith(".part.mp3"), p)
            self.assertFalse(p.name.endswith(".part.wav"), p)

    def test_voice_default_when_options_missing_edge_voice(self):
        """edge_voice 空时用引擎默认声线（不炸）。"""
        eng = self._prepare_engine()
        out = self.tmp / "m.wav"
        opts = SynthesisOptions()   # 未指定 edge_voice
        captured = {}

        def _catch(text, voice, speed, pitch, style):
            captured["voice"] = voice
            return b"MP3"

        with mock.patch.object(eng, "_request_mp3", side_effect=_catch), \
             self._patch_ffmpeg():
            eng.synthesize_full("hi", None, out, opts)
        self.assertEqual(captured["voice"], eng.default_voice)


class EngineExposureTests(unittest.TestCase):
    """正式版与 cloud_only 都要曝光 edge_tts。"""

    def test_normal_engines_include_edge_tts(self):
        self.assertIn("edge_tts", pipeline.ENGINE_KEYS)

    def test_cloud_engines_include_edge_tts(self):
        self.assertIn("edge_tts", pipeline.CLOUD_ENGINE_KEYS)
        self.assertIn("dots_remote", pipeline.CLOUD_ENGINE_KEYS)

    def test_make_engine_returns_edge_instance(self):
        eng = pipeline.make_engine("edge_tts")
        self.assertIsInstance(eng, EdgeTtsEngine)

    def test_state_payload_contains_edge_constants(self):
        # /api/state 返回体（不启动 HTTP，直接调函数）
        payload = web_server._state_payload()
        self.assertIn("edge_voices", payload)
        self.assertIn("edge_styles", payload)
        self.assertIn("edge_default_style", payload)
        # 覆盖至少任务书要求的声线
        ids = {v["id"] for v in payload["edge_voices"]}
        for required in ("zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural", "zh-CN-YunyangNeural",
                          "zh-CN-XiaoyiNeural", "zh-CN-XiaochenNeural", "zh-CN-XiaohanNeural",
                          "zh-CN-XiaomengNeural", "zh-CN-XiaomoNeural", "zh-CN-XiaoqiuNeural",
                          "zh-CN-XiaoruiNeural", "zh-CN-XiaoshuangNeural",
                          "zh-CN-XiaoxuanNeural", "zh-CN-XiaoyanNeural", "zh-CN-XiaoyouNeural",
                          "zh-CN-XiaozhenNeural", "zh-CN-YunjianNeural", "zh-CN-YunfengNeural",
                          "zh-CN-YunhaoNeural", "zh-CN-YunxiaNeural", "zh-CN-YunyeNeural",
                          "zh-CN-YunzeNeural"):
            self.assertIn(required, ids, required)
        # cloud_only 视图曝光 edge_tts
        with mock.patch.object(studio_settings, "cloud_only", return_value=True):
            cloud_payload = web_server._state_payload()
        self.assertIn("edge_tts", cloud_payload["engines"])
        self.assertIn("dots_remote", cloud_payload["engines"])


class CloudGpuIsolationTests(unittest.TestCase):
    """选 edge_tts 时**不**触发 cloud_gpu 看门狗（也不查 voice_library）。
    v0.7.71 P0-3：本地 CPU 阶段（timing/render/finalize/capcut/premiere/cleanup）
    即使 payload.engine=dots_remote 也**不**刷；只有 dub/rechunk/voice_try/run_all
    的**远程合成**阶段才刷（run_all 内部由 mark_cloud_gpu_active_now 显式打点）。"""

    def test_uses_cloud_gpu_only_for_dots_remote(self):
        # 直接远程合成 action + dots_remote → True
        self.assertTrue(web_server._uses_cloud_gpu("dub", {"engine": "dots_remote"}))
        self.assertTrue(web_server._uses_cloud_gpu("rechunk", {"engine": "dots_remote"}))
        self.assertTrue(web_server._uses_cloud_gpu("voice_try", {"engine": "dots_remote"}))
        self.assertTrue(web_server._uses_cloud_gpu("run_all", {"engine": "dots_remote"}))
        # 非 dots_remote 引擎无论什么 action → False
        for eng in ("edge_tts", "mock", "dots_local", "fish_local"):
            self.assertFalse(web_server._uses_cloud_gpu("run_all", {"engine": eng}), eng)
            self.assertFalse(web_server._uses_cloud_gpu("dub", {"engine": eng}), eng)
        # v0.7.71 关键契约：本地 CPU 阶段即使 engine=dots_remote 也不刷
        for action in ("timing", "render", "finalize", "capcut", "premiere", "cleanup",
                        "envcheck", "voice_release", "component"):
            self.assertFalse(
                web_server._uses_cloud_gpu(action, {"engine": "dots_remote"}),
                f"{action} + dots_remote 不能刷 cloud_gpu 空闲计时（本地 CPU 阶段）",
            )

    def test_mark_active_dispatch_matrix(self):
        """完整的 action×engine 矩阵：只有 (dub/rechunk/voice_try/run_all)×dots_remote
        才调 manager.mark_active。"""
        cases_should_call = [
            ("dub", "dots_remote"),
            ("rechunk", "dots_remote"),
            ("voice_try", "dots_remote"),
            ("run_all", "dots_remote"),
        ]
        cases_should_not_call = [
            # 本地 CPU 阶段 × dots_remote：绝不刷
            ("timing", "dots_remote"),
            ("render", "dots_remote"),
            ("finalize", "dots_remote"),
            ("capcut", "dots_remote"),
            ("premiere", "dots_remote"),
            ("cleanup", "dots_remote"),
            # 任意 action × 非 dots_remote：绝不刷
            ("dub", "edge_tts"),
            ("run_all", "edge_tts"),
            ("run_all", "mock"),
            ("run_all", "dots_local"),
            ("run_all", "fish_local"),
            ("voice_try", "edge_tts"),
        ]
        for action, engine in cases_should_call:
            with mock.patch("dub_align_studio.cloud_gpu.manager") as m:
                web_server._mark_cloud_gpu_active_if_needed(action, {"engine": engine})
                m.assert_called_once()
        for action, engine in cases_should_not_call:
            with mock.patch("dub_align_studio.cloud_gpu.manager") as m:
                web_server._mark_cloud_gpu_active_if_needed(action, {"engine": engine})
                m.assert_not_called()

    def test_mark_cloud_gpu_active_now_is_unconditional(self):
        """mark_cloud_gpu_active_now 无条件刷——供 run_all 内部在真的进入 dots_remote
        配音阶段时显式调用。"""
        with mock.patch("dub_align_studio.cloud_gpu.manager") as m:
            web_server.mark_cloud_gpu_active_now()
            m.assert_called_once()


class CloudGpuRunJobEndToEndTests(unittest.TestCase):
    """完整 `_run_job` 端到端矩阵（v0.7.71 P0-4）——只测辅助函数不够，必须证明
    真跑 action 时 cloud_gpu 保活次数符合契约。"""

    def _job(self, action):
        return web_server.JobState(slot="test", action=action)

    def _mock_manager(self):
        """把 cloud_gpu.manager 换成 MagicMock，返回 mark_active 计数器句柄。"""
        from unittest.mock import MagicMock as _M
        fake_manager = _M()
        fake_manager.mark_active = _M()
        return fake_manager

    def test_run_all_reuse_dub_never_marks(self):
        """run_all + reuse_dub=True（master 已存在）→ 完全跳过配音 → 0 次 mark_active。"""
        import shutil as _sh, tempfile as _tf, wave as _w, struct as _s, math as _m
        from dub_align_studio import studio_pipeline as _pl
        tmp = Path(_tf.mkdtemp(prefix="ra_reuse_"))
        try:
            master = tmp / _pl.MASTER_NAME
            # 造一段真实 WAV 让 reuse_dub 生效
            with _w.open(str(master), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
                for i in range(22050):
                    w.writeframes(_s.pack("<h", int(1000 * _m.sin(2*_m.pi*440*i/22050))))
            shots = tmp / "shots"; shots.mkdir()
            (shots / "1.mp4").write_bytes(b"MP4")
            payload = {"action": "run_all", "engine": "dots_remote",
                       "text": "只有一行", "output_dir": str(tmp),
                       "shots_dir": str(shots),
                       "reuse_dub": True,
                       "material_mode": "flat"}
            fake_manager = self._mock_manager()
            with mock.patch("dub_align_studio.cloud_gpu.manager", return_value=fake_manager):
                # 屏蔽后续 timing/render 真实调用——只关心配音是否被 reuse
                with mock.patch.object(_pl, "step_timing", return_value=([], [])), \
                     mock.patch.object(_pl, "step_render") as _sr, \
                     mock.patch.object(_pl, "select_shot_videos", return_value=[shots / "1.mp4"]):
                    _sr.return_value = type("R", (), {"ok": True, "subtitle_note": "",
                                                       "output_path": str(tmp / "成片.mp4")})()
                    job = self._job("run_all")
                    web_server._run_job(job, "run_all", payload)
            # 期望：0 次 mark_active（reuse_dub 全跳过；timing/render 不刷）
            self.assertEqual(fake_manager.mark_active.call_count, 0,
                              f"reuse_dub=True 期望 0 次 mark_active，实际 {fake_manager.mark_active.call_count}")
        finally:
            _sh.rmtree(tmp, ignore_errors=True)

    def test_run_all_real_dots_remote_marks_before_and_after(self):
        """run_all + dots_remote 真合成：入口 + 每次 progress/heartbeat + 收尾都 mark。"""
        import shutil as _sh, tempfile as _tf
        from dub_align_studio import studio_pipeline as _pl
        from dub_align_studio.engines.base import MasterAudio
        tmp = Path(_tf.mkdtemp(prefix="ra_real_"))
        try:
            shots = tmp / "shots"; shots.mkdir()
            (shots / "1.mp4").write_bytes(b"MP4")
            payload = {"action": "run_all", "engine": "dots_remote",
                       "text": "只有一行", "output_dir": str(tmp),
                       "shots_dir": str(shots), "material_mode": "flat"}
            fake_manager = self._mock_manager()
            fake_master = MasterAudio(path=tmp / _pl.MASTER_NAME, engine="dots_remote",
                                       voice_id="", model="?", seed=42,
                                       sample_rate=22050, seconds=2.0)
            with mock.patch("dub_align_studio.cloud_gpu.manager", return_value=fake_manager), \
                 mock.patch.object(_pl, "step_dub", return_value=fake_master) as _sd, \
                 mock.patch.object(_pl, "step_timing", return_value=([], [])), \
                 mock.patch.object(_pl, "step_render") as _sr, \
                 mock.patch.object(_pl, "select_shot_videos", return_value=[shots / "1.mp4"]):
                _sr.return_value = type("R", (), {"ok": True, "subtitle_note": "",
                                                   "output_path": str(tmp / "成片.mp4")})()
                job = self._job("run_all")
                web_server._run_job(job, "run_all", payload)
            # 期望：≥2 次（配音开始 + 结束；heartbeat/progress 未被真调因为 step_dub 被 mock）
            self.assertGreaterEqual(fake_manager.mark_active.call_count, 2,
                                     f"真远程配音期望 ≥2 次 mark，实际 {fake_manager.mark_active.call_count}")
        finally:
            _sh.rmtree(tmp, ignore_errors=True)

    def test_dub_action_heartbeat_marks(self):
        """dub 长文合成：入口 + 每次 heartbeat/progress + 收尾都 mark，
        edge/mock 引擎则完全不 mark。"""
        import shutil as _sh, tempfile as _tf
        from dub_align_studio import studio_pipeline as _pl
        from dub_align_studio.engines.base import MasterAudio

        tmp = Path(_tf.mkdtemp(prefix="dub_hb_"))
        try:
            payload = {"action": "dub", "engine": "dots_remote",
                       "text": "行一\n行二", "output_dir": str(tmp)}

            def _fake_step_dub(text, engine_key, output_dir, voice, options,
                                log=None, progress=None, heartbeat=None):
                # 模拟真实合成：先 heartbeat("加载")、progress(1,2)+heartbeat("行1")、progress(2,2)+heartbeat("行2")
                if heartbeat: heartbeat("加载模型")
                if progress: progress(1, 2)
                if heartbeat: heartbeat("行1 生成中")
                if progress: progress(2, 2)
                if heartbeat: heartbeat("行2 生成中")
                return MasterAudio(path=Path(output_dir) / _pl.MASTER_NAME,
                                    engine="dots_remote", voice_id="", model="?", seed=42,
                                    sample_rate=22050, seconds=2.0)

            fake_manager = self._mock_manager()
            with mock.patch("dub_align_studio.cloud_gpu.manager", return_value=fake_manager), \
                 mock.patch.object(_pl, "step_dub", side_effect=_fake_step_dub):
                job = self._job("dub")
                web_server._run_job(job, "dub", payload)
            # 期望：入口 1 + 3 heartbeat + 2 progress + 收尾 1 = 7
            self.assertEqual(fake_manager.mark_active.call_count, 7,
                              f"dub+dots_remote heartbeat/progress 期望 7 次，实际 {fake_manager.mark_active.call_count}")

            # edge_tts 走同一 action 应完全不 mark
            payload_edge = {"action": "dub", "engine": "edge_tts",
                            "text": "行", "output_dir": str(tmp)}
            fake_manager2 = self._mock_manager()
            with mock.patch("dub_align_studio.cloud_gpu.manager", return_value=fake_manager2), \
                 mock.patch.object(_pl, "step_dub", side_effect=_fake_step_dub):
                job = self._job("dub")
                web_server._run_job(job, "dub", payload_edge)
            self.assertEqual(fake_manager2.mark_active.call_count, 0,
                              f"dub+edge_tts 应 0 次，实际 {fake_manager2.mark_active.call_count}")
        finally:
            _sh.rmtree(tmp, ignore_errors=True)

    def test_rechunk_marks_only_for_dots_remote(self):
        """rechunk：dots_remote → mark（前后各一次）；其它引擎 → 0 次。"""
        import shutil as _sh, tempfile as _tf
        from dub_align_studio import studio_pipeline as _pl

        tmp = Path(_tf.mkdtemp(prefix="rc_"))
        try:
            fake_engine = mock.MagicMock()
            with mock.patch.object(_pl, "make_engine", return_value=fake_engine), \
                 mock.patch("dub_align_studio.engines.longform.redub_chunk") as _rc:
                # dots_remote → 期望 2 次 mark
                payload = {"action": "rechunk", "engine": "dots_remote",
                           "text": "行", "output_dir": str(tmp), "chunk_index": 1}
                fake_manager = self._mock_manager()
                with mock.patch("dub_align_studio.cloud_gpu.manager", return_value=fake_manager):
                    web_server._run_job(self._job("rechunk"), "rechunk", payload)
                self.assertEqual(fake_manager.mark_active.call_count, 2,
                                  f"rechunk+dots_remote 期望 2 次，实际 {fake_manager.mark_active.call_count}")
                # mock 引擎 → 0 次
                payload["engine"] = "mock"
                fake_manager2 = self._mock_manager()
                with mock.patch("dub_align_studio.cloud_gpu.manager", return_value=fake_manager2):
                    web_server._run_job(self._job("rechunk"), "rechunk", payload)
                self.assertEqual(fake_manager2.mark_active.call_count, 0)
        finally:
            _sh.rmtree(tmp, ignore_errors=True)

    def test_rechunk_validation_failure_does_not_mark(self):
        """rechunk 输入校验失败（output_dir 为空）→ 0 次 mark_active。"""
        payload = {"action": "rechunk", "engine": "dots_remote",
                   "text": "行", "output_dir": ""}
        fake_manager = self._mock_manager()
        with mock.patch("dub_align_studio.cloud_gpu.manager", return_value=fake_manager):
            web_server._run_job(self._job("rechunk"), "rechunk", payload)
        self.assertEqual(fake_manager.mark_active.call_count, 0,
                          "输入校验失败禁止刷 cloud_gpu")

    def test_voice_try_cache_hit_no_mark(self):
        """voice_try 命中缓存：0 次 mark_active（纯磁盘复用，无远程调用）。"""
        import shutil as _sh, tempfile as _tf
        from dub_align_studio import settings as _st
        tmp = Path(_tf.mkdtemp(prefix="vt_hit_"))
        try:
            # 直接用一个匹配 hash 的 cached 文件塞满
            cache_dir = tmp / "试听缓存"
            cache_dir.mkdir()
            fake_manager = self._mock_manager()
            # 用 patch 让 clones_dir 指向 tmp 并伪造缓存命中
            with mock.patch.object(_st, "clones_dir", return_value=tmp):
                # 计算与实现相同的 sig 值，直接放好命中文件
                import hashlib
                from dub_align_studio.engines import SynthesisOptions
                opts = SynthesisOptions(edge_voice="zh-CN-XiaoxiaoNeural")
                sample = "水星配音对齐，整篇克隆，逐行对齐，一句一画面。"
                sig = hashlib.md5(
                    f"zh-CN-XiaoxiaoNeural|edge_tts|{opts.to_payload()}|{sample}".encode("utf-8")
                ).hexdigest()[:10]
                # _safe_name 是 web_server 内部的清洗函数
                voice_id_safe = "zh-CN-XiaoxiaoNeural"
                cached = cache_dir / f"{voice_id_safe}_edge_tts_{sig}.wav"
                # 写足 44 字节的假 WAV header 让缓存命中判定通过
                cached.write_bytes(b"RIFF" + (b"\x00" * 100))
                payload = {"action": "voice_try", "engine": "edge_tts",
                           "edge_voice": "zh-CN-XiaoxiaoNeural"}
                with mock.patch("dub_align_studio.cloud_gpu.manager", return_value=fake_manager):
                    web_server._run_job(self._job("voice_try"), "voice_try", payload)
            self.assertEqual(fake_manager.mark_active.call_count, 0,
                              "voice_try 命中缓存禁止刷 cloud_gpu")
        finally:
            _sh.rmtree(tmp, ignore_errors=True)

    def test_voice_try_cache_miss_marks_only_for_dots_remote(self):
        """voice_try 缓存未命中：dots_remote → 前后各 1 次；edge/mock → 0 次。"""
        import shutil as _sh, tempfile as _tf, wave as _w, struct as _s, math as _m
        from dub_align_studio import settings as _st
        from dub_align_studio import studio_pipeline as _pl
        from dub_align_studio.engines.base import MasterAudio
        tmp = Path(_tf.mkdtemp(prefix="vt_miss_"))
        try:
            cache_dir = tmp / "试听缓存"; cache_dir.mkdir()

            def _fake_synth(text, voice, cand, opts):
                # 引擎写候选 WAV
                p = Path(cand)
                with _w.open(str(p), "wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
                    for i in range(22050):
                        w.writeframes(_s.pack("<h", int(1000 * _m.sin(2*_m.pi*440*i/22050))))
                return MasterAudio(path=p, engine="dots_remote", voice_id="",
                                    model="?", seed=42, sample_rate=22050, seconds=1.0)

            for engine, expected_marks in (("dots_remote", 2), ("edge_tts", 0), ("mock", 0)):
                fake_manager = self._mock_manager()
                fake_engine = mock.MagicMock()
                fake_engine.synthesize_full = mock.MagicMock(side_effect=_fake_synth)
                with mock.patch.object(_st, "clones_dir", return_value=tmp), \
                     mock.patch.object(_pl, "make_engine", return_value=fake_engine), \
                     mock.patch("dub_align_studio.cloud_gpu.manager", return_value=fake_manager):
                    payload = {"action": "voice_try", "engine": engine,
                               "edge_voice": "zh-CN-XiaoxiaoNeural"}
                    web_server._run_job(self._job("voice_try"), "voice_try", payload)
                self.assertEqual(fake_manager.mark_active.call_count, expected_marks,
                                  f"voice_try+{engine} 期望 {expected_marks} 次，"
                                  f"实际 {fake_manager.mark_active.call_count}")
        finally:
            _sh.rmtree(tmp, ignore_errors=True)


class UiWiringTests(unittest.TestCase):
    def test_index_has_edge_widgets(self):
        html = (Path(web_server.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        for marker in ('id="edgeVoice"', 'id="edgePitch"', 'id="edgeStyle"',
                        'id="edgeRow"', 'id="edgeParamsRow"', 'id="edgeNotice"',
                        'id="edgeTryBtn"', 'id="edgeEp"', 'id="edgeTtsAnchor"',
                        "onEngineChange", "tryEdgeVoice",
                        "loadEdgeSettings", "saveEdgeEndpoint", "testEdgeEndpoint",
                        "/api/edge_tts/test", "edge_tts_endpoint",
                        "Edge TTS（免费云端",  # 引擎下拉标签
                        # 明确"预设 · 不是克隆" 与 "不承诺" 提示
                        "不支持克隆", "稳定性及内容使用授权不由本软件承诺",
                        # 归属：仓库链接（MIT）
                        "wangwangit/tts"):
            self.assertIn(marker, html, marker)


class SynthesizeLongCompatTests(unittest.TestCase):
    """既有 synthesize_long(per_line=True) 逐行拼接对新引擎依旧有效。"""

    def test_per_line_pipeline_stitches_edge_chunks(self):
        from dub_align_studio.engines.longform import synthesize_long

        eng = EdgeTtsEngine(endpoint="https://x.workers.dev")
        tmp = Path(tempfile.mkdtemp(prefix="edge_long_"))
        try:
            out = tmp / "master.wav"
            with mock.patch.object(eng, "_request_mp3", return_value=b"MP3"), \
                 mock.patch("dub_align_studio.engines.edge_tts.run_silent",
                              side_effect=_fake_ffmpeg_ok):
                master = synthesize_long(eng, "第一行\n第二行\n第三行", None, out,
                                          SynthesisOptions(), max_chars=1000, per_line=True)
            self.assertEqual(master.engine, "edge_tts")
            self.assertTrue(out.is_file())
            manifest = (tmp / "master_chunks" / "分段清单.json").read_text(encoding="utf-8")
            self.assertIn("第一行", manifest)
            self.assertIn("第三行", manifest)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SynthesisOptionsCompatTests(unittest.TestCase):
    """SynthesisOptions 新增 edge_* 字段不能破坏旧引擎的调用签名。"""

    def test_options_defaults_backcompat(self):
        opts = SynthesisOptions()
        self.assertEqual(opts.edge_voice, "")
        self.assertEqual(opts.edge_pitch, 0)
        self.assertEqual(opts.edge_style, "general")
        # to_payload 包含新字段
        d = opts.to_payload()
        for k in ("edge_voice", "edge_pitch", "edge_style"):
            self.assertIn(k, d)

    def test_old_engine_ignores_edge_fields(self):
        """MockEngine 用了 SynthesisOptions，但只看它已知的字段——不能因新字段崩。"""
        from dub_align_studio.engines import MockEngine
        eng = MockEngine()
        tmp = Path(tempfile.mkdtemp(prefix="mock_compat_"))
        try:
            out = tmp / "m.wav"
            opts = SynthesisOptions(edge_voice="zh-CN-XiaoxiaoNeural",
                                     edge_pitch=10, edge_style="cheerful")
            master = eng.synthesize_full("行1", None, out, opts)
            self.assertEqual(master.engine, "mock")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

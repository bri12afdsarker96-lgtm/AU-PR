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
        # 不留 .part.mp3
        for p in self.tmp.iterdir():
            self.assertFalse(p.name.endswith(".part.mp3"), p)

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
    """选 edge_tts 时**不**触发 cloud_gpu 看门狗（也不查 voice_library）。"""

    def test_uses_cloud_gpu_only_for_dots_remote(self):
        self.assertTrue(web_server._uses_cloud_gpu("run_all", {"engine": "dots_remote"}))
        self.assertFalse(web_server._uses_cloud_gpu("run_all", {"engine": "edge_tts"}))
        self.assertFalse(web_server._uses_cloud_gpu("run_all", {"engine": "mock"}))
        self.assertFalse(web_server._uses_cloud_gpu("run_all", {"engine": "dots_local"}))
        self.assertFalse(web_server._uses_cloud_gpu("envcheck", {"engine": "dots_remote"}))

    def test_mark_active_only_called_for_dots_remote(self):
        with mock.patch("dub_align_studio.cloud_gpu.manager") as m:
            web_server._mark_cloud_gpu_active_if_needed("run_all", {"engine": "edge_tts"})
            m.assert_not_called()
            web_server._mark_cloud_gpu_active_if_needed("run_all", {"engine": "dots_remote"})
            m.assert_called_once()


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

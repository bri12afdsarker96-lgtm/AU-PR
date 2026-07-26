"""DotsRemoteEngine（云 dots.tts 远程引擎）单测：全程 stdlib，不联网、不依赖 torch/soundfile/numpy。

用 monkeypatch 把 urllib 请求换成罐装响应，验证：
  · 未配置地址 → probe 不可用且给出可读提示；
  · synthesize_full 把返回的 PCM16 wav 落盘、做了起音裁切、返回 MasterAudio(key=dots_remote)；
  · 请求体带上了客户端注入的引子「嗯。」、参考音频 base64、鉴权头；
  · 鉴权失败(401)映射成可读的 EngineUnavailable。
"""

from __future__ import annotations

import array
import io
import json
import unittest
import urllib.error
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

from dub_align_studio.engines.base import EngineUnavailable
from dub_align_studio.engines.dots_remote import DotsRemoteEngine
from dub_align_studio.engines.voice_ref import VoiceRef


def _pcm16_wav(segments) -> bytes:
    """segments=[(amp, ms), …] → 单声道 48k PCM16 wav 字节。"""
    sr = 48000
    data = array.array("h")
    for amp, ms in segments:
        data.extend([amp] * (sr * ms // 1000))
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes(data.tobytes())
    w.close()
    return buf.getvalue()


def _wav_seconds(b: bytes) -> float:
    r = wave.open(io.BytesIO(b), "rb")
    try:
        return r.getnframes() / r.getframerate()
    finally:
        r.close()


class _FakeResp:
    def __init__(self, body: bytes, ctype: str):
        self._body = body
        self.headers = {"Content-Type": ctype}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class DotsRemoteTests(unittest.TestCase):
    def test_probe_unconfigured(self):
        eng = DotsRemoteEngine(endpoint="", api_key="")
        st = eng.probe()
        self.assertFalse(st.available)
        self.assertIn("未配置", st.detail)

    def test_synthesize_roundtrip_and_trim(self):
        # 云端返回：20ms 静音 + 250ms「嗯」+ 500ms 停顿 + 800ms 正文 → 客户端应切掉嗯+停顿
        server_wav = _pcm16_wav([(0, 20), (8000, 250), (0, 500), (12000, 800)])
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = {k.lower(): v for k, v in req.header_items()}
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResp(server_wav, "audio/wav")

        eng = DotsRemoteEngine(endpoint="https://gpu.example.com", api_key="secret123")
        with TemporaryDirectory() as d:
            ref = Path(d) / "ref.wav"
            ref.write_bytes(_pcm16_wav([(6000, 300)]))
            voice = VoiceRef(voice_id="v1", reference_wav=ref, transcript="参考转写", name="测试音色")
            out = Path(d) / "master.wav"
            import dub_align_studio.engines.dots_remote as mod

            orig = mod.urllib.request.urlopen
            mod.urllib.request.urlopen = fake_urlopen
            try:
                master = eng.synthesize_full("你好世界", voice, out)
            finally:
                mod.urllib.request.urlopen = orig

            # 落盘且被裁切（原 1.57s → 只剩 ~0.8s 正文 + keep 余量，远小于原时长）
            self.assertTrue(out.is_file())
            self.assertLess(_wav_seconds(out.read_bytes()), 1.2)
            self.assertGreater(_wav_seconds(out.read_bytes()), 0.6)
            # MasterAudio 正确
            self.assertEqual(master.engine, "dots_remote")
            self.assertEqual(master.voice_id, "v1")
            # 请求：命中 /synthesize、带鉴权头、正文含客户端注入的引子「嗯。」、带参考 base64 与转写
            self.assertTrue(captured["url"].endswith("/synthesize"))
            self.assertEqual(captured["headers"].get("x-api-key"), "secret123")
            self.assertTrue(captured["body"]["text"].startswith("嗯。"))
            self.assertIn("你好世界", captured["body"]["text"])
            self.assertIn("prompt_audio_b64", captured["body"])
            self.assertEqual(captured["body"]["prompt_text"], "参考转写")

    def test_auth_failure_maps_to_readable_error(self):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"detail":"bad key"}'))

        eng = DotsRemoteEngine(endpoint="https://gpu.example.com", api_key="wrong")
        import dub_align_studio.engines.dots_remote as mod

        orig = mod.urllib.request.urlopen
        mod.urllib.request.urlopen = fake_urlopen
        try:
            with TemporaryDirectory() as d:
                with self.assertRaises(EngineUnavailable) as ctx:
                    eng.synthesize_full("文案", None, Path(d) / "o.wav")
            self.assertIn("鉴权失败", str(ctx.exception))
        finally:
            mod.urllib.request.urlopen = orig


if __name__ == "__main__":
    unittest.main()

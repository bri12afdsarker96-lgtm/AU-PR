"""dots.tts 适配器调用签名测试：注入假 dots_tts.runtime + soundfile，验证按 0.2.x 真实 API 调用。

不碰 GPU / 不装真包——只确保导入路径、generate 参数名、返回值落盘三处与上游一致
（用户反馈：module 'dots_tts' has no attribute 'DotsTtsRuntime' 即导入路径错）。
"""

import sys
import tempfile
import types
import unittest
from pathlib import Path

from dub_align_studio.engines import SynthesisOptions
from dub_align_studio.engines.dots_local import DotsLocalEngine
from dub_align_studio.engines.voice_ref import VoiceRef


class _FakeAudio:
    """模拟 torch.Tensor：支持 .float().cpu().squeeze().numpy() 链。"""

    def float(self):
        return self

    def cpu(self):
        return self

    def squeeze(self):
        return self

    def numpy(self):
        return [0.0, 0.1, -0.1]


_MISSING = object()


class _FakeRuntime:
    last_from_pretrained = None
    last_generate = None

    @classmethod
    def from_pretrained(cls, model_name_or_path, **kw):
        cls.last_from_pretrained = {"model": model_name_or_path, **kw}
        return cls()

    def generate(
        self,
        *,
        text,
        prompt_audio_path=None,
        prompt_text=_MISSING,
        num_steps=10,
        guidance_scale=1.2,
        normalize_text=_MISSING,
    ):
        kwargs = {
            "text": text,
            "num_steps": num_steps,
            "guidance_scale": guidance_scale,
        }
        if prompt_audio_path is not None:
            kwargs["prompt_audio_path"] = prompt_audio_path
        if prompt_text is not _MISSING:
            kwargs["prompt_text"] = prompt_text
        if normalize_text is not _MISSING:
            kwargs["normalize_text"] = normalize_text
        _FakeRuntime.last_generate = kwargs
        return {"audio": _FakeAudio(), "sample_rate": 48000}


class DotsAdapterCallSignatureTests(unittest.TestCase):
    def setUp(self):
        _FakeRuntime.last_from_pretrained = None
        _FakeRuntime.last_generate = None
        # 注入假模块：dots_tts（包）+ dots_tts.runtime（含 DotsTtsRuntime）+ soundfile
        self._saved = {n: sys.modules.get(n) for n in ("dots_tts", "dots_tts.runtime", "soundfile")}
        pkg = types.ModuleType("dots_tts")
        runtime_mod = types.ModuleType("dots_tts.runtime")
        runtime_mod.DotsTtsRuntime = _FakeRuntime
        pkg.runtime = runtime_mod
        sf = types.ModuleType("soundfile")
        self.written = {}

        def _write(path, data, sr):
            self.written = {"path": path, "data": data, "sr": sr}
            Path(path).write_bytes(b"RIFFfake")  # 占位文件，供后续存在性检查

        sf.write = _write
        sys.modules["dots_tts"] = pkg
        sys.modules["dots_tts.runtime"] = runtime_mod
        sys.modules["soundfile"] = sf
        # 清运行时缓存，避免跨用例串味
        from dub_align_studio.engines import dots_local
        dots_local._RUNTIME_CACHE.clear()

    def tearDown(self):
        for name, mod in self._saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    def test_generate_uses_runtime_submodule_and_correct_kwargs(self):
        engine = DotsLocalEngine(checkpoint="rednote-hilab/dots.tts-soar")
        out = Path(tempfile.mkdtemp()) / "master.wav"
        voice = VoiceRef(voice_id="晚棠", reference_wav=Path("/ref.wav"),
                         transcript="参考句", name="晚棠")
        engine._generate("要合成的整篇文案", voice, out, SynthesisOptions(num_steps=16, guidance_scale=1.5, seed=7))

        # from_pretrained 传了检查点 + 精度
        self.assertEqual(_FakeRuntime.last_from_pretrained["model"], "rednote-hilab/dots.tts-soar")
        self.assertEqual(_FakeRuntime.last_from_pretrained["precision"], "bfloat16")
        # generate 用的是上游参数名
        g = _FakeRuntime.last_generate
        self.assertEqual(g["text"], "要合成的整篇文案")
        self.assertEqual(g["prompt_audio_path"], "/ref.wav")   # 不是 prompt_audio
        self.assertEqual(g["prompt_text"], "参考句")
        self.assertEqual(g["num_steps"], 16)
        self.assertEqual(g["guidance_scale"], 1.5)
        # 不得传上游不支持的参数，否则真包会 TypeError
        for bad in ("seed", "speed", "max_pause", "max_pause_seconds", "normalize_text", "prompt_audio"):
            self.assertNotIn(bad, g, bad)
        # 落盘用返回的 sample_rate
        self.assertEqual(self.written["sr"], 48000)
        self.assertTrue(out.exists())

    def test_no_transcript_omits_prompt_text(self):
        engine = DotsLocalEngine()
        out = Path(tempfile.mkdtemp()) / "m.wav"
        voice = VoiceRef(voice_id="v", reference_wav=Path("/r.wav"), transcript="", name="v")
        engine._generate("文案", voice, out, SynthesisOptions())
        self.assertIn("prompt_audio_path", _FakeRuntime.last_generate)
        self.assertNotIn("prompt_text", _FakeRuntime.last_generate)

    def test_normalize_text_passes_only_when_enabled_and_supported(self):
        engine = DotsLocalEngine()
        out = Path(tempfile.mkdtemp()) / "m.wav"
        engine._generate("文案", None, out, SynthesisOptions(normalize_text=True))
        self.assertIs(_FakeRuntime.last_generate["normalize_text"], True)

    def test_runtime_cached_across_calls(self):
        from dub_align_studio.engines import dots_local
        engine = DotsLocalEngine()
        out = Path(tempfile.mkdtemp())
        engine._generate("一", None, out / "a.wav", SynthesisOptions())
        engine._generate("二", None, out / "b.wav", SynthesisOptions())
        self.assertEqual(len(dots_local._RUNTIME_CACHE), 1)  # 只加载一次

    def test_dict_result_missing_audio_errors_clearly(self):
        from dub_align_studio.engines.base import EngineUnavailable
        engine = DotsLocalEngine()
        with self.assertRaises(EngineUnavailable):
            engine._save_result({"sample_rate": 48000}, Path(tempfile.mkdtemp()) / "x.wav")


if __name__ == "__main__":
    unittest.main()

"""dots.tts 进程内适配：整篇文案 → 连贯 master.wav。

上游事实（https://github.com/rednote-hilab/dots.tts + PyPI dots.tts 0.2.1，2026-07-23 核实）：
    真实 Python API（子模块，非顶层）：
        from dots_tts.runtime import DotsTtsRuntime
        rt = DotsTtsRuntime.from_pretrained("rednote-hilab/dots.tts-soar",
                                            precision="bfloat16", optimize=True)
        result = rt.generate(text=..., prompt_audio_path=..., prompt_text=...,
                             num_steps=10, guidance_scale=1.2, seed=42)
        # result 为 dict：{"audio": torch.Tensor, "sample_rate": int(48000)}
        soundfile.write(out, result["audio"].float().cpu().squeeze().numpy(),
                        result["sample_rate"])
    2B 全连续 AR，48kHz；零样本克隆（参考音频 + 可选转写）；需 NVIDIA GPU ≥6GB。

纪律：
    - 真引擎不进单元测试（非确定/需 GPU），只进 capability-check 探测（probe）；
      但「调用签名正确性」用注入的假 dots_tts.runtime 模块单测（不碰 GPU）。
    - 惰性导入：未装 dots.tts / 无 GPU 的机器上 import 本模块零副作用。
    - 运行时按 (检查点,精度,优化) 缓存，避免每次成片/试听重复加载 2B 模型。
"""

from __future__ import annotations

import importlib
import importlib.util
from dataclasses import dataclass
from pathlib import Path

from .base import (
    EngineStatus,
    EngineUnavailable,
    MasterAudio,
    SynthesisOptions,
    wav_seconds,
    write_master_metadata,
)
from .voice_ref import VoiceRef


# 检查点仓库（HuggingFace）：SOAR = 自纠偏对齐版，克隆相似度最好，默认选它。
DEFAULT_CHECKPOINT = "rednote-hilab/dots.tts-soar"
_PACKAGE = "dots_tts"            # pip 名 dots.tts → 导入名 dots_tts
_RUNTIME_MODULE = "dots_tts.runtime"   # DotsTtsRuntime 在此子模块
EXPECTED_SAMPLE_RATE = 48000

# 运行时缓存：{(checkpoint, precision, optimize): runtime}，跨多次合成复用已加载的大模型。
_RUNTIME_CACHE: dict = {}


def _installed() -> bool:
    try:
        return importlib.util.find_spec(_PACKAGE) is not None
    except (ImportError, ValueError):
        return False


def _cuda_detail() -> tuple[bool, str]:
    try:
        torch = importlib.import_module("torch")
    except ImportError:
        return False, "未安装 torch。"
    if not torch.cuda.is_available():
        return False, "torch 已装但未检测到可用 CUDA GPU。"
    name = torch.cuda.get_device_name(0)
    free, total = torch.cuda.mem_get_info()
    return True, f"GPU：{name}，显存 {free / 2**30:.1f}/{total / 2**30:.1f} GB 可用（需 ≥6GB，推荐 ≥11GB）。"


_TORCH_CUDA_HINT = (
    "需要 CUDA 版 torch：到 https://pytorch.org/get-started/locally/ 选 CUDA 对应命令，"
    "例如 pip install torch --index-url https://download.pytorch.org/whl/cu121。"
    "无 NVIDIA 显卡的电脑无法用 dots.tts，请改用 fish-speech 或 mock（测试）引擎。"
)


@dataclass
class DotsLocalEngine:
    """dots.tts 本地引擎。checkpoint 可换（base/soar/mf），seed 固定以尽量可复现。"""

    checkpoint: str = DEFAULT_CHECKPOINT
    seed: int = 42
    precision: str = "bfloat16"
    optimize: bool = False   # torch.compile 加速：首次编译慢且在部分 Windows 环境易失败，默认关

    key: str = "dots_local"

    def probe(self) -> EngineStatus:
        if not _installed():
            return EngineStatus(
                key=self.key,
                available=False,
                detail="未安装 dots.tts（工具箱点「安装」，已内置国内镜像）；权重按需下载，不进安装包。",
            )
        cuda_ok, cuda_detail = _cuda_detail()
        if not cuda_ok:
            return EngineStatus(key=self.key, available=False,
                                detail=f"dots.tts 已装；{cuda_detail} {_TORCH_CUDA_HINT}")
        return EngineStatus(
            key=self.key,
            available=True,
            detail=f"dots.tts 可用（检查点 {self.checkpoint}）；{cuda_detail}",
        )

    def synthesize_full(
        self,
        text: str,
        voice: VoiceRef | None,
        output: Path,
        options: SynthesisOptions | None = None,
    ) -> MasterAudio:
        status = self.probe()
        if not status.available:
            raise EngineUnavailable(status.detail)
        if not text.strip():
            raise ValueError("整篇文案为空。")
        options = options or SynthesisOptions(seed=self.seed)
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)

        self._generate(text, voice, output, options)

        seconds = wav_seconds(output)
        master = MasterAudio(
            path=output,
            engine=self.key,
            voice_id=voice.voice_id if voice else "",
            model=self.checkpoint,
            seed=options.seed,
            sample_rate=EXPECTED_SAMPLE_RATE,
            seconds=round(seconds, 3),
            options=options.to_payload(),
        )
        write_master_metadata(master)
        return master

    # ------------------------------------------------------------------
    def _load_runtime(self):
        """加载（并缓存）DotsTtsRuntime。API：from dots_tts.runtime import DotsTtsRuntime。"""
        cache_key = (self.checkpoint, self.precision, self.optimize)
        cached = _RUNTIME_CACHE.get(cache_key)
        if cached is not None:
            return cached
        try:
            runtime_mod = importlib.import_module(_RUNTIME_MODULE)
        except ImportError as exc:
            raise EngineUnavailable(
                f"无法导入 {_RUNTIME_MODULE}（{exc}）。请确认 dots.tts 已正确安装（pip install dots.tts）。"
            ) from exc
        runtime_cls = getattr(runtime_mod, "DotsTtsRuntime", None)
        if runtime_cls is None:
            raise EngineUnavailable(
                f"{_RUNTIME_MODULE} 中未找到 DotsTtsRuntime。请核对 dots.tts 版本（本适配器按 0.2.x API）。"
            )
        runtime = runtime_cls.from_pretrained(
            self.checkpoint, precision=self.precision, optimize=self.optimize)
        _RUNTIME_CACHE[cache_key] = runtime
        return runtime

    def _generate(self, text: str, voice: VoiceRef | None, output: Path, options: SynthesisOptions) -> None:
        """按 dots.tts 0.2.x 真实 API 整篇生成并落盘。

        generate() 接受 text / prompt_audio_path / prompt_text / num_steps /
        guidance_scale / seed，返回 {"audio": tensor, "sample_rate": int}。
        只传上游支持的参数（speed/max_pause 等非其入参，避免 TypeError）。
        """
        runtime = self._load_runtime()
        kwargs: dict = {
            "text": text,
            "num_steps": int(options.num_steps),
            "guidance_scale": float(options.guidance_scale),
            "seed": int(options.seed),
        }
        if voice is not None:
            kwargs["prompt_audio_path"] = str(voice.reference_wav)
            if voice.transcript.strip():
                kwargs["prompt_text"] = voice.transcript.strip()  # 带转写：克隆相似度最高
        try:
            result = runtime.generate(**kwargs)
        except TypeError as exc:
            raise EngineUnavailable(
                f"dots.tts generate() 参数不匹配（{exc}）。请核对 dots.tts 版本（本适配器按 0.2.x）。"
            ) from exc
        except Exception as exc:
            raise EngineUnavailable(f"dots.tts 整篇合成失败：{exc}") from exc
        self._save_result(result, output)

    @staticmethod
    def _save_result(result, output: Path) -> None:
        """把 generate 的返回落成 48kHz WAV。兼容 dict / 张量 / 文件路径三种返回形态。"""
        # ① 直接返回文件路径
        if isinstance(result, (str, Path)):
            src = Path(result)
            if src != output:
                output.write_bytes(src.read_bytes())
            return
        # ② 标准形态：{"audio": tensor, "sample_rate": int}
        audio, sample_rate = result, EXPECTED_SAMPLE_RATE
        if isinstance(result, dict):
            audio = result.get("audio")
            sample_rate = int(result.get("sample_rate", EXPECTED_SAMPLE_RATE))
        if audio is None:
            raise EngineUnavailable("dots.tts 返回结果为空（无 audio）。请核对 dots.tts 版本。")
        try:
            soundfile = importlib.import_module("soundfile")
        except ImportError as exc:
            raise EngineUnavailable("缺少 soundfile（pip install soundfile）用于落盘 dots.tts 音频。") from exc
        array = _to_numpy(audio)
        soundfile.write(str(output), array, sample_rate)


def _to_numpy(audio):
    """torch.Tensor → numpy（float, cpu, squeeze）；已是 numpy/list 则原样。"""
    for step in ("float", "cpu", "squeeze"):
        method = getattr(audio, step, None)
        if callable(method):
            audio = method()
    to_numpy = getattr(audio, "numpy", None)
    return to_numpy() if callable(to_numpy) else audio

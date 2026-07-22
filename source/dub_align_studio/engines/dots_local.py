"""dots.tts 进程内适配（M2a）：整篇文案 → 连贯 master.wav。

上游事实（https://github.com/rednote-hilab/dots.tts，2026-07 核实）：
    - pip 包 `dots.tts`；Python API：DotsTtsRuntime.from_pretrained(...)；
    - 2B 全连续 AR，48kHz 单声道 WAV 输出；
    - 克隆两模式：参考音频 + 转写文本（continuation，相似度最高）/ 仅参考音频（x-vector 提音色）；
    - 显存 5.6–10.5GB（依检查点与音频长度）。

纪律：
    - 真引擎不进单元测试（非确定），只进 capability-check 探测（probe）；
    - 本适配器为惰性导入——未装 dots.tts / 无 GPU 的机器上 import 本模块零副作用，
      probe() 返回不可用原因与下一步指引，synthesize_full 抛 EngineUnavailable；
    - 生成调用签名以本地 GPU 验证轮为准收口（见下方 _generate 注释），
      本文件先固化「接口 + 探测 + 产物契约」这三层不变量。
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
_IMPORT_CANDIDATES = ("dots_tts", "dots.tts")  # pip 包名 dots.tts；模块名以装机实测为准
EXPECTED_SAMPLE_RATE = 48000


def _find_module() -> str | None:
    for name in _IMPORT_CANDIDATES:
        try:
            if importlib.util.find_spec(name) is not None:
                return name
        except (ImportError, ValueError):
            continue
    return None


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


@dataclass
class DotsLocalEngine:
    """dots.tts 本地引擎。checkpoint 可换（base/soar/mf），seed 固定以尽量可复现。"""

    checkpoint: str = DEFAULT_CHECKPOINT
    seed: int = 42

    key: str = "dots_local"

    def probe(self) -> EngineStatus:
        module = _find_module()
        if module is None:
            return EngineStatus(
                key=self.key,
                available=False,
                detail="未安装 dots.tts（pip install dots.tts）；权重走组件下载，不进安装包。",
            )
        cuda_ok, cuda_detail = _cuda_detail()
        if not cuda_ok:
            return EngineStatus(key=self.key, available=False, detail=f"dots.tts 已装；{cuda_detail}")
        return EngineStatus(
            key=self.key,
            available=True,
            detail=f"dots.tts 可用（模块 {module}，检查点 {self.checkpoint}）；{cuda_detail}",
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
    def _generate(self, text: str, voice: VoiceRef | None, output: Path, options: SynthesisOptions) -> None:
        """调 dots.tts Runtime 整篇生成。

        上游文档给出的入口是 DotsTtsRuntime.from_pretrained()；具体生成方法签名
        （参考音频/转写参数名）在本地 GPU 验证轮收口——此处按 README 语义调用，
        任何 API 不匹配都转成 EngineUnavailable 并提示核对 dots.tts 版本，
        不让上层拿到半截产物。
        """
        module = importlib.import_module(_find_module() or _IMPORT_CANDIDATES[0])
        try:
            runtime_cls = getattr(module, "DotsTtsRuntime")
            runtime = runtime_cls.from_pretrained(self.checkpoint)
            # 参数名对照 dots.tts 整合包 Settings 面板；最终以本地 GPU 验证轮收口。
            kwargs: dict = {
                "seed": options.seed,
                "num_steps": options.num_steps,
                "guidance_scale": options.guidance_scale,
                "speed": options.speed,
                "max_pause": options.max_pause_seconds,
                "normalize_text": options.normalize_text,
            }
            if voice is not None:
                kwargs["prompt_audio"] = str(voice.reference_wav)
                if voice.transcript.strip():
                    # continuation clone：带参考转写，说话人相似度最高。
                    kwargs["prompt_text"] = voice.transcript.strip()
            waveform = runtime.generate(text=text, **kwargs)
            self._save_wav(module, waveform, output)
        except EngineUnavailable:
            raise
        except AttributeError as exc:
            raise EngineUnavailable(
                f"dots.tts API 与适配器不匹配（{exc}）。请核对 dots.tts 版本，"
                "或在本地 GPU 验证轮更新 dots_local._generate 的调用签名。"
            ) from exc
        except Exception as exc:
            raise EngineUnavailable(f"dots.tts 整篇合成失败：{exc}") from exc

    @staticmethod
    def _save_wav(module, waveform, output: Path) -> None:
        # dots.tts 用 soundfile 落盘 48kHz 单声道 WAV；waveform 若已是文件路径则直接收下。
        if isinstance(waveform, (str, Path)):
            src = Path(waveform)
            if src != output:
                output.write_bytes(src.read_bytes())
            return
        soundfile = importlib.import_module("soundfile")
        soundfile.write(str(output), waveform, EXPECTED_SAMPLE_RATE)

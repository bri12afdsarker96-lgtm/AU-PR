"""云 dots.tts 推理服务端（配合客户端 DotsRemoteEngine）。

职责单一——一个「无脑 GPU worker」：
  · 收：文本 + 参考音频(base64) + 转写 + 合成参数；
  · 做：调用本机 GPU 上的 dots.tts runtime.generate 生成整句；
  · 吐：原始 PCM16 WAV 字节。

**不做**引子「嗯。」注入，也**不做**起音/尾部裁切——这些都在客户端完成，服务端保持纯粹，
方便任意 GPU 机器直接部署（只需 torch/CUDA + dots_tts + soundfile + fastapi + uvicorn）。

鉴权：请求头 X-API-Key 必须等于环境变量 DOTS_SERVER_API_KEY（未设则拒绝启动，杜绝裸奔）。

启动：
    export DOTS_SERVER_API_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(24))")
    export DOTS_CHECKPOINT=/root/models/dots.tts      # dots 检查点目录（或 HF 名）
    python dots_tts_server.py --host 0.0.0.0 --port 8000
详见同目录部署文档。
"""

from __future__ import annotations

import argparse
import base64
import inspect
import io
import os
import re
import tempfile
import threading
from pathlib import Path

try:
    import soundfile
    import uvicorn
    from fastapi import FastAPI, Header, HTTPException
    from fastapi.responses import JSONResponse, Response
    from pydantic import BaseModel
except Exception as exc:  # noqa: BLE001
    raise SystemExit(
        f"缺少服务端依赖（{exc}）。请先：pip install -r requirements.txt\n"
        "（fastapi uvicorn soundfile pydantic；另需 torch/CUDA 与 dots_tts）"
    )

# ---- 配置（环境变量） --------------------------------------------------------
API_KEY = os.environ.get("DOTS_SERVER_API_KEY", "").strip()
# 与客户端 dots_local.DEFAULT_CHECKPOINT 一致：HF 仓库名，首次 from_pretrained 自动下载，
# 无需手动拷检查点。国内机器可设 HF_ENDPOINT=https://hf-mirror.com 加速（见部署脚本）。
CHECKPOINT = os.environ.get("DOTS_CHECKPOINT", "").strip() or "rednote-hilab/dots.tts-soar"
RUNTIME_MODULE = os.environ.get("DOTS_RUNTIME_MODULE", "dots_tts.runtime").strip()
PRECISION = os.environ.get("DOTS_PRECISION", "bf16").strip()
OPTIMIZE = os.environ.get("DOTS_OPTIMIZE", "1").strip() not in ("0", "false", "False", "")
EXPECTED_SAMPLE_RATE = 48000
MGL_OUTPUT_BUDGET = 800   # 与客户端一致：参考音频较长时 max_generate_length 的额外预算

app = FastAPI(title="dots.tts remote", version="1.0")
_runtime = None
_runtime_lock = threading.Lock()
_mgl_cache: dict[str, int] = {}


def _ensure_tn_stub() -> bool:
    """dots_tts.utils.text 导入时硬性 `from tn.chinese.normalizer import Normalizer`。
    `tn` 来自 WeTextProcessing（依赖 pynini），我们一贯跳过。真 tn 不在时注入「原样返回」的桩，
    让 dots.tts 能导入并出声——只跳过数字/符号口语化预处理，不影响克隆音色。与本地引擎同一口径。"""
    import importlib.util
    import sys
    import types

    if "tn" in sys.modules:
        return True
    try:
        if importlib.util.find_spec("tn") is not None:
            return False  # 真 WeTextProcessing 在，优先用它
    except (ImportError, ValueError):
        pass

    class _PassthroughNormalizer:
        def __init__(self, *args, **kwargs):
            pass

        def normalize(self, text, *args, **kwargs):
            return text

    modules: dict = {}
    tn = types.ModuleType("tn")
    modules["tn"] = tn
    for lang in ("chinese", "english"):
        pkg = types.ModuleType(f"tn.{lang}")
        norm = types.ModuleType(f"tn.{lang}.normalizer")
        norm.Normalizer = _PassthroughNormalizer
        pkg.normalizer = norm
        setattr(tn, lang, pkg)
        modules[f"tn.{lang}"] = pkg
        modules[f"tn.{lang}.normalizer"] = norm
    sys.modules.update(modules)
    return True


# ---- 模型加载（懒加载 + 缓存） ----------------------------------------------
def _load_runtime():
    global _runtime
    if _runtime is not None:
        return _runtime
    with _runtime_lock:
        if _runtime is not None:
            return _runtime
        import importlib

        _ensure_tn_stub()   # 必须在导入 runtime 前注入 tn 桩（否则 from tn.chinese... 直接 ModuleNotFoundError）
        mod = importlib.import_module(RUNTIME_MODULE)
        cls = getattr(mod, "DotsTtsRuntime", None)
        if cls is None:
            raise RuntimeError(f"{RUNTIME_MODULE} 中未找到 DotsTtsRuntime，请核对 dots.tts 版本。")
        _runtime = cls.from_pretrained(CHECKPOINT, precision=PRECISION, optimize=OPTIMIZE)
        return _runtime


def _gpu_name() -> str:
    try:
        import torch

        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
        return "CPU（未检测到 CUDA，配音会非常慢）"
    except Exception:  # noqa: BLE001
        return "unknown"


def _supported_kwargs(runtime, kwargs: dict) -> dict:
    """只把 generate() 真实签名支持的参数传进去，避免 TypeError（不同 dots 版本入参有差异）。"""
    try:
        sig = inspect.signature(runtime.generate)
    except (TypeError, ValueError):
        return dict(kwargs)
    if any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
        return dict(kwargs)
    allowed = set(sig.parameters)
    return {k: v for k, v in kwargs.items() if k in allowed}


def _generate_wav_bytes(payload: "SynthReq") -> bytes:
    runtime = _load_runtime()
    kwargs: dict = {
        "text": payload.text,
        "num_steps": int(payload.num_steps),
        "guidance_scale": float(payload.guidance_scale),
        "seed": int(payload.seed),
    }
    if payload.normalize_text:
        kwargs["normalize_text"] = True

    ref_path = None
    ref_key = ""
    try:
        if payload.prompt_audio_b64:
            suffix = Path(payload.prompt_audio_name or "ref.wav").suffix or ".wav"
            fd, ref_path = tempfile.mkstemp(suffix=suffix)
            with os.fdopen(fd, "wb") as fh:
                fh.write(base64.b64decode(payload.prompt_audio_b64))
            ref_key = ref_path
            kwargs["prompt_audio_path"] = ref_path
            if payload.prompt_text.strip():
                kwargs["prompt_text"] = payload.prompt_text.strip()
            if ref_key in _mgl_cache:
                kwargs["max_generate_length"] = _mgl_cache[ref_key]

        try:
            result = runtime.generate(**_supported_kwargs(runtime, kwargs))
        except Exception as exc:  # noqa: BLE001
            result = _retry_longer(runtime, kwargs, ref_key, exc)
    finally:
        if ref_path:
            try:
                os.unlink(ref_path)
            except OSError:
                pass

    audio, sr = _extract_audio(result)
    buf = io.BytesIO()
    soundfile.write(buf, audio, sr, format="WAV", subtype="PCM_16")  # 固定 PCM16，客户端 stdlib 可读
    return buf.getvalue()


def _retry_longer(runtime, kwargs: dict, ref_key: str, exc: Exception):
    """参考音频较长时 dots 要求 max_generate_length > 参考 patch 数；从报错里取真实值精确重试一次。"""
    m = re.search(r"prompt_audio_patch_count\s*=\s*(\d+)", str(exc))
    if not m or int(kwargs.get("max_generate_length") or 0) > int(m.group(1)):
        raise RuntimeError(f"dots.tts 合成失败：{exc}") from exc
    need = int(m.group(1)) + MGL_OUTPUT_BUDGET
    kwargs["max_generate_length"] = need
    supported = _supported_kwargs(runtime, kwargs)
    if "max_generate_length" not in supported:
        raise RuntimeError(f"dots.tts 版本不支持 max_generate_length，无法适配较长参考音频：{exc}") from exc
    result = runtime.generate(**supported)
    if ref_key:
        _mgl_cache[ref_key] = need
    return result


def _extract_audio(result):
    """兼容 generate 的多种返回形态，统一转成 (numpy 一维/二维, sample_rate)。"""
    if isinstance(result, (str, Path)):
        audio, sr = soundfile.read(str(result), dtype="float32")
        return audio, int(sr)
    audio, sr = result, EXPECTED_SAMPLE_RATE
    if isinstance(result, dict):
        audio = result.get("audio")
        sr = int(result.get("sample_rate", EXPECTED_SAMPLE_RATE))
    if audio is None:
        raise RuntimeError("dots.tts 返回结果为空（无 audio）。")
    try:  # torch tensor → numpy
        audio = audio.float().cpu().squeeze().numpy()
    except AttributeError:
        import numpy as np

        audio = np.asarray(audio, dtype="float32").squeeze()
    return audio, int(sr)


# ---- 接口 -------------------------------------------------------------------
class SynthReq(BaseModel):
    text: str
    prompt_audio_b64: str | None = None
    prompt_audio_name: str = "ref.wav"
    prompt_text: str = ""
    num_steps: int = 10
    guidance_scale: float = 1.2
    seed: int = 42
    normalize_text: bool = False


def _auth(x_api_key: str | None):
    if not API_KEY:
        raise HTTPException(status_code=500, detail="服务端未设置 DOTS_SERVER_API_KEY。")
    if (x_api_key or "") != API_KEY:
        raise HTTPException(status_code=401, detail="API Key 不正确。")


@app.get("/health")
def health(x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    _auth(x_api_key)
    return {"status": "ok", "gpu": _gpu_name(), "model_loaded": _runtime is not None,
            "checkpoint": CHECKPOINT, "sample_rate": EXPECTED_SAMPLE_RATE}


@app.post("/synthesize")
def synthesize(req: SynthReq, x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    _auth(x_api_key)
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="text 为空。")
    try:
        wav = _generate_wav_bytes(req)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"detail": f"合成失败：{exc}"})
    return Response(content=wav, media_type="audio/wav")


def main():
    ap = argparse.ArgumentParser(description="dots.tts 云推理服务端")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--preload", action="store_true", help="启动即加载模型到显存（否则首个请求再加载）")
    args = ap.parse_args()
    if not API_KEY:
        raise SystemExit(
            "未设置 DOTS_SERVER_API_KEY，拒绝启动（避免接口裸奔被白嫖/打爆显卡）。\n"
            "先执行：export DOTS_SERVER_API_KEY=$(python -c \"import secrets;print(secrets.token_urlsafe(24))\")"
        )
    if args.preload:
        print("预加载 dots.tts 模型到显存…")
        _load_runtime()
        print("模型就绪。")
    print(f"dots.tts 云端启动：http://{args.host}:{args.port}  GPU={_gpu_name()}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

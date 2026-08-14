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
import heapq
import tempfile
import threading
from pathlib import Path

try:
    import soundfile
    import uvicorn
    from fastapi import FastAPI, Header, HTTPException, Request
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
# optimize=True 走 torch.compile(inductor)，能显著提速(单卡 AR 的主要加速手段)，但在 torch 2.9 上
# 曾崩(flex_attention「duplicate template name」)。DOTS_OPTIMIZE:
#   auto(默认) = 尝试开启 torch.compile；预热若崩则**自动回退 eager**(不影响可用，只是慢一点)
#   1/true     = 强制开启(崩了就报错)
#   0/false    = 强制关闭(纯 eager，最稳)
_OPT_MODE = os.environ.get("DOTS_OPTIMIZE", "auto").strip().lower()
EXPECTED_SAMPLE_RATE = 48000
MGL_OUTPUT_BUDGET = 800   # 与客户端一致：参考音频较长时 max_generate_length 的额外预算
MAX_INFLIGHT_PER_USER = int(os.environ.get("DOTS_MAX_INFLIGHT_PER_USER", "6"))  # 单用户排队上限，防刷占满

app = FastAPI(title="dots.tts remote", version="1.0")
_runtime = None
_runtime_lock = threading.Lock()
_mgl_cache: dict[str, int] = {}

# ---- 公平调度（多用户共用单 GPU）--------------------------------------------
# 单卡上 dots.generate 非线程安全且只有一张卡 → 一次只能跑一句。多用户如何排？
# 用「加权公平队列(WFQ)·等权」：每个用户各有一条虚拟时间线，谁被服务得越少虚拟时间越小、
# 越先被取——天然「人人平等轮流」，一个用户狂发也占不满、别人不饿死。单后台派发线程独占 GPU。
_sched_cv = threading.Condition()
_sched_heap: list = []            # [tag, seq, job]；tag 小者先出
_sched_seq = 0
_user_vtime: dict = {}            # user -> 该用户最近一次的虚拟完成刻度
_user_inflight: dict = {}         # user -> 在队/在跑数（限流用）
_global_vtime = 0.0
_dispatch_user = None             # 当前正在服务的用户（/health 展示）
_dispatcher: threading.Thread | None = None


class _Busy(Exception):
    """单用户排队数超上限。"""


class _Job:
    __slots__ = ("req", "user", "event", "result", "error")

    def __init__(self, req, user):
        self.req = req
        self.user = user
        self.event = threading.Event()
        self.result = None
        self.error = None


def _submit(req, user: str) -> "_Job":
    """把请求按等权 WFQ 入队；返回 _Job，调用方 event.wait() 等结果。超单用户上限抛 _Busy。"""
    global _sched_seq
    with _sched_cv:
        if _user_inflight.get(user, 0) >= MAX_INFLIGHT_PER_USER:
            raise _Busy()
        _user_inflight[user] = _user_inflight.get(user, 0) + 1
        tag = max(_global_vtime, _user_vtime.get(user, 0.0)) + 1.0  # 等权：每请求虚拟时间 +1 → 轮转
        _user_vtime[user] = tag
        _sched_seq += 1
        job = _Job(req, user)
        heapq.heappush(_sched_heap, [tag, _sched_seq, job])
        _sched_cv.notify()
        return job


def _dispatcher_loop():
    """独占 GPU 的单派发线程：每次取虚拟时间最小的请求跑，保证一次只一句 + 用户间公平。"""
    global _global_vtime, _dispatch_user
    while True:
        with _sched_cv:
            while not _sched_heap:
                _sched_cv.wait()
            tag, _seq, job = heapq.heappop(_sched_heap)
            _global_vtime = max(_global_vtime, tag)
            _dispatch_user = job.user
        try:
            job.result = _generate_wav_bytes(job.req)
        except Exception as exc:  # noqa: BLE001
            job.error = exc
        finally:
            with _sched_cv:
                _user_inflight[job.user] = max(0, _user_inflight.get(job.user, 1) - 1)
                _dispatch_user = None
            job.event.set()


def _ensure_dispatcher():
    global _dispatcher
    with _sched_cv:
        if _dispatcher is None or not _dispatcher.is_alive():
            _dispatcher = threading.Thread(target=_dispatcher_loop, name="gpu-dispatcher", daemon=True)
            _dispatcher.start()


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

        want_opt = _OPT_MODE in ("auto", "1", "true", "yes", "on")
        if want_opt:
            try:
                print(f"[optimize] 尝试开启 torch.compile 加速（DOTS_OPTIMIZE={_OPT_MODE}）…")
                rt = cls.from_pretrained(CHECKPOINT, precision=PRECISION, optimize=True)
                # 预热一小句真正触发编译；auto 模式下若崩→回退 eager，1/true 模式下崩→抛错
                rt.generate(**_supported_kwargs(rt, {
                    "text": "嗯。你好。", "num_steps": 4, "guidance_scale": 1.0}))
                _runtime = rt
                print("[optimize] torch.compile 已启用（加速生效）。")
                return _runtime
            except Exception as exc:  # noqa: BLE001
                if _OPT_MODE not in ("auto",):
                    raise
                print(f"[optimize] torch.compile 预热失败，自动回退 eager（不影响可用，仅速度）：{exc}")
        _runtime = cls.from_pretrained(CHECKPOINT, precision=PRECISION, optimize=False)
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
    with _sched_cv:
        queued = len(_sched_heap)
        waiting_users = len({j[2].user for j in _sched_heap})
        busy = _dispatch_user is not None
    return {"status": "ok", "gpu": _gpu_name(), "model_loaded": _runtime is not None,
            "busy": busy,                    # True = 正在配一句
            "queued": queued,                # 排队中的请求数
            "waiting_users": waiting_users,  # 排队中的用户数
            "checkpoint": CHECKPOINT, "sample_rate": EXPECTED_SAMPLE_RATE}


def _client_user(x_user_id: str | None, request: "Request") -> str:
    """识别用户：优先客户端上报的 X-User-Id（每台安装稳定唯一），退化到来源 IP，再不行归为 default。
    这样多用户共用同一把 API Key 也能被公平区分、各自限流。"""
    uid = (x_user_id or "").strip()
    if uid:
        return uid[:64]
    try:
        return (request.client.host if request and request.client else "") or "default"
    except Exception:  # noqa: BLE001
        return "default"


@app.post("/synthesize")
def synthesize(req: SynthReq, request: Request,
               x_api_key: str | None = Header(default=None, alias="X-API-Key"),
               x_user_id: str | None = Header(default=None, alias="X-User-Id")):
    _auth(x_api_key)
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="text 为空。")
    _ensure_dispatcher()
    user = _client_user(x_user_id, request)
    try:
        job = _submit(req, user)
    except _Busy:
        return JSONResponse(status_code=429, content={
            "detail": f"你的排队请求已达上限（{MAX_INFLIGHT_PER_USER}），请等前面的配音完成后再试。"})
    job.event.wait()                          # 等公平队列轮到并跑完（客户端每次只发一句，故通常很快轮到）
    if job.error is not None:
        return JSONResponse(status_code=500, content={"detail": f"合成失败：{job.error}"})
    return Response(content=job.result, media_type="audio/wav")


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
    _ensure_dispatcher()   # 启动公平派发线程（多用户等权轮流，单卡一次一句）
    print(f"dots.tts 云端启动：http://{args.host}:{args.port}  GPU={_gpu_name()}  "
          f"（多用户公平队列·单用户上限 {MAX_INFLIGHT_PER_USER}）")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

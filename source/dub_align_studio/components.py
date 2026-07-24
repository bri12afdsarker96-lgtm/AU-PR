"""工具箱组件商店：缺什么，点击即可下载/安装。

沿用水星轻量安装纪律：任何重资产都不进安装包，全部按需获取——
    - 下载类（download）：whisper-cli 运行时 + ggml 模型，复用水星
      model_registry（URL/size/SHA256）与 component_download（多源+断点续传+校验）；
    - 安装类（pip）：dots.tts / pyCapCut，调本机 pip 安装，输出逐行进日志；
    - 安装类（install）：fish-speech（源码镜像下载 → pip 装依赖 → 拉模型 →
      工具箱一键启动/停止本地 server，分阶段可断点续装）。

install_component 在后台任务槽里执行（web_server 多任务并发），log 回调逐行汇报进度。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import threading
import zipfile
from pathlib import Path
from typing import Callable

from integrated_workbench.component_download import download_verified_file
from integrated_workbench.model_registry import registry_entry, validate_component_file

from . import settings as studio_settings

LogFn = Callable[[str], None]

# key → 展示与行为定义（顺序即工具箱展示顺序）
COMPONENTS: list[dict] = [
    {"key": "whisper_cli", "name": "whisper-cli 运行时", "kind": "download",
     "purpose": "计时尺子：逐行量出配音时长（约 8MB）"},
    {"key": "tiny", "name": "ggml-tiny 模型", "kind": "download",
     "purpose": "whisper 最小模型（约 75MB，速度最快）"},
    {"key": "base", "name": "ggml-base 模型", "kind": "download",
     "purpose": "whisper 均衡模型（约 142MB，推荐）"},
    {"key": "small", "name": "ggml-small 模型", "kind": "download",
     "purpose": "whisper 高精模型（约 466MB）"},
    {"key": "torch_cuda", "name": "PyTorch GPU 版 (CUDA 12.1)", "kind": "torch",
     "purpose": "dots.tts / fish-speech 的 GPU 运行时（NVIDIA RTX 20/30/40 系适用，约 2.5GB）"},
    {"key": "dots_tts", "name": "dots.tts 配音引擎", "kind": "dots", "package": "dots.tts",
     "purpose": "整篇声音克隆（2B/48kHz，需先装 PyTorch GPU 版 + NVIDIA GPU ≥6GB）"},
    {"key": "pycapcut", "name": "pyCapCut 草稿组件", "kind": "pip", "package": "pycapcut",
     "purpose": "本机直接生成真实剪映草稿（缺失时交接包照常导出）"},
    {"key": "fish_speech", "name": "fish-speech 引擎", "kind": "install",
     "purpose": "整篇声音克隆备选（一键装源码+依赖+模型，装好后点「启动服务」）"},
]

# PyTorch GPU 版：官方 CUDA 12.1 wheel 源（RTX 20/30/40 系通用；不走 PyPI 镜像，torch 只在此源）
TORCH_CUDA_INDEX = "https://download.pytorch.org/whl/cu121"

# fish-speech 一键安装的固定口径
FISH_SOURCE_URLS = ["https://github.com/fishaudio/fish-speech/archive/refs/heads/main.zip"]
FISH_MODEL_REPO = "fishaudio/fish-speech-1.5"
FISH_SERVER_ADDR = "127.0.0.1:8080"
_HF_MIRROR = "https://hf-mirror.com"  # HF 直连不可达时的国内镜像（可用 HF_ENDPOINT 覆盖）


def _prepend_sys_paths(paths: list[Path]) -> None:
    for root in reversed(paths):
        if root.exists() and str(root) not in sys.path:
            sys.path.insert(0, str(root))


def _component_python_roots(package: str) -> list[Path]:
    """Offline Python package roots under the portable data/components folder."""
    root = studio_settings.components_root()
    torch_root = root / "torch" / "python"
    normalized = package.replace("-", "_").replace(".", "_").lower()
    if normalized in {"torch", "torchaudio"}:
        return [torch_root]
    if normalized == "dots_tts":
        return [torch_root, root / "dots.tts" / "python"]
    if normalized == "pycapcut":
        return [root / "pyCapCut", root / "pycapcut"]
    if normalized == "fish_speech":
        return [fish_python_dir(), torch_root, fish_source_dir()]
    return []


def _pip_installed(package: str) -> bool:
    _prepend_sys_paths(_component_python_roots(package))
    if package == "torch":
        return _torch_runtime_ready()
    module = package.replace("-", "_").replace(".", "_")
    for name in (module, package):
        try:
            if importlib.util.find_spec(name) is not None:
                return True
        except (ImportError, ValueError):
            continue
    return False


def _torch_runtime_ready() -> bool:
    import importlib.metadata as md

    _prepend_sys_paths(_component_python_roots("torch"))
    try:
        import torch  # noqa: F401
        import torchaudio  # noqa: F401
    except Exception:
        return False
    try:
        torch_minor = tuple(md.version("torch").split("+")[0].split(".")[:2])
        audio_minor = tuple(md.version("torchaudio").split("+")[0].split(".")[:2])
    except Exception:
        return False
    return torch_minor == audio_minor


def component_statuses() -> list[dict]:
    """工具箱清单：每项含 installed 与人类可读 detail（capability-check 口径）。"""
    result: list[dict] = []
    for item in COMPONENTS:
        entry = dict(item)
        if item["kind"] == "download":
            installed, detail = _download_status(item["key"])
            entry["installed"] = installed
            entry["detail"] = detail
        elif item["kind"] == "torch":
            ok, detail = _torch_status()
            entry["installed"] = ok
            entry["detail"] = detail
        elif item["kind"] == "dots":
            installed = _pip_installed("dots.tts")
            entry["installed"] = installed
            entry["detail"] = ("已安装（Windows 安全模式，已跳过 pynini/文本正则化）。"
                               if installed else "未安装。点「安装」（自动跳过 Windows 装不了的 pynini）。")
        elif item["kind"] == "pip":
            installed = _pip_installed(str(item["package"]))
            entry["installed"] = installed
            entry["detail"] = "已安装。" if installed else f"未安装（点击安装：pip install {item['package']}）。"
        else:  # fish_speech（install）
            stage, detail = fish_stage()
            entry["installed"] = stage in {"online", "startable"}
            entry["fish_stage"] = stage
            entry["detail"] = detail
        result.append(entry)
    return result


def install_component(key: str, log: LogFn) -> None:
    """执行下载/安装；进度与结果逐行写入 log。失败抛异常（信息面向用户）。"""
    item = next((c for c in COMPONENTS if c["key"] == key), None)
    if item is None:
        raise KeyError(f"未知组件：{key}")
    if item["kind"] == "install":
        _install_fish_speech(log)
        return
    if item["kind"] == "torch":
        _install_torch_cuda(log, key=key)
        return
    if item["kind"] == "dots":
        _install_dots_tts(log, key=key)
        return
    if item["kind"] == "pip":
        _pip_install(str(item["package"]), log, key=key)
        return
    if key == "whisper_cli":
        _download_whisper_runtime(log)
    else:
        _download_model(key, log)


def _download_status(key: str) -> tuple[bool, str]:
    """下载类组件状态：在设置的组件根下校验（SHA256）。"""
    root_note = f"保存位置：{studio_settings.component_root()}"
    if key == "whisper_cli":
        exe = studio_settings.whisper_cli_path()
        if exe:
            return True, f"就绪：{exe}"
        return False, f"缺少 whisper-cli（{root_note}）。"
    entry = registry_entry(key)
    path = studio_settings.whisper_models_dir() / str(entry["filename"])
    validation = validate_component_file(path, int(entry["size_bytes"]), str(entry["sha256"]))
    if validation.ok:
        return True, f"校验通过：{path}"
    if path.exists():
        return False, f"文件损坏（{validation.message}），请重新下载。{root_note}"
    return False, f"未下载（{root_note}）。"


def _manual_download_hint(entry: dict, target: Path, exc: Exception) -> str:
    """所有源都失败时的手动兜底话术：直连地址 + 存放路径 + 续走口径。"""
    url = list(entry["urls"])[0]
    return (f"所有下载源均失败（{exc}）。手动兜底：浏览器打开 {url} 下载，"
            f"把文件原名放入 {target.parent}，再点一次「下载」即自动校验/续装，不会重新下载。")


def _progress(log: LogFn):
    milestones = {25, 50, 75, 100}

    def callback(done: int, total: int | None) -> None:
        if not total:
            return
        percent = int(done * 100 / total)
        hit = {m for m in milestones if percent >= m}
        if hit:
            milestones.difference_update(hit)
            log(f"  下载进度 {percent}%（{done // (1024 * 1024)}MB / {total // (1024 * 1024)}MB）")

    return callback


def _gh_mirror_urls(urls: list[str]) -> list[str]:
    """GitHub 直连在部分网络不可达：为 github.com 链接生成镜像候选（镜像优先）。

    与 fonts.mirror_urls 同一套轮换池（2026-07 扩充到 6 家 + 直连），单点镜像失效不影响整体。"""
    from .fonts import mirror_urls

    expanded: list[str] = []
    for url in urls:
        expanded.extend(mirror_urls(url))
    return expanded


def _download_whisper_runtime(log: LogFn) -> None:
    entry = registry_entry("whisper_cli")
    if studio_settings.whisper_cli_path():
        log("✅ whisper-cli 已存在，无需重复下载。")
        return
    target = studio_settings.whisper_runtime_zip(str(entry["filename"]))
    log(f"开始下载 {entry['filename']}（GitHub+国内镜像轮询，SHA256 校验，断点续传）…")
    try:
        result = download_verified_file(
            _gh_mirror_urls(list(entry["urls"])), target, _progress(log),
            int(entry["size_bytes"]), str(entry["sha256"]),
        )
    except Exception as exc:
        raise RuntimeError(_manual_download_hint(entry, target, exc)) from exc
    log(f"  {result.message}")
    log("解压运行时…")
    with zipfile.ZipFile(target) as bundle:
        bundle.extractall(studio_settings.whisper_home())
    exe = studio_settings.whisper_cli_path()
    if not exe:
        home = studio_settings.whisper_home()
        top = "、".join(sorted(p.name for p in home.iterdir())[:8]) if home.is_dir() else "（空）"
        raise RuntimeError(
            f"解压完成但未找到 whisper-cli.exe。解压目录 {home} 顶层内容：{top}。"
            "请把此信息发给开发。")
    log(f"✅ whisper-cli 运行时就绪：{exe}")


def _download_model(key: str, log: LogFn) -> None:
    entry = registry_entry(key)
    target = studio_settings.whisper_models_dir() / str(entry["filename"])
    if validate_component_file(target, int(entry["size_bytes"]), str(entry["sha256"])).ok:
        log(f"✅ 模型 {entry['name']} 已存在且校验通过，无需重复下载。")
        return
    log(f"开始下载 {entry['filename']}（SHA256 校验，支持断点续传）…")
    try:
        result = download_verified_file(
            _gh_mirror_urls(list(entry["urls"])), target, _progress(log),
            int(entry["size_bytes"]), str(entry["sha256"]),
        )
    except Exception as exc:
        raise RuntimeError(_manual_download_hint(entry, target, exc)) from exc
    log(f"  {result.message}")
    validation = validate_component_file(target, int(entry["size_bytes"]), str(entry["sha256"]))
    if not validation.ok:
        raise RuntimeError(f"下载后校验未通过：{validation.message}")
    log(f"✅ 模型 {entry['name']} 就绪。")


# 国内 PyPI 镜像（直连 PyPI 在国内极慢，torch 等大包可卡数小时）——清华优先，阿里/中科大兜底
PIP_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
PIP_EXTRA = ["https://mirrors.aliyun.com/pypi/simple", "https://pypi.mirrors.ustc.edu.cn/simple",
             "https://pypi.org/simple"]


def _py() -> str:
    """跑 pip/子进程用的 Python：源码运行=自身；打包 exe=桥接到的系统 Python。

    exe 里的 sys.executable 是软件自己（没有 pip），直接用会静默失败——统一走这里。"""
    if getattr(sys, "frozen", False):
        from . import syspy

        found = syspy.system_python()
        if found is not None:
            return str(found)
    return sys.executable


def _pip_base_cmd() -> list[str]:
    """pip 安装基础命令：国内镜像 + 超时/重试 + 关进度条（日志更干净）。"""
    cmd = [_py(), "-m", "pip", "install",
           "-i", PIP_INDEX, "--timeout", "30", "--retries", "3", "--progress-bar", "off"]
    for extra in PIP_EXTRA:
        cmd += ["--extra-index-url", extra]
    return cmd


# dots.tts 运行依赖（对照 pyproject，去掉 torch/torchaudio 由 GPU 组件单独装；
# 去掉 WeTextProcessing —— 它靠 pynini，Windows 无法编译，且仅用于「可选、默认关」的文本正则化）
# 对照 dots.tts 官方 constraints/recommended.txt 锁版本：
# transformers 必须够新才有 Qwen2ForCausalLM（dots.tts 模型基于 Qwen2 架构，旧版报
# "Could not import module 'Qwen2ForCausalLM'"）；accelerate 供大模型加载。torch/torchaudio
# 由「PyTorch GPU 版」组件单独装，此处不含以免覆盖 CUDA 版。
DOTS_RUNTIME_DEPS = [
    "transformers==4.57.0", "accelerate==1.12.0",
    "huggingface-hub", "loguru", "langcodes[data]", "gradio",
    "einops", "librosa", "soundfile", "numpy", "pydantic", "PyYAML",
    "safetensors", "torchdiffeq", "tqdm", "lingua-language-detector",
]


def _install_dots_tts(log: LogFn, key: str | None = None) -> None:
    """Windows 安全装 dots.tts：本体 --no-deps + 手动补运行依赖，绕开 WeTextProcessing/pynini。

    pynini 在 Windows 编译不了（OpenFST，MSVC 不认其 GCC 编译参数）；而它只服务于
    dots.tts 的文本正则化（normalize_text 默认 False、懒加载），跳过不影响配音出声。
    """
    log("安装 dots.tts（Windows 安全模式：跳过 WeTextProcessing/pynini —— 仅用于可选文本正则化，"
        "Windows 编译不了，默认本就关闭，不影响配音）…")
    _stream_command(_pip_base_cmd() + ["--no-deps", "dots.tts"], log, key=key,
                    error="dots.tts 本体安装失败")
    log("补齐运行依赖（不含 torch —— 由「PyTorch GPU 版」提供，避免覆盖 CUDA 版）…")
    _stream_command(_pip_base_cmd() + DOTS_RUNTIME_DEPS, log, key=key,
                    error="dots.tts 运行依赖安装失败")
    if not _pip_installed("dots.tts"):
        raise RuntimeError("安装完成但未检测到 dots.tts，请重试。")
    log("✅ dots.tts 安装完成（已跳过 pynini；文本正则化默认关闭，不影响配音）。")


def _torch_status() -> tuple[bool, str]:
    """PyTorch GPU 版状态：torch 可导入且 CUDA 可用才算就绪。"""
    _prepend_sys_paths(_component_python_roots("torch"))
    try:
        torch = importlib.import_module("torch")
    except ImportError:
        return False, "未安装。点「安装」自动装 CUDA 12.1 版 torch（RTX 20/30/40 系适用）。"
    try:
        torchaudio = importlib.import_module("torchaudio")
    except ImportError:
        ver = getattr(torch, "__version__", "?")
        return False, f"torch {ver} 已装但缺 torchaudio。点「安装」补齐匹配的 torch+torchaudio。"
    ver = getattr(torch, "__version__", "?")
    ta = getattr(torchaudio, "__version__", "?")
    # torch 与 torchaudio 必须同小版本，否则 dots.tts 启动即报错
    t_minor = ".".join(ver.split("+")[0].split(".")[:2])
    a_minor = ".".join(ta.split("+")[0].split(".")[:2])
    if t_minor != a_minor:
        return (False, f"torch {ver} 与 torchaudio {ta} 版本不匹配（dots.tts 要求一致）。"
                       "点「安装」重装匹配对即可修复。")
    try:
        if torch.cuda.is_available():
            return True, f"已就绪：torch {ver} + torchaudio {ta}，GPU {torch.cuda.get_device_name(0)}。"
        return (False, f"torch {ver} 已装但为 CPU 版（CUDA 不可用）。点「安装」换装 CUDA 版，"
                       "或确认已装 NVIDIA 驱动。")
    except Exception as exc:
        return False, f"torch {ver} 已装但 CUDA 检测异常：{exc}"


def _installed_dist_version(package: str) -> str:
    """在子进程里读刚装好的版本（去掉 +cuXXX 本地标签），避开当前进程的导入缓存。"""
    try:
        out = subprocess.check_output(
            [_py(), "-c", f"import importlib.metadata as m;print(m.version({package!r}))"],
            text=True, stderr=subprocess.DEVNULL).strip()
        return out.split("+")[0]
    except Exception:
        return ""


def _install_torch_cuda(log: LogFn, key: str | None = None) -> None:
    """安装/换装 CUDA 12.1 版 torch + torchaudio（官方源，约 2.5GB）。

    关键：torchaudio 往往滞后 torch 一两个小版本，且不硬性锁 torch —— 放任 pip 各取
    最新会得到 torch 2.13 + torchaudio 2.11 这类不匹配（dots.tts 启动即报
    "minor versions do not match"）。故分两步：先装 torchaudio（最矮的板决定可用档位），
    再把 torch 锁到「与 torchaudio 完全相同的版本」，保证小版本一致。
    """
    index = ["--index-url", TORCH_CUDA_INDEX, "--timeout", "60", "--retries", "3", "--progress-bar", "off"]
    log("安装 PyTorch GPU 版（CUDA 12.1，官方源，约 2.5GB，请耐心）…")
    log("① 安装 torchaudio（它决定可匹配的 torch 版本）…")
    _stream_command([_py(), "-m", "pip", "install", "--force-reinstall", "torchaudio"] + index,
                    log, key=key, error="torchaudio 安装失败")
    ta_ver = _installed_dist_version("torchaudio")
    if ta_ver:
        log(f"② 将 torch 锁到与 torchaudio 相同的版本 {ta_ver}（避免 torch 被拉高造成不匹配）…")
        _stream_command([_py(), "-m", "pip", "install", "--force-reinstall", f"torch=={ta_ver}"] + index,
                        log, key=key,
                        error=f"torch=={ta_ver} 安装失败。可手动执行：pip install --force-reinstall torch=={ta_ver} torchaudio=={ta_ver} --index-url " + TORCH_CUDA_INDEX)
    _cleanup_torch_stragglers(log, key=key)
    ok, detail = _torch_status()
    if not ok:
        raise RuntimeError("安装完成但 CUDA 仍不可用：" + detail + " 请确认已装 NVIDIA 显卡驱动后重启软件。")
    log("✅ " + detail)


# 与 torch 小版本 ABI 锁死的生态包：torch 换版本后其 DLL 立即失配，Windows 弹
# 「无法定位程序输入点 aoti_torch_device_type_cuda」等错框（用户 2026-07-24 实测
# torchcodec 按 torch 2.13 编译、torch 回 2.11 后弹窗）。本软件不用它们 → 装完直接清掉。
_TORCH_ABI_STRAGGLERS = ["torchcodec"]


def _cleanup_torch_stragglers(log: LogFn, key: str | None = None) -> None:
    for package in _TORCH_ABI_STRAGGLERS:
        if not _installed_dist_version(package):
            continue
        log(f"③ 清理 {package}（其 DLL 锁死旧 torch 版本，torch 换版后必弹「无法定位程序输入点」；本软件不需要它）…")
        try:
            _stream_command([_py(), "-m", "pip", "uninstall", "-y", package],
                            log, key=key, error=f"{package} 卸载失败")
        except RuntimeError as exc:
            log(f"⚠ {exc} —— 可手动执行：pip uninstall -y {package}")


def _pip_install(package: str, log: LogFn, key: str | None = None) -> None:
    log(f"pip install {package} …（国内镜像加速：{PIP_INDEX}）")
    _stream_command(_pip_base_cmd() + [package], log, key=key,
                    error=f"pip 安装失败。请检查网络，或手动执行：pip install -i {PIP_INDEX} {package}")
    if not _pip_installed(package):
        raise RuntimeError(f"安装完成但导入检测未通过，请重启软件后再试（pip install {package}）。")
    log(f"✅ {package} 安装完成。")


# 运行中的子进程登记表：供「停止」掐断卡住的 pip/依赖安装
_ACTIVE_PROCS: dict[str, subprocess.Popen] = {}
_ACTIVE_LOCK = threading.Lock()


def stop_component(key: str, log: LogFn) -> None:
    """掐断某组件正在运行的安装子进程（pip/依赖）。下载类无独立子进程，提示改用镜像重试。"""
    with _ACTIVE_LOCK:
        proc = _ACTIVE_PROCS.get(key)
    if proc is None or proc.poll() is not None:
        log(f"「{key}」当前没有正在运行的安装进程（可能是下载类，或已结束）。")
        return
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
    log(f"✅ 已停止「{key}」的安装。可重新点击安装（走国内镜像会快很多）。")


def _stream_command(cmd: list[str], log: LogFn, error: str, cwd: Path | None = None,
                    env: dict | None = None, key: str | None = None) -> None:
    """子进程输出逐行进日志；非零退出码抛用户可读错误。key 非空时登记以便「停止」掐断。"""
    process = subprocess.Popen(
        cmd, cwd=str(cwd) if cwd else None, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    if key:
        with _ACTIVE_LOCK:
            _ACTIVE_PROCS[key] = process
    try:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip()
            if line:
                log("  " + line[-160:])
        code = process.wait()
    finally:
        if key:
            with _ACTIVE_LOCK:
                _ACTIVE_PROCS.pop(key, None)
    if code != 0:
        raise RuntimeError(f"{error}（退出码 {code}）")


# ------------------------------------------------------------------ fish-speech 一键安装 + 服务管理
def fish_source_dir() -> Path:
    return studio_settings.components_root() / "fish-speech"


def fish_python_dir() -> Path:
    return fish_source_dir() / "python"


def fish_python_runtime() -> Path:
    return fish_source_dir() / "python-runtime" / "python.exe"


def fish_checkpoints_dir() -> Path:
    return fish_source_dir() / "checkpoints" / FISH_MODEL_REPO.rsplit("/", 1)[-1]


def _fish_source_ready() -> bool:
    return (fish_source_dir() / "pyproject.toml").exists()


def _fish_deps_ready() -> bool:
    root = studio_settings.components_root()
    required = [
        fish_python_runtime(),
        fish_python_dir() / "uvicorn",
        fish_python_dir() / "kui",
        fish_python_dir() / "ormsgpack",
        fish_python_dir() / "transformers",
        fish_python_dir() / "librosa",
        fish_python_dir() / "audiotools",
        fish_python_dir() / "pytorch_lightning",
        fish_python_dir() / "pydantic",
        fish_python_dir() / "safetensors",
        root / "torch" / "python" / "torch",
        root / "torch" / "python" / "torchaudio",
        root / "torch" / "python" / "torio",
    ]
    return _fish_source_ready() and all(path.exists() for path in required)


def _fish_model_ready() -> bool:
    ckpt = fish_checkpoints_dir()
    return ckpt.is_dir() and any(p.suffix in {".pth", ".safetensors"} for p in ckpt.iterdir())


def fish_stage() -> tuple[str, str]:
    """fish-speech 安装阶段：online / startable / model / deps / source / none + 用户可读说明。"""
    from .engines import FishLocalEngine

    probe = FishLocalEngine().probe()
    if probe.available:
        return "online", probe.detail
    if _fish_source_ready() and _fish_deps_ready() and _fish_model_ready():
        return "startable", "已安装（源码+依赖+模型），点「启动服务」即可使用。"
    if _fish_source_ready() and _fish_deps_ready():
        return "model", "源码与依赖已装，缺模型 —— 点「安装」续装（断点续装，不重复下载）。"
    if _fish_source_ready():
        return "deps", "源码已就位，缺 Python 依赖 —— 点「安装」续装。"
    return "none", "未安装。点「安装」自动完成：源码（GitHub+镜像）→ pip 依赖 → 模型（HF 镜像）。"


def _install_fish_speech(log: LogFn) -> None:
    """分阶段一键安装：已完成的阶段自动跳过，失败从断点续装。"""
    src = fish_source_dir()
    # ① 源码
    if _fish_source_ready():
        log(f"✅ 源码已存在：{src}（跳过下载）")
    else:
        log("① 下载 fish-speech 源码（GitHub + 国内镜像轮询）…")
        bundle = studio_settings.components_root() / "fish-speech-main.zip"
        result = download_verified_file(_gh_mirror_urls(FISH_SOURCE_URLS), bundle, _progress(log))
        log(f"  {result.message}")
        log("  解压源码…")
        temp = studio_settings.components_root() / "_fish_unzip"
        shutil.rmtree(temp, ignore_errors=True)
        with zipfile.ZipFile(bundle) as pack:
            pack.extractall(temp)
        inner = next((p for p in temp.iterdir() if p.is_dir() and (p / "pyproject.toml").exists()), None)
        if inner is None:
            raise RuntimeError("源码包结构不对（未找到 pyproject.toml），请重试或换网络。")
        shutil.rmtree(src, ignore_errors=True)
        shutil.move(str(inner), str(src))
        shutil.rmtree(temp, ignore_errors=True)
        bundle.unlink(missing_ok=True)
        log(f"✅ 源码就位：{src}")
    # ② Python 依赖
    if _fish_deps_ready():
        log("✅ Python 依赖已安装（跳过）。")
    else:
        log(f"② 安装 Python 依赖（国内镜像 {PIP_INDEX}，含 torch 体量大请耐心）…")
        _stream_command(_pip_base_cmd() + ["-e", str(src)], log, key="fish_speech",
                        error="fish-speech 依赖安装失败。可手动执行：pip install -i " + PIP_INDEX + " -e " + str(src))
        log("✅ 依赖安装完成。GPU 加速需自行安装 CUDA 版 torch（pytorch.org 选择对应命令）。")
    # ③ 模型
    if _fish_model_ready():
        log("✅ 模型已存在（跳过下载）。")
    else:
        log(f"③ 下载模型 {FISH_MODEL_REPO}（HF 镜像 {os.environ.get('HF_ENDPOINT', _HF_MIRROR)}）…")
        env = dict(os.environ)
        env.setdefault("HF_ENDPOINT", _HF_MIRROR)
        env.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        script = (
            "from huggingface_hub import snapshot_download;"
            f"snapshot_download(repo_id={FISH_MODEL_REPO!r}, local_dir={str(fish_checkpoints_dir())!r})"
        )
        try:
            _stream_command([_py(), "-c", script], log, env=env, key="fish_speech",
                            error="模型下载失败")
        except RuntimeError as exc:
            raise RuntimeError(
                f"{exc} —— 可手动下载：huggingface-cli download {FISH_MODEL_REPO} "
                f"--local-dir \"{fish_checkpoints_dir()}\"（国内可先 set HF_ENDPOINT={_HF_MIRROR}），"
                "完成后回到工具箱点「启动服务」。") from exc
        if not _fish_model_ready():
            raise RuntimeError("模型下载完成但未找到权重文件，请重试或手动核对 checkpoints 目录。")
        log("✅ 模型就绪。")
    log("✅ fish-speech 安装完成，正在自动启动本地服务（首次加载大模型较慢，请稍候）…")
    try:
        start_fish_server(log)  # 装完自动起服务并探活，直接嫁接进配音引擎，无需再手点「启动服务」
    except Exception as exc:  # noqa: BLE001 —— 自动启动失败不算安装失败，给手动兜底
        log(f"⚠ 自动启动未成功（{exc}）。可到工具箱点「启动服务」重试；装好的源码/依赖/模型不受影响。")


_FISH_PROC: subprocess.Popen | None = None
_FISH_LOCK = threading.Lock()


def fish_server_running() -> bool:
    with _FISH_LOCK:
        return _FISH_PROC is not None and _FISH_PROC.poll() is None


def start_fish_server(log: LogFn, wait_seconds: float = 90.0) -> None:
    """启动本地 fish-speech server（127.0.0.1:8080），探活成功即返回，进程留在后台。"""
    global _FISH_PROC
    from .engines import FishLocalEngine

    if FishLocalEngine().probe().available:
        log("✅ fish-speech 服务已在线，无需再次启动。")
        return
    stage, detail = fish_stage()
    if stage in {"none", "source", "deps", "model"}:
        raise RuntimeError(f"还不能启动：{detail}")
    src = fish_source_dir()
    python = fish_python_runtime() if fish_python_runtime().exists() else Path(_py())
    cmd = [str(python), "-m", "tools.api_server", "--listen", FISH_SERVER_ADDR]
    ckpt = fish_checkpoints_dir()
    decoder = next((p for p in sorted(ckpt.glob("firefly*generator*.pth"))), None) if ckpt.is_dir() else None
    if ckpt.is_dir():
        cmd += ["--llama-checkpoint-path", str(ckpt)]
    if decoder is not None:
        cmd += ["--decoder-checkpoint-path", str(decoder), "--decoder-config-name", "firefly_gan_vq"]
    log("启动命令：" + " ".join(cmd))
    with _FISH_LOCK:
        _FISH_PROC = subprocess.Popen(
            cmd, cwd=str(src), env=_fish_subprocess_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        proc = _FISH_PROC
    tail: list[str] = []

    def _pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                tail.append(line[-160:])
                del tail[:-30]
                log("  " + line[-160:])

    threading.Thread(target=_pump, daemon=True).start()
    import time

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("fish-speech 服务启动即退出（见上方日志）。常见原因：模型缺失、显存不足、torch 未装好。")
        if FishLocalEngine().probe().available:
            log(f"✅ fish-speech 服务在线：http://{FISH_SERVER_ADDR}（关软件前可在工具箱停止）")
            return
        time.sleep(2)
    log("⚠ 服务仍在加载中（大模型首次加载较慢）；就绪后「探测组件」会显示在线。")


def stop_fish_server(log: LogFn) -> None:
    global _FISH_PROC
    with _FISH_LOCK:
        proc = _FISH_PROC
        _FISH_PROC = None
    if proc is None or proc.poll() is not None:
        log("fish-speech 服务本就未由本软件托管运行。")
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log("✅ fish-speech 服务已停止。")


def _fish_subprocess_env() -> dict:
    env = dict(os.environ)
    paths = [fish_python_dir(), studio_settings.components_root() / "torch" / "python", fish_source_dir()]
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(str(path) for path in paths if path.exists()) + (
        os.pathsep + existing if existing else ""
    )
    env["PYTHONNOUSERSITE"] = "1"
    env["PATH"] = str(fish_python_runtime().parent) + os.pathsep + env.get("PATH", "")
    return env

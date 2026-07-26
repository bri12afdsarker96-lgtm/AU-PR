# -*- coding: utf-8 -*-
"""环境快照：把本机目录结构 + Python 环境 + 组件状态 + dots.tts 真实 API + 音色参考体检
抓成一个文本文件（环境快照.txt）。

用途：配合 环境快照.bat 一键生成并推送到 GitHub，开发者直接读文件即可看到本机真实运行
状态（目录/版本/依赖/引擎探测/generate 真实签名/参考音频时长），用来精确定位到「哪一环
的代码报错」，不用来回截图。纯标准库，任何一步失败都只记录错误、不中断。

2026-07-26 加强：
  · 子进程统一 UTF-8（PYTHONUTF8/IO），修中文乱码；
  · git 不在 PATH 时读 .git/HEAD 兜底，不再整片 WinError 2；
  · 新增「dots.tts 运行时 API 详解」：不加载 2B 权重，只反射 from_pretrained/generate 真实
    签名 + 全包 grep max_generate_length 的定义位置与默认值 —— 定位长参考该往哪塞；
  · 新增「音色库参考音频体检」：逐条量参考时长/采样率/大小，超 30s 直接标红（dots.tts 只吃
    5–30s，过长即触发 max_generate_length 断言并劣化克隆）；
  · 新增「近期错误日志」：收集 启动错误.log 等，把最后一次真实报错带出来。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

OUT = Path(__file__).resolve().parent / "环境快照.txt"
ROOT = Path(__file__).resolve().parent
L: list[str] = []

# 子进程强制 UTF-8：否则本机控制台默认 GBK，中文经管道被当 UTF-8 解出乱码（旧快照第 23 行）。
_UTF8_ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def sec(title: str) -> None:
    L.append("")
    L.append("═" * 8 + " " + title + " " + "═" * 8)


def run(cmd: list[str], timeout: int = 30) -> str:
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT,
                                       timeout=timeout, encoding="utf-8", errors="replace",
                                       env=_UTF8_ENV).strip()
    except Exception as exc:  # noqa: BLE001
        return f"<失败: {exc}>"


def py(code: str, timeout: int = 120) -> str:
    """在 sys.executable 里跑一段代码（UTF-8 管道），用于反射组件真实 API。"""
    return run([sys.executable, "-c", code], timeout=timeout)


def tree(path: Path, depth: int = 2, max_entries: int = 40, exclude: set | None = None) -> None:
    exclude = exclude or set()
    if not path.exists():
        L.append(f"  （不存在）{path}")
        return

    def walk(p: Path, level: int) -> None:
        if level > depth:
            return
        try:
            entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
        except OSError as exc:
            L.append("  " * level + f"<无法读取: {exc}>")
            return
        shown = 0
        for e in entries:
            if e.name in exclude or e.name.startswith("."):
                continue
            if shown >= max_entries:
                L.append("  " * level + f"…（其余 {len(entries) - shown} 项略）")
                break
            shown += 1
            if e.is_dir():
                L.append("  " * level + f"📁 {e.name}/")
                walk(e, level + 1)
            else:
                try:
                    mb = e.stat().st_size / 1048576
                    stamp = datetime.fromtimestamp(e.stat().st_mtime).strftime("%m-%d %H:%M")
                    L.append("  " * level + f"   {e.name}  ({mb:.1f}MB, {stamp})")
                except OSError:
                    L.append("  " * level + f"   {e.name}")

    L.append(f"  {path}")
    walk(path, 1)


# ------------------------------------------------------------------ git 兜底（无 git 二进制）
def _git_state_fallback() -> list[str]:
    """git 不在 PATH 时，直接读 .git 纯文本，至少把分支/HEAD 带出来。"""
    gitdir = ROOT / ".git"
    if not gitdir.is_dir():
        return ["  <无 .git 目录，且 git 不在 PATH>"]
    out: list[str] = []
    try:
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            out.append(f"  分支: {ref.split('/')[-1]}（读自 .git/HEAD，git 不在 PATH）")
            sha = ""
            loose = gitdir / ref
            if loose.is_file():
                sha = loose.read_text(encoding="utf-8").strip()
            else:  # packed-refs
                packed = gitdir / "packed-refs"
                if packed.is_file():
                    for ln in packed.read_text(encoding="utf-8").splitlines():
                        if ln.endswith(ref):
                            sha = ln.split()[0]
                            break
            out.append(f"  HEAD: {sha or '<未找到 ref 对应 sha>'}")
        else:
            out.append(f"  HEAD(detached): {head}")
    except Exception as exc:  # noqa: BLE001
        out.append(f"  <读取 .git 失败: {exc}>")
    out.append("  ⚠ git 不在 PATH：无法列近 5 次提交/未提交改动。把 Git 加入系统 PATH 可获完整信息。")
    return out


# ------------------------------------------------------------------ dots.tts 真实 API 反射
_DOTS_API_PROBE = r"""
import sys, os, inspect, importlib
sys.path.insert(0, 'source')
try:
    from dub_align_studio.engines import dots_local as d
    d._ensure_component_python_path()
    d._ensure_tn_stub()
except Exception as e:
    print('准备 dots.tts 环境失败:', repr(e)); raise SystemExit(0)

try:
    mod = importlib.import_module('dots_tts.runtime')
except Exception as e:
    print('导入 dots_tts.runtime 失败:', repr(e)); raise SystemExit(0)

R = getattr(mod, 'DotsTtsRuntime', None)
if R is None:
    print('dots_tts.runtime 里没有 DotsTtsRuntime'); raise SystemExit(0)

def sig(fn, name):
    try:
        print(f'{name}: {inspect.signature(fn)}')
    except Exception as e:
        print(f'{name}: <取签名失败 {e!r}>')

sig(getattr(R, 'from_pretrained', None), 'from_pretrained')
sig(getattr(R, 'generate', None), 'generate')

# generate 到底接受哪些我们关心的参数（决定 max_generate_length 能否直接塞）
try:
    gsig = inspect.signature(R.generate)
    params = gsig.parameters
    var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    print('generate 接受 **kwargs:', var_kw)
    for k in ('text','prompt_audio_path','prompt_text','num_steps','guidance_scale',
              'seed','normalize_text','max_generate_length','speed','max_pause','length'):
        print(f'  接受 {k}: {var_kw or (k in params)}')
except Exception as e:
    print('分析 generate 参数失败:', repr(e))

# 全包 grep max_generate_length：定位它真正的入口/默认值（该往哪塞就看这里）
try:
    pkg_dir = os.path.dirname(importlib.import_module('dots_tts').__file__)
    hits = []
    for root, _, files in os.walk(pkg_dir):
        for f in files:
            if not f.endswith('.py'):
                continue
            p = os.path.join(root, f)
            try:
                with open(p, encoding='utf-8', errors='replace') as h:
                    for i, line in enumerate(h, 1):
                        if 'max_generate_length' in line:
                            rel = os.path.relpath(p, pkg_dir)
                            hits.append(f'{rel}:{i}: {line.strip()[:150]}')
            except Exception:
                pass
    print(f'max_generate_length 在 dots_tts 包内出现 {len(hits)} 处：')
    for hpath in hits[:50]:
        print('  ' + hpath)
    if not hits:
        print('  <包内未出现——可能来自更底层依赖或 C 扩展>')
except Exception as e:
    print('grep max_generate_length 失败:', repr(e))
"""


# ------------------------------------------------------------------ 参考音频体检
def _audio_info(path: Path) -> str:
    """量音频时长/采样率/大小。soundfile 0.14 的 libsndfile 可读 wav/mp3；失败再退 ffprobe。"""
    size_mb = path.stat().st_size / 1048576 if path.exists() else 0.0
    dur = sr = None
    try:
        import soundfile as sf  # 本机已装 0.14.0
        info = sf.info(str(path))
        dur, sr = info.duration, info.samplerate
    except Exception:
        # 退 ffprobe（可能在数据目录/父目录，不一定在 PATH）
        import shutil as _sh
        exe = _sh.which("ffprobe")
        if not exe:
            for cand in (ROOT.parent / "ffprobe.exe",
                         ROOT / "dist" / "水星配音对齐工作室" / "_internal" / "ffprobe.exe"):
                if cand.exists():
                    exe = str(cand)
                    break
        if exe:
            out = run([exe, "-v", "error", "-show_entries", "format=duration",
                       "-of", "default=nw=1:nk=1", str(path)])
            try:
                dur = float(out)
            except Exception:
                pass
    if dur is None:
        return f"{size_mb:.1f}MB · 时长未知（soundfile/ffprobe 都读不出）"
    # dots.tts 断言里 500 对应内置默认；870 patch ≈ 用户那条太长的参考。粗估 patch≈时长×(870/参考秒)
    # 不同参考编码率不同，这里只给「是否超 30s」的红线判断，精确 patch 数以 API 探测为准。
    flag = "  ⚠ 超 30s：dots.tts 只吃 5–30s，过长会触发 max_generate_length 断言且劣化克隆" if dur > 30 else ""
    srtxt = f"{sr}Hz" if sr else "采样率未知"
    return f"{size_mb:.1f}MB · 时长 {dur:.1f}s · {srtxt}{flag}"


def _voice_checkup(data_dir: Path) -> None:
    vroot = data_dir / "音色库"
    if not vroot.is_dir():
        L.append(f"  （无音色库目录）{vroot}")
        return
    names = ("参考音频.mp3", "参考音频.wav", "参考音频.m4a", "参考.wav", "参考.mp3")
    found = False
    for d in sorted(vroot.iterdir()):
        if not d.is_dir():
            continue
        ref = next((d / n for n in names if (d / n).exists()), None)
        if ref is None:
            # 兜底：目录里任何音频
            ref = next((p for p in d.iterdir()
                        if p.suffix.lower() in (".mp3", ".wav", ".m4a", ".flac")), None)
        found = True
        if ref is None:
            L.append(f"  📁 {d.name}: <未找到参考音频>")
        else:
            L.append(f"  📁 {d.name}/{ref.name}: {_audio_info(ref)}")
    if not found:
        L.append("  （音色库为空）")


# ------------------------------------------------------------------ 近期错误日志
def _recent_errors() -> None:
    candidates = [
        ROOT / "启动错误.log",
        ROOT / "dist" / "水星配音对齐工作室" / "启动错误.log",
        ROOT / "本次操作日志.txt",
    ]
    any_hit = False
    for c in candidates:
        if c.is_file() and c.stat().st_size > 0:
            any_hit = True
            stamp = datetime.fromtimestamp(c.stat().st_mtime).strftime("%m-%d %H:%M")
            L.append(f"  {c.name}（{stamp}，尾部 40 行）:")
            try:
                tail = c.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
                for ln in tail:
                    L.append("    " + ln)
            except Exception as exc:  # noqa: BLE001
                L.append(f"    <读取失败: {exc}>")
    if not any_hit:
        L.append("  （无 启动错误.log 等错误日志——启动阶段无异常）")


def main() -> int:
    L.append(f"水星配音对齐工作室 · 环境快照  {datetime.now():%Y-%m-%d %H:%M:%S}")

    sec("Python 环境")
    L.append(f"  python: {sys.executable}")
    L.append(f"  版本: {sys.version.split()[0]}")
    L.append(f"  frozen(打包版): {getattr(sys, 'frozen', False)}")
    for pkg in ("torch", "torchaudio", "torchvision", "torchcodec",
                "transformers", "accelerate", "dots-tts", "soundfile", "pyinstaller"):
        info = run([sys.executable, "-m", "pip", "show", pkg])
        ver = next((ln.split(":", 1)[1].strip() for ln in info.splitlines()
                    if ln.startswith("Version:")), None)
        L.append(f"  {pkg}: {ver or '未安装'}")

    sec("引擎探测（dots.tts probe）")
    probe = py("import sys;sys.path.insert(0,'source');"
               "from dub_align_studio.engines.dots_local import DotsLocalEngine as E;"
               "s=E().probe();print('available=',s.available);print(s.detail)")
    L.append("  " + probe.replace("\n", "\n  "))

    sec("dots.tts 运行时 API 详解（不加载权重，只反射签名）")
    api = py(_DOTS_API_PROBE, timeout=180)
    L.append("  " + api.replace("\n", "\n  "))

    sec("音色库参考音频体检（定位过长参考）")
    try:
        sys.path.insert(0, str(ROOT / "source"))
        from dub_align_studio.settings import data_root
        _voice_checkup(Path(data_root()))
    except Exception as exc:  # noqa: BLE001
        L.append(f"  <体检失败: {exc}>")

    sec("仓库状态")
    import shutil as _sh
    if _sh.which("git"):
        L.append("  分支: " + run(["git", "rev-parse", "--abbrev-ref", "HEAD"]))
        L.append("  HEAD: " + run(["git", "log", "--oneline", "-1"]))
        L.append("  近5次提交:")
        L.append("    " + run(["git", "log", "--oneline", "-5"]).replace("\n", "\n    "))
        status = run(["git", "status", "--short"])
        L.append("  未提交改动: " + ("（干净）" if not status else "\n    " + status.replace("\n", "\n    ")))
    else:
        L.extend(_git_state_fallback())

    sec("近期错误日志")
    _recent_errors()

    sec("仓库根目录（深度1）")
    tree(ROOT, depth=1, exclude={"git", ".git", "build", "__pycache__"})

    sec("父目录（检查散落的旧脚本副本）")
    tree(ROOT.parent, depth=1, max_entries=30)

    sec("dist 产物")
    tree(ROOT / "dist", depth=2, max_entries=15)

    sec("发布包")
    tree(ROOT / "发布包", depth=1, max_entries=15)

    sec("整合离线包")
    tree(ROOT / "整合离线包", depth=2, max_entries=15)

    sec("数据总目录")
    data_dir = None
    try:
        from dub_align_studio.settings import data_root
        data_dir = data_root()
        L.append(f"  软件当前指向: {data_dir}")
    except Exception as exc:  # noqa: BLE001
        L.append(f"  <读取失败: {exc}>")
    if data_dir:
        tree(Path(data_dir), depth=3, max_entries=25, exclude={"__pycache__"})

    OUT.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"[OK] 快照已生成：{OUT}（{OUT.stat().st_size // 1024}KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""配音对齐工作室——浏览器版界面的本地后端（纯标准库，零新依赖）。

沿用水星 api_server 的口径：http.server 薄封装、默认只绑 127.0.0.1、
长任务异步 + 轮询。本服务是**本机桌面 UI 的后端**（非对外 API），
浏览器页面（web/index.html）与其同源提供。

端点：
    GET  /                     → 界面（web/index.html）
    GET  /api/state            → 组件探测 + 音色列表 + 常量（引擎/尺子/位置预设）
    POST /api/voices?name=&transcript=&filename= → 原始音频字节上传并登记音色
    DELETE /api/voices/<id>    → 删除音色
    POST /api/run              → {action: run_all|dub|timing|render|capcut, ...} 开任务
    GET  /api/job              → {running, done, ok, log:[...]} 轮询
"""

from __future__ import annotations

import json
import tempfile
import threading
import traceback
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import components as toolbox
from . import studio_pipeline as pipeline
from . import voice_library
from .aligners import WhisperAligner
from .engines import DotsLocalEngine, FishLocalEngine, MockEngine, SynthesisOptions
from .overlays import POSITION_PRESETS, overlays_from_dicts
from .subtitles import SubtitleStyle

STUDIO_HOME = Path.home() / ".dub_align_studio"
DEFAULT_PORT = 8760
def _index_path() -> Path:
    """界面文件：PyInstaller 冻结包内落在 _MEIPASS/dub_align_studio/web。"""
    import sys

    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidate = base / "dub_align_studio" / "web" / "index.html"
        if candidate.exists():
            return candidate
    return Path(__file__).parent / "web" / "index.html"


_INDEX = _index_path()
_MAX_UPLOAD = 200 * 1024 * 1024  # 参考音频上限 200MB，足够富余


# ------------------------------------------------------------------ 任务状态
@dataclass
class JobState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    running: bool = False
    done: bool = False
    ok: bool = False
    action: str = ""
    log: list[str] = field(default_factory=list)
    timings: list | None = None
    result: object | None = None

    def snapshot(self) -> dict:
        with self.lock:
            timings = [
                {"index": t.index, "text": t.text, "duration": t.duration}
                for t in (self.timings or [])
            ]
            return {"running": self.running, "done": self.done, "ok": self.ok,
                    "action": self.action, "log": list(self.log), "timings": timings}

    def append(self, message: str) -> None:
        with self.lock:
            self.log.append(message)


JOB = JobState()


def _run_job(action: str, payload: dict) -> None:
    log = JOB.append
    try:
        text = str(payload.get("text") or "")
        output_dir = Path(str(payload.get("output_dir") or "")) if payload.get("output_dir") else None
        engine_key = str(payload.get("engine") or "mock")
        aligner_key = str(payload.get("aligner") or "均分兜底")
        options = SynthesisOptions(
            speed=float(payload.get("speed") or 1.0),
            max_pause_seconds=float(payload.get("max_pause") or 0.0),
            seed=int(payload.get("seed") or 42),
        )
        voice = None
        if payload.get("voice_id"):
            voice = voice_library.get_voice(STUDIO_HOME, str(payload["voice_id"])).to_ref()
        style = None
        if payload.get("burn_subtitles", True):
            style = SubtitleStyle(font_size_px=int(payload.get("subtitle_size") or 64))
        overlays = overlays_from_dicts(payload.get("overlays") or [])
        aspect = str(payload.get("aspect") or pipeline.DEFAULT_ASPECT)
        config = pipeline.make_render_config(aspect)
        canvas = (config.width, config.height)

        if action == "run_all":
            run = pipeline.run_all(
                text, engine_key, aligner_key,
                Path(str(payload.get("shots_dir") or "")), output_dir,
                voice=voice, options=options, subtitle_style=style,
                export_capcut=bool(payload.get("export_capcut")), overlays=overlays,
                config=config,
            )
            for note in run.notes:
                log(f"⚠ {note}")
            for shot in run.result.shots:
                log(f"  {shot.index:>2} | 音频{shot.target_seconds:>6.2f}s | {shot.strategy}")
            log(f"字幕/文本框：{run.result.subtitle_note or '未启用'}")
            if run.capcut:
                log(f"剪映：{run.capcut.message}")
            log(("✅ 成片完成：" if run.ok else "❌ 收口断言未通过：") + str(run.result.output_path))
            with JOB.lock:
                JOB.timings, JOB.result, JOB.ok = run.timings, run.result, run.ok
        elif action == "dub":
            master = pipeline.step_dub(text, engine_key, output_dir, voice, options)
            log(f"✅ master：{master.path.name}（{master.seconds:.2f}s，引擎 {master.engine}）")
            with JOB.lock:
                JOB.ok = True
        elif action == "timing":
            timings, notes = pipeline.step_timing(text, output_dir / pipeline.MASTER_NAME,
                                                  aligner_key, output_dir)
            for note in notes:
                log(f"⚠ {note}")
            for item in timings:
                log(f"  {item.index:>2} | {item.duration:>6.2f}s | {item.text[:18]}")
            log("✅ 计时表已写入输出目录。")
            with JOB.lock:
                JOB.timings, JOB.ok = timings, True
        elif action == "render":
            timings = JOB.timings or pipeline.load_timings(output_dir)
            videos = pipeline.list_shot_videos(Path(str(payload.get("shots_dir") or "")))[: len(timings)]
            result = pipeline.step_render(output_dir / pipeline.MASTER_NAME, timings, videos,
                                          output_dir, style, config=config, overlays=overlays)
            log(f"字幕/文本框：{result.subtitle_note or '未启用'}")
            log(("✅ 成片完成：" if result.ok else "❌ 收口断言未通过：") + str(result.output_path))
            with JOB.lock:
                JOB.result, JOB.ok = result, result.ok
        elif action == "capcut":
            timings = JOB.timings or pipeline.load_timings(output_dir)
            # JOB.result 为 None（软件重启后）时由 step_capcut 从磁盘读回分镜段
            package = pipeline.step_capcut(timings, JOB.result, output_dir / pipeline.MASTER_NAME,
                                           output_dir, style or SubtitleStyle(), canvas=canvas)
            log(f"✅ {package.message}")
            log(f"   交接包：{package.package_dir}")
            with JOB.lock:
                JOB.ok = True
        elif action == "probe":
            for status in (MockEngine().probe(), DotsLocalEngine().probe(), FishLocalEngine().probe()):
                log(("✅ " if status.available else "⛔ ") + f"{status.key}：{status.detail}")
            aligner = WhisperAligner().probe()
            log(("✅ " if aligner.available else "⛔ ") + f"whisper：{aligner.detail}")
            with JOB.lock:
                JOB.ok = True
        elif action == "component":
            key = str(payload.get("component_key") or "")
            toolbox.install_component(key, log)
            with JOB.lock:
                JOB.ok = True
        elif action == "verify":
            from . import cli

            code = cli.verify()
            log("✅ 端到端自检通过" if code == 0 else f"❌ 自检未通过（退出码 {code}）")
            with JOB.lock:
                JOB.ok = code == 0
        else:
            raise ValueError(f"未知动作：{action}")
    except Exception as exc:
        JOB.append("❌ 失败：" + "".join(traceback.format_exception_only(exc)).strip())
    finally:
        with JOB.lock:
            JOB.running = False
            JOB.done = True


# ------------------------------------------------------------------ HTTP
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # 静默访问日志
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route in {"/", "/index.html"}:
            body = _INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if route == "/api/state":
            self._json(_state_payload())
            return
        if route == "/api/job":
            self._json(JOB.snapshot())
            return
        if route == "/api/components":
            self._json({"components": toolbox.component_statuses()})
            return
        if route == "/api/probe":
            statuses = [
                {"key": s.key, "name": n, "available": s.available, "detail": s.detail}
                for s, n in (
                    (MockEngine().probe(), "mock 引擎"),
                    (DotsLocalEngine().probe(), "dots.tts"),
                    (FishLocalEngine().probe(), "fish-speech"),
                )
            ]
            aligner = WhisperAligner().probe()
            statuses.append({"key": aligner.key, "name": "whisper 尺子",
                             "available": aligner.available, "detail": aligner.detail})
            import shutil as _sh

            ff = bool(_sh.which("ffmpeg") and _sh.which("ffprobe"))
            statuses.insert(0, {"key": "ffmpeg", "name": "ffmpeg / ffprobe",
                                "available": ff,
                                "detail": "渲染就绪。" if ff else "未找到 ffmpeg/ffprobe，无法渲染成片。"})
            self._json({"components": statuses})
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        if route == "/api/voices":
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > _MAX_UPLOAD:
                self._json({"error": f"音频大小非法：{length}"}, 400)
                return
            suffix = Path(query.get("filename") or "参考.wav").suffix or ".wav"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    handle.write(chunk)
                    remaining -= len(chunk)
                temp = Path(handle.name)
            try:
                entry = voice_library.register_voice(
                    STUDIO_HOME, query.get("name") or "未命名",
                    temp, transcript=query.get("transcript") or "")
                self._json({"ok": True, "voice_id": entry.voice_id})
            except Exception as exc:
                self._json({"error": str(exc)}, 400)
            finally:
                temp.unlink(missing_ok=True)
            return
        if route == "/api/run":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json({"error": "JSON 无效"}, 400)
                return
            action = str(payload.get("action") or "")
            with JOB.lock:
                if JOB.running:
                    self._json({"error": "当前有任务在执行，请等它完成。"}, 409)
                    return
                JOB.running, JOB.done, JOB.ok = True, False, False
                JOB.action = action
                JOB.log = [f"══ {action} 开始 ══"]
            threading.Thread(target=_run_job, args=(action, payload), daemon=True).start()
            self._json({"ok": True})
            return
        self._json({"error": "not found"}, 404)

    def do_DELETE(self) -> None:
        route = urlparse(self.path).path
        if route.startswith("/api/voices/"):
            voice_library.delete_voice(STUDIO_HOME, unquote(route.rsplit("/", 1)[-1]))
            self._json({"ok": True})
            return
        self._json({"error": "not found"}, 404)


def _state_payload() -> dict:
    voices = [{"voice_id": v.voice_id, "name": v.name, "transcript": v.transcript[:40]}
              for v in voice_library.list_voices(STUDIO_HOME)]
    return {
        "engines": pipeline.ENGINE_KEYS,
        "aligners": pipeline.ALIGNER_KEYS,
        "aspects": pipeline.ASPECT_KEYS,
        "positions": list(POSITION_PRESETS),
        "voices": voices,
        "voice_root": str(voice_library.voices_root(STUDIO_HOME)),
    }


def serve(port: int = DEFAULT_PORT, open_browser: bool = True) -> ThreadingHTTPServer:
    server = None
    last_error: OSError | None = None
    for candidate in range(port, port + 20):  # 端口被占自动顺延，避免双击闪退
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), _Handler)
            port = candidate
            break
        except OSError as exc:
            last_error = exc
    if server is None:
        raise OSError(f"端口 {port}~{port + 19} 均被占用：{last_error}")
    url = f"http://127.0.0.1:{port}/"
    print(f"水星配音对齐工作室已启动：{url}（Ctrl+C 退出）")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    return server


def main(port: int = DEFAULT_PORT, open_browser: bool = True) -> int:
    server = serve(port, open_browser)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0

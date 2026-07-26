"""长文合成：把超长整篇文案按句/行分块，逐块用**同一参考音色**克隆，再无缝拼接为连贯 master。

为什么需要（2026-07-23 用户反馈"克隆音频漂移"根因）：
    真 TTS（dots.tts / fish-speech）单次合成有输入上限。一次性喂 900+ 字整篇，
    引擎会截断（只出开头一段）或在超长自回归生成中劣化 → master 缺内容、时长远短于文案，
    后续逐行时间轴全部错位，听感/画面「漂移」。

策略（既不截断、又不音色漂移）：
    - 按脚本行聚合分块，每块 ≤ max_chars（单行超限时再按句读点 。！？；，切）；
    - 每块都用同一 voice 参考做零样本克隆 → 音色被参考锚定，块间一致；
    - 各块 WAV 同引擎同格式，直接拼接为一条连贯 master（句边界处的自然停顿即接缝）。

纯逻辑的 split_for_synthesis 可单测；synthesize_long 负责编排+拼接。
"""

from __future__ import annotations

import json
import re
import wave
from pathlib import Path

from integrated_workbench.semantic_match import parse_script

from .base import MasterAudio, SynthesisOptions, wav_seconds, write_master_metadata
from .voice_ref import VoiceRef


_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;，,、])")


def _split_long_line(line: str, max_chars: int) -> list[str]:
    """单行超过 max_chars 时按标点软切；仍超长则硬切，保证每片 ≤ max_chars。"""
    pieces: list[str] = []
    cur = ""
    for seg in _SENT_SPLIT.split(line):
        if not seg:
            continue
        if cur and len(cur) + len(seg) > max_chars:
            pieces.append(cur)
            cur = ""
        cur += seg
    if cur:
        pieces.append(cur)
    final: list[str] = []
    for piece in pieces:
        while len(piece) > max_chars:
            final.append(piece[:max_chars])
            piece = piece[max_chars:]
        if piece:
            final.append(piece)
    return final


def split_for_synthesis(text: str, max_chars: int) -> list[str]:
    """把整篇文案切成若干块（每块 ≤ max_chars，尽量按脚本行聚合，块内保留换行）。"""
    lines = parse_script(text)
    if not lines:
        return []
    if max_chars <= 0:
        return ["\n".join(lines)]
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for line in lines:
        parts = _split_long_line(line, max_chars) if len(line) > max_chars else [line]
        for part in parts:
            if cur and cur_len + len(part) > max_chars:
                chunks.append("\n".join(cur))
                cur, cur_len = [], 0
            cur.append(part)
            cur_len += len(part)
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def split_per_line(text: str) -> list[str]:
    """逐行分段：每行脚本单独成段（一行=一段音频=一个分镜时长）。

    这样每个分镜的时长 = 该行克隆音频的真实时长，精确对齐、无需 whisper 估边界
    （多行合一段时 whisper 在段内切行会漂移）。行本身超长也不再拆，保持与脚本行 1:1。
    """
    return list(parse_script(text))


def synthesize_long(engine, text: str, voice: VoiceRef | None, output: Path,
                    options: SynthesisOptions | None, max_chars: int, log=None,
                    per_line: bool = False, progress=None, heartbeat=None) -> MasterAudio:
    """长文分段合成 + 拼接。per_line=True 时逐行一段（一行=一段=一分镜时长，精确对齐）。

    progress(done, total)：每完成一段回调一次，供 UI 进度条实时前进（配音是最耗时的一步）。
    heartbeat(stage)：在「模型加载」「每行开始生成」等长耗时不动百分比的时刻刷新看门狗心跳，
        避免 3050 冷启动/慢 GPU 单行数十秒被误判「疑似卡死」。可选。
    单段（短文且非逐行）直接单次合成，与原行为一致。
    """
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # 模型加载与逐行生成拆成两个可观测阶段：先预热（可能 1–3 分钟）并单独打心跳，
    # 再进入逐行循环——这样看门狗看到的是「加载→逐行」的连续心跳，而非 5% 处一动不动。
    _warm = getattr(engine, "warmup", None)
    if callable(_warm):
        if heartbeat:
            heartbeat("① 配音 · 加载模型…")
        _warm(log=log)
        if heartbeat:
            heartbeat("① 配音 · 模型就绪，开始逐行克隆")
    chunks = split_per_line(text) if per_line else split_for_synthesis(text, max_chars)
    if len(chunks) <= 1:
        if heartbeat:
            heartbeat("① 配音 · 生成中…")
        master = engine.synthesize_full(text, voice, output, options)
        if progress:
            progress(1, 1)
        return master

    if log:
        how = "逐行一段（精确对齐）" if per_line else f"每段≤{max_chars}字"
        log(f"长文分段合成：共 {len(chunks)} 段（{how}，同一音色参考，拼接为连贯 master，避免截断/漂移）")
    tmp_dir = output.parent / f"{output.stem}_chunks"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    manifest_chunks: list[dict] = []
    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        part = tmp_dir / f"chunk_{i:03d}.wav"
        if heartbeat:
            heartbeat(f"① 配音 · 第 {i}/{total} 行 生成中…")   # 行开始即打心跳（生成本身可数十秒）
        engine.synthesize_full(chunk, voice, part, options)
        parts.append(part)
        manifest_chunks.append({"index": i, "line": i, "text": chunk, "file": part.name,
                                "seconds": round(wav_seconds(part), 3)})
        # 每段完成即刷新清单：UI 分段面板边配边出现，可立即试听/重配已完成段（不必等整篇）
        _write_manifest(tmp_dir, output, manifest_chunks)
        if log:
            log(f"  段 {i}/{total} 完成（{len(chunk)} 字）")
        if progress:
            progress(i, total)
    _concat_wavs(parts, output)
    _write_manifest(tmp_dir, output, manifest_chunks)

    seconds = wav_seconds(output)
    with wave.open(str(parts[0]), "rb") as first:
        sample_rate = first.getframerate()
    master = MasterAudio(
        path=output,
        engine=getattr(engine, "key", "?"),
        voice_id=voice.voice_id if voice else "",
        model=str(getattr(engine, "checkpoint", getattr(engine, "key", ""))),
        seed=options.seed if options else 42,
        sample_rate=sample_rate,
        seconds=round(seconds, 3),
        options=options.to_payload() if options else None,
    )
    write_master_metadata(master)
    return master


MANIFEST_NAME = "分段清单.json"


def chunks_dir_for(master: Path) -> Path:
    master = Path(master)
    return master.parent / f"{master.stem}_chunks"


def _write_manifest(tmp_dir: Path, master: Path, chunks: list[dict]) -> None:
    (tmp_dir / MANIFEST_NAME).write_text(
        json.dumps({"master": str(master), "chunks": chunks}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def read_manifest(master: Path) -> dict | None:
    """读回分段清单（供 UI 逐段试听/重配）；无分段（短文单次合成）时返回 None。"""
    path = chunks_dir_for(master) / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def redub_chunk(engine, master: Path, index: int, voice: VoiceRef | None,
                options: SynthesisOptions | None, log=None) -> dict:
    """只重配某一段：重合成该段 → 重拼整条 master → 更新清单（避免整篇重来的死循环）。"""
    master = Path(master)
    tmp_dir = chunks_dir_for(master)
    manifest = read_manifest(master)
    if not manifest:
        raise ValueError("该成片没有分段清单（可能是短文单次合成，无需分段重配）。")
    chunks = manifest["chunks"]
    target = next((c for c in chunks if int(c["index"]) == int(index)), None)
    if target is None:
        raise ValueError(f"段号 {index} 不存在（共 {len(chunks)} 段）。")
    part = tmp_dir / str(target["file"])
    if log:
        log(f"重配第 {index}/{len(chunks)} 段（{len(target['text'])} 字）…同一音色参考，其余段不动。")
    engine.synthesize_full(str(target["text"]), voice, part, options)
    target["seconds"] = round(wav_seconds(part), 3)
    parts = [tmp_dir / str(c["file"]) for c in sorted(chunks, key=lambda x: int(x["index"]))]
    _concat_wavs(parts, master)
    _write_manifest(tmp_dir, master, chunks)
    if log:
        log(f"✅ 第 {index} 段已重配并重拼 master（{wav_seconds(master):.2f}s）。")
    return manifest


def _concat_wavs(parts: list[Path], output: Path) -> None:
    """把同格式的多个 WAV 无缝拼成一条（同引擎产出，采样率/声道/位深一致）。"""
    if not parts:
        raise ValueError("没有可拼接的音频段。")
    with wave.open(str(parts[0]), "rb") as w0:
        params = w0.getparams()
    with wave.open(str(output), "wb") as out:
        out.setparams(params)
        for part in parts:
            with wave.open(str(part), "rb") as w:
                if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (
                        params.framerate, params.nchannels, params.sampwidth):
                    raise ValueError(f"音频段格式不一致，无法拼接：{part}")
                out.writeframes(w.readframes(w.getnframes()))

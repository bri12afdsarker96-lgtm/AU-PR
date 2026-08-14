"""长文合成：把超长整篇文案按句/行分块，逐块用**同一参考音色**克隆，再无缝拼接为连贯 master。

为什么需要（2026-07-23 用户反馈"克隆音频漂移"根因）：
    真 TTS（dots.tts / fish-speech）单次合成有输入上限。一次性喂 900+ 字整篇，
    引擎会截断（只出开头一段）或在超长自回归生成中劣化 → master 缺内容、时长远短于文案，
    后续逐行时间轴全部错位，听感/画面「漂移」。

策略（既不截断、又不音色漂移）：
    - 按脚本行聚合分块，每块 ≤ max_chars（单行超限时再按句读点 。！？；，切）；
    - 每块都用同一 voice 参考做零样本克隆 → 音色被参考锚定，块间一致；
    - 各块 WAV 同引擎同格式，直接拼接为一条连贯 master（句边界处的自然停顿即接缝）。

事务式合成（v0.7.71 P0-1 / P0-2 假成功彻底消除）：
    · 单段 / 多段 / redub_chunk 均**整体事务提交**（AtomicMultiCommit）：
        - 单段：`master.wav + master.json` 一起 commit；
        - 多段：`master_chunks/ + master.wav + master.json` 一起 commit；
        - redub_chunk：staging 里复制/重建**完整**新 chunks 目录 + 新 master + 新 meta，
          全部就绪后一起 commit——避免"先改 chunk 再改 master 再写 manifest"的分步不一致。
    · **后处理失败 = 任务失败**：不再吞异常返回"未后处理"的原始秒数；上层看到清晰
      中文错，用户拿到的旧 master/chunks 字节完整保留。

云 GPU 保活（v0.7.71 P0-4）：
    · synthesize_long / redub_chunk 提供 `before_engine_call / after_engine_call` 回调，
      供调用方在**真正的 `engine.synthesize_full`** 前后 mark 一次云 GPU 活跃。
    · 回调必须紧邻真实引擎调用；`after` 放在 finally，成功/失败都执行——保证配音
      结束（无论正常还是异常）后云 GPU 都被续期一次，不至于合成完立即被空闲判定关机。
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
import wave
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from integrated_workbench.semantic_match import parse_script

from ..atomic_commit import AtomicMultiCommit
from .base import (
    EngineCapabilities,
    MASTER_METADATA_SUFFIX,
    MasterAudio,
    SynthesisOptions,
    wav_seconds,
)
from .voice_ref import VoiceRef


class PostprocessRequired(RuntimeError):
    """用户显式设置了需要软件后处理（speed!=1 或 max_pause>0）却失败——任务必须失败。

    带引擎名 + 段号 + 原因；前端把它转成中文错误提示。这是"假成功"根因：过去在这里
    吞掉异常继续走，导致 master 已被覆盖但 speed/pause 从未生效。"""


def _engine_capabilities(engine) -> EngineCapabilities:
    caps = getattr(engine, "capabilities", None)
    if isinstance(caps, EngineCapabilities):
        return caps
    return EngineCapabilities()


def _validate_options(options: SynthesisOptions | None) -> None:
    """在**任何**引擎调用/后处理**之前**做严格数值校验：
    · speed 必须是有限正数（0/负数/NaN/Inf 抛 PostprocessRequired）；
    · max_pause_seconds 必须是 >=0 的有限数。

    此函数只对**用户显式传入**的字段做校验；SynthesisOptions() 的默认值（speed=1.0、
    max_pause_seconds=0.0）本身是合法的，走不到这里的报错分支。"""
    if options is None:
        return
    try:
        s = float(options.speed)
    except (TypeError, ValueError) as exc:
        raise PostprocessRequired(f"speed 参数不是数字：{options.speed!r}") from exc
    if s != s:
        raise PostprocessRequired("speed 不能是 NaN")
    if s in (float("inf"), float("-inf")):
        raise PostprocessRequired(f"speed 不能是无穷大：{s!r}")
    if s <= 0:
        raise PostprocessRequired(
            f"speed 必须为正数，收到 {s!r}（0/负数无物理意义；如无需变速请显式传 1.0）"
        )
    try:
        mp = float(options.max_pause_seconds)
    except (TypeError, ValueError) as exc:
        raise PostprocessRequired(
            f"max_pause_seconds 不是数字：{options.max_pause_seconds!r}") from exc
    if mp != mp or mp in (float("inf"), float("-inf")):
        raise PostprocessRequired(
            f"max_pause_seconds 必须是有限数：{options.max_pause_seconds!r}")
    if mp < 0:
        raise PostprocessRequired(f"max_pause_seconds 不能为负：{mp!r}")


def _needs_software_postprocess(options: SynthesisOptions | None,
                                caps: EngineCapabilities) -> bool:
    """用户到底有没有要求做软件后处理？

    · speed 与 1.0 有显著差异（|Δ|>=1e-3）且引擎不是 native_speed → 需要 atempo；
    · max_pause_seconds>0 → 需要静音压缩（与引擎能力无关）；
    · Edge TTS（native_speed=True）+ speed!=1 → 由 Worker 原生消费，本地**不**需要；
      但用户改了 max_pause 依然需要。

    这里**不再用** `opts.speed or 1.0` 这种 falsy 兜底——那会把用户显式设的 0 吞
    成 1.0（假成功根因）。合法性由 `_validate_options` 上层保证。"""
    if options is None:
        return False
    speed = float(options.speed)
    max_pause = float(options.max_pause_seconds)
    need_speed = (not caps.native_speed) and abs(speed - 1.0) >= 1e-3
    need_pause = max_pause > 0
    return need_speed or need_pause


def _apply_postprocess(part: Path, options: SynthesisOptions | None, engine, log=None) -> float:
    """按引擎能力对段做 speed/max_pause 后处理；返回后处理后**实际**秒数（供 manifest）。

    调用者语义：**只在候选/staging 文件上跑**，让 postprocess_wav 在候选文件上做
    原子替换；失败时抛 PostProcessError，由上层清 staging 并让任务失败。
    不再吞异常继续用未后处理的原始文件。参数校验（speed/max_pause）由上层
    `_validate_options` 保证；这里不再做 falsy 兜底。"""
    from .audio_postprocess import postprocess_wav

    opts = options or SynthesisOptions()
    caps = _engine_capabilities(engine)
    return postprocess_wav(
        Path(part),
        speed=float(opts.speed),
        max_pause_seconds=float(opts.max_pause_seconds),
        native_speed=bool(caps.native_speed),
    )


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
    """逐行分段：每行脚本单独成段（一行=一段音频=一个分镜时长）。"""
    return list(parse_script(text))


# ------------------------------------------------------------------ 事务基础
def _rm(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists() or path.is_symlink():
            path.unlink()
    except Exception:  # noqa: BLE001
        pass


@contextmanager
def _staging_scratch(sibling: Path, tag: str):
    """在 sibling.parent 下建一个 uuid 后缀的临时目录承载候选产物；上下文退出时清干净。"""
    txn = uuid.uuid4().hex[:12]
    scratch = sibling.parent / f".{sibling.name}.staging.{tag}.{txn}"
    _rm(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        yield scratch, txn
    finally:
        _rm(scratch)


def _validate_candidate_wav(path: Path) -> None:
    """候选文件基本自检：能被 wave 打开、时长 > 0——防"引擎写了 0 字节 / 损坏文件"过关。"""
    path = Path(path)
    if not path.is_file() or path.stat().st_size < 44:
        raise RuntimeError(f"候选音频缺失/过小（小于 WAV 头）：{path}")
    try:
        with wave.open(str(path), "rb") as w:
            n = w.getnframes(); sr = w.getframerate()
    except Exception as exc:
        raise RuntimeError(f"候选音频不是合法 WAV：{path}（{exc}）") from exc
    if sr <= 0 or n <= 0:
        raise RuntimeError(f"候选音频时长为 0：{path}")


def _write_master_meta_bytes(master: MasterAudio) -> bytes:
    payload = asdict(master)
    payload["path"] = str(master.path)
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


# ------------------------------------------------------------------ 单段/多段合成
def synthesize_long(engine, text: str, voice: VoiceRef | None, output: Path,
                    options: SynthesisOptions | None, max_chars: int, log=None,
                    per_line: bool = False, progress=None, heartbeat=None,
                    before_engine_call=None, after_engine_call=None) -> MasterAudio:
    """长文分段合成 + 拼接（事务式：AtomicMultiCommit 整体提交）。

    per_line=True 时逐行一段。progress(done, total) 每完成一段回调；heartbeat(stage)
    用于长耗时不动百分比时刷新看门狗。**后处理失败 = 抛 PostprocessRequired，任务失败**。

    P0-4：`before_engine_call() / after_engine_call()` 紧邻真实
    `engine.synthesize_full` 调用（after 在 finally——引擎抛错也执行）。云 GPU 保活
    钩子应通过这里而不是在动作入口 mark；引擎不真正被调用时（比如 mock 掉 step_dub、
    参数校验失败）**不**会 mark。
    """
    _validate_options(options)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    meta_path = output.with_suffix(MASTER_METADATA_SUFFIX)

    # 模型加载：先预热并单独打心跳
    _warm = getattr(engine, "warmup", None)
    if callable(_warm):
        if heartbeat:
            heartbeat("① 配音 · 加载模型…")
        _warm(log=log)
        if heartbeat:
            heartbeat("① 配音 · 模型就绪，开始逐行克隆")
    chunks = split_per_line(text) if per_line else split_for_synthesis(text, max_chars)

    if len(chunks) <= 1:
        # 单段路径：候选 master.wav + 候选 master.json → AtomicMultiCommit 一起 commit
        if heartbeat:
            heartbeat("① 配音 · 生成中…")
        with _staging_scratch(output, "solo") as (scratch, txn_id):
            cand_master = scratch / output.name
            cand_meta = scratch / meta_path.name
            # 保活：紧邻真实引擎调用；after 放 finally
            if before_engine_call:
                before_engine_call()
            try:
                try:
                    master = engine.synthesize_full(text, voice, cand_master, options)
                finally:
                    if after_engine_call:
                        after_engine_call()
                # 后处理（内部原子替换）；异常包装成 PostprocessRequired
                actual_seconds = _apply_postprocess(cand_master, options, engine, log=log)
                _validate_candidate_wav(cand_master)
                # 构造 master 记录 + 元数据 bytes
                master_rec = MasterAudio(
                    path=output, engine=master.engine, voice_id=master.voice_id,
                    model=master.model, seed=master.seed, sample_rate=master.sample_rate,
                    seconds=round(actual_seconds, 3), options=master.options,
                )
                cand_meta.write_bytes(_write_master_meta_bytes(master_rec))
                # AtomicMultiCommit：master + meta 一起 commit
                txn = AtomicMultiCommit(txn_id=txn_id)
                txn.add(cand_master, output)
                txn.add(cand_meta, meta_path)
                txn.commit()
            except Exception as exc:
                from .audio_postprocess import PostProcessError
                caps = _engine_capabilities(engine)
                if isinstance(exc, PostProcessError) and _needs_software_postprocess(options, caps):
                    raise PostprocessRequired(
                        f"配音后处理失败（引擎 {getattr(engine, 'key', '?')}）：{exc}"
                    ) from exc
                raise
        if progress:
            progress(1, 1)
        return master_rec

    # 多段路径：整个 chunks 目录 + master + meta 一起 commit
    if log:
        how = "逐行一段（精确对齐）" if per_line else f"每段≤{max_chars}字"
        log(f"长文分段合成：共 {len(chunks)} 段（{how}，同一音色参考，拼接为连贯 master，避免截断/漂移）")
    caps = _engine_capabilities(engine)
    with _staging_scratch(output, "multi") as (scratch, txn_id):
        cand_chunks_dir = scratch / f"{output.stem}_chunks"
        cand_chunks_dir.mkdir(parents=True, exist_ok=True)
        parts: list[Path] = []
        manifest_chunks: list[dict] = []
        total = len(chunks)
        for i, chunk in enumerate(chunks, start=1):
            part = cand_chunks_dir / f"chunk_{i:03d}.wav"
            if heartbeat:
                heartbeat(f"① 配音 · 第 {i}/{total} 行 生成中…")
            if before_engine_call:
                before_engine_call()
            try:
                try:
                    engine.synthesize_full(chunk, voice, part, options)
                finally:
                    if after_engine_call:
                        after_engine_call()
                actual_seconds = _apply_postprocess(part, options, engine, log=log)
                _validate_candidate_wav(part)
            except Exception as exc:
                from .audio_postprocess import PostProcessError
                if isinstance(exc, PostProcessError) and _needs_software_postprocess(options, caps):
                    raise PostprocessRequired(
                        f"第 {i} 段配音后处理失败（引擎 {getattr(engine, 'key', '?')}）："
                        f"{exc}——已保留上一次成功的 master，未污染。"
                    ) from exc
                raise
            parts.append(part)
            manifest_chunks.append({"index": i, "line": i, "text": chunk, "file": part.name,
                                    "seconds": round(actual_seconds, 3)})
            _write_manifest(cand_chunks_dir, output, manifest_chunks)
            if log:
                log(f"  段 {i}/{total} 完成（{len(chunk)} 字）")
            if progress:
                progress(i, total)

        # 拼 staging master
        cand_master = scratch / output.name
        _concat_wavs(parts, cand_master)
        _validate_candidate_wav(cand_master)
        # 用第一段的采样率作为 master.sample_rate（wave 无损拼接保持一致）
        with wave.open(str(parts[0]), "rb") as first:
            sample_rate = first.getframerate()
        seconds = wav_seconds(cand_master)
        master_rec = MasterAudio(
            path=output,
            engine=getattr(engine, "key", "?"),
            voice_id=voice.voice_id if voice else "",
            model=str(getattr(engine, "checkpoint", getattr(engine, "key", ""))),
            seed=options.seed if options else 42,
            sample_rate=sample_rate,
            seconds=round(seconds, 3),
            options=options.to_payload() if options else None,
        )
        cand_meta = scratch / meta_path.name
        cand_meta.write_bytes(_write_master_meta_bytes(master_rec))

        real_chunks_dir = chunks_dir_for(output)
        txn = AtomicMultiCommit(txn_id=txn_id)
        # 提交顺序：先 chunks 目录 → 再 master → 再 meta（rollback 逆序恢复）
        txn.add(cand_chunks_dir, real_chunks_dir)
        txn.add(cand_master, output)
        txn.add(cand_meta, meta_path)
        txn.commit()
    return master_rec


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
                options: SynthesisOptions | None, log=None,
                before_engine_call=None, after_engine_call=None) -> dict:
    """事务式重配某一段——staging 里复制/重建**完整**新 chunks 目录（把 index 那段换成
    新候选）+ 新 master + 新 meta，全部就绪后一起 AtomicMultiCommit。

    参数校验（manifest 存在、index 在范围内）在 `before_engine_call` **之前**——
    保证云 GPU 保活钩子只在**真的要发远程请求**时触发，不因为 rechunk 校验错误就
    误刷（P0-4 契约）。
    """
    _validate_options(options)
    master = Path(master)
    meta_path = master.with_suffix(MASTER_METADATA_SUFFIX)
    real_chunks_dir = chunks_dir_for(master)
    manifest = read_manifest(master)
    if not manifest:
        raise ValueError("该成片没有分段清单（可能是短文单次合成，无需分段重配）。")
    chunks = manifest["chunks"]
    target = next((c for c in chunks if int(c["index"]) == int(index)), None)
    if target is None:
        raise ValueError(f"段号 {index} 不存在（共 {len(chunks)} 段）。")
    caps = _engine_capabilities(engine)

    if log:
        log(f"重配第 {index}/{len(chunks)} 段（{len(target['text'])} 字）…同一音色参考，其余段不动。")

    with _staging_scratch(master, "redub") as (scratch, txn_id):
        cand_chunks_dir = scratch / real_chunks_dir.name
        cand_chunks_dir.mkdir(parents=True, exist_ok=True)
        # 复制所有旧 chunk 文件到 staging（除了要重配的那段——它由本次合成产出）
        for c in sorted(chunks, key=lambda x: int(x["index"])):
            src = real_chunks_dir / str(c["file"])
            if int(c["index"]) == int(index):
                continue
            shutil.copy2(src, cand_chunks_dir / str(c["file"]))
        # 生成候选新 chunk（保活钩子紧邻引擎调用，after 在 finally）
        cand_chunk = cand_chunks_dir / str(target["file"])
        if before_engine_call:
            before_engine_call()
        try:
            try:
                engine.synthesize_full(str(target["text"]), voice, cand_chunk, options)
            finally:
                if after_engine_call:
                    after_engine_call()
            actual_seconds = _apply_postprocess(cand_chunk, options, engine, log=log)
            _validate_candidate_wav(cand_chunk)
        except Exception as exc:
            from .audio_postprocess import PostProcessError
            if isinstance(exc, PostProcessError) and _needs_software_postprocess(options, caps):
                raise PostprocessRequired(
                    f"第 {index} 段重配后处理失败（引擎 {getattr(engine, 'key', '?')}）："
                    f"{exc}——已保留上一次成功的段与 master。"
                ) from exc
            raise
        target["seconds"] = round(actual_seconds, 3)
        _write_manifest(cand_chunks_dir, master, chunks)

        # 拼 staging master + 元数据
        cand_master = scratch / master.name
        assembled = [cand_chunks_dir / str(c["file"])
                     for c in sorted(chunks, key=lambda x: int(x["index"]))]
        _concat_wavs(assembled, cand_master)
        _validate_candidate_wav(cand_master)

        # 尝试复用旧 meta 里的字段（engine/voice_id/model/seed 通常不变）
        try:
            old_meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
        except Exception:  # noqa: BLE001
            old_meta = {}
        with wave.open(str(cand_master), "rb") as w:
            sample_rate = w.getframerate()
        master_rec = MasterAudio(
            path=master,
            engine=str(old_meta.get("engine") or getattr(engine, "key", "?")),
            voice_id=str(old_meta.get("voice_id") or (voice.voice_id if voice else "")),
            model=str(old_meta.get("model") or getattr(engine, "key", "?")),
            seed=old_meta.get("seed"),
            sample_rate=sample_rate,
            seconds=round(wav_seconds(cand_master), 3),
            options=old_meta.get("options"),
        )
        cand_meta = scratch / meta_path.name
        cand_meta.write_bytes(_write_master_meta_bytes(master_rec))

        # 一起 commit
        txn = AtomicMultiCommit(txn_id=txn_id)
        txn.add(cand_chunks_dir, real_chunks_dir)
        txn.add(cand_master, master)
        txn.add(cand_meta, meta_path)
        txn.commit()

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

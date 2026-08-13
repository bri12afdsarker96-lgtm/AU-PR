"""长文合成：把超长整篇文案按句/行分块，逐块用**同一参考音色**克隆，再无缝拼接为连贯 master。

为什么需要（2026-07-23 用户反馈"克隆音频漂移"根因）：
    真 TTS（dots.tts / fish-speech）单次合成有输入上限。一次性喂 900+ 字整篇，
    引擎会截断（只出开头一段）或在超长自回归生成中劣化 → master 缺内容、时长远短于文案，
    后续逐行时间轴全部错位，听感/画面「漂移」。

策略（既不截断、又不音色漂移）：
    - 按脚本行聚合分块，每块 ≤ max_chars（单行超限时再按句读点 。！？；，切）；
    - 每块都用同一 voice 参考做零样本克隆 → 音色被参考锚定，块间一致；
    - 各块 WAV 同引擎同格式，直接拼接为一条连贯 master（句边界处的自然停顿即接缝）。

事务式合成（v0.7.71 P0-1 / P0-2）：
    - 单段合成：引擎写候选文件（.staging.wav），候选通过后处理与元数据校验后
      才 `Path.replace` 覆盖正式 master；失败清临时、保留旧字节。
    - 多段合成：所有候选 chunk 落在 `<master>_staging_<pid>/` 独立目录，全部合成 +
      后处理成功后统一 commit（chunks_dir/chunk/manifest/master 全体一次性替换）。
    - `redub_chunk`：候选 chunk 单独 staging，后处理成功并且候选 master 拼接成功后
      才提交；任一步失败旧 chunk/master/manifest 原样保留。
    - **后处理失败 = 任务失败**：不再吞异常返回"未后处理"的原始秒数，避免用户
      看到"✅ 已重配"实际 speed/max_pause 完全没生效的假成功。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import wave
from contextlib import contextmanager
from pathlib import Path

from integrated_workbench.semantic_match import parse_script

from .base import EngineCapabilities, MasterAudio, SynthesisOptions, wav_seconds, write_master_metadata
from .voice_ref import VoiceRef


class PostprocessRequired(RuntimeError):
    """用户显式设置了需要软件后处理（speed!=1 或 max_pause>0）却失败——任务必须失败。

    带引擎名 + 段号 + 原因；前端把它转成中文错误提示。这是"假成功"根因：过去在这里
    吞掉异常继续走，导致 master 已被覆盖但 speed/pause 从未生效。"""


def _engine_capabilities(engine) -> EngineCapabilities:
    """从引擎读出能力矩阵；未声明的老引擎按"什么都不原生支持"兜底（保守）。"""
    caps = getattr(engine, "capabilities", None)
    if isinstance(caps, EngineCapabilities):
        return caps
    return EngineCapabilities()


def _needs_software_postprocess(options: SynthesisOptions | None,
                                caps: EngineCapabilities) -> bool:
    """用户到底有没有要求做软件后处理？

    · speed!=1.0 且引擎不是 native_speed → 需要 atempo；
    · max_pause_seconds>0 → 需要静音压缩（与引擎能力无关）；
    · Edge TTS（native_speed=True）+ speed!=1 → 由 Worker 原生消费，本地**不**需要；
      但用户改了 max_pause 依然需要。"""
    opts = options or SynthesisOptions()
    speed = float(opts.speed or 1.0)
    max_pause = float(opts.max_pause_seconds or 0.0)
    need_speed = (not caps.native_speed) and abs(speed - 1.0) >= 1e-3
    need_pause = max_pause > 0
    return need_speed or need_pause


def _apply_postprocess(part: Path, options: SynthesisOptions | None, engine, log=None) -> float:
    """按引擎能力对段做 speed/max_pause 后处理；返回后处理后**实际**秒数（供 manifest）。

    调用者语义：**只在候选/staging 文件上跑**，让 postprocess_wav 在候选文件上做
    原子替换；失败时抛出 PostProcessError，由上层清 staging 并让任务失败。
    不再吞异常继续用未后处理的原始文件。"""
    from .audio_postprocess import postprocess_wav

    opts = options or SynthesisOptions()
    caps = _engine_capabilities(engine)
    return postprocess_wav(
        Path(part),
        speed=float(opts.speed or 1.0),
        max_pause_seconds=float(opts.max_pause_seconds or 0.0),
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


# ------------------------------------------------------------------ 事务式合成基础设施
def _staging_dir(master: Path) -> Path:
    """成对的 staging 目录名：`<stem>_chunks.staging_<pid>` —— 与正式 chunks 目录同层，
    进程号后缀避免多 worker/异常残留互相踩。"""
    master = Path(master)
    return master.parent / f"{master.stem}_chunks.staging_{os.getpid()}"


def _rm_tree(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()
    except Exception:  # noqa: BLE001
        pass


@contextmanager
def _staging_swap(master: Path):
    """事务上下文：yield staging_dir，退出时若未 commit → 清 staging；commit=True → 由调用方
    自己完成 replace，本上下文只保证"失败必清 staging、不留半成品"。"""
    staging = _staging_dir(master)
    _rm_tree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        yield staging
    finally:
        _rm_tree(staging)


def _atomic_replace_file(src: Path, dst: Path) -> None:
    """把 src → dst 原子替换。Windows 上 os.replace 与 Path.replace 都支持覆盖已存在目标。"""
    src = Path(src); dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(src), str(dst))


def _atomic_replace_dir(src_dir: Path, dst_dir: Path) -> None:
    """把 src_dir → dst_dir 原子替换（先备份 dst → 复用为 backup，成功后删 backup；失败回滚）。

    Windows 上 os.replace 不支持非空目录覆盖：分三步：
        1) 若 dst_dir 存在，先重命名成 dst_dir + ".old_<pid>"；
        2) 把 src_dir 重命名成 dst_dir；
        3) 成功即 shutil.rmtree(dst_dir.old_...)；任一步失败则 rollback。"""
    src_dir = Path(src_dir); dst_dir = Path(dst_dir)
    if not src_dir.is_dir():
        raise RuntimeError(f"staging 目录不存在：{src_dir}")
    dst_dir.parent.mkdir(parents=True, exist_ok=True)
    backup = dst_dir.with_name(dst_dir.name + f".old_{os.getpid()}")
    _rm_tree(backup)
    had_old = dst_dir.exists()
    try:
        if had_old:
            os.replace(str(dst_dir), str(backup))
        os.replace(str(src_dir), str(dst_dir))
    except Exception:
        # 回滚：把 backup 归位（若已挪走），src_dir 保留在 staging
        if had_old and backup.exists() and not dst_dir.exists():
            try:
                os.replace(str(backup), str(dst_dir))
            except Exception:  # noqa: BLE001
                pass
        raise
    _rm_tree(backup)


def synthesize_long(engine, text: str, voice: VoiceRef | None, output: Path,
                    options: SynthesisOptions | None, max_chars: int, log=None,
                    per_line: bool = False, progress=None, heartbeat=None) -> MasterAudio:
    """长文分段合成 + 拼接（事务式：候选→后处理→原子提交；失败旧字节全保留）。

    per_line=True 时逐行一段。progress(done, total) 每完成一段回调；heartbeat(stage) 用于
    长耗时不动百分比时刷新看门狗。**后处理失败 = 抛 PostprocessRequired，任务失败**。
    """
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # 模型加载：先预热并单独打心跳，再进逐行循环
    _warm = getattr(engine, "warmup", None)
    if callable(_warm):
        if heartbeat:
            heartbeat("① 配音 · 加载模型…")
        _warm(log=log)
        if heartbeat:
            heartbeat("① 配音 · 模型就绪，开始逐行克隆")
    chunks = split_per_line(text) if per_line else split_for_synthesis(text, max_chars)
    caps = _engine_capabilities(engine)
    need_pp = _needs_software_postprocess(options, caps)

    if len(chunks) <= 1:
        # 单段路径：候选文件 = <master>.staging.wav；旧 master 全程未被引擎触碰
        if heartbeat:
            heartbeat("① 配音 · 生成中…")
        cand = output.with_suffix(output.suffix + ".staging.wav")
        cand_meta = cand.with_suffix(".json")   # 引擎顺带写的 master.json 兄弟
        _rm_tree(cand); _rm_tree(cand_meta)
        try:
            master = engine.synthesize_full(text, voice, cand, options)
            # postprocess_wav 内部对 need=False 时自动 no-op（只回读实际秒数），
            # 因此这里**无条件**调用，保证测试可 patch 到调用；任何异常一律上抛。
            actual_seconds = _apply_postprocess(cand, options, engine, log=log)
            _validate_candidate_wav(cand)
            _atomic_replace_file(cand, output)
        except Exception as exc:
            _rm_tree(cand); _rm_tree(cand_meta)
            from .audio_postprocess import PostProcessError
            if isinstance(exc, PostProcessError) and need_pp:
                raise PostprocessRequired(
                    f"配音后处理失败（引擎 {getattr(engine, 'key', '?')}）：{exc}"
                ) from exc
            raise
        finally:
            _rm_tree(cand_meta)
        # 重建 master 记录，落 master.json
        master = MasterAudio(
            path=output, engine=master.engine, voice_id=master.voice_id,
            model=master.model, seed=master.seed, sample_rate=master.sample_rate,
            seconds=round(actual_seconds, 3), options=master.options,
        )
        write_master_metadata(master)
        if progress:
            progress(1, 1)
        return master

    # 多段路径：staging 目录承载所有候选 chunk / manifest / master
    if log:
        how = "逐行一段（精确对齐）" if per_line else f"每段≤{max_chars}字"
        log(f"长文分段合成：共 {len(chunks)} 段（{how}，同一音色参考，拼接为连贯 master，避免截断/漂移）")
    with _staging_swap(output) as staging:
        parts: list[Path] = []
        manifest_chunks: list[dict] = []
        total = len(chunks)
        for i, chunk in enumerate(chunks, start=1):
            part = staging / f"chunk_{i:03d}.wav"
            if heartbeat:
                heartbeat(f"① 配音 · 第 {i}/{total} 行 生成中…")
            try:
                engine.synthesize_full(chunk, voice, part, options)
                # postprocess_wav 内部对 need=False 时 no-op 只回读秒数；无条件调用保证
                # 测试可 patch、任何异常都上抛，杜绝旧版"try/except 吞异常返回原始秒数"
                actual_seconds = _apply_postprocess(part, options, engine, log=log)
                _validate_candidate_wav(part)
            except Exception as exc:
                # 任一段失败：旧 chunks/master 未动（还在正式目录），staging 由 with 兜底清
                from .audio_postprocess import PostProcessError
                if isinstance(exc, PostProcessError) and need_pp:
                    raise PostprocessRequired(
                        f"第 {i} 段配音后处理失败（引擎 {getattr(engine, 'key', '?')}）："
                        f"{exc}——已保留上一次成功的 master，未污染。"
                    ) from exc
                raise
            parts.append(part)
            manifest_chunks.append({"index": i, "line": i, "text": chunk, "file": part.name,
                                    "seconds": round(actual_seconds, 3)})
            # staging 内实时写 manifest（供中断后清理观察；正式 manifest 一起 commit）
            _write_manifest(staging, output, manifest_chunks)
            if log:
                log(f"  段 {i}/{total} 完成（{len(chunk)} 字）")
            if progress:
                progress(i, total)
        # 拼接 staging master
        staging_master = staging / (output.name + ".staging.wav")
        _concat_wavs(parts, staging_master)
        _validate_candidate_wav(staging_master)
        # 从 staging 采样率读一次——待会 staging 目录被 replace 后 parts[0] 路径失效
        with wave.open(str(parts[0]), "rb") as first:
            sample_rate = first.getframerate()
        # 原子提交：先 chunks 目录，再 master 文件（旧 master 备份于内存）
        old_master_bytes = output.read_bytes() if output.is_file() else None
        real_chunks = chunks_dir_for(output)
        _atomic_replace_dir(staging, real_chunks)
        # staging 目录已经变成 real_chunks；master 从 real_chunks 里的 staging 文件挪出
        real_staging_master = real_chunks / (output.name + ".staging.wav")
        try:
            _atomic_replace_file(real_staging_master, output)
        except Exception as exc:
            # master 替换失败：尽力回滚旧 master（chunks 保留新版，用户可手动重拼）
            if old_master_bytes is not None:
                try:
                    output.write_bytes(old_master_bytes)
                except Exception:  # noqa: BLE001
                    pass
            raise RuntimeError(f"master 原子提交失败：{exc}——旧 master 已回滚") from exc

    seconds = wav_seconds(output)
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


def _wav_seconds_or_master(path: Path, master: MasterAudio) -> float:
    """需要 skip 后处理时用：优先按 WAV 实际秒数为准，读不到时用引擎自报。"""
    try:
        return round(wav_seconds(Path(path)), 3)
    except Exception:  # noqa: BLE001
        return float(master.seconds)


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
    """事务式重配某一段：候选 chunk 后处理成功 + 候选 master 拼接成功后**一起**提交；
    任一步失败 → 旧 chunk/master/manifest 原字节保留。**后处理失败 = 任务失败**，
    不再吞异常保留未变速的新音频。"""
    master = Path(master)
    real_chunks = chunks_dir_for(master)
    manifest = read_manifest(master)
    if not manifest:
        raise ValueError("该成片没有分段清单（可能是短文单次合成，无需分段重配）。")
    chunks = manifest["chunks"]
    target = next((c for c in chunks if int(c["index"]) == int(index)), None)
    if target is None:
        raise ValueError(f"段号 {index} 不存在（共 {len(chunks)} 段）。")
    real_chunk_file = real_chunks / str(target["file"])
    old_chunk_bytes = real_chunk_file.read_bytes() if real_chunk_file.is_file() else None
    old_master_bytes = master.read_bytes() if master.is_file() else None
    caps = _engine_capabilities(engine)
    need_pp = _needs_software_postprocess(options, caps)

    if log:
        log(f"重配第 {index}/{len(chunks)} 段（{len(target['text'])} 字）…同一音色参考，其余段不动。")

    # 候选 chunk：写在同目录的 .staging.wav
    cand_chunk = real_chunk_file.with_suffix(real_chunk_file.suffix + ".staging.wav")
    cand_master = master.with_suffix(master.suffix + ".staging.wav")
    _rm_tree(cand_chunk); _rm_tree(cand_master)
    try:
        engine.synthesize_full(str(target["text"]), voice, cand_chunk, options)
        # postprocess_wav 内部对 need=False 时 no-op 只回读秒数；无条件调用
        actual_seconds = _apply_postprocess(cand_chunk, options, engine, log=log)
        _validate_candidate_wav(cand_chunk)
        # 候选 master：候选 chunk + 其他旧 chunks 拼接（顺序按 index）
        assembled: list[Path] = []
        for c in sorted(chunks, key=lambda x: int(x["index"])):
            if int(c["index"]) == int(index):
                assembled.append(cand_chunk)
            else:
                assembled.append(real_chunks / str(c["file"]))
        _concat_wavs(assembled, cand_master)
        _validate_candidate_wav(cand_master)
        # 两个候选都通过 → 一起 commit（先 chunk 后 master；失败尽力回滚）
        _atomic_replace_file(cand_chunk, real_chunk_file)
        try:
            _atomic_replace_file(cand_master, master)
        except Exception as exc:
            if old_chunk_bytes is not None:
                try:
                    real_chunk_file.write_bytes(old_chunk_bytes)
                except Exception:  # noqa: BLE001
                    pass
            raise RuntimeError(f"master 提交失败：{exc}——已回滚 chunk") from exc
    except Exception as exc:
        _rm_tree(cand_chunk); _rm_tree(cand_master)
        # 引擎/后处理阶段失败：旧 chunk/master 未被覆盖过（我们只碰了 cand_*）
        from .audio_postprocess import PostProcessError
        if isinstance(exc, PostProcessError) and need_pp:
            raise PostprocessRequired(
                f"第 {index} 段重配后处理失败（引擎 {getattr(engine, 'key', '?')}）："
                f"{exc}——已保留上一次成功的段与 master。"
            ) from exc
        raise
    finally:
        _rm_tree(cand_chunk); _rm_tree(cand_master)

    target["seconds"] = round(actual_seconds, 3)
    _write_manifest(real_chunks, master, chunks)
    if log:
        log(f"✅ 第 {index} 段已重配并重拼 master（{wav_seconds(master):.2f}s）。")
    # 交回控制前额外副产物：新的实际秒数，可供上层日志（保持既有 return 类型不变——测试兼容）
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

"""Premiere Pro 工程导出：FCP7 XML（xmeml v4）交接包（2026-07-25 需求）。

剪映草稿目录不识别外部草稿、且无法导入 PP 工程——精修改走 Premiere：
导出 Premiere「文件→导入」可直接打开的 XML 序列：
    V1 = 逐行分镜段（成片_segments/NNN.mp4，顺序排布，时长即逐行配音时长）
    A1 = 整轨配音 master.wav（B 方案唯一音轨口径）
字幕用同目录 成片.srt（Premiere 导入为字幕轨/Caption）。素材全部引用输出目录
内的现有文件（绝对路径 file URL），整个输出目录即交接包，拷走前先在本机导入验证。

纯标准库、纯字符串构造，可单测（xml.etree 可解析、帧数守恒）。
"""

from __future__ import annotations

from pathlib import Path
from urllib.request import pathname2url
from xml.sax.saxutils import escape


PPRO_TICKS_PER_SECOND = 254016000000  # Premiere 内部时间单位（ppro ticks/秒），FCP7 XML 需要


def _pathurl(path: Path) -> str:
    """绝对路径 → Premiere 认的 file URL（Windows 盘符/中文/空格均转义）。"""
    return "file://localhost" + pathname2url(str(Path(path).resolve()))


def _rate(fps: int) -> str:
    return f"<rate><timebase>{int(fps)}</timebase><ntsc>FALSE</ntsc></rate>"


def _timecode(fps: int) -> str:
    """00:00:00:00 起始时间码（非丢帧）——Premiere 的 FCP7 XML 导入必需项。"""
    return (f"<timecode>{_rate(fps)}<string>00:00:00:00</string><frame>0</frame>"
            "<displayformat>NDF</displayformat></timecode>")


def _ticks(frames: int, fps: int) -> int:
    return round(frames / float(fps) * PPRO_TICKS_PER_SECOND)


def _video_sc(width: int, height: int, fps: int) -> str:
    """视频采样特征：Premiere 导入要有像素宽高/像素比/场/色深，缺了会判「格式不正确」而拒收。"""
    return (f"<samplecharacteristics>{_rate(fps)}<width>{width}</width><height>{height}</height>"
            "<anamorphic>FALSE</anamorphic><pixelaspectratio>square</pixelaspectratio>"
            "<fielddominance>none</fielddominance><colordepth>24</colordepth></samplecharacteristics>")


def build_fcp7_xml(sequence_name: str, segments: list[Path], master_wav: Path,
                   frames_per_segment: list[int], fps: int,
                   width: int, height: int,
                   audio_sample_rate: int = 48000,
                   audio_channels: int = 2) -> str:
    """构造 Premiere 可「文件→导入」的 FCP7 XML（xmeml v4）。V1 顺排分镜段、A1 整轨配音；
    帧数由调用方给定（与渲染帧量化一致，Σ帧 = 序列总长 = master 总长）。

    audio_sample_rate/audio_channels：必须与实际 A1 文件真实参数一致；混音单轨导出
    模式下由 `export_premiere_project` 从 export_mixdown 传入（PCM16/48kHz/stereo）——
    避免旧版硬编码 48000 与实际 WAV 采样率不一致造成 Premiere 拒收/时长漂移。

    对齐 Premiere 严格导入所需字段：序列 rate/timecode/in-out/format；每段 masterclipid、
    file 的 duration/rate/timecode/media 采样特征、pproTicksIn/Out；音频 channel/sourcetrack。
    字段不全时 Premiere 会拒收并报「格式不正确」——这正是旧版精简 XML 导不进去的原因。"""
    if len(segments) != len(frames_per_segment):
        raise ValueError(f"分镜段数({len(segments)})与帧窗口数({len(frames_per_segment)})不一致。")
    if not segments:
        raise ValueError("没有分镜段可导出。")
    total = sum(frames_per_segment)
    video_items: list[str] = []
    cursor = 0
    for i, (seg, frames) in enumerate(zip(segments, frames_per_segment), start=1):
        start, end = cursor, cursor + frames
        cursor = end
        video_items.append(f"""      <clipitem id="clipitem-v{i}">
        <masterclipid>masterclip-v{i}</masterclipid>
        <name>{escape(seg.name)}</name>
        <enabled>TRUE</enabled>
        <duration>{frames}</duration>{_rate(fps)}
        <start>{start}</start><end>{end}</end><in>0</in><out>{frames}</out>
        <pproTicksIn>0</pproTicksIn><pproTicksOut>{_ticks(frames, fps)}</pproTicksOut>
        <file id="file-v{i}">
          <name>{escape(seg.name)}</name>
          <pathurl>{escape(_pathurl(seg))}</pathurl>{_rate(fps)}
          <duration>{frames}</duration>{_timecode(fps)}
          <media><video><samplecharacteristics>{_rate(fps)}<width>{width}</width><height>{height}</height>
            <anamorphic>FALSE</anamorphic><pixelaspectratio>square</pixelaspectratio>
            <fielddominance>none</fielddominance></samplecharacteristics></video></media>
        </file>
        <compositemode>normal</compositemode>
      </clipitem>""")
    audio_item = f"""      <clipitem id="clipitem-a1">
        <masterclipid>masterclip-a1</masterclipid>
        <name>{escape(Path(master_wav).name)}</name>
        <enabled>TRUE</enabled>
        <duration>{total}</duration>{_rate(fps)}
        <start>0</start><end>{total}</end><in>0</in><out>{total}</out>
        <pproTicksIn>0</pproTicksIn><pproTicksOut>{_ticks(total, fps)}</pproTicksOut>
        <file id="file-a1">
          <name>{escape(Path(master_wav).name)}</name>
          <pathurl>{escape(_pathurl(master_wav))}</pathurl>{_rate(fps)}
          <duration>{total}</duration>{_timecode(fps)}
          <media><audio><samplecharacteristics><depth>16</depth><samplerate>{audio_sample_rate}</samplerate></samplecharacteristics>
            <channelcount>{audio_channels}</channelcount></audio></media>
        </file>
        <sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex></sourcetrack>
      </clipitem>"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="4">
  <sequence id="sequence-1">
    <name>{escape(sequence_name)}</name>
    <duration>{total}</duration>{_rate(fps)}
    {_timecode(fps)}
    <in>-1</in><out>-1</out>
    <media>
      <video>
        <format><samplecharacteristics>{_rate(fps)}<width>{width}</width><height>{height}</height>
          <anamorphic>FALSE</anamorphic><pixelaspectratio>square</pixelaspectratio>
          <fielddominance>none</fielddominance><colordepth>24</colordepth></samplecharacteristics></format>
        <track>
{chr(10).join(video_items)}
        </track>
      </video>
      <audio>
        <numOutputChannels>{audio_channels}</numOutputChannels>
        <format><samplecharacteristics><depth>16</depth><samplerate>{audio_sample_rate}</samplerate></samplecharacteristics></format>
        <track>
{audio_item}
        </track>
      </audio>
    </media>
  </sequence>
</xmeml>
"""


def export_premiere_project(output_dir: Path, segments: list[Path], master_wav: Path,
                            frames_per_segment: list[int], fps: int,
                            width: int, height: int,
                            film_mp4: Path | None = None) -> Path:
    """写出 Premiere工程.xml + 自包含素材到输出目录。返回 xml 路径。

    2026-07-25：把分镜段与配音**复制**进「Premiere工程_素材/」再引用副本，这样清理
    缓存后 Premiere 工程仍可打开、整个工程可独立拷走。

    v0.7.71 P1-1：若给定 film_mp4（且成片存在），**A1 轨切换为"成片同款混音单轨"**——
    从成片提取 PCM16/48k/stereo WAV，含配音+BGM+SFX+原视频音效混音。分镜段也复制
    成"去音轨版本"，避免与 A1 混音重复出声。film_mp4=None 抛错，禁止静默回退到裸 master。

    v0.7.71 事务化（AtomicMultiCommit）：所有素材副本 / mixdown / xml / 说明先落入
    `.staging.<uuid>` 命名的 staging 位置（同盘）；XML 内的 pathurl **写提交后的最终
    路径**（不能写 staging，否则 rename 后引用失效——P0-1 修复根因）；全部就绪后
    走原子多目标事务，任一步失败旧素材/旧 xml/旧说明字节级恢复。"""
    import shutil
    import uuid as _uuid

    output_dir = Path(output_dir)

    # v0.7.71 契约：成片同款混音单轨必需
    from .atomic_commit import AtomicMultiCommit
    from .export_mixdown import (
        MIXDOWN_CHANNELS,
        MIXDOWN_NAME,
        MIXDOWN_SAMPLE_RATE,
        MixdownError,
        extract_mixdown_wav,
        make_silent_video,
    )

    if film_mp4 is None:
        raise MixdownError("Premiere 导出契约：必须先生成成片（成片.mp4），再导出 Premiere 工程"
                           "——A1 用成片同款混音单轨，避免和 BGM/音效不同步。")
    film_mp4 = Path(film_mp4)

    material_dir = output_dir / "Premiere工程_素材"
    xml_path = output_dir / "Premiere工程.xml"
    note_path = output_dir / "Premiere导入说明.txt"

    # 事务 uuid + staging 位置（同盘）
    txn_id = _uuid.uuid4().hex[:12]
    material_staging = material_dir.with_name(material_dir.name + f".staging.{txn_id}")
    xml_staging = xml_path.with_name(xml_path.name + f".staging.{txn_id}")
    note_staging = note_path.with_name(note_path.name + f".staging.{txn_id}")
    for p in (material_staging, xml_staging, note_staging):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            try:
                p.unlink()
            except Exception:  # noqa: BLE001
                pass
    material_staging.mkdir(parents=True, exist_ok=True)

    try:
        # 1) 生成候选素材：mixdown + 逐段无音副本，先写到 staging（真实文件）
        audio_path_staging = material_staging / MIXDOWN_NAME
        extract_mixdown_wav(film_mp4, audio_path_staging)
        if not audio_path_staging.is_file() or audio_path_staging.stat().st_size < 44:
            raise MixdownError("混音单轨提取后为空——请检查成片是否有声音。")
        audio_sr = MIXDOWN_SAMPLE_RATE
        audio_ch = MIXDOWN_CHANNELS

        staged_segments: list[Path] = []       # staging 里的真实副本（供 make_silent_video 校验）
        final_segments: list[Path] = []        # 提交后的最终路径（XML 里必须写这个）
        for i, seg in enumerate(segments, start=1):
            seg = Path(seg)
            if not seg.exists():
                raise FileNotFoundError(f"分镜段不存在：{seg}（请先执行「③ 渲染成片 / 生成成片」）")
            fname = f"{i:03d}{seg.suffix}"
            target_staging = material_staging / fname
            make_silent_video(seg, target_staging)
            if not target_staging.is_file() or target_staging.stat().st_size == 0:
                raise MixdownError(f"第 {i} 段无音副本转换失败：{target_staging}")
            staged_segments.append(target_staging)
            final_segments.append(material_dir / fname)   # 提交后 XML 引用这条

        # 2) 关键修复（P0-1）：构造 XML 时**必须**用**提交后**的最终路径——
        #    否则 XML 里写死了 `Premiere工程_素材.staging.<uuid>/001.mp4`，
        #    staging 目录被 rename 后 pathurl 变成不存在的路径。
        audio_path_final = material_dir / MIXDOWN_NAME
        xml = build_fcp7_xml(output_dir.name or "水星成片", final_segments,
                             audio_path_final, frames_per_segment, fps, width, height,
                             audio_sample_rate=audio_sr, audio_channels=audio_ch)
        xml_staging.write_text(xml, encoding="utf-8")
        note_staging.write_text(_PREMIERE_IMPORT_NOTE, encoding="utf-8")

        # 3) AtomicMultiCommit：素材目录 + XML + 说明 一起原子提交
        txn = AtomicMultiCommit(txn_id=txn_id)
        txn.add(material_staging, material_dir)
        txn.add(xml_staging, xml_path)
        txn.add(note_staging, note_path)
        txn.commit()
    except Exception:
        # 失败：清 staging 半成品；正式旧字节由 AtomicMultiCommit 保留（若已进 commit 阶段）
        if material_staging.exists():
            shutil.rmtree(material_staging, ignore_errors=True)
        for p in (xml_staging, note_staging):
            try:
                if p.exists():
                    p.unlink()
            except Exception:  # noqa: BLE001
                pass
        raise
    return xml_path


_PREMIERE_IMPORT_NOTE = (
    "【关键：用「导入」，不要用「打开项目」】\n"
    "Premiere Pro 的原生工程是 .prproj，无法由外部工具离线生成；行业通用做法是导出\n"
    "Final Cut Pro XML 交换文件，再用 Premiere「导入」生成时间线（DaVinci/FCP 也这样进 PR）。\n"
    "\n"
    "步骤：\n"
    "  1) 打开 Premiere Pro（可新建一个空白项目）。\n"
    "  2) 文件 → 导入（File → Import）… 注意不是「打开项目」——「打开项目」只认 .prproj，\n"
    "     所以直接双击 / 用「打开」会提示格式不正确。\n"
    "  3) 选择本目录的「Premiere工程.xml」→ Premiere 自动生成含完整时间线的序列\n"
    "     （V1＝逐行分镜段，A1＝整轨配音）。\n"
    "  4) 想要 .prproj：导入成功后「文件 → 另存为」即得到你自己的 .prproj 工程。\n"
    "  5) 字幕：再「导入」同目录「成片.srt」到字幕轨。\n"
    "\n"
    "素材已复制进「Premiere工程_素材/」并被工程引用——自包含，可随 XML＋素材文件夹整体拷走；\n"
    "换电脑后若提示缺素材，在 Premiere 里对「Premiere工程_素材」重新链接即可。\n"
    "（本工程不依赖 成片_segments/ 与 master_chunks/，清理缓存后仍可导入。）\n"
    "\n"
    "【v0.7.71 契约】A1 = mixdown.wav 是「成片同款混音单轨」，含配音+BGM+SFX+原视频音效，\n"
    "打开工程后 A1 单独播放就等于成片音轨。分镜段是「去音轨版本」，V1 静音、只出 A1，\n"
    "避免声音重叠。BGM/SFX 暂不承诺独立可编辑轨道；如需拆轨请单独出原始素材版本。\n"
)

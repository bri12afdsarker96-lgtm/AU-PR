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


def _pathurl(path: Path) -> str:
    """绝对路径 → Premiere 认的 file URL（Windows 盘符/中文/空格均转义）。"""
    return "file://localhost" + pathname2url(str(Path(path).resolve()))


def _rate(fps: int) -> str:
    return f"<rate><timebase>{int(fps)}</timebase><ntsc>FALSE</ntsc></rate>"


def build_fcp7_xml(sequence_name: str, segments: list[Path], master_wav: Path,
                   frames_per_segment: list[int], fps: int,
                   width: int, height: int) -> str:
    """构造 xmeml v4 工程字符串。V1 顺排分镜段、A1 整轨配音；帧数由调用方给定
    （与渲染帧量化一致，Σ帧 = 序列总长 = master 总长）。"""
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
        video_items.append(f"""      <clipitem id="v{i}">
        <name>{escape(seg.name)}</name>
        <duration>{frames}</duration>{_rate(fps)}
        <start>{start}</start><end>{end}</end><in>0</in><out>{frames}</out>
        <file id="vf{i}">
          <name>{escape(seg.name)}</name>
          <pathurl>{escape(_pathurl(seg))}</pathurl>{_rate(fps)}
          <media><video><samplecharacteristics><width>{width}</width><height>{height}</height></samplecharacteristics></video></media>
        </file>
      </clipitem>""")
    audio_item = f"""      <clipitem id="a1">
        <name>{escape(Path(master_wav).name)}</name>
        <duration>{total}</duration>{_rate(fps)}
        <start>0</start><end>{total}</end><in>0</in><out>{total}</out>
        <file id="af1">
          <name>{escape(Path(master_wav).name)}</name>
          <pathurl>{escape(_pathurl(master_wav))}</pathurl>{_rate(fps)}
          <media><audio><samplecharacteristics><depth>16</depth><samplerate>48000</samplerate></samplecharacteristics></audio></media>
        </file>
        <sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex></sourcetrack>
      </clipitem>"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="4">
  <sequence id="seq1">
    <name>{escape(sequence_name)}</name>
    <duration>{total}</duration>{_rate(fps)}
    <media>
      <video>
        <format><samplecharacteristics><width>{width}</width><height>{height}</height>{_rate(fps)}</samplecharacteristics></format>
        <track>
{chr(10).join(video_items)}
        </track>
      </video>
      <audio>
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
                            width: int, height: int) -> Path:
    """写出 Premiere工程.xml 到输出目录（输出目录整体即交接包）。返回 xml 路径。"""
    output_dir = Path(output_dir)
    xml = build_fcp7_xml(output_dir.name or "水星成片", [Path(s) for s in segments],
                         Path(master_wav), frames_per_segment, fps, width, height)
    path = output_dir / "Premiere工程.xml"
    path.write_text(xml, encoding="utf-8")
    note = output_dir / "Premiere导入说明.txt"
    note.write_text(
        "Premiere Pro：文件 → 导入 → 选择本目录的「Premiere工程.xml」→ 得到完整时间线\n"
        "（V1=逐行分镜段，A1=整轨配音）。字幕：再导入同目录「成片.srt」到字幕轨。\n"
        "素材按绝对路径引用本目录内文件——整个输出目录即交接包；换电脑请整目录拷贝后\n"
        "在 Premiere 里对缺失素材「重新链接」到新位置。\n", encoding="utf-8")
    return path

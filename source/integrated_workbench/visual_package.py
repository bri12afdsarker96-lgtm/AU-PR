from __future__ import annotations

import csv
import json
from .proc import run_silent
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .models import DIR_COVER, DIR_PACKAGED_VIDEO, DIR_VISUAL_PACKAGE, IMAGE_EXTENSIONS, ProjectConfig
from .project import iter_media, log_line, ready_videos
from .title_engine import generate_title_package, load_title_map


DEFAULT_TEMPLATE = "冲突开场"

PACKAGING_TEMPLATES = {
    "冲突开场": {
        "badge": "冲突爆点",
        "intro": "冲突开场",
        "outro": "下一段更狠",
        "poster": "高能短剧",
        "sub_intro": "3 秒进入剧情",
        "sub_outro": "收藏继续看反转",
        "sub_poster": "智能封面包装",
        "accent": (239, 68, 68, 232),
        "headline": (255, 226, 120, 255),
        "motion": "flash",
    },
    "解说口播": {
        "badge": "剧情解说",
        "intro": "先听这一句",
        "outro": "结尾还有反转",
        "poster": "短剧解说",
        "sub_intro": "口播节奏版片头",
        "sub_outro": "适合连续发布",
        "sub_poster": "标题+封面自动包装",
        "accent": (34, 108, 255, 232),
        "headline": (210, 232, 255, 255),
        "motion": "clean",
    },
    "沉浸预告": {
        "badge": "沉浸预告",
        "intro": "真相快要揭开",
        "outro": "下一秒更关键",
        "poster": "剧情预告",
        "sub_intro": "悬念氛围片头",
        "sub_outro": "拉高完播期待",
        "sub_poster": "氛围感封面",
        "accent": (139, 92, 246, 232),
        "headline": (235, 224, 255, 255),
        "motion": "cinematic",
    },
}


def packaging_template_names() -> list[str]:
    return list(PACKAGING_TEMPLATES)


try:
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
except Exception:  # pragma: no cover - runtime dependency is packaged in release build
    Image = ImageDraw = ImageEnhance = ImageFilter = ImageFont = None


@dataclass
class VisualPackageItem:
    video_path: str
    title: str
    template: str
    poster_path: str
    intro_card_path: str
    outro_card_path: str
    intro_video_path: str = ""
    outro_video_path: str = ""
    packaged_video_path: str = ""
    status: str = "ok"
    message: str = ""


@dataclass
class VisualPackageResult:
    manifest_csv: Path
    manifest_json: Path
    posters: list[Path]
    cards: list[Path]
    card_videos: list[Path]
    packaged_videos: list[Path]
    items: list[VisualPackageItem]


def generate_visual_package(
    config: ProjectConfig,
    make_videos: bool = False,
    intro_seconds: float = 1.4,
    outro_seconds: float = 1.0,
    width: int = 1080,
    height: int = 1920,
    template: str = DEFAULT_TEMPLATE,
) -> VisualPackageResult:
    if Image is None:
        raise RuntimeError("当前环境缺少 Pillow，无法生成海报封面。")

    generate_title_package(config, make_covers=True)
    videos = ready_videos(config)
    if not videos:
        raise FileNotFoundError("合格待发布目录中没有视频。请先生成可发布成片。")

    title_map = load_title_map(config)
    root = config.root / DIR_VISUAL_PACKAGE
    poster_dir = root / "海报封面"
    intro_dir = root / "片头卡"
    outro_dir = root / "片尾卡"
    intro_video_dir = root / "片头视频"
    outro_video_dir = root / "片尾视频"
    packaged_dir = config.root / DIR_PACKAGED_VIDEO
    for directory in [poster_dir, intro_dir, outro_dir, intro_video_dir, outro_video_dir, packaged_dir]:
        directory.mkdir(parents=True, exist_ok=True)

    items: list[VisualPackageItem] = []
    posters: list[Path] = []
    cards: list[Path] = []
    card_videos: list[Path] = []
    packaged_videos: list[Path] = []
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    template_name = template if template in PACKAGING_TEMPLATES else DEFAULT_TEMPLATE
    template_slug = _safe_fragment(template_name)

    for index, video in enumerate(videos, start=1):
        title = title_map.get(str(video), f"{config.drama_name or config.project_name} 精彩片段")
        cover = _cover_for_video(config, video)
        poster = poster_dir / f"{video.stem}_{template_slug}_海报.png"
        intro_card = intro_dir / f"{video.stem}_{template_slug}_片头.png"
        outro_card = outro_dir / f"{video.stem}_{template_slug}_片尾.png"
        _draw_card(cover, poster, title, config, "poster", index, width, height, template_name)
        _draw_card(cover, intro_card, title, config, "intro", index, width, height, template_name)
        _draw_card(cover, outro_card, title, config, "outro", index, width, height, template_name)
        posters.append(poster)
        cards.extend([intro_card, outro_card])

        item = VisualPackageItem(
            video_path=str(video),
            title=title,
            template=template_name,
            poster_path=str(poster),
            intro_card_path=str(intro_card),
            outro_card_path=str(outro_card),
        )

        if make_videos:
            try:
                intro_video = intro_video_dir / f"{video.stem}_{template_slug}_片头.mp4"
                outro_video = outro_video_dir / f"{video.stem}_{template_slug}_片尾.mp4"
                packaged = packaged_dir / f"{video.stem}_{template_slug}_包装版_{stamp}.mp4"
                _make_card_video(config, intro_card, intro_video, intro_seconds, width, height, template_name)
                _make_card_video(config, outro_card, outro_video, outro_seconds, width, height, template_name)
                _make_packaged_video(config, intro_card, video, outro_card, packaged, intro_seconds, outro_seconds, width, height, template_name)
                card_videos.extend([intro_video, outro_video])
                packaged_videos.append(packaged)
                item.intro_video_path = str(intro_video)
                item.outro_video_path = str(outro_video)
                item.packaged_video_path = str(packaged)
            except Exception as exc:
                item.status = "warn"
                item.message = str(exc)
                log_line(config, f"包装成片失败：{video.name} {exc}")

        items.append(item)

    manifest_csv = root / "包装素材清单.csv"
    manifest_json = root / "包装素材清单.json"
    _write_manifest(manifest_csv, manifest_json, items)
    log_line(config, f"标题封面包装素材已生成：模板 {template_name}，海报 {len(posters)} 张，包装成片 {len(packaged_videos)} 条。")
    return VisualPackageResult(manifest_csv, manifest_json, posters, cards, card_videos, packaged_videos, items)


def _cover_for_video(config: ProjectConfig, video: Path) -> Path | None:
    covers = iter_media(config.root / DIR_COVER, IMAGE_EXTENSIONS)
    if not covers:
        return None
    matched = [cover for cover in covers if cover.stem in video.stem or video.stem in cover.stem]
    return matched[0] if matched else covers[0]


def _draw_card(
    cover: Path | None,
    output: Path,
    title: str,
    config: ProjectConfig,
    variant: str,
    index: int,
    width: int,
    height: int,
    template_name: str = DEFAULT_TEMPLATE,
) -> None:
    template = PACKAGING_TEMPLATES.get(template_name, PACKAGING_TEMPLATES[DEFAULT_TEMPLATE])
    image = _background_image(cover, width, height)
    draw = ImageDraw.Draw(image, "RGBA")
    title_font = _font(82, bold=True)
    subtitle_font = _font(38, bold=True)
    small_font = _font(28, bold=False)
    badge_font = _font(30, bold=True)

    draw.rectangle((0, 0, width, height), fill=(0, 0, 0, 72))
    draw.rounded_rectangle((70, 100, 420, 162), radius=18, fill=template["accent"])
    draw.text((96, 116), template["badge"], fill=(255, 255, 255, 255), font=badge_font)
    _paste_feature_image(image, cover, (90, 250, width - 90, 1080))

    if variant == "outro":
        headline = template["outro"]
        subline = template["sub_outro"]
    elif variant == "intro":
        headline = template["intro"]
        subline = template["sub_intro"]
    else:
        headline = template["poster"] if template_name != DEFAULT_TEMPLATE else (config.drama_name or config.project_name)
        subline = template["sub_poster"]

    draw.rounded_rectangle((70, 1210, width - 70, 1730), radius=30, fill=(10, 14, 24, 214), outline=(255, 255, 255, 58), width=3)
    draw.text((102, 1250), headline, fill=template["headline"], font=subtitle_font)
    lines = _wrap_text(title, title_font, width - 200)
    y = 1320
    for line in lines[:4]:
        draw.text((102, y), line, fill=(255, 255, 255, 255), font=title_font)
        y += 100
    draw.text((102, 1648), subline, fill=(211, 225, 255, 255), font=small_font)
    draw.rounded_rectangle((width - 260, 100, width - 72, 162), radius=18, fill=(255, 255, 255, 222))
    draw.text((width - 234, 116), f"第 {index:02d} 条", fill=(21, 35, 70, 255), font=badge_font)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(output, quality=94)


def _paste_feature_image(canvas, cover: Path | None, box: tuple[int, int, int, int]) -> None:
    if not cover or not cover.exists():
        return
    try:
        photo = Image.open(cover).convert("RGB")
    except Exception:
        return
    left, top, right, bottom = box
    frame_w = right - left
    frame_h = bottom - top
    scale = min(frame_w / photo.width, frame_h / photo.height)
    new_size = (max(1, round(photo.width * scale)), max(1, round(photo.height * scale)))
    photo = photo.resize(new_size)
    x = left + (frame_w - photo.width) // 2
    y = top + (frame_h - photo.height) // 2
    draw = ImageDraw.Draw(canvas, "RGBA")
    draw.rounded_rectangle((left, top, right, bottom), radius=34, fill=(255, 255, 255, 30), outline=(255, 255, 255, 72), width=3)
    mask = Image.new("L", photo.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle((0, 0, photo.width, photo.height), radius=28, fill=255)
    canvas.paste(photo.convert("RGBA"), (x, y), mask)


def _background_image(cover: Path | None, width: int, height: int):
    if cover and cover.exists():
        base = Image.open(cover).convert("RGB")
    else:
        base = Image.new("RGB", (width, height), (19, 27, 43))
    base = _resize_cover(base, width, height)
    base = base.filter(ImageFilter.GaussianBlur(radius=16))
    base = ImageEnhance.Brightness(base).enhance(0.54)
    base = ImageEnhance.Contrast(base).enhance(1.18)
    return base.convert("RGBA")


def _resize_cover(image, width: int, height: int):
    src_w, src_h = image.size
    scale = max(width / src_w, height / src_h)
    new_size = (max(1, round(src_w * scale)), max(1, round(src_h * scale)))
    resized = image.resize(new_size)
    left = max(0, (resized.width - width) // 2)
    top = max(0, (resized.height - height) // 2)
    return resized.crop((left, top, left + width, top + height))


def _font(size: int, bold: bool = False):
    candidates = [
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        if path and Path(path).exists():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def _wrap_text(text: str, font, max_width: int) -> list[str]:
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = char
    if current:
        lines.append(current)
    return lines or [text]


def _safe_fragment(value: str) -> str:
    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if char in invalid else char for char in value).strip()
    return cleaned or "template"


def _card_motion_filter(width: int, height: int, seconds: float, template_name: str) -> str:
    out_start = max(0.0, seconds - 0.28)
    motion = PACKAGING_TEMPLATES.get(template_name, PACKAGING_TEMPLATES[DEFAULT_TEMPLATE]).get("motion", "clean")
    if motion == "flash":
        return f"scale={width}:{height},eq=contrast=1.12:saturation=1.15,fade=t=in:st=0:d=0.18,fade=t=out:st={out_start:.2f}:d=0.25,format=yuv420p"
    if motion == "cinematic":
        return f"scale={width}:{height},eq=brightness=-0.02:contrast=1.18:saturation=0.95,fade=t=in:st=0:d=0.28,fade=t=out:st={out_start:.2f}:d=0.30,format=yuv420p"
    return f"scale={width}:{height},fade=t=in:st=0:d=0.22,fade=t=out:st={out_start:.2f}:d=0.25,format=yuv420p"


def _make_card_video(config: ProjectConfig, image: Path, output: Path, seconds: float, width: int, height: int, template_name: str = DEFAULT_TEMPLATE) -> None:
    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    vf = _card_motion_filter(width, height, seconds, template_name)
    command = [
        ffmpeg,
        "-y",
        "-loop",
        "1",
        "-t",
        f"{seconds:.2f}",
        "-i",
        str(image),
        "-f",
        "lavfi",
        "-t",
        f"{seconds:.2f}",
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-shortest",
        str(output),
    ]
    _run(command)


def _make_packaged_video(
    config: ProjectConfig,
    intro_card: Path,
    video: Path,
    outro_card: Path,
    output: Path,
    intro_seconds: float,
    outro_seconds: float,
    width: int,
    height: int,
    template_name: str = DEFAULT_TEMPLATE,
) -> None:
    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    total_duration = intro_seconds + (_duration(config, video) or 0.0) + outro_seconds
    has_audio = _has_audio(config, video)
    inputs = [
        "-loop",
        "1",
        "-t",
        f"{intro_seconds:.2f}",
        "-i",
        str(intro_card),
        "-i",
        str(video),
        "-loop",
        "1",
        "-t",
        f"{outro_seconds:.2f}",
        "-i",
        str(outro_card),
    ]
    intro_filter = _card_motion_filter(width, height, intro_seconds, template_name)
    outro_filter = _card_motion_filter(width, height, outro_seconds, template_name)
    video_filter = (
        f"[0:v]{intro_filter},fps=30,trim=duration={intro_seconds:.3f},setpts=PTS-STARTPTS[v0];"
        f"[1:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,setpts=PTS-STARTPTS[v1];"
        f"[2:v]{outro_filter},fps=30,trim=duration={outro_seconds:.3f},setpts=PTS-STARTPTS[v2];"
        "[v0][v1][v2]concat=n=3:v=1:a=0[outv]"
    )
    if has_audio:
        delay_ms = round(intro_seconds * 1000)
        audio_filter = f";[1:a]adelay={delay_ms}:all=1,apad,atrim=duration={total_duration:.3f},asetpts=PTS-STARTPTS[outa]"
        command = [
            ffmpeg,
            "-y",
            *inputs,
            "-filter_complex",
            video_filter + audio_filter,
            "-map",
            "[outv]",
            "-map",
            "[outa]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(output),
        ]
    else:
        command = [
            ffmpeg,
            "-y",
            *inputs,
            "-f",
            "lavfi",
            "-t",
            f"{total_duration:.3f}",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter_complex",
            video_filter,
            "-map",
            "[outv]",
            "-map",
            "3:a",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output),
        ]
    _run(command)


def _duration(config: ProjectConfig, video: Path) -> float | None:
    ffprobe = config.tools.ffprobe or "ffprobe"
    completed = run_silent(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        return float(completed.stdout.strip()) if completed.returncode == 0 else None
    except ValueError:
        return None


def _has_audio(config: ProjectConfig, video: Path) -> bool:
    ffprobe = config.tools.ffprobe or "ffprobe"
    completed = run_silent(
        [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=index", "-of", "csv=p=0", str(video)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.returncode == 0 and bool(completed.stdout.strip())


def _run(command: list[str]) -> None:
    completed = run_silent(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        detail = completed.stderr[-2200:] if completed.stderr else completed.stdout[-2200:]
        raise RuntimeError(detail.strip() or "ffmpeg 执行失败")


def _write_manifest(csv_path: Path, json_path: Path, items: list[VisualPackageItem]) -> None:
    rows = [asdict(item) for item in items]
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = list(rows[0].keys()) if rows else [field.name for field in VisualPackageItem.__dataclass_fields__.values()]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

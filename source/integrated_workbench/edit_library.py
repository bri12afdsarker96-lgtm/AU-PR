"""剪辑素材库：滤镜 / 画面特效 / 视频转场 / 字幕样式 / 音效库。

每一项都映射到确定的 FFmpeg 片段或参数，既供可视化剪辑器调用，也供自动剪辑
在生成时直接套用。所有构造函数都是纯函数（返回字符串/命令），可单元测试；音效库
为目录型（不随安装包分发音频，实际文件走用户音效文件夹或组件下载）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------- 滤镜（调色）
# name -> FFmpeg 滤镜片段（作用于视频流）
FILTERS: dict[str, str] = {
    "原片": "",
    "清晰增强": "eq=contrast=1.06:saturation=1.08,unsharp=5:5:0.6:3:3:0.0",
    "暖阳": "eq=gamma_r=1.06:gamma_b=0.96:saturation=1.12",
    "冷调": "eq=gamma_b=1.08:gamma_r=0.95:saturation=1.05",
    "电影感": "curves=preset=medium_contrast,eq=saturation=0.92",
    "复古胶片": "curves=preset=vintage,eq=saturation=0.9:contrast=1.04",
    "黑白": "hue=s=0",
    "高对比": "eq=contrast=1.2:brightness=0.02",
    "美食": "eq=saturation=1.2:contrast=1.05:gamma_r=1.03",
    "人像柔肤": "gblur=sigma=1.2:steps=1,eq=brightness=0.02:saturation=1.05",
}


# --------------------------------------------------------------- 画面特效
@dataclass(frozen=True)
class EffectItem:
    name: str
    category: str
    ffmpeg: str  # 空串表示需下载/GPU 高级特效（可下载模型）
    builtin: bool


# 分类参考剪映“特效 / 画面特效”：热门/基础/动感/氛围/边框/潮酷/复古/电影/漫画/光。
# builtin=True 的用 FFmpeg 直接实现；builtin=False 为目录项，走组件/素材下载后启用。
EFFECT_CATALOG: list[EffectItem] = [
    # 基础
    EffectItem("模糊", "基础", "gblur=sigma=8", True),
    EffectItem("暗角", "基础", "vignette=PI/5", True),
    EffectItem("锐化", "基础", "unsharp=5:5:1.0:5:5:0.0", True),
    EffectItem("闭幕", "基础", "fade=t=out:st=0:d=0.8:c=black", True),
    EffectItem("模糊开幕", "基础", "gblur=sigma=14", True),
    EffectItem("移轴模糊", "基础", "gblur=sigma=6", True),
    # 动感
    EffectItem("轻微抖动", "动感", "crop=in_w-8:in_h-8:'4*sin(n/3)':'4*cos(n/4)'", True),
    EffectItem("推进放大", "动感", "scale=iw*1.15:ih*1.15,crop=iw/1.15:ih/1.15", True),
    EffectItem("来回推拉", "动感", "crop=in_w-16:in_h-16:'8+8*sin(n/12)':'8+8*cos(n/12)'", True),
    EffectItem("旋转变焦", "动感", "", False),
    EffectItem("冲出屏幕", "动感", "scale=iw*1.25:ih*1.25,crop=iw/1.25:ih/1.25", True),
    # 潮酷 / 故障
    EffectItem("RGB分离", "潮酷", "rgbashift=rh=6:bh=-6", True),
    EffectItem("色差故障", "潮酷", "rgbashift=rh=8:bv=6,noise=alls=8:allf=t", True),
    EffectItem("荧光扫描", "潮酷", "edgedetect=mode=colormix:high=0.2", True),
    EffectItem("马赛克", "潮酷", "pixelize", True),
    EffectItem("乌云漩涡", "潮酷", "", False),
    # 复古 / 电影
    EffectItem("老电视", "复古", "noise=alls=12:allf=t,vignette=PI/4", True),
    EffectItem("老电影", "复古", "noise=alls=10:allf=t,curves=preset=vintage", True),
    EffectItem("暗黑噪点", "复古", "noise=alls=20:allf=t", True),
    EffectItem("电视纹理", "复古", "noise=alls=6:allf=t,eq=contrast=1.05", True),
    EffectItem("电影感画幅", "电影", "pad=iw:ih+2*ih*0.12:0:ih*0.12:color=black", True),
    EffectItem("电影感", "电影", "curves=preset=medium_contrast,eq=saturation=0.92", True),
    # 光 / 氛围
    EffectItem("精致嫩光", "光", "gblur=sigma=3,eq=brightness=0.03:saturation=1.05", True),
    EffectItem("水雾消散", "氛围", "gblur=sigma=4", True),
    EffectItem("水波", "氛围", "gblur=sigma=0.6,hue=h=2*sin(t)", True),
    # 边框
    EffectItem("基础黑框", "边框", "pad=iw+40:ih+40:20:20:color=black", True),
    EffectItem("怀旧边框", "边框", "pad=iw+48:ih+48:24:24:color=#2b2b2b", True),
    # 漫画 / AI（高级，需下载）
    EffectItem("黑白漫画", "漫画", "hue=s=0,eq=contrast=1.4,edgedetect=mode=colormix:high=0.1", True),
    EffectItem("黑白线描", "漫画", "edgedetect=mode=wires", True),
    EffectItem("卡通渲染", "漫画", "", False),
    EffectItem("破次元", "漫画", "", False),
    # 大雪纷飞、金粉、爱心等叠加素材类：需下载贴图/粒子
    EffectItem("大雪纷飞", "自然", "", False),
    EffectItem("金粉", "金粉", "", False),
    EffectItem("多屏球形", "多屏", "", False),
]

# 兼容旧接口：可直接用 FFmpeg 的特效名 -> 片段（含“无”）。
EFFECTS: dict[str, str] = {"无": ""}
EFFECTS.update({item.name: item.ffmpeg for item in EFFECT_CATALOG if item.builtin})


def effect_categories() -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for item in EFFECT_CATALOG:
        grouped.setdefault(item.category, []).append(item.name)
    return grouped


def builtin_effect_names() -> list[str]:
    return [item.name for item in EFFECT_CATALOG if item.builtin]


def effect_fade_in(seconds: float = 0.6, color: str = "black") -> str:
    """开场淡入特效片段（黑/白场进入）。"""
    mode = "in"
    fade_color = "white" if color == "white" else "black"
    return f"fade=t={mode}:st=0:d={max(0.1, seconds):.2f}:c={fade_color}"


def effect_fade_out(total_seconds: float, seconds: float = 0.6, color: str = "black") -> str:
    start = max(0.0, total_seconds - max(0.1, seconds))
    fade_color = "white" if color == "white" else "black"
    return f"fade=t=out:st={start:.2f}:d={max(0.1, seconds):.2f}:c={fade_color}"


# --------------------------------------------------------------- 视频转场
# 友好名 -> (xfade transition, 默认时长秒)。含剪映常见转场名到 xfade 的映射。
TRANSITIONS: dict[str, tuple[str, float]] = {
    "叠化": ("dissolve", 0.5),
    "色彩溶解": ("dissolve", 0.6),
    "淡入淡出": ("fade", 0.5),
    "闪黑": ("fadeblack", 0.4),
    "黑场过渡": ("fadeblack", 0.6),
    "闪白": ("fadewhite", 0.4),
    "白场过渡": ("fadewhite", 0.5),
    "向左推拉": ("slideleft", 0.5),
    "向右推拉": ("slideright", 0.5),
    "上滑": ("slideup", 0.5),
    "下滑": ("slidedown", 0.5),
    "左擦除": ("wipeleft", 0.5),
    "右擦除": ("wiperight", 0.5),
    "渐变擦除": ("wiperight", 0.6),
    "圆形展开": ("circleopen", 0.6),
    "圆形收拢": ("circleclose", 0.6),
    "像素化": ("pixelize", 0.6),
    "推近": ("zoomin", 0.6),
    "放大冲入": ("zoomin", 0.6),
    "模糊": ("hblur", 0.5),
    "雾化": ("hblur", 0.6),
    "放射": ("radial", 0.6),
}


@dataclass(frozen=True)
class TransitionItem:
    name: str
    category: str
    base: str  # TRANSITIONS 键；空串表示需下载/GPU 高级转场
    builtin: bool


# 分类参考剪映“转场”：热门/叠化/幻灯片/运镜/模糊/光效/故障/分割/MG动画/AI一镜到底。
# 运镜、MG动画、AI 一镜到底类需素材/GPU，标记为目录项（下载后启用）。
TRANSITION_CATALOG: list[TransitionItem] = [
    # 叠化
    TransitionItem("叠化", "叠化", "叠化", True),
    TransitionItem("色彩溶解", "叠化", "色彩溶解", True),
    TransitionItem("闪黑", "叠化", "闪黑", True),
    TransitionItem("闪白", "叠化", "闪白", True),
    TransitionItem("雾化", "叠化", "雾化", True),
    TransitionItem("淡入淡出", "叠化", "淡入淡出", True),
    # 幻灯片
    TransitionItem("向左推拉", "幻灯片", "向左推拉", True),
    TransitionItem("向右推拉", "幻灯片", "向右推拉", True),
    TransitionItem("左擦除", "幻灯片", "左擦除", True),
    TransitionItem("渐变擦除", "幻灯片", "渐变擦除", True),
    TransitionItem("圆形展开", "幻灯片", "圆形展开", True),
    # 模糊
    TransitionItem("模糊", "模糊", "模糊", True),
    TransitionItem("径向放射", "模糊", "放射", True),
    # 分割 / 故障
    TransitionItem("像素化", "故障", "像素化", True),
    TransitionItem("推近", "运镜", "推近", True),
    # 运镜 / MG动画 / AI 一镜到底（高级，需下载）
    TransitionItem("镜头速移", "运镜", "", False),
    TransitionItem("无限穿越", "运镜", "", False),
    TransitionItem("横移模糊", "运镜", "", False),
    TransitionItem("水波涟漪", "运镜", "", False),
    TransitionItem("海浪翻涌", "MG动画", "", False),
    TransitionItem("螺旋推进", "MG动画", "", False),
    TransitionItem("动漫闪电", "MG动画", "", False),
    TransitionItem("3D环绕运镜", "AI一镜到底", "", False),
    TransitionItem("无人机穿越", "AI一镜到底", "", False),
    TransitionItem("360运镜", "AI一镜到底", "", False),
]


def transition_categories() -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for item in TRANSITION_CATALOG:
        grouped.setdefault(item.category, []).append(item.name)
    return grouped


def builtin_transition_names() -> list[str]:
    return list(TRANSITIONS.keys())


def transition_filter(name: str, offset_seconds: float, duration: float | None = None) -> str:
    """构造两段视频衔接处的 xfade 滤镜表达式（作用于 [0][1] 两输入）。"""
    if name not in TRANSITIONS:
        raise KeyError(f"未知转场：{name}")
    transition, default_d = TRANSITIONS[name]
    d = default_d if duration is None else max(0.1, duration)
    return f"xfade=transition={transition}:duration={d:.2f}:offset={max(0.0, offset_seconds):.2f}"


# --------------------------------------------------------------- 字幕样式
@dataclass(frozen=True)
class SubtitleStyle:
    name: str
    font_size: int
    primary: str  # ASS &HBBGGRR 或颜色名
    outline: int
    position: str  # top / center / bottom
    bold: bool = False


SUBTITLE_STYLES: dict[str, SubtitleStyle] = {
    "默认白字黑边": SubtitleStyle("默认白字黑边", 42, "white", 3, "bottom"),
    "综艺大字": SubtitleStyle("综艺大字", 60, "yellow", 4, "bottom", bold=True),
    "标题居中": SubtitleStyle("标题居中", 66, "white", 3, "center", bold=True),
    "底部说明": SubtitleStyle("底部说明", 34, "white", 2, "bottom"),
    "关键词高亮": SubtitleStyle("关键词高亮", 54, "#FFD54F", 4, "center", bold=True),
    "顶部标签": SubtitleStyle("顶部标签", 40, "white", 3, "top"),
    "解说橙字": SubtitleStyle("解说橙字", 46, "#FB923C", 3, "bottom", bold=True),
}


def subtitle_drawtext(text: str, style: SubtitleStyle, frame_height: int = 1920) -> str:
    """把一条字幕渲染成 drawtext 片段（供预览/烧录）。"""
    safe = text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’")
    if style.position == "top":
        y = "h*0.08"
    elif style.position == "center":
        y = "(h-text_h)/2"
    else:
        y = "h-text_h-h*0.08"
    color = style.primary.replace("#", "0x") if style.primary.startswith("#") else style.primary
    return (
        f"drawtext=text='{safe}':fontsize={style.font_size}:fontcolor={color}"
        f":borderw={style.outline}:bordercolor=black@0.9:x=(w-text_w)/2:y={y}"
    )


# --------------------------------------------------------------- 音效库（目录型）
@dataclass(frozen=True)
class SoundEffect:
    name: str
    category: str
    filename: str  # 相对用户音效目录


# 音效库（目录型，分类参考剪映“音效素材”）。音频不随包分发，走用户音效目录/下载。
SOUND_EFFECTS: list[SoundEffect] = [
    # 转场
    SoundEffect("转场嗖声", "转场", "whoosh.wav"),
    SoundEffect("Swish", "转场", "swish.wav"),
    SoundEffect("弹幕嗖", "转场", "swoosh2.wav"),
    SoundEffect("水滴古风转场", "转场", "waterdrop_transition.wav"),
    SoundEffect("紧张转场音效", "转场", "tense_transition.wav"),
    # 提示
    SoundEffect("叮", "提示", "ding.wav"),
    SoundEffect("叮声", "提示", "ding2.wav"),
    SoundEffect("点击", "提示", "click.wav"),
    SoundEffect("金币", "提示", "coin.wav"),
    SoundEffect("任务完成", "提示", "task_done.wav"),
    SoundEffect("打卡成功", "提示", "checkin.wav"),
    SoundEffect("综艺灵光一闪", "提示", "ding_sparkle.wav"),
    # 节奏
    SoundEffect("爆点鼓点", "节奏", "impact.wav"),
    SoundEffect("咚 撞击轻响", "节奏", "thud.wav"),
    SoundEffect("综艺开头-咚", "节奏", "boom_intro.wav"),
    # 氛围
    SoundEffect("掌声", "氛围", "applause.wav"),
    SoundEffect("掌声2", "氛围", "applause2.wav"),
    SoundEffect("笑声", "氛围", "laugh.wav"),
    SoundEffect("氛围音 大气低沉", "氛围", "ambient_deep.wav"),
    SoundEffect("紧张气氛", "氛围", "tension.wav"),
    SoundEffect("舒缓钢琴音效", "氛围", "soft_piano.wav"),
    # 影视 / 悬疑
    SoundEffect("悬疑嗡声", "影视", "suspense_hum.wav"),
    SoundEffect("剧情悬疑梆", "影视", "suspense_knock.wav"),
    SoundEffect("影视悲伤节奏", "影视", "sad_beat.wav"),
    SoundEffect("空洞的声效", "影视", "hollow.wav"),
    SoundEffect("恐怖阴森转场", "影视", "horror_transition.wav"),
    # 综艺
    SoundEffect("综艺惊讶", "综艺", "variety_surprise.wav"),
    SoundEffect("综艺悬疑 不对劲", "综艺", "variety_suspense.wav"),
    SoundEffect("综艺咚", "综艺", "variety_boom.wav"),
    # 古风 / 其它
    SoundEffect("空灵水滴", "古风", "ethereal_drop.wav"),
    SoundEffect("水滴声", "古风", "waterdrop.wav"),
    SoundEffect("叹气声", "其它", "sigh.wav"),
    SoundEffect("丢书", "其它", "book_drop.wav"),
]


def sound_effect_categories() -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for sfx in SOUND_EFFECTS:
        grouped.setdefault(sfx.category, []).append(sfx.name)
    return grouped


def resolve_sound_effect(name: str, sound_dir: Path) -> Path | None:
    """在用户音效目录里定位音效文件；不存在返回 None（不随包分发音频）。"""
    for sfx in SOUND_EFFECTS:
        if sfx.name == name:
            candidate = Path(sound_dir) / sfx.filename
            return candidate if candidate.exists() else None
    return None


# --------------------------------------------------------------- 组合链
def build_video_filter_chain(filter_name: str = "原片", effect_names: list[str] | None = None) -> str:
    """把一个滤镜 + 若干画面特效组合成单条 -vf 表达式。"""
    parts: list[str] = []
    look = FILTERS.get(filter_name, "")
    if look:
        parts.append(look)
    for effect in effect_names or []:
        fragment = EFFECTS.get(effect, "")
        if fragment:
            parts.append(fragment)
    return ",".join(parts)


def library_summary() -> dict[str, int]:
    """供 UI / 自检展示：各库条目数量。"""
    return {
        "滤镜": len(FILTERS),
        "画面特效": len(EFFECTS),
        "视频转场": len(TRANSITIONS),
        "字幕样式": len(SUBTITLE_STYLES),
        "音效": len(SOUND_EFFECTS),
    }

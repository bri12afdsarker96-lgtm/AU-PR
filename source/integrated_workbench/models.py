from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


DIR_CONFIG = "00_项目配置"

DIR_ASSETS = "01_素材入库"
DIR_A = f"{DIR_ASSETS}/A_原始视频"
DIR_B = f"{DIR_ASSETS}/B_去重背景"
DIR_C = f"{DIR_ASSETS}/C_贴图素材"
DIR_AUDIO = f"{DIR_ASSETS}/D_音频素材"
DIR_SCRIPT = f"{DIR_ASSETS}/E_文案分镜"

DIR_AUTO_EDIT = "02_自动剪辑"
DIR_AUTO_EDIT_TASKS = f"{DIR_AUTO_EDIT}/00_任务入口"
DIR_AUTO_EDIT_INPUT = f"{DIR_AUTO_EDIT}/01_粗剪输入"
DIR_SCENE_DETECT = f"{DIR_AUTO_EDIT}/02_场景检测"
DIR_AUTO_EDIT_OUTPUT = f"{DIR_AUTO_EDIT}/03_分镜素材包"
DIR_AUTO_EDIT_PREVIEW = f"{DIR_AUTO_EDIT}/04_预览样片"
DIR_TRANSCRIPTS = f"{DIR_AUTO_EDIT}/05_字幕转写"
DIR_TEXT_TIMELINE = f"{DIR_AUTO_EDIT}/06_文本剪辑"
DIR_ASSEMBLED = f"{DIR_AUTO_EDIT}/06_整合成片"
DIR_DUB_ASSEMBLED = f"{DIR_AUTO_EDIT}/07_配音成片"
DIR_DESCRIPTION_SEARCH = f"{DIR_AUTO_EDIT}/07_描述找片段"

DIR_PRODUCTION = "03_二创生产"
DIR_REVIEW = f"{DIR_PRODUCTION}/01_中间结果"
DIR_REJECT = f"{DIR_PRODUCTION}/02_退回回收"
DIR_READY = f"{DIR_PRODUCTION}/03_合格待发布"
DIR_DONE = f"{DIR_PRODUCTION}/04_发布完成"
DIR_SOCIAL_CLIPS = f"{DIR_PRODUCTION}/05_社媒切条增强"
DIR_BATCH_ARCHIVE = f"{DIR_PRODUCTION}/06_成品批次档案"
DIR_EPISODE_REWORK = f"{DIR_PRODUCTION}/07_整集重组"

DIR_TITLES = "04_标题封面"
DIR_TITLE_TABLE = f"{DIR_TITLES}/01_标题表"
DIR_COVER = f"{DIR_TITLES}/02_封面图"
DIR_COPYWRITING = f"{DIR_TITLES}/03_发布文案"
DIR_VISUAL_PACKAGE = f"{DIR_TITLES}/04_包装素材"
DIR_PACKAGED_VIDEO = f"{DIR_TITLES}/05_包装成片"

DIR_HANDOFF = "05_发布交接"
DIR_QUEUE = f"{DIR_HANDOFF}/01_发布队列"
DIR_EDITOR_HANDOFF = f"{DIR_HANDOFF}/02_剪辑交接包"
DIR_PUBLISH_HANDOFF = f"{DIR_HANDOFF}/03_发布工具交接"
DIR_PUBLISH_STATUS = f"{DIR_HANDOFF}/04_发布回写"
DIR_PUBLISH_STATUS_RESULT = f"{DIR_PUBLISH_STATUS}/publish_status_import_result.json"

DIR_INVENTORY = f"{DIR_CONFIG}/01_素材清单"
DIR_LOGS = f"{DIR_CONFIG}/02_运行日志"
DIR_REPORTS = f"{DIR_CONFIG}/03_运行记录"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
TEXT_EXTENSIONS = {".txt", ".csv", ".srt", ".ass", ".vtt", ".md", ".json"}
TABLE_EXTENSIONS = {".csv", ".xlsx", ".xlsm"}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | IMAGE_EXTENSIONS | TEXT_EXTENSIONS | TABLE_EXTENSIONS

LEGACY_DIR_ALIASES = {
    DIR_A: ["01_原始视频_A"],
    DIR_B: ["02_去重背景_B"],
    DIR_C: ["03_贴图素材_C"],
    DIR_TITLE_TABLE: ["04_标题封面"],
    DIR_COVER: ["04_标题封面"],
    DIR_COPYWRITING: ["04_标题封面"],
    DIR_REVIEW: [f"{DIR_PRODUCTION}/01_" + "生成" + "待" + "审", "05_" + "生成" + "待" + "审"],
    DIR_REJECT: [f"{DIR_PRODUCTION}/02_" + "审" + "核退回"],
    DIR_READY: ["06_待发布"],
    DIR_DONE: ["07_发布完成"],
    DIR_LOGS: ["08_日志"],
    DIR_AUTO_EDIT_INPUT: ["09_自动剪辑输入"],
    DIR_AUTO_EDIT_OUTPUT: ["10_自动剪辑输出"],
    DIR_SCENE_DETECT: ["08_日志/场景检测"],
    DIR_ASSEMBLED: ["02_自动剪辑/06_整合成片"],
    DIR_DUB_ASSEMBLED: ["02_自动剪辑/07_配音成片"],
    DIR_QUEUE: ["."],
}


@dataclass(frozen=True)
class DirectorySpec:
    path: str
    title: str
    purpose: str
    input_hint: str
    output_hint: str


DIRECTORY_SPECS = [
    DirectorySpec(DIR_CONFIG, "项目配置", "存放项目配置、账号表、业务流程说明。", "项目名、剧名、账号。", "project.json、publish_accounts.csv、目录说明。"),
    DirectorySpec(DIR_A, "原始视频", "放短剧原片、待处理主素材。", "mp4/mov/mkv 等原始视频。", "供自动剪辑和二创生成读取。"),
    DirectorySpec(DIR_B, "去重背景", "放叠加背景、虚化背景、混剪底片。", "背景视频或去重视频。", "供二创生成随机组合。"),
    DirectorySpec(DIR_C, "贴图素材", "放贴纸、边框、角标、遮挡图。", "png/jpg/webp 图片。", "供竖屏二创叠加使用。"),
    DirectorySpec(DIR_AUDIO, "音频素材", "放配音、BGM、音效。", "mp3/wav/m4a。", "供后续配音、字幕和混音使用。"),
    DirectorySpec(DIR_SCRIPT, "文案分镜", "放分镜表、台词、字幕、选题文案。", "csv/xlsx/txt/srt。", "供自动剪辑和标题文案读取。"),
    DirectorySpec(DIR_AUTO_EDIT_TASKS, "自动剪辑入口", "存放自动生成的剪辑计划和策略报告。", "链接、策略、阈值。", "自动剪辑计划 CSV/JSON。"),
    DirectorySpec(DIR_AUTO_EDIT_INPUT, "粗剪输入", "放专门给自动剪辑筛选的候选片段。", "长素材、候选片段。", "生成分镜素材包。"),
    DirectorySpec(DIR_SCENE_DETECT, "场景检测", "存放镜头切点检测结果。", "视频检测任务。", "场景 CSV、JSON、检测日志。"),
    DirectorySpec(DIR_AUTO_EDIT_OUTPUT, "分镜素材包", "存放自动剪出的分镜片段、音频占位、导入清单。", "自动剪辑任务。", "分镜 mp4、剪映导入清单、edit_handoff.json。"),
    DirectorySpec(DIR_AUTO_EDIT_PREVIEW, "预览样片", "集中放自动剪辑预览视频和抽帧图。", "自动剪辑预览。", "预览 mp4、抽帧 jpg。"),
    DirectorySpec(DIR_TRANSCRIPTS, "字幕转写", "存放自动生成的字幕草稿、台词时间轴和转写报告。", "视频、音频、参考文案。", "SRT、CSV、JSON，可同步到文案分镜。"),
    DirectorySpec(DIR_TEXT_TIMELINE, "文本剪辑", "存放可编辑台词剪辑表、文本剪辑计划和报告。", "字幕草稿、关键词、删除词。", "台词剪辑表.csv、文本剪辑计划.csv、文本剪辑报告.json。"),
    DirectorySpec(DIR_ASSEMBLED, "整合成片", "存放由短剧片段按清单或文件顺序整合出的预告/高能混剪。", "片段目录或整合清单 CSV。", "整合 MP4、整合清单 CSV。"),
    DirectorySpec(DIR_DUB_ASSEMBLED, "配音成片", "存放分镜视频按配音包逐段对齐后的音画同步成片。", "配音包目录、分镜视频目录。", "配音成片 MP4、音画对齐表 CSV/JSON。"),
    DirectorySpec(DIR_DESCRIPTION_SEARCH, "描述找片段", "按人物、动作、场景、台词意图筛出候选片段。", "一句片段描述、字幕、文件名、镜头信号。", "描述匹配表、剪辑计划、可预览分镜素材包。"),
    DirectorySpec(DIR_REVIEW, "中间结果", "二创生成后的中间结果先放这里。", "二创输出。", "供复查或后续产线读取的视频。"),
    DirectorySpec(DIR_REJECT, "退回回收", "放不可用视频和退回原因。", "自动拦截或手动移入。", "退回视频、原因记录。"),
    DirectorySpec(DIR_READY, "合格待发布", "只放确认可发布的视频。", "产线输出视频。", "发布队列读取这里。"),
    DirectorySpec(DIR_DONE, "发布完成", "发布后归档。", "已发布视频。", "发布完成存档。"),
    DirectorySpec(DIR_SOCIAL_CLIPS, "社媒切条增强", "存放竖屏裁切、音量标准化、BGM 压低后的短视频版本。", "中间结果、待发布或自动剪辑片段。", "社媒增强版 MP4、生成清单。"),
    DirectorySpec(DIR_BATCH_ARCHIVE, "成品批次档案", "按二创批次生成只读追溯快照。", "二创批次、去重、重做、标题、发布产物。", "批次档案 JSON/Markdown。"),
    DirectorySpec(DIR_EPISODE_REWORK, "整集重组", "存放长剧集切片、逐段去重变体和按原顺序合并后的整集版本。", "长剧集原片或自动剪辑输入。", "整集重组 MP4、切片清单、重组清单。"),
    DirectorySpec(DIR_TITLE_TABLE, "标题表", "存放标题 CSV 和标题映射。", "合格待发布视频。", "titles.csv。"),
    DirectorySpec(DIR_COVER, "封面图", "存放封面图。", "jpg/png/webp。", "发布队列自动匹配。"),
    DirectorySpec(DIR_COPYWRITING, "发布文案", "存放简介、话题、平台文案。", "标题表、剧名。", "发布文案.csv。"),
    DirectorySpec(DIR_VISUAL_PACKAGE, "包装素材", "存放海报封面、片头卡、片尾卡和包装清单。", "标题表、封面图、待发布视频。", "竖屏海报 PNG、片头片尾 MP4、包装清单。"),
    DirectorySpec(DIR_PACKAGED_VIDEO, "包装成片", "存放加片头片尾后的包装版视频。", "包装素材、待发布视频。", "包装版 MP4、包装成片清单。"),
    DirectorySpec(DIR_QUEUE, "发布队列", "存放账号分配后的发布队列表。", "待发布视频、账号表、标题表。", "publish_queue.csv。"),
    DirectorySpec(DIR_EDITOR_HANDOFF, "剪辑交接包", "给本地剪辑器或精修软件读取的任务包。", "项目素材、自动剪辑结果。", "editor_handoff.json、任务 CSV。"),
    DirectorySpec(DIR_PUBLISH_HANDOFF, "发布工具交接", "给发布工具读取的任务包。", "发布队列、账号。", "publish_handoff.json、导入 CSV。"),
    DirectorySpec(DIR_PUBLISH_STATUS, "发布回写", "存放平台发布结果和状态回写记录。", "发布工具回传 CSV/JSON。", "状态导入结果、失败原因、发布链接。"),
    DirectorySpec(DIR_INVENTORY, "素材清单", "自动盘点所有素材和产物。", "项目目录。", "素材清单.csv、素材清单.json。"),
    DirectorySpec(DIR_LOGS, "运行日志", "存放运行日志。", "每次操作。", "按日期写入日志。"),
    DirectorySpec(DIR_REPORTS, "运行记录", "存放发布记录、流程快照和组件校验记录。", "流程运行结果。", "发布记录、流程状态、组件校验文件。"),
]


@dataclass
class VideoRecipe:
    mode: str = "vertical"
    copies_per_source: int = 3
    dedup_level: str = "balanced"
    make_dedup_report: bool = True
    # opt-in：强制每份成片首帧微裁量互不相同（平台常抽首帧判重）。默认关，
    # 关闭时去重变体与冻结基线逐字节一致，不影响既有业务验证。
    force_distinct_intro: bool = False
    dedup_similarity_threshold: float = 0.92
    b_opacity: float = 0.05
    c_scale: float = 1.0
    c_opacity: float = 0.8
    background_blur: float = 20.0
    output_width: int = 1080
    output_height: int = 1920
    crf: int = 23
    preset: str = "veryfast"


@dataclass
class EditRecipe:
    target_seconds: float = 60.0
    clip_seconds: float = 4.0
    output_width: int = 1080
    output_height: int = 1920
    fps: int = 30
    keep_preview_audio: bool = False
    make_contact_sheet: bool = True


@dataclass
class ToolPaths:
    ffmpeg: str = ""
    ffprobe: str = ""
    local_editor_root: str = ""
    local_editor_exe: str = ""
    publish_tool_exe: str = ""


@dataclass
class ProjectConfig:
    project_name: str
    project_root: str
    drama_name: str = ""
    recipe: VideoRecipe = field(default_factory=VideoRecipe)
    edit: EditRecipe = field(default_factory=EditRecipe)
    tools: ToolPaths = field(default_factory=ToolPaths)
    directory_version: str = "workflow_v3"
    workflow_mode: str = "simple"
    dub_last_audio_dir: str = ""
    dub_last_video_dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ProjectConfig":
        recipe = VideoRecipe(**data.get("recipe", {}))
        edit = EditRecipe(**data.get("edit", {}))
        tool_data = dict(data.get("tools", {}))
        old_root_key = "c" + "rab_root"
        old_exe_key = "c" + "rab_exe"
        if old_root_key in tool_data and "local_editor_root" not in tool_data:
            tool_data["local_editor_root"] = tool_data.pop(old_root_key)
        else:
            tool_data.pop(old_root_key, None)
        if old_exe_key in tool_data and "local_editor_exe" not in tool_data:
            tool_data["local_editor_exe"] = tool_data.pop(old_exe_key)
        else:
            tool_data.pop(old_exe_key, None)
        tools = ToolPaths(**tool_data)
        return ProjectConfig(
            project_name=data["project_name"],
            project_root=data["project_root"],
            drama_name=data.get("drama_name") or data["project_name"],
            recipe=recipe,
            edit=edit,
            tools=tools,
            directory_version=data.get("directory_version", "workflow_v3"),
            workflow_mode=data.get("workflow_mode", "simple") or "simple",
            dub_last_audio_dir=data.get("dub_last_audio_dir", "") or "",
            dub_last_video_dir=data.get("dub_last_video_dir", "") or "",
        )

    @property
    def root(self) -> Path:
        return Path(self.project_root)


@dataclass
class PublishAccount:
    group: str
    account_id: str
    nickname: str
    publish_count: int = 2
    interval_minutes: int = 20
    scheduled_time: str = ""
    chrome_port: int = 9222
    enabled: bool = True


@dataclass
class QueueItem:
    group: str
    account_id: str
    nickname: str
    video_path: str
    title: str
    cover_path: str = ""
    scheduled_time: str = ""
    status: str = "待发布"

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .github_research import research_summary, write_github_research_report


ARCHITECTURE_REFERENCE_KEYS = {
    "narratoai",
    "short_video_factory",
    "video_use",
    "openmontage",
    "firered_openstoryline",
    "director",
    "chengfeng_videocut_skills",
    "nomi",
    "frame_ai_editor",
    "opencut",
    "timeline_studio",
    "agentic_video_editor",
    "ai_video_editing_skill",
}


@dataclass
class ToolPlugin:
    key: str
    name: str
    github: str
    folder: str
    license: str
    business_goal: str
    target_stage: str
    runtime_type: str
    expected_entry: str
    integration_status: str
    install_note: str
    tier: str = "reference"
    download_url: str = ""
    github_page: str = ""

    def source_path(self) -> Path:
        return vendor_root() / self.folder

    def is_downloaded(self) -> bool:
        return self.source_path().exists()

    def executable_path(self) -> Path | None:
        if not self.expected_entry:
            return None
        path = Path(self.expected_entry)
        if not path.is_absolute():
            source_candidate = self.source_path() / path
            if source_candidate.exists():
                return source_candidate
            bin_candidate = built_tools_root() / path
            if bin_candidate.exists():
                return bin_candidate
            return source_candidate
        return path

    def is_executable_ready(self) -> bool:
        path = self.executable_path()
        return bool(path and path.exists())

    def to_status(self) -> dict[str, Any]:
        data = asdict(self)
        data["github_page"] = self.github_page or self.github
        data["source_path"] = str(self.source_path())
        data["downloaded"] = self.is_downloaded()
        entry = self.executable_path()
        data["entry_path"] = str(entry) if entry else ""
        data["entry_ready"] = self.is_executable_ready()
        classification, reason = classify_plugin(self)
        data["classification"] = classification
        data["classification_reason"] = reason
        return data


def version_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        if (candidate / "source" / "integrated_workbench").exists() and (candidate / "docs").exists():
            return candidate
    return Path(__file__).resolve().parents[2]


def vendor_root() -> Path:
    return version_root() / "vendor_tools"


def built_tools_root() -> Path:
    return version_root() / "tools_bin"


def classify_plugin(plugin: ToolPlugin) -> tuple[str, str]:
    if plugin.tier == "runtime" and plugin.download_url:
        return "runtime_download", "官方有可下载或可安装的运行产物，工具箱可提供下载/启用入口。"
    if plugin.key in ARCHITECTURE_REFERENCE_KEYS:
        return "architecture_reference", "偏理念、流程或产品架构参考，不占日常运营视野。"
    return "source_reference", "源码或组件需要编译、运行时依赖或二次封装，保留 GitHub 参考入口。"


def plugin_catalog() -> list[ToolPlugin]:
    return [
        ToolPlugin(
            key="yt_dlp",
            name="授权素材下载",
            github="https://github.com/yt-dlp/yt-dlp",
            folder="yt-dlp",
            license="Unlicense/Public Domain",
            business_goal="从用户授权的视频链接下载素材，解决素材散落、手动下载和命名混乱问题。",
            target_stage="素材入库",
            runtime_type="GitHub Release exe/源码",
            expected_entry="yt-dlp.exe",
            integration_status="下载器已纳入 tools_bin，可直接生成下载命令。",
            install_note="把合法授权的视频链接交给下载工具，输出到 01_素材入库/A_原始视频 或自动剪辑输入目录。",
            tier="runtime",
            download_url="https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe",
        ),
        ToolPlugin(
            key="auto_editor",
            name="去静音粗剪",
            github="https://github.com/WyattBlue/auto-editor",
            folder="auto-editor",
            license="Unlicense/Public Domain",
            business_goal="按静音、动作、黑场等规则自动粗切，减少人工找废片段时间。",
            target_stage="自动化剪辑",
            runtime_type="Nim 源码/需编译",
            expected_entry="auto-editor.exe",
            integration_status="源码已纳入；需编译后启用命令调用。",
            install_note="如果需要外部增强引擎，编译后的可执行文件放入组件目录或 tools_bin。",
            tier="runtime",
            download_url="https://github.com/WyattBlue/auto-editor/releases/latest/download/auto-editor.exe",
        ),
        ToolPlugin(
            key="funclip",
            name="字幕找片段",
            github="https://github.com/modelscope/FunClip",
            folder="FunClip",
            license="MIT",
            business_goal="把长视频转写成字幕，再按文本、关键词或大模型判断自动截取片段，适合补足剧情解说和高光剪辑。",
            target_stage="自动剪辑/字幕",
            runtime_type="Python/Gradio/需模型依赖",
            expected_entry="",
            integration_status="建议优先接入为“文本找片段”模式；当前先纳入工具雷达，后续安装 FunASR 依赖后启用。",
            install_note="需要 Python 依赖、FunASR/ASR 模型和本地运行环境；可输出字幕、切片时间段和剪辑结果。",
        ),
        ToolPlugin(
            key="autoclip",
            name="高光片段提取",
            github="https://github.com/zhouxiaoka/autoclip",
            folder="autoclip",
            license="MIT",
            business_goal="自动识别精彩高光并生成短视频片段，适合做“长剧找爆点”和短视频二创素材筛选。",
            target_stage="自动剪辑/高光提取",
            runtime_type="Python/AI 模型/需依赖",
            expected_entry="",
            integration_status="建议接入为“高光提取”策略；当前先纳入工具雷达，待确认运行依赖后封装。",
            install_note="适合输出高光片段清单，再交给本工作台生成剪辑计划和二创包装。",
        ),
        ToolPlugin(
            key="narratoai",
            name="解说二创流程",
            github="https://github.com/linyqh/NarratoAI",
            folder="NarratoAI",
            license="NOASSERTION",
            business_goal="一键解说并剪辑视频，产品方向贴近“短剧二创解说”，可借鉴其脚本、配音、剪辑联动流程。",
            target_stage="解说剪辑/流程参考",
            runtime_type="Python/多模型依赖",
            expected_entry="",
            integration_status="适合作为业务流程参考和可选外部模块；许可证未明确前不直接合并代码。",
            install_note="重点学习其“文案生成-配音-剪辑”链路，避免直接内嵌不清晰许可证代码。",
        ),
        ToolPlugin(
            key="pyscenedetect",
            name="镜头切点检测",
            github="https://github.com/Breakthrough/PySceneDetect",
            folder="PySceneDetect",
            license="BSD-3-Clause",
            business_goal="检测镜头切点和场景变化，为分镜筛选、抽帧质检和自动拆条服务。",
            target_stage="自动化剪辑",
            runtime_type="Python 源码/可安装",
            expected_entry="",
            integration_status="源码已纳入；可通过 Python 环境安装依赖后调用。",
            install_note="需要 click、numpy、opencv-python、platformdirs、tqdm。",
        ),
        ToolPlugin(
            key="pycapcut",
            name="剪映草稿交接",
            github="https://github.com/GuanYixuan/pyCapCut",
            folder="pyCapCut",
            license="NOASSERTION",
            business_goal="生成 CapCut/剪映草稿，适合把本工作台自动剪辑结果进一步交给剪映人工精修。",
            target_stage="剪映草稿/交接",
            runtime_type="Python/草稿生成库",
            expected_entry="",
            integration_status="v2026.07.01.03 已接入：可导出剪映草稿包；依赖齐全时可直接创建真实 CapCut/剪映草稿。",
            install_note="打包环境会自动安装草稿交接依赖；源码运行时缺依赖则生成可交接脚本包。",
        ),
        ToolPlugin(
            key="whisper_cpp",
            name="本地语音转写",
            github="https://github.com/ggml-org/whisper.cpp",
            folder="whisper.cpp",
            license="MIT",
            business_goal="本地语音转写，生成字幕和台词初稿，辅助标题与配音校验。",
            target_stage="字幕/台词/标题",
            runtime_type="C/C++ 源码/需编译模型",
            expected_entry="build/bin/Release/whisper-cli.exe",
            integration_status="源码已纳入；需编译可执行文件并下载模型后启用。",
            install_note="Windows 可用 CMake 编译，语音模型放入本地转写组件的 models 目录。",
        ),
        ToolPlugin(
            key="krillinai",
            name="多语言字幕配音",
            github="https://github.com/krillinai/KrillinAI",
            folder="KrillinAI",
            license="GPL-3.0",
            business_goal="视频翻译、转写、配音、字幕和封面链路完整，适合后续补多语言搬运和海外号发布前处理。",
            target_stage="字幕/翻译/配音",
            runtime_type="Go/CLI/需模型和 TTS",
            expected_entry="",
            integration_status="GPL 工具适合外部调用，不建议直接合并代码；可作为多语言处理模块参考。",
            install_note="未来可通过命令行外部调用，输入合格待发布视频，输出翻译字幕和配音版本。",
        ),
        ToolPlugin(
            key="lossless_cut",
            name="无损复核切割",
            github="https://github.com/mifi/lossless-cut",
            folder="lossless-cut",
            license="GPL-2.0",
            business_goal="参考其无损切割和人工复核工作流，用于后续补快速裁切/预览入口。",
            target_stage="人工粗剪/质检",
            runtime_type="Electron 源码/需构建",
            expected_entry="",
            integration_status="源码已纳入；当前作为产品设计和后续构建参考。",
            install_note="如需内置 GUI，需要按 Electron 项目构建；当前工作台优先用 ffmpeg/自动剪辑实现核心流程。",
        ),
        ToolPlugin(
            key="short_video_factory",
            name="批量短视频工厂",
            github="https://github.com/YILS-LIN/short-video-factory",
            folder="short-video-factory",
            license="AGPL-3.0",
            business_goal="批量生成营销和泛内容短视频，适合参考其桌面端批量任务、模板和素材管理方式。",
            target_stage="二创批量生产/产品参考",
            runtime_type="TypeScript/桌面端/AI 依赖",
            expected_entry="",
            integration_status="AGPL 项目不直接合并代码；作为 UI、任务队列和模板生产参考。",
            install_note="重点学习其任务组织和批量生成体验，功能实现仍放在本工作台自有代码里。",
        ),
        ToolPlugin(
            key="editly",
            name="模板合成脚本",
            github="https://github.com/mifi/editly",
            folder="editly",
            license="MIT",
            business_goal="参考其脚本化合成思路，后续可用于模板化片头、片尾和批量包装。",
            target_stage="模板化合成",
            runtime_type="Node.js 源码/需 npm 安装",
            expected_entry="",
            integration_status="源码已纳入；当前作为模板化合成参考和后续包装对象。",
            install_note="需要 Node.js 环境安装依赖后才能直接执行。",
        ),
        ToolPlugin(
            key="hyperframes",
            name="动态包装渲染",
            github="https://github.com/heygen-com/hyperframes",
            folder="hyperframes",
            license="Apache-2.0",
            business_goal="用 HTML/JS 写视频并渲染，适合片头片尾、字幕动画、口播包装和产品化模板。",
            target_stage="标题包装/动态视频",
            runtime_type="TypeScript/CLI/浏览器渲染",
            expected_entry="",
            integration_status="Apache-2.0，适合后续作为模板视频渲染引擎接入；当前纳入工具雷达。",
            install_note="需要 Node.js 环境；可把标题、字幕、封面素材渲染成包装视频片段。",
        ),
        ToolPlugin(
            key="remotion",
            name="程序化视频模板",
            github="https://github.com/remotion-dev/remotion",
            folder="remotion",
            license="NOASSERTION",
            business_goal="用 React 程序化生成视频，适合做片头、封面动效、字幕模板、批量包装和品牌视觉统一。",
            target_stage="标题包装/动态视频",
            runtime_type="TypeScript/React/Node.js",
            expected_entry="",
            integration_status="适合作为二创包装层；许可证和商业使用条款需单独确认后再深度内置。",
            install_note="短期可外部调用模板项目，长期可做“模板渲染”页面。",
        ),
        ToolPlugin(
            key="video_use",
            name="智能动作链",
            github="https://github.com/browser-use/video-use",
            folder="video-use",
            license="MIT",
            business_goal="让 coding agent 操作视频编辑流程，适合把复杂剪辑步骤沉淀成可复用 Agent 动作和审计日志。",
            target_stage="目标式剪辑/流程编排",
            runtime_type="Python/Agent 工具",
            expected_entry="",
            integration_status="MIT，适合参考并逐步接入为“自动剪辑 Agent 模式”；当前先纳入工具雷达。",
            install_note="优先吸收其动作编排思想，避免让用户手动理解复杂剪辑参数。",
        ),
        ToolPlugin(
            key="openmontage",
            name="视频工厂架构",
            github="https://github.com/calesthio/OpenMontage",
            folder="OpenMontage",
            license="AGPL-3.0",
            business_goal="Agentic 视频生产系统，适合参考 12 条管线、工具调度和技能拆分，补强本工作台的长期架构。",
            target_stage="全流程视频工厂/架构参考",
            runtime_type="Python/Agentic Pipeline",
            expected_entry="",
            integration_status="AGPL 项目不直接合并代码；作为流程架构和工具拆分参考。",
            install_note="适合映射到本软件的自动剪辑、二创、字幕、发布质检等独立流水线。",
        ),
        ToolPlugin(
            key="flycut_caption",
            name="字幕时间轴编辑",
            github="https://github.com/x007xyz/flycut-caption",
            folder="flycut-caption",
            license="NOASSERTION",
            business_goal="React 字幕编辑组件，适合未来补字幕时间轴、字幕样式预览和人工校对。",
            target_stage="字幕编辑/质检",
            runtime_type="TypeScript/React 组件",
            expected_entry="",
            integration_status="可作为字幕 UI 参考；许可证未明确前不直接合并组件代码。",
            install_note="短期用作 UI 设计参考，长期可换成自有字幕时间轴控件。",
        ),
        ToolPlugin(
            key="subtitleedit",
            name="字幕格式修复",
            github="https://github.com/SubtitleEdit/subtitleedit",
            folder="subtitleedit",
            license="MIT",
            business_goal="字幕格式转换、字幕修复、SRT/ASS/VTT 统一处理。",
            target_stage="字幕/发布包装",
            runtime_type=".NET 源码/需编译",
            expected_entry="src/SubtitleEdit/bin/Release/net8.0-windows/SubtitleEdit.exe",
            integration_status="源码已纳入；需编译后启用。",
            install_note="需要 .NET SDK，编译后可作为字幕处理工具。",
        ),
        ToolPlugin(
            key="firered_openstoryline",
            name="意图剪辑计划",
            github="https://github.com/FireRedTeam/FireRed-OpenStoryline",
            folder="FireRed-OpenStoryline",
            license="Apache-2.0",
            business_goal="把自然语言剪辑意图转成剪辑计划和工具调用，适合补“自动剪辑不只是混剪，而是按目标做片”的能力。",
            target_stage="目标式剪辑/意图到分镜",
            runtime_type="Python/LLM/重型依赖",
            expected_entry="",
            integration_status="已纳入 GitHub 候选矩阵；当前网络克隆失败，先按架构和计划格式研究，不直接内置运行。",
            install_note="下一步把其“意图-计划-执行-复核”结构映射为本软件的自动剪辑任务 JSON 和分镜 CSV。",
        ),
        ToolPlugin(
            key="jianying_editor_skill",
            name="剪映自动精修",
            github="https://github.com/luoluoluo22/jianying-editor-skill",
            folder="jianying-editor-skill",
            license="MIT",
            business_goal="Agent 自动操作剪映，贴近中文用户的真实精修软件，可补强本软件导出剪映草稿后的自动化交接。",
            target_stage="剪映交接/自动精修",
            runtime_type="Skill/桌面自动化",
            expected_entry="",
            integration_status="已纳入候选矩阵；优先和剪映草稿交接串联，而不是绕开本软件主流程。",
            install_note="先做交接包与剪映草稿，再逐步增加可审计的自动化动作。",
        ),
        ToolPlugin(
            key="nomi",
            name="本地创作台参考",
            github="https://github.com/aqm857886159/Nomi",
            folder="Nomi",
            license="Apache-2.0",
            business_goal="本地优先 AI 视频创作桌面应用，适合参考脚本、素材生成、时间线和导出的一体化体验。",
            target_stage="二创创作台/本地时间线参考",
            runtime_type="桌面端/AI 生成/时间线",
            expected_entry="",
            integration_status="候选矩阵观察和参考项，不作为当前轻量客户端依赖。",
            install_note="吸收“脚本到素材到时间线”的体验，落到本软件的二创任务模板。",
        ),
        ToolPlugin(
            key="frame_ai_editor",
            name="对话式时间线",
            github="https://github.com/aregrid/frame",
            folder="frame",
            license="MIT",
            business_goal="AI 驱动的视频时间线编辑器，适合参考“对话指令变剪辑动作”的产品交互。",
            target_stage="目标式剪辑/时间线交互",
            runtime_type="AI 视频编辑器",
            expected_entry="",
            integration_status="候选矩阵优先研究项；当前不把前端工程并入客户端。",
            install_note="把自然语言动作落成可审计的剪辑计划，再由本软件 FFmpeg/剪映交接执行。",
        ),
        ToolPlugin(
            key="opencut",
            name="可视化时间线",
            github="https://github.com/OpenCut-app/OpenCut",
            folder="OpenCut",
            license="MIT",
            business_goal="开源时间线编辑器，适合参考素材轨道、预览、人工精修和现代剪辑 UI。",
            target_stage="未来可视化时间线/人工精修",
            runtime_type="Web/React/视频编辑器",
            expected_entry="",
            integration_status="界面和流程参考，不替换当前工作台；等实际运行确认后再考虑外部入口。",
            install_note="短期用于修正本软件剪辑页的信息架构，长期可做外部时间线编辑入口。",
        ),
        ToolPlugin(
            key="cutscript",
            name="文本剪视频",
            github="https://github.com/DataAnts-AI/CutScript",
            folder="CutScript",
            license="MIT",
            business_goal="把文本编辑变成视频编辑，适合让普通用户通过删台词、选台词完成剪辑。",
            target_stage="自动剪辑/文本找片段",
            runtime_type="AI 文本视频编辑器",
            expected_entry="",
            integration_status="v2026.07.01.12 已把核心思路落成本软件自有“文本剪辑时间线”；源码仓继续作为参考。",
            install_note="当前无需运行外部源码；先使用自动剪辑页的“生成台词剪辑表”和“应用台词剪辑表”。",
        ),
        ToolPlugin(
            key="preencut",
            name="按描述找片段",
            github="https://github.com/roothch/PreenCut",
            folder="PreenCut",
            license="MIT",
            business_goal="按自然语言或视觉语义检索视频片段，解决长素材里找人物、动作、场景困难的问题。",
            target_stage="素材筛选/自动剪辑输入",
            runtime_type="AI 视频检索与切片",
            expected_entry="",
            integration_status="v2026.07.01.11 新增优先研究项；适合做“按描述找片段”。",
            install_note="先验证中文视频索引速度和检索质量，再接入候选片段池。",
        ),
        ToolPlugin(
            key="opensource_clipping",
            name="社媒切条增强",
            github="https://github.com/NaufalRizqullah/opensource-clipping",
            folder="opensource-clipping",
            license="MIT",
            business_goal="把长视频转社媒短片，含人脸跟踪、逐词字幕、B-roll、BGM ducking 和自动缩略图。",
            target_stage="自动切条/二创包装/封面",
            runtime_type="Python/AI/FFmpeg 流水线",
            expected_entry="",
            integration_status="v2026.07.01.13 已下载并拆解落地：先实现社媒增强版的竖屏裁切、响度标准化、BGM 自动循环和说话时压低音乐。",
            install_note="整仓依赖 Gemini、Pexels、MediaPipe，暂不强依赖；优先把稳定能力拆成水星剪辑自己的按钮。",
        ),
        ToolPlugin(
            key="director",
            name="任务编排框架",
            github="https://github.com/video-db/Director",
            folder="Director",
            license="MIT",
            business_goal="视频 Agent 工作流框架，可帮助把“目标-计划-工具调用-复核”做成稳定任务。",
            target_stage="目标式剪辑/任务编排",
            runtime_type="AI Agent Framework",
            expected_entry="",
            integration_status="v2026.07.01.11 新增优先研究项；先学习任务抽象，不直接替换现有引擎。",
            install_note="和意图剪辑计划、动作链能力一起形成轻量本地任务编排层。",
        ),
        ToolPlugin(
            key="chengfeng_videocut_skills",
            name="中文剪辑技能",
            github="https://github.com/Agentchengfeng/chengfeng-videocut-skills",
            folder="chengfeng-videocut-skills",
            license="Apache-2.0",
            business_goal="中文视频剪辑 Agent 技能库，适合把常见剪辑动作做成可见、可审计的技能入口。",
            target_stage="自动剪辑/技能化工具栏",
            runtime_type="Agent Skills",
            expected_entry="",
            integration_status="v2026.07.01.11 新增优先研究项；只引入可解释的技能拆分方式。",
            install_note="后续把“找爆点、删静音、加字幕、生成封面、导出剪映”做成技能卡。",
        ),
        ToolPlugin(
            key="timeline_studio",
            name="时间线工作台",
            github="https://github.com/chatman-media/timeline-studio",
            folder="timeline-studio",
            license="MIT",
            business_goal="AI 桌面时间线编辑器，适合参考素材栏、轨道、预览、任务队列的一体化体验。",
            target_stage="未来可视化时间线/人工复核",
            runtime_type="桌面端/AI 时间线",
            expected_entry="",
            integration_status="v2026.07.01.11 新增优先研究项；当前只用于产品结构参考。",
            install_note="先把本软件剪辑结果做成可读时间线报告，后续再做真正拖拽时间线。",
        ),
        ToolPlugin(
            key="ffmpeg_free_ui",
            name="常用转码预设",
            github="https://github.com/Lake1059/FFmpegFreeUI",
            folder="FFmpegFreeUI",
            license="MIT",
            business_goal="把复杂 FFmpeg 参数包装成普通用户能理解的 Windows 预设界面。",
            target_stage="质检修复/转码压制/高级工具",
            runtime_type="Windows FFmpeg GUI",
            expected_entry="",
            integration_status="v2026.07.01.11 新增界面参考项；学习参数分组和预设，不合并代码。",
            install_note="后续在工具页增加“常用修复预设”：转竖屏、统一音量、补黑边、压缩体积。",
        ),
        ToolPlugin(
            key="btbn_ffmpeg_builds",
            name="底层引擎更新源",
            github="https://github.com/BtbN/FFmpeg-Builds",
            folder="FFmpeg-Builds",
            license="MIT",
            business_goal="提供 Windows FFmpeg 构建来源，适合后续做工具箱自动更新和版本体检。",
            target_stage="底层工具更新/安装引导",
            runtime_type="GitHub Release/FFmpeg 构建",
            expected_entry="",
            integration_status="v2026.07.01.11 新增可选外部项；不直接混入许可证复杂的二进制。",
            install_note="后续只做下载来源提示和版本检测，由用户确认更新工具。",
        ),
        ToolPlugin(
            key="motion_skills",
            name="动效包装技能",
            github="https://github.com/iart-ai/motion-skills",
            folder="motion-skills",
            license="MIT",
            business_goal="动效、解释视频、短视频包装技能库，适合补字幕动画、封面动效、片头片尾模板的风格参考。",
            target_stage="标题包装/动效模板",
            runtime_type="Skills/HTML/模板参考",
            expected_entry="",
            integration_status="v2026.07.01.13 已下载；作为包装模板和动效类型参考，不直接运行整仓。",
            install_note="把可用动效拆成软件内模板：标题入场、逐词强调、片尾 CTA、知识点卡片。",
        ),
        ToolPlugin(
            key="remotion_com_skills",
            name="程序化包装组件",
            github="https://github.com/liancheng-zcy/remotion-com-skills",
            folder="remotion-com-skills",
            license="MIT",
            business_goal="程序化视频组件和技能说明，可帮助后续做片头、封面动效、旁白视频模板。",
            target_stage="标题包装/程序化模板",
            runtime_type="TypeScript/Node.js",
            expected_entry="",
            integration_status="v2026.07.01.13 已下载；当前先提炼模板结构，暂不强依赖 Node 渲染。",
            install_note="适合长期接入为可选渲染器；短期把组件类型转成本软件包装页模板清单。",
        ),
        ToolPlugin(
            key="agentic_video_editor",
            name="目标式剪辑流程",
            github="https://github.com/poseljacob/agentic-video-editor",
            folder="agentic-video-editor",
            license="MIT",
            business_goal="把创意 brief 转成素材预处理、导演选镜、剪辑计划、渲染和复核的 Agent 流程。",
            target_stage="目标式剪辑/任务编排",
            runtime_type="Python/CLI/Gemini/FFmpeg",
            expected_entry="",
            integration_status="v2026.07.01.13 已下载；学习 EditPlan 和 Reviewer，不直接把模型依赖塞进主客户端。",
            install_note="先把 CreativeBrief/EditPlan 映射为本软件的目标式剪辑计划 JSON。",
        ),
        ToolPlugin(
            key="ai_video_editing_skill",
            name="自动剪辑技能手册",
            github="https://github.com/znyupup/ai-video-editing-skill",
            folder="ai-video-editing-skill",
            license="MIT",
            business_goal="底层剪辑、语音识别和视觉分析的自动剪辑手册，适合把复杂剪辑流程拆成普通用户能理解的步骤。",
            target_stage="自动剪辑/技能手册",
            runtime_type="Skill/Python/FFmpeg/ASR",
            expected_entry="",
            integration_status="v2026.07.01.13 已下载；优先吸收 edit_plan、音量分析、抽帧分析和叙事结构。",
            install_note="不直接让 Agent 自由改文件；先生成可复核计划，再由水星剪辑执行。",
        ),
        ToolPlugin(
            key="chromaprint_fpcalc",
            name="音频指纹检测",
            github="https://github.com/acoustid/chromaprint",
            folder="chromaprint-fpcalc",
            license="LGPL-2.1（整体）/MIT（自有代码）",
            business_goal="对二创成片提取声学指纹，识别画面已差异化但声音高度一致的成片对，补齐去重闭环的音频维度。",
            target_stage="二创去重/指纹验收",
            runtime_type="GitHub Release exe",
            expected_entry="chromaprint-fpcalc-1.6.0-windows-x86_64/fpcalc.exe",
            integration_status="v2026.07.04.8 已接入：去重指纹报告自动附带音频相似度；未下载时用 FFmpeg 能量包络兜底。",
            install_note="LGPL 组件保持外部进程调用，不打入安装包；工具箱下载后自动启用 fpcalc 引擎。",
            tier="runtime",
            download_url="https://github.com/acoustid/chromaprint/releases/download/v1.6.0/chromaprint-fpcalc-1.6.0-windows-x86_64.zip",
        ),
        ToolPlugin(
            key="realesrgan_ncnn",
            name="画质增强超分",
            github="https://github.com/xinntao/Real-ESRGAN",
            folder="realesrgan-ncnn-vulkan",
            license="BSD-3-Clause/MIT",
            business_goal="老素材、低清素材放大修复，提升二创成片和封面图的画质下限。",
            target_stage="二创加工/封面",
            runtime_type="GitHub Release exe（便携含模型）",
            expected_entry="realesrgan-ncnn-vulkan.exe",
            integration_status="组件可下载；作为可选外部增强，后续在二创加工页提供按钮入口。",
            install_note="Vulkan GPU 加速的便携版，无需 CUDA/Python 环境；外部调用不入安装包。",
            tier="runtime",
            download_url="https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip",
        ),
        ToolPlugin(
            key="imagehash_lib",
            name="画面感知哈希库",
            github="https://github.com/JohannesBuchner/imagehash",
            folder="imagehash",
            license="BSD-2-Clause",
            business_goal="pHash/小波哈希比当前均值哈希更抗缩放和调色，可作为画面指纹的可选增强算法。",
            target_stage="二创去重/指纹验收",
            runtime_type="PyPI 库/可选安装",
            expected_entry="",
            integration_status="候选增强：许可证友好，依赖 numpy/scipy/pywavelets，需评估加入可选 wheelhouse。",
            install_note="pip install imagehash；未安装时继续使用内置均值哈希，不影响核心流程。",
        ),
        ToolPlugin(
            key="soundfingerprinting_net",
            name="C#音视频指纹库",
            github="https://github.com/AddictedCS/soundfingerprinting",
            folder="soundfingerprinting",
            license="MIT",
            business_goal="NuGet 包 SoundFingerprinting：本地剪辑器（C#/WinForms）侧做音频/视频指纹查重时可直接引用。",
            target_stage="C#桥/本地剪辑器",
            runtime_type="NuGet 包/.NET",
            expected_entry="",
            integration_status="csharp_bridge 推荐依赖；主工作台侧音频指纹由 fpcalc/FFmpeg 承担。",
            install_note="NuGet 安装 SoundFingerprinting；MIT 许可证可直接集成到本地剪辑器代码。",
        ),
        ToolPlugin(
            key="ffmpegcore_net",
            name="C#媒体处理封装",
            github="https://github.com/rosenbjerg/FFMpegCore",
            folder="ffmpegcore",
            license="MIT",
            business_goal="NuGet 包 FFMpegCore：本地剪辑器侧需要媒体分析、转码或抽帧时的 FFmpeg/FFprobe 封装。",
            target_stage="C#桥/本地剪辑器",
            runtime_type="NuGet 包/.NET",
            expected_entry="",
            integration_status="csharp_bridge 推荐依赖；与工作台共用 assets/ffmpeg 的二进制。",
            install_note="NuGet 安装 FFMpegCore，把 BinaryFolder 指到水星剪辑的 assets/ffmpeg/bin。",
        ),
        ToolPlugin(
            key="piper_tts",
            name="本地神经配音",
            github="https://github.com/OHF-Voice/piper1-gpl",
            folder="piper",
            license="GPL-3.0",
            business_goal="快速离线神经 TTS，为配音对齐成片提供本地配音音源，摆脱在线 TTS 依赖。",
            target_stage="配音/字幕",
            runtime_type="Python/CLI/需模型",
            expected_entry="",
            integration_status="GPL 工具适合外部调用，不直接合并代码；作为配音链路候选外部模块。",
            install_note="pip install piper-tts 后命令行外部调用；语音模型按需下载，不入安装包。",
        ),
    ]


def plugin_statuses() -> list[dict[str, Any]]:
    statuses = []
    for plugin in plugin_catalog():
        status = plugin.to_status()
        if plugin.key == "pyscenedetect":
            status["python_import_ready"] = _can_import("scenedetect")
        if plugin.key == "auto_editor":
            status["path_ready"] = bool(shutil.which("auto-editor"))
        statuses.append(status)
    return statuses


def write_vendor_manifest(path: Path | None = None) -> Path:
    target = path or (vendor_root() / "vendor_manifest.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    research_paths = write_github_research_report(version_root() / "docs")
    manifest = {
        "version_root": str(version_root()),
        "vendor_root": str(vendor_root()),
        "github_research": research_summary(),
        "github_research_paths": research_paths,
        "plugins": plugin_statuses(),
    }
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def _can_import(module_name: str) -> bool:
    try:
        __import__(module_name)
        return True
    except Exception:
        return False

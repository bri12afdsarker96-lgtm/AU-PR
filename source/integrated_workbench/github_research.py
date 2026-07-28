from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


SEARCH_URLS = {
    "AI video editing": "https://github.com/search?q=AI+video+editing&type=repositories",
    "Auto video editing": "https://github.com/search?q=Auto+video+editing&type=repositories",
    "automated video editing": "https://github.com/search?q=automated+video+editing&type=repositories",
    "video clipping": "https://github.com/search?q=video+clipping&type=repositories",
    "AI Agent video": "https://github.com/search?q=AI+Agent+video&type=repositories",
    "Agentic video": "https://github.com/search?q=Agentic+video&type=repositories",
    "React video": "https://github.com/search?q=React+video&type=repositories",
    "FFmpeg": "https://github.com/search?q=FFmpeg&type=repositories",
    "remotion skills": "https://github.com/search?q=remotion+skills&type=repositories",
    "openmontage": "https://github.com/search?q=openmontage&type=repositories",
}


FRIENDLY_REPO_NAMES = {
    "WyattBlue/auto-editor": "去静音粗剪",
    "modelscope/FunClip": "字幕找片段",
    "zhouxiaoka/autoclip": "高光片段提取",
    "FireRedTeam/FireRed-OpenStoryline": "意图剪辑计划",
    "browser-use/video-use": "智能动作链",
    "luoluoluo22/jianying-editor-skill": "剪映自动精修",
    "GuanYixuan/pyCapCut": "剪映草稿交接",
    "DataAnts-AI/CutScript": "文本剪视频",
    "roothch/PreenCut": "按描述找片段",
    "NaufalRizqullah/opensource-clipping": "社媒切条增强",
    "Agentchengfeng/chengfeng-videocut-skills": "中文剪辑技能",
    "video-db/Director": "任务编排框架",
    "chatman-media/timeline-studio": "时间线工作台",
}


@dataclass(frozen=True)
class RepositoryCandidate:
    query: str
    repo: str
    github: str
    stars_seen: int
    license: str
    category: str
    decision: str
    usable_for: str
    integration_target: str
    next_action: str
    risk: str


@dataclass(frozen=True)
class SearchResultReview:
    query: str
    repo: str
    verdict: str
    reason: str


def version_root() -> Path:
    return Path(__file__).resolve().parents[2]


CANDIDATES: tuple[RepositoryCandidate, ...] = (
    RepositoryCandidate(
        "AI video editing",
        "linyqh/NarratoAI",
        "https://github.com/linyqh/NarratoAI",
        10093,
        "NOASSERTION",
        "解说二创流程",
        "流程参考",
        "一键解说、配音、剪辑链路接近本软件业务。",
        "二创生成、标题文案、自动剪辑任务编排",
        "拆业务流程，不直接合并代码；把脚本-配音-剪辑的顺序沉淀为本软件模板。",
        "许可证不明确且依赖重，不能直接内置。",
    ),
    RepositoryCandidate(
        "AI video editing",
        "FireRedTeam/FireRed-OpenStoryline",
        "https://github.com/FireRedTeam/FireRed-OpenStoryline",
        3026,
        "Apache-2.0",
        "AI 剪辑 Agent",
        "优先研究",
        "自然语言意图到剪辑计划、工具编排、人机协同审稿。",
        "自动剪辑入口、Agent 任务计划、审核解释",
        "抽取“意图-计划-执行-复核”的结构，先转成剪辑计划 JSON/CSV。",
        "模型与工具链较重，不能直接当本地轻量功能启用。",
    ),
    RepositoryCandidate(
        "AI video editing",
        "visomaster/VisoMaster",
        "https://github.com/visomaster/VisoMaster",
        1952,
        "GPL-3.0",
        "换脸编辑",
        "暂不接入",
        "视频换脸与人脸编辑，不是当前剪辑和二创主痛点。",
        "无",
        "保留为特殊效果观察项。",
        "GPL，且功能方向容易让主流程跑偏。",
    ),
    RepositoryCandidate(
        "AI video editing",
        "x007xyz/flycut-caption",
        "https://github.com/x007xyz/flycut-caption",
        1677,
        "NOASSERTION",
        "字幕编辑 UI",
        "界面参考",
        "字幕时间轴、语音识别、可视化字幕编辑。",
        "字幕校对、发布前质检、字幕样式编辑",
        "参考 UI 结构，自研轻量字幕检查入口。",
        "许可证不明确，不能合并组件代码。",
    ),
    RepositoryCandidate(
        "AI video editing",
        "ncounterspecialist/twick",
        "https://github.com/ncounterspecialist/twick",
        504,
        "NOASSERTION",
        "React 时间线 SDK",
        "观察",
        "React 画布时间线、AI 字幕、导出能力。",
        "未来可视化时间线",
        "只记录到工具雷达；等许可证清晰后再评估。",
        "许可证不明确。",
    ),
    RepositoryCandidate(
        "AI video editing",
        "aqm857886159/Nomi",
        "https://github.com/aqm857886159/Nomi",
        241,
        "Apache-2.0",
        "本地 AI 视频创作",
        "优先研究",
        "脚本到图片、视频、时间线、导出的本地优先桌面形态。",
        "二创创作台、素材生成到剪辑的闭环",
        "参考本地项目组织和时间线设计，不替换当前 Python 主流程。",
        "重桌面应用，依赖模型和 API key。",
    ),
    RepositoryCandidate(
        "AI video editing",
        "aregrid/frame",
        "https://github.com/aregrid/frame",
        235,
        "MIT",
        "AI 时间线编辑器",
        "优先研究",
        "用自然语言驱动专业视频切片与时间线交互。",
        "自动剪辑 Agent、可视化时间线",
        "抽取“对话指令转剪辑动作”的交互模式。",
        "项目成熟度仍需实际运行确认。",
    ),
    RepositoryCandidate(
        "Auto video editing",
        "WyattBlue/auto-editor",
        "https://github.com/WyattBlue/auto-editor",
        4498,
        "Unlicense",
        "规则自动粗剪",
        "已接入/增强",
        "按静音、动作、黑场自动粗剪，直接解决废片段剔除。",
        "自动剪辑入口、去静音粗剪",
        "本软件已用 FFmpeg 实现基础去静音；后续可在 CLI 可用时切到 auto-editor 引擎。",
        "需要用户环境有 CLI 或编译产物。",
    ),
    RepositoryCandidate(
        "Auto video editing",
        "DevonCrawford/Video-Editing-Automation",
        "https://github.com/DevonCrawford/Video-Editing-Automation",
        1195,
        "GPL-3.0",
        "自动剪辑算法示例",
        "算法参考",
        "示例级自动化剪辑算法。",
        "自动剪辑策略库",
        "只参考算法思路，不合并代码。",
        "GPL，且更像演示项目。",
    ),
    RepositoryCandidate(
        "Auto video editing",
        "OpenNewsLabs/autoEdit_2",
        "https://github.com/OpenNewsLabs/autoEdit_2",
        454,
        "MIT",
        "文本剪视频",
        "参考",
        "按文本转写编辑视频的老牌思路。",
        "字幕关键词、文本找片段",
        "吸收“文本就是剪辑入口”的产品逻辑。",
        "项目较老，不适合直接依赖。",
    ),
    RepositoryCandidate(
        "automated video editing",
        "luoluoluo22/jianying-editor-skill",
        "https://github.com/luoluoluo22/jianying-editor-skill",
        2238,
        "MIT",
        "剪映 Agent 技能",
        "优先研究",
        "Agent 自动操作剪映，贴近中文用户真实工作流。",
        "剪映草稿交接、自动剪辑到剪映",
        "和 pyCapCut 草稿包一起形成“自动生成-剪映精修”链路。",
        "需要核对实际可操作接口和剪映版本差异。",
    ),
    RepositoryCandidate(
        "automated video editing",
        "GuanYixuan/pyCapCut",
        "https://github.com/GuanYixuan/pyCapCut",
        570,
        "NOASSERTION",
        "剪映/CapCut 草稿",
        "已落地",
        "用 Python 生成剪映/CapCut 草稿，适合交接给人工精修。",
        "发布交接、自动剪辑导出",
        "v2026.07.01.03 已实现草稿包和真实草稿创建。",
        "许可证不明确，保持外部适配和依赖调用。",
    ),
    RepositoryCandidate(
        "video clipping",
        "modelscope/FunClip",
        "https://github.com/modelscope/FunClip",
        5873,
        "MIT",
        "ASR 文本切片",
        "优先接入",
        "转写、字幕、按文字和 LLM 辅助切片。",
        "自动剪辑入口、字幕关键词、剧情高能片段",
        "先把输出时间段转成剪辑计划 CSV，再交给现有 FFmpeg 剪辑器。",
        "ASR 模型和依赖较重，需要做可选安装。",
    ),
    RepositoryCandidate(
        "video clipping",
        "zhouxiaoka/autoclip",
        "https://github.com/zhouxiaoka/autoclip",
        5861,
        "MIT",
        "AI 高光切片",
        "优先接入",
        "智能高光提取和二创切片。",
        "自动剪辑入口、高光粗剪、二创素材筛选",
        "封装为“高光提取”策略，输出候选片段清单。",
        "模型依赖和运行耗时需实测。",
    ),
    RepositoryCandidate(
        "video clipping",
        "YILS-LIN/short-video-factory",
        "https://github.com/YILS-LIN/short-video-factory",
        4135,
        "AGPL-3.0",
        "批量短视频工厂",
        "产品参考",
        "批量任务、模板、素材组织很贴近当前产品目标。",
        "二创生成、任务队列、桌面 UI",
        "参考任务队列和模板体验，不合并代码。",
        "AGPL，不适合内置到闭源桌面客户端。",
    ),
    RepositoryCandidate(
        "AI Agent video",
        "heygen-com/hyperframes",
        "https://github.com/heygen-com/hyperframes",
        32339,
        "Apache-2.0",
        "HTML 视频渲染",
        "优先接入",
        "用 HTML 写视频，适合 Agent 生成片头片尾、字幕动画、包装片段。",
        "标题封面、片头片尾、模板化包装",
        "先作为可选模板渲染引擎，输出 mp4 再回流到二创。",
        "需要 Node.js、浏览器渲染和模板工程。",
    ),
    RepositoryCandidate(
        "AI Agent video",
        "calesthio/OpenMontage",
        "https://github.com/calesthio/OpenMontage",
        29719,
        "AGPL-3.0",
        "Agentic 视频工厂",
        "架构参考",
        "多管线、多工具、多技能的视频生产系统。",
        "长期架构、Agent 任务拆解",
        "学习“管线-工具-技能”的拆分方式，映射到本软件六大板块。",
        "AGPL 且体量巨大，不直接内置。",
    ),
    RepositoryCandidate(
        "AI Agent video",
        "waooAI/waoowaoo",
        "https://github.com/waooAI/waoowaoo",
        12994,
        "NOASSERTION",
        "AI 影视平台",
        "观察",
        "工业级影视生产平台概念，对短剧生产有参考价值。",
        "长线创作、AI 影视流程",
        "仅记录为观察项。",
        "许可证和依赖不清晰。",
    ),
    RepositoryCandidate(
        "Agentic video",
        "browser-use/video-use",
        "https://github.com/browser-use/video-use",
        12481,
        "MIT",
        "Agent 剪视频",
        "优先研究",
        "让 coding agent 操作视频编辑流程。",
        "自动剪辑 Agent、操作审计",
        "把复杂参数封装成目标式动作：找高光、剪 5 条、生成预览、等待审核。",
        "需要把 Agent 行为限制在可审计任务内。",
    ),
    RepositoryCandidate(
        "React video",
        "remotion-dev/remotion",
        "https://github.com/remotion-dev/remotion",
        0,
        "Remotion License",
        "React 程序化视频",
        "可选外部",
        "React 生成视频，适合封面动效、字幕动效、片头片尾。",
        "标题封面、模板包装",
        "先保留外部模板渲染入口；商业条款确认后再深度内置。",
        "商业授权条款需要单独确认。",
    ),
    RepositoryCandidate(
        "React video",
        "OpenCut-app/OpenCut",
        "https://github.com/OpenCut-app/OpenCut",
        0,
        "MIT",
        "开源时间线编辑器",
        "界面参考",
        "时间线、素材轨道、预览导出的现代视频编辑体验。",
        "未来可视化时间线、人工精修",
        "参考 UI 和工作流，不替换当前轻量桌面端。",
        "需要实际克隆和运行确认。",
    ),
    RepositoryCandidate(
        "FFmpeg",
        "FFmpeg/FFmpeg",
        "https://github.com/FFmpeg/FFmpeg",
        61586,
        "LGPL/GPL",
        "底层视频引擎",
        "已内置",
        "剪切、转码、抽帧、混流、滤镜、质检。",
        "全流程底层能力",
        "继续以外部可执行文件调用，避免许可证混入主程序。",
        "需区分 LGPL/GPL 构建。",
    ),
    RepositoryCandidate(
        "FFmpeg",
        "mifi/lossless-cut",
        "https://github.com/mifi/lossless-cut",
        41771,
        "GPL-2.0",
        "无损切割 UI",
        "界面参考",
        "人工复核、快速切割、无损处理体验优秀。",
        "审核中心、人工精修入口",
        "参考交互，不合并代码；必要时外部打开。",
        "GPL。",
    ),
    RepositoryCandidate(
        "FFmpeg",
        "mifi/editly",
        "https://github.com/mifi/editly",
        0,
        "MIT",
        "声明式合成",
        "参考",
        "用 JSON/JS 描述视频合成。",
        "模板化包装、片头片尾",
        "参考 DSL 思想，形成本软件自己的模板配置。",
        "Node 依赖，项目维护状态需关注。",
    ),
    RepositoryCandidate(
        "remotion skills",
        "wshuyi/remotion-video-skill",
        "https://github.com/wshuyi/remotion-video-skill",
        303,
        "NOASSERTION",
        "Remotion 技能",
        "参考",
        "把程序化视频生成拆成可复用技能。",
        "模板包装、Agent 生成视频",
        "参考技能说明结构，不依赖代码。",
        "许可证不明确。",
    ),
    RepositoryCandidate(
        "remotion skills",
        "iart-ai/motion-skills",
        "https://github.com/iart-ai/motion-skills",
        227,
        "MIT",
        "动效技能库",
        "优先研究",
        "文字动效、解释视频、短视频动效技能。",
        "标题封面、字幕动画、片头片尾",
        "抽取可复用动效模板规范。",
        "需要筛选适合竖屏短剧的模板。",
    ),
    RepositoryCandidate(
        "openmontage",
        "calesthio/OpenMontage forks",
        "https://github.com/search?q=openmontage&type=repositories",
        0,
        "mixed",
        "Fork/镜像",
        "不接入",
        "多数是 fork、镜像或测试仓。",
        "无",
        "只保留主仓分析，不把 fork 列为候选。",
        "重复、低维护、授权不一。",
    ),
)


EXTRA_CANDIDATES: tuple[RepositoryCandidate, ...] = (
    RepositoryCandidate(
        "AI video editing",
        "chatman-media/timeline-studio",
        "https://github.com/chatman-media/timeline-studio",
        179,
        "MIT",
        "AI 桌面时间线",
        "优先研究",
        "本地桌面时间线、AI 辅助编辑和素材管理，能补当前软件缺少的可视化精修感。",
        "未来可视化时间线、人工复核、素材管理",
        "先抽交互结构：素材栏、时间线轨道、预览区、任务队列；不直接迁移前端。",
        "桌面技术栈不同，直接合并成本高。",
    ),
    RepositoryCandidate(
        "AI video editing",
        "DataAnts-AI/CutScript",
        "https://github.com/DataAnts-AI/CutScript",
        150,
        "MIT",
        "文本剪视频",
        "已落地",
        "按文字编辑视频，正好补“普通用户不知道怎么设剪辑参数”的痛点。",
        "字幕关键词、文本剪辑时间线、自动剪辑入口",
        "v2026.07.01.12 已落地：字幕草稿生成台词剪辑表，用户改“保留”列后可直接应用为剪辑计划。",
        "后续仍需补可视化勾选和批量删除体验。",
    ),
    RepositoryCandidate(
        "AI video editing",
        "znyupup/ai-video-editing-skill",
        "https://github.com/znyupup/ai-video-editing-skill",
        75,
        "MIT",
        "Agent 剪辑技能",
        "优先研究",
        "FFmpeg、Whisper、视觉 API 组合成 vlog 自动剪辑动作链。",
        "自动剪辑 Agent、任务审计、剪辑说明",
        "参考它的“原片输入-转写-分句-合并-字幕”技能拆分，转成本软件可审计任务。",
        "依赖外部视觉模型，不能默认强制安装。",
    ),
    RepositoryCandidate(
        "Auto video editing",
        "maxazure/video-editing-skill",
        "https://github.com/maxazure/video-editing-skill",
        104,
        "NOASSERTION",
        "口播/访谈粗剪",
        "参考",
        "面向 talk/vlog 的转写、分句、字幕烧录和合并流程。",
        "口播视频自动剪辑、字幕烧录、废话裁剪",
        "参考流程，把“句子级剪辑”作为自动剪辑的下一档精细模式。",
        "许可证不明确，不能合并代码。",
    ),
    RepositoryCandidate(
        "Auto video editing",
        "jappeace/cut-the-crap",
        "https://github.com/jappeace/cut-the-crap",
        115,
        "MIT",
        "直播废片段粗剪",
        "参考",
        "面向主播的自动删空白、删无效段落，适合补长素材粗清洗。",
        "自动剪辑入口、长视频预处理",
        "把它归入“长视频预清洗”策略，优先用本地 FFmpeg 静音/黑场检测实现。",
        "使用场景偏直播，不完全匹配短剧二创。",
    ),
    RepositoryCandidate(
        "Auto video editing",
        "eddieoz/youtube-clips-automator",
        "https://github.com/eddieoz/youtube-clips-automator",
        162,
        "MIT",
        "AI 切片与缩略图",
        "流程参考",
        "从长视频自动生成短切片和缩略图，能补封面与切片联动。",
        "自动切条、标题封面、发布交接",
        "参考“切片后立刻生成封面/标题”的闭环，接到现有包装成片和发布队列。",
        "偏 YouTube 频道自动化，上传部分不直接接入。",
    ),
    RepositoryCandidate(
        "automated video editing",
        "SaarD00/AI-Youtube-Shorts-Generator",
        "https://github.com/SaarD00/AI-Youtube-Shorts-Generator",
        155,
        "MIT",
        "无露脸短视频工厂",
        "流程参考",
        "热点选题、TTS、动态 FFmpeg 编辑组成全自动短视频生产线。",
        "二创生成、配音、模板化包装",
        "抽取“选题-脚本-配音-画面-字幕-成片”的任务模板，避免用户手动串步骤。",
        "题材偏 faceless Shorts，不能代替短剧混剪剪辑器。",
    ),
    RepositoryCandidate(
        "automated video editing",
        "linyqh/speclip-skills",
        "https://github.com/linyqh/speclip-skills",
        88,
        "NOASSERTION",
        "对话式剪辑技能",
        "观察",
        "对话驱动复杂剪辑工作流，方向接近本软件想要的低门槛操作。",
        "自动剪辑 Agent、自然语言任务入口",
        "只参考技能分层和对话入口，暂不直接接入。",
        "许可证不明确，能力边界需实测。",
    ),
    RepositoryCandidate(
        "video clipping",
        "roothch/PreenCut",
        "https://github.com/roothch/PreenCut",
        401,
        "MIT",
        "视频检索与切片",
        "优先研究",
        "用 AI 检索视频片段，适合从长剧里找人物、动作、场景和台词。",
        "素材筛选、自动剪辑输入、爆点候选池",
        "作为“按描述找片段”的候选方案，输出时间段再交给本地剪辑器。",
        "需要确认检索模型、索引速度和中文素材表现。",
    ),
    RepositoryCandidate(
        "video clipping",
        "NaufalRizqullah/opensource-clipping",
        "https://github.com/NaufalRizqullah/opensource-clipping",
        28,
        "MIT",
        "社媒切片流水线",
        "已拆解落地",
        "长视频转短视频，含人脸跟踪、卡拉 OK 字幕、B-roll、BGM ducking、自动缩略图。",
        "自动切条、字幕样式、画面重构、封面包装",
        "v2026.07.01.13 已先落地社媒增强版：竖屏裁切、响度标准化、BGM 自动循环和说话时压低音乐。",
        "完整人脸跟踪、逐词字幕和 B-roll 仍依赖重型模型/API，后续分开验证。",
    ),
    RepositoryCandidate(
        "video clipping",
        "SamurAIGPT/ai-clipping-generator",
        "https://github.com/SamurAIGPT/ai-clipping-generator",
        34,
        "MIT",
        "AI 切片 SaaS",
        "产品参考",
        "把长视频自动抽取成 Reels/TikTok/Shorts 的产品闭环完整。",
        "项目向导、队列、积分/任务概念、切片列表",
        "参考“上传长视频-生成候选切片-人工确认-导出”的页面流程。",
        "SaaS 业务代码多，剪辑核心不一定比本地实现更适合。",
    ),
    RepositoryCandidate(
        "video clipping",
        "tryvinci/vinci-clips",
        "https://github.com/tryvinci/vinci-clips",
        127,
        "NOASSERTION",
        "社媒切片平台",
        "观察",
        "长视频自动转社媒短切片，方向正确。",
        "自动切片、平台适配",
        "等许可证和核心算法清晰后再判断是否接入。",
        "许可证不明确。",
    ),
    RepositoryCandidate(
        "AI Agent video",
        "video-db/Director",
        "https://github.com/video-db/Director",
        1416,
        "MIT",
        "AI 视频 Agent 框架",
        "优先研究",
        "面向视频交互和工作流的 Agent 框架，适合做任务编排层。",
        "自动剪辑 Agent、任务队列、工具调度",
        "参考它的工具调用和工作流抽象，映射为本软件的“目标-计划-执行-复核”。",
        "框架型项目，不能直接解决本地剪辑输出，需要二次封装。",
    ),
    RepositoryCandidate(
        "AI Agent video",
        "Agentchengfeng/chengfeng-videocut-skills",
        "https://github.com/Agentchengfeng/chengfeng-videocut-skills",
        2432,
        "Apache-2.0",
        "中文剪辑 Agent 技能",
        "优先研究",
        "用 Claude Code Skills 做视频剪辑 Agent，中文场景贴近本软件用户。",
        "自动剪辑入口、技能化工具栏、操作审计",
        "抽取“技能卡片+可执行命令+产物检查”的结构，融合到插件工具页。",
        "需要逐个核对技能依赖，不能盲目安装。",
    ),
    RepositoryCandidate(
        "AI Agent video",
        "Agents365-ai/video-podcast-maker",
        "https://github.com/Agents365-ai/video-podcast-maker",
        1393,
        "MIT",
        "口播/播客视频生成",
        "流程参考",
        "自动生成 4K 视频播客，适合口播类二创和知识视频包装。",
        "配音口播、包装成片、标题封面",
        "参考模板化口播场景，把音频、字幕、头像/背景和包装串成一键任务。",
        "更偏生成视频，不是长素材剪辑核心。",
    ),
    RepositoryCandidate(
        "React video",
        "openvideodev/react-video-editor",
        "https://github.com/openvideodev/react-video-editor",
        1699,
        "NOASSERTION",
        "Web 时间线编辑器",
        "界面参考",
        "类 CapCut/Canva 的 React 视频编辑器，适合参考时间线和模板 UI。",
        "未来可视化时间线、人工精修、模板编辑",
        "参考交互，不合并代码；当前桌面端先保持轻量。",
        "许可证不明确，且前端栈迁移成本高。",
    ),
    RepositoryCandidate(
        "FFmpeg",
        "Lake1059/FFmpegFreeUI",
        "https://github.com/Lake1059/FFmpegFreeUI",
        7239,
        "MIT",
        "Windows FFmpeg 外壳",
        "界面参考",
        "把 FFmpeg 参数包装成普通用户能理解的 Windows 界面。",
        "转码压制、参数预设、质检修复",
        "学习它的参数分组，把复杂 FFmpeg 能力做成“清晰预设+高级展开”。",
        "不需要合并代码；重点学交互与预设库。",
    ),
    RepositoryCandidate(
        "FFmpeg",
        "BtbN/FFmpeg-Builds",
        "https://github.com/BtbN/FFmpeg-Builds",
        11185,
        "MIT",
        "FFmpeg 构建来源",
        "可选外部",
        "提供 Windows FFmpeg 构建来源，适合后续自动下载/更新工具箱。",
        "底层工具更新、安装引导、工具体检",
        "做成可选工具源：检测本地 FFmpeg 版本，提示下载合适构建。",
        "仍要注意 FFmpeg 自身 GPL/LGPL 组件差异。",
    ),
    RepositoryCandidate(
        "FFmpeg",
        "kkroening/ffmpeg-python",
        "https://github.com/kkroening/ffmpeg-python",
        11003,
        "Apache-2.0",
        "Python FFmpeg 封装",
        "参考",
        "用 Python 构建复杂 FFmpeg filter graph。",
        "视频引擎、滤镜组合、包装成片",
        "现阶段继续用显式命令，复杂滤镜增加时可借鉴其链式结构。",
        "增加依赖未必比当前 subprocess 清晰。",
    ),
    RepositoryCandidate(
        "remotion skills",
        "liancheng-zcy/remotion-com-skills",
        "https://github.com/liancheng-zcy/remotion-com-skills",
        86,
        "MIT",
        "Remotion 组件技能",
        "优先研究",
        "Remotion 常用组件和自定义 skills，适合抽短视频包装模板。",
        "标题封面、字幕动画、片头片尾",
        "整理成竖屏短剧模板清单，先让本软件生成等价本地包装。",
        "需要筛掉不适合短剧二创的组件。",
    ),
    RepositoryCandidate(
        "remotion skills",
        "Maartenlouis/remotion-ads",
        "https://github.com/Maartenlouis/remotion-ads",
        47,
        "MIT",
        "短视频广告模板",
        "参考",
        "Instagram Reels/Carousel 广告模板可转化为标题封面与片尾 CTA 模板。",
        "模板包装、标题封面、发布交接",
        "吸收模板字段：卖点、素材、字幕、CTA，不直接合并技能代码。",
        "广告模板要改成二创/短剧语境。",
    ),
)


ALL_CANDIDATES: tuple[RepositoryCandidate, ...] = CANDIDATES + EXTRA_CANDIDATES


SEARCH_REVIEWS: tuple[SearchResultReview, ...] = (
    SearchResultReview("AI video editing", "visomaster/VisoMaster", "排除主流程", "GPL 且偏换脸，不能解决自动剪辑核心门槛。"),
    SearchResultReview("AI video editing", "x007xyz/flycut-caption", "界面参考", "字幕时间轴和 AI 字幕编辑有价值，但许可证不清晰。"),
    SearchResultReview("AI video editing", "linyqh/NarratoAI", "流程参考", "脚本、解说、配音、剪辑链路接近二创业务。"),
    SearchResultReview("AI video editing", "FireRedTeam/FireRed-OpenStoryline", "优先研究", "自然语言意图到剪辑计划，适合做自动剪辑 Agent。"),
    SearchResultReview("AI video editing", "DataAnts-AI/CutScript", "已落地", "文本剪视频最能降低普通用户操作门槛，v2026.07.01.12 已落为文本剪辑时间线。"),
    SearchResultReview("AI video editing", "chatman-media/timeline-studio", "优先研究", "本地 AI 时间线适合补人工复核和时间线观感。"),
    SearchResultReview("AI video editing", "znyupup/ai-video-editing-skill", "优先研究", "技能化剪辑链路可转为本软件可审计动作。"),
    SearchResultReview("AI video editing", "Arabianaischool/AI-Video-Gen-by-ARABIAN-AI-SCHOOL", "不接入", "AGPL 且偏 API 生成示例，不适合作为桌面剪辑核心。"),
    SearchResultReview("Auto video editing", "WyattBlue/auto-editor", "已接入/增强", "去静音、动作、黑场粗剪是自动剪辑基础能力。"),
    SearchResultReview("Auto video editing", "DevonCrawford/Video-Editing-Automation", "算法参考", "算法演示有启发，但 GPL 且维护价值弱于 auto-editor。"),
    SearchResultReview("Auto video editing", "makiisthenes/TiktokAutoUploader", "暂不接入", "重点是上传，不是剪辑；平台自动化风险也更高。"),
    SearchResultReview("Auto video editing", "OpenNewsLabs/autoEdit_2", "参考", "文本剪辑产品逻辑值得吸收，项目本身较老。"),
    SearchResultReview("Auto video editing", "jappeace/cut-the-crap", "参考", "长视频预清洗有价值，偏直播素材。"),
    SearchResultReview("Auto video editing", "maxazure/video-editing-skill", "参考", "口播/访谈句子级剪辑流程可借鉴，许可证不清晰。"),
    SearchResultReview("Auto video editing", "eddieoz/youtube-clips-automator", "流程参考", "切片与缩略图闭环值得接入到标题封面。"),
    SearchResultReview("automated video editing", "luoluoluo22/jianying-editor-skill", "优先研究", "贴近中文剪映精修工作流。"),
    SearchResultReview("automated video editing", "GuanYixuan/pyCapCut", "已落地", "已形成剪映/CapCut 草稿交接能力。"),
    SearchResultReview("automated video editing", "aregrid/frame", "优先研究", "自然语言时间线交互适合长期可视化方向。"),
    SearchResultReview("automated video editing", "SaarD00/AI-Youtube-Shorts-Generator", "流程参考", "选题到成片流水线可改造成二创任务模板。"),
    SearchResultReview("automated video editing", "linyqh/speclip-skills", "观察", "对话式剪辑方向正确，许可证和依赖需确认。"),
    SearchResultReview("video clipping", "modelscope/FunClip", "优先接入", "转写、字幕、文本切片直接服务剧情爆点提取。"),
    SearchResultReview("video clipping", "zhouxiaoka/autoclip", "优先接入", "高光提取和二创切片非常贴近核心需求。"),
    SearchResultReview("video clipping", "roothch/PreenCut", "优先研究", "AI 检索片段能补“按描述找素材”。"),
    SearchResultReview("video clipping", "YILS-LIN/short-video-factory", "产品参考", "批量短视频工厂形态值得学，但 AGPL 不合并。"),
    SearchResultReview("video clipping", "NaufalRizqullah/opensource-clipping", "已拆解落地", "v2026.07.01.13 已先落地竖屏裁切、响度标准化和 BGM ducking。"),
    SearchResultReview("video clipping", "SamurAIGPT/ai-clipping-generator", "产品参考", "长视频生成候选切片的 SaaS 流程值得参考。"),
    SearchResultReview("video clipping", "deepmedia/Transcoder", "不接入", "Android MediaCodec 场景，不适合当前 Windows 桌面端。"),
    SearchResultReview("AI Agent video", "livekit/agents", "不接入主流程", "是实时语音/视频 Agent 底座，不是剪辑工具。"),
    SearchResultReview("AI Agent video", "calesthio/OpenMontage", "架构参考", "多管线/多工具/多技能视频工厂值得拆解。"),
    SearchResultReview("AI Agent video", "heygen-com/hyperframes", "优先接入", "HTML 渲染视频适合片头片尾、字幕动画和包装。"),
    SearchResultReview("AI Agent video", "video-db/Director", "优先研究", "视频 Agent 工作流框架可做任务编排参考。"),
    SearchResultReview("AI Agent video", "Agentchengfeng/chengfeng-videocut-skills", "优先研究", "中文剪辑 Agent 技能贴近目标用户。"),
    SearchResultReview("AI Agent video", "Agents365-ai/video-podcast-maker", "流程参考", "适合口播/知识类包装，不是长素材剪辑核心。"),
    SearchResultReview("Agentic video", "browser-use/video-use", "优先研究", "Agent 操作视频编辑流程，可抽象为目标式动作链。"),
    SearchResultReview("Agentic video", "calesthio/OpenMontage", "架构参考", "12 管线、工具和技能思想可映射到本软件板块。"),
    SearchResultReview("Agentic video", "video-db/Director", "优先研究", "适合任务调度，不直接生成本地成片。"),
    SearchResultReview("Agentic video", "HKUDS/VideoAgent", "暂不接入", "更偏视频理解研究，离桌面剪辑落地较远。"),
    SearchResultReview("React video", "remotion-dev/remotion", "可选外部", "程序化视频很强，但许可证/商业条款需单独确认。"),
    SearchResultReview("React video", "openvideodev/react-video-editor", "界面参考", "类 CapCut/Canva 时间线有价值，许可证不清晰。"),
    SearchResultReview("React video", "video-react/video-react", "不接入", "只是播放器，不提供剪辑生产能力。"),
    SearchResultReview("React video", "shahen94/react-native-video-processing", "不接入", "移动端裁剪压缩库，不匹配 Windows 桌面端。"),
    SearchResultReview("React video", "TheWidlarzGroup/react-native-video", "不接入", "播放组件，不是剪辑或合成引擎。"),
    SearchResultReview("FFmpeg", "FFmpeg/FFmpeg", "已内置", "底层剪切、转码、滤镜、质检继续以外部程序调用。"),
    SearchResultReview("FFmpeg", "BtbN/FFmpeg-Builds", "可选外部", "适合作为 Windows FFmpeg 更新来源。"),
    SearchResultReview("FFmpeg", "Lake1059/FFmpegFreeUI", "界面参考", "普通用户友好的 FFmpeg 参数外壳值得学习。"),
    SearchResultReview("FFmpeg", "kkroening/ffmpeg-python", "参考", "复杂 filter graph 可借鉴，当前不急于加依赖。"),
    SearchResultReview("FFmpeg", "ffmpegwasm/ffmpeg.wasm", "暂不接入", "浏览器端 FFmpeg 不适合当前本地桌面主流程。"),
    SearchResultReview("FFmpeg", "arthenica/ffmpeg-kit", "暂不接入", "更偏移动端/跨平台 SDK，当前使用外部 ffmpeg.exe 更稳。"),
    SearchResultReview("remotion skills", "wshuyi/remotion-video-skill", "参考", "Remotion 技能化说明可借鉴。"),
    SearchResultReview("remotion skills", "liancheng-zcy/remotion-com-skills", "优先研究", "常用组件和 skills 可转化为包装模板清单。"),
    SearchResultReview("remotion skills", "iart-ai/motion-skills", "优先研究", "动效技能库适合字幕动画、解释视频、短视频包装。"),
    SearchResultReview("remotion skills", "Maartenlouis/remotion-ads", "参考", "广告模板字段可改造成标题封面/片尾 CTA。"),
    SearchResultReview("remotion skills", "snori74/linuxupskillchallenge", "不接入", "搜索误命中，与视频剪辑无关。"),
    SearchResultReview("openmontage", "calesthio/OpenMontage", "架构参考", "只分析主仓，学习管线拆解，不合并 AGPL 代码。"),
    SearchResultReview("openmontage", "Open-Montage/OpenMontage", "不接入", "疑似下载/镜像仓，星标低，不作为来源。"),
    SearchResultReview("openmontage", "47thtechcorner/RayCodes_OpenMontage", "不接入", "衍生/包装仓，优先回到主仓判断。"),
    SearchResultReview("openmontage", "其他 OpenMontage forks", "不接入", "多为 fork、备份、测试和资产仓，重复价值低。"),
)


INTEGRATION_TRACKS: tuple[dict[str, str], ...] = (
    {
        "name": "自动剪辑入口",
        "goal": "让普通用户只输入目标、关键词或选择模板，就能得到可复核的剪辑片段。",
        "repos": "FunClip、AutoClip、auto-editor、CutScript、PreenCut、opensource-clipping",
        "landing": "爆点融合策略、字幕关键词、按描述找片段、去静音粗剪、候选片段评分表",
        "first_step": "先做本地统一输出格式：segments.csv + edit_plan.json，再由现有 FFmpeg 引擎执行。",
    },
    {
        "name": "二创包装成片",
        "goal": "把剪好的片段自动变成可发布短视频，而不是只产出半成品素材。",
        "repos": "opensource-clipping、HyperFrames、Remotion、motion-skills、remotion-com-skills",
        "landing": "逐词字幕、人脸/主体居中、B-roll 插入、BGM ducking、片头片尾、封面海报",
        "first_step": "把每个包装能力做成独立开关，默认使用本地 FFmpeg/Pillow，重型渲染器做可选增强。",
    },
    {
        "name": "剪映精修交接",
        "goal": "自动剪辑完成后，用户能直接交给剪映继续精修，不再手动找素材和对时间。",
        "repos": "pyCapCut、jianying-editor-skill、OpenCut、react-video-editor",
        "landing": "剪映草稿包、真实草稿创建、素材清单、时间线说明、人工精修入口",
        "first_step": "继续强化已落地的草稿包，补“草稿检查”和“剪映版本兼容提示”。",
    },
    {
        "name": "发布前质检与工具底座",
        "goal": "减少黑屏、无声、比例错误、码率异常、封面缺失这类发布事故。",
        "repos": "FFmpeg、BtbN/FFmpeg-Builds、FFmpegFreeUI、ffmpeg-python、LosslessCut",
        "landing": "视频质检、自动修复建议、FFmpeg 版本体检、参数预设、高级工具页",
        "first_step": "保持外部 ffmpeg.exe 调用，新增工具来源和版本检测，不把复杂许可证混入主程序。",
    },
    {
        "name": "Agent 任务编排",
        "goal": "把复杂剪辑参数包装成自然语言任务，并留下可解释、可回退的执行记录。",
        "repos": "FireRed-OpenStoryline、video-use、Director、chengfeng-videocut-skills、OpenMontage",
        "landing": "目标式剪辑、自动推荐策略、任务计划、执行日志、人机复核",
        "first_step": "先做轻量本地 Agent：目标解析只负责选策略和生成计划，不让模型直接不可控地改文件。",
    },
)


def repository_candidates() -> list[RepositoryCandidate]:
    return list(ALL_CANDIDATES)


def candidates_by_query() -> dict[str, list[RepositoryCandidate]]:
    grouped: dict[str, list[RepositoryCandidate]] = {query: [] for query in SEARCH_URLS}
    for candidate in ALL_CANDIDATES:
        grouped.setdefault(candidate.query, []).append(candidate)
    return grouped


def reviews_by_query() -> dict[str, list[SearchResultReview]]:
    grouped: dict[str, list[SearchResultReview]] = {query: [] for query in SEARCH_URLS}
    for review in SEARCH_REVIEWS:
        grouped.setdefault(review.query, []).append(review)
    return grouped


def integration_tracks() -> list[dict[str, str]]:
    return list(INTEGRATION_TRACKS)


def research_summary() -> dict[str, object]:
    decisions: dict[str, int] = {}
    categories: dict[str, int] = {}
    for candidate in ALL_CANDIDATES:
        decisions[candidate.decision] = decisions.get(candidate.decision, 0) + 1
        categories[candidate.category] = categories.get(candidate.category, 0) + 1
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "search_url_count": len(SEARCH_URLS),
        "candidate_count": len(ALL_CANDIDATES),
        "reviewed_result_count": len(SEARCH_REVIEWS),
        "decisions": decisions,
        "categories": categories,
    }


def priority_candidates(limit: int = 10) -> list[RepositoryCandidate]:
    rank = {
        "已落地": 0,
        "已内置": 0,
        "已拆解落地": 0,
        "已接入/增强": 1,
        "优先接入": 2,
        "优先研究": 3,
        "可选外部": 4,
        "参考": 5,
        "界面参考": 6,
        "流程参考": 7,
        "架构参考": 8,
        "产品参考": 9,
        "算法参考": 10,
        "观察": 20,
        "暂不接入": 30,
        "不接入": 40,
    }
    return sorted(ALL_CANDIDATES, key=lambda item: (rank.get(item.decision, 99), -item.stars_seen, item.repo))[:limit]


def auto_edit_enhancement_cards() -> list[dict[str, str]]:
    wanted = {
        "WyattBlue/auto-editor",
        "modelscope/FunClip",
        "zhouxiaoka/autoclip",
        "FireRedTeam/FireRed-OpenStoryline",
        "browser-use/video-use",
        "luoluoluo22/jianying-editor-skill",
        "GuanYixuan/pyCapCut",
        "DataAnts-AI/CutScript",
        "roothch/PreenCut",
        "NaufalRizqullah/opensource-clipping",
        "Agentchengfeng/chengfeng-videocut-skills",
        "video-db/Director",
        "chatman-media/timeline-studio",
    }
    cards = []
    for candidate in ALL_CANDIDATES:
        if candidate.repo not in wanted:
            continue
        cards.append(
            {
                "name": FRIENDLY_REPO_NAMES.get(candidate.repo, candidate.repo.split("/", 1)[-1]),
                "repo": candidate.repo,
                "decision": candidate.decision,
                "target": candidate.integration_target,
                "usable_for": candidate.usable_for,
                "next_action": candidate.next_action,
                "risk": candidate.risk,
            }
        )
    return cards


def render_markdown(candidates: Iterable[RepositoryCandidate] | None = None) -> str:
    lines = [
        "# GitHub 自动剪辑仓库分析矩阵",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "这份矩阵对应用户给出的 10 个 GitHub 检索入口，分两层记录：先复核每个入口里为什么采纳或排除，再把能补足水星剪辑业务短板的仓库列入候选。",
        "",
        "v2026.07.01.05 已把 FireRed-OpenStoryline/video-use 的 Agent 思路轻量化为“按目标推荐策略”：用户输入一句剪辑目标，软件自动选择字幕关键词、音量高光、去静音粗剪、镜头节奏粗剪或顺序分镜，并写出推荐报告。",
        "",
        "v2026.07.01.11 补充 GitHub 当前搜索结果：新增文本剪视频、AI 视频检索、社媒切片流水线、中文剪辑 Agent、FFmpeg Windows 化和 Remotion 模板技能的接入判断。",
        "",
        "v2026.07.01.12 已把 CutScript 的文本剪视频思路落地为“文本剪辑时间线”：字幕草稿生成可编辑台词表，应用后生成自动剪辑素材包。",
        "",
        "v2026.07.01.13 已把 opensource-clipping 的社媒包装思路拆为水星剪辑自有功能：竖屏裁切、响度标准化、BGM 自动循环和说话时压低音乐，并新增 GitHub 实时搜索逐项分析报告。",
        "",
        "## 检索入口",
        "",
    ]
    for query, url in SEARCH_URLS.items():
        lines.append(f"- {query}: {url}")
    lines.extend(["", "## 接入结论", ""])
    summary = research_summary()
    for decision, count in sorted(summary["decisions"].items(), key=lambda item: item[0]):
        lines.append(f"- {decision}: {count}")
    lines.extend(["", "## 搜索入口逐项筛查", ""])
    for query, reviews in reviews_by_query().items():
        lines.extend([f"### {query}", "", f"原始入口：{SEARCH_URLS[query]}", ""])
        for review in reviews:
            lines.append(f"- {review.repo}：{review.verdict}。{review.reason}")
        lines.append("")
    lines.extend(["", "## 按功能融入路线", ""])
    for track in INTEGRATION_TRACKS:
        lines.extend(
            [
                f"### {track['name']}",
                "",
                f"- 最终目的：{track['goal']}",
                f"- 可借鉴/调用：{track['repos']}",
                f"- 融入位置：{track['landing']}",
                f"- 第一落地动作：{track['first_step']}",
                "",
            ]
        )
    lines.extend(["", "## 自动剪辑优先融合项", ""])
    for card in auto_edit_enhancement_cards():
        lines.extend(
            [
                f"### {card['repo']}",
                "",
                f"- 决策：{card['decision']}",
                f"- 用途：{card['usable_for']}",
                f"- 融入位置：{card['target']}",
                f"- 下一步：{card['next_action']}",
                f"- 风险：{card['risk']}",
                "",
            ]
        )
    lines.extend(["## 逐项矩阵", ""])
    for query, grouped in candidates_by_query().items():
        lines.extend([f"### {query}", "", f"原始入口：{SEARCH_URLS[query]}", ""])
        if not grouped:
            lines.extend(["- 未筛出可直接映射到当前业务的候选。", ""])
            continue
        for item in grouped:
            lines.extend(
                [
                    f"- [{item.repo}]({item.github})",
                    f"  - 星标抓取值：{item.stars_seen}",
                    f"  - 许可证：{item.license}",
                    f"  - 分类：{item.category}",
                    f"  - 决策：{item.decision}",
                    f"  - 能解决：{item.usable_for}",
                    f"  - 融入位置：{item.integration_target}",
                    f"  - 下一步：{item.next_action}",
                    f"  - 风险：{item.risk}",
                ]
            )
        lines.append("")
    return "\n".join(lines)


def write_github_research_report(output_dir: str | Path | None = None) -> dict[str, str]:
    target = Path(output_dir) if output_dir else version_root() / "docs"
    target.mkdir(parents=True, exist_ok=True)
    markdown_path = target / "github_research_matrix.md"
    json_path = target / "github_research_matrix.json"
    csv_path = target / "github_research_matrix.csv"

    markdown_path.write_text(render_markdown(), encoding="utf-8")
    payload = {
        "summary": research_summary(),
        "search_urls": SEARCH_URLS,
        "search_reviews": [asdict(item) for item in SEARCH_REVIEWS],
        "integration_tracks": list(INTEGRATION_TRACKS),
        "candidates": [asdict(item) for item in ALL_CANDIDATES],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(ALL_CANDIDATES[0]).keys()))
        writer.writeheader()
        for item in ALL_CANDIDATES:
            writer.writerow(asdict(item))

    return {
        "markdown": str(markdown_path),
        "json": str(json_path),
        "csv": str(csv_path),
    }

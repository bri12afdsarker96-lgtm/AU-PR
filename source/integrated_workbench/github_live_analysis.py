from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .github_research import SEARCH_URLS
from .plugins import version_root


@dataclass(frozen=True)
class LiveRepoDecision:
    query: str
    repo: str
    github: str
    stars: int
    language: str
    license: str
    updated_at: str
    decision: str
    target_stage: str
    usable_for: str
    integration_mode: str
    reason: str
    first_action: str


SPECIFIC_DECISIONS: dict[str, tuple[str, str, str, str, str, str]] = {
    "linyqh/NarratoAI": (
        "流程参考",
        "二创解说/脚本配音剪辑",
        "一键解说、配音、剪辑链路接近短剧二创业务。",
        "学习流程，不合并代码",
        "许可证不明确且依赖较重，不适合直接内置。",
        "把脚本-配音-剪辑顺序沉淀为水星剪辑任务模板。",
    ),
    "x007xyz/flycut-caption": (
        "界面参考",
        "字幕/文本剪辑",
        "AI 字幕识别、字幕时间轴和可视化编辑。",
        "UI 参考 + 自有字幕入口",
        "许可证不清，不直接合并组件代码。",
        "优化文本剪辑页的字幕校对和逐句操作体验。",
    ),
    "ncounterspecialist/twick": (
        "观察",
        "未来可视化时间线",
        "React 时间线 SDK、AI captions、MP4 export。",
        "长期参考",
        "许可证不清，且当前客户端主栈不是 React。",
        "保留为未来时间线编辑器参考。",
    ),
    "chatman-media/timeline-studio": (
        "优先研究",
        "未来可视化时间线/人工复核",
        "AI 桌面时间线，适合学习素材栏、轨道、预览和任务队列。",
        "产品结构参考",
        "MIT，可研究交互，但不替换当前轻量客户端。",
        "先把自动剪辑结果做成更可读的时间线报告。",
    ),
    "aregrid/frame": (
        "优先研究",
        "Agent 自动剪辑/时间线交互",
        "自然语言驱动专业视频切片和时间线交互。",
        "交互模式参考",
        "MIT，但项目成熟度还要运行验证。",
        "吸收“对话指令变剪辑动作”的交互，不直接并入前端。",
    ),
    "modelscope/FunClip": (
        "优先接入",
        "智能剪辑/字幕找片段",
        "长视频转写、字幕切片、文本/LLM 辅助找爆点。",
        "可选外部工具 + 自有时间线格式",
        "MIT，业务命中度高，但 ASR/模型依赖较重。",
        "继续把输出转成台词剪辑表和 segments.csv。",
    ),
    "zhouxiaoka/autoclip": (
        "优先接入",
        "智能剪辑/高光提取",
        "自动找精彩片段，适合长剧拆条和二创素材筛选。",
        "可选外部工具 + 自研评分表",
        "MIT，方向贴合，但依赖和模型耗时需要实测。",
        "封装为“高光候选池”，输出候选片段清单。",
    ),
    "NaufalRizqullah/opensource-clipping": (
        "已拆解落地",
        "二创成片/社媒切条增强",
        "主体裁切、逐词字幕、BGM ducking、自动封面等社媒包装能力。",
        "拆成自有功能开关",
        "MIT，功能正中短视频痛点，但整仓依赖 Gemini/Pexels/MediaPipe，不能一次性强塞。",
        "v2026.07.01.13 先落地竖屏裁切、响度标准化、BGM 自动压低。",
    ),
    "WyattBlue/auto-editor": (
        "已接入/增强",
        "智能剪辑/去静音粗剪",
        "按静音、动作和黑场自动删废片段。",
        "外部 CLI 或自有 FFmpeg 回退",
        "Unlicense，适合外部调用；本软件已有去静音策略。",
        "检测到可执行文件时允许作为增强引擎。",
    ),
    "DevonCrawford/Video-Editing-Automation": (
        "算法参考",
        "智能剪辑/算法示例",
        "自动剪辑算法工具包，可参考粗剪思路。",
        "只看算法，不合并代码",
        "GPL-3.0 且更像演示项目，维护价值弱于 auto-editor。",
        "把有效算法思想转成自有评分策略。",
    ),
    "OpenNewsLabs/autoEdit_2": (
        "参考",
        "智能剪辑/文本剪视频",
        "老牌文本驱动视频编辑产品，说明“编辑文本就是编辑视频”的产品价值。",
        "流程参考",
        "项目较老，不适合直接依赖。",
        "继续强化台词表剪辑和字幕校对闭环。",
    ),
    "makiisthenes/TiktokAutoUploader": (
        "暂不接入",
        "发布自动化",
        "主要做 TikTok 上传，不是剪辑核心。",
        "不进入当前客户端",
        "平台自动化风险高，且不补剪辑能力。",
        "发布阶段仍采用可审计队列和人工确认。",
    ),
    "DataAnts-AI/CutScript": (
        "已落地",
        "智能剪辑/文本剪视频",
        "通过编辑台词表完成剪辑，降低普通用户门槛。",
        "自有实现",
        "MIT，理念明确，v2026.07.01.12 已落地文本剪辑时间线。",
        "继续强化台词表校对和一键应用。",
    ),
    "roothch/PreenCut": (
        "优先研究",
        "素材筛选/按描述找片段",
        "AI 视频检索与片段切取，补足“我想找某个画面”的需求。",
        "可选外部工具",
        "MIT，但依赖语音识别和检索服务。",
        "先把描述检索结果统一为候选片段 CSV。",
    ),
    "FireRedTeam/FireRed-OpenStoryline": (
        "优先研究",
        "Agent 自动剪辑/意图到计划",
        "自然语言导演意图、计划生成、工具编排、人机复核。",
        "学习架构 + 自有轻量计划",
        "Apache-2.0，方向强，但完整工具链较重。",
        "映射为本软件的“目标-策略-分镜-复核”任务计划。",
    ),
    "poseljacob/agentic-video-editor": (
        "优先研究",
        "Agent 自动剪辑/任务编排",
        "创意 brief 到素材预处理、导演选镜、渲染和复核。",
        "学习 Pipeline 和 EditPlan",
        "MIT，结构清晰，但依赖 Gemini。",
        "把 CreativeBrief/EditPlan 思路转成本地剪辑目标 JSON。",
    ),
    "video-db/Director": (
        "优先研究",
        "Agent 自动剪辑/视频工作流",
        "视频 Agent 工作流和交互框架。",
        "架构参考",
        "MIT，适合借鉴任务抽象，不直接替换当前引擎。",
        "整理任务节点：转写、找片段、渲染、质检。",
    ),
    "browser-use/video-use": (
        "优先研究",
        "Agent 自动剪辑/动作链",
        "把视频编辑动作交给 coding agent 执行。",
        "动作链参考",
        "适合学习可审计动作，不适合直接让 Agent uncontrolled 改文件。",
        "只允许生成计划和调用白名单工具。",
    ),
    "Agentchengfeng/chengfeng-videocut-skills": (
        "优先研究",
        "Agent 自动剪辑/中文技能卡",
        "中文视频剪辑 Agent 技能库，贴近目标用户表达。",
        "技能拆分参考",
        "Apache-2.0，适合转成软件内技能入口。",
        "把找爆点、加字幕、生成封面做成可解释技能卡。",
    ),
    "heygen-com/hyperframes": (
        "优先接入",
        "标题包装/模板视频",
        "HTML 写视频，适合片头、片尾、字幕动画、包装片段。",
        "可选外部渲染器",
        "Apache-2.0，适合 Agent 化模板视频；需要 Node/浏览器渲染。",
        "先作为模板渲染候选，输出 MP4 回流到包装成片。",
    ),
    "remotion-dev/remotion": (
        "可选外部",
        "标题包装/React 视频模板",
        "程序化生成片头、封面动效、字幕动画和广告模板。",
        "外部模板工程",
        "功能强，但商业/许可证条款和 Node 环境要单独确认。",
        "短期只做模板参考，长期做可选渲染入口。",
    ),
    "liancheng-zcy/remotion-com-skills": (
        "优先研究",
        "标题包装/Remotion 组件",
        "常用 Remotion 组件和技能说明，可转成包装模板清单。",
        "模板参考",
        "MIT，已下载，适合拆组件思路。",
        "提炼字幕动效、标题页、代码/知识类场景模板。",
    ),
    "iart-ai/motion-skills": (
        "优先研究",
        "标题包装/动效技能",
        "动效、解释视频、短视频包装、WebGL/Manim 等技能包。",
        "模板和提示词参考",
        "MIT，已下载，可作为包装风格库。",
        "把动效类型归入包装页模板选择。",
    ),
    "GuanYixuan/pyCapCut": (
        "已落地",
        "发布交接/剪映草稿",
        "生成 CapCut/剪映草稿，承接自动剪辑后的人工精修。",
        "外部库适配",
        "许可证不清，保持外部适配和交接包方式。",
        "继续补草稿检查、素材缺失提示、剪映版本提示。",
    ),
    "luoluoluo22/jianying-editor-skill": (
        "优先研究",
        "发布交接/剪映自动精修",
        "Agent 自动操作剪映，贴近中文剪辑用户。",
        "技能参考 + 外部动作",
        "MIT，但依赖桌面自动化和剪映版本。",
        "先与 pyCapCut 草稿包串联，动作必须可审计。",
    ),
    "YILS-LIN/short-video-factory": (
        "产品参考",
        "二创批量生产/任务队列",
        "批量短视频工厂、任务队列、模板和素材管理很贴近产品形态。",
        "产品参考，不合并代码",
        "AGPL-3.0，不适合直接放入闭源桌面客户端。",
        "学习批量任务和模板体验，自研实现。",
    ),
    "calesthio/OpenMontage": (
        "架构参考",
        "全流程/Agent 视频工厂",
        "多管线、多工具、多技能的视频生产系统。",
        "只学架构",
        "AGPL，不直接合并代码；仓库大而重。",
        "把 12 管线思想拆成水星剪辑的七个业务页。",
    ),
    "livekit/agents": (
        "不接入主流程",
        "实时语音/视频 Agent",
        "更适合实时语音视频代理，不是本地剪辑工具。",
        "不进入软件",
        "与当前剪辑、二创、发布质检主痛点距离较远。",
        "排除。",
    ),
    "TEN-framework/ten-framework": (
        "不接入主流程",
        "实时语音 Agent 框架",
        "偏实时 conversational agent，不是剪辑流水线。",
        "不进入软件",
        "许可证不清且能力不对焦。",
        "排除。",
    ),
    "GetStream/Vision-Agents": (
        "不接入主流程",
        "视觉 Agent 示例",
        "更像实时/视觉 Agent 框架，不直接生成剪辑成片。",
        "不进入软件",
        "不能解决当前自动剪辑操作门槛。",
        "保留观察。",
    ),
    "NVIDIA-AI-Blueprints/video-search-and-summarization": (
        "优先研究",
        "素材筛选/视频检索摘要",
        "视频搜索和摘要能力可补“按描述找片段”。",
        "外部重型方案参考",
        "C++/NVIDIA 方案较重，适合作为高配增强方向。",
        "先学习索引和摘要输出结构，统一为候选片段 CSV。",
    ),
    "HKUDS/VideoAgent": (
        "暂不接入",
        "视频理解研究",
        "All-in-one 视频理解、编辑和 remaking 框架，研究味更重。",
        "研究观察",
        "离轻量桌面可用功能还有距离。",
        "只记录，不进入本轮实现。",
    ),
    "openvideodev/react-video-editor": (
        "界面参考",
        "未来可视化时间线",
        "类 CapCut/Canva 的 React 视频编辑器。",
        "UI 参考",
        "许可证不清，且当前客户端不是 Web 前端。",
        "学习素材轨道、预览、时间线布局。",
    ),
    "FFmpeg/FFmpeg": (
        "已内置",
        "底层工具/剪切转码质检",
        "剪切、转码、滤镜、抽帧、音频处理。",
        "外部 ffmpeg.exe 调用",
        "继续作为核心引擎；注意 LGPL/GPL 构建差异。",
        "保持体检、路径配置和版本提示。",
    ),
    "BtbN/FFmpeg-Builds": (
        "可选外部",
        "工具箱/FFmpeg 更新",
        "Windows FFmpeg 构建来源。",
        "下载来源提示",
        "MIT 脚本仓；二进制许可证随构建配置变化。",
        "只做版本检测和下载引导。",
    ),
    "Lake1059/FFmpegFreeUI": (
        "界面参考",
        "工具箱/FFmpeg 预设",
        "把 FFmpeg 参数包装成普通用户能理解的 Windows UI。",
        "产品参考",
        "MIT，功能面宽，但不需要整体并入。",
        "转成“常用修复预设”：转竖屏、压缩、统一音量。",
    ),
    "kkroening/ffmpeg-python": (
        "参考",
        "底层工具/复杂滤镜图",
        "Python 构建复杂 FFmpeg filter graph。",
        "代码风格参考",
        "Apache-2.0，但当前直接拼命令更可控。",
        "仅在滤镜图复杂到难维护时再考虑。",
    ),
    "znyupup/ai-video-editing-skill": (
        "优先研究",
        "Agent 自动剪辑/技能手册",
        "ffmpeg + Whisper/FunASR + 视觉 API 的素材分析和 edit_plan 工作流。",
        "技能说明转产品流程",
        "MIT，已下载；适合把复杂自动剪辑拆成用户可见步骤。",
        "把 edit_plan JSON 思路对齐现有自动剪辑计划。",
    ),
}


def load_live_results(path: Path | None = None) -> list[dict[str, Any]]:
    source = path or version_root() / "docs" / "github_search_live" / "all_search_results.json"
    if not source.exists():
        return []
    data = json.loads(source.read_text(encoding="utf-8-sig"))
    return data if isinstance(data, list) else []


def analyze_live_results(path: Path | None = None) -> list[LiveRepoDecision]:
    return [_classify(row) for row in load_live_results(path)]


def write_live_search_report(output_dir: Path | None = None, snapshot_path: Path | None = None) -> dict[str, str]:
    target = output_dir or version_root() / "docs"
    target.mkdir(parents=True, exist_ok=True)
    decisions = analyze_live_results(snapshot_path)

    markdown_path = target / "github_live_repository_analysis.md"
    json_path = target / "github_live_repository_analysis.json"
    csv_path = target / "github_live_repository_analysis.csv"

    markdown_path.write_text(_render_markdown(decisions), encoding="utf-8")
    json_path.write_text(json.dumps([asdict(item) for item in decisions], ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = list(asdict(decisions[0]).keys()) if decisions else list(LiveRepoDecision.__dataclass_fields__.keys())
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in decisions:
            writer.writerow(asdict(item))

    return {"markdown": str(markdown_path), "json": str(json_path), "csv": str(csv_path)}


def _classify(row: dict[str, Any]) -> LiveRepoDecision:
    repo = str(row.get("full_name") or "")
    specific = SPECIFIC_DECISIONS.get(repo)
    if specific:
        decision, target_stage, usable_for, integration_mode, reason, first_action = specific
    else:
        decision, target_stage, usable_for, integration_mode, reason, first_action = _heuristic_decision(row)
    return LiveRepoDecision(
        query=str(row.get("query") or ""),
        repo=repo,
        github=str(row.get("html_url") or f"https://github.com/{repo}"),
        stars=_as_int(row.get("stars")),
        language=str(row.get("language") or ""),
        license=str(row.get("license") or "NOASSERTION"),
        updated_at=str(row.get("updated_at") or ""),
        decision=decision,
        target_stage=target_stage,
        usable_for=usable_for,
        integration_mode=integration_mode,
        reason=reason,
        first_action=first_action,
    )


def _heuristic_decision(row: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    repo = str(row.get("full_name") or "")
    name = repo.lower()
    description = str(row.get("description") or "").lower()
    license_id = str(row.get("license") or "")
    language = str(row.get("language") or "")
    text = f"{name} {description}"

    if any(word in text for word in ["video call", "webrtc", "react-native-video", "player", "alexa", "linuxupskill"]):
        return (
            "不接入",
            "无关/播放器/课程",
            "不解决自动剪辑、二创包装或发布质检问题。",
            "不进入软件",
            "搜索误命中或只是播放/课程类项目。",
            "仅保留在排除清单。",
        )
    if "face swap" in text or "faceswap" in text or "换脸" in text:
        return (
            "排除主流程",
            "特殊效果",
            "换脸不是当前剪辑和二创主痛点，且风险较高。",
            "不进入主流程",
            "容易偏离剪辑、包装、发布闭环。",
            "仅作为特殊效果观察项。",
        )
    if license_id in {"AGPL-3.0", "GPL-3.0", "GPL-2.0"}:
        return (
            "只参考/外部调用",
            "许可证受限能力",
            "可学习产品形态或流程，但不直接合并代码。",
            "流程参考或外部工具",
            f"{license_id} 不适合直接并入闭源桌面客户端。",
            "拆业务思路，自研关键能力。",
        )
    if any(word in text for word in ["caption", "subtitle", "transcription", "whisper", "funasr"]):
        return (
            "参考/可选接入",
            "字幕/文本剪辑",
            "补字幕转写、逐词字幕、文本剪视频体验。",
            "输出到字幕和台词时间线",
            "与降低剪辑门槛相关，但要先确认依赖和许可证。",
            "先统一成 SRT/CSV，再交给文本剪辑入口。",
        )
    if any(word in text for word in ["clip", "clipping", "shorts", "tiktok", "reels"]):
        return (
            "参考/可选接入",
            "社媒切条/二创成片",
            "补长视频切短、封面、标题和社媒包装。",
            "拆成功能开关",
            "方向相关，但质量和依赖差异较大。",
            "优先吸收竖屏裁切、字幕、封面、BGM 等稳定能力。",
        )
    if any(word in text for word in ["agent", "brief", "workflow", "pipeline"]):
        return (
            "优先研究",
            "Agent 任务编排",
            "把自然语言目标变成可复核剪辑计划。",
            "架构参考",
            "Agent 项目通常依赖模型，适合先学计划结构。",
            "映射为目标式剪辑计划，不直接自动乱改素材。",
        )
    if "ffmpeg" in text or language in {"C", "C++", "Shell"}:
        return (
            "参考",
            "底层工具/转码处理",
            "补剪切、转码、滤镜、封装和质检底座。",
            "外部工具或参数参考",
            "当前软件已有 ffmpeg.exe 调用，新增依赖要谨慎。",
            "只提炼常用参数和版本检测。",
        )
    if any(word in text for word in ["remotion", "motion", "animation", "template"]):
        return (
            "参考/可选接入",
            "标题包装/模板视频",
            "补动态标题、片头片尾、解释视频模板。",
            "模板参考",
            "需要 Node/前端渲染环境，先不强依赖。",
            "提炼成包装页模板清单。",
        )
    return (
        "观察",
        "待确认",
        "当前描述无法证明能直接解决核心剪辑痛点。",
        "暂不进入软件",
        "需要进一步运行验证或阅读源码。",
        "保留链接，后续按用户场景再复查。",
    )


def _render_markdown(decisions: list[LiveRepoDecision]) -> str:
    lines = [
        "# GitHub 搜索结果逐项分析",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "范围：对用户给出的 10 个 GitHub 检索入口各抓取前排仓库快照，逐项判断是否能融入水星剪辑。",
        "",
        "采纳原则：能直接降低普通用户剪辑门槛的优先；MIT/Apache/Unlicense 更适合接入；GPL/AGPL 只做外部调用或流程参考；许可证不清先不合并代码。",
        "",
        "## 检索入口",
        "",
    ]
    for query, url in SEARCH_URLS.items():
        lines.append(f"- {query}: {url}")

    summary: dict[str, int] = {}
    for item in decisions:
        summary[item.decision] = summary.get(item.decision, 0) + 1
    lines.extend(["", "## 决策汇总", ""])
    for decision, count in sorted(summary.items(), key=lambda pair: pair[0]):
        lines.append(f"- {decision}: {count}")

    lines.extend(["", "## 按搜索入口逐项判断", ""])
    grouped: dict[str, list[LiveRepoDecision]] = {query: [] for query in SEARCH_URLS}
    for item in decisions:
        grouped.setdefault(item.query, []).append(item)
    for query, items in grouped.items():
        lines.extend([f"### {query}", "", f"原始入口：{SEARCH_URLS.get(query, '')}", ""])
        if not items:
            lines.extend(["- 本地快照中没有抓到结果。", ""])
            continue
        for item in sorted(items, key=lambda value: (-value.stars, value.repo.lower())):
            lines.extend(
                [
                    f"- [{item.repo}]({item.github})",
                    f"  - 星标：{item.stars}；语言：{item.language or '未标明'}；许可证：{item.license}；更新：{item.updated_at}",
                    f"  - 决策：{item.decision}",
                    f"  - 可融入板块：{item.target_stage}",
                    f"  - 能解决：{item.usable_for}",
                    f"  - 接入方式：{item.integration_mode}",
                    f"  - 判断理由：{item.reason}",
                    f"  - 第一动作：{item.first_action}",
                ]
            )
        lines.append("")
    return "\n".join(lines)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

# AU&PR

AU&PR 是一个音频驱动的视频分镜对齐研究项目。它的核心思路是：

1. 使用 Fish Audio S2 Pro / Fish Speech 生成逐句配音包。
2. 以配音时长作为主时间轴。
3. 将逐句配音与分镜片段自动对齐，生成可预览、可复核、可导出的成片计划。

项目显示名保留 `AU&PR`；如果后续创建 GitHub 远端仓库，建议使用 URL 友好的仓库名 `AU-PR`。

## 目标

- 把文案、角色音色、情绪标签、逐句配音、分镜片段和最终成片串成一条可复核流水线。
- 让配音生成和音画对齐解耦：TTS 引擎可以替换，对齐渲染也可以独立测试。
- 先做研究型 MVP，再逐步演进成短剧、解说、矩阵视频可用的生产工具。

## 参考能力

- Fish Audio S2 Pro / Fish Speech：多语言 TTS、短参考音色克隆、多说话人、多轮上下文、行内情绪控制。参考仓库：[fishaudio/fish-speech](https://github.com/fishaudio/fish-speech)。
- 水星剪辑 / Mercury Premiere Pro：已有“配音包与分镜视频按句对齐”的实现，配音时长作为画面主轴，分镜通过裁剪、慢放、定格等策略贴合配音。

## 设计原则

- 配音是主轴：成片总时长由逐句配音时长决定。
- 分镜是素材：分镜片段可以被裁剪、变速、定格，但原素材不被覆盖。
- 所有中间结果可追踪：脚本、音频清单、分镜清单、对齐计划、渲染清单都写成 JSON/CSV。
- 第三方能力可插拔：Fish Speech、FFmpeg、剪映/Premiere 导出都放在适配器层。
- 先保守合规：不把第三方模型权重、私有源码、token 或本机重资产写入仓库。

## 第一版模块

- `Script Studio`：台词拆句、角色分配、情绪标签、停顿标记。
- `Voice Lab`：连接 Fish S2 Pro，按句生成配音，支持单句重生、音色引用、质量检查。
- `Dub Package`：统一输出逐句音频、配音清单、时长、speaker、hash。
- `Shot Pack`：导入分镜视频，生成分镜清单、时长、画幅、可用性状态。
- `Alignment Engine`：按顺序或语义辅助配对，生成音画对齐计划。
- `Render Engine`：调用 FFmpeg 合成，支持字幕、BGM 避让、画幅缩放、导出成片。
- `Review Desk`：人工复核每句台词、音频、画面、策略和异常。
- `Export Bridge`：导出 MP4、对齐表、剪映草稿包、Premiere XML/EDL 等。

## 文档

- [架构设计](docs/architecture.md)
- [调研笔记](docs/research-notes.md)
- [路线图](docs/roadmap.md)


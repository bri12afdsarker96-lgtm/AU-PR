# 调研笔记

## Fish Audio S2 Pro / Fish Speech

参考来源：

- GitHub 仓库：[fishaudio/fish-speech](https://github.com/fishaudio/fish-speech)
- 官方文档入口：[speech.fish.audio](https://speech.fish.audio/)
- 模型入口：[fishaudio/s2-pro](https://huggingface.co/fishaudio/s2-pro)

当前可作为 AU&PR 上游配音层的能力：

- 多语言 TTS，覆盖中文、英文等主流语言。
- 支持自然语言行内控制标签，例如语气、情绪、停顿、强调等。
- 支持短参考音频的音色克隆。
- 支持多说话人与多轮上下文，适合短剧对白和解说旁白。
- 适合封装成本地服务或命令行适配器。

需要注意：

- Fish Speech README 明确提示代码和模型权重使用 FISH AUDIO RESEARCH LICENSE。
- AU&PR 仓库不应直接收纳第三方模型权重。
- 商业化、分发和内置模型前，需要单独复核许可证与使用边界。

## 水星剪辑配音包对齐分镜

本机可读源码入口：

- `D:\GitHub\By\Mercury-Premiere-Pro`

已观察到的核心思路：

- `dub_bridge.py` 负责把外部逐句配音包、文本行和分镜片段整理成统一计划。
- `dub_sync.py` 负责实际对齐和 FFmpeg 合成。
- 输入以配音包目录和分镜视频目录为主。
- 音频文件支持 `.wav/.mp3/.m4a/.aac/.flac/.ogg`。
- 配音清单优先使用 `配音清单.xlsx`，也兼容 `配音清单*.csv`。
- 台词列只依赖 text/B 列，用于字幕，不强校验音频文件名。
- 音频和分镜按文件夹实际文件自然排序配对。
- 成片总时长等于所有成功配对的配音时长之和。
- 输出包含成片、`音画对齐表.csv` 和 `音画对齐表.json`。

对齐策略：

- 差值小于约 0.1 秒：保持原速。
- 视频长于音频：按裁剪锚点裁头、裁中或裁尾。
- 视频短于音频且差距较小：慢放视频。
- 视频短于音频且差距较大：末帧定格补齐。

可复用但应重构的点：

- 复用“配音是主轴”的产品逻辑。
- 复用 manifest + plan 的可审计产物设计。
- 复用自然排序和多编码 CSV 兼容思路。
- 对齐算法重写为独立 `align` 包，避免新软件被旧 UI 和旧项目结构绑住。

## AU&PR 的关键新增价值

水星剪辑目前默认配音来自外部软件。AU&PR 要补上的正是上游：

- 从脚本直接生成逐句配音。
- 统一管理角色音色、参考音频和情绪标签。
- 生成配音后自动写回 manifest。
- 单句配音质量不满意时，只重生该句并局部重算对齐。
- 最终保留“人工复核台”，让用户能看懂每句为什么这样裁、慢放或定格。

## 命名建议

- 显示名：`AU&PR`
- GitHub 仓库名建议：`AU-PR`
- 解释方向：`Audio-driven Production & Picture Reflow`


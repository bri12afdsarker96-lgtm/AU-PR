# 水星配音对齐工作室（Mercury Dub Align Studio）

独立新软件，复用水星剪辑对齐内核。**非商用**。
口径基准：`docs/配音对齐工作室架构与路线_20260722.md`。

一句话链路：**整篇克隆连贯配音 → whisper 逐行量时长 → 每句画面按时长裁/变速 → 一句一画面拼接 → 整条配音叠上（不切音频）→ 帧收口成片**。

## 启动

```powershell
$env:PYTHONPATH='source'
python -m dub_align_studio               # 启动软件（浏览器版界面，自动打开浏览器）
python -m dub_align_studio.cli web --port 8760   # 等价入口，可指定端口
python -m dub_align_studio.cli engines   # 命令行组件探测
python -m dub_align_studio.cli verify    # 端到端渲染自检（mock，无需 GPU）
```

界面为纯标准库实现（http.server + 单文件 HTML，无 Gradio/Flask），只绑
127.0.0.1 本机访问；四页签：一键成片 / 文本框 / 音色库 / 工具箱自检。
「工具箱自检」页即组件商店：whisper-cli / ggml 模型点击下载（SHA256 校验 +
断点续传），dots.tts / pyCapCut 点击 pip 安装，fish-speech 给部署指引。

## 使用（GUI 一键成片页）

1. 文案框一行一句（每句 ≥5 秒）；
2. 选配音引擎（dots.tts 本地 / fish-speech 本地服务 / mock 测试）与音色；
3. 选计时尺子（whisper 推荐；未装组件可用「均分兜底」先跑通）；
4. 选分镜目录（每行一个视频，按文件名序配对）与输出目录；
5. 字幕：勾选烧录 + 自定义字号(px)；可勾「完成后导出剪映草稿」；
6. ▶ 一键成片，或按 ①②③④ 分步执行（分步结果在输出目录续跑）。

输出目录产物：`master.wav`（整轨配音）、`配音计时表.csv`、`成片.mp4`、`成片.srt`、
`成片_segments/`（逐行分镜段）、`剪映草稿包_*/`。

## 依赖（都不进安装包，按需补齐）

| 组件 | 用途 | 补齐方式 |
|---|---|---|
| ffmpeg/ffprobe | 渲染必需 | 随水星 vendor_tools 或系统安装 |
| dots.tts | 本地整篇克隆（2B/48kHz，Apache-2.0） | `pip install dots.tts` + GPU（≥6GB 显存） |
| fish-speech | 本地服务整篇克隆（S2 Pro） | 按官方文档启动 server（默认 127.0.0.1:8080） |
| whisper-cli + ggml | 逐行计时尺子 | 水星工具箱组件下载 |
| pyCapCut | 真实剪映草稿生成 | 剪辑机安装；缺失时交接包照常产出 |
| 中文字体 | 字幕烧录 | Windows 自带微软雅黑；缺失时只出 SRT |

音色库目录：`~/.dub_align_studio/音色库/`（一个音色 = 参考音频 + 可选转写）。

## 测试

```powershell
python -m unittest tests.test_dub_align_b tests.test_dub_align_engines ^
  tests.test_dub_align_aligners tests.test_dub_align_subtitles tests.test_dub_align_pipeline
```

确定性断言全走 mock；真组件（dots/fish/whisper/GPU）只进「工具箱自检」探测。

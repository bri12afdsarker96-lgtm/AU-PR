# AU&PR · 水星配音对齐工作室

> ✅ **统一分支说明（换机 / 新窗口先读）**：本分支已把**全套源码 + 测试 + 打包脚本 + 换机对齐工具**
> 合并到一处，是"什么都不缺"的完整项目——GitHub Desktop 同步本分支即得全部内容，不会残缺。
> 换机/新开对话请先读 [`docs/项目交接与颗粒度对齐_20260729.md`](docs/项目交接与颗粒度对齐_20260729.md)，
> 并双击仓库根 `生成环境快照.bat` 出本机体检。

AU&PR 是一个音频驱动的视频分镜对齐项目，现已从研究框架落地为可运行软件——
**水星配音对齐工作室（Mercury Dub Align Studio）**。本仓库包含全套源码，
后续研发以本仓库为主线。**非商用。**

## 核心链路（设计锁定版）

> 整篇文案 → fish-speech / dots.tts **整篇克隆**出连贯 master.wav（音色不漂移）
> → whisper **逐行量出真实时长**（只当尺子、不切音频、无静音吸附，每句 ≥5s）
> → 每句画面按该时长**裁剪 / 变速**对齐 → **一句一画面**顺序拼接
> → **整条 master 叠为唯一音轨** → 末段吸收帧舍入残差，**帧级收口**。

颗粒度纪律：音频只在篇级、对齐只在行级、帧只在渲染层。
口径基准：`docs/配音对齐工作室架构与路线_20260722.md`。

## 启动

零第三方依赖（纯 Python 标准库 + 本机 ffmpeg），Python 3.11+：

```powershell
$env:PYTHONPATH='source'
python -m dub_align_studio          # 启动软件（浏览器界面，自动打开）
python -m dub_align_studio.cli verify   # 端到端渲染自检（mock，无需 GPU）
```

界面四页签：**一键成片**（引擎/音色/尺子/五种画面比例/字幕字号/一键或①②③④分步）、
**文本框**（视频文字层：位置/字号/颜色/背景框/时间窗，手机实时预览）、
**音色库**（参考音频+转写，双引擎共用）、
**工具箱自检**（组件商店：whisper-cli/ggml 模型点击下载，dots.tts/pyCapCut 点击安装）。

## 目录

```
source/dub_align_studio/      软件本体（engines/尺子/渲染/字幕/文本框/剪映导出/Web 界面）
source/integrated_workbench/  水星剪辑内核（完整收录：对齐五档/语义匹配/组件下载等）
tests/                        软件测试套件（116 项，确定性走 mock）
docs/                         口径基准与早期研究笔记（architecture/roadmap/research-notes）
```

详细使用说明与外部组件对照表：`source/dub_align_studio/README.md`。

## 打包（Windows 发行版）

```powershell
python -m pip install pyinstaller
$env:PYTHONPATH='source'
pyinstaller --noconfirm --clean --onedir --name "水星配音对齐工作室" `
  --paths source `
  --add-data "source\dub_align_studio\web;dub_align_studio\web" `
  --collect-submodules dub_align_studio --collect-submodules integrated_workbench `
  --console source\dub_align_studio\launcher.py
```

产物在 `dist\水星配音对齐工作室\`；ffmpeg.exe/ffprobe.exe 放 exe 同目录或 PATH
（轻量安装纪律：重资产一律软件内工具箱按需下载，不进安装包）。

## 待本地收口（代码就绪）

- GPU 机器实跑 dots.tts，核对 `engines/dots_local.py` 生成调用签名；
- 启动 fish-speech 本地 server，核对 `/v1/tts` 请求字段；
- 剪辑机跑剪映交接包脚本，微调字号换算系数。

## 验证

```powershell
$env:PYTHONPATH='source'
python -m compileall -q source
python -m unittest discover -s tests -p "test_*.py"
```

## 来源与同源仓库

软件在水星剪辑仓库（Mercury-Premiere-Pro）内孵化完成（PR #5），本仓库为
全套源码主线；`source/integrated_workbench/` 为水星内核完整收录，
两侧后续演进以本仓库为准。

## 免费云端引擎 · Edge TTS（v0.7.70 新增）

新增一个**独立**的配音引擎 `edge_tts`，与 `dots_remote` **并列**（不替代）。
接的是开源项目 [wangwangit/tts](https://github.com/wangwangit/tts)（MIT），
把 Cloudflare Worker 包装成 OpenAI 兼容的 `POST /v1/audio/speech`，背后调用
**Microsoft Edge Read Aloud** 免费朗读接口。

用途定位：
- 无 GPU / dots.tts 服务器未起时的**兜底**，让整条流水线仍能出声；
- 展示 / 短剧 / 解说等**不需要克隆用户音色**的场景；
- 与 dots.tts 并存，用户随时切引擎。

**不做的事**：
- 不做音色克隆（20+ 中文预设声线：晓晓/晓伊/云希/云扬…）；
- 不读取参考音频，也不写入 `音色库/`；
- 不宣传"无限免费/高可用/可商用"——微软 Edge 内部朗读接口不承诺 SLA 与商用授权。

配置（软件内 · 无需 API Key）：
1. 到 [Cloudflare](https://dash.cloudflare.com/sign-up) 注册免费账户；
2. 打开 [wangwangit/tts 官方 Deploy Workers 直达链接](https://deploy.workers.cloudflare.com/?url=https://github.com/wangwangit/tts)，
   一键部署到你自己的账户 → 拿到 `https://xxx.workers.dev`；
3. 软件「工具箱 → 免费 Edge TTS」填这个地址 → 保存 → 「测试连接」；
4. 「配音引擎」下拉选「Edge TTS（免费云端 · 预设音色 · 不支持克隆）」→ 选声线/风格/音调 → 一键成片。

**免费额度归属你自己的 Cloudflare 账户**（个人使用绰绰有余）；作者公共 Worker
仅适合临时试用、共用限流。软件不预置任何公共 Worker 地址。

许可与归属：
- Worker 源码 [MIT](https://github.com/wangwangit/tts/blob/master/LICENSE)——本项目
  仅调用其 HTTP 接口，未复制其代码；接口字段命名为兼容而对齐。
- MIT 只覆盖 Worker 源码；Microsoft Edge 生成音频的商业发布授权不在此内，
  用户按自己场景自负合规责任。

云配版口径不变：`MERCURY_CLOUD_ONLY=1` 时引擎下拉曝光 `dots_remote + edge_tts`
两选；顶栏 ☁ 云 GPU 状态灯仍**只代表 dots_remote**（Edge TTS 走 Cloudflare，与
优云智算/云 GPU 会话完全独立，不会因用 Edge 而误报"GPU 正在工作"）。

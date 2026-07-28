# C# 接入代码

这里的代码给本地剪辑器和发布模块做平滑接入。

- 本地剪辑器读取 `05_发布交接/02_剪辑交接包/editor_handoff.json`，自动填入新目录体系下的原始视频、背景、贴图、输出目录和生成参数。
- 本地剪辑器读取 `edit_handoff.json`，导入自动剪辑后的分镜素材、音频占位和时间线顺序。
- 发布模块读取 `05_发布交接/03_发布工具交接/publish_handoff.json` 或 `发布工具导入队列.csv`，自动生成账号发布任务。
- 本地剪辑器可回写 `editor_result.csv`，工作台会导入到 `03_二创生产/01_生成待审`。
- 发布工具可回写 `publish_status.csv`，工作台会更新发布队列、发布链接、失败原因和发布记录。

适用环境：.NET Framework 4.7 / WinForms。若本地剪辑器目录里已有 `Newtonsoft.Json.dll`，可直接引用。

## 本地剪辑器接入

```csharp
var handoff = HandoffLoader.LoadEditor(
    @"D:\短剧项目\项目名\05_发布交接\02_剪辑交接包\editor_handoff.json");

LocalEditorWinFormsBinder.Apply(
    handoff,
    txtAFolder,
    txtBFolder,
    txtCFolder,
    txtOutputFolder,
    numCopies,
    txtLog);
```

## 自动剪辑分镜接入

```csharp
var edit = HandoffLoader.LoadLatestAutoEditFromEditor(
    @"D:\短剧项目\项目名\05_发布交接\02_剪辑交接包\editor_handoff.json");

var timelineRows = AutoEditTimelineImporter.FromHandoff(edit);
foreach (var row in timelineRows)
{
    // row.TrackType 为 video 或 audio_placeholder
    // 按 row.TimelineStartSeconds 和 row.DurationSeconds 放入你的时间线或表格
}
```

## 本地剪辑器回写

剪辑器完成精修后，把输出视频写成 `editor_result.csv`：

```csharp
StatusCallbackWriter.WriteEditorResultCsv(
    handoff.EditorResultImportDir,
    new []
    {
        new EditorResultCallbackDto
        {
            OutputVideo = @"D:\外部剪辑器输出\成片001.mp4",
            Status = "ok",
            Title = "外部精修成片",
            Message = "已完成"
        }
    });
```

随后在工作台点击“导入剪辑器回写”，视频会进入待审。

## 发布模块接入

```csharp
var publish = HandoffLoader.LoadPublish(
    @"D:\短剧项目\项目名\05_发布交接\03_发布工具交接\publish_handoff.json");

var rows = PublishQueueImporter.FromHandoff(publish);
```

如果暂时不读 JSON，也可以直接读 CSV：

```csharp
var rows = HandoffLoader.LoadPublishQueueCsv(
    @"D:\短剧项目\项目名\05_发布交接\03_发布工具交接\发布工具导入队列.csv");
```

## 发布状态回写

发布工具上传成功或失败后，把状态写回 `publish.StatusCallbackDir`：

```csharp
StatusCallbackWriter.WritePublishStatusCsv(
    publish.StatusCallbackDir,
    new []
    {
        new PublishStatusCallbackDto
        {
            VideoPath = rows[0].VideoPath,
            Status = "published",
            Platform = "douyin",
            PlatformUrl = "https://example.com/published-video",
            ExternalTaskId = "task_001",
            PublishedAt = DateTime.Now.ToString("s"),
            PublishError = ""
        }
    });
```

随后在工作台点击“导入发布回写”，队列会更新为已发布；失败时 `Status` 写 `failed`，`PublishError` 写失败原因。

## 推荐 NuGet 依赖

本地剪辑器/发布工具侧如需扩展能力，优先使用以下许可证友好的 NuGet 包：

| NuGet 包 | 许可证 | 用途 |
| --- | --- | --- |
| `Newtonsoft.Json` | MIT | 交接 JSON 读写（现有代码已引用） |
| `FFMpegCore` | MIT | 媒体分析、转码、抽帧；`GlobalFFOptions` 的 `BinaryFolder` 指到水星剪辑 `assets/ffmpeg/bin` 即可复用同一套 FFmpeg |
| `SoundFingerprinting` | MIT | 剪辑器侧音频/视频指纹查重；与工作台的 fpcalc 音频指纹口径互补 |

GPL/AGPL 的 .NET 组件（如 Subtitle Edit 的 libse）只做外部进程调用或流程参考，不直接引用进剪辑器代码。

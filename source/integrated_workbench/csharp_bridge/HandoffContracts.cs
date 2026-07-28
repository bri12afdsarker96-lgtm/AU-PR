using System.Collections.Generic;
using Newtonsoft.Json;

namespace IntegratedWorkbenchBridge
{
    public sealed class VideoRecipeDto
    {
        [JsonProperty("mode")]
        public string Mode { get; set; }

        [JsonProperty("copies_per_source")]
        public int CopiesPerSource { get; set; }

        [JsonProperty("b_opacity")]
        public double BOpacity { get; set; }

        [JsonProperty("c_scale")]
        public double CScale { get; set; }

        [JsonProperty("c_opacity")]
        public double COpacity { get; set; }

        [JsonProperty("background_blur")]
        public double BackgroundBlur { get; set; }

        [JsonProperty("output_width")]
        public int OutputWidth { get; set; }

        [JsonProperty("output_height")]
        public int OutputHeight { get; set; }
    }

    public sealed class EditorHandoffDto
    {
        [JsonProperty("created_at")]
        public string CreatedAt { get; set; }

        [JsonProperty("project_name")]
        public string ProjectName { get; set; }

        [JsonProperty("drama_name")]
        public string DramaName { get; set; }

        [JsonProperty("project_root")]
        public string ProjectRoot { get; set; }

        [JsonProperty("directory_version")]
        public string DirectoryVersion { get; set; }

        [JsonProperty("local_editor_exe")]
        public string LocalEditorExe { get; set; }

        [JsonProperty("recipe")]
        public VideoRecipeDto Recipe { get; set; }

        [JsonProperty("source_count")]
        public int SourceCount { get; set; }

        [JsonProperty("background_count")]
        public int BackgroundCount { get; set; }

        [JsonProperty("sticker_count")]
        public int StickerCount { get; set; }

        [JsonProperty("source_folder")]
        public string SourceFolder { get; set; }

        [JsonProperty("background_folder")]
        public string BackgroundFolder { get; set; }

        [JsonProperty("sticker_folder")]
        public string StickerFolder { get; set; }

        [JsonProperty("review_output")]
        public string ReviewOutput { get; set; }

        [JsonProperty("ready_publish")]
        public string ReadyPublish { get; set; }

        [JsonProperty("auto_edit_output")]
        public string AutoEditOutput { get; set; }

        [JsonProperty("latest_auto_edit_handoff")]
        public string LatestAutoEditHandoff { get; set; }

        [JsonProperty("task_plan_csv")]
        public string TaskPlanCsv { get; set; }

        [JsonProperty("editor_result_import_dir")]
        public string EditorResultImportDir { get; set; }

        [JsonProperty("expected_editor_result_files")]
        public List<string> ExpectedEditorResultFiles { get; set; }

        [JsonProperty("expected_editor_result_fields")]
        public List<string> ExpectedEditorResultFields { get; set; }
    }

    public sealed class AutoEditShotDto
    {
        [JsonProperty("shot_id")]
        public string ShotId { get; set; }

        [JsonProperty("source_path")]
        public string SourcePath { get; set; }

        [JsonProperty("selected_clip")]
        public string SelectedClip { get; set; }

        [JsonProperty("audio_placeholder")]
        public string AudioPlaceholder { get; set; }

        [JsonProperty("start_seconds")]
        public double StartSeconds { get; set; }

        [JsonProperty("duration_seconds")]
        public double DurationSeconds { get; set; }

        [JsonProperty("speaker")]
        public string Speaker { get; set; }

        [JsonProperty("voice_type")]
        public string VoiceType { get; set; }

        [JsonProperty("text")]
        public string Text { get; set; }

        [JsonProperty("note")]
        public string Note { get; set; }
    }

    public sealed class AutoEditHandoffDto
    {
        [JsonProperty("created_at")]
        public string CreatedAt { get; set; }

        [JsonProperty("project_name")]
        public string ProjectName { get; set; }

        [JsonProperty("drama_name")]
        public string DramaName { get; set; }

        [JsonProperty("project_root")]
        public string ProjectRoot { get; set; }

        [JsonProperty("task_type")]
        public string TaskType { get; set; }

        [JsonProperty("work_dir")]
        public string WorkDir { get; set; }

        [JsonProperty("selected_dir")]
        public string SelectedDir { get; set; }

        [JsonProperty("audio_dir")]
        public string AudioDir { get; set; }

        [JsonProperty("preview_path")]
        public string PreviewPath { get; set; }

        [JsonProperty("plan_csv")]
        public string PlanCsv { get; set; }

        [JsonProperty("import_csv")]
        public string ImportCsv { get; set; }

        [JsonProperty("contact_sheet")]
        public string ContactSheet { get; set; }

        [JsonProperty("selected_count")]
        public int SelectedCount { get; set; }

        [JsonProperty("total_seconds")]
        public double TotalSeconds { get; set; }

        [JsonProperty("shots")]
        public List<AutoEditShotDto> Shots { get; set; }
    }

    public sealed class PublishAccountDto
    {
        [JsonProperty("group")]
        public string Group { get; set; }

        [JsonProperty("account_id")]
        public string AccountId { get; set; }

        [JsonProperty("nickname")]
        public string Nickname { get; set; }

        [JsonProperty("publish_count")]
        public string PublishCount { get; set; }

        [JsonProperty("interval_minutes")]
        public string IntervalMinutes { get; set; }

        [JsonProperty("scheduled_time")]
        public string ScheduledTime { get; set; }

        [JsonProperty("chrome_port")]
        public string ChromePort { get; set; }

        [JsonProperty("enabled")]
        public string Enabled { get; set; }
    }

    public sealed class PublishQueueRowDto
    {
        [JsonProperty("group")]
        public string Group { get; set; }

        [JsonProperty("account_id")]
        public string AccountId { get; set; }

        [JsonProperty("nickname")]
        public string Nickname { get; set; }

        [JsonProperty("video_path")]
        public string VideoPath { get; set; }

        [JsonProperty("source_type")]
        public string SourceType { get; set; }

        [JsonProperty("original_video_path")]
        public string OriginalVideoPath { get; set; }

        [JsonProperty("title")]
        public string Title { get; set; }

        [JsonProperty("cover_path")]
        public string CoverPath { get; set; }

        [JsonProperty("description")]
        public string Description { get; set; }

        [JsonProperty("tags")]
        public string Tags { get; set; }

        [JsonProperty("scheduled_time")]
        public string ScheduledTime { get; set; }

        [JsonProperty("chrome_port")]
        public string ChromePort { get; set; }

        [JsonProperty("platform")]
        public string Platform { get; set; }

        [JsonProperty("platform_url")]
        public string PlatformUrl { get; set; }

        [JsonProperty("external_task_id")]
        public string ExternalTaskId { get; set; }

        [JsonProperty("status")]
        public string Status { get; set; }

        [JsonProperty("published_at")]
        public string PublishedAt { get; set; }

        [JsonProperty("publish_error")]
        public string PublishError { get; set; }

        [JsonProperty("updated_at")]
        public string UpdatedAt { get; set; }
    }

    public sealed class PublishHandoffDto
    {
        [JsonProperty("created_at")]
        public string CreatedAt { get; set; }

        [JsonProperty("project_name")]
        public string ProjectName { get; set; }

        [JsonProperty("drama_name")]
        public string DramaName { get; set; }

        [JsonProperty("directory_version")]
        public string DirectoryVersion { get; set; }

        [JsonProperty("publish_accounts_csv")]
        public string PublishAccountsCsv { get; set; }

        [JsonProperty("publish_queue_csv")]
        public string PublishQueueCsv { get; set; }

        [JsonProperty("publish_queue_dir")]
        public string PublishQueueDir { get; set; }

        [JsonProperty("ready_video_dir")]
        public string ReadyVideoDir { get; set; }

        [JsonProperty("packaged_video_dir")]
        public string PackagedVideoDir { get; set; }

        [JsonProperty("status_callback_dir")]
        public string StatusCallbackDir { get; set; }

        [JsonProperty("expected_status_files")]
        public List<string> ExpectedStatusFiles { get; set; }

        [JsonProperty("expected_status_fields")]
        public List<string> ExpectedStatusFields { get; set; }

        [JsonProperty("queue")]
        public List<PublishQueueRowDto> Queue { get; set; }
    }

    public sealed class PublishStatusCallbackDto
    {
        [JsonProperty("video_path")]
        public string VideoPath { get; set; }

        [JsonProperty("status")]
        public string Status { get; set; }

        [JsonProperty("platform")]
        public string Platform { get; set; }

        [JsonProperty("platform_url")]
        public string PlatformUrl { get; set; }

        [JsonProperty("external_task_id")]
        public string ExternalTaskId { get; set; }

        [JsonProperty("published_at")]
        public string PublishedAt { get; set; }

        [JsonProperty("publish_error")]
        public string PublishError { get; set; }
    }

    public sealed class EditorResultCallbackDto
    {
        [JsonProperty("output_video")]
        public string OutputVideo { get; set; }

        [JsonProperty("status")]
        public string Status { get; set; }

        [JsonProperty("title")]
        public string Title { get; set; }

        [JsonProperty("message")]
        public string Message { get; set; }
    }
}

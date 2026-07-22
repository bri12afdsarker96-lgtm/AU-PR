using System.Collections.Generic;

namespace IntegratedWorkbenchBridge
{
    public sealed class AutoEditTimelineRow
    {
        public string TrackType { get; set; }
        public int Order { get; set; }
        public string ShotId { get; set; }
        public string Path { get; set; }
        public double TimelineStartSeconds { get; set; }
        public double DurationSeconds { get; set; }
        public string Speaker { get; set; }
        public string VoiceType { get; set; }
        public string Text { get; set; }
    }

    public static class AutoEditTimelineImporter
    {
        public static List<AutoEditTimelineRow> FromHandoff(AutoEditHandoffDto handoff)
        {
            var rows = new List<AutoEditTimelineRow>();
            if (handoff == null || handoff.Shots == null)
            {
                return rows;
            }

            var timeline = 0.0;
            for (var index = 0; index < handoff.Shots.Count; index++)
            {
                var shot = handoff.Shots[index];
                var order = index + 1;
                rows.Add(new AutoEditTimelineRow
                {
                    TrackType = "video",
                    Order = order,
                    ShotId = shot.ShotId,
                    Path = shot.SelectedClip,
                    TimelineStartSeconds = timeline,
                    DurationSeconds = shot.DurationSeconds,
                    Speaker = shot.Speaker,
                    VoiceType = shot.VoiceType,
                    Text = shot.Text
                });
                rows.Add(new AutoEditTimelineRow
                {
                    TrackType = "audio_placeholder",
                    Order = order,
                    ShotId = shot.ShotId,
                    Path = shot.AudioPlaceholder,
                    TimelineStartSeconds = timeline,
                    DurationSeconds = shot.DurationSeconds,
                    Speaker = shot.Speaker,
                    VoiceType = shot.VoiceType,
                    Text = shot.Text
                });
                timeline += shot.DurationSeconds;
            }
            return rows;
        }
    }
}

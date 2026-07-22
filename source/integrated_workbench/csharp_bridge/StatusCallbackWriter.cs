using System.Collections.Generic;
using System.IO;
using System.Text;

namespace IntegratedWorkbenchBridge
{
    public static class StatusCallbackWriter
    {
        public static string WritePublishStatusCsv(string callbackDir, IEnumerable<PublishStatusCallbackDto> rows)
        {
            Directory.CreateDirectory(callbackDir);
            var path = Path.Combine(callbackDir, "publish_status.csv");
            using (var writer = new StreamWriter(path, false, new UTF8Encoding(true)))
            {
                writer.WriteLine("video_path,status,platform,platform_url,external_task_id,published_at,publish_error");
                foreach (var row in rows)
                {
                    writer.WriteLine(string.Join(",", new[]
                    {
                        Csv(row.VideoPath),
                        Csv(row.Status),
                        Csv(row.Platform),
                        Csv(row.PlatformUrl),
                        Csv(row.ExternalTaskId),
                        Csv(row.PublishedAt),
                        Csv(row.PublishError)
                    }));
                }
            }
            return path;
        }

        public static string WriteEditorResultCsv(string callbackDir, IEnumerable<EditorResultCallbackDto> rows)
        {
            Directory.CreateDirectory(callbackDir);
            var path = Path.Combine(callbackDir, "editor_result.csv");
            using (var writer = new StreamWriter(path, false, new UTF8Encoding(true)))
            {
                writer.WriteLine("output_video,status,title,message");
                foreach (var row in rows)
                {
                    writer.WriteLine(string.Join(",", new[]
                    {
                        Csv(row.OutputVideo),
                        Csv(row.Status),
                        Csv(row.Title),
                        Csv(row.Message)
                    }));
                }
            }
            return path;
        }

        private static string Csv(string value)
        {
            value = value ?? string.Empty;
            if (value.Contains("\"") || value.Contains(",") || value.Contains("\n") || value.Contains("\r"))
            {
                return "\"" + value.Replace("\"", "\"\"") + "\"";
            }
            return value;
        }
    }
}

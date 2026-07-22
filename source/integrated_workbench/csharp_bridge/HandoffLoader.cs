using System;
using System.Collections.Generic;
using System.IO;
using Microsoft.VisualBasic.FileIO;
using Newtonsoft.Json;

namespace IntegratedWorkbenchBridge
{
    public static class HandoffLoader
    {
        public static EditorHandoffDto LoadEditor(string jsonPath)
        {
            EnsureFile(jsonPath);
            return JsonConvert.DeserializeObject<EditorHandoffDto>(File.ReadAllText(jsonPath));
        }

        public static PublishHandoffDto LoadPublish(string jsonPath)
        {
            EnsureFile(jsonPath);
            return JsonConvert.DeserializeObject<PublishHandoffDto>(File.ReadAllText(jsonPath));
        }

        public static AutoEditHandoffDto LoadAutoEdit(string jsonPath)
        {
            EnsureFile(jsonPath);
            return JsonConvert.DeserializeObject<AutoEditHandoffDto>(File.ReadAllText(jsonPath));
        }

        public static AutoEditHandoffDto LoadLatestAutoEditFromEditor(string editorHandoffJsonPath)
        {
            var editor = LoadEditor(editorHandoffJsonPath);
            EnsureFile(editor.LatestAutoEditHandoff);
            return LoadAutoEdit(editor.LatestAutoEditHandoff);
        }

        public static List<PublishQueueRowDto> LoadPublishQueueCsv(string csvPath)
        {
            EnsureFile(csvPath);
            var rows = new List<PublishQueueRowDto>();
            using (var parser = new TextFieldParser(csvPath))
            {
                parser.TextFieldType = FieldType.Delimited;
                parser.SetDelimiters(",");
                parser.HasFieldsEnclosedInQuotes = true;

                var header = parser.ReadFields();
                while (!parser.EndOfData)
                {
                    var fields = parser.ReadFields();
                    if (fields == null || fields.Length == 0)
                    {
                        continue;
                    }

                    rows.Add(new PublishQueueRowDto
                    {
                        Group = GetField(header, fields, "group"),
                        AccountId = GetField(header, fields, "account_id"),
                        Nickname = GetField(header, fields, "nickname"),
                        VideoPath = GetField(header, fields, "video_path"),
                        Title = GetField(header, fields, "title"),
                        CoverPath = GetField(header, fields, "cover_path"),
                        ScheduledTime = GetField(header, fields, "scheduled_time"),
                        Status = GetField(header, fields, "status")
                    });
                }
            }
            return rows;
        }

        private static void EnsureFile(string path)
        {
            if (string.IsNullOrWhiteSpace(path) || !File.Exists(path))
            {
                throw new FileNotFoundException("交接文件不存在", path);
            }
        }

        private static string GetField(string[] header, string[] fields, string name)
        {
            if (header == null)
            {
                return string.Empty;
            }

            for (var index = 0; index < header.Length; index++)
            {
                if (string.Equals(header[index], name, StringComparison.OrdinalIgnoreCase))
                {
                    return index < fields.Length ? fields[index] : string.Empty;
                }
            }
            return string.Empty;
        }
    }
}

using System.Collections.Generic;
using System.IO;
using System.Linq;

namespace IntegratedWorkbenchBridge
{
    public static class PublishQueueImporter
    {
        public static List<PublishQueueRowDto> FromHandoff(PublishHandoffDto handoff)
        {
            if (handoff == null || handoff.Queue == null)
            {
                return new List<PublishQueueRowDto>();
            }

            return handoff.Queue
                .Where(row => row != null)
                .Where(row => string.IsNullOrWhiteSpace(row.Status) || row.Status == "待发布")
                .Where(row => File.Exists(row.VideoPath))
                .ToList();
        }

        public static Dictionary<string, List<PublishQueueRowDto>> GroupByAccount(IEnumerable<PublishQueueRowDto> rows)
        {
            return rows
                .GroupBy(row => row.AccountId ?? string.Empty)
                .ToDictionary(group => group.Key, group => group.ToList());
        }
    }
}


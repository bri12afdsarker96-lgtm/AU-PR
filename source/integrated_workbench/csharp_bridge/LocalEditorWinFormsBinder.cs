using System.IO;
using System.Windows.Forms;

namespace IntegratedWorkbenchBridge
{
    public static class LocalEditorWinFormsBinder
    {
        public static void Apply(
            EditorHandoffDto handoff,
            TextBox aFolderTextBox,
            TextBox bFolderTextBox,
            TextBox cFolderTextBox,
            TextBox outputFolderTextBox,
            NumericUpDown copiesInput,
            TextBox logTextBox)
        {
            if (handoff == null)
            {
                MessageBox.Show("交接包为空。");
                return;
            }

            var projectRoot = handoff.ProjectRoot;
            aFolderTextBox.Text = FirstNonEmpty(handoff.SourceFolder, Path.Combine(projectRoot, "01_素材入库", "A_原始视频"));
            bFolderTextBox.Text = FirstNonEmpty(handoff.BackgroundFolder, Path.Combine(projectRoot, "01_素材入库", "B_去重背景"));
            cFolderTextBox.Text = FirstNonEmpty(handoff.StickerFolder, Path.Combine(projectRoot, "01_素材入库", "C_贴图素材"));
            outputFolderTextBox.Text = handoff.ReviewOutput;

            if (handoff.Recipe != null)
            {
                copiesInput.Value = Clamp(handoff.Recipe.CopiesPerSource, copiesInput.Minimum, copiesInput.Maximum);
            }

            if (logTextBox != null)
            {
                logTextBox.AppendText("已导入集成工作台项目：" + handoff.ProjectName + "\r\n");
                logTextBox.AppendText("A 原视频：" + handoff.SourceCount + " 个\r\n");
                logTextBox.AppendText("B 去重背景：" + handoff.BackgroundCount + " 个\r\n");
                logTextBox.AppendText("C 贴图素材：" + handoff.StickerCount + " 个\r\n");
            }
        }

        private static decimal Clamp(int value, decimal min, decimal max)
        {
            if (value < min)
            {
                return min;
            }
            if (value > max)
            {
                return max;
            }
            return value;
        }

        private static string FirstNonEmpty(string value, string fallback)
        {
            return string.IsNullOrWhiteSpace(value) ? fallback : value;
        }
    }
}

using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;
using System.Reflection;

[assembly: AssemblyTitle("剪辑工作台")]
[assembly: AssemblyProduct("剪辑工作台")]
[assembly: AssemblyVersion("0.6.0.0")]

internal static class Launcher
{
    // Only launch the installed engine; never copy accounts or embed machine paths.
    [STAThread]
    private static int Main(string[] args)
    {
        string log = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "JianyingEngine", "workbench", "launcher.log");
        try
        {
            string python = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "JianyingEngine", "venv", "Scripts", "pythonw.exe");
            if (!File.Exists(python)) throw new InvalidOperationException("找不到本机剪辑引擎环境，请先安装引擎及 desktop 依赖。\n" + python);
            string workspace = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "剪辑工作台");
            if (args.Length != 0)
            {
                if (args.Length != 2 || args[0] != "--workspace") throw new ArgumentException("仅支持 --workspace 文件夹路径。");
                workspace = Path.GetFullPath(args[1]);
            }
            Directory.CreateDirectory(workspace);
            Directory.CreateDirectory(Path.GetDirectoryName(log));
            var start = new ProcessStartInfo(python, "-m jianying_engine.workbench --desktop --workspace " + Quote(workspace));
            start.WorkingDirectory = workspace;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.RedirectStandardOutput = true;
            start.RedirectStandardError = true;
            start.StandardOutputEncoding = Encoding.UTF8;
            start.StandardErrorEncoding = Encoding.UTF8;
            start.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            start.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";
            var errors = new StringBuilder();
            using (var child = new Process { StartInfo = start })
            {
                child.OutputDataReceived += (sender, e) => { /* Drain stdout; never mirror tokens. */ };
                child.ErrorDataReceived += (sender, e) => { if (e.Data != null) lock(errors) { string line = e.Data + "\n"; errors.Append(line, 0, Math.Min(line.Length, 32000 - errors.Length)); } };
                child.Start();
                child.BeginOutputReadLine();
                child.BeginErrorReadLine();
                child.WaitForExit();
                if (child.ExitCode != 0)
                {
                    lock(errors) File.WriteAllText(log, DateTime.Now.ToString("s") + "\n" + errors, Encoding.UTF8);
                    throw new InvalidOperationException("工作台未能正常运行。请查看日志：\n" + log);
                }
            }
            return 0;
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "剪辑工作台", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    private static string Quote(string value)
    {
        // Windows command-line quoting, including a root path ending with backslash.
        return "\"" + System.Text.RegularExpressions.Regex.Replace(value, "(\\\\*)\"", "$1$1\\\"")
            + new string('\\', value.Length - value.TrimEnd('\\').Length) + "\"";
    }
}




// Complete-package starter. No Python discovery, downloads, unpacking or setup.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Windows.Forms;

internal static class PortableLauncher
{
    private static string Quote(string value)
    {
        var result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in value ?? "")
        {
            if (c == '\\') slashes++;
            else if (c == '"') { result.Append('\\', slashes * 2 + 1); result.Append('"'); slashes = 0; }
            else { result.Append('\\', slashes); slashes = 0; result.Append(c); }
        }
        result.Append('\\', slashes * 2); result.Append('"');
        return result.ToString();
    }

    private static string Json(string value)
    {
        return "\"" + value.Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\r", "\\r").Replace("\n", "\\n").Replace("\t", "\\t") + "\"";
    }

    private static string Diagnostic(string program, Exception error)
    {
        var native = error as Win32Exception;
        string text = "青简工具箱完整包启动失败\r\n时间：" + DateTime.Now.ToString("s")
            + "\r\n系统：" + Environment.OSVersion + "\r\n主程序：" + program
            + "\r\n错误：" + error.Message + "\r\nHRESULT：0x" + error.HResult.ToString("X8")
            + (native != null ? "\r\nWin32错误码：" + native.NativeErrorCode : "")
            + "\r\n\r\n请确认压缩包已完整解压。若 Windows 在启动器出现前拒绝访问，"
            + "请运行程序文件中的启动诊断.cmd，并交由本机管理员核查拦截记录。";
        string[] roots = { Path.Combine(Path.GetDirectoryName(program), "启动诊断"),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "青简工具箱", "启动诊断") };
        foreach (string root in roots)
        {
            try
            {
                Directory.CreateDirectory(root);
                string path = Path.Combine(root, "启动失败-" + DateTime.Now.ToString("yyyyMMdd-HHmmss") + ".txt");
                File.WriteAllText(path, text, new UTF8Encoding(true));
                return text + "\r\n\r\n诊断文件：" + path;
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
        return text;
    }

    [STAThread]
    private static int Main(string[] args)
    {
        try { Console.SetOut(new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false)) { AutoFlush = true }); }
        catch (IOException) { }
        bool noUi = false, check = false, forwarding = false;
        var appArgs = new List<string>();
        foreach (string arg in args)
        {
            if (!forwarding && arg == "--") { forwarding = true; continue; }
            if (!forwarding && arg == "--no-ui") { noUi = true; continue; }
            if (!forwarding && (arg == "--check-only" || arg == "--prepare-only")) { check = true; continue; }
            appArgs.Add(arg);
        }
        string executable = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "程序文件", "青简工具箱.exe");
        try
        {
            if (!File.Exists(executable)) throw new FileNotFoundException("未找到完整包主程序，请完整解压后启动。", executable);
            if (check)
            {
                Console.WriteLine("{\"event\":\"check\",\"available\":true,\"portable\":true,\"bootstrap\":\"skipped\",\"executable\":" + Json(executable) + "}");
                return 0;
            }
            var info = new ProcessStartInfo(executable, String.Join(" ", appArgs.Select(Quote)))
            { UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = Path.GetDirectoryName(executable) };
            foreach (string key in new[] { "PYTHONHOME", "PYTHONPATH", "QINGJIAN_BOOTSTRAP_READY", "QINGJIAN_LIBREOFFICE",
                "QINGJIAN_NATIVE_COMPONENTS", "QINGJIAN_NATIVE_ROOTS", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH" })
                info.EnvironmentVariables.Remove(key);
            using (Process process = Process.Start(info))
            {
                if (process == null) throw new IOException("主程序未能启动。");
                if (noUi) { process.WaitForExit(); return process.ExitCode; }
            }
            return 0;
        }
        catch (Exception error)
        {
            string detail = Diagnostic(executable, error);
            if (noUi || check) Console.WriteLine("{\"event\":\"error\",\"message\":" + Json(detail) + "}");
            else MessageBox.Show(detail, "青简工具箱启动诊断", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 2;
        }
    }
}

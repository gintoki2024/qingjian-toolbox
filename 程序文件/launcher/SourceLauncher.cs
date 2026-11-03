// Source launcher: .NET Framework 4.5+, no Python dependency.
// Build with launcher/build_launcher.py. This file deliberately uses only
// framework assemblies; dependency preparation lives in bootstrap.py.
// Standalone rebuild (run from the source root, without Python):
//   %WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe /nologo
//   /target:winexe /platform:anycpu /codepage:65001 /r:System.Core.dll /r:System.Drawing.dll
//   /r:System.Windows.Forms.dll /r:System.Web.Extensions.dll
//   /r:System.IO.Compression.dll /r:System.IO.Compression.FileSystem.dll
//   /out:启动青简工具箱.exe launcher\SourceLauncher.cs
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Net;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

namespace Qingjian.SourceLauncher
{
    internal sealed class Options
    {
        internal string Python;
        internal string RuntimeHome;
        internal bool CheckOnly;
        internal bool NoUi;
        internal bool PrepareOnly;
        internal bool NoDiscovery;
        internal bool ChooseEnvironment;
        internal bool ManagedOnly;
        internal bool ManagedExplicit;
        internal bool RuntimeExplicit;
        internal bool PythonExplicit;
        internal bool ConfigurationRequired;
        internal readonly List<string> AppArgs = new List<string>();

        internal static Options Parse(string[] args)
        {
            var o = new Options();
            o.RuntimeHome = Environment.GetEnvironmentVariable("QINGJIAN_RUNTIME_HOME");
            o.RuntimeExplicit = !String.IsNullOrWhiteSpace(o.RuntimeHome);
            for (int i = 0; i < args.Length; i++)
            {
                string arg = args[i];
                if (arg == "--") { o.AppArgs.AddRange(args.Skip(i + 1)); break; }
                if (arg == "--python" || arg == "--runtime-home")
                {
                    if (++i >= args.Length || String.IsNullOrWhiteSpace(args[i]))
                        throw new ArgumentException(arg + " 缺少参数。");
                    if (arg == "--python") { o.Python = Path.GetFullPath(args[i]); o.PythonExplicit = true; }
                    else { o.RuntimeHome = args[i]; o.RuntimeExplicit = true; }
                }
                else if (arg == "--check-only") { o.CheckOnly = true; o.NoUi = true; }
                else if (arg == "--no-ui") o.NoUi = true;
                else if (arg == "--prepare-only") o.PrepareOnly = true;
                else if (arg == "--no-discovery") o.NoDiscovery = true;
                else if (arg == "--choose-environment") o.ChooseEnvironment = true;
                else if (arg == "--deploy-runtime") { o.ManagedOnly = true; o.ManagedExplicit = true; }
                else throw new ArgumentException("不支持的启动参数：" + arg + "。主程序参数请放在 -- 之后。");
            }
            if (!String.IsNullOrWhiteSpace(o.RuntimeHome)) o.RuntimeHome = Path.GetFullPath(o.RuntimeHome);
            if (o.ManagedExplicit && o.PythonExplicit)
                throw new ArgumentException("--deploy-runtime 与 --python 分别用于部署和复用环境，请选择其中一种。");
            return o;
        }
    }

    internal sealed class StartupChoice
    {
        public int schema_version = 1;
        public string mode;
        public string python;
        public string runtime_home;
    }

    internal static class StartupSettings
    {
        internal static string FilePath(string sourceDir) { return Path.Combine(sourceDir, "config", "startup-selection.json"); }
        internal static StartupChoice Read(string sourceDir)
        {
            string path = FilePath(sourceDir);
            if (!File.Exists(path)) return null;
            try
            {
                if (new FileInfo(path).Length > 16384) throw new InvalidDataException();
                var choice = Json.Serializer().Deserialize<StartupChoice>(File.ReadAllText(path, Encoding.UTF8));
                if (choice == null || choice.schema_version != 1 || (choice.mode != "reuse" && choice.mode != "managed") ||
                    String.IsNullOrWhiteSpace(choice.runtime_home) || !Path.IsPathRooted(choice.runtime_home) ||
                    (choice.mode == "reuse" && (String.IsNullOrWhiteSpace(choice.python) || !Path.IsPathRooted(choice.python))))
                    throw new InvalidDataException();
                choice.runtime_home = Path.GetFullPath(choice.runtime_home);
                if (!String.IsNullOrWhiteSpace(choice.python)) choice.python = Path.GetFullPath(choice.python);
                return choice;
            }
            catch (Exception ex)
            { throw new InvalidDataException("启动位置配置无法读取，请使用 --choose-environment 重新选择：" + path, ex); }
        }

        internal static void Resolve(Options options, string sourceDir)
        {
            StartupChoice saved = null;
            try { saved = Read(sourceDir); }
            catch (InvalidDataException) { if (!options.ChooseEnvironment) throw; }
            options.ConfigurationRequired = saved == null && !options.RuntimeExplicit && !options.PythonExplicit;
            if (!options.RuntimeExplicit)
                options.RuntimeHome = saved != null ? saved.runtime_home : Path.Combine(sourceDir, "runtime");
            if (!options.PythonExplicit && !options.ManagedExplicit && saved != null)
            {
                options.ManagedOnly = saved.mode == "managed";
                if (!options.ManagedOnly) options.Python = saved.python;
            }
            options.RuntimeHome = Path.GetFullPath(options.RuntimeHome);
        }

        internal static void Save(string sourceDir, Options options)
        {
            string path = FilePath(sourceDir), directory = Path.GetDirectoryName(path);
            string staged = Path.Combine(directory, ".startup-selection-" + Guid.NewGuid().ToString("N") + ".tmp");
            Directory.CreateDirectory(directory);
            try
            {
                var choice = new StartupChoice { mode = options.ManagedOnly ? "managed" : "reuse", python = options.Python ?? "", runtime_home = options.RuntimeHome };
                File.WriteAllText(staged, Json.Encode(choice), new UTF8Encoding(false));
                if (File.Exists(path)) File.Replace(staged, path, null);
                else File.Move(staged, path);
            }
            finally { if (File.Exists(staged)) File.Delete(staged); }
        }

        internal static string SourceDirectory(string root)
        {
            string nested = Path.Combine(root, "程序文件");
            return File.Exists(Path.Combine(nested, "main.py")) ? nested : root;
        }

        internal static string PortableExecutable(string root)
        {
            string nested = Path.Combine(root, "程序文件", "青简工具箱.exe");
            return File.Exists(nested) && !File.Exists(Path.Combine(root, "程序文件", "main.py")) ? nested : null;
        }
    }

    internal sealed class PythonInfo
    {
        public string python;
        public int major;
        public int minor;
        public int micro;
        public int bits;
        internal string Version { get { return major + "." + minor + "." + micro; } }
    }

    internal sealed class SourceUrl
    {
        public string name;
        public string url;
    }

    internal sealed class PythonSpec
    {
        public string version;
        public string filename;
        public string sha256;
        public long bytes;
        public SourceUrl[] urls;
    }

    internal sealed class Manifest
    {
        public PythonSpec python;
    }

    internal sealed class ReadyInfo
    {
        internal string Python;
        internal readonly Dictionary<string, string> Environment = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
    }

    internal static class Json
    {
        internal static JavaScriptSerializer Serializer()
        {
            return new JavaScriptSerializer { MaxJsonLength = 1024 * 1024, RecursionLimit = 32 };
        }
        internal static string Encode(object value) { return Serializer().Serialize(value); }
        internal static Dictionary<string, object> Object(string value)
        {
            return Serializer().Deserialize<Dictionary<string, object>>(value);
        }
        internal static string Text(Dictionary<string, object> d, string key)
        {
            object value;
            return d != null && d.TryGetValue(key, out value) && value != null ? Convert.ToString(value, CultureInfo.InvariantCulture) : "";
        }
    }

    // A job only owns the worker started by this launcher and its descendants.
    // Closing it never searches for, or terminates, any other Python process.
    internal sealed class ChildJob : IDisposable
    {
        [StructLayout(LayoutKind.Sequential)]
        private struct BasicLimits
        {
            public long PerProcessUserTimeLimit, PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass, SchedulingClass;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct IoCounters
        {
            public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount;
            public ulong ReadTransferCount, WriteTransferCount, OtherTransferCount;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct ExtendedLimits
        {
            public BasicLimits BasicLimitInformation;
            public IoCounters IoInfo;
            public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed;
        }
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        private static extern IntPtr CreateJobObject(IntPtr attributes, string name);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(IntPtr job, int infoClass, IntPtr info, uint size);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
        [DllImport("kernel32.dll")]
        private static extern bool CloseHandle(IntPtr handle);
        private IntPtr handle;
        internal bool IsAssigned { get { return handle != IntPtr.Zero; } }
        internal ChildJob(Process child)
        {
            handle = CreateJobObject(IntPtr.Zero, null);
            if (handle == IntPtr.Zero) return;
            var limits = new ExtendedLimits();
            limits.BasicLimitInformation.LimitFlags = 0x2000; // KILL_ON_JOB_CLOSE
            int length = Marshal.SizeOf(typeof(ExtendedLimits));
            IntPtr memory = Marshal.AllocHGlobal(length);
            try
            {
                Marshal.StructureToPtr(limits, memory, false);
                if (!SetInformationJobObject(handle, 9, memory, (uint)length) || !AssignProcessToJobObject(handle, child.Handle))
                { CloseHandle(handle); handle = IntPtr.Zero; }
            }
            finally { Marshal.FreeHGlobal(memory); }
        }
        public void Dispose()
        {
            if (handle != IntPtr.Zero) { CloseHandle(handle); handle = IntPtr.Zero; }
        }
    }

    internal static class SafeFiles
    {
        internal static string Hash(string path)
        {
            using (var input = File.OpenRead(path))
            using (var sha = SHA256.Create())
                return BitConverter.ToString(sha.ComputeHash(input)).Replace("-", "").ToLowerInvariant();
        }

        internal static bool Matches(string path, PythonSpec spec)
        {
            try { return File.Exists(path) && new FileInfo(path).Length == spec.bytes && Hash(path) == spec.sha256.ToLowerInvariant(); }
            catch (IOException) { return false; }
            catch (UnauthorizedAccessException) { return false; }
        }

        internal static void ValidateSpec(PythonSpec s)
        {
            if (s == null || !Regex.IsMatch(s.version ?? "", @"^3\.(10|11|12|13|14)\.\d+$") ||
                !Regex.IsMatch(s.filename ?? "", @"^[A-Za-z0-9_.-]+\.zip$") ||
                !Regex.IsMatch(s.sha256 ?? "", @"^[A-Fa-f0-9]{64}$") || s.bytes < 1 || s.bytes > 256L * 1024 * 1024 ||
                s.urls == null || s.urls.Length == 0 || s.urls.Length > 8)
                throw new InvalidDataException("runtime-sources.json 中的 Python 下载信息无效。");
            foreach (var source in s.urls)
            {
                Uri uri;
                if (source == null || !Uri.TryCreate(source.url, UriKind.Absolute, out uri) || uri.Scheme != Uri.UriSchemeHttps ||
                    !String.IsNullOrEmpty(uri.UserInfo))
                    throw new InvalidDataException("Python 下载地址必须使用不含凭据的 HTTPS 链接。");
            }
        }

        private static string EntryPath(string root, string name)
        {
            if (String.IsNullOrEmpty(name) || name.IndexOf('\0') >= 0 || name.IndexOf(':') >= 0 ||
                name.StartsWith("/") || name.StartsWith("\\"))
                throw new InvalidDataException("运行环境压缩包含有不安全路径。");
            string[] parts = name.Replace('\\', '/').Split('/');
            for (int i = 0; i < parts.Length; i++)
            {
                string part = parts[i];
                if (part.Length == 0 && i == parts.Length - 1) continue;
                if (part.Length == 0 || part == "." || part == ".." || part.EndsWith(".") || part.EndsWith(" ") ||
                    part.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0 ||
                    Regex.IsMatch(part, @"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\.|$)", RegexOptions.IgnoreCase))
                    throw new InvalidDataException("运行环境压缩包含有不安全路径。");
            }
            string full = Path.GetFullPath(Path.Combine(root, name.Replace('/', Path.DirectorySeparatorChar)));
            string prefix = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            if (!full.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("运行环境压缩包试图写入目标目录之外。");
            return full;
        }

        internal static void ExtractZip(string archive, string destination, CancellationToken token, Action<int> progress)
        {
            // Destination must be a newly-created, empty staging directory.
            if (!Directory.Exists(destination) || Directory.EnumerateFileSystemEntries(destination).Any() ||
                (File.GetAttributes(destination) & FileAttributes.ReparsePoint) != 0)
                throw new InvalidDataException("运行环境暂存目录不安全或不为空。");
            using (var zip = ZipFile.OpenRead(archive))
            {
                if (zip.Entries.Count == 0 || zip.Entries.Count > 50000)
                    throw new InvalidDataException("运行环境压缩包文件数量异常。");
                long total = 0;
                var files = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                foreach (var entry in zip.Entries)
                {
                    token.ThrowIfCancellationRequested();
                    string target = EntryPath(destination, entry.FullName);
                    int kind = (entry.ExternalAttributes >> 16) & 0xF000;
                    if (kind == 0xA000 || (entry.ExternalAttributes & (int)FileAttributes.ReparsePoint) != 0)
                        throw new InvalidDataException("运行环境压缩包包含链接，已停止解压。");
                    bool directory = entry.FullName.EndsWith("/") || entry.FullName.EndsWith("\\");
                    if (!directory && !files.Add(target)) throw new InvalidDataException("运行环境压缩包包含重复文件。");
                    if (entry.Length < 0 || entry.Length > 256L * 1024 * 1024 || total > 1024L * 1024 * 1024 - entry.Length)
                        throw new InvalidDataException("运行环境压缩包展开大小异常。");
                    total += entry.Length;
                }
                int completed = 0;
                byte[] buffer = new byte[128 * 1024];
                foreach (var entry in zip.Entries)
                {
                    token.ThrowIfCancellationRequested();
                    string target = EntryPath(destination, entry.FullName);
                    if (entry.FullName.EndsWith("/") || entry.FullName.EndsWith("\\")) Directory.CreateDirectory(target);
                    else
                    {
                        Directory.CreateDirectory(Path.GetDirectoryName(target));
                        using (var input = entry.Open())
                        using (var output = new FileStream(target, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                        {
                            long written = 0;
                            int read;
                            while ((read = input.Read(buffer, 0, buffer.Length)) > 0)
                            {
                                token.ThrowIfCancellationRequested();
                                written += read;
                                if (written > entry.Length) throw new InvalidDataException("运行环境文件大小与目录记录不符。");
                                output.Write(buffer, 0, read);
                            }
                            if (written != entry.Length) throw new InvalidDataException("运行环境文件解压不完整。");
                        }
                    }
                    progress(++completed * 100 / zip.Entries.Count);
                }
            }
        }

        internal static void DeleteOwnedStage(string runtimeHome, string stage)
        {
            string root = Path.GetFullPath(runtimeHome).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            string full = Path.GetFullPath(stage);
            if (!full.StartsWith(root, StringComparison.OrdinalIgnoreCase) ||
                !Path.GetFileName(full).StartsWith(".install-python-", StringComparison.Ordinal) ||
                !Directory.Exists(full)) return;
            // Do not recursively traverse a junction if a third party changed the staging area.
            var dirs = new Queue<string>(); dirs.Enqueue(full);
            while (dirs.Count > 0)
            {
                string current = dirs.Dequeue();
                if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0) return;
                foreach (string item in Directory.EnumerateFileSystemEntries(current))
                {
                    FileAttributes attrs = File.GetAttributes(item);
                    if ((attrs & FileAttributes.ReparsePoint) != 0) return;
                    if ((attrs & FileAttributes.Directory) != 0) dirs.Enqueue(item);
                }
            }
            Directory.Delete(full, true);
        }
    }

    internal sealed class Runner
    {
        private readonly Options options;
        private readonly string sourceDir;
        private readonly Action<Dictionary<string, object>> sink;
        private readonly object writeLock = new object();
        private readonly object readyLock = new object();
        private ReadyInfo ready;
        private string protocolError;
        private PythonSpec spec;
        private StreamWriter logFile;

        internal Runner(Options options, string sourceDir, Action<Dictionary<string, object>> sink)
        { this.options = options; this.sourceDir = sourceDir; this.sink = sink; }

        private void Emit(string type, string message, object percent)
        {
            var e = new Dictionary<string, object> { { "event", type }, { "message", message } };
            if (percent != null) e["percent"] = percent;
            Forward(e);
        }
        private void Forward(Dictionary<string, object> e)
        {
            lock (writeLock)
            {
                string line = Json.Encode(e);
                try { Console.WriteLine(line); Console.Out.Flush(); } catch (IOException) { }
                if (logFile != null) { logFile.WriteLine(line); logFile.Flush(); }
                if (sink != null) sink(e);
            }
        }

        private void LoadManifest()
        {
            string path = Path.Combine(sourceDir, "runtime-sources.json");
            if (!File.Exists(path)) throw new FileNotFoundException("源码包缺少 runtime-sources.json，请完整解压新版源码。", path);
            if (new FileInfo(path).Length > 1024 * 1024) throw new InvalidDataException("runtime-sources.json 文件大小异常。");
            Manifest manifest = Json.Serializer().Deserialize<Manifest>(File.ReadAllText(path, Encoding.UTF8));
            SafeFiles.ValidateSpec(manifest == null ? null : manifest.python);
            spec = manifest.python;
        }

        internal static string Quote(string value)
        {
            if (value == null) value = "";
            var b = new StringBuilder("\"");
            int slashes = 0;
            foreach (char c in value)
            {
                if (c == '\\') slashes++;
                else if (c == '"') { b.Append('\\', slashes * 2 + 1); b.Append('"'); slashes = 0; }
                else { b.Append('\\', slashes); slashes = 0; b.Append(c); }
            }
            b.Append('\\', slashes * 2); b.Append('"'); return b.ToString();
        }
        private static string Arguments(IEnumerable<string> args) { return String.Join(" ", args.Select(Quote)); }

        private static ProcessStartInfo StartInfo(string executable, IEnumerable<string> args, string workdir)
        {
            var info = new ProcessStartInfo(executable, Arguments(args));
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.WorkingDirectory = workdir;
            info.EnvironmentVariables.Remove("PYTHONHOME");
            info.EnvironmentVariables.Remove("PYTHONPATH");
            info.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            info.EnvironmentVariables["PYTHONUTF8"] = "1";
            return info;
        }

        private static void StopOwnedTree(Process child)
        {
            // Fallback for hosts which prohibit nested Windows jobs. The child
            // handle remains open, so its PID cannot be recycled during this call.
            if (child.HasExited) return;
            string taskkill = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "taskkill.exe");
            if (File.Exists(taskkill))
            {
                try
                {
                    var start = StartInfo(taskkill, new[] { "/PID", child.Id.ToString(CultureInfo.InvariantCulture), "/T", "/F" }, Environment.CurrentDirectory);
                    start.RedirectStandardOutput = true; start.RedirectStandardError = true;
                    using (Process stop = Process.Start(start)) { if (stop != null) stop.WaitForExit(5000); }
                }
                catch (System.ComponentModel.Win32Exception) { }
                catch (InvalidOperationException) { }
            }
            try { if (!child.HasExited) child.Kill(); } catch (InvalidOperationException) { }
            catch (System.ComponentModel.Win32Exception) { }
        }

        private static int RunCaptured(string executable, IEnumerable<string> args, string directory, int timeoutSeconds,
            CancellationToken token, Action<string> output, Action<string> error)
        {
            var start = StartInfo(executable, args, directory);
            start.RedirectStandardOutput = true; start.RedirectStandardError = true;
            start.StandardOutputEncoding = Encoding.UTF8; start.StandardErrorEncoding = Encoding.UTF8;
            using (var process = new Process { StartInfo = start })
            {
                Exception callbackError = null;
                object callbackLock = new object();
                process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e)
                {
                    if (e.Data == null) return;
                    try { output(e.Data); } catch (Exception ex) { lock (callbackLock) callbackError = ex; }
                };
                process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e)
                {
                    if (e.Data == null) return;
                    try { error(e.Data); } catch (Exception ex) { lock (callbackLock) callbackError = ex; }
                };
                token.ThrowIfCancellationRequested();
                process.Start();
                using (var job = new ChildJob(process))
                {
                    process.BeginOutputReadLine(); process.BeginErrorReadLine();
                    var timer = Stopwatch.StartNew();
                    try
                    {
                        while (!process.WaitForExit(100))
                        {
                            token.ThrowIfCancellationRequested();
                            if (timer.Elapsed.TotalSeconds > timeoutSeconds) throw new TimeoutException("本次环境准备操作超时，请检查网络后重试。");
                        }
                        // Flush the asynchronous line handlers after process termination.
                        process.WaitForExit();
                        lock (callbackLock) { if (callbackError != null) throw new IOException("读取环境准备输出失败。", callbackError); }
                        return process.ExitCode;
                    }
                    catch
                    {
                        // Closing the job ends only this worker and its own descendants.
                        bool supervised = job.IsAssigned;
                        job.Dispose();
                        if (!supervised) StopOwnedTree(process);
                        try { if (!process.HasExited) process.Kill(); } catch (InvalidOperationException) { }
                        catch (System.ComponentModel.Win32Exception) { }
                        try { process.WaitForExit(5000); } catch (InvalidOperationException) { }
                        throw;
                    }
                }
            }
        }

        private PythonInfo Probe(string candidate, CancellationToken token)
        {
            try
            {
                if (String.IsNullOrWhiteSpace(candidate)) return null;
                candidate = Path.GetFullPath(candidate.Trim().Trim('"'));
                if (Path.GetFileName(candidate).Equals("pythonw.exe", StringComparison.OrdinalIgnoreCase))
                { string console = Path.Combine(Path.GetDirectoryName(candidate), "python.exe"); if (File.Exists(console)) candidate = console; }
                if (!File.Exists(candidate) || !candidate.EndsWith(".exe", StringComparison.OrdinalIgnoreCase) ||
                    candidate.IndexOf("\\WindowsApps\\", StringComparison.OrdinalIgnoreCase) >= 0) return null;
                string captured = "";
                string code = "import json,struct,sys,ssl;print(json.dumps({'python':sys.executable,'major':sys.version_info[0],'minor':sys.version_info[1],'micro':sys.version_info[2],'bits':struct.calcsize('P')*8}))";
                int status = RunCaptured(candidate, new[] { "-I", "-B", "-c", code }, sourceDir, 12, token,
                    delegate(string line) { if (line.TrimStart().StartsWith("{")) captured = line; }, delegate(string line) { });
                if (status != 0 || captured.Length == 0) return null;
                PythonInfo found = Json.Serializer().Deserialize<PythonInfo>(captured);
                if (found == null || found.major != 3 || found.minor < 10 || found.minor > 14 || found.bits != 64) return null;
                found.python = Path.GetFullPath(found.python);
                return File.Exists(found.python) ? found : null;
            }
            catch (OperationCanceledException) { throw; }
            catch (Exception) { return null; }
        }

        private static IEnumerable<string> PathExecutables(string filename)
        {
            string path = Environment.GetEnvironmentVariable("PATH") ?? "";
            foreach (string item in path.Split(Path.PathSeparator))
            {
                string result = null;
                try { if (!String.IsNullOrWhiteSpace(item)) result = Path.Combine(item.Trim().Trim('"'), filename); }
                catch (ArgumentException) { }
                if (result != null && File.Exists(result)) yield return result;
            }
        }

        private IEnumerable<string> Candidates(CancellationToken token)
        {
            var activated = new List<string>();
            var paths = new List<string>();
            var cached = new List<string>();
            if (!options.NoDiscovery && !options.ManagedOnly)
            {
                // Respect the environment activated by the caller before looking
                // at general installations. Never combine packages across them.
                foreach (string variable in new[] { "VIRTUAL_ENV", "CONDA_PREFIX" })
                {
                    string directory = Environment.GetEnvironmentVariable(variable);
                    if (String.IsNullOrWhiteSpace(directory)) continue;
                    try
                    {
                        activated.Add(Path.Combine(directory.Trim().Trim('"'), variable == "VIRTUAL_ENV" ? @"Scripts\python.exe" : "python.exe"));
                    }
                    catch (ArgumentException) { }
                }
                paths.AddRange(PathExecutables("python.exe"));
                foreach (string py in PathExecutables("py.exe").Take(3))
                {
                    if (py.IndexOf("\\WindowsApps\\", StringComparison.OrdinalIgnoreCase) >= 0) continue;
                    try
                    {
                        Action<string> parse = delegate(string line)
                        {
                            Match m = Regex.Match(line, @"([A-Za-z]:\\.+?\\python(?:w)?\.exe)\s*$", RegexOptions.IgnoreCase);
                            if (m.Success) { lock (paths) paths.Add(m.Groups[1].Value.Trim()); }
                        };
                        RunCaptured(py, new[] { "-0p" }, sourceDir, 8, token, parse, parse);
                    }
                    catch (OperationCanceledException) { throw; }
                    catch (Exception) { }
                }
                foreach (RegistryHive hive in new[] { RegistryHive.CurrentUser, RegistryHive.LocalMachine })
                foreach (RegistryView view in new[] { RegistryView.Registry64, RegistryView.Registry32 })
                {
                    try
                    {
                        using (RegistryKey root = RegistryKey.OpenBaseKey(hive, view))
                        using (RegistryKey core = root.OpenSubKey(@"SOFTWARE\Python\PythonCore"))
                        {
                            if (core == null) continue;
                            foreach (string name in core.GetSubKeyNames().Take(30))
                            using (RegistryKey install = core.OpenSubKey(name + @"\InstallPath"))
                            {
                                if (install == null) continue;
                                string exe = Convert.ToString(install.GetValue("ExecutablePath"));
                                if (String.IsNullOrWhiteSpace(exe))
                                { string dir = Convert.ToString(install.GetValue("")); if (!String.IsNullOrWhiteSpace(dir)) exe = Path.Combine(dir, "python.exe"); }
                                if (!String.IsNullOrWhiteSpace(exe)) paths.Add(exe);
                            }
                        }
                    }
                    catch (Exception) { }
                }
            }
            // A previously downloaded fallback must not shadow a system Python
            // discovered on PATH, through py.exe, or in the read-only registry.
            string runtimeRoot = options.RuntimeHome.TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            var discovered = new List<string>();
            foreach (string candidate in paths)
            {
                try
                {
                    string full = Path.GetFullPath(candidate);
                    if (full.StartsWith(runtimeRoot, StringComparison.OrdinalIgnoreCase)) cached.Add(full);
                    else discovered.Add(full);
                }
                catch (ArgumentException) { }
                catch (NotSupportedException) { }
                catch (PathTooLongException) { }
            }
            if (spec != null)
            {
                cached.Add(Path.Combine(options.RuntimeHome, "python-" + spec.version, "python.exe"));
                cached.Add(Path.Combine(options.RuntimeHome, "python-" + spec.version + "-" + spec.sha256.Substring(0, 12), "python.exe"));
            }
            string selection = Path.Combine(options.RuntimeHome, "python-selection.json");
            try
            {
                if (File.Exists(selection) && new FileInfo(selection).Length < 8192)
                {
                    string selected = Json.Text(Json.Object(File.ReadAllText(selection, Encoding.UTF8)), "python");
                    if (!String.IsNullOrWhiteSpace(selected) && Path.GetFullPath(selected).StartsWith(runtimeRoot, StringComparison.OrdinalIgnoreCase)) cached.Add(selected);
                }
            }
            catch (Exception) { }
            return activated.Concat(discovered).Concat(cached).Distinct(StringComparer.OrdinalIgnoreCase).Take(100).ToArray();
        }

        private PythonInfo FindPython(CancellationToken token)
        {
            if (!String.IsNullOrWhiteSpace(options.Python))
            {
                token.ThrowIfCancellationRequested();
                PythonInfo selected = Probe(options.Python, token);
                if (selected == null)
                    throw new ArgumentException("指定的 Python 无法运行或不符合要求：" + options.Python +
                        "\r\n请选择 Python 3.10–3.14 的 64 位解释器（须包含 ssl 模块）。未切换或修改其他 Python 环境。");
                return selected;
            }
            foreach (string candidate in Candidates(token))
            {
                token.ThrowIfCancellationRequested();
                PythonInfo found = Probe(candidate, token);
                if (found != null) return found;
            }
            return null;
        }

        internal PythonInfo DiscoverPython(CancellationToken token)
        {
            LoadManifest();
            return FindPython(token);
        }

        internal PythonInfo ValidatePython(string path, CancellationToken token) { return Probe(path, token); }

        private void Download(SourceUrl source, string target, CancellationToken token)
        {
            var request = (HttpWebRequest)WebRequest.Create(source.url);
            request.Method = "GET"; request.Timeout = 15000; request.ReadWriteTimeout = 20000;
            request.UserAgent = "QingjianSourceLauncher/1.0";
            request.AllowAutoRedirect = true; request.MaximumAutomaticRedirections = 5;
            ApplyProxyEnvironment(request);
            // No certificate or TLS validation is disabled for mirror fallback.
            using (token.Register(delegate { request.Abort(); }))
            using (var response = (HttpWebResponse)request.GetResponse())
            {
                if (response.ResponseUri.Scheme != Uri.UriSchemeHttps)
                    throw new InvalidDataException("下载源跳转到非 HTTPS 地址，已停止。");
                if (response.ContentLength >= 0 && response.ContentLength != spec.bytes)
                    throw new InvalidDataException("下载源返回的 Python 文件大小与清单不符。");
                using (var input = response.GetResponseStream())
                using (var output = new FileStream(target, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                {
                    long received = 0;
                    int lastPercent = -1;
                    byte[] buffer = new byte[128 * 1024];
                    var timer = Stopwatch.StartNew();
                    int read;
                    while ((read = input.Read(buffer, 0, buffer.Length)) > 0)
                    {
                        token.ThrowIfCancellationRequested();
                        if (timer.Elapsed.TotalMinutes > 15) throw new TimeoutException("当前下载源传输超时。");
                        received += read;
                        if (received > spec.bytes) throw new InvalidDataException("下载文件超过清单大小，已停止。");
                        output.Write(buffer, 0, read);
                        int percent = (int)(received * 100 / spec.bytes);
                        if (percent != lastPercent)
                        { Emit("progress", "正在从“" + source.name + "”下载 Python（" + percent + "%）", percent); lastPercent = percent; }
                    }
                    if (received != spec.bytes) throw new InvalidDataException("Python 下载未完成。");
                }
            }
        }

        private static void ApplyProxyEnvironment(HttpWebRequest request)
        {
            // HttpWebRequest reads Windows proxy settings but not the conventional
            // proxy environment variables used by Python and enterprise wrappers.
            string noProxy = Environment.GetEnvironmentVariable("NO_PROXY") ?? Environment.GetEnvironmentVariable("no_proxy") ?? "";
            foreach (string item in noProxy.Split(','))
            {
                string host = item.Trim();
                if (host.Length == 0) continue;
                if (host == "*" || request.RequestUri.Host.Equals(host.TrimStart('.'), StringComparison.OrdinalIgnoreCase) ||
                    request.RequestUri.Host.EndsWith("." + host.TrimStart('.'), StringComparison.OrdinalIgnoreCase))
                { request.Proxy = null; return; }
            }
            string value = Environment.GetEnvironmentVariable("HTTPS_PROXY") ?? Environment.GetEnvironmentVariable("https_proxy") ??
                Environment.GetEnvironmentVariable("HTTP_PROXY") ?? Environment.GetEnvironmentVariable("http_proxy");
            if (String.IsNullOrWhiteSpace(value)) return;
            Uri uri;
            if (!Uri.TryCreate(value, UriKind.Absolute, out uri) || (uri.Scheme != "http" && uri.Scheme != "https"))
                throw new InvalidDataException("网络代理环境变量格式无效，请检查本机代理设置。");
            var address = new UriBuilder(uri) { UserName = "", Password = "" };
            var proxy = new WebProxy(address.Uri);
            if (!String.IsNullOrEmpty(uri.UserInfo))
            {
                string[] credentials = uri.UserInfo.Split(new[] { ':' }, 2);
                proxy.Credentials = new NetworkCredential(Uri.UnescapeDataString(credentials[0]), credentials.Length > 1 ? Uri.UnescapeDataString(credentials[1]) : "");
            }
            request.Proxy = proxy;
        }

        private PythonInfo InstallPython(CancellationToken token)
        {
            string downloads = Path.Combine(options.RuntimeHome, "downloads");
            Directory.CreateDirectory(downloads);
            string archive = Path.Combine(downloads, spec.sha256.Substring(0, 16) + "-" + spec.filename);
            if (!SafeFiles.Matches(archive, spec))
            {
                bool downloaded = false;
                var errors = new List<string>();
                foreach (SourceUrl source in spec.urls)
                {
                    token.ThrowIfCancellationRequested();
                    string part = Path.Combine(downloads, ".python-" + Guid.NewGuid().ToString("N") + ".partial");
                    try
                    {
                        Emit("log", "当前电脑未发现可用的 64 位 Python，尝试下载源：" + source.name, null);
                        Download(source, part, token);
                        Emit("log", "正在校验 Python 文件大小和 SHA-256…", null);
                        if (!SafeFiles.Matches(part, spec)) throw new InvalidDataException("Python 安装包 SHA-256 校验失败。");
                        if (File.Exists(archive)) archive = Path.Combine(downloads, spec.sha256.Substring(0, 16) + "-" + Guid.NewGuid().ToString("N") + "-" + spec.filename);
                        File.Move(part, archive);
                        downloaded = true; break;
                    }
                    catch (OperationCanceledException) { throw; }
                    catch (Exception ex)
                    {
                        token.ThrowIfCancellationRequested();
                        errors.Add(source.name + "：" + ex.Message);
                        Emit("log", source.name + " 暂不可用，继续尝试备用源。" + ex.Message, null);
                    }
                    finally { if (File.Exists(part)) { try { File.Delete(part); } catch (IOException) { } catch (UnauthorizedAccessException) { } } }
                }
                if (!downloaded) throw new IOException("所有 Python 下载源均不可用。可连接网络后重试，或使用完整便携版。\r\n" + String.Join("\r\n", errors));
            }
            else Emit("log", "已找到通过校验的 Python 缓存，离线继续准备。", null);
            token.ThrowIfCancellationRequested();
            // Short staging names leave room for standard-library package paths
            // even on Windows hosts which retain the legacy MAX_PATH limit.
            string stage = Path.Combine(options.RuntimeHome, ".install-python-" + Guid.NewGuid().ToString("N").Substring(0, 12));
            Directory.CreateDirectory(stage);
            bool moved = false;
            try
            {
                int lastPercent = -1;
                SafeFiles.ExtractZip(archive, stage, token, delegate(int percent)
                { if (percent != lastPercent) { Emit("progress", "正在展开私有 Python 环境（" + percent + "%）", percent); lastPercent = percent; } });
                PythonInfo python = Probe(Path.Combine(stage, "python.exe"), token);
                if (python == null || python.Version != spec.version) throw new InvalidDataException("Python 包已通过文件校验，但展开后的环境无法运行或版本不符。");
                string target = Path.Combine(options.RuntimeHome, "python-" + spec.version);
                if (Directory.Exists(target)) target += "-" + spec.sha256.Substring(0, 12);
                if (Directory.Exists(target)) target += "-" + Guid.NewGuid().ToString("N").Substring(0, 8);
                Directory.Move(stage, target); moved = true;
                python = Probe(Path.Combine(target, "python.exe"), token);
                if (python == null) throw new InvalidDataException("私有 Python 环境移动后自检失败。");
                File.WriteAllText(Path.Combine(options.RuntimeHome, "python-selection.json"), Json.Encode(new { python = python.python, version = python.Version }), new UTF8Encoding(false));
                return python;
            }
            finally
            {
                if (!moved) { try { SafeFiles.DeleteOwnedStage(options.RuntimeHome, stage); } catch (IOException) { } catch (UnauthorizedAccessException) { } }
            }
        }

        private void OnBootstrapLine(string line)
        {
            if (line.Length > 1024 * 1024) { Emit("log", "环境准备输出过长，已省略一行。", null); return; }
            Dictionary<string, object> e = null;
            try { if (line.TrimStart().StartsWith("{")) e = Json.Object(line); } catch (Exception) { }
            if (e == null) { if (!String.IsNullOrWhiteSpace(line)) Emit("log", line, null); return; }
            if (Json.Text(e, "event") == "ready")
            {
                var candidate = new ReadyInfo { Python = Json.Text(e, "python") };
                object raw;
                if (e.TryGetValue("environment", out raw) && raw is Dictionary<string, object>)
                    foreach (var kv in (Dictionary<string, object>)raw)
                    {
                        if (!(kv.Value is string) || String.IsNullOrEmpty(kv.Key) || kv.Key.IndexOfAny(new[] { '=', '\0' }) >= 0 || ((string)kv.Value).IndexOf('\0') >= 0)
                        {
                            lock (readyLock) protocolError = "环境准备程序返回了无效的环境变量。";
                            Emit("error", "环境准备程序返回了无效的环境变量。", null);
                            return;
                        }
                        candidate.Environment[kv.Key] = (string)kv.Value;
                    }
                lock (readyLock) ready = candidate;
            }
            Forward(e);
        }

        private ReadyInfo Prepare(PythonInfo python, CancellationToken token)
        {
            string bootstrap = Path.Combine(sourceDir, "bootstrap.py");
            if (!File.Exists(bootstrap)) throw new FileNotFoundException("源码包缺少 bootstrap.py，请完整解压新版源码。", bootstrap);
            string logDir = Path.Combine(options.RuntimeHome, "logs");
            Directory.CreateDirectory(logDir);
            string eventPath = Path.Combine(logDir, "bootstrap-" + DateTime.UtcNow.ToString("yyyyMMdd-HHmmss", CultureInfo.InvariantCulture) + "-" + Guid.NewGuid().ToString("N").Substring(0, 8) + ".jsonl");
            lock (readyLock) { ready = null; protocolError = null; }
            Emit("log", "选中的本地 Python：" + python.python + "（" + python.Version + "，64 位）", null);
            Emit("log", "正在检查并补齐本地 Python 组件；已满足要求的组件会直接复用。", null);
            int code = RunCaptured(python.python,
                new[] { "-E", "-X", "utf8", bootstrap, "--prepare-only", "--runtime-home", options.RuntimeHome, "--events", eventPath },
                sourceDir, 45 * 60, token, OnBootstrapLine, delegate(string line) { if (!String.IsNullOrWhiteSpace(line)) Emit("log", line, null); });
            ReadyInfo result;
            lock (readyLock) result = ready;
            // An events file is also accepted if a worker could not write stdout.
            if (result == null && File.Exists(eventPath))
            {
                foreach (string line in File.ReadLines(eventPath, Encoding.UTF8))
                { try { var e = Json.Object(line); if (Json.Text(e, "event") == "ready") OnBootstrapLine(line); } catch (Exception) { } }
                lock (readyLock) result = ready;
            }
            if (code != 0) throw new IOException("环境准备未完成（退出码 " + code + "）。请按上方具体原因处理后重试。\r\n准备日志：" + eventPath);
            lock (readyLock) { if (protocolError != null) throw new InvalidDataException(protocolError); }
            if (result == null || String.IsNullOrWhiteSpace(result.Python) || !Path.IsPathRooted(result.Python) ||
                !result.Python.EndsWith(".exe", StringComparison.OrdinalIgnoreCase) || !File.Exists(result.Python))
                throw new InvalidDataException("环境准备程序未返回有效的就绪结果。\r\n准备日志：" + eventPath);
            result.Environment["QINGJIAN_RUNTIME_HOME"] = options.RuntimeHome;
            return result;
        }

        private void StartApp(ReadyInfo info, CancellationToken token)
        {
            token.ThrowIfCancellationRequested();
            string main = Path.Combine(sourceDir, "main.py");
            if (!File.Exists(main)) throw new FileNotFoundException("源码包缺少 main.py，请完整解压新版源码。", main);
            var args = new List<string> { "-E", "-X", "utf8", main }; args.AddRange(options.AppArgs);
            var start = StartInfo(info.Python, args, sourceDir);
            foreach (var kv in info.Environment) start.EnvironmentVariables[kv.Key] = kv.Value;
            Emit("log", "环境已就绪，正在打开青简工具箱…", null);
            using (Process app = Process.Start(start))
            {
                // The app is intentionally outside the bootstrap job: it survives launcher exit.
                if (app == null) throw new IOException("未能创建主程序进程。");
                var timer = Stopwatch.StartNew();
                try
                {
                    while (timer.Elapsed.TotalSeconds < 15)
                    {
                        token.ThrowIfCancellationRequested();
                        if (app.WaitForExit(100))
                        {
                            if (app.ExitCode != 0) throw new IOException("主程序启动失败（退出码 " + app.ExitCode + "）。环境日志已保留，可重试或查看源码启动说明。");
                            return;
                        }
                        app.Refresh();
                        if (app.MainWindowHandle != IntPtr.Zero) return;
                    }
                }
                catch (OperationCanceledException) { StopOwnedTree(app); throw; }
            }
        }

        internal int Run(CancellationToken token)
        {
            ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12;
            if (!Environment.Is64BitOperatingSystem) throw new PlatformNotSupportedException("青简工具箱需要 64 位 Windows 系统。");
            LoadManifest();
            Emit("log", "正在检查本机 Python 环境…", null);
            if (options.CheckOnly)
            {
                PythonInfo found = FindPython(token);
                Forward(new Dictionary<string, object> { { "event", "check" }, { "available", found != null },
                    { "python", found == null ? "" : found.python }, { "version", found == null ? "" : found.Version },
                    { "bits", found == null ? 0 : found.bits }, { "runtime_home", options.RuntimeHome },
                    { "configuration_required", options.ConfigurationRequired }, { "mode", options.ManagedOnly ? "managed" : "reuse" } });
                return found == null ? 3 : 0;
            }
            Directory.CreateDirectory(options.RuntimeHome);
            string temporary = Path.Combine(options.RuntimeHome, "temp");
            Directory.CreateDirectory(temporary);
            // These are process-local settings, inherited only by our workers.
            // pip --cache-dir is additionally explicit because --isolated ignores
            // pip environment settings. No machine/user environment is changed.
            Environment.SetEnvironmentVariable("QINGJIAN_RUNTIME_HOME", options.RuntimeHome);
            Environment.SetEnvironmentVariable("TEMP", temporary);
            Environment.SetEnvironmentVariable("TMP", temporary);
            Environment.SetEnvironmentVariable("TMPDIR", temporary);
            Environment.SetEnvironmentVariable("PIP_CACHE_DIR", Path.Combine(options.RuntimeHome, "pip-cache"));
            string logs = Path.Combine(options.RuntimeHome, "logs"); Directory.CreateDirectory(logs);
            string logfile = Path.Combine(logs, "launcher-" + DateTime.UtcNow.ToString("yyyyMMdd-HHmmss", CultureInfo.InvariantCulture) + "-" + Guid.NewGuid().ToString("N").Substring(0, 8) + ".jsonl");
            using (logFile = new StreamWriter(logfile, false, new UTF8Encoding(false)))
            {
                Emit("log", "准备日志：" + logfile, null);
                string mutexName;
                using (var sha = SHA256.Create())
                    mutexName = "Local\\QingjianSourceSetup-" + BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(options.RuntimeHome.ToUpperInvariant()))).Replace("-", "").Substring(0, 24);
                ReadyInfo result;
                using (var mutex = new Mutex(false, mutexName))
                {
                    bool owned = false;
                    try
                    {
                        try { owned = mutex.WaitOne(0); } catch (AbandonedMutexException) { owned = true; }
                        if (!owned) Emit("log", "另一个启动窗口正在准备同一环境，正在等待…", null);
                        while (!owned)
                        { token.ThrowIfCancellationRequested(); try { owned = mutex.WaitOne(250); } catch (AbandonedMutexException) { owned = true; } }
                        PythonInfo python = FindPython(token);
                        if (python == null) python = InstallPython(token);
                        result = Prepare(python, token);
                    }
                    finally { if (owned) mutex.ReleaseMutex(); }
                }
                if (!options.PrepareOnly) StartApp(result, token);
                Emit("log", options.PrepareOnly ? "源码环境准备完成。" : "青简工具箱已启动。", 100);
            }
            logFile = null;
            return 0;
        }
    }

    internal sealed class EnvironmentChoiceForm : Form
    {
        private readonly Options options;
        private readonly string sourceDir;
        private readonly RadioButton reuse = new RadioButton();
        private readonly RadioButton managed = new RadioButton();
        private readonly TextBox python = new TextBox();
        private readonly TextBox location = new TextBox();
        private readonly Label detail = new Label();
        private readonly Button proceed = new Button();
        private readonly CancellationTokenSource cancellation = new CancellationTokenSource();
        internal EnvironmentChoiceForm(Options options, string sourceDir)
        {
            this.options = options; this.sourceDir = sourceDir;
            Text = "青简工具箱 · 选择运行环境";
            Font = new Font("Microsoft YaHei UI", 9F);
            ClientSize = new Size(690, 475); MinimumSize = new Size(620, 450);
            StartPosition = FormStartPosition.CenterScreen;
            BackColor = Color.FromArgb(248, 249, 252);
            var body = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(26, 22, 26, 18), ColumnCount = 1, RowCount = 9 };
            foreach (int height in new[] { 38, 46, 28, 36, 32, 26, 38, 100, 50 }) body.RowStyles.Add(new RowStyle(SizeType.Absolute, height));
            body.RowStyles[7] = new RowStyle(SizeType.Percent, 100);
            var heading = new Label { Text = "先选位置，再准备组件", Font = new Font(Font.FontFamily, 17F, FontStyle.Bold), Dock = DockStyle.Fill, ForeColor = Color.FromArgb(37, 43, 62) };
            var description = new Label { Text = "可复用本机 Python，也可部署到 D / E 盘等位置。确认前不会下载组件或创建运行目录。", Dock = DockStyle.Fill, ForeColor = Color.FromArgb(95, 104, 126) };
            reuse.Text = "复用已有 Python，只补充缺失的组件"; reuse.Dock = DockStyle.Fill;
            managed.Text = "在指定位置部署共享 Python"; managed.Dock = DockStyle.Fill;
            var pythonRow = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2 };
            pythonRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100)); pythonRow.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 86));
            python.Dock = DockStyle.Fill; python.Text = options.Python ?? "";
            var pythonBrowse = new Button { Text = "选择…", Dock = DockStyle.Fill };
            pythonBrowse.Click += delegate
            {
                using (var dialog = new OpenFileDialog { Title = "选择已有的 64 位 Python", Filter = "Python 解释器|python.exe;pythonw.exe|可执行文件|*.exe", CheckFileExists = true })
                    if (dialog.ShowDialog(this) == DialogResult.OK) { python.Text = dialog.FileName; reuse.Checked = true; }
            };
            pythonRow.Controls.Add(python, 0, 0); pythonRow.Controls.Add(pythonBrowse, 1, 0);
            var locationLabel = new Label { Text = "运行组件与下载缓存位置", Dock = DockStyle.Fill };
            var locationRow = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2 };
            locationRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100)); locationRow.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 86));
            location.Text = options.RuntimeHome; location.Dock = DockStyle.Fill;
            var locationBrowse = new Button { Text = "选择…", Dock = DockStyle.Fill };
            locationBrowse.Click += delegate
            {
                using (var dialog = new FolderBrowserDialog { Description = "选择运行组件所在文件夹，可选择 D / E 盘。所选目录将保存 Python、转换引擎、缓存和临时文件。", ShowNewFolderButton = true })
                {
                    if (Directory.Exists(location.Text)) dialog.SelectedPath = location.Text;
                    if (dialog.ShowDialog(this) == DialogResult.OK) location.Text = dialog.SelectedPath;
                }
            };
            locationRow.Controls.Add(location, 0, 0); locationRow.Controls.Add(locationBrowse, 1, 0);
            detail.Dock = DockStyle.Fill; detail.Padding = new Padding(10); detail.BorderStyle = BorderStyle.FixedSingle;
            detail.ForeColor = Color.FromArgb(73, 84, 111); detail.BackColor = Color.White;
            var buttons = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.RightToLeft, Padding = new Padding(0, 10, 0, 0) };
            proceed.Text = "确认并继续"; proceed.Size = new Size(112, 32); proceed.Enabled = false;
            var cancel = new Button { Text = "取消", Size = new Size(88, 32), DialogResult = DialogResult.Cancel };
            buttons.Controls.Add(proceed); buttons.Controls.Add(cancel); CancelButton = cancel;
            body.Controls.Add(heading, 0, 0); body.Controls.Add(description, 0, 1); body.Controls.Add(reuse, 0, 2);
            body.Controls.Add(pythonRow, 0, 3); body.Controls.Add(managed, 0, 4); body.Controls.Add(locationLabel, 0, 5);
            body.Controls.Add(locationRow, 0, 6); body.Controls.Add(detail, 0, 7); body.Controls.Add(buttons, 0, 8);
            Controls.Add(body);
            Action update = delegate
            {
                python.Enabled = pythonBrowse.Enabled = reuse.Checked;
                detail.Text = reuse.Checked ? "组件安装到所选 Python；使用同一解释器的其他程序可以直接复用。如果该 Python 不可写，可能使用其标准用户组件目录。若希望组件全部在所选磁盘，请选择“部署共享 Python”。"
                    : "Python、依赖、转换引擎、下载缓存和大型临时文件都放在上述位置；不创建项目虚拟环境，不修改系统 PATH。其他源码可指定该 Python 继续复用。";
            };
            reuse.CheckedChanged += delegate { update(); }; managed.CheckedChanged += delegate { update(); };
            if (options.ManagedOnly) managed.Checked = true; else reuse.Checked = true;
            proceed.Click += delegate { Confirm(); };
            Shown += delegate { Discover(); };
            FormClosed += delegate { cancellation.Cancel(); };
        }

        private void Discover()
        {
            detail.Text = "正在只读检查本机 Python；尚未下载或创建运行目录…";
            ThreadPool.QueueUserWorkItem(delegate
            {
                PythonInfo found = null;
                try { found = new Runner(options, sourceDir, null).DiscoverPython(cancellation.Token); }
                catch (Exception) { }
                if (IsDisposed || !IsHandleCreated) return;
                try { BeginInvoke((Action)delegate
                {
                    if (IsDisposed) return;
                    if (found != null) { python.Text = found.python; if (!options.ManagedOnly) reuse.Checked = true; }
                    else if (String.IsNullOrWhiteSpace(python.Text)) managed.Checked = true;
                    detail.Text = reuse.Checked ? "已显示实际 Python 路径。只补其中缺失或不兼容的组件；复用现有 Python 不会把它搬到新位置。运行组件和缓存保存到下面选定的目录。"
                        : "将把共享 Python、依赖、转换引擎、缓存和临时文件部署到所选位置。选择完成后会自动记忆，后续启动直接复用。";
                    proceed.Enabled = true;
                }); } catch (InvalidOperationException) { }
            });
        }

        private void Confirm()
        {
            try
            {
                if (String.IsNullOrWhiteSpace(location.Text) || !Path.IsPathRooted(location.Text))
                    throw new ArgumentException("请选择明确的绝对目录，例如 D:\\青简运行环境。");
                string chosenHome = Path.GetFullPath(location.Text.Trim());
                if (File.Exists(chosenHome)) throw new ArgumentException("运行位置指向文件，请选择文件夹。");
                if (reuse.Checked)
                {
                    PythonInfo found = new Runner(options, sourceDir, null).ValidatePython(python.Text, cancellation.Token);
                    if (found == null) throw new ArgumentException("请选择可运行的 Python 3.10–3.14 64 位解释器。也可选择在指定位置部署共享 Python。");
                    options.Python = found.python;
                }
                else options.Python = null;
                options.RuntimeHome = chosenHome; options.ManagedOnly = managed.Checked;
                // Persist before any download. If the source folder is read-only,
                // explain the problem instead of silently using another drive.
                StartupSettings.Save(sourceDir, options);
                options.ConfigurationRequired = false;
                DialogResult = DialogResult.OK; Close();
            }
            catch (Exception ex) { MessageBox.Show(this, ex.Message, "尚未开始部署", MessageBoxButtons.OK, MessageBoxIcon.Warning); }
        }
    }

    internal sealed class LauncherForm : Form
    {
        private readonly Options options;
        private readonly string sourceDir;
        private readonly Label status = new Label();
        private readonly ProgressBar progress = new ProgressBar();
        private readonly TextBox logs = new TextBox();
        private readonly Button retry = new Button();
        private readonly Button close = new Button();
        private CancellationTokenSource cancellation;
        private bool running;
        private bool closeRequested;
        private int resultCode = 2;
        internal int ResultCode { get { return resultCode; } }

        internal LauncherForm(Options options, string sourceDir)
        {
            this.options = options; this.sourceDir = sourceDir;
            Text = "青简工具箱 · 启动准备";
            Font = new Font("Microsoft YaHei UI", 9F);
            ClientSize = new Size(690, 475); MinimumSize = new Size(570, 420);
            StartPosition = FormStartPosition.CenterScreen;
            BackColor = Color.FromArgb(247, 248, 252);
            var body = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(26, 24, 26, 18), ColumnCount = 1, RowCount = 6 };
            body.RowStyles.Add(new RowStyle(SizeType.Absolute, 38));
            body.RowStyles.Add(new RowStyle(SizeType.Absolute, 48));
            body.RowStyles.Add(new RowStyle(SizeType.Absolute, 36));
            body.RowStyles.Add(new RowStyle(SizeType.Absolute, 24));
            body.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            body.RowStyles.Add(new RowStyle(SizeType.Absolute, 54));
            var heading = new Label { Text = "先检查环境，再开始办公", Font = new Font(Font.FontFamily, 17F, FontStyle.Bold), Dock = DockStyle.Fill,
                ForeColor = Color.FromArgb(37, 43, 62), AutoEllipsis = true };
            var description = new Label { Text = "直接复用选中的本地 Python，并补齐其中缺少的组件；其他程序使用同一环境时也可共享。下载失败会切换国内备用源，无 Python 时自动准备。", Dock = DockStyle.Fill,
                ForeColor = Color.FromArgb(95, 104, 126) };
            status.Text = "准备检查…"; status.Dock = DockStyle.Fill; status.AutoEllipsis = true;
            progress.Dock = DockStyle.Fill; progress.Margin = new Padding(0, 0, 0, 8); progress.Style = ProgressBarStyle.Marquee;
            logs.Multiline = true; logs.ReadOnly = true; logs.ScrollBars = ScrollBars.Vertical; logs.Dock = DockStyle.Fill;
            logs.BackColor = Color.White; logs.BorderStyle = BorderStyle.FixedSingle; logs.Font = new Font("Microsoft YaHei UI", 8.5F);
            var buttons = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.RightToLeft, Padding = new Padding(0, 12, 0, 0), WrapContents = false };
            close.Text = "取消并关闭"; close.Size = new Size(110, 32);
            retry.Text = "重新检查"; retry.Size = new Size(105, 32); retry.Enabled = false;
            close.Click += delegate { Close(); }; retry.Click += delegate { StartPreparation(); };
            buttons.Controls.Add(close); buttons.Controls.Add(retry);
            body.Controls.Add(heading, 0, 0); body.Controls.Add(description, 0, 1); body.Controls.Add(status, 0, 2);
            body.Controls.Add(progress, 0, 3); body.Controls.Add(logs, 0, 4); body.Controls.Add(buttons, 0, 5);
            Controls.Add(body);
            Shown += delegate { StartPreparation(); };
            FormClosing += delegate(object sender, FormClosingEventArgs e)
            {
                if (!running) return;
                e.Cancel = true; closeRequested = true; close.Enabled = false;
                status.Text = "正在停止本次准备，请稍候…";
                cancellation.Cancel();
            };
        }

        private void Receive(Dictionary<string, object> e)
        {
            if (IsDisposed || !IsHandleCreated) return;
            try
            {
                BeginInvoke((Action)delegate
                {
                    if (IsDisposed) return;
                    string message = Json.Text(e, "message");
                    if (!String.IsNullOrWhiteSpace(message))
                    {
                        if (Json.Text(e, "detail") != "pip") status.Text = message.Replace("\r", " ").Replace("\n", " ");
                        // Keep the window responsive during verbose pip output.
                        if (logs.TextLength > 200000) logs.Text = logs.Text.Substring(logs.TextLength - 100000);
                        logs.AppendText(message + Environment.NewLine);
                    }
                    object value;
                    double percent;
                    if (e.TryGetValue("percent", out value) && Double.TryParse(Convert.ToString(value, CultureInfo.InvariantCulture), NumberStyles.Float, CultureInfo.InvariantCulture, out percent))
                    { progress.Style = ProgressBarStyle.Continuous; progress.Value = Math.Max(0, Math.Min(100, (int)percent)); }
                    else if ((Json.Text(e, "event") == "log" || Json.Text(e, "event") == "progress") && Json.Text(e, "detail") != "pip")
                        progress.Style = ProgressBarStyle.Marquee;
                });
            }
            catch (InvalidOperationException) { }
        }

        private void StartPreparation()
        {
            if (running) return;
            closeRequested = false; running = true; retry.Enabled = false; close.Enabled = true;
            close.Text = "取消并关闭"; progress.Style = ProgressBarStyle.Marquee;
            cancellation = new CancellationTokenSource();
            ThreadPool.QueueUserWorkItem(delegate
            {
                int code;
                string failure = null;
                try { code = new Runner(options, sourceDir, Receive).Run(cancellation.Token); }
                catch (OperationCanceledException) { code = 130; }
                catch (Exception ex) { code = 2; failure = ex.Message; }
                if (IsDisposed || !IsHandleCreated) return;
                try
                {
                    BeginInvoke((Action)delegate
                    {
                        running = false; resultCode = code; cancellation.Dispose();
                        if (closeRequested || code == 0) { Close(); return; }
                        progress.Style = ProgressBarStyle.Continuous; progress.Value = 0;
                        status.Text = "准备未完成，可根据日志处理后重试。";
                        logs.AppendText(Environment.NewLine + failure + Environment.NewLine);
                        retry.Enabled = true; close.Enabled = true; close.Text = "关闭";
                    });
                }
                catch (InvalidOperationException) { }
            });
        }
    }

    internal static class Program
    {
        private static int StartPortable(string executable, Options options)
        {
            // The full package already carries Python and its engines. Never
            // load the source manifest, discover Python, or prepare anything.
            if (options.CheckOnly || options.PrepareOnly)
            {
                Console.WriteLine(Json.Encode(new { @event = "check", available = true, portable = true, executable = executable, bootstrap = "skipped" }));
                return 0;
            }
            var info = new ProcessStartInfo(executable, String.Join(" ", options.AppArgs.Select(Runner.Quote)))
            { UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = Path.GetDirectoryName(executable) };
            foreach (string key in new[] { "PYTHONHOME", "PYTHONPATH", "QINGJIAN_BOOTSTRAP_READY", "QINGJIAN_LIBREOFFICE" })
                info.EnvironmentVariables.Remove(key);
            using (var process = Process.Start(info))
            {
                if (process == null) throw new IOException("完整包主程序无法启动，请确认已完整解压。");
                // Headless test/automation callers can observe the real result;
                // ordinary GUI launch detaches so modules survive this launcher.
                if (options.NoUi) { process.WaitForExit(); return process.ExitCode; }
            }
            return 0;
        }

        [STAThread]
        private static int Main(string[] args)
        {
            // A windowed exe may have redirected pipes without an attached
            // console. Setting OutputEncoding alone can fail on that handle.
            try { Console.SetOut(new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false)) { AutoFlush = true }); }
            catch (IOException) { }
            Options options;
            try { options = Options.Parse(args); }
            catch (Exception ex)
            {
                if (args.Contains("--no-ui") || args.Contains("--check-only")) Console.WriteLine(Json.Encode(new { @event = "error", message = ex.Message }));
                else MessageBox.Show(ex.Message, "青简工具箱启动参数", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 2;
            }
            string root = Path.GetFullPath(AppDomain.CurrentDomain.BaseDirectory);
            string sourceDir = StartupSettings.SourceDirectory(root);
            try
            {
                string portable = StartupSettings.PortableExecutable(root);
                if (portable != null) return StartPortable(portable, options);
                StartupSettings.Resolve(options, sourceDir);
            }
            catch (Exception ex)
            {
                if (options.NoUi) Console.WriteLine(Json.Encode(new { @event = "error", message = ex.Message }));
                else MessageBox.Show(ex.Message, "青简工具箱启动准备", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 2;
            }
            if (options.NoUi)
            {
                using (var cancellation = new CancellationTokenSource())
                {
                    Console.CancelKeyPress += delegate(object sender, ConsoleCancelEventArgs e) { e.Cancel = true; cancellation.Cancel(); };
                    try { return new Runner(options, sourceDir, null).Run(cancellation.Token); }
                    catch (OperationCanceledException) { Console.WriteLine(Json.Encode(new { @event = "error", message = "已取消本次环境准备。" })); return 130; }
                    catch (Exception ex) { Console.WriteLine(Json.Encode(new { @event = "error", message = ex.Message })); return 2; }
                }
            }
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            if (options.ChooseEnvironment || options.ConfigurationRequired)
            {
                using (var selection = new EnvironmentChoiceForm(options, sourceDir))
                    if (selection.ShowDialog() != DialogResult.OK) return 130;
            }
            using (var form = new LauncherForm(options, sourceDir)) { Application.Run(form); return form.ResultCode; }
        }
    }
}

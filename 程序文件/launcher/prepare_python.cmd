@echo off
setlocal DisableDelayedExpansion
if not defined QJ_CMD_PROJECT for %%I in ("%~dp0..") do set "QJ_CMD_PROJECT=%%~fI"
set "QJ_PREPARE_FILE=%~f0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -Command "$s=[IO.File]::ReadAllText($env:QJ_PREPARE_FILE,[Text.Encoding]::UTF8); $m='#<QJ-POWERSHELL>'; $i=$s.LastIndexOf($m); if($i -lt 0){exit 2}; & ([ScriptBlock]::Create($s.Substring($i)))"
exit /b %errorlevel%
#<QJ-POWERSHELL>
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)

function Get-AbsolutePath([string]$Value, [string]$Label) {
    if ([string]::IsNullOrWhiteSpace($Value) -or -not [IO.Path]::IsPathRooted($Value)) { throw "$Label 必须为绝对路径。" }
    return [IO.Path]::GetFullPath($Value)
}

function Test-Artifact([string]$Path, $Spec) {
    if (-not [IO.File]::Exists($Path)) { return $false }
    $info = New-Object IO.FileInfo($Path)
    if ($info.Length -ne [long]$Spec.bytes -or ($info.Attributes -band [IO.FileAttributes]::ReparsePoint)) { return $false }
    $stream = [IO.File]::OpenRead($Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() -eq $Spec.sha256) }
    finally { $stream.Dispose(); $sha.Dispose() }
}

function Read-RuntimeSpec([string]$Project) {
    $manifest = [IO.Path]::Combine($Project, 'runtime-sources.json')
    if ((New-Object IO.FileInfo($manifest)).Length -gt 65536) { throw '下载清单过大。' }
    $value = [IO.File]::ReadAllText($manifest, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $spec = $value.python
    if ($value.schema_version -ne 1 -or $spec.sha256 -notmatch '^[a-f0-9]{64}$' -or [long]$spec.bytes -le 0 -or [long]$spec.bytes -gt 268435456 -or
        $spec.filename -notmatch '^python-[0-9.]+-amd64\.zip$' -or $spec.version -notmatch '^3\.(10|11|12|13|14)\.[0-9]+$' -or @($spec.urls).Count -lt 1) { throw 'Python 下载清单字段无效。' }
    foreach ($source in $spec.urls) {
        $uri = New-Object Uri($source.url)
        if ($uri.Scheme -ne 'https' -or -not $uri.Host -or $uri.UserInfo) { throw '下载源必须使用不含凭据的 HTTPS。' }
    }
    return $spec
}

function Probe-Python([string]$Path) {
    if (-not [IO.File]::Exists($Path) -or [IO.Path]::GetExtension($Path) -ne '.exe') { return $null }
    if ((New-Object IO.FileInfo($Path)).Attributes -band [IO.FileAttributes]::ReparsePoint) { return $null }
    # Never evaluate a candidate as a command string; the executable and fixed arguments are separate.
    $code = 'import sys,struct,ssl,json; print(json.dumps(dict(python=sys.executable,version=list(sys.version_info[:3]),bits=struct.calcsize(chr(80))*8)))'
    try {
        $lines = @(& $Path -E -X utf8 -c $code 2>$null)
        if ($LASTEXITCODE -ne 0 -or $lines.Count -eq 0) { return $null }
        $value = $lines[-1] | ConvertFrom-Json
        if ($value.bits -ne 64 -or $value.version[0] -ne 3 -or $value.version[1] -lt 10 -or $value.version[1] -gt 14) { return $null }
        $resolved = Get-AbsolutePath $value.python 'Python 路径'
        if (-not [IO.File]::Exists($resolved)) { return $null }
        return @{ python = $resolved; version = ($value.version -join '.') }
    } catch { return $null }
}

function Find-Python {
    $paths = New-Object Collections.Generic.List[string]
    $explicit = $env:QJ_CMD_PYTHON
    if (-not $explicit) { $explicit = $env:QINGJIAN_CMD_PYTHON }
    if ($explicit) {
        $selected = Probe-Python (Get-AbsolutePath $explicit 'Python 路径')
        if (-not $selected) { throw '指定的 Python 无法运行或不兼容；未切换其他解释器、未下载。' }
        return $selected
    }
    if ($env:QJ_CMD_NO_DISCOVERY -eq '1') { return $null }
    if ($env:VIRTUAL_ENV) { $paths.Add([IO.Path]::Combine($env:VIRTUAL_ENV, 'Scripts\python.exe')) }
    foreach ($name in @('python.exe', 'python3.exe')) {
        foreach ($item in @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)) { $paths.Add($item.Source) }
    }
    $py = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($py) {
        foreach ($minor in @(14,13,12,11,10)) {
            try {
                $candidate = @(& $py.Source "-3.$minor-64" -E -X utf8 -c 'import sys; print(sys.executable)' 2>$null)
                if ($LASTEXITCODE -eq 0 -and $candidate.Count) { $paths.Add($candidate[-1]) }
            } catch { }
        }
    }
    foreach ($path in @($paths | Select-Object -Unique)) { $found = Probe-Python $path; if ($found) { return $found } }
    return $null
}

function Get-RemoteArtifact($Source, [string]$Target, [long]$ExpectedBytes) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $current = New-Object Uri($Source.url)
    $response = $null
    for ($redirect = 0; $redirect -le 5; $redirect++) {
        if ($current.Scheme -ne 'https' -or $current.UserInfo) { throw '下载跳转到非 HTTPS 或含凭据的地址。' }
        $request = [Net.HttpWebRequest]::Create($current)
        $request.AllowAutoRedirect = $false
        $request.Timeout = 15000
        $request.ReadWriteTimeout = 30000
        $request.UserAgent = 'QingjianCmdSource/0.10.2'
        $response = $request.GetResponse()
        $status = [int]$response.StatusCode
        if ($status -lt 300 -or $status -ge 400) { break }
        $location = $response.Headers['Location']
        $response.Dispose(); $response = $null
        if (-not $location -or $redirect -eq 5) { throw '下载重定向过多或地址缺失。' }
        $current = New-Object Uri($current, $location)
    }
    if (-not $response) { throw '无法取得下载响应。' }
    $inputStream = $null; $outputStream = $null
    try {
        if ($response.ContentLength -ge 0 -and $response.ContentLength -ne $ExpectedBytes) { throw '下载大小与固定清单不符。' }
        $inputStream = $response.GetResponseStream()
        $outputStream = New-Object IO.FileStream($Target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $buffer = New-Object byte[] 131072
        $total = 0L
        $timer = [Diagnostics.Stopwatch]::StartNew()
        while (($count = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $total += $count
            if ($total -gt $ExpectedBytes -or $timer.Elapsed.TotalMinutes -gt 15) { throw '下载超过固定大小或超时。' }
            $outputStream.Write($buffer, 0, $count)
        }
        if ($total -ne $ExpectedBytes) { throw '下载未完成。' }
    } finally {
        if ($outputStream) { $outputStream.Dispose() }
        if ($inputStream) { $inputStream.Dispose() }
        $response.Dispose()
    }
}

function Get-PythonArchive($Spec, [string]$RuntimeRoot, [bool]$Offline) {
    $downloads = [IO.Path]::Combine($RuntimeRoot, 'downloads')
    $archive = [IO.Path]::Combine($downloads, $Spec.sha256.Substring(0,16) + '-' + $Spec.filename)
    if (Test-Artifact $archive $Spec) { Write-Host '复用已校验的 Python 缓存。'; return $archive }
    if ($Offline) { throw '离线模式未下载 Python。请联网准备一次或使用完整运行包。' }
    [void][IO.Directory]::CreateDirectory($downloads)
    $errors = New-Object Collections.Generic.List[string]
    foreach ($source in $Spec.urls) {
        $partial = [IO.Path]::Combine($downloads, '.python-' + [Guid]::NewGuid().ToString('N') + '.partial')
        try {
            Write-Host ('正在下载 Python：' + $source.name)
            Get-RemoteArtifact $source $partial ([long]$Spec.bytes)
            if (-not (Test-Artifact $partial $Spec)) { throw 'SHA256 或文件大小校验失败，未解压或执行。' }
            if ([IO.File]::Exists($archive)) { $archive = [IO.Path]::Combine($downloads, $Spec.sha256.Substring(0,16) + '-' + [Guid]::NewGuid().ToString('N') + '-' + $Spec.filename) }
            [IO.File]::Move($partial, $archive)
            return $archive
        } catch { $errors.Add($source.name + '：' + $_.Exception.Message); Write-Host $errors[$errors.Count-1] }
        finally { if ([IO.File]::Exists($partial)) { [IO.File]::Delete($partial) } }
    }
    throw ('所有 Python 下载源均未完成，请检查连接后重试。' + [Environment]::NewLine + ($errors -join [Environment]::NewLine))
}

function Expand-VerifiedZip([string]$Archive, [string]$Target) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $root = [IO.Path]::GetFullPath($Target).TrimEnd('\') + '\'
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        if ($zip.Entries.Count -gt 20000) { throw 'Python 压缩包条目过多。' }
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName.Replace('/', '\')
            if ($name.StartsWith('\') -or $name.Contains(':') -or @($name.Split('\') | Where-Object { $_ -eq '..' }).Count -or
                (($entry.ExternalAttributes -shr 16) -band 0xF000) -eq 0xA000) { throw 'Python 压缩包包含不安全路径。' }
            $path = [IO.Path]::GetFullPath([IO.Path]::Combine($Target, $name))
            if (-not $path.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { throw 'Python 压缩包路径越界。' }
            if ($name.EndsWith('\')) { [void][IO.Directory]::CreateDirectory($path); continue }
            [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($path))
            $inputStream = $entry.Open()
            $outputStream = $null
            try {
                $outputStream = New-Object IO.FileStream($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
                $inputStream.CopyTo($outputStream)
            } finally { if ($outputStream) { $outputStream.Dispose() }; $inputStream.Dispose() }
        }
    } finally { $zip.Dispose() }
}

function Save-Json([string]$Path, $Value) {
    [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Path))
    $staged = [IO.Path]::Combine([IO.Path]::GetDirectoryName($Path), '.cmd-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($staged, ($Value | ConvertTo-Json -Depth 10), (New-Object Text.UTF8Encoding($false)))
        if ([IO.File]::Exists($Path)) { [IO.File]::Replace($staged, $Path, $null) } else { [IO.File]::Move($staged, $Path) }
    } finally { if ([IO.File]::Exists($staged)) { [IO.File]::Delete($staged) } }
}

function Find-ManagedPython($Spec, [string]$RuntimeRoot) {
    $target = [IO.Path]::Combine($RuntimeRoot, 'python-' + $spec.version)
    $found = Probe-Python ([IO.Path]::Combine($target, 'python.exe'))
    if ($found -and $found.version -eq $spec.version) { return $found }
    $selectionFile = [IO.Path]::Combine($RuntimeRoot, 'python-selection.json')
    if ([IO.File]::Exists($selectionFile) -and (New-Object IO.FileInfo($selectionFile)).Length -lt 8192) {
        try {
            $cached = [IO.File]::ReadAllText($selectionFile, [Text.Encoding]::UTF8) | ConvertFrom-Json
            $cachedPath = Get-AbsolutePath $cached.python '已部署 Python'
            if ($cachedPath.StartsWith($RuntimeRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
                $found = Probe-Python $cachedPath
                if ($found -and $found.version -eq $spec.version) { return $found }
            }
        } catch { }
    }
    return $null
}

function Install-Python([string]$Project, [string]$RuntimeRoot, [bool]$Offline) {
    $spec = Read-RuntimeSpec $Project
    $found = Find-ManagedPython $spec $RuntimeRoot
    if ($found) { return $found }
    $target = [IO.Path]::Combine($RuntimeRoot, 'python-' + $spec.version)
    $selectionFile = [IO.Path]::Combine($RuntimeRoot, 'python-selection.json')
    $archive = Get-PythonArchive $spec $RuntimeRoot $Offline
    $stage = [IO.Path]::Combine($RuntimeRoot, '.cmd-python-' + [Guid]::NewGuid().ToString('N').Substring(0,12))
    [void][IO.Directory]::CreateDirectory($stage)
    $moved = $false
    try {
        Expand-VerifiedZip $archive $stage
        $found = Probe-Python ([IO.Path]::Combine($stage, 'python.exe'))
        if (-not $found -or $found.version -ne $spec.version) { throw 'Python 解压后的运行自检失败。' }
        if ([IO.Directory]::Exists($target)) { $target += '-' + [Guid]::NewGuid().ToString('N').Substring(0,8) }
        [IO.Directory]::Move($stage, $target); $moved = $true
        $found = Probe-Python ([IO.Path]::Combine($target, 'python.exe'))
        if (-not $found) { throw 'Python 部署后的运行自检失败。' }
        Save-Json $selectionFile $found
        return $found
    } finally {
        if (-not $moved -and [IO.Directory]::Exists($stage) -and [IO.Path]::GetDirectoryName($stage) -eq $RuntimeRoot -and [IO.Path]::GetFileName($stage).StartsWith('.cmd-python-')) { [IO.Directory]::Delete($stage, $true) }
    }
}

function Get-SharedDefault([string]$Project) {
    return [IO.Path]::Combine([IO.Path]::GetDirectoryName((Get-PackageRoot $Project)), '青简共享环境')
}

function Get-PackageRoot([string]$Project) {
    $packageRoot = $Project
    $pattern = '^青简工具箱(?:源码|_v\d+\.\d+\.\d+(?:[_ ].*)?)?$'
    if ([IO.Path]::GetFileName($packageRoot) -eq '程序文件') { $packageRoot = [IO.Path]::GetDirectoryName($packageRoot) }
    $parent = [IO.Path]::GetDirectoryName($packageRoot)
    while ($parent -and [IO.Path]::GetFileName($parent) -match $pattern) {
        $packageRoot = $parent
        $parent = [IO.Path]::GetDirectoryName($packageRoot)
    }
    return $packageRoot
}

function Read-SmallJson([string]$Path, [long]$Maximum=65536) {
    try {
        if (-not [IO.File]::Exists($Path)) { return $null }
        $info = New-Object IO.FileInfo($Path)
        if ($info.Length -gt $Maximum -or ($info.Attributes -band [IO.FileAttributes]::ReparsePoint)) { return $null }
        $parentInfo = New-Object IO.DirectoryInfo($info.DirectoryName)
        if ($parentInfo.Attributes -band [IO.FileAttributes]::ReparsePoint) { return $null }
        return [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8) | ConvertFrom-Json
    } catch { return $null }
}

function Read-SharedChoice([string]$Project, [string]$PythonOverride='') {
    $index = $env:QINGJIAN_SHARED_RUNTIME_INDEX
    if (-not $index -and $env:QINGJIAN_USER_DATA) { $index = [IO.Path]::Combine($env:QINGJIAN_USER_DATA, 'shared-runtime.json') }
    if (-not $index -and $env:LOCALAPPDATA) { $index = [IO.Path]::Combine($env:LOCALAPPDATA, 'QingjianLocal\shared-runtime.json') }
    $entries = New-Object Collections.Generic.List[object]
    if ($index -and [IO.Path]::IsPathRooted($index)) {
        $catalog = Read-SmallJson $index
        if ($catalog -and $catalog.schema_version -eq 1 -and @($catalog.entries).Count -le 16) {
            foreach ($entry in @($catalog.entries | Sort-Object updated_ns -Descending)) { if ($entry) { $entries.Add($entry) } }
        }
    }
    if ($env:QJ_CMD_NO_DISCOVERY -ne '1') {
        $pattern = '^青简工具箱(?:源码|_v\d+\.\d+\.\d+(?:[_ ].*)?)?$'
        $parent = [IO.Path]::GetDirectoryName($Project)
        for ($level=0; $level -lt 3 -and $parent -and $parent -ne [IO.Path]::GetPathRoot($parent); $level++) {
            try {
                $neighbors = @([IO.Directory]::EnumerateDirectories($parent) | Select-Object -First 256)
                foreach ($neighbor in $neighbors) {
                    if ([IO.Path]::GetFileName($neighbor) -notmatch $pattern) { continue }
                    foreach ($relative in @('程序文件', '青简工具箱源码\程序文件', '青简工具箱源码', '')) {
                        $folder = if ($relative) { [IO.Path]::Combine($neighbor, $relative) } else { $neighbor }
                        $old = Read-SmallJson ([IO.Path]::Combine($folder, 'config\startup-selection.json')) 16384
                        if ($old -and $old.schema_version -eq 1) {
                            $entries.Add(@{project=$folder; mode=$old.mode; python=$old.python; runtime_home=$old.runtime_home})
                        }
                    }
                }
            } catch { }
            $parent = [IO.Path]::GetDirectoryName($parent)
        }
    }
    foreach ($entry in $entries) {
        try {
            if ($entry.mode -notin @('reuse','managed')) { continue }
            $sharedRoot = Get-AbsolutePath $entry.runtime_home '共享运行目录'
            if (-not [IO.Directory]::Exists($sharedRoot) -or ((New-Object IO.DirectoryInfo($sharedRoot)).Attributes -band [IO.FileAttributes]::ReparsePoint)) { continue }
            if ($entry.project) {
                $oldProject = (Get-PackageRoot (Get-AbsolutePath $entry.project '旧源码目录')).TrimEnd('\')
                if ($sharedRoot -eq $oldProject -or $sharedRoot.StartsWith($oldProject + '\', [StringComparison]::OrdinalIgnoreCase)) { continue }
            }
            if ($PythonOverride) { $found = Probe-Python (Get-AbsolutePath $PythonOverride '指定Python') }
            elseif ($entry.mode -eq 'managed') { $found = Find-ManagedPython (Read-RuntimeSpec $Project) $sharedRoot }
            else { $found = Probe-Python (Get-AbsolutePath $entry.python '已选Python') }
            $mode = if ($PythonOverride) { 'reuse' } else { $entry.mode }
            if ($found) { return @{schema_version=1; mode=$mode; python=$found.python; runtime_home=$sharedRoot} }
        } catch { }
    }
    return $null
}

#<QJ-MAIN>
try {
    $project = Get-AbsolutePath $env:QJ_CMD_PROJECT '程序文件位置'
    $choiceFile = [IO.Path]::Combine($project, 'config\startup-selection.json')
    $choice = $null
    if ($env:QJ_CMD_CHOOSE -ne '1' -and [IO.File]::Exists($choiceFile)) {
        if ((New-Object IO.FileInfo($choiceFile)).Length -gt 16384) { throw '启动位置配置过大。' }
        $choice = [IO.File]::ReadAllText($choiceFile, [Text.Encoding]::UTF8) | ConvertFrom-Json
        if ($choice.schema_version -ne 1 -or $choice.mode -notin @('reuse','managed')) { throw '启动位置配置无效，请用 --choose-environment 重新选择。' }
        $choice.runtime_home = Get-AbsolutePath $choice.runtime_home '部署位置'
    }
    $homeOverride = $env:QJ_CMD_HOME
    if (-not $homeOverride) { $homeOverride = $env:QINGJIAN_RUNTIME_HOME }
    $pythonOverride = $env:QJ_CMD_PYTHON
    if (-not $pythonOverride) { $pythonOverride = $env:QINGJIAN_CMD_PYTHON }
    if (-not $choice -and -not $homeOverride -and $env:QJ_CMD_CHOOSE -ne '1') {
        $choice = Read-SharedChoice $project $pythonOverride
        if ($choice) {
            Write-Host ('复用上次选择的包外共享运行目录：' + $choice.runtime_home)
            if ($env:QJ_CMD_CHECK_ONLY -ne '1') { Save-Json $choiceFile $choice }
        }
    }
    $found = $null
    if ($homeOverride) {
        $found = Find-Python
        $choice = @{schema_version=1; mode='managed'; python=''; runtime_home=(Get-AbsolutePath $homeOverride '部署位置')}
        if ($found) { $choice.mode='reuse'; $choice.python=$found.python }
    } elseif ($choice -and ($choice.mode -eq 'reuse' -or $pythonOverride)) {
        $python = $choice.python
        if ($pythonOverride) { $python = $pythonOverride }
        $found = Probe-Python (Get-AbsolutePath $python 'Python 路径')
        if (-not $found) { throw '已选择的 Python 无法运行；未切换其他解释器，请用 --choose-environment 重新选择。' }
        $choice.python = $found.python
        $choice.mode = 'reuse'
    }
    if (-not $choice) {
        if ($env:QJ_CMD_NO_UI -eq '1') { throw '首次启动须选择部署位置；命令行可指定 --python 和 --runtime-home。' }
        $found = Find-Python
        Write-Host '青简工具箱：先选择运行环境，确认后才开始下载或创建部署目录。'
        $mode = 'managed'
        if ($found) {
            Write-Host ('发现可复用 Python：' + $found.python)
            $answer = Read-Host '输入 1 复用现有 Python；2 在自选位置部署 Python；直接回车取消'
            if (-not $answer) { exit 130 }
            if ($answer -eq '1') { $mode='reuse' } elseif ($answer -eq '2') { $found=$null } else { throw '请选择 1 或 2。' }
        } else { Write-Host '未发现可用的64位 Python，将按选定位置下载并解压；直接回车可取消。' }
        if ($mode -eq 'reuse') {
            Write-Host '将复用上述 Python；缺少的 Python 库补入该解释器的标准位置，不会再部署一套 Python。'
            Write-Host '另选的目录仅用于转换引擎、下载缓存、临时文件和日志。'
            $defaultRoot = Get-SharedDefault $project
            Write-Host ('工具组件与缓存默认目录：' + $defaultRoot)
            Write-Host '默认目录在源码包外；更新时删除旧源码包不会删除已准备的组件。'
            $answer = (Read-Host '直接回车使用默认目录；也可输入其他绝对路径；输入 Q 取消').Trim().Trim('"')
            if ($answer -eq 'q') { exit 130 }
            if (-not $answer) { $answer = $defaultRoot }
            $runtimeRoot = Get-AbsolutePath $answer '工具组件与缓存目录'
            Write-Host ('工具组件与缓存目录：' + $runtimeRoot)
        } else {
            Write-Host '所选目录用于部署 Python、转换引擎，以及存放下载缓存、临时文件和日志。'
            $answer = Read-Host '输入部署位置的绝对路径（例如 D:\青简环境；直接回车取消）'
            if (-not $answer) { exit 130 }
            $runtimeRoot = Get-AbsolutePath $answer.Trim().Trim('"') '部署位置'
            Write-Host ('部署位置：' + $runtimeRoot)
        }
        if ((Read-Host '输入 Y 确认，其他输入取消').Trim().ToLowerInvariant() -ne 'y') { exit 130 }
        $choice = @{schema_version=1; mode=$mode; python=''; runtime_home=$runtimeRoot}
        if ($found) { $choice.python=$found.python }
        Save-Json $choiceFile $choice
    }
    if ($choice.mode -eq 'managed') {
        if ($env:QJ_CMD_CHECK_ONLY -eq '1') {
            $found = Find-ManagedPython (Read-RuntimeSpec $project) $choice.runtime_home
            if (-not $found) { throw '所选位置尚未部署 Python；仅检查模式未下载或创建运行环境。' }
        } else { $found = Install-Python $project $choice.runtime_home ($env:QJ_CMD_OFFLINE -eq '1') }
    }
    if (-not $found) { $found = Probe-Python $choice.python }
    if (-not $found) { throw '没有可运行的 Python，未执行程序。' }
    if ($env:QJ_CMD_RESULT) {
        $result = Get-AbsolutePath $env:QJ_CMD_RESULT '启动结果位置'
        $expected = [IO.Path]::Combine($project, 'config').TrimEnd('\') + '\'
        if (-not $result.StartsWith($expected, [StringComparison]::OrdinalIgnoreCase)) { throw '启动结果位置超出程序配置目录。' }
        [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($result))
        [IO.File]::WriteAllText($result, $found.python, (New-Object Text.UTF8Encoding($false)))
    } else { Write-Host ('Python 已就绪：' + $found.python) }
    exit 0
} catch {
    Write-Host ('CMD 环境准备未完成：' + $_.Exception.Message)
    exit 2
}

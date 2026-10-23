# ============================================================
# 打包 Python 后端为单文件 cam-backend.exe（Tauri sidecar）
#
# 产物: frontend\src-tauri\backend\cam-backend.exe
#      之后 cargo tauri build 时由 build.rs 用 include_bytes! 编进主程序
#
# 用法: powershell -ExecutionPolicy Bypass -File scripts\build_sidecar.ps1
#       powershell ... build_sidecar.ps1 -Clean     # 先清 build 目录再打
#
# 六步：环境检查 -> 依赖探测 -> PyInstaller -> 产物校验 -> 启动自检 -> 注入资源目录
# ============================================================
param(
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'

$root    = Resolve-Path (Join-Path $PSScriptRoot '..')
$backend = Join-Path $root 'backend'
$build   = Join-Path $backend 'build'
$dist    = Join-Path $build 'dist'
$work    = Join-Path $build 'work'
$spec    = Join-Path $build 'spec'
$venvPy  = Join-Path $backend '.venv\Scripts\python.exe'
$entry   = Join-Path $backend 'sidecar.py'
$probe   = Join-Path $PSScriptRoot 'probe_env.py'
$outDir  = Join-Path $root 'frontend\src-tauri\backend'
$outExe  = Join-Path $outDir 'cam-backend.exe'
$checkPort = 8976

function Step([string]$n, [string]$text) { Write-Host "`n[$n] $text" -ForegroundColor Cyan }
function Ok([string]$text)   { Write-Host "     OK   $text" -ForegroundColor Green }
function Warn([string]$text) { Write-Host "     WARN $text" -ForegroundColor Yellow }
function Fail([string]$text) { Write-Host "`n[失败] $text" -ForegroundColor Red; exit 1 }

# ------------------------------------------------------------ 原生命令包装
# PowerShell 5.1 在 ErrorActionPreference='Stop' 下，会把原生命令写到 stderr 的
# 普通日志（pip 的 already satisfied、PyInstaller 的 INFO）升级成终止错误，
# 脚本会在毫无问题的地方中断。所以统一降到 Continue，只认退出码。
function Invoke-Native {
    param([string]$Exe, [string[]]$ArgList = @())
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        # 必须 Out-Host：否则子进程输出会和返回值混在同一条管道里，
        # 调用方拿到的是数组，`$code -ne 0` 永远成立。
        & $Exe @ArgList | Out-Host
    } finally {
        $ErrorActionPreference = $prev
    }
    return $LASTEXITCODE
}

function Invoke-Quiet {
    param([string]$Exe, [string[]]$ArgList = @())
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $null = & $Exe @ArgList 2>&1
    } finally {
        $ErrorActionPreference = $prev
    }
    return $LASTEXITCODE
}

function Get-Native {
    param([string]$Exe, [string[]]$ArgList = @())
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $Exe @ArgList 2>&1
    } finally {
        $ErrorActionPreference = $prev
    }
    return (($out | Out-String).Trim())
}

# 端口探测不用 Test-NetConnection：它在某些环境要 10s+，用 TcpClient 更快
function Test-Port([int]$port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.Connect('127.0.0.1', $port)
        return $true
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

Write-Host "== BtCAM 后端 sidecar 打包 ==" -ForegroundColor Cyan

# ------------------------------------------------------------ 1/6 环境检查
Step '1/6' '检查 Python 环境'
if (-not (Test-Path $venvPy)) {
    Fail "找不到虚拟环境：$venvPy`n       先运行：powershell -ExecutionPolicy Bypass -File scripts\install_backend.ps1"
}
if (-not (Test-Path $entry)) { Fail "找不到入口 $entry" }
Ok "解释器 $venvPy"

$code = Invoke-Quiet $venvPy @('-m', 'PyInstaller', '--version')
if ($code -ne 0) {
    Warn 'PyInstaller 未安装，正在安装（首次几百 MB，耐心等）...'
    $code = Invoke-Native $venvPy @('-m', 'pip', 'install', '--upgrade', 'pyinstaller')
    if ($code -ne 0) {
        Fail "PyInstaller 安装失败（退出码 $code）。检查网络或换国内 pip 源后重试：`n       $venvPy -m pip install --upgrade pyinstaller"
    }
}
Ok ("PyInstaller " + (Get-Native $venvPy @('-m', 'PyInstaller', '--version')))

# ------------------------------------------------------------ 2/6 依赖探测
Step '2/6' '探测几何 / 排料依赖'
$raw = Get-Native $venvPy @($probe)
try {
    $envInfo = $raw | ConvertFrom-Json
} catch {
    Fail "依赖探测失败，python 原始输出：`n$raw"
}
Ok ("Python " + $envInfo.python)

$occt = $envInfo.occt
if ($occt) {
    Ok "OCCT 绑定 $occt（会一并打包，注意 onefile 体积会明显变大）"
} else {
    Warn '未检测到 OCC / OCP：本次产物只带 pyclipper 几何引擎，功能可用，圆弧精度略低'
}
if ($envInfo.deepnest) {
    Ok 'DeepNest 已安装，会一并打包'
} else {
    Warn '未检测到 deepnest：排料将回退内置 shelf 算法'
}
if (-not $envInfo.pyclipper) { Fail '缺少 pyclipper，先 pip install -r requirements.txt' }

# ------------------------------------------------------------ 3/6 PyInstaller
Step '3/6' 'PyInstaller 打包（单文件）'
if ($Clean -and (Test-Path $build)) {
    Write-Host '     清理 build 目录...'
    Remove-Item $build -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $dist, $work, $spec | Out-Null

$pyi = @(
    '--noconfirm', '--clean', '--onefile', '--console', '--name', 'cam-backend',
    '--distpath', $dist, '--workpath', $work, '--specpath', $spec,
    '--paths', $backend,
    '--hidden-import', 'app.main',
    '--collect-submodules', 'uvicorn',
    '--collect-submodules', 'websockets',
    '--collect-submodules', 'pyclipper'
)
if ($occt) { $pyi += @('--collect-submodules', $occt) }
if ($envInfo.deepnest) { $pyi += @('--collect-submodules', 'deepnest') }
$pyi += $entry

$sw = [System.Diagnostics.Stopwatch]::StartNew()
$code = Invoke-Native $venvPy (@('-m', 'PyInstaller') + $pyi)
$sw.Stop()
if ($code -ne 0) {
    Fail @"
PyInstaller 打包失败（退出码 $code）。常见原因：
       - 依赖缺失：$venvPy -m pip install -r backend\requirements.txt
       - 杀毒软件锁文件：把 backend\.venv 加入白名单后重试
       - 路径含空格/中文：本项目路径应无中文，确认后重试
"@
}
$exe = Join-Path $dist 'cam-backend.exe'
if (-not (Test-Path $exe)) { Fail "打包成功但找不到产物：$exe" }
Ok ("产物 {0:N2} MB，耗时 {1:N1}s" -f ((Get-Item $exe).Length / 1MB), $sw.Elapsed.TotalSeconds)

# ------------------------------------------------------------ 4/6 启动自检
Step '4/6' '启动自检（真实跑一遍 exe）'
if (Test-Port $checkPort) { Fail "自检端口 $checkPort 已被占用，先关掉占用进程再重试" }

$env:BTCAM_HOST = '127.0.0.1'
$env:BTCAM_PORT = "$checkPort"
$env:BTCAM_LOG_LEVEL = 'warning'
$proc = Start-Process -FilePath $exe -PassThru -WindowStyle Hidden
$health = $null
try {
    for ($i = 0; $i -lt 90; $i++) {
        Start-Sleep -Milliseconds 1000
        if ($proc.HasExited) { break }
        try {
            $health = Invoke-RestMethod "http://127.0.0.1:$checkPort/api/health" -TimeoutSec 2
            break
        } catch { }
    }
} finally {
    if (-not $proc.HasExited) {
        $null = Invoke-Quiet 'taskkill' @('/PID', "$($proc.Id)", '/T', '/F')
    }
    Remove-Item Env:\BTCAM_HOST, Env:\BTCAM_PORT, Env:\BTCAM_LOG_LEVEL -ErrorAction SilentlyContinue
}
if (-not $health) {
    Fail "自检失败：后端 90s 内没有响应 /api/health。`n       手动跑一次看报错：$exe"
}
Ok ("自检通过：/api/health = {0}，几何引擎 = {1}，排料引擎 = {2}" -f `
        $health.status, $health.geometryEngine, $health.nestingEngine)

# ------------------------------------------------------------ 5/6 注入侧车目录
Step '5/6' '注入 Tauri 资源目录'
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
Copy-Item $exe $outExe -Force
$hash = (Get-FileHash $outExe -Algorithm SHA256).Hash
Ok "已写入 $outExe"
Ok ("SHA256 " + $hash)

# ------------------------------------------------------------ 6/6 提示下一步
Step '6/6' '完成'
Write-Host '     后端已就绪，接下来打包主程序：' -ForegroundColor Green
Write-Host '       cd frontend; cargo tauri build' -ForegroundColor Green
Write-Host '     或一键（含图标/前端/汇总）：powershell -ExecutionPolicy Bypass -File scripts\build.ps1' -ForegroundColor Green

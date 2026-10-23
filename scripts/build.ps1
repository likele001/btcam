# ============================================================
# 一键出包：图标 -> 后端 sidecar -> 前端依赖 -> Tauri 构建 -> 汇总 dist\
#
# 用法: powershell -ExecutionPolicy Bypass -File scripts\build.ps1
#       powershell ... build.ps1 -SkipSidecar    # 后端 exe 没改，省 2 分钟
#       powershell ... build.ps1 -SkipBundle     # 只出便携 exe，不打安装包
#
# 产物（dist\）：
#   BtCAM-<ver>-portable.exe   单文件，后端已内嵌，拷贝即运行（车间交付只用这个）
#   BtCAM-<ver>-setup.exe      NSIS 安装包（次选）
#   BtCAM-<ver>.msi            MSI 安装包（次选）
# ============================================================
param(
    [switch]$SkipSidecar,
    [switch]$SkipBundle
)

$ErrorActionPreference = 'Stop'

$root     = Resolve-Path (Join-Path $PSScriptRoot '..')
$frontend = Join-Path $root 'frontend'
$tauriDir = Join-Path $frontend 'src-tauri'
$dist     = Join-Path $root 'dist'
$conf     = Join-Path $tauriDir 'tauri.conf.json'

function Step([string]$n, [string]$text) { Write-Host "`n[$n] $text" -ForegroundColor Cyan }
function Ok([string]$text)   { Write-Host "     OK   $text" -ForegroundColor Green }
function Warn([string]$text) { Write-Host "     WARN $text" -ForegroundColor Yellow }
function Fail([string]$text) { Write-Host "`n[失败] $text" -ForegroundColor Red; exit 1 }

# ------------------------------------------------------------ 原生命令包装
# 与 build_sidecar.ps1 相同：PS 5.1 会把原生命令的 stderr 日志升级成终止错误，
# 必须降级 ErrorActionPreference 并只认退出码；输出统一 Out-Host 防污染返回值。
function Invoke-Native {
    param([string]$Exe, [string[]]$ArgList = @())
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
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

Write-Host "== BtCAM 打包 ==" -ForegroundColor Cyan
Write-Host "项目根目录: $root"

# ------------------------------------------------------------ 1/6 环境检查
Step '1/6' '检查构建环境'
if (-not (Test-Path $conf)) { Fail "找不到 $conf，确认在 btcam 项目根目录下运行" }

$nodeVer = Get-Native 'node' @('--version')
if (-not $nodeVer) { Fail '未找到 node，先安装 Node.js 18+：https://nodejs.org/' }
Ok "Node $nodeVer"

if ((Invoke-Quiet 'cargo' @('--version')) -ne 0) {
    Fail @"
未找到 Rust 工具链，无法编译 Tauri 主程序。安装步骤：
       1) winget install Rustlang.Rustup
       2) rustup default stable-msvc
       3) 安装 Visual Studio Build Tools，勾选"使用 C++ 的桌面开发"
       （Rust MSVC 目标需要 link.exe，缺了会在链接阶段报错）
"@
}
Ok ("cargo " + (Get-Native 'cargo' @('--version')))

$cargoTauri = (Invoke-Quiet 'cargo' @('tauri', '--version')) -eq 0
if ($cargoTauri) {
    Ok 'cargo tauri 可用，使用 cargo tauri build'
} else {
    Warn '未安装 cargo-tauri，改用 npm run tauri（功能等价，走 frontend 里的 @tauri-apps/cli）'
}

# ------------------------------------------------------------ 2/6 图标
Step '2/6' '检查应用图标'
$icons = Join-Path $tauriDir 'icons'
if (-not (Test-Path (Join-Path $icons 'icon.ico'))) {
    Warn '缺少 icons\icon.ico，尝试用 logo 生成...'
    $gen = Join-Path $PSScriptRoot 'make_icons.py'
    if (Test-Path $gen) {
        $venvPy = Join-Path $root 'backend\.venv\Scripts\python.exe'
        if (-not (Test-Path $venvPy)) { Fail '缺少虚拟环境，先运行 scripts\install_backend.ps1（生成图标需要 Pillow）' }
        if ((Invoke-Native $venvPy @($gen)) -ne 0) { Fail '图标生成失败，把 1024x1024 的 PNG 放 assets\ 后重试' }
        Ok '图标已生成'
    } else {
        Fail '缺少 scripts\make_icons.py，无法生成图标'
    }
} else {
    Ok '图标齐全'
}

# ------------------------------------------------------------ 3/6 后端 sidecar
Step '3/6' '打包后端 sidecar'
if ($SkipSidecar) {
    Warn '已跳过（-SkipSidecar）；确认 frontend\src-tauri\backend\cam-backend.exe 是最新的'
} else {
    $code = Invoke-Native 'powershell' @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', (Join-Path $PSScriptRoot 'build_sidecar.ps1')
    )
    if ($code -ne 0) { Fail "后端打包失败（退出码 $code），先单独跑 scripts\build_sidecar.ps1 看详细日志" }
    Ok '后端 sidecar 就绪'
}

# ------------------------------------------------------------ 4/6 前端依赖
Step '4/6' '检查前端依赖'
Push-Location $frontend
try {
    if (-not (Test-Path (Join-Path $frontend 'node_modules'))) {
        Write-Host '     首次安装依赖（较慢）...'
        if ((Invoke-Native 'npm' @('install')) -ne 0) { Fail 'npm install 失败，检查网络或换 registry' }
    }
    Ok ("npm " + (Get-Native 'npm' @('--version')))
} finally {
    Pop-Location
}

# ------------------------------------------------------------ 5/6 Tauri 构建
Step '5/6' 'Tauri 构建（Release，首次编译较慢）'
Push-Location $frontend
try {
    if ($cargoTauri) {
        $buildArgs = @('tauri', 'build')
        if (-not $SkipBundle) { $buildArgs += @('--bundles', 'nsis,msi') }
        $code = Invoke-Native 'cargo' $buildArgs
    } else {
        $buildArgs = @('run', 'tauri', '--', 'build')
        if (-not $SkipBundle) { $buildArgs += @('--bundles', 'nsis,msi') }
        $code = Invoke-Native 'npm' $buildArgs
    }
} finally {
    Pop-Location
}
if ($code -ne 0) {
    Fail @"
Tauri 构建失败（退出码 $code）。排查顺序：
       - 链接错误 LNK1104：主程序/后端 exe 正被占用，先关掉 btcam.exe 与 cam-backend.exe
       - 缺 WebView2：tauri.conf.json 已配 embedBootstrapper，联网重试即可
       - 缺 MSVC link.exe：装 VS Build Tools 的"C++ 桌面开发"
"@
}

# ------------------------------------------------------------ 6/6 汇总产物
Step '6/6' '汇总产物到 dist'
$ver = '0.0.0'
try { $ver = (Get-Content $conf -Raw | ConvertFrom-Json).version } catch { }
New-Item -ItemType Directory -Force -Path $dist | Out-Null

$release = Join-Path $tauriDir 'target\release\btcam.exe'
if (-not (Test-Path $release)) { Fail "找不到 $release，构建没有产物" }
$portable = Join-Path $dist "BtCAM-$ver-portable.exe"
Copy-Item $release $portable -Force
Ok ("便携版 {0:N2} MB  $portable" -f ((Get-Item $portable).Length / 1MB))

if (-not $SkipBundle) {
    $nsis = Get-ChildItem (Join-Path $tauriDir 'target\release\bundle\nsis\*.exe') -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($nsis) {
        $target = Join-Path $dist "BtCAM-$ver-setup.exe"
        Copy-Item $nsis.FullName $target -Force
        Ok ("NSIS    {0:N2} MB  $target" -f ($nsis.Length / 1MB))
    } else {
        Warn '未找到 NSIS 安装包（可能缺少 NSIS 依赖，Tauri 会自动下载，检查网络）'
    }

    $msi = Get-ChildItem (Join-Path $tauriDir 'target\release\bundle\msi\*.msi') -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($msi) {
        $target = Join-Path $dist "BtCAM-$ver.msi"
        Copy-Item $msi.FullName $target -Force
        Ok ("MSI     {0:N2} MB  $target" -f ($msi.Length / 1MB))
    } else {
        Warn '未找到 MSI（企业部署才需要，可忽略）'
    }
}

Write-Host "`n完成。车间交付只需拷贝 $portable" -ForegroundColor Green

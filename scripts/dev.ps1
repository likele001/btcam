# ============================================================
# 一键开发启动：后端(FastAPI) + 前端(Tauri dev)
# 用法:  powershell -ExecutionPolicy Bypass -File scripts\dev.ps1
#        powershell ... dev.ps1 -NoTauri    # 只起后端 + Vite（浏览器里调 UI）
#
# 说明：核心调试命令就是 `cargo tauri dev`（在 frontend\ 下执行）。
#       本脚本是可选便利入口：先拉起 uvicorn，再把 Tauri 交给 cargo；
#       已装 Rust 时两者等价 —— lib.rs 在 dev 模式下会自己起后端。
#
# 进程回收：下面注入 BTCAM_PARENT_PID，后端会盯着启动它的进程，
#           本窗口一关，后端自杀，不会在 8975 端口留孤儿进程。
# ============================================================
param(
    [switch]$NoTauri,
    [switch]$NoBackend
)

$ErrorActionPreference = 'Stop'
$root     = Resolve-Path (Join-Path $PSScriptRoot '..')
$backend  = Join-Path $root 'backend'
$frontend = Join-Path $root 'frontend'
$venvPy   = Join-Path $backend '.venv\Scripts\python.exe'

Write-Host "== BtCAM 开发环境 ==" -ForegroundColor Cyan
Write-Host "项目根目录: $root"

# ---------- 1. 后端环境 ----------
if (-not $NoBackend) {
    if (-not (Test-Path $venvPy)) {
        Write-Host "[1/3] 创建 Python 虚拟环境..." -ForegroundColor Yellow
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'install_backend.ps1')
        if (-not (Test-Path $venvPy)) { throw "虚拟环境创建失败，请检查 Python 安装" }
    }

    Write-Host "[2/3] 启动后端 (127.0.0.1:8975)..." -ForegroundColor Yellow
    # 进程级环境变量会被子进程继承；后端 procguard 用它监听本窗口存活
    $env:BTCAM_PARENT_PID = $PID
    Start-Process -FilePath $venvPy `
        -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8975', '--reload') `
        -WorkingDirectory $backend `
        -WindowStyle Minimized
    Start-Sleep -Seconds 2
}

# ---------- 2. 前端依赖 ----------
if (-not (Test-Path (Join-Path $frontend 'node_modules'))) {
    Write-Host "[3/3] 安装前端依赖 (首次较慢)..." -ForegroundColor Yellow
    Push-Location $frontend
    npm install
    Pop-Location
}

# ---------- 3. 启动 Tauri ----------
Push-Location $frontend
try {
    if ($NoTauri) {
        Write-Host "启动 Vite 开发服务器: http://127.0.0.1:5173" -ForegroundColor Green
        npm run dev
    } else {
        Write-Host "启动 Tauri 桌面窗口（首次编译 Rust 约需几分钟）..." -ForegroundColor Green
        npm run tauri dev
    }
} finally {
    Pop-Location
}

# ============================================================
# 后端开发服务器（单独启动，方便只调 Python）
# 用法: powershell -ExecutionPolicy Bypass -File scripts\run_backend.ps1
# 可选: -Port 8975  -Reload:$false  -Workers 4
# ============================================================
param(
    [int]$Port = 8975,
    [string]$HostAddr = '127.0.0.1',
    [switch]$NoReload,
    [int]$Workers = 1
)

$ErrorActionPreference = 'Stop'
$backend = Resolve-Path (Join-Path $PSScriptRoot '..\backend')
$venvPy  = Join-Path $backend '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPy)) {
    Write-Host "未找到虚拟环境，先执行 scripts\install_backend.ps1" -ForegroundColor Yellow
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'install_backend.ps1')
}

$env:PYTHONUTF8 = '1'
$env:BTCAM_PORT = "$Port"

# 注：不要用 $args，它是 PowerShell 自动变量
$uvicornArgs = @('-m', 'uvicorn', 'app.main:app', '--host', $HostAddr, '--port', "$Port")
if (-not $NoReload) { $uvicornArgs += '--reload' }
if ($Workers -gt 1) { $uvicornArgs += @('--workers', "$Workers") }

Write-Host "BtCAM 后端: http://${HostAddr}:$Port  (API 文档 /docs)" -ForegroundColor Cyan
Push-Location $backend
try {
    & $venvPy @uvicornArgs
} finally {
    Pop-Location
}

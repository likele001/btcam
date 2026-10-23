# ============================================================
# 后端环境安装：虚拟环境 + 依赖 + (可选) OpenCASCADE 绑定
# 用法: powershell -ExecutionPolicy Bypass -File scripts\install_backend.ps1
#       -SkipOcct   跳过 OCCT 安装（只跑 pyclipper 兜底引擎）
# ============================================================
param(
    [switch]$SkipOcct,
    [string]$PythonExe = 'python'
)

$ErrorActionPreference = 'Stop'
$backend = Resolve-Path (Join-Path $PSScriptRoot '..\backend')
$venvDir = Join-Path $backend '.venv'
$venvPy  = Join-Path $venvDir 'Scripts\python.exe'

Write-Host "== 安装 BtCAM 后端依赖 ==" -ForegroundColor Cyan

# ---------- 1. 检查 Python 版本 ----------
$version = & $PythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
Write-Host "检测到 Python $version"
if ([version]$version -lt [version]'3.9') {
    Write-Host "警告: 建议使用 Python 3.10/3.11，当前版本可能导致 OCCT 绑定无法安装" -ForegroundColor Yellow
}

# ---------- 2. 创建虚拟环境 ----------
if (-not (Test-Path $venvPy)) {
    Write-Host "创建虚拟环境 -> $venvDir" -ForegroundColor Yellow
    & $PythonExe -m venv $venvDir
}

# ---------- 3. 安装依赖 ----------
Write-Host "升级 pip / setuptools / wheel..." -ForegroundColor Yellow
& $venvPy -m pip install --upgrade pip setuptools wheel --quiet

Write-Host "安装 requirements.txt..." -ForegroundColor Yellow
& $venvPy -m pip install -r (Join-Path $backend 'requirements.txt')

# ---------- 4. OpenCASCADE 绑定（可选，失败不阻断） ----------
if (-not $SkipOcct) {
    Write-Host "尝试安装 OCCT 绑定 cadquery-ocp（约 200MB，可跳过）..." -ForegroundColor Yellow
    & $venvPy -m pip install cadquery-ocp
    if ($LASTEXITCODE -ne 0) {
        Write-Host "OCCT 安装失败，将使用 pyclipper 引擎；如需 OCCT 请用 conda:" -ForegroundColor Yellow
        Write-Host "  conda install -c conda-forge pythonocc-core" -ForegroundColor Yellow
    }
}

# ---------- 5. 自检 ----------
Write-Host "环境自检:" -ForegroundColor Green
# 用单引号 here-string，避免 PowerShell 转义与 Python 引号互相干扰
$checkScript = @'
import importlib
for name in ('fastapi', 'uvicorn', 'numpy', 'pyclipper'):
    try:
        mod = importlib.import_module(name)
        print('  [OK]   %-12s %s' % (name, getattr(mod, '__version__', '')))
    except Exception as exc:
        print('  [MISS] %-12s %s' % (name, exc))
try:
    import OCC.Core  # noqa: F401
    print('  [OK]   OCCT (pythonocc-core)')
except ImportError:
    try:
        import OCP  # noqa: F401
        print('  [OK]   OCCT (cadquery-ocp)')
    except ImportError:
        print('  [----] OCCT 未安装，将使用 pyclipper 兜底引擎')
'@
& $venvPy -c $checkScript

Write-Host "完成。启动后端: scripts\run_backend.ps1" -ForegroundColor Green

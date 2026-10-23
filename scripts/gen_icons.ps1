# ============================================================
# 生成应用图标（占位图标，正式产品请替换为设计稿）
# 产物: frontend\src-tauri\icons\{32x32.png,128x128.png,128x128@2x.png,icon.png,icon.ico}
# 用法: powershell -ExecutionPolicy Bypass -File scripts\gen_icons.ps1
#       -OutDir 自定义输出目录
# ============================================================
param(
    [string]$OutDir = ''
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

if ([string]::IsNullOrWhiteSpace($OutDir)) {
    $OutDir = Join-Path $PSScriptRoot '..\frontend\src-tauri\icons'
}
$OutDir = [System.IO.Path]::GetFullPath($OutDir)
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

Write-Host "生成图标 -> $OutDir" -ForegroundColor Cyan

function New-IconPngBytes {
    param([int]$Size)

    $bmp = [System.Drawing.Bitmap]::new(
        $Size, $Size, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    try {
        $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
        $g.Clear([System.Drawing.Color]::FromArgb(255, 20, 22, 26))

        # 顶部品牌色条
        $barH = [Math]::Max(1, [int]($Size * 0.09))
        $bar = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(255, 79, 163, 255))
        $g.FillRectangle($bar, 0, 0, $Size, $barH)
        $bar.Dispose()

        # 轮廓刀路（开口 U 形）
        $penW = [Math]::Max(1.0, $Size * 0.075)
        $pen = [System.Drawing.Pen]::new([System.Drawing.Color]::FromArgb(255, 214, 226, 240), [single]$penW)
        $pen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
        $pen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
        $pen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round

        $points = [System.Drawing.PointF[]]@(
            [System.Drawing.PointF]::new([single]($Size * 0.70), [single]($Size * 0.30)),
            [System.Drawing.PointF]::new([single]($Size * 0.34), [single]($Size * 0.30)),
            [System.Drawing.PointF]::new([single]($Size * 0.34), [single]($Size * 0.72)),
            [System.Drawing.PointF]::new([single]($Size * 0.72), [single]($Size * 0.72))
        )
        $g.DrawLines($pen, $points)
        $pen.Dispose()

        # 刀尖（终点高亮）
        $r = [single]($Size * 0.09)
        $dot = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(255, 255, 107, 61))
        $g.FillEllipse($dot, [single]($Size * 0.72 - $r), [single]($Size * 0.72 - $r),
            [single]($r * 2), [single]($r * 2))
        $dot.Dispose()
    } finally {
        $g.Dispose()
    }

    $ms = [System.IO.MemoryStream]::new()
    $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    $bytes = $ms.ToArray()
    $ms.Dispose()
    return $bytes
}

# ---------- 1. 生成 PNG ----------
$pngTargets = @{
    '32x32.png'       = 32
    '128x128.png'     = 128
    '128x128@2x.png'  = 256
    'icon.png'        = 512
}
$pngData = @{}
foreach ($name in $pngTargets.Keys) {
    $size = $pngTargets[$name]
    $bytes = New-IconPngBytes -Size $size
    [System.IO.File]::WriteAllBytes((Join-Path $OutDir $name), $bytes)
    $pngData[$size] = $bytes
    Write-Host ("  PNG {0,-14} {1,4}x{1,-4} {2,6} B" -f $name, $size, $bytes.Length)
}

# ---------- 2. 生成 ICO（多尺寸，PNG 压缩格式，Vista+ 原生支持） ----------
$icoSizes = @(16, 32, 48, 256)
$entries = @()
foreach ($size in $icoSizes) {
    # 显式转 [byte[]]，否则 PowerShell 可能按 Write(byte) 重载绑定，只写 1 字节
    [byte[]]$png = if ($pngData.ContainsKey($size)) { $pngData[$size] } else { New-IconPngBytes -Size $size }
    $entries += [pscustomobject]@{ Size = $size; Bytes = $png }
}

$icoPath = Join-Path $OutDir 'icon.ico'
$msIco = [System.IO.MemoryStream]::new()
$writer = [System.IO.BinaryWriter]::new($msIco)
try {
    # ICONDIR
    $writer.Write([UInt16]0)                      # reserved
    $writer.Write([UInt16]1)                      # type = icon
    $writer.Write([UInt16]$entries.Count)         # count

    # ICONDIRENTRY x N
    $offset = 6 + 16 * $entries.Count
    foreach ($entry in $entries) {
        [byte[]]$data = $entry.Bytes
        $dim = if ($entry.Size -ge 256) { 0 } else { $entry.Size }   # 0 表示 256
        $writer.Write([byte]$dim)
        $writer.Write([byte]$dim)
        $writer.Write([byte]0)                    # colorCount
        $writer.Write([byte]0)                    # reserved
        $writer.Write([UInt16]1)                  # planes
        $writer.Write([UInt16]32)                 # bitCount
        $writer.Write([UInt32]$data.Length)
        $writer.Write([UInt32]$offset)
        $offset += $data.Length
    }

    # 图像数据（用带 offset/count 的重载，避免重载歧义）
    foreach ($entry in $entries) {
        [byte[]]$data = $entry.Bytes
        $writer.Write($data, 0, $data.Length)
    }
    $writer.Flush()
} finally {
    $writer.Dispose()
}

[System.IO.File]::WriteAllBytes($icoPath, $msIco.ToArray())
$msIco.Dispose()

$icoSize = (Get-Item $icoPath).Length
if ($icoSize -lt 1024) {
    throw "ICO 生成异常（仅 $icoSize 字节），请检查图标数据"
}
Write-Host ("  ICO icon.ico    {0,8} B  含尺寸: {1}" -f $icoSize, ($icoSizes -join ',')) -ForegroundColor Green
Write-Host "图标生成完成" -ForegroundColor Green

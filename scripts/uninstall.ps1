#requires -Version 5.1
<#
  像素画（Aseprite）preset + 工具箱 —— 卸载器

  做两件事：
    1) 从 profile 的 cordis.patch.yml 里删掉本安装器追加的 preset 块（带标记，精确删除）
    2) 可选：删掉工具包目录（-RemovePayload）

  用法：
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\uninstall.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\uninstall.ps1 -RemovePayload
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\uninstall.ps1 -Profile desktop

  删完后要重启 DSH 才会从预设列表里消失。
#>
[CmdletBinding()]
param(
    [string]$ToolkitDir = (Join-Path $env:USERPROFILE ".dsh\pixel-toolkit"),
    [string]$Profile = "",
    [switch]$RemovePayload
)

$ErrorActionPreference = "Stop"
$MarkerBegin = "# >>> pixel-toolkit preset-pixel >>>"
$MarkerEnd = "# <<< pixel-toolkit preset-pixel <<<"

function Write-Step([string]$Text) { Write-Host "" ; Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "    OK  $Text" -ForegroundColor Green }
function Write-Note([string]$Text) { Write-Host "    --  $Text" -ForegroundColor Yellow }
function Fail([string]$Text) { Write-Host "" ; Write-Host "[卸载失败] $Text" -ForegroundColor Red ; exit 1 }

# ---------------------------------------------------------------- 1. 找 profile --
Write-Step "定位 DSH profile"
$profileRoot = Join-Path $env:USERPROFILE ".dsh\profiles"
if (-not (Test-Path $profileRoot)) { Fail "找不到 $profileRoot" }
$profileName = ""
if ($Profile) {
    if (-not (Test-Path (Join-Path $profileRoot $Profile))) { Fail "profile 不存在：$Profile" }
    $profileName = $Profile
}
else {
    foreach ($n in @("web", "desktop")) { if (Test-Path (Join-Path $profileRoot $n)) { $profileName = $n ; break } }
    if (-not $profileName) { Fail "没找到可用的 profile，用 -Profile 指定" }
}
$patchPath = Join-Path (Join-Path $profileRoot $profileName) "cordis.patch.yml"
if (-not (Test-Path $patchPath)) { Fail "找不到 $patchPath" }
Write-Ok "补丁文件=$patchPath"

# ---------------------------------------------------------------- 2. 删块 --
Write-Step "删除 preset 块"
$orig = [IO.File]::ReadAllText($patchPath, [Text.Encoding]::UTF8)
if (-not $orig.Contains($MarkerBegin) -and -not $orig.Contains($MarkerEnd)) {
    Write-Note "补丁里没有本安装器的标记块 —— 可能已卸载过，或这份 preset 是手工装的（需手工删）。"
}
else {
    $hasBom = $false
    $fs = [IO.File]::OpenRead($patchPath)
    $b0 = $fs.ReadByte() ; $b1 = $fs.ReadByte() ; $b2 = $fs.ReadByte()
    $fs.Close()
    if ($b0 -eq 239 -and $b1 -eq 187 -and $b2 -eq 191) { $hasBom = $true }

    $stamp = (Get-Date).ToString("yyyyMMdd-HHmmss")
    $backupPath = "$patchPath.bak-pixel-uninstall-$stamp"
    Copy-Item $patchPath $backupPath -Force
    Write-Ok "已备份：$(Split-Path -Leaf $backupPath)"

    $strip = "(?s)\r?\n?" + [regex]::Escape($MarkerBegin) + ".*?" + [regex]::Escape($MarkerEnd) + "\r?\n?"
    $newText = [regex]::Replace($orig, $strip, "")
    $enc = New-Object System.Text.UTF8Encoding($hasBom)
    [IO.File]::WriteAllText($patchPath, $newText, $enc)

    $after = [IO.File]::ReadAllText($patchPath, [Text.Encoding]::UTF8)
    $problems = New-Object System.Collections.Generic.List[string]
    if ($after.Contains($MarkerBegin) -or $after.Contains($MarkerEnd)) { $problems.Add("块标记仍在") }
    if ($after.Contains("- id: preset-pixel")) { $problems.Add("preset-pixel 行仍在") }
    if ($after.Length -ge $orig.Length) { $problems.Add("文件没有变短") }
    if ($problems.Count -gt 0) {
        Copy-Item $backupPath $patchPath -Force
        Fail ("删除后自校验未通过，已回滚：" + ($problems -join "；"))
    }
    Write-Ok "已删除（$([math]::Round(($orig.Length - $after.Length) / 1024, 1)) KB），自校验通过"
}

# ---------------------------------------------------------------- 3. payload --
Write-Step "工具包目录"
if ($RemovePayload) {
    if (Test-Path $ToolkitDir) {
        $resolved = (Resolve-Path $ToolkitDir).Path
        if (-not $resolved.EndsWith("pixel-toolkit") -and -not $resolved.EndsWith("pixel-kit")) {
            Write-Note "注意：目录名不是 pixel-toolkit/pixel-kit（$resolved）—— 请确认没有删错，5 秒后继续；Ctrl+C 可中止。"
            Start-Sleep -Seconds 5
        }
        Remove-Item $resolved -Recurse -Force
        Write-Ok "已删除 $resolved"
    }
    else { Write-Note "目录不存在，跳过：$ToolkitDir" }
}
else {
    Write-Note "保留 $ToolkitDir（要一起删就加 -RemovePayload）"
}

Write-Host ""
Write-Host "卸载完成。重启 DSH 后，「像素画（Aseprite）」才会从预设列表消失。" -ForegroundColor Green

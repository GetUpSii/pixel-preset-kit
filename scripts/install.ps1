#requires -Version 5.1
<#
  像素画（Aseprite）preset + 工具箱 —— 安装器

  两种用法：
    A) 用 release zip：解压后直接跑本脚本（脚本旁边就有 manifest.json 和 payload\）
    B) 用 git clone：在仓库里跑 scripts\install.ps1，脚本会自动先构建 dist\ 再安装

  做五件事：
    1) 校验发布包完整性（manifest.json 的文件清单 + 关键文件校验和）
    2) 检查前置条件（node / Aseprite）
    3) 把 payload 复制到工具包目录（默认 %USERPROFILE%\.dsh\pixel-toolkit）
    4) 生成 preset 块（填入工具包目录与 Aseprite 路径），追加到 profile 的 cordis.patch.yml
    5) 结构自校验（标记成对、preset 只有一份、无 tab 缩进、关键行齐全）；任一条不过就回滚

  参数：
    -ToolkitDir  工具包落地目录（默认 %USERPROFILE%\.dsh\pixel-toolkit）
    -Profile     DSH profile 名（默认自动：先 web，再 desktop）
    -DryRun      只演练，不写任何文件
#>
[CmdletBinding()]
param(
    [string]$ToolkitDir = (Join-Path $env:USERPROFILE ".dsh\pixel-toolkit"),
    [string]$Profile = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$MarkerBegin = "# >>> pixel-toolkit preset-pixel >>>"
$MarkerEnd = "# <<< pixel-toolkit preset-pixel <<<"
$KitDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Step([string]$Text) { Write-Host "" ; Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "    OK  $Text" -ForegroundColor Green }
function Write-Note([string]$Text) { Write-Host "    --  $Text" -ForegroundColor Yellow }
function Fail([string]$Text) { Write-Host "" ; Write-Host "[安装失败] $Text" -ForegroundColor Red ; exit 1 }

# --------------------------------------------------- 0. 从源码仓库跑时先构建 --
if (-not (Test-Path (Join-Path $KitDir "manifest.json"))) {
    $repoRoot = Split-Path -Parent $KitDir
    $builder = Join-Path $repoRoot "scripts\build-kit.mjs"
    if (-not (Test-Path $builder)) {
        Fail "找不到 manifest.json，也没找到 scripts\build-kit.mjs —— 请解压完整的 release zip，或在仓库根目录运行 scripts\install.ps1。"
    }
    Write-Step "这是源码仓库，先构建发布包"
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) { Fail "构建需要 node（18+），请先安装。" }
    & node $builder
    if ($LASTEXITCODE -ne 0) { Fail "构建失败（node scripts/build-kit.mjs 的报错见上）" }
    $KitDir = Join-Path (Join-Path $repoRoot "dist") "pixel-preset-kit"
    if (-not (Test-Path (Join-Path $KitDir "manifest.json"))) { Fail "构建后仍找不到 $KitDir\manifest.json" }
    Write-Ok "构建完成：$KitDir"
}

# ---------------------------------------------------------------- 1. 包完整性 --
Write-Step "校验发布包完整性"
$manifest = Get-Content (Join-Path $KitDir "manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$checked = 0
foreach ($f in $manifest.files) {
    $p = Join-Path $KitDir ($f.path -replace "/", "\")
    if (-not (Test-Path $p)) { Fail "发布包缺文件：$($f.path)" }
    if ((Get-Item $p).Length -ne $f.bytes) { Fail "文件大小不符（zip 可能被截断）：$($f.path)" }
    $checked++
}
foreach ($key in @("payload/mcp/build/index.js", "payload/skills/aseprite-pixel-art/SKILL.md", "preset-pixel.patch.yml.tpl")) {
    $p = Join-Path $KitDir ($key -replace "/", "\")
    if (-not (Test-Path $p)) { Fail "发布包缺关键文件：$key" }
    $rec = $manifest.files | Where-Object { $_.path -eq $key }
    if ($rec) {
        $got = (Get-FileHash $p -Algorithm SHA256).Hash.ToLower()
        if ($got -ne $rec.sha256) { Fail "文件校验和不符：$key" }
    }
}
Write-Ok "$checked 个文件齐全，关键文件校验通过（v$($manifest.version)）"

# ---------------------------------------------------------------- 2. 前置条件 --
Write-Step "检查前置条件"
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Fail "未找到 node。Aseprite MCP 服务端由 node 启动，请先装 Node.js 18+（推荐 20/22）。"
}
$nodeVer = (& node -p "process.versions.node").Trim()
$nodeMajor = [int](($nodeVer -split '\.')[0])
if ($nodeMajor -lt 18) { Fail "node 版本过低：$nodeVer（需要 18+）" }
Write-Ok "node $nodeVer"

function Find-Aseprite {
    if ($env:ASEPRITE_PATH -and (Test-Path $env:ASEPRITE_PATH)) { return $env:ASEPRITE_PATH }
    $cands = @(
        "C:\Program Files\Aseprite\Aseprite.exe",
        "C:\Program Files (x86)\Aseprite\Aseprite.exe",
        "C:\Program Files (x86)\Steam\steamapps\common\Aseprite\Aseprite.exe",
        "C:\Program Files\Steam\steamapps\common\Aseprite\Aseprite.exe",
        (Join-Path $env:LOCALAPPDATA "Programs\Aseprite\Aseprite.exe")
    )
    foreach ($c in $cands) { if ($c -and (Test-Path $c)) { return $c } }
    $w = & cmd /c "where aseprite 2>nul"
    if ($LASTEXITCODE -eq 0 -and $w) { return (($w | Select-Object -First 1) -as [string]).Trim() }
    return ""
}
$aseprite = Find-Aseprite
if ($aseprite) { Write-Ok "Aseprite：$aseprite" }
else {
    Write-Note "没找到 Aseprite.exe —— preset 仍会装上，但第一次画图时 MCP 会报错。"
    Write-Note "装好 Aseprite 后，手动在 cordis.patch.yml 的 mcp-aseprite → env 下加 ASEPRITE_PATH: <路径>，或重跑本安装器。"
}

# ---------------------------------------------------------------- 3. 找 profile --
Write-Step "定位 DSH profile"
$profileRoot = Join-Path $env:USERPROFILE ".dsh\profiles"
if (-not (Test-Path $profileRoot)) { Fail "找不到 $profileRoot —— 这台机器上还没跑过 DSH，或 DSH 主目录不在默认位置。" }
$profileName = ""
if ($Profile) {
    if (-not (Test-Path (Join-Path $profileRoot $Profile))) {
        $avail = (Get-ChildItem $profileRoot -Directory | Select-Object -ExpandProperty Name) -join ", "
        Fail "profile 不存在：$Profile（现有：$avail）"
    }
    $profileName = $Profile
}
else {
    foreach ($n in @("web", "desktop")) { if (Test-Path (Join-Path $profileRoot $n)) { $profileName = $n ; break } }
    if (-not $profileName) { Fail "没找到可用的 profile（$profileRoot 下）。用 -Profile <名字> 指定。" }
}
$patchPath = Join-Path (Join-Path $profileRoot $profileName) "cordis.patch.yml"
if (-not (Test-Path $patchPath)) { Fail "找不到 $patchPath —— 先启动一次 DSH（例如 dsh web）让它生成，再重跑本安装器。" }
Write-Ok "profile=$profileName"
Write-Ok "补丁文件=$patchPath"
if ($profileName -eq "web" -and (Test-Path (Join-Path $profileRoot "desktop"))) {
    Write-Note "这台机器还有 desktop profile；如果你日常用桌面版，请加 -Profile desktop 重跑。"
}

# ---------------------------------------------------------------- 4. 读旧补丁 --
Write-Step "读取现有 profile 补丁"
$orig = [IO.File]::ReadAllText($patchPath, [Text.Encoding]::UTF8)
$hasBom = $false
$fs = [IO.File]::OpenRead($patchPath)
$b0 = $fs.ReadByte() ; $b1 = $fs.ReadByte() ; $b2 = $fs.ReadByte()
$fs.Close()
if ($b0 -eq 239 -and $b1 -eq 187 -and $b2 -eq 191) { $hasBom = $true }
$eol = "`n"
if ($orig.Contains("`r`n")) { $eol = "`r`n" }
$eolName = "LF"
if ($eol -eq "`r`n") { $eolName = "CRLF" }
Write-Ok "现有补丁 $([math]::Round($orig.Length / 1024, 1)) KB，EOL=$eolName，BOM=$hasBom"

# 去掉旧块（重装 = 原地升级）
$base = $orig
if ($orig.Contains($MarkerBegin)) {
    $strip = "(?s)" + [regex]::Escape($MarkerBegin) + ".*?" + [regex]::Escape($MarkerEnd)
    $base = [regex]::Replace($orig, $strip, "")
    $base = $base.TrimEnd([char[]]"`r`n")
    Write-Ok "检测到旧的 pixel-toolkit 块，将原地替换"
}
else {
    if ($base.Contains("- id: preset-pixel")) {
        Fail "补丁里已有一个手写的 preset-pixel（不是本安装器生成的）。请先自行删除那一段再重跑，避免出现同名 preset。"
    }
}

# ---------------------------------------------------------------- 5. 复制 payload --
Write-Step "复制工具包到 $ToolkitDir"
if ($DryRun) {
    Write-Note "DryRun：不实际复制、不写文件"
}
else {
    New-Item -ItemType Directory -Force -Path $ToolkitDir | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $ToolkitDir "tmp") | Out-Null
    $srcPayload = Join-Path $KitDir "payload"
    & robocopy $srcPayload $ToolkitDir /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "robocopy 复制失败（exit $LASTEXITCODE）" }
    foreach ($need in @("mcp\build\index.js", "skills\aseprite-pixel-art\SKILL.md", "vision\image-diff.py", "tools\atlas-pack.py")) {
        if (-not (Test-Path (Join-Path $ToolkitDir $need))) { Fail "复制后缺文件：$need" }
    }
    Write-Ok "已复制 mcp / skills / vision / tools"
}

# ---------------------------------------------------------------- 6. 生成块 --
Write-Step "生成 preset 块"
$tpl = [IO.File]::ReadAllText((Join-Path $KitDir "preset-pixel.patch.yml.tpl"), [Text.Encoding]::UTF8)
$toolkitFull = $ToolkitDir.TrimEnd("\")
if ($aseprite) { $aseLine = "                ASEPRITE_PATH: $aseprite" }
else { $aseLine = "                # ASEPRITE_PATH: 未检测到 Aseprite；如需指定，取消注释并填 Aseprite.exe 完整路径" }
$blockLines = New-Object System.Collections.Generic.List[string]
foreach ($line in ($tpl -split "\r?\n")) {
    if ($line.Trim() -eq "__ASEPRITE_PATH_LINE__") { $blockLines.Add($aseLine) }
    else { $blockLines.Add($line.Replace("__PIXEL_TOOLKIT__", $toolkitFull)) }
}
$block = ($blockLines -join $eol)
if ($block.Contains("__PIXEL_TOOLKIT__") -or $block.Contains("__ASEPRITE_PATH_LINE__")) {
    Fail "占位符没替换干净，中止（避免写坏 profile）"
}
foreach ($need in @("- insert:", "- id: preset-pixel", "id: pixel", "- id: mcp-aseprite", "customSkillDirs", "- id: skill-filesystem")) {
    if (-not $block.Contains($need)) { Fail "生成的块缺关键行：$need" }
}
Write-Ok "块生成完毕（$($blockLines.Count) 行），工具包路径=$toolkitFull"

# ---------------------------------------------------------------- 7. 追加 + 校验 --
Write-Step "写入 profile 补丁"
$payloadText = $eol + $MarkerBegin + $eol + $block.TrimEnd([char[]]"`r`n") + $eol + $MarkerEnd + $eol
$newText = $base.TrimEnd([char[]]"`r`n") + $eol + $payloadText

if ($DryRun) {
    Write-Note "DryRun：以下内容本应追加到 $patchPath"
    Write-Host "----- 8< -----"
    Write-Host $block
    Write-Host "----- >8 -----"
    Write-Host ""
    Write-Host "DryRun 结束：没有写任何文件。" -ForegroundColor Yellow
    exit 0
}

$stamp = (Get-Date).ToString("yyyyMMdd-HHmmss")
$backupPath = "$patchPath.bak-pixel-$stamp"
Copy-Item $patchPath $backupPath -Force
Write-Ok "已备份：$(Split-Path -Leaf $backupPath)"

$enc = New-Object System.Text.UTF8Encoding($hasBom)
[IO.File]::WriteAllText($patchPath, $newText, $enc)

# 结构自校验（任何一条不过就回滚）
$problems = New-Object System.Collections.Generic.List[string]
$after = [IO.File]::ReadAllText($patchPath, [Text.Encoding]::UTF8)
if (-not $after.StartsWith($base.TrimEnd([char[]]"`r`n"), [StringComparison]::Ordinal)) { $problems.Add("写入后旧内容被改动（不是纯追加）") }
if ($after.Length -le $base.Length) { $problems.Add("写入后文件没有变长") }
$cntId = ([regex]::Matches($after, [regex]::Escape("- id: preset-pixel"))).Count
if ($cntId -ne 1) { $problems.Add("preset-pixel 出现 $cntId 次（应为 1）") }
$cntB = ([regex]::Matches($after, [regex]::Escape($MarkerBegin))).Count
$cntE = ([regex]::Matches($after, [regex]::Escape($MarkerEnd))).Count
if ($cntB -ne 1 -or $cntE -ne 1) { $problems.Add("块标记不成对（begin=$cntB end=$cntE）") }
foreach ($line in ($block -split "\r?\n")) {
    if ($line.Contains("`t")) { $problems.Add("块内含 tab 缩进：$line") ; break }
    if ($line.Trim() -eq "") { continue }
    if ($line.StartsWith("#")) { continue }
    if ($line -eq "- insert:") { continue }
    if (-not $line.StartsWith("    ")) { $problems.Add("块内有行缩进不足 4 空格：$line") ; break }
}
if ($problems.Count -gt 0) {
    Copy-Item $backupPath $patchPath -Force
    $msg = ($problems | ForEach-Object { "  - $_" }) -join "`r`n"
    Fail "结构自校验未通过，已回滚到备份：`r`n$msg"
}
Write-Ok "结构自校验通过（标记成对 / preset-pixel 唯一 / 无 tab / 缩进正确）"

Write-Host ""
Write-Host "安装完成。" -ForegroundColor Green
Write-Host ""
Write-Host "下一步：" -ForegroundColor Cyan
Write-Host "  1) 重启 DSH：关掉正在跑的 dsh web（或桌面版）再启动 —— preset 只在重启后被注册。"
Write-Host "  2) 打开 设置 → 通用 → 「Agent 预设」，应能看到卡片「像素画（Aseprite）」。"
Write-Host "     卡片显示「配置失败」= 插件行有问题，把卡片里「查看配置」顶部的诊断发出来。"
Write-Host "  3) 新建任务时选这个预设，然后说：画一个 16x16 的红色方块给我看。"
Write-Host "     成败判据：工具列表里有 mcp__aseprite__*，且能导出 PNG。"
Write-Host ""
Write-Host "工具包目录：$toolkitFull"
Write-Host "补丁文件：  $patchPath"
Write-Host "备份：      $backupPath"
Write-Host "卸载：      powershell -NoProfile -ExecutionPolicy Bypass -File uninstall.ps1"

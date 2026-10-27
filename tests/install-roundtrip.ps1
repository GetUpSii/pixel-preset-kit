#requires -Version 5.1
<#
  安装器往返测试 —— 不碰真实 profile。

  做法：在 tests\.tmp\home 造一个假 %USERPROFILE%，放一份最小 patch（tests\fixture.patch.yml），
  然后跑 install → install（应原地升级）→ uninstall，并断言：
    · 块写入且只有一个 preset-pixel、标记成对、占位符替换干净
    · payload 落地到假 home 的 ~\.dsh\pixel-toolkit（含 mcp\build\index.js）
    · preset 里指向的是 mcp\build\index.js（不是 mcp\index.js）
    · 二次安装在原地替换，文件不再变长
    · 卸载后补丁与 fixture 逐字节相同

  用法（仓库根目录）：
    powershell -NoProfile -ExecutionPolicy Bypass -File tests\install-roundtrip.ps1
    powershell ... -File tests\install-roundtrip.ps1 -Kit ..\dist\pixel-preset-kit
#>
[CmdletBinding()]
param(
    [string]$Kit = ""
)

$ErrorActionPreference = "Stop"
$tests = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $tests
if (-not $Kit) { $Kit = Join-Path (Join-Path $repo "dist") "pixel-preset-kit" }
$tmp = Join-Path $tests ".tmp"
$home2 = Join-Path $tmp "home"
$fail = 0

function Check($ok, $what) {
    if ($ok) { Write-Host "  PASS $what" }
    else { Write-Host "  FAIL $what" -ForegroundColor Red; $script:fail++ }
}

if (-not (Test-Path (Join-Path $Kit "manifest.json"))) {
    throw "no release at $Kit — run `node scripts/build-kit.mjs` first (or pass -Kit)"
}

Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
$profileDir = Join-Path $home2 ".dsh\profiles\web"
New-Item -ItemType Directory -Force -Path $profileDir | Out-Null
$patch = Join-Path $profileDir "cordis.patch.yml"
Copy-Item (Join-Path $tests "fixture.patch.yml") $patch
$origHash = (Get-FileHash $patch -Algorithm SHA256).Hash
$origLen = (Get-Item $patch).Length

# A stand-in Aseprite that really exists, so the installer renders an ASEPRITE_PATH line.
$fakeAse = Join-Path $tmp "FakeAseprite.exe"
[IO.File]::WriteAllText($fakeAse, "")
$env:USERPROFILE = $home2
$env:ASEPRITE_PATH = $fakeAse

Write-Host "`n[1] install"
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Kit "install.ps1")
Check ($LASTEXITCODE -eq 0) "installer exit 0"
$t = [IO.File]::ReadAllText($patch, [Text.Encoding]::UTF8)
Check (([regex]::Matches($t, [regex]::Escape("- id: preset-pixel"))).Count -eq 1) "preset-pixel appears once"
Check (([regex]::Matches($t, "# >>> pixel-toolkit preset-pixel >>>")).Count -eq 1) "begin marker once"
Check (([regex]::Matches($t, "# <<< pixel-toolkit preset-pixel <<<")).Count -eq 1) "end marker once"
Check ($t.Contains("- id: preset-demo")) "existing entries untouched"
Check ($t.Contains($home2)) "toolkit path points at the fake home"
Check ($t.Contains("ASEPRITE_PATH: $fakeAse")) "ASEPRITE_PATH rendered from detection"
Check ($t.Contains("mcp\build\index.js")) "preset launches mcp\build\index.js"
Check (-not $t.Contains("__PIXEL_TOOLKIT__") -and -not $t.Contains("__ASEPRITE_PATH_LINE__")) "no placeholders left"
Check ((Get-Item $patch).Length -gt $origLen) "patch grew"
$tk = Join-Path $home2 ".dsh\pixel-toolkit"
Check (Test-Path (Join-Path $tk "mcp\build\index.js")) "payload: mcp"
Check (Test-Path (Join-Path $tk "skills\aseprite-pixel-art\SKILL.md")) "payload: skill"
Check (Test-Path (Join-Path $tk "vision\image-diff.py")) "payload: vision"
$afterFirst = $t

Write-Host "`n[2] install again (must upgrade in place)"
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Kit "install.ps1")
Check ($LASTEXITCODE -eq 0) "second run exit 0"
$t2 = [IO.File]::ReadAllText($patch, [Text.Encoding]::UTF8)
Check (([regex]::Matches($t2, [regex]::Escape("- id: preset-pixel"))).Count -eq 1) "still one preset-pixel"
Check (([regex]::Matches($t2, "# >>> pixel-toolkit preset-pixel >>>")).Count -eq 1) "still one marker pair"
Check ($t2.Length -eq $afterFirst.Length) "file did not grow"

Write-Host "`n[3] uninstall restores the original bytes"
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Kit "uninstall.ps1")
Check ($LASTEXITCODE -eq 0) "uninstaller exit 0"
$t3 = [IO.File]::ReadAllText($patch, [Text.Encoding]::UTF8)
Check (-not $t3.Contains("- id: preset-pixel")) "preset-pixel removed"
Check (-not $t3.Contains("pixel-toolkit")) "no toolkit references left"
Check ((Get-FileHash $patch -Algorithm SHA256).Hash -eq $origHash) "byte-identical to fixture"

Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
Write-Host ""
if ($fail -eq 0) { Write-Host "ALL CHECKS PASSED" -ForegroundColor Green ; exit 0 }
Write-Host "$fail CHECK(S) FAILED" -ForegroundColor Red
exit 1

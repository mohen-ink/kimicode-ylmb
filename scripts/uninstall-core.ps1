# Kimi Code usage panel - uninstall worker (runs from %TEMP%, never inside the
# plugin directory so the directory can be deleted afterwards).
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File uninstall-core.ps1 -Root <plugin-root>
# Keep ASCII only.
param([string]$Root = '')
$ErrorActionPreference = 'SilentlyContinue'

function Write-Step([string]$text) { Write-Host ('[*] ' + $text) }
function Write-Ok([string]$text)   { Write-Host ('    ' + $text) }
function Write-WarnLine([string]$text) { Write-Host ('    ! ' + $text) }

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($Root -eq '') { $Root = Split-Path -Parent $scriptDir }
$Root = [IO.Path]::GetFullPath($Root)

$kimiHome = Join-Path $env:USERPROFILE '.kimi-code'
$stateDir = Join-Path $kimiHome 'usage-dashboard'
$marker   = Join-Path $stateDir 'plugin-uninstall.json'
$workerDir = Join-Path $stateDir 'runtime\mobile-worker'
$tempCopy = Join-Path $env:TEMP 'kimi-code-usage-uninstall.ps1'
$runningFromTemp = ([IO.Path]::GetFullPath($MyInvocation.MyCommand.Path) -eq [IO.Path]::GetFullPath($tempCopy))

# ---------------------------------------------------------------- dist dir --
$dist = ''
$cfg = Join-Path $Root 'scripts\desktop_path.txt'
if (Test-Path -LiteralPath $cfg) {
    $p = (Get-Content -LiteralPath $cfg -Raw).Trim().Trim('"').Trim("'")
    if ($p -and (Test-Path -LiteralPath $p)) { $dist = $p }
}
if (-not $dist) {
    foreach ($c in @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Kimi Code\resources\desktop-dist'),
        'C:\Program Files\Kimi Code\resources\desktop-dist',
        'C:\Program Files (x86)\Kimi Code\resources\desktop-dist')) {
        if ($c -and (Test-Path -LiteralPath $c)) { $dist = $c; break }
    }
}
if (-not $dist) {
    foreach ($proc in (Get-Process -Name 'Kimi Code' -ErrorAction SilentlyContinue)) {
        $c = Join-Path (Split-Path -Parent $proc.Path) 'resources\desktop-dist'
        if (Test-Path -LiteralPath $c) { $dist = $c; break }
    }
}
if ($dist) { Write-Ok ("Desktop resources: " + $dist) } else { Write-WarnLine 'desktop-dist not found; HTML injection cleanup skipped' }

# ------------------------------------------------------------ guard marker --
[IO.Directory]::CreateDirectory($stateDir) | Out-Null
'{"plugin":"kimi-code-usage","state":"uninstalled"}' | Set-Content -LiteralPath $marker -Encoding ASCII
Write-Step 'Restart guard written (hooks will no longer respawn the service)'

# ------------------------------------------------------------- kill procs ---
$selfPid = $PID
$names = @(
    ([IO.Path]::GetFullPath((Join-Path $Root 'scripts\service.py'))).ToLowerInvariant(),
    ([IO.Path]::GetFullPath((Join-Path $Root 'scripts\mobile_worker.py'))).ToLowerInvariant()
)
$killed = 0
foreach ($p in (Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
    $cl = [string]$p.CommandLine
    if (-not $cl -or $p.ProcessId -eq $selfPid) { continue }
    $low = $cl.ToLowerInvariant()
    $hit = $false
    foreach ($n in $names) { if ($low.Contains($n)) { $hit = $true; break } }
    if (-not $hit -and $low.Contains('usage-dashboard') -and $low.Contains('cloudflared')) { $hit = $true }
    if (-not $hit -and $low.Contains('mobile-worker')) { $hit = $true }
    if (-not $hit) { continue }
    Write-Ok ("Stopping PID " + $p.ProcessId + "  " + ($cl.Substring(0, [Math]::Min(110, $cl.Length))))
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    $killed++
}
if ($killed -eq 0) { Write-Ok 'No plugin processes were running' }

# ------------------------------------------------------------- index.html ---
if ($dist) {
    $index = Join-Path $dist 'index.html'
    if (Test-Path -LiteralPath $index) {
        try {
            $html = [IO.File]::ReadAllText($index, [Text.Encoding]::UTF8)
            $before = $html.Length
            $patterns = @(
                '<script src="/assets/kimi-(embedded|usage)-(data|widget)\.js(\?[tv]=[0-9a-z]+)?"></script>',
                '<script src="/kimi-(embedded|usage)-(data|widget)\.js(\?[tv]=[0-9a-z]+)?"></script>',
                '<script src="/assets/kimi-usage-needpy\.js(\?v=[0-9a-f]+)?"></script>',
                '<script src="/assets/(vendor/qrcodegen|kimi-remote-(qr|api|widget)|kimi-mobile-api)\.js(\?v=[0-9a-f]+)?"></script>',
                '<link[^>]*href="/assets/kimi-remote-widget-live\.css(\?v=[0-9a-f]+)?"[^>]*>'
            )
            foreach ($pat in $patterns) { $html = [regex]::Replace($html, '(?m)^[ \t]*' + $pat + '[ \t]*\r?\n', '') }
            foreach ($pat in $patterns) { $html = [regex]::Replace($html, $pat, '') }
            $html = [regex]::Replace($html, '(\r?\n[ \t]*){3,}', "`n`n")
            if ($html.Length -ne $before) {
                [IO.File]::WriteAllText($index, $html, (New-Object Text.UTF8Encoding($false)))
                Write-Ok 'index.html injection tags removed'
            } else {
                Write-Ok 'index.html already clean'
            }
        } catch {
            Write-WarnLine ('index.html cleanup failed: ' + $_.Exception.Message)
        }
    }
    # --------------------------------------------------- injected assets ----
    $files = @(
        'kimi-usage-widget.js', 'kimi-usage-data.js', 'kimi-embedded-data.js',
        'kimi-usage-needpy.js', 'kimi-remote-qr.js', 'kimi-remote-api.js',
        'kimi-mobile-api.js', 'kimi-remote-widget.js', 'kimi-remote-widget-live.css',
        'vendor\qrcodegen.js'
    )
    $removed = 0
    foreach ($rel in $files) {
        $f = Join-Path (Join-Path $dist 'assets') $rel
        if (Test-Path -LiteralPath $f) { Remove-Item -LiteralPath $f -Force; $removed++ }
    }
    $rootJson = Join-Path $dist 'kimi-usage.json'
    if (Test-Path -LiteralPath $rootJson) { Remove-Item -LiteralPath $rootJson -Force; $removed++ }
    $vendorDir = Join-Path (Join-Path $dist 'assets') 'vendor'
    if ((Test-Path -LiteralPath $vendorDir) -and
        -not (Get-ChildItem -LiteralPath $vendorDir -Force -ErrorAction SilentlyContinue)) {
        Remove-Item -LiteralPath $vendorDir -Force
    }
    Write-Ok ("Removed " + $removed + " injected asset file(s)")
}

# ------------------------------------------------------- state directory ---
if (Test-Path -LiteralPath $stateDir) {
    Remove-Item -LiteralPath $stateDir -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $stateDir) {
        Write-WarnLine 'Some files under usage-dashboard are still locked; deleting what is possible'
        Write-WarnLine ("Left behind: " + $stateDir)
    } else {
        Write-Ok 'Removed usage-dashboard state directory'
    }
}

# ------------------------------------------------------- plugin directory --
if (-not $runningFromTemp) {
    Write-WarnLine 'Plugin directory kept (script was not staged in TEMP)'
} else {
    for ($i = 0; $i -lt 5; $i++) {
        Remove-Item -LiteralPath $Root -Recurse -Force -ErrorAction SilentlyContinue
        if (-not (Test-Path -LiteralPath $Root)) { break }
        Start-Sleep -Milliseconds 700
    }
    if (Test-Path -LiteralPath $Root) {
        Write-WarnLine ("Plugin directory could not be fully removed: " + $Root)
        Write-WarnLine 'Delete it manually after closing programs that use it.'
    } else {
        Write-Ok ("Plugin directory removed: " + $Root)
    }
}

Write-Host ''
if (Test-Path -LiteralPath $Root) {
    Write-Host 'Uninstall finished with warnings (see lines marked with !).' -ForegroundColor Yellow
} else {
    Write-Host 'Uninstall finished. The usage panel will not start again.' -ForegroundColor Green
}
Write-Host 'Restart Kimi Code to unload the sidebar widget from this session.'
Write-Host ''
Remove-Item -LiteralPath $tempCopy -Force -ErrorAction SilentlyContinue
Read-Host 'Press Enter to close this window'

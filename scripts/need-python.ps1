# Injected when bootstrap.cmd cannot find any Python interpreter.
# Finds desktop-dist the same way service.py does (desktop_path.txt -> fixed
# paths -> running Kimi Code.exe process scan), then injects a tiny placeholder
# widget so the sidebar shows "install Python" instead of staying blank.
$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $PSScriptRoot          # plugin root
$cfg  = Join-Path $PSScriptRoot 'desktop_path.txt'
$marker = Join-Path $env:USERPROFILE '.kimi-code\usage-dashboard\plugin-uninstall.json'
try { if (Test-Path $marker) { exit 0 } } catch { exit 0 }

function Find-Dist {
    if (Test-Path $cfg) {
        $p = (Get-Content $cfg -Raw).Trim().Trim('"').Trim("'")
        if ($p -and (Test-Path $p)) { return $p }
    }
    $cand = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Kimi Code\resources\desktop-dist'),
        'C:\Program Files\Kimi Code\resources\desktop-dist',
        'C:\Program Files (x86)\Kimi Code\resources\desktop-dist'
    )
    foreach ($c in $cand) { if (Test-Path $c) { return $c } }
    $exe = Get-Process -Name 'Kimi Code' | Select-Object -First 1 -ExpandProperty Path
    if ($exe) {
        $c = Join-Path (Split-Path $exe) 'resources\desktop-dist'
        if (Test-Path $c) { return $c }
    }
    return $null
}

$dist = Find-Dist
if (-not $dist) { exit 0 }

$assets = Join-Path $dist 'assets'
$index  = Join-Path $dist 'index.html'
New-Item -ItemType Directory -Force $assets | Out-Null

# Placeholder widget: minimal sidebar card telling the user to install Python.
$placeholder = @'
(function () {
  if (window.__KIMI_USAGE_NEEDPY__) return; window.__KIMI_USAGE_NEEDPY__ = true;
  function mount() {
    if (document.getElementById('kimi-usage-needpy')) return;
    var footer = document.querySelector('.side-footer') || document.querySelector('[class*="side-footer"]');
    if (!footer || !footer.parentNode) return;
    var d = document.createElement('div');
    d.id = 'kimi-usage-needpy';
    d.style.cssText = 'margin:8px;padding:12px;border-radius:10px;font:12px/1.5 system-ui;'
      + 'background:color-mix(in srgb,#d29922 12%,transparent);border:1px solid color-mix(in srgb,#d29922 35%,transparent);'
      + 'color:var(--color-text,#1f2329)';
    d.innerHTML = '<b>Kimi 用量面板</b><br>需要 Python ≥3.8 才能运行。<br>'
      + '安装后重启 Kimi Code，或到插件目录运行 scripts\\bootstrap.cmd。'
      + '<br><a href="https://www.python.org/downloads/" target="_blank" '
      + 'style="color:var(--color-accent,#1a88ff)">下载 Python</a>';
    footer.parentNode.insertBefore(d, footer);
  }
  new MutationObserver(mount).observe(document.body, { childList: true, subtree: true });
  mount();
})();
'@
Set-Content -Path (Join-Path $assets 'kimi-usage-needpy.js') -Value $placeholder -Encoding UTF8

# Inject placeholder tag into index.html once (before </body>)
if (Test-Path $index) {
    $html = Get-Content $index -Raw -Encoding UTF8
    $tag  = '<script src="/assets/kimi-usage-needpy.js"></script>'
    if ($html -notmatch 'kimi-usage-needpy') {
        $html = $html -replace '</body>', ("  " + $tag + "`r`n  </body>")
        Set-Content $index -Value $html -Encoding UTF8 -NoNewline
    }
}

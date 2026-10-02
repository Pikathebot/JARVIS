<#
.SYNOPSIS
    Screenshots the running Jarvis windows, liquid glass included.

.DESCRIPTION
    Jarvis's windows are excluded from screen capture: the glass is built from a live capture
    of the desktop, and a capturable window would capture itself and never settle. This asks
    the app (SnapshotService) to freeze its glass and animations, lift the exclusion, and
    report where its windows are; copies those pixels off the screen; then lets it resume.
    The app re-excludes itself after 10 s on its own if this script dies mid-way.

    The main window is only captured while it is the foreground window: this copies screen
    pixels, and a covered window would yield whatever covers it (someone else's content). Note
    the glass itself shows the desktop behind the window, blurred -- that is the app's look.

    Frozen means frozen: a transition caught mid-flight is held (XAML and glass together) for
    the length of the capture and then carries on.

.PARAMETER OutDir
    Where the PNGs go (one per visible window: main.png, hud.png). Default: a temp folder.

.PARAMETER Window
    Which window(s) to keep: main, hud, settings, or all (default main).

.PARAMETER DelayMs
    Wait this long before requesting, e.g. to catch the startup sequence part-way.

.EXAMPLE
    .\scripts\snapshot-window.ps1 -OutDir $env:TEMP\jarvis-shots
#>
param(
    [string]$OutDir = (Join-Path $env:TEMP "jarvis-snapshots"),
    [ValidateSet("main", "hud", "settings", "all")][string]$Window = "main",
    [int]$DelayMs = 0,
    [string]$Configuration = "Debug"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$appx = Join-Path $repo "desktop-winui\Jarvis.App\bin\$Configuration\net10.0-windows10.0.26100.0\win-x64\AppX"
$request = Join-Path $appx "snapshot.request"
$ready = Join-Path $appx "snapshot.ready"

if (-not (Get-Process -Name "Jarvis.App" -ErrorAction SilentlyContinue)) { throw "Jarvis.App is not running." }
New-Item -ItemType Directory -Force $OutDir | Out-Null

# Physical pixels: the app reports its rects DPI-aware, and a DPI-unaware copy would be scaled.
Add-Type -Namespace Snap -Name Native -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();'
[Snap.Native]::SetProcessDPIAware() | Out-Null
Add-Type -AssemblyName System.Drawing

if ($DelayMs -gt 0) { Start-Sleep -Milliseconds $DelayMs }
Set-Content -Path $request -Value (Get-Date -Format o)
try {
    $deadline = (Get-Date).AddSeconds(5)
    while (-not (Test-Path $ready)) {
        if ((Get-Date) -gt $deadline) { throw "The app did not answer the snapshot request (is this build older than SnapshotService?)." }
        Start-Sleep -Milliseconds 50
    }
    Start-Sleep -Milliseconds 50 # the file is written in one call, but don't race its close
    $saved = @()
    foreach ($line in Get-Content $ready) {
        # "# main skipped: not the foreground window" -- the app refuses to expose a covered
        # window, since the screen copy would then contain whatever covers it.
        if ($line.StartsWith('#')) { Write-Warning $line.TrimStart('#', ' '); continue }
        $name, $x, $y, $w, $h = $line -split ' '
        if ($Window -ne "all" -and $name -ne $Window) { continue }
        $bmp = New-Object System.Drawing.Bitmap ([int]$w), ([int]$h)
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.CopyFromScreen([int]$x, [int]$y, 0, 0, $bmp.Size)
        $g.Dispose()
        $path = Join-Path $OutDir "$name.png"
        $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
        $bmp.Dispose()
        $saved += $path
    }
}
finally {
    Remove-Item $request -ErrorAction SilentlyContinue
}
$saved

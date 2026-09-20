<#
.SYNOPSIS
    Starts the JARVIS WinUI desktop app.

.DESCRIPTION
    The double-click entry point for the app. Everything the app needs beyond this
    script it starts itself: App.xaml.cs walks up from its own directory to find the
    repo root, and BackendHost spawns uvicorn inside a Job Object, so there is no
    separate backend window to babysit and nothing is left running when the app exits.

    What this script has to do, in order:
      1. Stop a stale Jarvis.App -- a running one holds a file lock on its own exe,
         which is what makes a rebuild fail with MSB3027.
      2. Build, but only when there is nothing to launch (or -Build was passed).
      3. Register the loose build output as a development MSIX package. WinUI 3 here
         is packaged, so the app needs package identity to start at all, and the
         microphone capability in the manifest only takes effect through registration.
      4. Launch by AUMID, which is how Windows starts a packaged app -- running the
         exe directly starts it without identity.

.PARAMETER Build
    Rebuild before launching, even if the app is already built.

.PARAMETER Configuration
    Debug (default) or Release.
#>
[CmdletBinding()]
param(
    [switch]$Build,
    [ValidateSet('Debug', 'Release')]
    [string]$Configuration = 'Debug'
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$logPath  = Join-Path $repoRoot 'launcher-winui.log'

function Write-Step {
    param([string]$Message, [string]$Color = 'Cyan')
    Write-Host "  $Message" -ForegroundColor $Color
    "[{0:yyyy-MM-dd HH:mm:ss}] $Message" -f (Get-Date) | Add-Content -Path $logPath
}

function Fail {
    param([string]$Message)
    Write-Host ''
    Write-Host "  $Message" -ForegroundColor Red
    "[{0:yyyy-MM-dd HH:mm:ss}] FAILED: $Message" -f (Get-Date) | Add-Content -Path $logPath
    exit 1
}

Write-Host ''
Write-Host '  JARVIS' -ForegroundColor White
Write-Host '  ------' -ForegroundColor DarkGray

# --- 1. Clear a stale instance ------------------------------------------------
$running = Get-Process -Name 'Jarvis.App' -ErrorAction SilentlyContinue
if ($running) {
    Write-Step 'Closing the running instance...'
    $running | Stop-Process -Force
    # The file lock outlives the process by a moment; a rebuild started too soon
    # still fails.
    Start-Sleep -Milliseconds 700
}

# --- 2. Build if needed -------------------------------------------------------
$appDir   = Join-Path $repoRoot "desktop-winui\Jarvis.App\bin\$Configuration\net10.0-windows10.0.26100.0\win-x64\AppX"
$manifest = Join-Path $appDir 'AppxManifest.xml'

# "Needed" means the registered layout is older than the code: a source file edited since the
# layout was made, or a `dotnet build` run by hand (which refreshes bin\...\win-x64 but not the
# AppX layout beside it). Without this check a launch after either silently ran the previous
# build -- a whole round of testing was done against stale binaries that way.
$layoutDll = Join-Path $appDir 'Jarvis.App.dll'
$reason = $null
if ($Build) { $reason = 'Rebuilding' }
elseif (-not (Test-Path $manifest)) { $reason = 'Not built yet -- building' }
elseif (Test-Path $layoutDll) {
    $layoutTime = (Get-Item $layoutDll).LastWriteTimeUtc
    $srcDir = Join-Path $repoRoot 'desktop-winui'
    $newer = Get-ChildItem $srcDir -Recurse -File -Include *.cs,*.xaml,*.csproj,*.slnx,*.hlsl,*.appxmanifest,*.txt,*.dll |
        Where-Object { $_.FullName -notlike '*\obj\*' -and $_.FullName -notlike '*\AppX\*' -and $_.LastWriteTimeUtc -gt $layoutTime } |
        Select-Object -First 1
    if ($newer) { $reason = "Newer than the last layout ($($newer.Name)) -- rebuilding" }
}

if ($reason) {
    Write-Step "$reason (this takes a minute the first time)..."

    if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
        Fail 'The .NET SDK is not installed, so the app cannot be built. Install .NET 10, then run this again.'
    }

    $solution = Join-Path $repoRoot 'desktop-winui\Jarvis.slnx'
    & dotnet build $solution -c $Configuration --nologo -v quiet 2>&1 | Tee-Object -FilePath $logPath -Append | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Fail "The build failed. The compiler output is in $logPath."
    }

    # A plain build refreshes bin\...\win-x64 but leaves the AppX layout next to it as it was,
    # so a launch would run the previous build. The winapp CLI behind `dotnet run` is what
    # lays out and registers the package; invoking its target without launching does the same.
    $appProject = Join-Path $repoRoot 'desktop-winui\Jarvis.App\Jarvis.App.csproj'
    & dotnet msbuild $appProject -t:RunPackagedApp -p:Configuration=$Configuration -p:WinAppRunNoLaunch=true -nologo -v quiet 2>&1 | Tee-Object -FilePath $logPath -Append | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Fail "The build succeeded but the app package could not be laid out. The output is in $logPath."
    }
    if (-not (Test-Path $manifest)) {
        Fail "The build reported success but produced no app package at $appDir."
    }
}

# --- 3. Register the development package --------------------------------------
# Identity/@Name from Package.appxmanifest. Read rather than hardcoded so a change
# to the manifest does not silently launch a stale registration.
$identityName = ([xml](Get-Content $manifest)).Package.Identity.Name
$package = Get-AppxPackage -Name $identityName -ErrorAction SilentlyContinue

# Re-register when the registration is missing, or points somewhere other than this
# build output (e.g. after switching between Debug and Release).
if (-not $package -or $package.InstallLocation -ne $appDir) {
    Write-Step 'Registering the app with Windows...'
    try {
        Add-AppxPackage -Register $manifest -ErrorAction Stop
    }
    catch {
        $detail = $_.Exception.Message
        if ($detail -match '0x80073CFF|developer mode') {
            Fail "Windows refused to register the app because Developer Mode is off. Turn it on in Settings > System > For developers, then run this again."
        }
        Fail "Windows could not register the app: $detail"
    }
    $package = Get-AppxPackage -Name $identityName -ErrorAction SilentlyContinue
}

if (-not $package) {
    Fail 'The app registered without error but Windows does not list it. Check launcher-winui.log.'
}

# --- 4. Warn about a missing backend environment ------------------------------
# Not fatal: the app starts and shows its own "backend offline" state, which is a
# better place to see this than a launcher that refuses to open.
if (-not (Test-Path (Join-Path $repoRoot '.venv\Scripts\python.exe'))) {
    Write-Step 'Note: .venv is missing, so the backend will not start and chat will be offline.' 'Yellow'
}

# --- 5. Launch ----------------------------------------------------------------
# Application Id="App" in the manifest; AUMID is "<family name>!<app id>". Going
# through explorer.exe is what gives the process package identity.
$aumid = "$($package.PackageFamilyName)!App"
Write-Step 'Starting...'
Start-Process 'explorer.exe' -ArgumentList "shell:AppsFolder\$aumid"

Write-Host ''
Write-Host '  Jarvis is starting. The backend takes a few seconds to come up.' -ForegroundColor Green
Write-Host ''

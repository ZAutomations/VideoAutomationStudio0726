<#
    create_desktop_shortcut.ps1
    ----------------------------
    Creates a "Video Automation Studio" shortcut (with the colourful app icon)
    on the Desktop and in the Start Menu, pointing at run.bat.

    Called automatically at the end of setup, and can be re-run any time via
    setup\create_desktop_shortcut.bat.
#>

$ErrorActionPreference = 'Stop'

# --- Resolve paths (this script lives in <repo>\setup) --------------------
$SetupDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoDir  = Split-Path -Parent $SetupDir
$Target   = Join-Path $RepoDir 'run.bat'
$IconPath = Join-Path $RepoDir 'assets\app_icon.ico'
$Name     = 'Video Automation Studio'

if (-not (Test-Path $Target)) {
    Write-Host "  [ERROR] run.bat not found at $Target" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $IconPath)) {
    # Icon missing (e.g. fresh clone before make_icon ran) — try to build it.
    $py = Join-Path $RepoDir '.venv\Scripts\python.exe'
    $mk = Join-Path $SetupDir 'make_icon.py'
    if ((Test-Path $py) -and (Test-Path $mk)) {
        try { & $py $mk | Out-Null } catch {}
    }
}

function New-AppShortcut([string]$LinkPath) {
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($LinkPath)
    $sc.TargetPath       = $Target
    $sc.WorkingDirectory = $RepoDir
    $sc.Description       = 'Video Automation Studio - Professional Edition'
    $sc.WindowStyle       = 1            # 1 = normal window
    if (Test-Path $IconPath) { $sc.IconLocation = "$IconPath,0" }
    $sc.Save()
    # Mark the .lnk to run as administrator? No — normal user is fine.
    Write-Host "  [OK] $LinkPath" -ForegroundColor Green
}

# --- Desktop (handles OneDrive-redirected Desktop automatically) ----------
$Desktop = [Environment]::GetFolderPath('Desktop')
New-AppShortcut (Join-Path $Desktop "$Name.lnk")

# --- Start Menu (so it shows up in Start-menu search) ---------------------
$Programs = [Environment]::GetFolderPath('Programs')
if ($Programs -and (Test-Path $Programs)) {
    New-AppShortcut (Join-Path $Programs "$Name.lnk")
}

Write-Host ""
Write-Host "  Desktop icon created. Look for '$Name' on your Desktop." -ForegroundColor Cyan

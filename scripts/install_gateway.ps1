# Install Kara gateway as a Windows Scheduled Task (runs at logon, fully hidden).
param(
    [Parameter(Mandatory = $true)]
    [string]$PackageDir
)

$ErrorActionPreference = "Stop"
$TaskName = "KaraGateway"
$Python = Join-Path $PackageDir ".venv\Scripts\python.exe"
$RunGateway = Join-Path $PackageDir "scripts\run_gateway.py"
$LauncherVbs = Join-Path $PackageDir "scripts\launch_gateway.vbs"
$Wscript = Join-Path $env:SystemRoot "System32\wscript.exe"
$StopScript = Join-Path $PackageDir "scripts\stop_gateway.ps1"

if (-not (Test-Path $Python)) {
    Write-Error "python not found at $Python - run 'uv sync' in $PackageDir first."
}
if (-not (Test-Path $RunGateway)) {
    Write-Error "run_gateway.py not found at $RunGateway"
}

# Stop any existing gateway so we do not leave an old console-attached process running.
if (Test-Path $StopScript) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File $StopScript
}

# The launcher is a checked-in script that resolves its own paths, so there is
# nothing to generate here -- and nothing to re-run after moving the repo.
if (-not (Test-Path $LauncherVbs)) {
    Write-Error "launch_gateway.vbs not found at $LauncherVbs"
}

# wscript //B = batch mode (no script errors dialog). Never run python.exe directly here.
$Action = New-ScheduledTaskAction `
    -Execute $Wscript `
    -Argument "//B `"$LauncherVbs`"" `
    -WorkingDirectory $PackageDir

$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description 'Kara personal AI gateway - Telegram 24/7' `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName'"
Write-Host '  Fully hidden at logon (wscript + pythonw, no console window)'
Write-Host "  Start now: schtasks /Run /TN $TaskName"

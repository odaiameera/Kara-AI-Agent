# Remove Kara gateway Windows Scheduled Task.
$ErrorActionPreference = "Stop"
$TaskName = "KaraGateway"

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "Removed scheduled task '$TaskName' (if it existed)."

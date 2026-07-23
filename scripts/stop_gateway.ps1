# Stop any running Kara gateway process.
$ErrorActionPreference = "SilentlyContinue"
$PackageDir = Split-Path $PSScriptRoot -Parent
$PidFile = Join-Path $PackageDir "brain\gateway.pid"

if (Test-Path $PidFile) {
    $procId = (Get-Content $PidFile -Raw).Trim()
    if ($procId -match '^\d+$') {
        Stop-Process -Id ([int]$procId) -Force -ErrorAction SilentlyContinue
        Write-Host "Stopped gateway pid $procId"
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

Get-CimInstance Win32_Process |
    Where-Object {
        ($_.Name -eq 'pythonw.exe' -or $_.Name -eq 'python.exe' -or $_.Name -eq 'wscript.exe') -and
        ($_.CommandLine -like '*-m gateway.run*' -or $_.CommandLine -like '*run_gateway.py*' -or $_.CommandLine -like '*\gateway.py*' -or $_.CommandLine -like '*from gateway.run*')
    } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "Stopped gateway pid $($_.ProcessId)"
    }

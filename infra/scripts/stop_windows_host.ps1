# Stops the RemoteDev Windows Host Executor Daemon
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$InfraDir = Split-Path -Parent $ScriptDir
$PidFile = Join-Path $InfraDir ".remotedev_host.pid"

Write-Host "Stopping RemoteDev Windows Host Executor..." -ForegroundColor Cyan

if (Test-Path $PidFile) {
    $TargetPid = Get-Content $PidFile
    try {
        Stop-Process -Id $TargetPid -Force -ErrorAction Stop
        Write-Host "Terminated process PID $TargetPid." -ForegroundColor Green
    } catch {
        Write-Host "Process PID $TargetPid was not running." -ForegroundColor Gray
    }
    Remove-Item -Path $PidFile -Force -ErrorAction SilentlyContinue
} else {
    Write-Host "No PID file found. Checking for any running bridge.service processes..." -ForegroundColor Gray
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*bridge.service*" } | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force
        Write-Host "Terminated lingering process PID $($_.ProcessId)" -ForegroundColor Green
    }
}

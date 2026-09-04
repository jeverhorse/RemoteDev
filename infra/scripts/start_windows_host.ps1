# Starts the RemoteDev Windows Host Executor Daemon
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$InfraDir = Split-Path -Parent $ScriptDir
$RootDir = Split-Path -Parent $InfraDir
$PidFile = Join-Path $InfraDir ".remotedev_host.pid"
$LogFile = Join-Path $InfraDir "host_executor.log"

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " Starting RemoteDev Windows Host Executor Daemon" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

# Check if already running
if (Test-Path $PidFile) {
    $OldPid = Get-Content $PidFile
    $Process = Get-Process -Id $OldPid -ErrorAction SilentlyContinue
    if ($Process) {
        Write-Host "Host Executor is already running with PID $OldPid." -ForegroundColor Yellow
        exit 0
    }
}

$PythonBin = (Get-Command python).Source
$PythonwBin = Join-Path (Split-Path $PythonBin) "pythonw.exe"
if (-not (Test-Path $PythonwBin)) {
    $PythonwBin = $PythonBin
}
$ConfigPath = Join-Path $InfraDir "config\config.yaml"

# Start detached background process
$Process = Start-Process -FilePath $PythonwBin `
    -ArgumentList "-u", "-m", "bridge.service", "--config", "$ConfigPath" `
    -WorkingDirectory $InfraDir `
    -RedirectStandardOutput $LogFile `
    -RedirectStandardError (Join-Path $InfraDir "host_executor_err.log") `
    -PassThru -WindowStyle Hidden

$Process.Id | Out-File -FilePath $PidFile -Encoding ascii

Write-Host "Waiting for service to initialize on http://127.0.0.1:8765/health..."
$Healthy = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $resp = Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -TimeoutSec 1 -ErrorAction Stop
        if ($resp.status -eq "ok") {
            $Healthy = $true
            break
        }
    } catch {
        # continue waiting
    }
}

if ($Healthy) {
    Write-Host "Windows Host Executor is RUNNING! (PID: $($Process.Id))" -ForegroundColor Green
    Write-Host "Logs: $LogFile" -ForegroundColor Gray
} else {
    Write-Host "Warning: Service started with PID $($Process.Id) but health check didn't respond yet." -ForegroundColor Yellow
    Write-Host "Check logs at: $LogFile" -ForegroundColor Yellow
}

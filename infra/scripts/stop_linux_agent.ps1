# Stops and removes the Linux Agent Docker container
Write-Host "Stopping and removing 'remotedev-linux-agent' container..." -ForegroundColor Cyan
docker rm -f remotedev-linux-agent 2>$null
Write-Host "Linux Agent container removed." -ForegroundColor Green

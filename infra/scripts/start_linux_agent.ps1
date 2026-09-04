# Builds and runs the Linux Agent Docker container
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$InfraDir = Split-Path -Parent $ScriptDir
$RootDir = Split-Path -Parent $InfraDir

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " Building & Starting Linux Agent Container" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

# Stop existing container if running
docker rm -f remotedev-linux-agent 2>$null

Write-Host "Building Docker image 'remotedev-agent:latest'..." -ForegroundColor Yellow
docker build -t remotedev-agent:latest -f "$InfraDir\docker\Dockerfile" "$RootDir"

if ($LASTEXITCODE -ne 0) {
    Write-Host "Docker build failed!" -ForegroundColor Red
    exit 1
}

Write-Host "Starting container 'remotedev-linux-agent'..." -ForegroundColor Yellow
docker run -d `
    --name remotedev-linux-agent `
    --add-host=host.docker.internal:host-gateway `
    -e WINDOWS_HOST=host.docker.internal `
    -e LINUX_WORKSPACE=/workspace/project `
    -e REMOTEDEV_CONFIG=/infra/config/config.yaml `
    remotedev-agent:latest `
    tail -f /dev/null

if ($LASTEXITCODE -eq 0) {
    Write-Host "Linux Agent container is RUNNING!" -ForegroundColor Green
    Write-Host "To open an interactive Linux shell inside the container, run:" -ForegroundColor Cyan
    Write-Host "   docker exec -it remotedev-linux-agent bash" -ForegroundColor White
} else {
    Write-Host "Failed to start Docker container!" -ForegroundColor Red
}

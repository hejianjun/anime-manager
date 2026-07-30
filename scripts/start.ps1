$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$backendRoot = Join-Path $projectRoot "backend"
$frontendRoot = Join-Path $projectRoot "frontend"
$pythonExe = Join-Path $backendRoot ".venv\Scripts\python.exe"
$viteEntry = Join-Path $frontendRoot "node_modules\vite\bin\vite.js"
$runtimeRoot = Join-Path $projectRoot ".runtime"
$backendPort = 18010
$frontendPort = 5173

if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Dependencies are not installed. Run .\scripts\setup.ps1 first."
}
if (-not (Test-Path -LiteralPath $viteEntry)) {
    throw "Frontend dependencies are not installed. Run .\scripts\setup.ps1 first."
}
$nodeExe = (Get-Command node.exe -ErrorAction Stop).Source
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null

foreach ($name in @("backend", "frontend")) {
    $pidPath = Join-Path $runtimeRoot "$name.pid"
    if (Test-Path -LiteralPath $pidPath) {
        $savedProcessId = [int](Get-Content -LiteralPath $pidPath -Raw)
        if (Get-Process -Id $savedProcessId -ErrorAction SilentlyContinue) {
            throw "$name is already running (PID $savedProcessId). Run .\scripts\stop.ps1 first."
        }
        Remove-Item -LiteralPath $pidPath -Force
    }
}

foreach ($port in @($backendPort, $frontendPort)) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
        throw "Port $port is already in use. Refusing to start a duplicate service."
    }
}

$backendLog = Join-Path $runtimeRoot "backend.log"
$backendErrorLog = Join-Path $runtimeRoot "backend-error.log"
$frontendLog = Join-Path $runtimeRoot "frontend.log"
$frontendErrorLog = Join-Path $runtimeRoot "frontend-error.log"

try {
    $backendProcess = Start-Process -FilePath $pythonExe `
        -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$backendPort" `
        -WorkingDirectory $backendRoot -WindowStyle Hidden -RedirectStandardOutput $backendLog -RedirectStandardError $backendErrorLog -PassThru
    $frontendProcess = Start-Process -FilePath $nodeExe `
        -ArgumentList "`"$viteEntry`"", "--host", "127.0.0.1", "--port", "$frontendPort", "--strictPort" `
        -WorkingDirectory $frontendRoot -WindowStyle Hidden `
        -RedirectStandardOutput $frontendLog -RedirectStandardError $frontendErrorLog -PassThru

    Set-Content -LiteralPath (Join-Path $runtimeRoot "backend.pid") -Value $backendProcess.Id
    Set-Content -LiteralPath (Join-Path $runtimeRoot "frontend.pid") -Value $frontendProcess.Id

    $deadline = (Get-Date).AddSeconds(15)
    do {
        if ($backendProcess.HasExited -or $frontendProcess.HasExited) {
            throw "A service process exited during startup. Check the logs in $runtimeRoot."
        }
        $backendReady = [bool](Get-NetTCPConnection -State Listen -LocalPort $backendPort -ErrorAction SilentlyContinue)
        $frontendReady = [bool](Get-NetTCPConnection -State Listen -LocalPort $frontendPort -ErrorAction SilentlyContinue)
        if ($backendReady -and $frontendReady) {
            break
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)

    if (-not ($backendReady -and $frontendReady)) {
        throw "Services did not become ready within 15 seconds. Check the logs in $runtimeRoot."
    }
}
catch {
    & (Join-Path $PSScriptRoot "stop.ps1") | Out-Null
    throw
}

Write-Host "Anime Manager started: http://127.0.0.1:$frontendPort"
Write-Host "Backend: http://127.0.0.1:$backendPort"
Write-Host "Logs: $runtimeRoot"

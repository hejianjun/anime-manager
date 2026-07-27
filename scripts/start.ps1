$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$backendRoot = Join-Path $projectRoot "backend"
$frontendRoot = Join-Path $projectRoot "frontend"
$pythonExe = Join-Path $backendRoot ".venv\Scripts\python.exe"
$runtimeRoot = Join-Path $projectRoot ".runtime"

if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Dependencies are not installed. Run .\scripts\setup.ps1 first."
}
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null

$backendLog = Join-Path $runtimeRoot "backend.log"
$backendErrorLog = Join-Path $runtimeRoot "backend-error.log"
$frontendLog = Join-Path $runtimeRoot "frontend.log"
$frontendErrorLog = Join-Path $runtimeRoot "frontend-error.log"
$backendProcess = Start-Process -FilePath $pythonExe `
    -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "10910" `
    -WorkingDirectory $backendRoot -WindowStyle Hidden -RedirectStandardOutput $backendLog -RedirectStandardError $backendErrorLog -PassThru
$frontendProcess = Start-Process -FilePath "npm.cmd" `
    -ArgumentList "run", "dev" -WorkingDirectory $frontendRoot -WindowStyle Hidden `
    -RedirectStandardOutput $frontendLog -RedirectStandardError $frontendErrorLog -PassThru

Set-Content -LiteralPath (Join-Path $runtimeRoot "backend.pid") -Value $backendProcess.Id
Set-Content -LiteralPath (Join-Path $runtimeRoot "frontend.pid") -Value $frontendProcess.Id
Write-Host "Anime Manager started: http://127.0.0.1:5173"
Write-Host "Logs: $runtimeRoot"

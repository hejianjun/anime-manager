$ErrorActionPreference = "Continue"
$projectRoot = Split-Path -Parent $PSScriptRoot
$backendRoot = Join-Path $projectRoot "backend"
$frontendRoot = Join-Path $projectRoot "frontend"
$pythonExe = Join-Path $backendRoot ".venv\Scripts\python.exe"
$viteEntry = Join-Path $frontendRoot "node_modules\vite\bin\vite.js"
$runtimeRoot = Join-Path $projectRoot ".runtime"

function Stop-ProcessTree {
    param([int]$RootProcessId)

    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $RootProcessId" -ErrorAction SilentlyContinue
    foreach ($child in $children) {
        Stop-ProcessTree -RootProcessId $child.ProcessId
    }
    Stop-Process -Id $RootProcessId -ErrorAction SilentlyContinue
}

function Test-ProjectProcess {
    param(
        [Microsoft.Management.Infrastructure.CimInstance]$Process,
        [string]$Role
    )

    if (-not $Process) {
        return $false
    }
    if ($Role -eq "backend") {
        return $Process.ExecutablePath -eq $pythonExe `
            -and $Process.CommandLine -like "*uvicorn*app.main:app*"
    }
    return $Process.Name -eq "node.exe" `
        -and $Process.CommandLine -like "*$viteEntry*"
}

$stoppedIds = [System.Collections.Generic.HashSet[int]]::new()
foreach ($name in @("backend", "frontend")) {
    $pidPath = Join-Path $runtimeRoot "$name.pid"
    if (Test-Path -LiteralPath $pidPath) {
        $savedProcessId = [int](Get-Content -LiteralPath $pidPath -Raw)
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $savedProcessId" -ErrorAction SilentlyContinue
        if (Test-ProjectProcess -Process $process -Role $name) {
            Stop-ProcessTree -RootProcessId $savedProcessId
            $stoppedIds.Add($savedProcessId) | Out-Null
            Write-Host "Stopped $name ($savedProcessId)"
        }
        elseif ($process) {
            Write-Warning "Ignored stale $name PID $savedProcessId because it belongs to another process."
        }
        Remove-Item -LiteralPath $pidPath -Force
    }

    $fallbackProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { Test-ProjectProcess -Process $_ -Role $name }
    foreach ($process in $fallbackProcesses) {
        if ($stoppedIds.Add($process.ProcessId)) {
            Stop-ProcessTree -RootProcessId $process.ProcessId
            Write-Host "Stopped untracked $name process ($($process.ProcessId))"
        }
    }
}

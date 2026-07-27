$ErrorActionPreference = "Continue"
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $projectRoot ".runtime"

function Stop-ProcessTree {
    param([int]$RootProcessId)

    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $RootProcessId" -ErrorAction SilentlyContinue
    foreach ($child in $children) {
        Stop-ProcessTree -RootProcessId $child.ProcessId
    }
    Stop-Process -Id $RootProcessId -ErrorAction SilentlyContinue
}

foreach ($name in @("backend", "frontend")) {
    $pidPath = Join-Path $runtimeRoot "$name.pid"
    if (Test-Path -LiteralPath $pidPath) {
        $processId = [int](Get-Content -LiteralPath $pidPath -Raw)
        $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($process) {
            Stop-ProcessTree -RootProcessId $processId
            Write-Host "Stopped $name ($processId)"
        }
        Remove-Item -LiteralPath $pidPath -Force
    }
}

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$backendRoot = Join-Path $projectRoot "backend"
$frontendRoot = Join-Path $projectRoot "frontend"
$venvRoot = Join-Path $backendRoot ".venv"

if (-not (Test-Path -LiteralPath $venvRoot)) {
    python -m venv $venvRoot
}

$pythonExe = Join-Path $venvRoot "Scripts\python.exe"
& $pythonExe -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip." }
& $pythonExe -m pip install -e "$backendRoot[dev]"
if ($LASTEXITCODE -ne 0) { throw "Failed to install backend dependencies." }

Push-Location $backendRoot
try {
    & (Join-Path $venvRoot "Scripts\alembic.exe") upgrade head
    if ($LASTEXITCODE -ne 0) { throw "Database migration failed." }
}
finally {
    Pop-Location
}

Push-Location $frontendRoot
try {
    npm install
    if ($LASTEXITCODE -ne 0) { throw "Failed to install frontend dependencies." }
}
finally {
    Pop-Location
}

Write-Host "Setup complete."

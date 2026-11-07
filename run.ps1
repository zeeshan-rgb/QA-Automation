<#
  Polarion ALM Validation Studio - developer launcher.
  The venv, pip cache and app data live on D: (C: is nearly full on this machine).
    .\run.ps1          start the desktop app
    .\run.ps1 -Test    run the test suite
#>
param([switch]$Test)

$ErrorActionPreference = 'Stop'
$Root = 'D:\PolarionValidationStudio'
$Venv = Join-Path $Root 'venv'
$Py = Join-Path $Venv 'Scripts\python.exe'
$env:PIP_CACHE_DIR = Join-Path $Root 'pip-cache'
$env:PAVS_DATA_DIR = Join-Path $Root 'data'

if (-not (Test-Path $Py)) {
    New-Item -ItemType Directory -Force $Root | Out-Null
    python -m venv $Venv
    & $Py -m pip install --upgrade pip
}
& $Py -c "import pavs" 2>$null
if ($LASTEXITCODE -ne 0) { & $Py -m pip install -e "$PSScriptRoot[dev]" }

Push-Location $PSScriptRoot
try {
    if ($Test) { & $Py -m pytest } else { & $Py -m pavs }
} finally { Pop-Location }

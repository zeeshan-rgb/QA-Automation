<#
Build the Windows executables with PyInstaller (one folder: PolarionValidationStudio.exe + pavs-cli.exe).
  .\tools\build_exe.ps1                      # output in .\dist
  .\tools\build_exe.ps1 -OutDir D:\PavsBuild # keep the build off a full C: drive
  .\tools\build_exe.ps1 -BundlePlaywright    # also bundle Playwright so the exe can run browser checks (adds ~100 MB)
#>
param(
    [string]$Python = "python",
    [string]$OutDir = "",
    [switch]$BundlePlaywright
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $OutDir) { $OutDir = Join-Path $root "dist" }
$work = Join-Path $OutDir "work"
if ($BundlePlaywright) { $env:PAVS_BUNDLE_PLAYWRIGHT = "1" } else { $env:PAVS_BUNDLE_PLAYWRIGHT = "0" }
& $Python -m PyInstaller --noconfirm --clean --distpath (Join-Path $OutDir "dist") --workpath $work (Join-Path $root "packaging\pavs.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Write-Host "Built: $(Join-Path $OutDir 'dist\PolarionValidationStudio\PolarionValidationStudio.exe')"

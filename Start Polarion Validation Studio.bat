@echo off
rem Double-click to start Polarion ALM Validation Studio.
rem The Python environment and app data live on D: (C: is nearly full on this machine).
setlocal
set "ROOT=D:\PolarionValidationStudio"
set "PY=%ROOT%\venv\Scripts\pythonw.exe"
set "PAVS_DATA_DIR=%ROOT%\data"
set "PYTHONPATH=%~dp0src;%~dp0"
if not exist "%PY%" (
  echo The Python environment was not found at %ROOT%\venv.
  echo Run run.ps1 once to create it.
  pause
  exit /b 1
)
start "" "%PY%" -m pavs
endlocal

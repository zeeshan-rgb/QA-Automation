@echo off
rem QA launcher for Polarion ALM Validation Studio.   qa help   shows the commands.
rem Python environment and data live on D: (C: is nearly full); override with PAVS_HOME.
setlocal
set "ROOT=%~dp0.."
if "%PAVS_HOME%"=="" set "PAVS_HOME=D:\PolarionValidationStudio"
set "PY=%PAVS_HOME%\venv\Scripts\python.exe"
set "PYW=%PAVS_HOME%\venv\Scripts\pythonw.exe"
if "%PAVS_DATA_DIR%"=="" set "PAVS_DATA_DIR=%PAVS_HOME%\data"
set "PYTHONPATH=%ROOT%\src;%ROOT%"
if not exist "%PY%" (
  echo The Python environment was not found at %PAVS_HOME%\venv. Run run.ps1 once to create it.
  exit /b 1
)
set "CMD=%~1"
if "%CMD%"=="" set "CMD=start"
if /i "%CMD%"=="start" (start "" "%PYW%" -m pavs & exit /b 0)
if /i "%CMD%"=="test"  (pushd "%ROOT%" & "%PY%" -m pytest tests -q %2 %3 %4 & popd & exit /b 0)
if /i "%CMD%"=="mock"  (
  set "PROFILE=%~2" & set "PORT=%~3"
  if "%PROFILE%"=="" set "PROFILE=2606"
  if "%PORT%"=="" set "PORT=8810"
  pushd "%ROOT%" & "%PY%" -m mock_polarion.serve %PROFILE% --port %PORT% & popd & exit /b 0)
if /i "%CMD%"=="cli"   (shift & "%PY%" -m pavs.cli %1 %2 %3 %4 %5 %6 %7 %8 %9 & exit /b %ERRORLEVEL%)
if /i "%CMD%"=="exe"   (start "" "D:\PavsBuild\dist\PolarionValidationStudio\PolarionValidationStudio.exe" & exit /b 0)
echo.
echo   qa start              open the app (default)
echo   qa mock [profile] [port]   start the fake Polarion (default 2606, port 8810; admin / admin-test-pw)
echo   qa test               run the automated tests
echo   qa cli ...            command line, e.g.  qa cli run --url URL --project ID --out report.pdf
echo   qa exe                open the built executable
exit /b 0

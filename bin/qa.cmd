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
if /i "%CMD%"=="start" goto start
if /i "%CMD%"=="new"   goto new
if /i "%CMD%"=="test"  goto test
if /i "%CMD%"=="mock"  goto mock
if /i "%CMD%"=="cli"   goto cli
if /i "%CMD%"=="exe"   goto exe
if /i "%CMD%"=="check" goto check
goto help

:start
start "" "%PYW%" -m pavs
exit /b 0

:new
start "" "%PYW%" -m pavs --new
exit /b 0

:test
pushd "%ROOT%"
shift
"%PY%" -m pytest tests -q %1 %2 %3 %4 %5 %6
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

:mock
set "PROFILE=%~2"
set "PORT=%~3"
if "%PROFILE%"=="" set "PROFILE=2606"
if "%PORT%"=="" set "PORT=8810"
pushd "%ROOT%"
"%PY%" -m mock_polarion.serve %PROFILE% --port %PORT%
popd
exit /b 0

:cli
shift
if "%~1"=="" (
  "%PY%" -m pavs.cli --help
  exit /b 0
)
"%PY%" -m pavs.cli %1 %2 %3 %4 %5 %6 %7 %8 %9
exit /b %ERRORLEVEL%

:check
pushd "%ROOT%"
shift
"%PY%" tools\e2e_check.py %1 %2 %3 %4 %5 %6
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

:exe
start "" "D:\PavsBuild\dist\PolarionValidationStudio\PolarionValidationStudio.exe"
exit /b 0

:help
echo.
echo   qa start              open the app; if it is already open, bring that window to the front
echo   qa new                open another, independent window on purpose
echo   qa mock [profile] [port]   start the fake Polarion (default 2606, port 8810; admin / admin-test-pw)
echo   qa test               run the automated self-checks (about a minute; touches no real server)
echo   qa cli ...            command line tool, e.g.  qa cli run --url URL --project ID --out report.pdf
echo   qa cli                with nothing after it: shows the command line help
echo   qa check              full end-to-end check on this machine (mock servers; add --real-local --exe D:\PavsBuild\dist\PolarionValidationStudio)
echo   qa exe                open the built executable
exit /b 0

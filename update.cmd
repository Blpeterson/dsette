@echo off
setlocal
set "ROOT=%~dp0"
set "LAUNCHER=%ROOT%scripts\dsette_launcher.py"
if not exist "%LAUNCHER%" (
  echo Datasette launcher is missing: "%LAUNCHER%" 1>&2
  exit /b 1
)
if exist "%ROOT%.venv312\Scripts\python.exe" (
  "%ROOT%.venv312\Scripts\python.exe" -c "import sys;raise SystemExit(sys.version_info < (3,9))" >nul 2>&1
  if not errorlevel 1 goto venv312
)
if exist "%ROOT%.venv\Scripts\python.exe" (
  "%ROOT%.venv\Scripts\python.exe" -c "import sys;raise SystemExit(sys.version_info < (3,9))" >nul 2>&1
  if not errorlevel 1 goto venv
)
if exist "%ROOT%.dsette-bootstrap\Scripts\python.exe" (
  "%ROOT%.dsette-bootstrap\Scripts\python.exe" -c "import sys;raise SystemExit(sys.version_info < (3,9))" >nul 2>&1
  if not errorlevel 1 goto bootstrap
)
where py >nul 2>&1
if not errorlevel 1 (
  py -3 -c "import sys;raise SystemExit(sys.version_info < (3,9))" >nul 2>&1
  if not errorlevel 1 goto py
)
where python3 >nul 2>&1
if not errorlevel 1 (
  python3 -c "import sys;raise SystemExit(sys.version_info < (3,9))" >nul 2>&1
  if not errorlevel 1 goto python3
)
where python >nul 2>&1
if not errorlevel 1 (
  python -c "import sys;raise SystemExit(sys.version_info < (3,9))" >nul 2>&1
  if not errorlevel 1 goto python
)
echo Datasette requires Python 3.9+ to start its isolated updater. 1>&2
exit /b 1
:venv312
"%ROOT%.venv312\Scripts\python.exe" "%LAUNCHER%" update %*
exit /b %errorlevel%
:venv
"%ROOT%.venv\Scripts\python.exe" "%LAUNCHER%" update %*
exit /b %errorlevel%
:py
py -3 "%LAUNCHER%" update %*
exit /b %errorlevel%
:bootstrap
"%ROOT%.dsette-bootstrap\Scripts\python.exe" "%LAUNCHER%" update %*
exit /b %errorlevel%
:python3
python3 "%LAUNCHER%" update %*
exit /b %errorlevel%
:python
python "%LAUNCHER%" update %*
exit /b %errorlevel%

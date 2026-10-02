@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Parallax Hedge - Arcus x Lighter

rem ================================================================
rem  One-click start. On a brand-new Windows PC it will:
rem    1. find Python 3.11/3.12 (or download and install Python 3.11
rem       for the current user only - no admin rights needed)
rem    2. create a private virtualenv in .venv
rem    3. install dependencies (falls back to a China mirror)
rem    4. create .env from .env.example (DRY_RUN=true, no orders)
rem    5. start the panel and open it in the browser
rem ================================================================

set "PY_VER=3.11.9"
set "PY_LOCAL=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
set "PIP_MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple"

rem --- port from .env (default 8004) ---
set "PORT=8004"
if exist ".env" for /f "usebackq tokens=1,* delims==" %%a in (`findstr /b /c:"PORT=" ".env"`) do set "PORT=%%b"

rem --- a stale instance keeps trading if the new one cannot bind ---
netstat -ano | findstr /r /c:"127.0.0.1:%PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 goto err_port

if exist ".venv\Scripts\python.exe" goto have_venv

echo [1/4] looking for Python 3.11 / 3.12 ...
set "PY="
(py -3.11 -c "import sys" >nul 2>&1 && set "PY=py -3.11")
if not defined PY (py -3.12 -c "import sys" >nul 2>&1 && set "PY=py -3.12")
if not defined PY (if exist "%PY_LOCAL%" set "PY="%PY_LOCAL%"")
if not defined PY (python -c "import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,12) else 1)" >nul 2>&1 && set "PY=python")
if defined PY goto make_venv

echo       Python 3.11/3.12 not found - downloading Python %PY_VER% ...
set "PY_EXE=%TEMP%\python-%PY_VER%-amd64.exe"
curl.exe -L --fail -o "%PY_EXE%" "https://www.python.org/ftp/python/%PY_VER%/python-%PY_VER%-amd64.exe"
if errorlevel 1 curl.exe -L --fail -o "%PY_EXE%" "https://registry.npmmirror.com/-/binary/python/%PY_VER%/python-%PY_VER%-amd64.exe"
if not exist "%PY_EXE%" goto err_python
echo       installing Python %PY_VER% for the current user (takes a minute) ...
start "" /wait "%PY_EXE%" /quiet InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_test=0 Shortcuts=0 AssociateFiles=0
if not exist "%PY_LOCAL%" goto err_python
set "PY="%PY_LOCAL%""

:make_venv
echo [2/4] creating virtualenv with %PY% ...
%PY% -m venv .venv
if not exist ".venv\Scripts\python.exe" goto err_python
".venv\Scripts\python.exe" -m pip install -q --upgrade pip >nul 2>&1
if errorlevel 1 ".venv\Scripts\python.exe" -m pip install -q --upgrade pip -i %PIP_MIRROR% >nul 2>&1

:have_venv
rem sync deps every start: cheap when already satisfied
echo [3/4] installing / checking dependencies ...
".venv\Scripts\python.exe" -m pip install -q -e .
if not errorlevel 1 goto deps_ok
echo       default index failed - retrying with mirror %PIP_MIRROR%
".venv\Scripts\python.exe" -m pip install -q -e . -i %PIP_MIRROR%
if not errorlevel 1 goto deps_ok
rem offline but already installed once? then just start
".venv\Scripts\python.exe" -c "import parallax_hedge, lighter, fastapi" >nul 2>&1
if errorlevel 1 goto err_deps
echo       (offline - using the packages installed last time)
:deps_ok

if exist ".env" goto env_ready
copy /Y ".env.example" ".env" >nul
echo.
echo  A new .env was created from .env.example (DRY_RUN=true: no orders).
echo  Fill in your Arcus / Lighter keys in .env, then restart this script.
echo.
:env_ready

echo [4/4] starting panel http://127.0.0.1:%PORT%/
".venv\Scripts\python.exe" -m parallax_hedge
pause
exit /b 0

:err_port
echo.
echo ############################################################
echo  PORT %PORT% IS ALREADY IN USE
echo.
echo  An older instance is probably still running - and still
echo  placing orders. Close its window first, then run this again.
echo ############################################################
echo.
pause
exit /b 1

:err_python
echo.
echo Could not find or install Python 3.11.
echo Install it manually from https://www.python.org/downloads/release/python-3119/
echo (tick "Add python.exe to PATH"), then run this script again.
pause
exit /b 1

:err_deps
echo.
echo Dependency install failed - most likely a network or proxy issue.
echo Retry manually:  .venv\Scripts\python.exe -m pip install -e .
pause
exit /b 1

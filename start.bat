@echo off
rem ============================================================
rem  Firmware Manager - reliable launcher
rem  Usage:  start.bat [app.py options]
rem  Example: start.bat --host 0.0.0.0
rem ============================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
set "PORT=5000"

rem --- WinRAR / 7-Zip are usually NOT in PATH; RAR needs them ---
for %%D in ("C:\Program Files\WinRAR" "C:\Program Files\7-Zip" "C:\Program Files (x86)\WinRAR" "C:\Program Files (x86)\7-Zip") do (
    if exist "%%~D" set "PATH=%%~D;!PATH!"
)

rem --- locate Python (py launcher, then PATH, then default install dirs) ---
set "PY="
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto :python_found

python -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto :python_found

if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if defined PY goto :python_found
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PY goto :python_found
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if defined PY goto :python_found
if exist "C:\Python313\python.exe" set "PY=C:\Python313\python.exe"
if defined PY goto :python_found
if exist "C:\Python312\python.exe" set "PY=C:\Python312\python.exe"
if defined PY goto :python_found

echo [ERROR] Python 3.10+ not found.
echo Install it from https://www.python.org/downloads/ and enable "Add python.exe to PATH".
echo.
pause
exit /b 1

:python_found
echo [1/3] Python: %PY%

rem --- dependencies ---
%PY% -c "import flask, rarfile, py7zr" >nul 2>&1
if not errorlevel 1 goto :deps_ok
echo [2/3] Installing dependencies from requirements.txt ...
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] pip install failed.
    echo Run manually: %PY% -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)
goto :deps_done

:deps_ok
echo [2/3] Dependencies: OK

:deps_done
rem --- if the server is already up, just open the browser ---
netstat -an | findstr LISTENING | findstr ":%PORT% " >nul 2>&1
if not errorlevel 1 (
    echo [3/3] Server is already running on port %PORT% - opening browser.
    start "" "http://127.0.0.1:%PORT%"
    exit /b 0
)

echo [3/3] Starting server ...
echo.
echo ============================================================
echo   Firmware Manager is starting
echo   Open:  http://127.0.0.1:%PORT%
echo   Close this window (or press Ctrl+C) to stop the server
echo ============================================================
echo.

%PY% app.py %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    echo [ERROR] app.py exited with code %RC% - see messages above.
) else (
    echo Server stopped.
)
echo.
pause
exit /b %RC%

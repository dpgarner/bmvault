@echo off
setlocal
cd /d "%~dp0"

REM Prefer the Python launcher ("py -3"), fall back to python on PATH.
py -3 --version >nul 2>nul
if errorlevel 1 (set "PY=python") else (set "PY=py -3")

%PY% --version >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python 3 was not found on this machine.
  echo Install it from https://www.python.org/downloads/windows/ and tick
  echo "Add python.exe to PATH" during setup, then run this file again.
  echo.
  pause
  exit /b 1
)

echo.
echo   bmvault GUI:  http://127.0.0.1:8000
echo   Close this window to stop it.
echo.
%PY% gui.py --db vault.db --port 8000
pause

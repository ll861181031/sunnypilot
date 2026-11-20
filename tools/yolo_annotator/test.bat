@echo off
rem Unit tests (dataset / geometry / undo / sync with a mocked paramiko).
setlocal
cd /d "%~dp0"
set "VENV=C:\Users\lsr\sunnypilot-yolo\venv"
set "PY=%VENV%\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] not found: %PY%
  pause
  exit /b 1
)
"%PY%" -m pytest "%~dp0tests" -q %*
exit /b %ERRORLEVEL%

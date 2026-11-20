@echo off
rem ==========================================================================
rem  sunnypilot YOLO labeler - first time setup (Windows 11)
rem  Reuses / creates the venv at C:\Users\lsr\sunnypilot-yolo\venv and installs
rem  the two runtime dependencies (Pillow + paramiko).  No GPU / torch needed.
rem ==========================================================================
setlocal
set "VENV=C:\Users\lsr\sunnypilot-yolo\venv"
set "PY=%VENV%\Scripts\python.exe"

echo [1/3] venv: %VENV%
if not exist "%PY%" (
  echo       creating venv ...
  python -m venv "%VENV%"
  if errorlevel 1 (
    echo [ERROR] failed to create the venv - is python 3.12 on PATH?
    pause
    exit /b 1
  )
)

echo [2/3] installing Pillow + paramiko ...
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install --upgrade pillow paramiko pytest
if errorlevel 1 (
  echo [ERROR] pip install failed - check network / proxy
  pause
  exit /b 1
)

echo [3/3] checking imports ...
"%PY%" -c "import sys, PIL, paramiko, tkinter; print('python', sys.version.split()[0]); print('Pillow', PIL.__version__); print('paramiko', paramiko.__version__); print('Tk', tkinter.TkVersion)"
if errorlevel 1 (
  echo [ERROR] dependency import failed
  pause
  exit /b 1
)

echo.
echo Installed. Next steps:
echo   1) copy sync.json.example to sync.json and edit it
echo   2) double click run_labeler.bat to start
echo   3) double click selftest.bat for a headless self test
pause

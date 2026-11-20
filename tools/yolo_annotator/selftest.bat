@echo off
rem ==========================================================================
rem  Headless self test: builds the UI, loads the first image, zooms, draws a
rem  box, undoes it, saves, then exits.  Writes a screenshot next to this file.
rem  Usage: selftest.bat [batch dir] [screenshot png]
rem ==========================================================================
setlocal
cd /d "%~dp0"
set "VENV=C:\Users\lsr\sunnypilot-yolo\venv"
set "PY=%VENV%\Scripts\python.exe"
set "DATASET=%~1"
set "SHOT=%~2"

if not exist "%PY%" (
  echo [ERROR] not found: %PY%
  echo         run install.bat first
  pause
  exit /b 1
)
if "%DATASET%"=="" set "DATASET=C:\Users\lsr\sunnypilot-yolo\dataset\batch_001"
if "%SHOT%"=="" set "SHOT=%~dp0selftest.png"

if exist "%DATASET%\dataset.json" (
  "%PY%" "%~dp0labeler.py" --selftest --dataset "%DATASET%" --screenshot "%SHOT%"
) else (
  echo [WARN] no dataset at %DATASET% - generating a demo batch instead
  "%PY%" "%~dp0labeler.py" --selftest --screenshot "%SHOT%"
)
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (echo SELFTEST PASSED) else (echo SELFTEST FAILED - rc=%RC%)
exit /b %RC%

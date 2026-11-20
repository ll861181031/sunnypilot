@echo off
rem ==========================================================================
rem  Launch the labeler with pythonw (no console window).
rem  Default batch: C:\Users\lsr\sunnypilot-yolo\dataset\batch_001
rem  Any extra arguments are appended and therefore override the default:
rem     run_labeler.bat --dataset C:\path\to\other_batch
rem     run_labeler.bat --no-sync
rem ==========================================================================
setlocal
cd /d "%~dp0"
set "VENV=C:\Users\lsr\sunnypilot-yolo\venv"
set "PYW=%VENV%\Scripts\pythonw.exe"
set "DATASET=C:\Users\lsr\sunnypilot-yolo\dataset\batch_001"

if not exist "%PYW%" (
  echo [ERROR] not found: %PYW%
  echo         run install.bat first
  pause
  exit /b 1
)

if not exist "%DATASET%\dataset.json" (
  echo [WARN] default batch not found: %DATASET%
  echo        the labeler falls back to dataset_dir.txt / a demo batch;
  echo        use run_labeler.bat --dataset ^<batch dir^> to pick another one.
)

start "" "%PYW%" "%~dp0labeler.py" --dataset "%DATASET%" %*
exit /b 0

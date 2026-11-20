@echo off
set VENV=C:\Users\lsr\sunnypilot-yolo\venv
"%VENV%\Scripts\python.exe" -m pip install --upgrade ultralytics pyyaml paramiko
echo.
echo Installed. Run train.bat after copying a dataset to C:\Users\lsr\sunnypilot-yolo\dataset\batch_001
pause

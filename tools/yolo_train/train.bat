@echo off
set VENV=C:\Users\lsr\sunnypilot-yolo\venv
set DATASET=%1
if "%DATASET%"=="" set DATASET=C:\Users\lsr\sunnypilot-yolo\dataset\batch_001
"%VENV%\Scripts\python.exe" "%~dp0train_yolo.py" "%DATASET%" --epochs 120 --imgsz 640 --batch 12 --device 0 --workers 6 --export-onnx --upload --sync-config "%~dp0sync.json"
pause

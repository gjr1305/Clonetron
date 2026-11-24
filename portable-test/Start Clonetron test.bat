@echo off
rem Clonetron portable test. Uses the official Python in the "python" folder
rem next to this file if it is there, otherwise the Python installed on the PC.
cd /d "%~dp0"
if exist "python\python.exe" (
    "python\python.exe" hello.py
) else (
    python hello.py
)
if errorlevel 1 (
    echo.
    echo Python could not run. Take a photo of this window and send it to Geofrey.
    pause
)

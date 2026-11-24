@echo off
rem Clonetron portable test. Looks for Python in this order:
rem   1. the "python" folder next to this file (portable, official zip)
rem   2. the "py" launcher that the python.org installer adds
rem   3. "python" on the PATH
rem   4. the usual python.org install folders
cd /d "%~dp0"
set "PY="
if exist "python\python.exe" set "PY=python\python.exe"
if not defined PY ( where py >nul 2>nul && set "PY=py -3" )
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY (
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "%ProgramFiles%\Python3*") do (
        if exist "%%~D\python.exe" set "PY=%%~D\python.exe"
    )
)
if not defined PY (
    echo Could not find Python on this PC.
    echo Take a photo of this window and send it to Geofrey.
    pause
    exit /b 1
)
echo Using: %PY%
%PY% hello.py
if errorlevel 1 (
    echo.
    echo Python could not run. Take a photo of this window and send it to Geofrey.
    pause
)

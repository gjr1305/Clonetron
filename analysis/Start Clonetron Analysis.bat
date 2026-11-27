@echo off
rem Clonetron Analysis. Double-click to start; keep the black window open while you use it.
rem Looks for Python in this order: a "python" folder next to this file (portable),
rem the "py" launcher, "python" on the PATH, the usual python.org install folders.
title Clonetron Analysis
cd /d "%~dp0"
set "PY="
if exist "python\python.exe" set "PY=python\python.exe"
if not defined PY ( where py >nul 2>nul && set "PY=py -3" )
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY (
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "%ProgramFiles%\Python3*") do (
        if exist "%%~D\python.exe" set PY="%%~D\python.exe"
    )
)
if not defined PY (
    echo Could not find Python on this PC.
    echo Install Python from python.org, or see README.md for the no-install option.
    pause
    exit /b 1
)

rem First run only: numpy and OpenCV, installed for this Windows user (no admin needed)
%PY% -c "import numpy, cv2" >nul 2>nul
if errorlevel 1 (
    echo First start: installing numpy and OpenCV for your user account.
    echo This needs the internet and takes a minute or two. It only happens once.
    echo.
    %PY% -m pip install --user --disable-pip-version-check numpy opencv-python-headless
    %PY% -c "import numpy, cv2" >nul 2>nul
    if errorlevel 1 (
        echo.
        echo Installing numpy/OpenCV did not work. Take a photo of this window and send it to Geofrey.
        pause
        exit /b 1
    )
)

%PY% clonetron_analysis.py
if errorlevel 1 (
    echo.
    echo Clonetron Analysis stopped with a problem. Take a photo of this window and send it to Geofrey.
    pause
)

@echo off
rem OmniDecompiler launcher
rem   Double-click:            pick maps in a file dialog
rem   Drag maps onto this file: convert them straight away (.bsp or .map)
title OmniDecompiler
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3 from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during setup, then run this again.
    pause
    exit /b 1
)

if "%~1"=="" (
    python -m bsp2vmf --gui
) else (
    python -m bsp2vmf %*
    echo.
    pause
)

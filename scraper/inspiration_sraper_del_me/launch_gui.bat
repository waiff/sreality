@echo off
title Sreality Scraper GUI

REM Change to the directory where this script is located
cd /d "%~dp0"

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH.
    echo Please run setup.bat first.
    pause
    exit /b 1
)

REM Launch GUI
python scraper_gui.py

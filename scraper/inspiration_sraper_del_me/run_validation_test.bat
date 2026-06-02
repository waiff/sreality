@echo off
chcp 65001 >nul 2>&1
title Sreality Scraper - Validation Test
color 0E

REM Change to the directory where this script is located
cd /d "%~dp0"

echo.
echo ============================================================
echo        SREALITY SCRAPER VALIDATION TEST
echo ============================================================
echo.
echo This test will:
echo   1. Scrape 3 cities x 5 months in RANDOM order
echo   2. Scrape the same data in SEQUENTIAL order
echo   3. Compare results to check consistency
echo.
echo Test cities: Praha, Brno, Ostrava
echo Test period: June 2024 - October 2024
echo.
echo ============================================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH.
    echo Please run setup.bat first.
    pause
    exit /b 1
)

echo Starting validation test...
echo This will take approximately 10-15 minutes.
echo.

python validation_test.py

echo.
echo ============================================================
echo Test complete! Check the validation_test_*.json file for results.
echo ============================================================
echo.
pause

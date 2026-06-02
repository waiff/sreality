@echo off
echo ============================================================
echo SREALITY SCRAPER SETUP
echo ============================================================
echo.
echo This will install the required dependencies.
echo.

REM Check if Python is available
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python is not installed or not in PATH.
    echo Please install Python from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

echo Python found. Installing dependencies...
echo.

REM Install Python packages using python -m pip (more reliable than just pip)
echo Installing playwright and pandas...
python -m pip install playwright pandas

if %errorlevel% neq 0 (
    echo.
    echo ERROR: Failed to install Python packages.
    echo Try running: python -m pip install playwright pandas
    pause
    exit /b 1
)

echo.
echo Installing Playwright browsers...
python -m playwright install chromium

if %errorlevel% neq 0 (
    echo.
    echo ERROR: Failed to install Playwright browsers.
    echo Try running: python -m playwright install chromium
    pause
    exit /b 1
)

echo.
echo ============================================================
echo SETUP COMPLETE!
echo ============================================================
echo.
echo You can now run the scraper:
echo   - GUI: Double-click launch_gui.bat
echo   - CLI: Double-click run_scraper.bat
echo   - Validation: Double-click run_validation_test.bat
echo.
pause

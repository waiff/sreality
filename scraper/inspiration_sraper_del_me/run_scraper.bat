@echo off
chcp 65001 >nul 2>&1
title Sreality.cz Scraper
color 0A

REM Change to the directory where this script is located
cd /d "%~dp0"

echo.
echo ============================================================
echo        Sreality.cz Real Estate Price Scraper v2.0
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

:menu
echo.
echo ============================================================
echo   SELECT BATCH TO RUN:
echo ============================================================
echo.
echo   [1] Batch 1: Teplice, Most, Usti nad Labem, Chomutov, 
echo                Sokolov, Beroun
echo.
echo   [2] Batch 2: Kraluv Dvur, Kladno, Horovice, Marianske Lazne,
echo                Tachov, Cheb
echo.
echo   [3] Batch 3: Ostrov, Klatovy, Plzen, Pardubice, 
echo                Hradec Kralove, Rychnov nad Kneznou
echo.
echo   [4] Batch 4: Chrudim, Jihlava, Havlickuv Brod, Humpolec,
echo                Podebrad, Nymburk
echo.
echo   [5] Batch 5: Liberec, Ceska Lipa, Ceske Budejovice, 
echo                Pisek, Mlada Boleslav
echo.
echo   [A] Run ALL batches (full config.json)
echo.
echo   [T] Test mode (2 cities, 1 year)
echo.
echo   [0] Exit
echo.
echo ============================================================

set /p batch="Enter your choice: "

if "%batch%"=="1" goto batch1
if "%batch%"=="2" goto batch2
if "%batch%"=="3" goto batch3
if "%batch%"=="4" goto batch4
if "%batch%"=="5" goto batch5
if "%batch%"=="A" goto all
if "%batch%"=="a" goto all
if "%batch%"=="T" goto test
if "%batch%"=="t" goto test
if "%batch%"=="0" goto end

echo Invalid choice. Please try again.
goto menu

:batch1
echo.
echo Starting Batch 1 (Teplice, Most, Usti, Chomutov, Sokolov, Beroun)...
call :run_mode config_batch1.json
goto done

:batch2
echo.
echo Starting Batch 2 (Kraluv Dvur, Kladno, Horovice, Mar. Lazne, Tachov, Cheb)...
call :run_mode config_batch2.json
goto done

:batch3
echo.
echo Starting Batch 3 (Ostrov, Klatovy, Plzen, Pardubice, HK, Rychnov)...
call :run_mode config_batch3.json
goto done

:batch4
echo.
echo Starting Batch 4 (Chrudim, Jihlava, Havl. Brod, Humpolec, Podebrad, Nymburk)...
call :run_mode config_batch4.json
goto done

:batch5
echo.
echo Starting Batch 5 (Liberec, Ceska Lipa, Ceske Budejovice, Pisek, Ml. Boleslav)...
call :run_mode config_batch5.json
goto done

:all
echo.
echo Starting ALL cities (full config.json)...
call :run_mode config.json
goto done

:test
echo.
echo Starting TEST mode...
echo.
echo Select run mode:
echo   [1] Normal (browser visible)
echo   [2] Headless (no browser window - faster)
echo.
set /p mode="Enter mode (1 or 2): "

if "%mode%"=="2" (
    python scraper.py --config config_batch1.json --test --headless
) else (
    python scraper.py --config config_batch1.json --test
)
goto done

:run_mode
echo.
echo Select run mode:
echo   [1] Normal (browser visible)
echo   [2] Headless (no browser window - faster)
echo.
set /p mode="Enter mode (1 or 2): "

if "%mode%"=="1" (
    python scraper.py --config %1
) else (
    python scraper.py --config %1 --headless
)
exit /b

:done
echo.
echo ============================================================
echo   Scraping finished! Check the output folder for results.
echo ============================================================
echo.
pause
goto menu

:end
echo Goodbye!
exit /b 0

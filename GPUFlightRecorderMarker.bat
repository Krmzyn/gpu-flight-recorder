@echo off
setlocal
cd /d "%~dp0"

if "%~1"=="" (
    echo Usage: GPUFlightRecorderMarker.bat "display froze / observed"
    exit /b 2
)

where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    python -m gpu_flight_recorder marker %*
    goto :done
)

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    py -3 -m gpu_flight_recorder marker %*
    goto :done
)

echo Python 3 was not found. Install Python 3.11+ or add it to PATH.
exit /b 1

:done
endlocal

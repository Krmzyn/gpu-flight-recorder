@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    python -m gpu_flight_recorder %*
    goto :done
)

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    py -3 -m gpu_flight_recorder %*
    goto :done
)

echo Python 3 was not found. Install Python 3.11+ or add it to PATH.
pause

:done
endlocal

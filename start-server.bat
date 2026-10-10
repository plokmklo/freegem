@echo off
setlocal
cd /d "%~dp0"

rem ============================================================
rem  FreeGem server launcher
rem  Starts the OpenAI-compatible + Google-native + health API.
rem
rem  Usage:
rem    start-server.bat                 (default port 8081)
rem    start-server.bat --port 9000     (any freegem serve args work)
rem
rem  Stop the server: Ctrl+C in this window, or close the window.
rem ============================================================

where freegem >nul 2>nul
if %errorlevel%==0 (
    set "RUNNER=freegem"
) else (
    set "RUNNER=python -m freegem"
)

echo  FreeGem server
echo  --------------------------------------------------
echo  URLs below use the default port 8081; pass --port to change it.
echo  Stop   : Ctrl+C (or close this window)
echo  --------------------------------------------------
echo.

%RUNNER% serve %*

set "EXITCODE=%errorlevel%"
echo.
if "%EXITCODE%"=="0" (
    echo Server stopped.
) else (
    echo Server exited with code %EXITCODE%.
    echo If this is an import/install error, run: pip install -e ".[full]"
)
pause
exit /b %EXITCODE%

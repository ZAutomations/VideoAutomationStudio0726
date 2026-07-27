@echo off
REM ===================================================================
REM  create_desktop_shortcut.bat
REM  Creates a "Video Automation Studio" desktop + Start-menu shortcut
REM  with the colourful app icon. Safe to run any time.
REM ===================================================================
echo.
echo Creating "Video Automation Studio" desktop shortcut ...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_desktop_shortcut.ps1"
if errorlevel 1 (
    echo.
    echo [WARN] Could not create the shortcut automatically.
    echo        You can right-click run.bat -^> Send to -^> Desktop instead.
)
echo.
pause

@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_web.ps1" %*
set "EXITCODE=%ERRORLEVEL%"
echo.
if not "%EXITCODE%"=="0" echo Startup failed. See the messages above.
if "%EXITCODE%"=="0" echo EduAgent is running in the background. You can close this window.
pause
exit /b %EXITCODE%

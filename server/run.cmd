@echo off
setlocal
REM Starts the Shisu-ko transcription server and restarts it if it stops unexpectedly
REM (for example after a GPU driver reset). Extra arguments are passed through, e.g.
REM       run.cmd --model kotoba-tech/kotoba-whisper-v2.0-faster
REM       run.cmd --cookies-from-browser firefox
REM       run.cmd --check

set "VENV=%USERPROFILE%\.shisu-ko\venv"
if not exist "%VENV%\Scripts\python.exe" (
  echo The Python environment is missing. Run setup.cmd first.
  pause
  exit /b 1
)

:loop
"%VENV%\Scripts\python.exe" "%~dp0server.py" %*
set "CODE=%ERRORLEVEL%"
if "%CODE%"=="0" goto end
if "%CODE%"=="2" goto end
echo.
echo The server stopped unexpectedly (exit code %CODE%). Restarting in 5 seconds... press Ctrl+C to quit.
timeout /t 5 /nobreak >nul
goto loop

:end
pause

@echo off
REM Stops and removes the Shisu-ko server container (models and caches in DATA_DIR are kept)
REM and closes the WSL keep-alive window if one is open.
setlocal
cd /d "%~dp0.."
where docker >nul 2>nul
if not errorlevel 1 (
  docker compose down
) else (
  wsl -d Ubuntu -- docker compose down
  taskkill /fi "WINDOWTITLE eq Shisu-ko WSL keep-alive" >nul 2>nul
)
pause

@echo off
REM Follows the Shisu-ko server container log (Ctrl+C to stop following).
setlocal
cd /d "%~dp0.."
where docker >nul 2>nul
if not errorlevel 1 (
  docker compose logs -f --tail 100
) else (
  wsl -d Ubuntu -- docker compose logs -f --tail 100
)

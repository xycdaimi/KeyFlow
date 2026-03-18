@echo off
setlocal
cd /d "%~dp0.."

if "%~1"=="--volumes" (
  docker compose -f docker\postgresql\docker-compose.yml down --volumes
) else (
  docker compose -f docker\postgresql\docker-compose.yml down
)

exit /b %errorlevel%

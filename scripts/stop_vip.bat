@echo off
setlocal
cd /d "%~dp0.."

if not exist ".env" (
  echo Skip vip down: .env not found.
  exit /b 0
)

if "%~1"=="--volumes" (
  docker compose -f docker\vip\docker-compose.yml down --volumes
) else (
  docker compose -f docker\vip\docker-compose.yml down
)

exit /b %errorlevel%

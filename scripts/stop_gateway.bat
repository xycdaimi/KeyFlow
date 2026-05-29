@echo off
setlocal
cd /d "%~dp0.."

if not exist ".env.gateway" (
  echo Skip gateway down: .env.gateway not found.
  exit /b 0
)

if "%~1"=="--volumes" (
  docker compose -f docker\gateway\docker-compose.yml down --volumes
) else (
  docker compose -f docker\gateway\docker-compose.yml down
)

exit /b %errorlevel%

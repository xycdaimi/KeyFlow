@echo off
setlocal
cd /d "%~dp0.."

if not exist ".env.gateway" (
  copy /Y ".env.gateway.example" ".env.gateway" >nul
  echo Created .env.gateway from .env.gateway.example
)

if "%~1"=="--no-build" (
  docker compose --env-file .env.gateway -f docker\gateway\docker-compose.yml up -d
) else (
  docker compose --env-file .env.gateway -f docker\gateway\docker-compose.yml up -d --build
)

exit /b %errorlevel%

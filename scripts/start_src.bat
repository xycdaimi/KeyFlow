@echo off
setlocal
cd /d "%~dp0.."

if not exist ".env" (
  copy /Y ".env.example" ".env" >nul
  echo Created .env from .env.example
)

if "%~1"=="--no-build" (
  docker compose --env-file .env -f docker\src\docker-compose.yml up -d
) else (
  docker compose --env-file .env -f docker\src\docker-compose.yml up -d --build
)

exit /b %errorlevel%

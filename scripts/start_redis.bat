@echo off
setlocal
cd /d "%~dp0.."

docker compose -f docker\redis\docker-compose.yml up -d
exit /b %errorlevel%

@echo off
setlocal

cd /d "%~dp0\.."

if "%KEYFLOW_GATEWAY_IMAGE_NAME%"=="" set "KEYFLOW_GATEWAY_IMAGE_NAME=keyflow-gateway"
if "%KEYFLOW_IMAGE_TAG%"=="" set "KEYFLOW_IMAGE_TAG=prod"
if "%~1"=="" (
  set "OUTPUT=dist\%KEYFLOW_GATEWAY_IMAGE_NAME%-%KEYFLOW_IMAGE_TAG%.tar"
) else (
  set "OUTPUT=%~1"
)

if not exist "dist" mkdir "dist"

docker build -f docker\src\Dockerfile -t "%KEYFLOW_GATEWAY_IMAGE_NAME%:%KEYFLOW_IMAGE_TAG%" .
if errorlevel 1 exit /b %errorlevel%

docker save -o "%OUTPUT%" "%KEYFLOW_GATEWAY_IMAGE_NAME%:%KEYFLOW_IMAGE_TAG%"
if errorlevel 1 exit /b %errorlevel%

echo Gateway image saved: %OUTPUT%
echo Gateway image tag: %KEYFLOW_GATEWAY_IMAGE_NAME%:%KEYFLOW_IMAGE_TAG%

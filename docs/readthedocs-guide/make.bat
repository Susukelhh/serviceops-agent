@echo off
setlocal

if "%1"=="" goto html
if /I "%1"=="html" goto html

echo 用法: make.bat html
exit /b 2

:html
uv run --with sphinx --with sphinx-rtd-theme sphinx-build -W --keep-going -b html . _build/html
exit /b %ERRORLEVEL%

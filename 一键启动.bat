@echo off
chcp 65001 >nul 2>&1
setlocal

:: ============================================
:: Grok2API 一键启动脚本
:: 首次运行自动安装 uv 和项目依赖
:: ============================================

title Grok2API

set "PROJECT_DIR=%~dp0"
cd /d "%PROJECT_DIR%"

set "HOST=127.0.0.1"
set "PORT=8000"
set "ADMIN_URL=http://%HOST%:%PORT%/admin"

echo.
echo  ========================================
echo      Grok2API Launcher
echo  ========================================
echo.

:: ===================== Step 1: uv =====================
echo [1/4] Checking uv...
set "PATH=%USERPROFILE%\.local\bin;%PATH%"

where uv >nul 2>&1
if %ERRORLEVEL% equ 0 goto UV_OK

echo       uv not found, installing...
powershell -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
where uv >nul 2>&1
if %ERRORLEVEL% equ 0 goto UV_INSTALLED

echo [ERROR] uv install failed. See: https://docs.astral.sh/uv/
pause
exit /b 1

:UV_INSTALLED
echo       uv installed successfully.
goto UV_DONE

:UV_OK
echo       uv is ready.

:UV_DONE

:: ===================== Step 2: sync =====================
echo [2/4] Syncing dependencies...
uv sync
if %ERRORLEVEL% neq 0 goto SYNC_FAIL
echo       Dependencies synced.
goto SYNC_DONE

:SYNC_FAIL
echo [ERROR] Dependency sync failed. Check network or Python version (requires >=3.13).
pause
exit /b 1

:SYNC_DONE

:: ===================== Step 3: config =====================
echo [3/4] Checking config...
if exist "config.toml" goto CONFIG_EXISTS
copy /y config.defaults.toml config.toml >nul
echo       config.toml created from defaults.
goto CONFIG_DONE

:CONFIG_EXISTS
echo       config.toml exists.

:CONFIG_DONE

:: ===================== Step 4: start =====================
echo [4/4] Starting Grok2API...
echo.
echo  ----------------------------------------
echo    Admin:    %ADMIN_URL%
echo    API:      http://%HOST%:%PORT%/v1
echo    Password: grok2api
echo    Press Ctrl+C to stop
echo  ----------------------------------------
echo.

:: Open browser after 3 seconds
start "" cmd /c "timeout /t 3 /nobreak >nul && start %ADMIN_URL%"

:: Start server
uv run python main.py

echo.
echo Server stopped.
pause

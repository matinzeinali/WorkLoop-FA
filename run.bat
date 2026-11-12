@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title WorkLoop
set BACK_PORT=8000
set FRONT_PORT=5173

rem Optional: run.bat <number> runs one option without the menu.
if not "%~1"=="" (
  set "choice=%~1"
  set ONESHOT=1
  goto dispatch
)

:menu
cls
echo ==========================================
echo                 WorkLoop
echo ==========================================
echo   1. Run backend + frontend
echo   2. Run frontend only
echo   3. Run backend only
echo   4. Install all dependencies
echo   5. Remove dependencies and caches
echo   6. Stop everything
echo   7. Exit
echo ==========================================
set "choice="
set /p choice=Choose [1-7]:
:dispatch
if "%choice%"=="1" goto both
if "%choice%"=="2" goto front
if "%choice%"=="3" goto back
if "%choice%"=="4" goto install
if "%choice%"=="5" goto clean
if "%choice%"=="6" goto stop
if "%choice%"=="7" goto end
echo Invalid choice.
if defined ONESHOT goto end
ping -n 1 127.0.0.1 >nul
goto menu

:both
call :start_back || goto pause_menu
call :start_front
ping -n 3 127.0.0.1 >nul
start "" http://127.0.0.1:%FRONT_PORT%
echo Backend and frontend are running.
goto pause_menu

:front
call :start_front
ping -n 2 127.0.0.1 >nul
start "" http://127.0.0.1:%FRONT_PORT%
goto pause_menu

:back
call :start_back
goto pause_menu

:install
echo Installing dependencies from requirements.txt ...
python -m pip install -r requirements.txt
if errorlevel 1 (echo Backend install failed.& goto pause_menu)
echo Installing frontend packages ...
pushd frontend
call npm install
popd
if errorlevel 1 (echo Frontend install failed.) else (echo Done.)
goto pause_menu

:clean
echo This removes the packages in requirements.txt from your Python
echo and deletes frontend\node_modules, __pycache__ folders, and the pip and npm caches.
echo Your data folder (database, keys) is NOT touched.
set "ok="
set /p ok=Continue? [y/N]:
if /i not "%ok%"=="y" goto menu
call :stop_all
python -m pip uninstall -y -r requirements.txt
for /d /r "%~dp0" %%d in (__pycache__) do if exist "%%d" rd /s /q "%%d"
python -m pip cache purge
if exist "frontend\node_modules" rd /s /q "frontend\node_modules"
if exist "frontend\dist" rd /s /q "frontend\dist"
call npm cache clean --force
echo Done.
goto pause_menu

:stop
call :stop_all
echo Everything stopped.
goto pause_menu

:end
endlocal
exit /b 0

:pause_menu
if defined ONESHOT goto end
echo.
pause
goto menu

rem ---------- helpers ----------
:start_back
python -c "import fastapi, litellm" 2>nul
if errorlevel 1 (
  echo Dependencies are missing. Choose option 4 first.
  exit /b 1
)
echo Starting backend on http://127.0.0.1:%BACK_PORT% ...
start "WorkLoop backend" cmd /k python -m uvicorn app.main:app --host 127.0.0.1 --port %BACK_PORT%
exit /b 0

:start_front
echo Starting frontend on http://127.0.0.1:%FRONT_PORT% ...
if not exist "frontend\node_modules" (echo Frontend packages are missing. Choose option 4 first. & exit /b 1)
start "WorkLoop frontend" cmd /k "cd /d "%~dp0frontend" && npm run dev -- --host 127.0.0.1 --port %FRONT_PORT% --strictPort"
exit /b 0

:stop_all
for %%p in (%BACK_PORT% %FRONT_PORT%) do (
  for /f "tokens=5" %%i in ('netstat -ano ^| findstr /r /c:"127.0.0.1:%%p .*LISTENING"') do taskkill /PID %%i /T /F >nul 2>&1
)
taskkill /FI "WINDOWTITLE eq WorkLoop backend*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq WorkLoop frontend*" /T /F >nul 2>&1
exit /b 0

@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "ROOT=%~dp0"
set "PYTHON_DIR=%ROOT%runtime\python-embed"
set "PYTHON_EXE=%PYTHON_DIR%\python.exe"
set "SITE_PACKAGES=%ROOT%runtime\site-packages"
set "SMOKE=%ROOT%tools\import_smoke.py"
set "PUSHD_OK="

echo [INFO] KCH-PPT-Tool startup diagnostics
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=[System.IO.Path]::GetFullPath($env:ROOT); if($p.Length -ge 240){Write-Host ('[WARN] Package path length {0} is close to the Windows 260-character limit: {1}' -f $p.Length,$p)} else {Write-Host ('[OK] Package path length {0}' -f $p.Length)}"

pushd "%ROOT%" || goto fail
set "PUSHD_OK=1"

set "WRITE_TEST=%ROOT%.__write_test_%RANDOM%.tmp"
> "%WRITE_TEST%" echo ok
if errorlevel 1 (
    echo [ERROR] Package directory is not writable: "%ROOT%"
    goto fail
)
del "%WRITE_TEST%" >nul 2>nul
echo [OK] Write permission check passed.

if exist "%PYTHON_EXE%" goto have_embedded

echo [WARN] Embedded Python not found under this folder: "%PYTHON_EXE%"

rem --- Dev-source convenience: delegate to the built self-contained package if present. ---
if exist "%ROOT%build\dist\KCH-PPT-Tool\START.bat" (
    echo [INFO] Built package detected. Launching build\dist\KCH-PPT-Tool\START.bat ...
    if defined PUSHD_OK popd
    call "%ROOT%build\dist\KCH-PPT-Tool\START.bat"
    exit /b %ERRORLEVEL%
)

rem --- Otherwise fall back to a system Python on PATH (requires deps installed). ---
where py >nul 2>nul
if not errorlevel 1 ( set "PYTHON_EXE=py" & goto got_system )
where python >nul 2>nul
if not errorlevel 1 ( set "PYTHON_EXE=python" & goto got_system )
where python3 >nul 2>nul
if not errorlevel 1 ( set "PYTHON_EXE=python3" & goto got_system )
echo [ERROR] No embedded runtime, no built package, and no system Python found.
echo         Run the packaged build\dist\KCH-PPT-Tool\START.bat, or unzip KCH-PPT-Tool-*.zip and run its START.bat.
goto fail

:got_system
echo [INFO] Using system Python: %PYTHON_EXE% (source-run mode; requires dependencies installed)
set "SITE_PACKAGES="
goto run_server

:have_embedded
echo [OK] Embedded Python found.
if not exist "%SITE_PACKAGES%" (
    echo [ERROR] Runtime site-packages not found: "%SITE_PACKAGES%"
    goto fail
)
if not exist "%SMOKE%" set "SMOKE=%ROOT%build\import_smoke.py"
if not exist "%SMOKE%" (
    echo [ERROR] import_smoke.py not found under tools or build.
    goto fail
)
"%PYTHON_EXE%" "%SMOKE%" --mode import --site-packages "%SITE_PACKAGES%" --root "%ROOT%"
if errorlevel 1 (
    echo [ERROR] Python package self-diagnosis failed.
    goto fail
)
echo [OK] Python package self-diagnosis passed.

:run_server
for %%C in (claude codex gemini) do (
    where %%C >nul 2>nul
    if errorlevel 1 (
        echo [WARN] %%C CLI not found. Related options will be unavailable until it is installed and on PATH.
    ) else (
        echo [OK] %%C CLI detected.
    )
)

set "PYTHONUTF8=1"
if defined SITE_PACKAGES (
    set "PYTHONPATH=%ROOT%;%SITE_PACKAGES%"
) else (
    set "PYTHONPATH=%ROOT%"
)
echo [INFO] Starting server. The server opens the browser; keep this console open to keep it running.
"%PYTHON_EXE%" -m app.server
set "EXITCODE=%ERRORLEVEL%"
if defined PUSHD_OK popd
if not "%EXITCODE%"=="0" (
    echo [ERROR] Server exited with code %EXITCODE%.
    pause
)
exit /b %EXITCODE%

:fail
echo.
echo [ERROR] Startup diagnostics failed. Review the messages above.
if defined PUSHD_OK popd
pause
exit /b 1

@echo off
setlocal

:: Calcula la ruta absoluta ANTES del bloque IF para evitar expansion vacia
for %%I in ("%~dp0..") do set "KIT_ROOT=%%~fI"

if /I not "%~d0"=="P:" (
    subst P: /D >nul 2>&1
    subst P: "%KIT_ROOT%"
    if errorlevel 1 (
        echo [ERROR] No se pudo montar la unidad virtual P: hacia "%KIT_ROOT%"
        pause
        exit /b 1
    )
)

set "PATH=P:\tools\python;P:\tools\python\Scripts;%PATH%"

cd /d P:\local-nvr
echo Iniciando NVR Local en http://127.0.0.1:8000 ...
P:\tools\python\python.exe nvr_local.py
pause
@echo off
setlocal

:: Solo monta P: si el script se ejecuto desde otra unidad (ej. C:\ o USB)
if /I not "%~d0"=="P:" (
    for %%I in ("%~dp0..") do set "KIT_ROOT=%%~fI"
    subst P: /D >nul 2>&1
    subst P: "%KIT_ROOT%"
    if errorlevel 1 (
        echo [ERROR] No se pudo montar la unidad virtual P: hacia "%KIT_ROOT%"
        pause
        exit /b 1
    )
)

set "PLATFORMIO_CORE_DIR=P:\tools\pio_core"
set "PATH=P:\tools\python;P:\tools\python\Scripts;P:\tools\git\cmd;%PATH%"

start "" "P:\tools\vscode\Code.exe" "P:\esp32-firmware"
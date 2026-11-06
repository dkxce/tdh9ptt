@echo off

rem # author  # github.com\dkxce
rem # version # v1.0.0.0

:begin
chcp 65001 >nul
set "PROCESS_NAME=sound_bt"

:iflaunched
tasklist /nh /fi "imagename eq %PROCESS_NAME%.exe" 2>nul | findstr /i "%PROCESS_NAME%" >nul
if %errorlevel% neq 0 goto build
echo Процесс %PROCESS_NAME% сейчас запущен.
set /p "choice=Остановить его и запустить сборку exe? [Y/N]: "
if /i "%choice%"=="Y" (
    powershell -Command "Stop-Process -Name %PROCESS_NAME% -Force"
	goto build
) else exit /b	

:build
@echo on
.venv\Scripts\create-version-file sound_bt.yaml --outfile sound_bt.txt
@rem .venv\Scripts\pyinstaller.exe  --version-file="sound_bt.txt" -i "sound_bt.ico" --noconsole --onefile sound_bt/app.py
.venv\Scripts\pyinstaller.exe  --clean --version-file="sound_bt.txt" -i "sound_bt.ico" --noconsole --onefile sound_bt/app.py
@del /Q sound_bt.txt
tools\upx.exe -9f dist\app.exe
move /y dist\app.exe dist\sound_bt.exe
@echo Сборка завершена
@pause

:end
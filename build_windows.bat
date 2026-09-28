@echo off
cd /d "%~dp0"
py -3 -m pip install pyinstaller
if errorlevel 1 goto fail
py -3 -m PyInstaller --clean --onefile --windowed --name AnimeSubtitleFinder run.py
if errorlevel 1 goto fail
echo Built: dist\AnimeSubtitleFinder.exe
pause
exit /b 0
:fail
echo Build failed. Python and pip status must be checked.
pause
exit /b 1

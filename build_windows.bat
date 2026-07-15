@echo off
setlocal

py -m PyInstaller ^
  --clean ^
  --noconfirm ^
  --onefile ^
  --name TwitchChannelPointsMiner ^
  --collect-all TwitchChannelPointsMiner ^
  --add-data "assets;assets" ^
  --add-data "config.example.py;." ^
  windows_launcher.py

if errorlevel 1 exit /b %errorlevel%

echo.
echo Built dist\TwitchChannelPointsMiner.exe

@echo off
echo ================================
echo  PC Control Hub - Agent Builder
echo ================================
echo.

:: Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.11+ from python.org
    pause
    exit /b 1
)

:: Install/upgrade dependencies
echo [1/3] Installing dependencies...
pip install pyinstaller websockets qrcode pillow pystray psutil pycaw pywin32 --quiet
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies
    pause
    exit /b 1
)

:: Clean previous build
echo [2/3] Cleaning previous build...
if exist dist\PCControlHub-Agent.exe del /f /q dist\PCControlHub-Agent.exe
if exist build rmdir /s /q build

:: Build exe
echo [3/3] Building executable...
pyinstaller agent.spec --clean --noconfirm
if errorlevel 1 (
    echo [ERROR] Build failed
    pause
    exit /b 1
)

echo.
echo ================================
echo  Build complete!
echo  Output: dist\PCControlHub-Agent.exe
echo ================================
echo.
echo Upload dist\PCControlHub-Agent.exe to GitHub Releases.
pause

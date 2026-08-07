@echo off
chcp 65001 >nul
title 매크로 EXE 만들기
cd /d "%~dp0"

echo ============================================
echo   자동 새로고침 매크로 - EXE 만들기
echo ============================================
echo.

REM 1) 파이썬 확인
python --version >nul 2>&1
if errorlevel 1 (
    echo [오류] 파이썬이 설치되어 있지 않습니다.
    echo    https://www.python.org/downloads/ 에서 설치하세요.
    echo    설치 시 "Add Python to PATH" 를 꼭 체크하세요.
    echo.
    pause
    exit /b 1
)

echo [1/3] 필요한 프로그램 설치 중... (처음 한 번만 오래 걸립니다)
python -m pip install --upgrade pip >nul 2>&1
python -m pip install pyautogui pyinstaller
if errorlevel 1 (
    echo [오류] 설치에 실패했습니다. 인터넷 연결을 확인하세요.
    pause
    exit /b 1
)
echo.

echo [2/3] EXE 파일 만드는 중... (1~2분 걸립니다)
python -m PyInstaller --onefile --windowed --name "자동새로고침매크로" "매크로.py"
if errorlevel 1 (
    echo [오류] EXE 만들기에 실패했습니다.
    pause
    exit /b 1
)
echo.

echo [3/3] 완료!
echo.
echo   dist 폴더 안의  "자동새로고침매크로.exe"  를 실행하세요.
echo   이 exe 파일 하나만 복사하면 다른 윈도우 PC에서도 실행됩니다.
echo   (파이썬 설치 없이 실행 가능)
echo.

REM 만들어진 exe 가 있는 dist 폴더 열기
if exist "dist\자동새로고침매크로.exe" (
    start "" "dist"
)
pause

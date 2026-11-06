@echo off
rem Airbot Log Analyzer 실행 (Windows)
rem   최초 실행 시 .venv 를 만들고 requirements.txt 를 설치한다 (requirements 가 바뀌면 다시 설치).
rem   사용: run.bat [로그 압축/폴더 ...]  (탐색기에서 더블클릭해도 됨)
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [setup] .venv 준비 중 ^(최초 1회, 수 분 걸릴 수 있음^)...
  py -3 -m venv .venv 2>nul || python -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo [setup] Python 3.10 이상을 설치한 뒤 다시 실행하세요: https://www.python.org/downloads/
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install -q --upgrade pip
)
rem requirements.txt 가 설치 당시와 다르면 (git pull 등으로 바뀌면) 다시 설치
fc /b requirements.txt ".venv\requirements.installed" >nul 2>&1
if errorlevel 1 (
  echo [setup] 필요한 패키지 설치 중...
  ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt && copy /y requirements.txt ".venv\requirements.installed" >nul
)

".venv\Scripts\python.exe" run.py %*
if errorlevel 1 pause

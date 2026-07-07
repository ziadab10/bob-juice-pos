@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================
echo   BOB JUICE POS - Setup and Run
echo ============================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [1/4] Creating Python virtual environment...
    py -3 -m venv .venv 2>nul
    if errorlevel 1 (
        python -m venv .venv
        if errorlevel 1 (
            echo ERROR: Could not create .venv. Install Python 3.10+ and try again.
            pause
            exit /b 1
        )
    )
) else (
    echo [1/4] Virtual environment found.
)

echo [2/4] Installing dependencies...
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip --quiet
pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: pip install failed.
    pause
    exit /b 1
)

echo [3/4] Initializing fresh database...
python initialize_database.py
if errorlevel 1 (
    echo ERROR: Database initialization failed.
    pause
    exit /b 1
)

echo [4/4] Starting server on http://localhost:8000
echo.
echo   Cashier POS : http://localhost:8000
echo   Admin       : http://localhost:8000/admin/dashboard
echo   Login       : http://localhost:8000/login
echo.
python -m uvicorn main:app --host 0.0.0.0 --port 8000

pause

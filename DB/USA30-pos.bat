@echo off
:: 1. Navigate to your project directory
cd /d "C:\Users\Uyonoh\Documents\Coding\Python\automations\autofx\DB"

:: 2. Run Python directly from the .venv without needing "activate"
:: This is the "Pro" way for automation—it avoids shell nesting issues
set PYTHON_PATH=..\.venv\Scripts\python.exe

:: 3. Set the window title
title USA30

:: 4. Start MT5
start "" "C:\Program Files\HFM Metatrader 5\terminal64.exe"

:: 5. Optional: Wait 10 seconds for MT5/Internet to stabilize
timeout /t 10 /nobreak

:: 6. Run the script and keep the window open if it crashes
"%PYTHON_PATH%" positions_manager.py usa30 --magic 225737 --max-tp 200
pause
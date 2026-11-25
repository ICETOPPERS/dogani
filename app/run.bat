@echo off
rem Usage: run.bat [--dir "D:\Notes"] [--port 8765] [--name NAME]
rem Run with a Python that has PySide6/pystray (PATH python first, then versions known to the py launcher)
python -c "import PySide6, pystray" >nul 2>&1 && (start "" pythonw "%~dp0dogani.py" %* & exit /b)
for %%V in (3.13 3.12 3.14 3.11 3.15) do (
  py -%%V -c "import PySide6, pystray" >nul 2>&1 && (start "" pyw -%%V "%~dp0dogani.py" %* & exit /b)
)
echo No Python with PySide6 found. Run: pip install -r "%~dp0requirements.txt"
pause

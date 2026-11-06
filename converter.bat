@echo off
REM Arraste um ou mais PDFs (ou uma pasta) para cima deste arquivo.
chcp 65001 >nul
set PYTHONUTF8=1
"%~dp0.venv\Scripts\python.exe" "%~dp0pdf2epub.py" %* --report
echo.
pause

@echo off
REM Modo automatico: todo PDF colocado em "entrada" vira EPUB em "saida".
chcp 65001 >nul
set PYTHONUTF8=1
if not exist "%~dp0entrada" mkdir "%~dp0entrada"
if not exist "%~dp0saida" mkdir "%~dp0saida"
"%~dp0.venv\Scripts\python.exe" "%~dp0pdf2epub.py" --watch "%~dp0entrada" -o "%~dp0saida" --report

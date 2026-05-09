@echo off
rem Wrapper for the BambuCLI Scheduled Task. Runs uvicorn from the venv,
rem appending stdout/stderr to log files alongside the script.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m uvicorn app:app --host 0.0.0.0 --port 8000 >> "%~dp0uvicorn.out.log" 2>> "%~dp0uvicorn.err.log"

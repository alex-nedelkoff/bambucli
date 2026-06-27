@echo off
rem Wrapper for the BambuCLI Scheduled Task. Lives in deploy\; runs uvicorn
rem from the venv at the repo root (the parent of this folder), appending
rem stdout/stderr to log files there.
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m uvicorn app:app --host 0.0.0.0 --port 8000 >> "uvicorn.out.log" 2>> "uvicorn.err.log"

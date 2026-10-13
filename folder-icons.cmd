@echo off
rem folder-icons on PATH: runs folder_icons.py with the py launcher if there is one, else python
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "%~dp0folder_icons.py" %*
) else (
    python "%~dp0folder_icons.py" %*
)

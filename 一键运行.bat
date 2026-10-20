@echo off
rem One-click launcher: opens a small menu (demo check / real check / test email).
rem Keep this file ASCII-only; all Chinese text lives in menu.py.
cd /d "%~dp0"
python menu.py
rem Window must stay open so crash messages / tracebacks can be read.
pause

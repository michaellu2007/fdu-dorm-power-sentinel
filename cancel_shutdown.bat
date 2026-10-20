@echo off
rem Cancel the pending shutdown issued by the monitor, and mark today as
rem cancelled so the script will not re-issue a shutdown until tomorrow.
shutdown /a
type nul > "%~dp0shutdown_cancelled.txt"
echo Shutdown cancelled. The monitor will not re-issue a shutdown until tomorrow.
pause

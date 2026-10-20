@echo off
rem Tell the monitor the dorm has been recharged: snooze alerts for 24h.
rem (The school meter only refreshes once a day; this bridges the lag.)
cd /d "%~dp0"
python snooze.py

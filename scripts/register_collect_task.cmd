@echo off
rem Register a daily 01:30 task. Remove with: schtasks /Delete /TN WelfareTwin_Collect /F
schtasks /Create /TN "WelfareTwin_Collect" /TR "\"%~dp0collect_daily.cmd\"" /SC DAILY /ST 01:30 /F

@echo off
rem WelfareTwin: resume welfare-service collection (data.go.kr daily quota). Register with scripts\register_collect_task.cmd
cd /d "%~dp0.."
echo ==== %date% %time% >> .cache\collect_daily.log
"%APPDATA%\Python\Python313\Scripts\uv.exe" run python -m welfaretwin.data.collect --source central --source local >> .cache\collect_daily.log 2>&1

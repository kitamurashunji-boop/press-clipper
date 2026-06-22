@echo off
cd /d "%~dp0"
if exist env.bat call env.bat
echo Press Clipper を起動中...
echo http://localhost:8502 をブラウザで開いてください
python -m uvicorn server:app --host 0.0.0.0 --port 8502 --reload
pause

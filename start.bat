@echo off
cd /d "%~dp0"
set ANTHROPIC_API_KEY=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
set GOOGLE_API_KEY=AIzaSyBrmgwvo3Rs154MqTauwNRpPNGHrIDtR6E
set GOOGLE_CX=26fdb0bfd0a7943ec
echo Press Clipper を起動中...
echo http://localhost:8502 をブラウザで開いてください
python -m uvicorn server:app --host 0.0.0.0 --port 8502 --reload
pause

@echo off
cd /d "%~dp0"
pip install -q -r requirements.txt
start "" http://127.0.0.1:8777
python app.py

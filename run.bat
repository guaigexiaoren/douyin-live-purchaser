@echo off
cd /d "%~dp0douyin-live-purchaser"
start "" /B C:\Users\YYMY\AppData\Local\Programs\Python\Python313\pythonw.exe app.py %* 2>error.log

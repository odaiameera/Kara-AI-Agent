@echo off
call "%~dp0stop_gateway.cmd"
ping 127.0.0.1 -n 3 >nul
call "%~dp0start_gateway.cmd"

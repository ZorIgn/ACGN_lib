@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden -Wait -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"%~dp0allow-lan.ps1\"'"
if errorlevel 1 pause

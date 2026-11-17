@echo off
rem Usage: run.bat [--dir "D:\Obsidian\Daily"] [--port 8765] [--name NAME]
start "" pythonw "%~dp0dogani.py" %*

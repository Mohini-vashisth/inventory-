@echo off
rem Nightly DB backup, run by Windows Task Scheduler (task: InventoryDbBackup).
cd /d "%~dp0..\inventory"
"%~dp0..\.venv\Scripts\python.exe" manage.py backup_db >> db_backups\backup.log 2>&1

@echo off
rem BUKAN SARAN INVESTASI. Daftarkan tugas "Asisten BEI Harian" di Task Scheduler (SPEC bagian 19.3).
rem Kebijakan eksekusi sistem tidak diubah: hanya skrip ini yang dijalankan dengan -ExecutionPolicy Bypass.
setlocal
cd /d "%~dp0.."
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0daftar_jadwal.ps1"
set KODE=%ERRORLEVEL%
pause
exit /b %KODE%

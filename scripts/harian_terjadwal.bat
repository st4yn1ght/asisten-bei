@echo off
rem BUKAN SARAN INVESTASI. Dipanggil Task Scheduler "Asisten BEI Harian" (SPEC bagian 19.3). Tanpa pause.
rem Semua keluaran konsol (termasuk error impor / venv rusak) ditambahkan ke output\log\terjadwal.log.
setlocal
cd /d "%~dp0.."
if not exist "output\log" mkdir "output\log"
chcp 65001 >nul
set PYTHONUTF8=1
echo Asisten BEI: run harian terjadwal sedang berjalan. Jangan tutup jendela ini.
echo ==== %DATE% %TIME% harian --jadwal ==== >> "output\log\terjadwal.log" 2>&1
if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv tidak ada; jalankan scripts\pasang.bat. BUKAN SARAN INVESTASI. >> "output\log\terjadwal.log" 2>&1
    exit /b 1
)
".venv\Scripts\python.exe" -m asisten_bei harian --jadwal >> "output\log\terjadwal.log" 2>&1
set KODE=%ERRORLEVEL%
echo ==== selesai, kode keluar %KODE% ==== >> "output\log\terjadwal.log" 2>&1
exit /b %KODE%

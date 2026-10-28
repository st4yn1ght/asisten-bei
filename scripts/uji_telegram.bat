@echo off
rem BUKAN SARAN INVESTASI. Kirim pesan uji Telegram (tambahkan --cari-chat-id untuk mencari chat ID) (SPEC bagian 19.1). Jalankan dengan klik ganda.
setlocal
cd /d "%~dp0.."
chcp 65001 >nul
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
    echo Program belum terpasang: klik ganda scripts\pasang.bat terlebih dahulu.
    echo BUKAN SARAN INVESTASI.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m asisten_bei uji-telegram %*
set KODE=%ERRORLEVEL%
echo.
echo Selesai dengan kode keluar %KODE% (0 = OK; arti kode lain: README, bagian "Kode keluar").
pause
exit /b %KODE%

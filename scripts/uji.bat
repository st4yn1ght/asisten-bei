@echo off
rem BUKAN SARAN INVESTASI. Jalankan semua uji otomatis (pytest) (SPEC bagian 19.1). Jalankan dengan klik ganda.
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

".venv\Scripts\python.exe" -m pytest -q %*
set KODE=%ERRORLEVEL%
echo.
if "%KODE%"=="0" (echo Semua uji lulus.) else (echo Ada uji yang gagal: kode %KODE%.)
echo BUKAN SARAN INVESTASI.
pause
exit /b %KODE%

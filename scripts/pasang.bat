@echo off
rem BUKAN SARAN INVESTASI. Pasang program: buat .venv dan pasang dependensi yang dipin (SPEC bagian 19.1 langkah 3).
setlocal
cd /d "%~dp0.."
chcp 65001 >nul
set PYTHONUTF8=1
echo BUKAN SARAN INVESTASI.
set PYVER=
py -3.12 --version >nul 2>&1 && set PYVER=-3.12
if not defined PYVER (py -3.13 --version >nul 2>&1 && set PYVER=-3.13)
if not defined PYVER (
    echo ERROR: Python 3.12 atau 3.13 ^(64-bit^) tidak ditemukan.
    echo Pasang dari python.org dan centang "Add python.exe to PATH" serta "py launcher", lalu ulangi.
    pause
    exit /b 1
)
echo Memakai Python:
py %PYVER% --version
if not exist ".venv\Scripts\python.exe" (
    echo Membuat .venv ...
    py %PYVER% -m venv ".venv"
    if errorlevel 1 (
        echo ERROR: gagal membuat .venv.
        pause
        exit /b 1
    )
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r "requirements.txt"
if errorlevel 1 (
    echo ERROR: gagal memasang dependensi. Periksa koneksi internet lalu ulangi.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install -e . --no-deps
set KODE=%ERRORLEVEL%
echo.
if "%KODE%"=="0" (echo Pemasangan selesai. Langkah berikutnya: isi input\ dan config.yaml, lalu jalankan cek.bat.) else (echo ERROR: pemasangan paket gagal.)
echo BUKAN SARAN INVESTASI.
pause
exit /b %KODE%

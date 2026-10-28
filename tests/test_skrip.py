"""Uji statis berkas operasional Windows dan README (SPEC §19.1 konvensi `.bat`, §19.3 Task Scheduler, §22.5 isi
README).

Berkas `.bat`/`.ps1` hanya berjalan di Windows; di sini konvensinya diperiksa secara tekstual: ASCII + CRLF (dijaga
`.gitattributes`), `cd /d "%~dp0.."` sebagai perintah pertama, `.venv\\Scripts\\python.exe -m asisten_bei` dipanggil
langsung dengan `PYTHONUTF8=1`, kode keluar diteruskan, dan path dikutip. `scripts/info_jadwal.py` (pembantu
`daftar_jadwal.ps1`) dijalankan sungguhan dengan salinan config rilis di `tmp_path`.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from asisten_bei import DISCLAIMER, DISCLAIMER_SINGKAT
from asisten_bei.__main__ import buat_parser
from asisten_bei.laporan.model import GLOSARIUM

AKAR = Path(__file__).resolve().parents[1]
SKRIP = AKAR / "scripts"
README = AKAR / "README.md"
RILIS = AKAR / "tests" / "fixtures" / "config_rilis.yaml"
PYTHON_VENV = '".venv\\Scripts\\python.exe"'

# Berkas .bat manual → subperintah CLI (§19.1, §19.2).
PERINTAH_BAT = {"cek": "cek", "unduh": "unduh", "backtest": "backtest", "harian": "harian", "jurnal": "jurnal",
                "uji_telegram": "uji-telegram"}
SEMUA_BAT = sorted([*PERINTAH_BAT, "harian_terjadwal", "pasang", "uji", "daftar_jadwal"])


def _baris(nama: str) -> list[str]:
    return (SKRIP / nama).read_bytes().decode("ascii").split("\r\n")


def _perintah(nama: str) -> list[str]:
    """Baris perintah tanpa baris kosong dan komentar `rem`."""
    return [b.strip() for b in _baris(nama) if b.strip() and not b.strip().lower().startswith("rem ")]


def _subperintah_cli() -> set[str]:
    sub = next(a for a in buat_parser()._actions if isinstance(a, argparse._SubParsersAction))
    return set(sub.choices)


def test_semua_berkas_skrip_ada():
    ada = {p.name for p in SKRIP.iterdir()}
    assert {f"{n}.bat" for n in SEMUA_BAT} | {"daftar_jadwal.ps1", "info_jadwal.py"} <= ada


def test_setiap_subperintah_cli_punya_bat():
    assert set(PERINTAH_BAT.values()) == _subperintah_cli()


@pytest.mark.parametrize("nama", [f"{n}.bat" for n in SEMUA_BAT] + ["daftar_jadwal.ps1"])
def test_ascii_crlf_dan_disclaimer(nama):
    isi = (SKRIP / nama).read_bytes()
    assert isi.isascii(), nama
    assert isi.count(b"\n") == isi.count(b"\r\n") and isi.endswith(b"\r\n"), f"{nama}: bukan CRLF"
    assert DISCLAIMER_SINGKAT.encode() in isi


def test_gitattributes_menjaga_crlf():
    baris = (AKAR / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "*.bat text eol=crlf" in baris and "*.ps1 text eol=crlf" in baris


@pytest.mark.parametrize("nama", [f"{n}.bat" for n in SEMUA_BAT])
def test_bat_dimulai_dari_folder_proyek(nama):
    p = _perintah(nama)
    assert p[0] == "@echo off"
    awal = [b for b in p[1:] if b.lower() != "setlocal"]
    assert awal[0] == 'cd /d "%~dp0.."', nama
    assert "activate" not in "\n".join(p).lower()


@pytest.mark.parametrize("nama", [n for n in SEMUA_BAT if n != "daftar_jadwal"])
def test_bat_memanggil_python_venv_dengan_utf8(nama):
    p = _perintah(f"{nama}.bat")
    i_utf8 = p.index("set PYTHONUTF8=1")
    panggil = [i for i, b in enumerate(p) if b.startswith(PYTHON_VENV)]
    assert panggil and min(panggil) > i_utf8
    assert p[-1] == "exit /b %KODE%"


@pytest.mark.parametrize("nama", sorted(PERINTAH_BAT))
def test_bat_manual_meneruskan_kode_keluar_lalu_pause(nama):
    p = _perintah(f"{nama}.bat")
    i = p.index(f"{PYTHON_VENV} -m asisten_bei {PERINTAH_BAT[nama]} %*")
    assert p[i + 1] == "set KODE=%ERRORLEVEL%"
    ekor = p[i + 1:]
    assert ekor.index("pause") < ekor.index("exit /b %KODE%") == len(ekor) - 1
    assert 'if not exist ".venv\\Scripts\\python.exe" (' in p  # petunjuk pasang.bat bila .venv belum ada


def test_uji_bat_menjalankan_pytest():
    p = _perintah("uji.bat")
    i = p.index(f"{PYTHON_VENV} -m pytest -q %*")
    assert p[i + 1] == "set KODE=%ERRORLEVEL%" and "pause" in p[i:]


def test_harian_terjadwal():
    p = _perintah("harian_terjadwal.bat")
    i_cd = p.index('cd /d "%~dp0.."')
    assert p[i_cd + 1] == 'if not exist "output\\log" mkdir "output\\log"'
    log = '>> "output\\log\\terjadwal.log" 2>&1'
    i = p.index(f"{PYTHON_VENV} -m asisten_bei harian --jadwal {log}")
    assert p[i + 1] == "set KODE=%ERRORLEVEL%" and p[-1] == "exit /b %KODE%"
    assert not any(re.search(r"(?i)\bpause\b", b) for b in p)
    tampil = [b for b in p if b.lower().startswith("echo ") and not b.endswith(log)]
    assert tampil == ["echo Asisten BEI: run harian terjadwal sedang berjalan. Jangan tutup jendela ini."]
    assert all(b.endswith(log) for b in p if b.startswith(PYTHON_VENV))


def test_pasang_bat():
    teks = "\n".join(_perintah("pasang.bat"))
    for wajib in ("py -3.12 --version", "py -3.13 --version", "tidak ditemukan", 'py %PYVER% -m venv ".venv"',
                  f'{PYTHON_VENV} -m pip install -r "requirements.txt"', f"{PYTHON_VENV} -m pip install -e . --no-deps"):
        assert wajib in teks, wajib


def test_daftar_jadwal_bat_tanpa_mengubah_kebijakan_sistem():
    p = _perintah("daftar_jadwal.bat")
    assert 'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0daftar_jadwal.ps1"' in p
    assert "set KODE=%ERRORLEVEL%" in p and p[-1] == "exit /b %KODE%"
    assert "set-executionpolicy" not in "\n".join(p).lower()


def _ps1() -> str:
    return (SKRIP / "daftar_jadwal.ps1").read_bytes().decode("ascii").replace("\r\n", "\n")


def test_ps1_tugas_sesuai_spec_19_3():
    teks = _ps1()
    for wajib in (
        '$NamaTugas = "Asisten BEI Harian"',
        'New-ScheduledTaskAction -Execute "cmd.exe" -Argument (\'/c "{0}"\' -f $Bat) -WorkingDirectory $Akar',
        '"scripts\\harian_terjadwal.bat"',
        "New-ScheduledTaskTrigger -Weekly -DaysOfWeek $HariLokal -At $Lokal",
        "-StartWhenAvailable", "-AllowStartIfOnBatteries", "-DontStopIfGoingOnBatteries", "-ExecutionTimeLimit $Batas",
        "-MultipleInstances IgnoreNew",
        "New-ScheduledTaskPrincipal -UserId $Pengguna -LogonType Interactive",
        "Register-ScheduledTask", "Get-ScheduledTask -TaskName $NamaTugas", "Get-ScheduledTaskInfo",
        '"SE Asia Standard Time"', "[TimeZoneInfo]::ConvertTime(",
        "PERINGATAN: zona waktu Windows bukan WIB",
        "Wake the computer to run this task",
        '"scripts\\info_jadwal.py"',
    ):
        assert wajib in teks, wajib
    for hari in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"):
        assert f"[DayOfWeek]::{hari}" in teks
    for dilarang in ("Set-ExecutionPolicy", "-Password", "2>&1", "Saturday", "Sunday"):
        assert dilarang not in teks, dilarang  # 2>&1 + ErrorActionPreference Stop menghentikan PowerShell 5.1


def _kurung_seimbang(teks: str) -> bool:
    """Kurung (), {}, [] seimbang di luar string dan komentar PowerShell (cek sintaks kasar; pwsh tidak ada)."""
    pasangan, tumpukan, i = {")": "(", "}": "{", "]": "["}, [], 0
    while i < len(teks):
        c = teks[i]
        if c == "#":
            i = teks.find("\n", i)
            if i < 0:
                break
        elif c in "\"'":
            j = i + 1
            while j < len(teks):
                if c == '"' and teks[j] == "`":
                    j += 2
                    continue
                if teks[j] == c:
                    if j + 1 < len(teks) and teks[j + 1] == c:  # "" atau '' di dalam string
                        j += 2
                        continue
                    break
                j += 1
            else:
                return False
            i = j
        elif c in "({[":
            tumpukan.append(c)
        elif c in ")}]":
            if not tumpukan or tumpukan.pop() != pasangan[c]:
                return False
        i += 1
    return not tumpukan


def test_ps1_kurung_dan_string_seimbang():
    assert _kurung_seimbang(_ps1())
    assert not _kurung_seimbang("if ($a -eq 1 { }")  # pemeriksa sendiri mendeteksi kurung yang hilang
    assert not _kurung_seimbang('Write-Host "tidak ditutup')


def test_ps1_baris_lanjutan_backtick_di_akhir_baris():
    for b in _ps1().split("\n"):
        if "`" in b and not b.lstrip().startswith("#"):
            assert b.endswith(" `"), b  # backtick lanjutan tanpa spasi sesudahnya


def _jalankan_info(cwd: Path, *argumen: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(AKAR / "src"))
    return subprocess.run([sys.executable, str(SKRIP / "info_jadwal.py"), *argumen], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=120)


def test_info_jadwal_mencetak_jam_run_dan_batas_waktu(tmp_path):
    shutil.copy2(RILIS, tmp_path / "config.yaml")
    data = yaml.safe_load(RILIS.read_text(encoding="utf-8"))
    hasil = _jalankan_info(tmp_path, "config.yaml")
    assert hasil.returncode == 0 and hasil.stderr == ""
    jam, batas = hasil.stdout.split()
    assert jam == data["jadwal"]["jam_run"]
    assert float(batas) == float(data["operasional"]["batas_waktu_run_jam"])


@pytest.mark.parametrize("rusak", ["tidak_ada", "jam_salah"])
def test_info_jadwal_config_tidak_valid_kode_3_tanpa_stderr(tmp_path, rusak):
    if rusak == "jam_salah":
        data = yaml.safe_load(RILIS.read_text(encoding="utf-8"))
        data["jadwal"]["jam_run"] = "jam tujuh"
        (tmp_path / "config.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    hasil = _jalankan_info(tmp_path, "config.yaml")
    assert hasil.returncode == 3 and hasil.stderr == ""
    assert hasil.stdout.startswith("ERROR: ")


# --- README (§22.5) -------------------------------------------------------------------------------------------


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _bagian_spec(judul: str) -> str:
    spec = (AKAR / "SPEC.md").read_text(encoding="utf-8")
    awal = spec.index(judul)
    return spec[awal:spec.index("\n## ", awal + 1)]


def test_readme_disclaimer_di_awal_dan_akhir():
    baris = [b for b in _readme().splitlines() if b.strip()]
    assert baris[0] == "# asisten-bei" and baris[1].startswith("> **BUKAN SARAN INVESTASI.**")
    ekor = []
    for b in reversed(baris):
        if not b.startswith(">"):
            break
        ekor.insert(0, b[1:].strip())
    assert " ".join(ekor).replace("**", "") == DISCLAIMER


def test_readme_memuat_glosarium_4_2():
    teks = _readme()
    for istilah, uraian in GLOSARIUM:
        assert f"| {istilah} | {uraian} |" in teks, istilah


def test_readme_tabel_modal_sama_dengan_spec_3():
    spec = _bagian_spec("## 3. Konsekuensi modal Rp 3 juta")
    baris_spec = [b for b in spec.splitlines() if re.match(r"\| \d+% \|", b)]
    assert len(baris_spec) == 5
    readme = _readme()
    for b in baris_spec:
        assert b in readme, b
    assert "Rp 9.985" in readme and "Rp 115 ribu/tahun" in readme


def test_readme_daftar_cek_24_lengkap():
    nomor_spec = re.findall(r"^\| (\d+) \|", _bagian_spec("## 24. Yang harus Anda cek dan isi sendiri"), re.M)
    bagian = _readme().split("## 3. Berkas yang Anda isi sendiri")[1].split("\n## ")[0]
    assert re.findall(r"^\| (\d+) \|", bagian, re.M) == nomor_spec == [str(i) for i in range(1, 15)]


def test_readme_instalasi_jadwal_telegram():
    teks = _readme()
    for n in SEMUA_BAT:
        assert f"{n}.bat" in teks, n
    urutan = [teks.index(f"scripts\\{n}.bat") for n in
              ("pasang", "cek", "unduh", "backtest", "uji_telegram", "harian", "daftar_jadwal")]
    assert urutan == sorted(urutan)  # urutan instalasi §19.1
    for wajib in ("Add python.exe to PATH", "OneDrive", "@BotFather", "--cari-chat-id", ".env.txt",
                  "Wake the computer to run this task", "Asisten BEI Harian", "output\\log\\terjadwal.log"):
        assert wajib in teks, wajib


def test_readme_rutinitas_19_6_dengan_contoh_fiktif():
    spec = _bagian_spec("## 19. Operasional di Windows").split("### 19.6")[1]
    readme = _readme()
    for angka in re.findall(r"\*\*[\d.,]+\*\*", spec):
        assert angka in readme, angka
    assert "`AAAA,2026-10-16,2010,1,PB,1888,,,,`" in spec and "`AAAA,2026-10-16,2010,1,PB,1888,,,,`" in readme


def test_readme_kode_keluar_20_1():
    teks = _readme()
    for kode in range(7):
        assert re.search(rf"^\| {kode} \| ", teks, re.M), kode
    assert "3 > 1 > 5 > 2 > 6 > 4 > 0" in teks


def test_readme_tanpa_kata_trade():
    teks = _readme()
    for m in re.finditer(r"(?i)\btrade\b", teks):
        assert teks[m.end():].startswith(" confirmation"), m.start()  # nama dokumen broker (§19.6)

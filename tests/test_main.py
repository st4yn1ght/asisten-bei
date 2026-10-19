"""Uji CLI `python -m asisten_bei` (`asisten_bei.__main__`; SPEC §19.2 opsi global & perintah, §19.5
penyaring rahasia sebagai langkah pertama, §20.1 kode keluar dan prioritasnya; bagian T-CFG, T-OP, T-LP §22.1).

- Dalam proses: `main(argv, keluar=...)`. Tata letak proyek sementara: config di `konfig/`, berkas input di
  `proyek/` (= `--akar`), folder kerja kosong `kerja/` — sehingga `--config` dan `--akar` yang diabaikan
  langsung terlihat (dan config.yaml pengguna di folder kerja tidak pernah terbaca).
- Satu set uji menjalankan proses sungguhan `[sys.executable, "-m", "asisten_bei", ...]` dengan
  `PYTHONPATH=src` dan `ASISTEN_BEI_WAKTU_SEKARANG`.
- Tanpa jaringan dan tanpa `time.sleep` sungguhan; aliran sys dan `operasional.PENYARING` dipulihkan.

Kode saham dan token di sini FIKTIF – KHUSUS UJI. Waktu default (conftest): 2026-10-15 20:00 WIB → t = 2026-10-15.
"""

from __future__ import annotations

import argparse
import copy
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from asisten_bei import cek as cek_mod
from asisten_bei import config as config_mod
from asisten_bei import operasional
from asisten_bei import unduh as unduh_mod
from asisten_bei.__main__ import main
from asisten_bei.config import baca_yaml
from asisten_bei.data import yfinance_provider as yfp
from asisten_bei.operasional import PenyaringRahasia

AKAR_PROYEK = Path(__file__).resolve().parents[1]
FIXTURES = AKAR_PROYEK / "tests" / "fixtures"
_CONFIG_UJI = baca_yaml(FIXTURES / "config_uji.yaml")

DISCLAIMER_TEKS = "BUKAN SARAN INVESTASI"
FIKTIF = "FIKTIF – KHUSUS UJI"
WAKTU_UJI = "2026-10-15T20:00:00+07:00"
TOKEN_PALSU = "123456789:FIKTIF_tokenUjiMain_qwertyZXCV"
EKOR_TOKEN = "FIKTIF_tokenUjiMain_qwertyZXCV"  # bagian token yang tidak boleh muncul walau sebagian
PERINTAH_TAHAP_BERIKUT = [
    ("backtest",),
    ("backtest", "--strategi", "pb", "--mode", "edge"),
    ("harian",),
    ("harian", "--offline", "--tanggal", "2026-10-15", "--tanpa-notifikasi", "--catat"),
    ("harian", "--jadwal"),
    ("jurnal",),
    ("jurnal", "--ekspor"),
    ("jurnal", "--rekonsiliasi"),
    ("uji-telegram",),
    ("uji-telegram", "--cari-chat-id"),
]


# ---------------------------------------------------------------------------
# Pengaman & tata letak proyek
# ---------------------------------------------------------------------------


class _AksesDilarang(BaseException):
    """Bukan turunan Exception agar tidak tertelan `except Exception` di `main`."""


@pytest.fixture(autouse=True)
def _isolasi(monkeypatch):
    upaya: list[str] = []

    def tolak(nama):
        def _tolak(*args, **kwargs):
            upaya.append(nama)
            raise _AksesDilarang(f"dilarang dalam uji main: {nama}")

        return _tolak

    monkeypatch.setattr(socket.socket, "connect", tolak("socket.connect"))
    monkeypatch.setattr(socket, "create_connection", tolak("socket.create_connection"))
    monkeypatch.setattr(yfp, "_siapkan_yfinance", tolak("yfinance.Ticker asli"))
    monkeypatch.setattr(time, "sleep", tolak("time.sleep"))
    for nama in ("stdout", "stderr", "excepthook"):  # main memasang penyaring di sini (§19.5)
        monkeypatch.setattr(sys, nama, getattr(sys, nama))
    monkeypatch.setattr(operasional, "PENYARING", PenyaringRahasia())
    yield
    assert upaya == [], f"main mencoba mengakses jaringan/tidur: {upaya}"


@dataclass
class Proyek:
    root: Path
    konfig: Path  # folder berisi config (BUKAN akar)
    akar: Path  # folder berisi input_uji/ (= --akar)
    kerja: Path  # folder kerja kosong

    @property
    def config(self) -> Path:
        return self.konfig / "config_uji.yaml"

    def umum(self) -> list[str]:
        return ["--config", str(self.config), "--akar", str(self.akar)]


def data_config(ubah: dict | None = None, hapus: tuple[str, ...] = ()) -> dict:
    data = copy.deepcopy(_CONFIG_UJI)
    for kunci, nilai in (ubah or {}).items():
        *jalur, akhir = kunci.split(".")
        simpul = data
        for k in jalur:
            simpul = simpul[k]
        simpul[akhir] = copy.deepcopy(nilai)
    for kunci in hapus:
        *jalur, akhir = kunci.split(".")
        simpul = data
        for k in jalur:
            simpul = simpul[k]
        del simpul[akhir]
    return data


def tulis_config(path: Path, ubah: dict | None = None, hapus: tuple[str, ...] = ()) -> Path:
    isi = yaml.safe_dump(data_config(ubah, hapus), allow_unicode=True, sort_keys=False)
    path.write_text(f"# {DISCLAIMER_TEKS}. Config uji {FIKTIF}.\n" + isi, encoding="utf-8")
    return path


@pytest.fixture
def proyek(tmp_path, monkeypatch) -> Proyek:
    p = Proyek(tmp_path, tmp_path / "konfig", tmp_path / "proyek", tmp_path / "kerja")
    for folder in (p.konfig, p.akar, p.kerja):
        folder.mkdir()
    shutil.copy2(FIXTURES / "config_uji.yaml", p.config)
    shutil.copytree(FIXTURES / "input_uji", p.akar / "input_uji")
    monkeypatch.chdir(p.kerja)
    return p


@dataclass
class Hasil:
    kode: int
    baris: list[str]
    sistem_exit: bool = False  # main melempar SystemExit (mis. --help) alih-alih mengembalikan kode

    @property
    def teks(self) -> str:
        return "\n".join(self.baris)


def jalankan(argv: list[str]) -> Hasil:
    keluaran: list[str] = []
    try:
        return Hasil(main(argv, keluar=keluaran.append), keluaran)
    except SystemExit as e:
        kode = 0 if e.code is None else e.code
        return Hasil(kode if isinstance(kode, int) else 1, keluaran, sistem_exit=True)


def ada_error(h: Hasil) -> bool:
    return any("ERROR" in b for b in h.baris)


# ---------------------------------------------------------------------------
# Opsi global --config / --akar sebelum atau sesudah perintah (§19.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "susun",
    [
        lambda c, a: ["--config", c, "--akar", a, "cek"],
        lambda c, a: ["cek", "--config", c, "--akar", a],
        lambda c, a: ["--config", c, "cek", "--akar", a],
        lambda c, a: ["--akar", a, "cek", "--config", c],
    ],
    ids=["keduanya_sebelum", "keduanya_sesudah", "config_sebelum_akar_sesudah", "akar_sebelum_config_sesudah"],
)
def test_opsi_global_sebelum_atau_sesudah_perintah(proyek, susun):
    h = jalankan(susun(str(proyek.config), str(proyek.akar)))
    assert h.kode == 0, h.teks
    # Path relatif config diselesaikan terhadap --akar: log ditulis di akar, bukan di folder config/kerja.
    assert list((proyek.akar / "output" / "log").glob("*.log"))
    assert not (proyek.konfig / "output").exists()
    assert not (proyek.kerja / "output").exists()


def test_tanpa_akar_dasar_path_adalah_folder_config(proyek):
    """§19.1: default --akar = folder berisi config, bukan folder kerja. Config di konfig/ tanpa input_uji/
    di sana → input tidak ditemukan → kode 3 (membuktikan --akar pada uji di atas benar-benar dipakai)."""
    h = jalankan(["--config", str(proyek.config), "cek"])
    assert h.kode == 3, h.teks
    shutil.copytree(proyek.akar / "input_uji", proyek.konfig / "input_uji")
    h = jalankan(["cek", "--config", str(proyek.config)])
    assert h.kode == 0, h.teks
    assert list((proyek.konfig / "output" / "log").glob("*.log"))
    assert not (proyek.kerja / "output").exists()


def test_tanpa_config_memakai_config_yaml_di_folder_proyek(proyek, monkeypatch):
    shutil.copy2(proyek.config, proyek.akar / "config.yaml")
    monkeypatch.chdir(proyek.akar)
    h = jalankan(["cek"])
    assert h.kode == 0, h.teks


def test_opsi_global_campur_opsi_perintah(proyek):
    """Opsi perintah (--silang) dan opsi global sesudah perintah bersama-sama; --silang sesudah t → 3."""
    h = jalankan(["cek", "--silang", "AAAA", "2026-10-16", *proyek.umum()])
    assert h.kode == 3, h.teks


# ---------------------------------------------------------------------------
# Kesalahan argumen → kode 3 (bukan kode 2 argparse; kode 2 berarti data belum tersedia, §20.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--config", "{config}"],
        ["--config", "{config}", "--akar", "{akar}"],
    ],
    ids=["kosong", "hanya_config", "hanya_opsi_global"],
)
def test_tanpa_perintah_kode_3_dengan_pesan(proyek, argv):
    argv = [a.format(config=proyek.config, akar=proyek.akar) for a in argv]
    h = jalankan(argv)
    assert h.kode == 3, h.teks
    assert ada_error(h), h.teks
    assert DISCLAIMER_TEKS in h.teks


@pytest.mark.parametrize("perintah", ["hapus", "Cek", "unduh-semua", "--"])
def test_perintah_tidak_dikenal_kode_3(proyek, perintah):
    h = jalankan([*proyek.umum(), perintah])
    assert h.kode == 3, h.teks
    assert ada_error(h), h.teks


@pytest.mark.parametrize(
    "argv, disebut",
    [
        # "{U}" = opsi global (--config ... --akar ...); opsi yang menunggu nilai ditaruh paling akhir.
        (["cek", "--tidak-ada", "{U}"], "--tidak-ada"),
        (["--tidak-ada", "cek", "{U}"], "--tidak-ada"),
        (["cek", "--silang", "AAAA", "{U}"], "--silang"),  # TANGGAL hilang
        (["{U}", "cek", "--silang"], "--silang"),
        (["cek", "lebih", "{U}"], "lebih"),
        (["unduh", "--penuh", "ya", "{U}"], "ya"),
        (["{U}", "unduh", "--simbol"], "--simbol"),
        (["{U}", "harian", "--tanggal"], "--tanggal"),
        (["{U}", "cek", "--config"], "--config"),
    ],
)
def test_opsi_salah_kode_3_dengan_pesan(proyek, argv, disebut):
    lengkap = [x for a in argv for x in (proyek.umum() if a == "{U}" else [a])]
    h = jalankan(lengkap)
    assert h.kode == 3, h.teks
    assert ada_error(h), h.teks
    assert disebut in h.teks, h.teks
    assert DISCLAIMER_TEKS in h.teks


@pytest.mark.parametrize("simbol", ["aaaa", "AAAA.JK", "AA-AA"])
def test_opsi_simbol_tidak_valid_kode_3_tanpa_unduhan(proyek, simbol):
    h = jalankan([*proyek.umum(), "unduh", "--simbol", simbol])
    assert h.kode == 3, h.teks
    assert ada_error(h), h.teks
    assert not (proyek.akar / "data" / "cache").exists()


@pytest.mark.parametrize(
    "argv, termuat",
    [
        (["--help"], ["cek", "unduh", "backtest", "harian", "jurnal", "uji-telegram"]),
        (["-h"], ["cek", "unduh"]),
        (["cek", "--help"], ["--silang"]),
        (["unduh", "--help"], ["--penuh", "--simbol"]),
    ],
)
def test_help_kode_0(proyek, capsys, argv, termuat):
    h = jalankan(argv)
    keluaran = capsys.readouterr().out + h.teks
    assert h.kode == 0, keluaran
    for t in termuat:
        assert t in keluaran, keluaran


# ---------------------------------------------------------------------------
# Config tidak valid → kode 3 dengan kunci disebut (§20.2, T-CFG)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ubah, hapus, kunci",
    [
        ({"versi_config": 2}, (), "versi_config"),
        ({"kunci_asing_uji": 1}, (), "kunci_asing_uji"),
        ({"risiko.kunci_asing_uji": 1}, (), "risiko.kunci_asing_uji"),
        ({}, ("biaya.slippage_pct",), "biaya.slippage_pct"),
        ({"risiko.maks_posisi": "tiga"}, (), "risiko.maks_posisi"),
        ({"biaya.fee_beli_pct": 150}, (), "biaya.fee_beli_pct"),
        ({"skor.bobot": {"sinyal": 40, "kekuatan_relatif": 30, "likuiditas": 15, "jarak_stop": 10}}, (), "skor.bobot"),
    ],
    ids=["versi_2", "kunci_asing_akar", "kunci_asing_bersarang", "kunci_hilang", "tipe_salah", "persen_di_luar",
         "bobot_bukan_100"],
)
def test_config_tidak_valid_kode_3_menyebut_kunci(proyek, ubah, hapus, kunci):
    tulis_config(proyek.config, ubah, hapus)
    h = jalankan([*proyek.umum(), "cek"])
    assert h.kode == 3, h.teks
    assert any(kunci in b for b in h.baris), h.teks
    assert "Traceback" not in h.teks
    assert DISCLAIMER_TEKS in h.teks


def test_config_yaml_rusak_kode_3(proyek):
    proyek.config.write_text("versi_config: [1\nakun: {\n", encoding="utf-8")
    h = jalankan([*proyek.umum(), "cek"])
    assert h.kode == 3, h.teks
    assert ada_error(h)


def test_config_tidak_ditemukan_kode_3_menyebut_path(proyek):
    hilang = proyek.konfig / "tidak_ada.yaml"
    h = jalankan(["--config", str(hilang), "--akar", str(proyek.akar), "cek"])
    assert h.kode == 3, h.teks
    assert "tidak_ada.yaml" in h.teks


@pytest.mark.parametrize("perintah", [("cek",), ("unduh",), ("unduh", "--penuh"), ("backtest",), ("harian",),
                                      ("jurnal",), ("uji-telegram",)])
def test_config_tidak_valid_kode_3_untuk_setiap_perintah(proyek, perintah):
    """§20.1 prioritas 3 > 1: config tidak valid mengalahkan "belum tersedia" (1); unduh tidak mengunduh."""
    tulis_config(proyek.config, {"versi_config": 2})
    h = jalankan([*proyek.umum(), *perintah])
    assert h.kode == 3, h.teks
    assert any("versi_config" in b for b in h.baris), h.teks
    assert not (proyek.akar / "data").exists()


# ---------------------------------------------------------------------------
# Perintah tahap berikutnya (§19.2, §23): "belum tersedia", kode 1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("perintah", PERINTAH_TAHAP_BERIKUT, ids=lambda p: "_".join(p).replace("-", ""))
def test_perintah_tahap_berikut_belum_tersedia_kode_1(proyek, perintah):
    h = jalankan([*proyek.umum(), *perintah])
    assert h.kode == 1, h.teks
    assert "belum tersedia" in h.teks.lower(), h.teks
    assert DISCLAIMER_TEKS in h.teks


# ---------------------------------------------------------------------------
# Penyaring rahasia: langkah PERTAMA (§19.5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv_fn",
    [
        lambda p: [*p.umum(), "cek"],
        lambda p: [*p.umum(), "cek", "--tidak-ada"],
        lambda p: [],
        lambda p: ["--config", str(p.konfig / "tidak_ada.yaml"), "cek"],
        lambda p: [*p.umum(), "backtest"],
    ],
    ids=["cek_valid", "opsi_salah", "tanpa_perintah", "config_hilang", "belum_tersedia"],
)
def test_penyaring_rahasia_dipasang_sebelum_apa_pun(proyek, monkeypatch, argv_fn):
    kejadian: list[str] = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN_PALSU)

    asli_pasang = operasional.pasang_penyaring_rahasia

    def pasang():
        kejadian.append("penyaring")
        asli_pasang()

    def mata(nama, fungsi):
        def _mata(*args, **kwargs):
            kejadian.append(nama)
            return fungsi(*args, **kwargs)

        return _mata

    monkeypatch.setattr(operasional, "pasang_penyaring_rahasia", pasang)
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", mata("parse_args", argparse.ArgumentParser.parse_args))
    monkeypatch.setattr(config_mod, "muat_config", mata("muat_config", config_mod.muat_config))
    monkeypatch.setattr(operasional, "siapkan_log", mata("siapkan_log", operasional.siapkan_log))

    def keluar(s: str) -> None:
        # Saat keluaran pertama, token dari env sudah terdaftar di penyaring.
        kejadian.append("keluar" if operasional.PENYARING.sensor(TOKEN_PALSU) == "***" else "keluar_TANPA_PENYARING")

    try:
        main(argv_fn(proyek), keluar=keluar)
    except SystemExit:
        pass
    assert kejadian and kejadian[0] == "penyaring", kejadian
    assert "keluar_TANPA_PENYARING" not in kejadian
    assert "keluar" in kejadian


def test_main_memasang_penyaring_di_stdout_stderr_dan_excepthook(proyek, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN_PALSU)
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    kode = main([*proyek.umum(), "cek"])  # keluar default: print ke sys.stdout
    assert kode == 0
    print(f"sesudah main {TOKEN_PALSU}")
    sys.stderr.write(f"galat {TOKEN_PALSU}\n")
    try:
        raise RuntimeError(f"https://api.telegram.org/bot{TOKEN_PALSU}/getMe")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())
    out, err = capsys.readouterr()
    assert DISCLAIMER_TEKS in out
    assert TOKEN_PALSU not in out + err and EKOR_TOKEN not in out + err
    assert "sesudah main ***" in out
    assert "galat ***" in err
    assert "bot***/getMe" in err


# ---------------------------------------------------------------------------
# Error tak terduga → kode 1, pesan tercetak, token tidak bocor (§20.1, §19.5, T-OP)
# ---------------------------------------------------------------------------


def _meledak(*args, **kwargs):
    url = urllib.parse.quote(TOKEN_PALSU, safe="")
    try:
        raise ConnectionError(f"Failed to connect https://api.telegram.org/bot{url}/sendMessage")
    except ConnectionError as e:
        raise RuntimeError(f"kirim gagal: token {TOKEN_PALSU} ditolak") from e


@pytest.mark.parametrize(
    "modul, nama, perintah",
    [(cek_mod, "jalankan_cek", ("cek",)), (cek_mod, "jalankan_cek", ("cek", "--silang", "AAAA", "2026-10-15")),
     (unduh_mod, "jalankan_unduh", ("unduh",))],
    ids=["cek", "cek_silang", "unduh"],
)
def test_error_tak_terduga_kode_1_tanpa_membocorkan_token(proyek, monkeypatch, capsys, modul, nama, perintah):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN_PALSU)
    monkeypatch.setattr(modul, nama, _meledak)
    kode = main([*proyek.umum(), *perintah])  # keluar default: print (lewat penyaring sys.stdout)
    out, err = capsys.readouterr()
    log = "".join(p.read_text(encoding="utf-8") for p in (proyek.akar / "output" / "log").glob("*.log"))
    assert kode == 1, out + err
    gabungan = out + err + log
    for bocor in (TOKEN_PALSU, EKOR_TOKEN, urllib.parse.quote(TOKEN_PALSU, safe=""), urllib.parse.quote(TOKEN_PALSU)):
        assert bocor not in gabungan, f"token bocor ({bocor!r})"
    # Pesannya tetap tercetak (tersensor) dan tercatat di log; disclaimer tetap ada.
    assert "ERROR" in out and "RuntimeError" in out and "***" in out, out
    assert "RuntimeError" in log
    assert DISCLAIMER_TEKS in out


# ---------------------------------------------------------------------------
# Proses sungguhan: python -m asisten_bei (sys.exit(main()))
# ---------------------------------------------------------------------------


def jalankan_proses(argv: list[str], cwd: Path, **env_tambahan: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("TELEGRAM_")}
    env["ASISTEN_BEI_WAKTU_SEKARANG"] = WAKTU_UJI
    env["PYTHONPATH"] = str(AKAR_PROYEK / "src")
    env["PYTHONUTF8"] = "1"
    env.update(env_tambahan)
    return subprocess.run(
        [sys.executable, "-m", "asisten_bei", *argv], cwd=cwd, env=env, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=300,
    )


def test_proses_sungguhan_cek_kode_0_dengan_disclaimer(akar_uji, tmp_path_factory):
    kerja = tmp_path_factory.mktemp("kerja_proses")
    hasil = jalankan_proses(["--config", str(akar_uji / "config_uji.yaml"), "--akar", str(akar_uji), "cek"], kerja)
    assert hasil.returncode == 0, hasil.stdout + hasil.stderr
    assert DISCLAIMER_TEKS in hasil.stdout
    assert "Traceback" not in hasil.stderr
    assert list((akar_uji / "output" / "log").glob("*.log"))  # log di --akar
    assert not (kerja / "output").exists()


@pytest.mark.parametrize(
    "argv, kode",
    [
        (["--help"], 0),
        ([], 3),
        (["hapus"], 3),
        (["cek", "--tidak-ada"], 3),
        (["backtest"], 1),
    ],
    ids=["help", "tanpa_perintah", "perintah_asing", "opsi_salah", "belum_tersedia"],
)
def test_proses_sungguhan_kode_keluar(akar_uji, tmp_path_factory, argv, kode):
    kerja = tmp_path_factory.mktemp("kerja_proses")
    umum = ["--config", str(akar_uji / "config_uji.yaml"), "--akar", str(akar_uji)] if argv else []
    hasil = jalankan_proses([*umum, *argv], kerja)
    assert hasil.returncode == kode, hasil.stdout + hasil.stderr


def test_proses_sungguhan_error_tak_terduga_token_tersensor(tmp_path):
    """Error sungguhan (folder log tidak bisa dibuat: `output/log` berupa berkas) yang pesannya memuat token
    (token ada di path akar) → kode 1; stdout/stderr/traceback tersensor (§19.5, T-OP)."""
    akar = tmp_path / f"proyek-{TOKEN_PALSU}"
    akar.mkdir()
    shutil.copy2(FIXTURES / "config_uji.yaml", akar / "config_uji.yaml")
    shutil.copytree(FIXTURES / "input_uji", akar / "input_uji")
    (akar / "output").mkdir()
    (akar / "output" / "log").write_text("bukan folder\n", encoding="utf-8")
    kerja = tmp_path / "kerja"
    kerja.mkdir()
    hasil = jalankan_proses(["--config", str(akar / "config_uji.yaml"), "--akar", str(akar), "cek"], kerja,
                            TELEGRAM_BOT_TOKEN=TOKEN_PALSU)
    gabungan = hasil.stdout + hasil.stderr
    assert hasil.returncode == 1, gabungan
    assert TOKEN_PALSU not in gabungan and EKOR_TOKEN not in gabungan, gabungan
    assert "***" in gabungan
    assert DISCLAIMER_TEKS in hasil.stdout

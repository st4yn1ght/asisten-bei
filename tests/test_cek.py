"""Uji perintah `cek` (SPEC §19.2 "cek" dan "cek --silang", §6 aturan umum CSV, §6.5–§6.7, §7 "Resolusi
rujukan", §8.2, §8.4, §8.5, §8.6, §8.7, §16.4, §19.3, §19.4, §20.1, §24 butir 6–10; bagian T-INP, T-OP,
T-LP, T-KL, T-SP §22.1).

- Proyek uji = salinan `tests/fixtures/` di `tmp_path` (`akar_uji`); config diubah dengan menulis ulang
  `config_uji.yaml` salinan itu (kunci bertitik), berkas input ditulis ulang langsung.
- Kode keluar dinilai lewat `__main__.main([... "cek" ...])` (antarmuka SPEC §19.2/§20.1); beberapa uji
  memanggil `cek.jalankan_cek(ctx, silang_kode=..., keluar=...)` langsung.
- Cache yfinance (format §8.3) ditulis lewat `CacheProvider.tulis`; berkas CSV manual format §6.9.
- Tanpa jaringan: socket, `requests`, dan `yfinance.Ticker` asli digagalkan; `time.sleep` dilarang.

Semua kode saham, harga, volume, tanggal libur, jam sesi, tabel fraksi, dan batas ARA/ARB di berkas ini
FIKTIF – KHUSUS UJI (bukan aturan BEI). Waktu default (conftest): Kamis 2026-10-15 20:00 WIB, sesudah
`jadwal.jam_run` 19:30 → t = 2026-10-15.
"""

from __future__ import annotations

import copy
import hashlib
import re
import socket
import sys
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
import yaml

from asisten_bei import operasional, waktu
from asisten_bei.__main__ import main
from asisten_bei.cek import jalankan_cek
from asisten_bei.config import KonteksApp, baca_yaml, validasi_dict
from asisten_bei.data import yfinance_provider as yfp
from asisten_bei.data.cache import CacheProvider, buat_meta
from asisten_bei.operasional import PenyaringRahasia, kunci_proses

FIXTURES = Path(__file__).resolve().parent / "fixtures"
_CONFIG_UJI = baca_yaml(FIXTURES / "config_uji.yaml")

FIKTIF = "FIKTIF – KHUSUS UJI"
T = date(2026, 10, 15)  # Kamis; t pada waktu default
DISCLAIMER_TEKS = "BUKAN SARAN INVESTASI"
# Pesan khusus SPEC §6 (disalin dari SPEC, bukan diimpor dari src).
PESAN_REGIONAL_SPEC = (
    "Berkas tampaknya disimpan Excel dengan format regional Indonesia. "
    "Simpan ulang sebagai CSV UTF-8 (koma) atau edit dengan Notepad."
)
# §19.2 butir 3 / §24 butir 6–10.
ITEM_VERIFIKASI = ("pasar.jadwal_sesi", "pasar.fraksi_harga", "pasar.auto_reject", "biaya", "data.pengali_volume")
BREADTH_FIXTURE = ("AAAA", "BBBB", "CCCC", "DDDD")  # tests/fixtures/input_uji/breadth_universe.csv
HEADER_PORTO = "kode,tanggal_beli,harga_beli,lot,strategi,stop_awal,sektor,harga_per_tanggal,lembar,catatan\n"
HEADER_LIBUR = "tanggal,keterangan\n"
LIBUR_FIXTURE = f"2026-12-25,{FIKTIF}\n2027-01-01,{FIKTIF}\n"
HEADER_HOLDOUT = "waktu,holdout_mulai,hash_aturan,hash_input,strategi,mode,pf_r_holdout,n_holdout\n"
TOKEN_PALSU = "987654321:FIKTIF_tokenUjiCek_abcdefXYZ"

# --silang: seri AAAA dengan split 1:5 (rasio 5 = lembar baru per lembar lama) pada S, sudah disesuaikan.
S_SPLIT = date(2026, 6, 1)  # Senin
D_SILANG = date(2026, 3, 2)  # Senin, sebelum split → F = 5
BAR_D = {"open": 202.0, "high": 212.0, "low": 196.0, "close": 208.0, "volume": 6_000_000.0}
BAR_LAIN = {"open": 200.0, "high": 210.0, "low": 190.0, "close": 204.0, "volume": 5_000_000.0}
PENGALI_SILANG = 100


# ---------------------------------------------------------------------------
# Pengaman: tanpa jaringan, tanpa tidur sungguhan, aliran sys dan penyaring dipulihkan
# ---------------------------------------------------------------------------


class _AksesDilarang(BaseException):
    """Bukan turunan Exception agar tidak tertelan `except Exception` di `main`."""


@pytest.fixture(autouse=True)
def _isolasi(monkeypatch, tmp_path_factory):
    upaya: list[str] = []

    def tolak(nama):
        def _tolak(*args, **kwargs):
            upaya.append(nama)
            raise _AksesDilarang(f"dilarang dalam uji cek: {nama}")

        return _tolak

    import requests

    monkeypatch.setattr(socket.socket, "connect", tolak("socket.connect"))
    monkeypatch.setattr(socket, "create_connection", tolak("socket.create_connection"))
    monkeypatch.setattr(requests.Session, "request", tolak("requests"))
    monkeypatch.setattr(yfp, "_siapkan_yfinance", tolak("yfinance.Ticker asli"))
    monkeypatch.setattr(time, "sleep", tolak("time.sleep"))
    # `main` memasang penyaring rahasia pada aliran sys (§19.5): pulihkan sesudah uji.
    for nama in ("stdout", "stderr", "excepthook"):
        monkeypatch.setattr(sys, nama, getattr(sys, nama))
    monkeypatch.setattr(operasional, "PENYARING", PenyaringRahasia())
    # Folder kerja kosong: config.yaml pengguna di folder kerja tidak pernah terbaca.
    monkeypatch.chdir(tmp_path_factory.mktemp("kerja"))
    yield
    assert upaya == [], f"cek mencoba mengakses jaringan/tidur: {upaya}"


# ---------------------------------------------------------------------------
# Pembantu: config, input, cache, CSV manual, menjalankan cek
# ---------------------------------------------------------------------------


def data_config(ubah: dict | None = None) -> dict:
    data = copy.deepcopy(_CONFIG_UJI)
    for kunci, nilai in (ubah or {}).items():
        *jalur, akhir = kunci.split(".")
        simpul = data
        for k in jalur:
            simpul = simpul[k]
        simpul[akhir] = copy.deepcopy(nilai)
    return data


def tulis_config(akar: Path, ubah: dict | None = None) -> Path:
    path = akar / "config_uji.yaml"
    isi = yaml.safe_dump(data_config(ubah), allow_unicode=True, sort_keys=False)
    path.write_text(f"# {DISCLAIMER_TEKS}. Config uji {FIKTIF}.\n" + isi, encoding="utf-8")
    return path


def konteks(akar: Path, ubah: dict | None = None) -> KonteksApp:
    akar = Path(akar).resolve()
    return KonteksApp(cfg=validasi_dict(data_config(ubah)), akar=akar, path_config=akar / "config_uji.yaml")


def tulis_input(akar: Path, nama: str, isi: str, encoding: str = "utf-8") -> None:
    (akar / "input_uji" / nama).write_text(isi, encoding=encoding)


def tulis_libur(akar: Path, *baris: str) -> None:
    tulis_input(akar, "holidays.csv", HEADER_LIBUR + "".join(f"{b},{FIKTIF}\n" for b in baris))


def bingkai(mulai: str, akhir: str, *, kecuali=(), **nilai) -> pd.DataFrame:
    """Bar Senin–Jumat (libur tidak dibuang: data mentah) dengan nilai konstan `BAR_LAIN` (+ `nilai`)."""
    idx = pd.bdate_range(mulai, akhir)
    if kecuali:
        idx = idx[~idx.isin(pd.to_datetime([str(d) for d in kecuali]))]
    kolom = {**BAR_LAIN, **nilai}
    return pd.DataFrame(
        {k: float(kolom[k]) for k in ("open", "high", "low", "close", "volume")},
        index=pd.DatetimeIndex(idx, name="tanggal").as_unit("ns"),
    )


def tulis_cache(akar: Path, kode: str, df: pd.DataFrame, *, split: dict | None = None) -> None:
    bar = df.copy()
    bar["sumber"] = "yfinance"
    bar["split"] = 0.0
    for d, r in (split or {}).items():
        assert pd.Timestamp(d) in bar.index, "split uji harus jatuh pada tanggal bar"
        bar.loc[pd.Timestamp(d), "split"] = float(r)
    folder = akar / _CONFIG_UJI["data"]["folder_cache"] / "yfinance"
    CacheProvider(folder).tulis(kode, bar, buat_meta(bar, sekarang=waktu.sekarang(), versi_provider="palsu-1.0"))


def tulis_manual(akar: Path, kode: str, df: pd.DataFrame) -> None:
    folder = akar / _CONFIG_UJI["data"]["folder_manual"]
    folder.mkdir(parents=True, exist_ok=True)
    baris = ["tanggal,open,high,low,close,volume"]
    for ts, r in df.iterrows():
        baris.append(f"{ts.date().isoformat()},{r['open']:g},{r['high']:g},{r['low']:g},{r['close']:g},{r['volume']:.0f}")
    (folder / f"{kode}.csv").write_text("\n".join(baris) + "\n", encoding="utf-8")


@dataclass
class Hasil:
    kode: int
    baris: list[str]

    @property
    def teks(self) -> str:
        return "\n".join(self.baris)

    def dengan(self, *potongan: str, kecil: bool = False) -> list[str]:
        """Baris yang memuat semua potongan (kecil=True: tanpa membedakan huruf besar/kecil)."""
        if kecil:
            return [b for b in self.baris if all(p.lower() in b.lower() for p in potongan)]
        return [b for b in self.baris if all(p in b for p in potongan)]

    def ada(self, *potongan: str, kecil: bool = False) -> bool:
        return bool(self.dengan(*potongan, kecil=kecil))

    def error(self) -> list[str]:
        """Baris yang diawali ERROR (bukan baris ringkasan "0 ERROR")."""
        return [b for b in self.baris if re.match(r"^\s*ERROR\b", b)]

    def indeks(self, potongan: str, kecil: bool = True) -> int:
        for i, b in enumerate(self.baris):
            if (potongan.lower() in b.lower()) if kecil else (potongan in b):
                return i
        raise AssertionError(f"{potongan!r} tidak ada di keluaran cek:\n{self.teks}")


def cek(akar: Path, *opsi: str) -> Hasil:
    """`python -m asisten_bei --config <akar>/config_uji.yaml --akar <akar> cek <opsi>` di dalam proses."""
    keluaran: list[str] = []
    kode = main(["--config", str(akar / "config_uji.yaml"), "--akar", str(akar), "cek", *opsi], keluar=keluaran.append)
    # Path tmp memuat nama uji: ganti dengan penanda agar pencarian teks tidak salah cocok.
    path = sorted({str(akar.resolve()), str(akar)}, key=len, reverse=True)
    baris = []
    for b in keluaran:
        for p in path:
            b = b.replace(p, "<AKAR>")
        baris.append(b)
    return Hasil(kode, baris)


def silang(akar: Path, kode: str, tanggal: str | date) -> Hasil:
    return cek(akar, "--silang", kode, str(tanggal))


_ANGKA = r"\d+(?:\.\d{3})*(?:,\d+)?"


def ke_angka(s: str) -> Decimal:
    """Angka format Indonesia ("1.010,00", "120.000.000") → Decimal."""
    return Decimal(s.replace(".", "").replace(",", "."))


def nilai_label(baris: str, label: str) -> Decimal:
    m = re.search(rf"\b{label}\b\s*:?\s*(?:Rp\s*)?({_ANGKA})", baris)
    assert m, f"label {label!r} tanpa angka di baris: {baris!r}"
    return ke_angka(m.group(1))


def semua_angka(baris: str) -> set[Decimal]:
    return {ke_angka(m) for m in re.findall(_ANGKA, baris)}


def potret(akar: Path) -> dict[str, str]:
    """Semua berkas (isi sha256) dan folder di bawah `akar`."""
    hasil = {}
    for p in sorted(akar.rglob("*")):
        rel = p.relative_to(akar).as_posix()
        hasil[rel] = "<folder>" if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest()
    return hasil


# ---------------------------------------------------------------------------
# Fixture valid: kode 0, bagian berurutan (§19.2), BELUM DIVERIFIKASI, disclaimer
# ---------------------------------------------------------------------------


def test_fixture_valid_kode_0_tanpa_error(akar_uji):
    h = cek(akar_uji)
    assert h.kode == 0, h.teks
    assert h.error() == []
    assert h.ada("valid", kecil=True)  # bagian 1: config valid


def test_bagian_cek_berurutan_sesuai_spec(akar_uji):
    """§19.2: config → input → BELUM DIVERIFIKASI → holidays/kalender → holdout_mulai → .env → resolusi."""
    h = cek(akar_uji)
    urutan = [
        h.indeks("BELUM DIVERIFIKASI", kecil=False),
        h.indeks("kalender"),
        h.indeks("holdout_mulai"),
        h.indeks(".env"),
        h.indeks("realistis"),
    ]
    assert urutan == sorted(urutan), h.teks
    assert h.ada("holdout_mulai", "2024-10-01")  # nilai config_uji dicetak


@pytest.mark.parametrize("item", ITEM_VERIFIKASI)
def test_item_belum_diverifikasi_dicantumkan(akar_uji, item):
    h = cek(akar_uji)
    assert h.ada("BELUM DIVERIFIKASI", item), f"{item} tidak tercantum sebagai BELUM DIVERIFIKASI:\n{h.teks}"
    assert h.kode == 0  # item belum diverifikasi tidak mengubah kode keluar


@pytest.mark.parametrize(
    "siapkan",
    ["valid", "input_error", "silang_error", "silang_lewat_t", "peringatan"],
)
def test_keluaran_selalu_memuat_disclaimer(akar_uji, siapkan):
    """T-LP: terminal semua perintah memuat "BUKAN SARAN INVESTASI"."""
    opsi: tuple[str, ...] = ()
    if siapkan == "input_error":
        tulis_input(akar_uji, "universe.csv", "kode,nama,sektor\nAAAA,Satu,Keuangan\nAAAA,Dua,Energi\n")
    elif siapkan == "silang_error":
        opsi = ("--silang", "aaaa", "2026-03-02")
    elif siapkan == "silang_lewat_t":
        opsi = ("--silang", "AAAA", "2026-10-16")
    elif siapkan == "peringatan":
        (akar_uji / ".env.txt").write_text("x=1\n", encoding="utf-8")
    h = cek(akar_uji, *opsi)
    assert DISCLAIMER_TEKS in h.teks


# ---------------------------------------------------------------------------
# Input (§6, §6.5, §6.6, T-INP): ERROR → 3, PERINGATAN saja → 0
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "berkas, isi",
    [
        ("universe.csv", "kode,nama,sektor\nAAAA,Satu,Keuangan\nAAAA,Dua,Energi\n"),  # kode ganda
        ("universe.csv", "kode,nama\nAAAA,Satu\n"),  # kolom sektor wajib hilang
        ("portfolio.csv", HEADER_PORTO + "AAAA,2026-10-08,1500,0,PB,1400,,,,\n"),  # lot < 1
        ("portfolio.csv", HEADER_PORTO + "AAAA,2026-10-08,1500,1,XX,1400,,,,\n"),  # strategi bukan enum
        ("portfolio.csv", HEADER_PORTO + "AAAA,2026-10-16,1500,1,PB,1400,,,,\n"),  # tanggal_beli > t
        ("portfolio.csv", HEADER_PORTO + "AAAA,2026-10-10,1500,1,PB,1400,,,,\n"),  # Sabtu
        ("portfolio.csv", HEADER_PORTO + "AAAA,2026-12-25,1500,1,PB,1400,,,,\n"),  # libur & > t
    ],
    ids=["kode_ganda", "kolom_hilang", "lot_nol", "strategi_asing", "beli_sesudah_t", "beli_sabtu", "beli_libur"],
)
def test_input_error_kode_3(akar_uji, berkas, isi):
    tulis_input(akar_uji, berkas, isi)
    h = cek(akar_uji)
    assert h.kode == 3, h.teks
    assert any(berkas in b for b in h.error()), h.teks


def test_tanggal_beli_sama_dengan_t_sah(akar_uji):
    tulis_input(akar_uji, "portfolio.csv", HEADER_PORTO + "AAAA,2026-10-15,1500,1,PB,1400,,,,\n")
    assert cek(akar_uji).kode == 0


def test_tanggal_beli_t_sebelum_batas_bar_final_error(akar_uji, atur_waktu):
    """19:00 < jam_run 19:30 → t = 2026-10-14, jadi tanggal_beli 2026-10-15 > t."""
    atur_waktu("2026-10-15T19:00:00+07:00")
    tulis_input(akar_uji, "portfolio.csv", HEADER_PORTO + "AAAA,2026-10-15,1500,1,PB,1400,,,,\n")
    h = cek(akar_uji)
    assert h.kode == 3
    assert h.ada("2026-10-14")  # t yang dipakai tercantum


def test_tanggal_beli_pada_libur_holidays_error(akar_uji):
    tulis_libur(akar_uji, "2026-10-08", "2026-12-25", "2027-01-01")
    tulis_input(akar_uji, "portfolio.csv", HEADER_PORTO + "AAAA,2026-10-08,1500,1,PB,1400,,,,\n")
    h = cek(akar_uji)
    assert h.kode == 3
    assert any("portfolio.csv" in b and "2026-10-08" in b for b in h.error()), h.teks


@pytest.mark.parametrize(
    "berkas, isi",
    [
        ("universe.csv", "kode;nama;sektor\nAAAA;Satu (FIKTIF);Keuangan\n"),
        ("exclude.csv", "kode;alasan;tanggal_mulai;tanggal_akhir\n"),
        ("holidays.csv", f"tanggal;keterangan\n2026-12-25;{FIKTIF}\n2027-01-01;{FIKTIF}\n"),
        ("portfolio.csv", HEADER_PORTO + 'AAAA,2026-10-08,"1500,5",1,PB,1400,,,,\n'),  # koma desimal
        ("portfolio.csv", HEADER_PORTO + "AAAA,2026-10-08,1500,5,1,PB,1400,,,,\n"),  # koma desimal tak berkutip
        ("portfolio.csv", HEADER_PORTO + "AAAA,08/10/2026,1500,1,PB,1400,,,,\n"),  # format tanggal lain
        ("holidays.csv", f"tanggal,keterangan\n25/12/2026,{FIKTIF}\n2027-01-01,{FIKTIF}\n"),
    ],
    ids=["universe_titik_koma", "exclude_titik_koma", "libur_titik_koma", "koma_desimal_berkutip",
         "koma_desimal_tanpa_kutip", "tanggal_dd_mm_yyyy", "libur_dd_mm_yyyy"],
)
def test_format_regional_indonesia_error_dengan_pesan_khusus(akar_uji, berkas, isi):
    """§6, T-INP: pemisah `;`, koma desimal, atau tanggal bukan YYYY-MM-DD → ERROR berpesan khusus."""
    tulis_input(akar_uji, berkas, isi)
    h = cek(akar_uji)
    assert h.kode == 3, h.teks
    assert any(berkas in b and PESAN_REGIONAL_SPEC in b for b in h.error()), h.teks


def test_bom_excel_diterima(akar_uji):
    tulis_input(akar_uji, "universe.csv", "kode,nama,sektor\nAAAA,Satu (FIKTIF),Keuangan\n", encoding="utf-8-sig")
    tulis_input(akar_uji, "portfolio.csv", HEADER_PORTO + "AAAA,2026-10-08,1500,1,PB,1400,,,,\n", encoding="utf-8-sig")
    h = cek(akar_uji)
    assert h.kode == 0, h.teks
    assert h.error() == []


@pytest.mark.parametrize(
    "baris, kode_harap",
    [
        ("ZZZZ,2026-10-08,1500,1,MANUAL,1400,,,,", 3),  # di luar universe, sektor kosong → ERROR
        ("ZZZZ,2026-10-08,1500,1,MANUAL,1400,Properti,,,", 0),  # di luar universe, sektor diisi
        ("AAAA,2026-10-08,1500,1,PB,1400,,,,", 0),  # di universe: sektor boleh kosong
    ],
    ids=["luar_universe_tanpa_sektor", "luar_universe_bersektor", "dalam_universe_tanpa_sektor"],
)
def test_sektor_portofolio_wajib_bila_di_luar_universe(akar_uji, baris, kode_harap):
    tulis_input(akar_uji, "portfolio.csv", HEADER_PORTO + baris + "\n")
    h = cek(akar_uji)
    assert h.kode == kode_harap, h.teks
    galat_sektor = [b for b in h.error() if "portfolio.csv" in b and "sektor" in b]
    assert bool(galat_sektor) == (kode_harap == 3), h.teks


@pytest.mark.parametrize(
    "stop, peringatan",
    [("1499", False), ("1500", True), ("1600", True)],
    ids=["stop_di_bawah", "stop_sama", "stop_di_atas"],
)
def test_stop_awal_tidak_di_bawah_harga_beli_hanya_peringatan(akar_uji, stop, peringatan):
    """§6.5: stop_awal ≥ harga_beli → PERINGATAN (bukan ERROR), kode 0."""
    tulis_input(akar_uji, "portfolio.csv", HEADER_PORTO + f"AAAA,2026-10-08,1500,1,PB,{stop},,,,\n")
    h = cek(akar_uji)
    assert h.kode == 0, h.teks
    assert h.error() == []
    assert h.ada("PERINGATAN", "stop_awal") == peringatan, h.teks


@pytest.mark.parametrize(
    "diperbarui, peringatan",
    [("2026-10-08", False), ("2026-10-07", True)],
    ids=["lima_hari_bursa", "enam_hari_bursa"],
)
def test_umur_akun_hanya_peringatan(akar_uji, diperbarui, peringatan):
    """§6.6: lebih tua dari maks_umur_hari_bursa (5) hari bursa → peringatan saja; kode tetap 0."""
    tulis_input(akar_uji, "akun.yaml", f"kas_idr: 3000000\ndiperbarui: {diperbarui}\n")
    h = cek(akar_uji)
    assert h.kode == 0, h.teks
    assert h.ada("PERINGATAN", "akun.yaml") == peringatan, h.teks


def test_error_dan_peringatan_bersamaan_kode_3(akar_uji):
    tulis_input(akar_uji, "portfolio.csv", HEADER_PORTO + "AAAA,2026-10-08,1500,1,PB,1600,,,,\n"
                "ZZZZ,2026-10-08,900,1,MANUAL,800,,,,\n")
    (akar_uji / ".env.txt").write_text("x=1\n", encoding="utf-8")
    h = cek(akar_uji)
    assert h.kode == 3
    assert h.ada("PERINGATAN", "stop_awal")  # pemeriksaan berlanjut walau ada ERROR input


def test_error_input_tercantum_sebelum_belum_diverifikasi(akar_uji):
    tulis_input(akar_uji, "universe.csv", "kode,nama,sektor\nAAAA,Satu,Keuangan\nAAAA,Dua,Energi\n")
    h = cek(akar_uji)
    galat = [i for i, b in enumerate(h.baris) if re.match(r"^\s*ERROR\b", b) and "universe.csv" in b]
    assert galat and galat[0] < h.indeks("BELUM DIVERIFIKASI", kecil=False)


def test_config_error_menghentikan_pemeriksaan(akar_uji):
    """§19.2 butir 1: ERROR config menghentikan pemeriksaan (input tidak dinilai), kode 3, kunci disebut."""
    tulis_config(akar_uji, {"versi_config": 2})
    tulis_input(akar_uji, "universe.csv", "kode,nama,sektor\nAAAA,Satu,Keuangan\nAAAA,Dua,Energi\n")
    h = cek(akar_uji)
    assert h.kode == 3
    assert h.ada("versi_config")
    assert not h.ada("universe.csv")
    assert not h.ada("BELUM DIVERIFIKASI", "pasar.fraksi_harga")
    assert DISCLAIMER_TEKS in h.teks


# ---------------------------------------------------------------------------
# Item terverifikasi (FIKTIF) beserta sumber & tanggal berlaku (§19.2 butir 3)
# ---------------------------------------------------------------------------

SUMBER_JADWAL = f"{FIKTIF}: jadwal-uji-71"
SUMBER_FRAKSI = f"{FIKTIF}: fraksi-uji-72"
SUMBER_ARA = f"{FIKTIF}: ara-uji-73"


def jadwal_fiktif() -> dict:
    # Jam FIKTIF (bukan jadwal BEI): batas bar final 16:07 + 60 menit = 17:07 < 20:00 → t = 2026-10-15.
    return {"terverifikasi": True, "sumber": SUMBER_JADWAL, "tanggal_cek": "2026-10-01",
            "jam_penutupan": "16:07", "jam_pra_pembukaan": "08:13"}


def fraksi_fiktif(berlaku_mulai: str) -> dict:
    # Tabel FIKTIF – KHUSUS UJI (angka buatan, bukan aturan fraksi BEI).
    return {"terverifikasi": True, "sumber": SUMBER_FRAKSI, "tanggal_cek": "2026-10-01",
            "berlaku_mulai": berlaku_mulai,
            "tabel": [{"harga_min": 0, "harga_maks": 1000, "fraksi": 3},
                      {"harga_min": 1000, "harga_maks": None, "fraksi": 7}]}


def ara_fiktif(berlaku_mulai: str = "2026-01-05", ara: list[float] | None = None) -> dict:
    # Batas FIKTIF – KHUSUS UJI (bukan batas auto rejection BEI). `ara`: batas_pct per rentang harga.
    ara = [33.0] if ara is None else ara
    batas = [100.0 * (i + 1) for i in range(len(ara) - 1)] + [None]
    baris = []
    for i, pct in enumerate(ara):
        baris.append({"harga_min": 0 if i == 0 else batas[i - 1], "harga_maks": batas[i], "batas_pct": pct})
    return {"terverifikasi": True, "sumber": SUMBER_ARA, "tanggal_cek": "2026-10-01",
            "berlaku_mulai": berlaku_mulai, "papan_berlaku": "FIKTIF", "harga_acuan": "close_kemarin",
            "ara": baris, "arb": [{"harga_min": 0, "harga_maks": None, "batas_pct": 13.0}]}


def test_item_terverifikasi_fiktif_dengan_sumber_dan_berlaku_mulai(akar_uji):
    tulis_config(akar_uji, {
        "pasar.jadwal_sesi": jadwal_fiktif(),
        "pasar.fraksi_harga": fraksi_fiktif("2027-03-01"),
        "pasar.auto_reject": ara_fiktif("2026-01-05"),
    })
    h = cek(akar_uji)
    assert h.kode == 0, h.teks
    for item in ("pasar.jadwal_sesi", "pasar.fraksi_harga", "pasar.auto_reject"):
        assert not h.ada("BELUM DIVERIFIKASI", item), h.teks
    for item in ("biaya", "data.pengali_volume"):
        assert h.ada("BELUM DIVERIFIKASI", item), h.teks
    # Item yang sudah diverifikasi dicantumkan beserta sumbernya.
    assert h.ada("jadwal_sesi", SUMBER_JADWAL), h.teks
    baris_fraksi = h.dengan("fraksi_harga", SUMBER_FRAKSI)
    baris_ara = h.dengan("auto_reject", SUMBER_ARA)
    assert baris_fraksi and baris_ara, h.teks
    # ... dan tanggal berlakunya; berlaku_mulai di masa depan diberi catatan.
    assert "2027-03-01" in baris_fraksi[0] and "belum" in baris_fraksi[0].lower()
    assert "2026-01-05" in baris_ara[0] and "belum" not in baris_ara[0].lower()


@pytest.mark.parametrize(
    "berlaku_mulai, belum",
    [("2026-10-15", False), ("2026-10-16", True)],
    ids=["mulai_hari_ini", "mulai_besok"],
)
def test_berlaku_mulai_batas_hari_ini(akar_uji, berlaku_mulai, belum):
    tulis_config(akar_uji, {"pasar.fraksi_harga": fraksi_fiktif(berlaku_mulai)})
    h = cek(akar_uji)
    baris = h.dengan("fraksi_harga", SUMBER_FRAKSI)
    assert baris and berlaku_mulai in baris[0], h.teks
    assert ("belum" in baris[0].lower()) == belum, baris[0]


def test_semua_item_terverifikasi_tanpa_belum_diverifikasi(akar_uji):
    tulis_config(akar_uji, {
        "pasar.jadwal_sesi": jadwal_fiktif(),
        "pasar.fraksi_harga": fraksi_fiktif("2026-01-05"),
        "pasar.auto_reject": ara_fiktif(),
        "biaya.terverifikasi": True,
        "data.pengali_volume_terverifikasi": True,
    })
    h = cek(akar_uji)
    assert h.kode == 0
    for item in ITEM_VERIFIKASI:
        assert not h.ada("BELUM DIVERIFIKASI", item), h.teks


@pytest.mark.parametrize(
    "ara, peringatan",
    [
        ([33.0], False),
        ([39.99], False),
        ([40.0], True),  # sama dengan lonjakan_mencurigakan_pct (40): tidak "di atas" → peringatan
        ([40.01], True),
        ([45.0, 35.0], True),  # yang dinilai batas TERTINGGI, bukan baris terakhir
        ([35.0, 45.0], True),
    ],
)
def test_lonjakan_mencurigakan_tidak_di_atas_ara_tertinggi_peringatan(akar_uji, ara, peringatan):
    """config §7 (data.validasi): setelah ARA diverifikasi, lonjakan_mencurigakan_pct harus di atas batas
    ARA tertinggi; bila tidak, cek memberi peringatan (bukan ERROR)."""
    tulis_config(akar_uji, {"pasar.auto_reject": ara_fiktif(ara=ara)})
    h = cek(akar_uji)
    assert h.kode == 0, h.teks
    assert h.ada("lonjakan_mencurigakan_pct") == peringatan, h.teks


# ---------------------------------------------------------------------------
# holidays.csv tahun berjalan/berikutnya (§6.7)
# ---------------------------------------------------------------------------


def _peringatan_tahun(h: Hasil, tahun: int) -> list[str]:
    return [b for b in h.dengan("holidays", str(tahun)) if "PERINGATAN" in b or "BANNER" in b]


@pytest.mark.parametrize(
    "libur, kurang",
    [
        (("2026-12-25", "2027-01-01"), ()),
        (("2026-12-25",), (2027,)),
        (("2027-01-01",), (2026,)),
        ((), (2026, 2027)),
        (("2025-12-25",), (2026, 2027)),
    ],
    ids=["lengkap", "tanpa_tahun_depan", "tanpa_tahun_ini", "kosong", "hanya_tahun_lalu"],
)
def test_holidays_tanpa_tahun_berjalan_atau_berikutnya_peringatan(akar_uji, libur, kurang):
    tulis_libur(akar_uji, *libur)
    h = cek(akar_uji)
    assert h.kode == 0, h.teks
    for tahun in (2026, 2027):
        assert bool(_peringatan_tahun(h, tahun)) == (tahun in kurang), h.teks


@pytest.mark.parametrize(
    "sekarang, banner",
    [("2026-11-30T20:00:00+07:00", False), ("2026-12-01T20:00:00+07:00", True)],
    ids=["november", "desember"],
)
def test_holidays_tahun_berikutnya_banner_sejak_desember(akar_uji, atur_waktu, sekarang, banner):
    atur_waktu(sekarang)
    tulis_libur(akar_uji, "2026-12-25")
    h = cek(akar_uji)
    assert h.kode == 0
    baris = _peringatan_tahun(h, 2027)
    assert baris, h.teks
    assert ("BANNER" in baris[0]) == banner, baris[0]


# ---------------------------------------------------------------------------
# Perbandingan kalender manual vs turunan dari cache breadth (§8.7)
# ---------------------------------------------------------------------------

LIBUR_UJI = "2026-09-09"  # Rabu; libur FIKTIF yang ditambahkan ke holidays.csv
HARI_SEPI = "2026-09-16"  # Rabu; bukan libur di holidays.csv


def tulis_breadth(akar: Path, *, mulai="2026-07-01", akhir="2026-10-15", kecuali_semua=(), khusus=None) -> None:
    """Cache keempat saham breadth. `kecuali_semua`: tanggal tanpa bar di semua saham. `khusus`: tanggal →
    {kode: "ada" | "nol" | "hilang"} (volume > 0 / volume 0 / tanpa baris)."""
    khusus = khusus or {}
    for kode in BREADTH_FIXTURE:
        hilang = [*kecuali_semua, *[d for d, peta in khusus.items() if peta.get(kode) == "hilang"]]
        df = bingkai(mulai, akhir, kecuali=hilang)
        for d, peta in khusus.items():
            if peta.get(kode) == "nol":
                df.loc[pd.Timestamp(d), "volume"] = 0.0
        tulis_cache(akar, kode, df)


def test_perbandingan_kalender_dilewati_tanpa_cache(akar_uji):
    h = cek(akar_uji)
    assert h.kode == 0
    assert h.ada("kalender", "dilewati", kecil=True), h.teks


def test_kalender_cocok_tanpa_peringatan(akar_uji):
    tulis_libur(akar_uji, LIBUR_UJI, "2026-12-25", "2027-01-01")
    tulis_breadth(akar_uji, kecuali_semua=[LIBUR_UJI])
    h = cek(akar_uji)
    assert h.kode == 0, h.teks
    assert not h.ada(LIBUR_UJI), h.teks
    assert not h.ada("kalender", "dilewati", kecil=True)  # cache ada: perbandingan dijalankan


def test_libur_tercatat_tetapi_ada_transaksi_peringatan(akar_uji):
    tulis_libur(akar_uji, LIBUR_UJI, "2026-12-25", "2027-01-01")
    tulis_breadth(akar_uji)  # semua saham bertransaksi pada LIBUR_UJI
    h = cek(akar_uji)
    assert h.kode == 0, h.teks
    assert h.ada(LIBUR_UJI, "PERINGATAN"), h.teks


@pytest.mark.parametrize(
    "peta, peringatan",
    [
        ({"AAAA": "hilang", "BBBB": "hilang", "CCCC": "hilang"}, True),  # 1 dari 4 (25% < 50%)
        ({"AAAA": "nol", "BBBB": "hilang", "CCCC": "hilang"}, True),  # volume 0 = tanpa transaksi
        ({"AAAA": "hilang", "BBBB": "hilang"}, False),  # 2 dari 4 = 50% ≥ min_hari_kalender_historis_pct
        ({}, False),
    ],
    ids=["satu_dari_empat", "volume_nol", "dua_dari_empat", "lengkap"],
)
def test_hari_bursa_hampir_tanpa_transaksi_bukan_libur_peringatan(akar_uji, peta, peringatan):
    tulis_breadth(akar_uji, khusus={HARI_SEPI: peta})
    h = cek(akar_uji)
    assert h.kode == 0, h.teks
    assert h.ada(HARI_SEPI) == peringatan, h.teks


def test_tahun_tanpa_entri_holidays_tidak_dibandingkan(akar_uji):
    """§8.7: hanya tanggal lampau di tahun yang tercakup holidays.csv (2026/2027) yang dibandingkan."""
    tulis_breadth(akar_uji, mulai="2025-11-03", kecuali_semua=["2025-11-12"])
    h = cek(akar_uji)
    assert h.kode == 0
    assert not h.ada("2025-11-12"), h.teks


# ---------------------------------------------------------------------------
# holdout_mulai vs baris pertama holdout_log.csv (§16.4)
# ---------------------------------------------------------------------------


def tulis_holdout_log(akar: Path, *nilai: str) -> None:
    path = akar / "output" / "backtest" / "holdout_log.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    baris = [f"2026-09-0{i + 1}T20:00:00+07:00,{v},abcd1234,ef567890,pb,edge,1.5,40\n" for i, v in enumerate(nilai)]
    path.write_text(f"# {DISCLAIMER_TEKS}. Log holdout uji {FIKTIF}.\n" + HEADER_HOLDOUT + "".join(baris),
                    encoding="utf-8")


def _peringatan_holdout(h: Hasil) -> list[str]:
    return [b for b in h.dengan("holdout") if "PERINGATAN" in b]


@pytest.mark.parametrize(
    "nilai, peringatan",
    [
        (("2024-10-01",), False),
        (("2024-07-01",), True),
        (("2024-07-01", "2024-10-01"), True),  # baris PERTAMA yang menentukan
        (("2024-10-01", "2024-07-01"), False),
    ],
    ids=["sama", "berbeda", "pertama_berbeda", "pertama_sama"],
)
def test_holdout_mulai_dibandingkan_dengan_baris_pertama_log(akar_uji, nilai, peringatan):
    tulis_holdout_log(akar_uji, *nilai)
    h = cek(akar_uji)
    assert h.kode == 0, h.teks
    assert bool(_peringatan_holdout(h)) == peringatan, h.teks
    if peringatan:
        assert "2024-07-01" in _peringatan_holdout(h)[0]


def test_holdout_tanpa_log_tanpa_peringatan(akar_uji):
    h = cek(akar_uji)
    assert _peringatan_holdout(h) == []


def test_holdout_mulai_config_diubah_peringatan(akar_uji):
    tulis_holdout_log(akar_uji, "2024-10-01")
    tulis_config(akar_uji, {"backtest.holdout_mulai": "2025-01-02"})
    h = cek(akar_uji)
    assert h.kode == 0
    assert _peringatan_holdout(h), h.teks


# ---------------------------------------------------------------------------
# .env (§19.2 butir 6, §19.4, T-OP)
# ---------------------------------------------------------------------------


def test_env_txt_terdeteksi(akar_uji):
    (akar_uji / ".env.txt").write_text(f"TELEGRAM_BOT_TOKEN={TOKEN_PALSU}\n", encoding="utf-8")
    h = cek(akar_uji)
    assert h.kode == 0, h.teks
    assert h.ada(".env.txt", "PERINGATAN"), h.teks
    assert TOKEN_PALSU not in h.teks


def test_tanpa_env_txt_tanpa_peringatannya(akar_uji):
    assert not cek(akar_uji).ada(".env.txt")


def _peringatan_telegram(h: Hasil) -> list[str]:
    return [b for b in h.baris if "PERINGATAN" in b and ("telegram" in b.lower() or ".env" in b)]


@pytest.mark.parametrize(
    "aktif, isi_env, peringatan",
    [
        (True, None, True),  # aktif tanpa .env
        (True, f"TELEGRAM_BOT_TOKEN={TOKEN_PALSU}\nTELEGRAM_CHAT_ID=12345678\n", False),
        (True, "TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=12345678\n", True),  # token kosong
        (False, None, False),  # nonaktif: .env tidak diperlukan
    ],
    ids=["aktif_tanpa_env", "aktif_env_lengkap", "aktif_token_kosong", "nonaktif_tanpa_env"],
)
def test_telegram_aktif_tanpa_env_peringatan(akar_uji, aktif, isi_env, peringatan):
    tulis_config(akar_uji, {"notifikasi.telegram.aktif": aktif})
    if isi_env is not None:
        (akar_uji / ".env").write_text(isi_env, encoding="utf-8")
    h = cek(akar_uji)
    assert h.kode == 0, h.teks
    assert bool(_peringatan_telegram(h)) == peringatan, h.teks
    assert TOKEN_PALSU not in h.teks  # cek tidak pernah mencetak token


def test_peringatan_token_kosong_menyebut_kuncinya(akar_uji):
    tulis_config(akar_uji, {"notifikasi.telegram.aktif": True})
    (akar_uji / ".env").write_text("TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=12345678\n", encoding="utf-8")
    h = cek(akar_uji)
    assert any("TELEGRAM_BOT_TOKEN" in b for b in _peringatan_telegram(h)), h.teks


# ---------------------------------------------------------------------------
# Resolusi rujukan per konteks (§7, T-CFG)
# ---------------------------------------------------------------------------


def baris_konteks(h: Hasil, nama: str) -> str:
    awal = h.indeks("rujukan")
    for b in h.baris[awal + 1:]:
        if re.search(rf"\b{nama}\b", b):
            return b
    raise AssertionError(f"konteks {nama} tidak dicetak:\n{h.teks}")


@pytest.mark.parametrize(
    "ubah, harap",
    [
        # (modal, maks_posisi, alokasi_maks pada modal awal) per konteks
        ({}, {"live": (3_000_000, 3, "1000000"), "edge": (1_000_000_000, 3, "333333333.33"),
              "realistis": (3_000_000, 3, "1000000")}),
        ({"risiko.maks_posisi": 4}, {"live": (3_000_000, 4, "750000"), "edge": (1_000_000_000, 4, "250000000"),
                                     "realistis": (3_000_000, 4, "750000")}),
        # ikuti_maks_posisi diresolusi dalam konteks mode yang berjalan (rujukan berantai)
        ({"backtest.mode.edge.maks_posisi": 5}, {"live": (3_000_000, 3, "1000000"),
                                                  "edge": (1_000_000_000, 5, "200000000"),
                                                  "realistis": (3_000_000, 3, "1000000")}),
        ({"akun.modal_awal_idr": 6_000_000}, {"live": (6_000_000, 3, "2000000"),
                                              "edge": (1_000_000_000, 3, "333333333.33"),
                                              "realistis": (6_000_000, 3, "2000000")}),
        ({"risiko.alokasi_maks_pct": 25}, {"live": (3_000_000, 3, "750000"), "edge": (1_000_000_000, 3, "250000000"),
                                           "realistis": (3_000_000, 3, "750000")}),
    ],
    ids=["default", "maks_posisi_4", "edge_maks_posisi_5", "modal_akun_6jt", "alokasi_25pct"],
)
def test_resolusi_rujukan_live_edge_realistis(akar_uji, ubah, harap):
    if ubah:
        tulis_config(akar_uji, ubah)
    h = cek(akar_uji)
    assert h.kode == 0, h.teks
    for nama, (modal, maks_posisi, alokasi) in harap.items():
        b = baris_konteks(h, nama)
        angka = semua_angka(b)
        assert Decimal(modal) in angka, b
        assert Decimal(maks_posisi) in angka, b
        assert Decimal(alokasi) in angka, b


def test_edge_alokasi_ekuitas_dibagi_maks_posisi(akar_uji):
    """T-CFG: edge: alokasi = ekuitas ÷ 3 (pembagian langsung, bukan lewat persen)."""
    h = cek(akar_uji)
    b = baris_konteks(h, "edge")
    assert Decimal("333333333.33") in semua_angka(b), b


# ---------------------------------------------------------------------------
# Hanya membaca (§19.2): tidak mengunduh, tidak memegang kunci, hanya menulis log
# ---------------------------------------------------------------------------


def test_cek_tidak_membuat_data_cache_kualitas(akar_uji):
    sebelum = potret(akar_uji)
    h = cek(akar_uji)
    assert h.kode == 0
    sesudah = potret(akar_uji)
    baru = sorted(set(sesudah) - set(sebelum))
    assert all(p == "output" or p.startswith("output/log") for p in baru), baru
    assert not (akar_uji / "data").exists()
    assert not (akar_uji / "output" / "kualitas").exists()
    assert any(p.startswith("output/log/") and p.endswith(".log") for p in baru)  # log boleh ditulis
    assert {p: v for p, v in sesudah.items() if p in sebelum} == sebelum  # tidak ada yang diubah


def test_cek_silang_dengan_cache_hanya_menulis_log(akar_uji):
    tulis_config(akar_uji, {"notifikasi.telegram.aktif": True})
    tulis_cache(akar_uji, "AAAA", bingkai("2025-06-02", "2026-10-15"), split={S_SPLIT: 5})
    tulis_holdout_log(akar_uji, "2024-07-01")
    (akar_uji / ".env.txt").write_text("x=1\n", encoding="utf-8")
    sebelum = potret(akar_uji)
    h = silang(akar_uji, "AAAA", D_SILANG)
    assert h.kode == 0, h.teks
    sesudah = potret(akar_uji)
    baru = sorted(set(sesudah) - set(sebelum))
    assert all(p.startswith("output/log") for p in baru), baru
    assert {p: v for p, v in sesudah.items() if p in sebelum} == sebelum
    assert not (akar_uji / "data" / ".kunci").exists()


def test_jalankan_cek_langsung_tidak_menulis_apa_pun(akar_uji):
    tulis_cache(akar_uji, "AAAA", bingkai("2025-06-02", "2026-10-15"))
    sebelum = potret(akar_uji)
    keluaran: list[str] = []
    jalankan_cek(konteks(akar_uji), silang_kode=("AAAA", "2026-03-02"), keluar=keluaran.append)
    assert potret(akar_uji) == sebelum
    assert DISCLAIMER_TEKS in "\n".join(keluaran)


def test_cek_tidak_memegang_kunci(akar_uji):
    """§19.2/§19.3: cek tidak memegang kunci → tetap berjalan (kode 0, bukan 5) saat kunci dipegang."""
    with kunci_proses(akar_uji, "unduh"):
        h = cek(akar_uji)
    assert h.kode == 0, h.teks


# ---------------------------------------------------------------------------
# cek --silang KODE TANGGAL (§19.2, §22.3 langkah 3, §8.4)
# ---------------------------------------------------------------------------

def seri_silang(*, disesuaikan: bool = True, **bar_d) -> pd.DataFrame:
    """AAAA 2025-06-02…2026-10-15, split 1:5 pada S_SPLIT. Sudah disesuaikan: harga sebelum S dibagi 5
    (tanpa lompatan di S). Belum disesuaikan: harga sebelum S nominal (q = close_S / close_S−1 = 1/5)."""
    df = bingkai("2025-06-02", "2026-10-15")
    for k, v in {**BAR_D, **bar_d}.items():
        df.loc[pd.Timestamp(D_SILANG), k] = float(v)
    if not disesuaikan:
        sebelum = df.index < pd.Timestamp(S_SPLIT)
        for k in ("open", "high", "low", "close"):
            df.loc[sebelum, k] = df.loc[sebelum, k] * 5
        df.loc[sebelum, "volume"] = df.loc[sebelum, "volume"] / 5
    return df


@pytest.fixture
def akar_silang(akar_uji):
    tulis_config(akar_uji, {"data.pengali_volume": PENGALI_SILANG})
    tulis_cache(akar_uji, "AAAA", seri_silang(), split={S_SPLIT: 5})
    return akar_uji


def baris_bar(h: Hasil, nominal: bool) -> str:
    kandidat = [b for b in h.baris if re.search(r"\bopen\b", b) and (("nominal" in b.lower()) == nominal)]
    assert kandidat, f"baris {'nominal' if nominal else 'cache'} tidak dicetak:\n{h.teks}"
    return kandidat[0]


def baris_f(h: Hasil, d: date) -> str:
    kandidat = h.dengan(f"F({d.isoformat()})")
    assert kandidat, f"F({d}) tidak dicetak:\n{h.teks}"
    return kandidat[0]


def nilai_f(h: Hasil, d: date) -> Decimal:
    m = re.search(rf"F\({d.isoformat()}\)\s*=\s*({_ANGKA})", baris_f(h, d))
    assert m, baris_f(h, d)
    return ke_angka(m.group(1))


def test_silang_split_sesudah_tanggal_f_5_dan_nilai_nominal(akar_silang):
    h = silang(akar_silang, "AAAA", D_SILANG)
    assert h.kode == 0, h.teks
    # OHLCV cache (tersesuaikan) apa adanya.
    cache = baris_bar(h, nominal=False)
    for k, v in BAR_D.items():
        assert nilai_label(cache, k) == Decimal(str(v)), cache
    # F(d) = 5 dan split sesudahnya disebut.
    assert nilai_f(h, D_SILANG) == 5
    assert S_SPLIT.isoformat() in baris_f(h, D_SILANG)
    # Nominal: OHLC × F; volume ÷ F × pengali_volume.
    nom = baris_bar(h, nominal=True)
    for k in ("open", "high", "low", "close"):
        assert nilai_label(nom, k) == Decimal(str(BAR_D[k])) * 5, nom
    assert nilai_label(nom, "volume") == Decimal("6000000") / 5 * PENGALI_SILANG, nom  # 120.000.000
    # Nilai transaksi = close nominal × volume nominal (× pengali) = 1.040 × 120.000.000.
    transaksi = h.dengan("nilai transaksi", kecil=True)
    assert transaksi and Decimal("124800000000") in semua_angka(transaksi[0]), h.teks


def test_silang_sumber_dan_tanda_bar_valid(akar_silang):
    h = silang(akar_silang, "AAAA", D_SILANG)
    per_bar = h.dengan("sumber", "tanda", kecil=True)
    assert per_bar and "yfinance" in per_bar[0], h.teks
    for tanda in ("TANPA_TRANSAKSI", "BAR_RUSAK", "BAR_HARI_NONBURSA"):
        assert tanda not in per_bar[0]


@pytest.mark.parametrize(
    "tanggal, f_harap",
    [(date(2026, 5, 29), 5), (S_SPLIT, 1), (date(2026, 6, 2), 1)],
    ids=["jumat_sebelum_split", "hari_split", "sesudah_split"],
)
def test_silang_f_hanya_split_sesudah_tanggal(akar_silang, tanggal, f_harap):
    """§8.4: F(d) = hasil kali rasio split yang terjadi SETELAH d (split pada d sendiri tidak ikut)."""
    h = silang(akar_silang, "AAAA", tanggal)
    assert h.kode == 0, h.teks
    assert nilai_f(h, tanggal) == f_harap
    nom = baris_bar(h, nominal=True)
    assert nilai_label(nom, "close") == Decimal(str(BAR_LAIN["close"])) * f_harap, nom
    assert nilai_label(nom, "volume") == Decimal(str(BAR_LAIN["volume"])) / f_harap * PENGALI_SILANG, nom


def test_silang_split_belum_disesuaikan_tidak_ikut_f(akar_uji):
    """§8.4: split yang belum disesuaikan sumber tidak ikut F(d): harga cache sebelum s sudah nominal."""
    tulis_cache(akar_uji, "AAAA", seri_silang(disesuaikan=False), split={S_SPLIT: 5})
    h = silang(akar_uji, "AAAA", D_SILANG)
    assert h.kode == 0, h.teks
    assert nilai_f(h, D_SILANG) == 1
    nom = baris_bar(h, nominal=True)
    assert nilai_label(nom, "open") == Decimal(str(BAR_D["open"])) * 5, nom  # = harga cache (nominal)


@pytest.mark.parametrize(
    "bar_d, tanda",
    [({"volume": 0.0}, "TANPA_TRANSAKSI"), ({"high": 190.0, "low": 196.0}, "BAR_RUSAK")],
    ids=["volume_nol", "high_di_bawah_low"],
)
def test_silang_tanda_bar(akar_uji, bar_d, tanda):
    tulis_cache(akar_uji, "AAAA", seri_silang(**bar_d), split={S_SPLIT: 5})
    h = silang(akar_uji, "AAAA", D_SILANG)
    assert h.kode == 0, h.teks
    per_bar = h.dengan("sumber", "tanda", kecil=True)
    assert per_bar and tanda in per_bar[0], h.teks


def test_silang_bar_pada_hari_libur_bertanda_bar_hari_nonbursa(akar_uji):
    tulis_libur(akar_uji, "2026-03-03", "2026-12-25", "2027-01-01")
    tulis_cache(akar_uji, "AAAA", seri_silang(), split={S_SPLIT: 5})  # cache memuat bar 2026-03-03
    h = silang(akar_uji, "AAAA", "2026-03-03")
    assert h.kode == 0, h.teks
    per_bar = h.dengan("sumber", "tanda", kecil=True)
    assert per_bar and "BAR_HARI_NONBURSA" in per_bar[0], h.teks


def test_silang_sumber_per_bar_sambungan_csv_manual(akar_uji):
    """§8.2 butir 4: setiap bar menyimpan sumbernya; bar t dari sambungan csv_manual bersumber csv_manual."""
    tulis_cache(akar_uji, "AAAA", bingkai("2026-07-01", "2026-10-14"))  # SEBAGIAN: tanpa bar t
    manual = bingkai("2026-10-01", "2026-10-15")  # 10 bar tumpang tindih identik + bar t
    manual.loc[pd.Timestamp(T), ["open", "high", "low", "close", "volume"]] = [1001.0, 1011.0, 991.0, 1005.0, 1234000.0]
    tulis_manual(akar_uji, "AAAA", manual)
    h_t = silang(akar_uji, "AAAA", T)
    h_lama = silang(akar_uji, "AAAA", "2026-10-14")
    sumber_t = h_t.dengan("sumber", "tanda", kecil=True)
    sumber_lama = h_lama.dengan("sumber", "tanda", kecil=True)
    assert sumber_t and "csv_manual" in sumber_t[0], h_t.teks
    assert nilai_label(baris_bar(h_t, nominal=False), "close") == Decimal("1005")
    assert sumber_lama and "yfinance" in sumber_lama[0] and "csv_manual" not in sumber_lama[0], h_lama.teks


def test_silang_ihsg_tanpa_volume(akar_uji):
    df = bingkai("2025-06-02", "2026-10-15", open=7100.0, high=7150.0, low=7050.0, close=7123.45, volume=float("nan"))
    tulis_cache(akar_uji, "IHSG", df)
    h = silang(akar_uji, "IHSG", D_SILANG)
    assert h.kode == 0, h.teks
    assert nilai_label(baris_bar(h, nominal=False), "close") == Decimal("7123.45")
    assert nilai_f(h, D_SILANG) == 1


@pytest.mark.parametrize(
    "tanggal",
    ["2026-03-03", "2026-03-07"],
    ids=["hari_kerja_tanpa_bar", "sabtu"],
)
def test_silang_tanpa_bar_pada_tanggal_tidak_mencari_terdekat(akar_uji, tanggal):
    df = seri_silang().drop(pd.Timestamp("2026-03-03"))
    tulis_cache(akar_uji, "AAAA", df, split={S_SPLIT: 5})
    h = silang(akar_uji, "AAAA", tanggal)
    assert h.ada("tidak ada bar", kecil=True), h.teks
    assert h.ada("terdekat", kecil=True), h.teks
    # Tidak ada nilai bar tetangga (2026-03-02 / 2026-03-04) yang dicetak sebagai pengganti.
    assert not [b for b in h.baris if re.search(r"\bopen\b", b)], h.teks
    assert not h.ada("F(")


@pytest.mark.parametrize(
    "sekarang, tanggal",
    [
        ("2026-10-15T20:00:00+07:00", "2026-10-16"),
        ("2026-10-15T20:00:00+07:00", "2026-10-17"),
        ("2026-10-15T19:00:00+07:00", "2026-10-15"),  # sebelum batas bar final: t = 2026-10-14
    ],
    ids=["besok", "sabtu_sesudah_t", "hari_ini_belum_final"],
)
def test_silang_tanggal_sesudah_t_error_kode_3(akar_silang, atur_waktu, sekarang, tanggal):
    atur_waktu(sekarang)
    h = silang(akar_silang, "AAAA", tanggal)
    assert h.kode == 3, h.teks
    assert h.error(), h.teks
    assert not [b for b in h.baris if re.search(r"\bopen\b", b)]


def test_silang_tanggal_sama_dengan_t_sah(akar_silang):
    h = silang(akar_silang, "AAAA", T)
    assert h.kode == 0, h.teks
    assert h.error() == []


@pytest.mark.parametrize(
    "kode, tanggal",
    [
        ("aaaa", "2026-03-02"),
        ("AAAA.JK", "2026-03-02"),
        ("AA-A", "2026-03-02"),
        ("AAAA", "02/03/2026"),
        ("AAAA", "2026-3-2"),
        ("AAAA", "2026-02-30"),
        ("AAAA", "kemarin"),
    ],
)
def test_silang_kode_atau_tanggal_tidak_valid_error_kode_3(akar_silang, kode, tanggal):
    h = silang(akar_silang, kode, tanggal)
    assert h.kode == 3, h.teks
    assert h.error(), h.teks
    assert DISCLAIMER_TEKS in h.teks


def test_silang_kode_tanpa_data_tidak_crash(akar_uji):
    h = silang(akar_uji, "BBBB", D_SILANG)
    assert h.kode in (0, 3), h.teks  # SPEC tidak menetapkan kode untuk kasus ini; yang diuji: tanpa crash
    assert not h.ada("F(")
    assert DISCLAIMER_TEKS in h.teks


def test_silang_lewat_jalankan_cek_langsung_error_tercatat(akar_uji):
    hasil = jalankan_cek(konteks(akar_uji), silang_kode=("AAAA", "2026-10-16"), keluar=lambda s: None)
    assert hasil.kode_keluar == 3
    assert hasil.error


# ---------------------------------------------------------------------------
# Perbaikan hasil tinjauan: cache breadth basi tidak memunculkan dugaan libur palsu (§8.7)
# ---------------------------------------------------------------------------


def test_cache_breadth_basi_diperingatkan_tanpa_dugaan_libur_palsu(akar_uji):
    tulis_cache(akar_uji, "AAAA", bingkai("2026-01-05", T.isoformat()))
    for kode in ("BBBB", "CCCC", "DDDD"):  # cache belum diperbarui sejak 30 September
        tulis_cache(akar_uji, kode, bingkai("2026-01-05", "2026-09-30"))
    h = cek(akar_uji)
    assert h.kode == 0
    assert h.ada("cache breadth belum sampai t", "BBBB", "CCCC", "DDDD")
    assert not h.ada("mungkin libur", kecil=True), h.teks

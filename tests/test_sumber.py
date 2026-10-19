"""Uji `data/sumber.py` (SPEC §8.1 "Kebijakan per run", §8.2 "Pemilihan sumber per simbol" + tabel
"Urutan cadangan", §6.9, §8.5–§8.7; bagian T-DQ/T-CA §22.1).

- `KebijakanAmbil`: provider jaringan bernaskah (urutan hasil per kode) + stub `tidur` pencatat, sehingga
  urutan "jeda ↔ permintaan" teruji tanpa pernah tidur sungguhan dan tanpa jaringan.
- `pilih_seri`: proyek sementara di `tmp_path` (config_uji.yaml), cache yfinance ditulis lewat
  `CacheProvider.tulis`, berkas CSV manual format §6.9 ditulis langsung.
- `muat_pasar` / `baris_kualitas`: salinan fixture `input_uji/` (`akar_uji`).

Semua kode saham, harga, dan tanggal libur di sini FIKTIF – KHUSUS UJI. Waktu virtual dari conftest
(2026-10-15 20:00 WIB, Kamis; batas bar final = `jadwal.jam_run` 19:30 → t = 2026-10-15).
"""

from __future__ import annotations

import copy
import dataclasses
import socket
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from asisten_bei import waktu
from asisten_bei.config import KonteksApp, baca_yaml, validasi_dict
from asisten_bei.data.cache import CacheProvider, HasilPembaruan, IsiCache, buat_meta
from asisten_bei.data.provider import KOLOM_BAR, HasilAmbil, gagal
from asisten_bei.data.sumber import KebijakanAmbil, baris_kualitas, muat_pasar, pilih_seri
from asisten_bei.inputs import muat_masukan

from bantu_pasar import cfg_uji

FIXTURES = Path(__file__).resolve().parent / "fixtures"
_CONFIG_UJI = baca_yaml(FIXTURES / "config_uji.yaml")

FIKTIF = "FIKTIF – KHUSUS UJI"
T = date(2026, 10, 15)  # Kamis; t pada waktu uji default


def hari_bursa(akhir: date, n: int) -> list[date]:
    """`n` hari Senin–Jumat berakhir di `akhir` (Sep–Okt 2026 tanpa libur di holidays.csv fixture)."""
    return [ts.date() for ts in pd.bdate_range(end=akhir, periods=n)]


HARI = hari_bursa(T, 30)  # HARI[-1] = 2026-10-15, HARI[-2] = 2026-10-14, HARI[-3] = 2026-10-13


def ts(d: date) -> pd.Timestamp:
    return pd.Timestamp(d)


# ---------------------------------------------------------------------------
# Pengaman: tanpa jaringan, tanpa tidur sungguhan
# ---------------------------------------------------------------------------


class _AksesDilarang(BaseException):
    """Bukan turunan Exception agar tidak tertelan `except Exception` di kode yang diuji."""


@pytest.fixture(autouse=True)
def _tanpa_jaringan_dan_tidur_nyata(monkeypatch):
    def tolak(nama):
        def _tolak(*args, **kwargs):
            raise _AksesDilarang(f"dilarang dalam uji: {nama}")

        return _tolak

    monkeypatch.setattr(socket.socket, "connect", tolak("socket.connect"))
    monkeypatch.setattr(socket, "create_connection", tolak("socket.create_connection"))
    monkeypatch.setattr(time, "sleep", tolak("time.sleep"))


# ---------------------------------------------------------------------------
# Pembantu: config, bingkai bar, cache, CSV manual
# ---------------------------------------------------------------------------


def konteks(akar: Path, ubah: dict | None = None) -> KonteksApp:
    """KonteksApp dari config_uji.yaml dengan perubahan bertitik (`{"data.toleransi_revisi_pct": 1.0}`)."""
    data = copy.deepcopy(_CONFIG_UJI)
    for kunci, nilai in (ubah or {}).items():
        *jalur, akhir = kunci.split(".")
        simpul = data
        for k in jalur:
            simpul = simpul[k]
        simpul[akhir] = copy.deepcopy(nilai)
    akar = Path(akar).resolve()
    return KonteksApp(cfg=validasi_dict(data), akar=akar, path_config=akar / "config_uji.yaml")


@pytest.fixture
def ctx(tmp_path) -> KonteksApp:
    return konteks(tmp_path)


def bingkai(tanggal, *, o=1000.0, h=1200.0, low=800.0, c=1000.0, v=1_000_000.0) -> pd.DataFrame:
    """Bar open..volume pada `tanggal`. Nilai bawaan (1000/1200/800/1000) membuat 0,5% dan 1% dari setiap
    kolom OHLC bernilai bulat (5/6/4/5 dan 10/12/8/10), sehingga batas toleransi teruji eksak."""
    n = len(tanggal)
    idx = pd.DatetimeIndex(pd.to_datetime(list(tanggal)), name="tanggal").as_unit("ns")

    def kolom(x):
        return np.broadcast_to(np.asarray(x, dtype="float64"), (n,)).copy()

    return pd.DataFrame(
        {"open": kolom(o), "high": kolom(h), "low": kolom(low), "close": kolom(c), "volume": kolom(v)}, index=idx
    )


def tulis_cache(ctx: KonteksApp, kode: str, df: pd.DataFrame, *, split: dict | None = None) -> pd.DataFrame:
    """Tulis cache yfinance (format §8.3) dan kembalikan bingkai open..volume yang ditulis."""
    bar = df.copy()
    bar["sumber"] = "yfinance"
    bar["split"] = 0.0
    for d, r in (split or {}).items():
        assert ts(d) in bar.index, "split uji harus jatuh pada tanggal bar"
        bar.loc[ts(d), "split"] = r
    cache = CacheProvider(ctx.path(ctx.cfg.data.folder_cache) / "yfinance")
    cache.tulis(kode, bar, buat_meta(bar, sekarang=waktu.sekarang(), versi_provider="palsu-1.0"))
    return df.copy()


def _path_cache(ctx: KonteksApp, kode: str) -> tuple[Path, Path]:
    folder = ctx.path(ctx.cfg.data.folder_cache) / "yfinance"
    return folder / f"{kode}.parquet", folder / f"{kode}.meta.json"


def _angka(x: float) -> str:
    if np.isnan(x):
        return ""
    return format(Decimal(repr(float(x))), "f")  # titik desimal, tanpa eksponen/pemisah ribuan (§6)


def folder_manual(ctx: KonteksApp) -> Path:
    folder = ctx.path(ctx.cfg.data.folder_manual)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def tulis_csv(ctx: KonteksApp, kode: str, df: pd.DataFrame, *, volume: bool = True, nama: str | None = None) -> Path:
    """`<folder_manual>/<nama|kode>.csv` format §6.9 (`tanggal,open,high,low,close,volume`)."""
    kolom = ["open", "high", "low", "close"] + (["volume"] if volume else [])
    baris = ["tanggal," + ",".join(kolom)]
    for t_, r in df.iterrows():
        baris.append(",".join([t_.date().isoformat(), *(_angka(r[k]) for k in kolom)]))
    path = folder_manual(ctx) / f"{nama or kode}.csv"
    path.write_text("\n".join(baris) + "\n", encoding="utf-8")
    return path


def tulis_split_csv(ctx: KonteksApp, kode: str, split: dict) -> Path:
    path = folder_manual(ctx) / f"{kode}_split.csv"
    isi = ["tanggal,rasio"] + [f"{d.isoformat()},{_angka(r)}" for d, r in sorted(split.items())]
    path.write_text("\n".join(isi) + "\n", encoding="utf-8")
    return path


def tulis_csv_rusak(ctx: KonteksApp, kode: str) -> Path:
    """Berkas berpemisah `;` dan berkoma desimal (ekspor Excel regional) → GAGAL FORMAT (§6, T-INP)."""
    path = folder_manual(ctx) / f"{kode}.csv"
    path.write_text("tanggal;open;high;low;close;volume\n2026-10-15;1000,5;1200;800;1000;1000000\n", encoding="utf-8")
    return path


def potret(akar: Path) -> dict[str, bytes]:
    """Semua berkas & folder di bawah `akar` (folder bernilai b"<dir>")."""
    hasil = {}
    for p in sorted(Path(akar).rglob("*")):
        rel = p.relative_to(akar).as_posix()
        hasil[rel] = b"<dir>" if p.is_dir() else p.read_bytes()
    return hasil


def assert_ohlcv(aktual: pd.DataFrame, harapan: pd.DataFrame) -> None:
    pd.testing.assert_frame_equal(
        aktual.loc[:, list(KOLOM_BAR)].astype("float64"),
        harapan.loc[:, list(KOLOM_BAR)].astype("float64"),
        check_freq=False,
        check_names=False,
        check_index_type=False,
    )


def teks_catatan(s) -> str:
    return " | ".join([s.pesan, *s.catatan])


# ===========================================================================
# (1) KebijakanAmbil — §8.1 "Kebijakan per run"
# ===========================================================================

MULAI, AKHIR = date(2026, 10, 1), T


class ProviderNaskah:
    """Provider jaringan palsu. `naskah[kode]` = urutan hasil ("OK" atau `jenis_error_ambil`); setelah
    naskah habis dipakai `bawaan`. Setiap panggilan dicatat di `peristiwa` (bersama stub tidur)."""

    nama = "yfinance"
    versi_provider = "naskah-1.0"

    def __init__(self, naskah: dict[str, list[str]] | None = None, peristiwa: list | None = None, bawaan: str = "OK"):
        self.naskah = {k: list(v) for k, v in (naskah or {}).items()}
        self.peristiwa = [] if peristiwa is None else peristiwa
        self.bawaan = bawaan
        self.panggilan: list[tuple[str, date, date]] = []

    def simbol(self, kode: str) -> str:
        return f"{kode}.JK"

    def ambil_harian(self, kode: str, mulai: date, akhir: date) -> HasilAmbil:
        self.panggilan.append((kode, mulai, akhir))
        self.peristiwa.append(("ambil", kode))
        antrean = self.naskah.get(kode)
        jenis = antrean.pop(0) if antrean else self.bawaan
        if jenis == "OK":
            return HasilAmbil(kode, self.nama, "OK", None, "", bingkai(HARI[-3:]), None, self.versi_provider)
        return gagal(kode, self.nama, jenis, f"{kode}: {jenis} ({FIKTIF})", self.versi_provider)

    def kode_diminta(self) -> list[str]:
        return [k for k, _, _ in self.panggilan]


class TidurCatat:
    def __init__(self, peristiwa: list):
        self.peristiwa = peristiwa
        self.detik: list[float] = []

    def __call__(self, detik: float) -> None:
        self.detik.append(float(detik))
        self.peristiwa.append(("tidur", float(detik)))


def siapkan(naskah: dict | None = None, ubah: dict | None = None, bawaan: str = "OK"):
    peristiwa: list = []
    prov = ProviderNaskah(naskah, peristiwa, bawaan)
    tidur = TidurCatat(peristiwa)
    cfg = cfg_uji(ubah)
    return KebijakanAmbil(cfg, tidur=tidur), prov, tidur, peristiwa, cfg


def jeda_lain(tidur: TidurCatat, cfg) -> list[float]:
    """Jeda selain `jeda_antar_permintaan_detik` (backoff JARINGAN / jeda batas permintaan)."""
    antar = float(cfg.data.jeda_antar_permintaan_detik)
    return [d for d in tidur.detik if d != antar]


@pytest.mark.parametrize("jeda", [0.5, 1.25])  # 0.5 = nilai config; 1.25 = diubah lewat config (P4)
def test_jeda_antar_permintaan_sebelum_setiap_permintaan_kecuali_pertama(jeda):
    keb, prov, tidur, ev, _ = siapkan(ubah={"data.jeda_antar_permintaan_detik": jeda})
    hasil = [keb.ambil(prov, k, MULAI, AKHIR) for k in ("AAAA", "BBBB", "CCCC")]
    assert [h.status for h in hasil] == ["OK", "OK", "OK"]
    assert ev == [
        ("ambil", "AAAA"),
        ("tidur", jeda),
        ("ambil", "BBBB"),
        ("tidur", jeda),
        ("ambil", "CCCC"),
    ]
    assert prov.panggilan == [("AAAA", MULAI, AKHIR), ("BBBB", MULAI, AKHIR), ("CCCC", MULAI, AKHIR)]


def test_jeda_antar_permintaan_juga_sesudah_simbol_yang_gagal():
    keb, prov, tidur, ev, cfg = siapkan({"AAAA": ["TIDAK_ADA_DATA"]})
    keb.ambil(prov, "AAAA", MULAI, AKHIR)
    keb.ambil(prov, "BBBB", MULAI, AKHIR)
    assert ev == [("ambil", "AAAA"), ("tidur", float(cfg.data.jeda_antar_permintaan_detik)), ("ambil", "BBBB")]


@pytest.mark.parametrize("n_jaringan", [0, 1, 2, 3])
def test_jaringan_dicoba_lagi_dengan_jeda_2_4_8_sampai_hasil_lain(n_jaringan):
    keb, prov, tidur, _, cfg = siapkan({"AAAA": ["JARINGAN"] * n_jaringan + ["OK"]})
    h = keb.ambil(prov, "AAAA", MULAI, AKHIR)
    assert h.status == "OK" and h.jenis_error is None
    assert prov.panggilan == [("AAAA", MULAI, AKHIR)] * (n_jaringan + 1)  # argumen sama di setiap coba lagi
    assert jeda_lain(tidur, cfg) == [2.0, 4.0, 8.0][:n_jaringan]


def test_jaringan_terus_menerus_menyerah_setelah_empat_panggilan():
    keb, prov, tidur, _, cfg = siapkan({"AAAA": ["JARINGAN"] * 10})
    h = keb.ambil(prov, "AAAA", MULAI, AKHIR)
    assert h.status == "GAGAL" and h.jenis_error == "JARINGAN"
    assert h.pesan  # pesan asli provider diteruskan
    assert prov.kode_diminta() == ["AAAA"] * 4  # 1 permintaan + 3 kali coba lagi
    assert jeda_lain(tidur, cfg) == [2.0, 4.0, 8.0]
    # JARINGAN tidak memblokir simbol berikutnya.
    assert keb.ambil(prov, "BBBB", MULAI, AKHIR).status == "OK"
    assert prov.kode_diminta()[-1] == "BBBB"


@pytest.mark.parametrize(
    "naskah, jenis_akhir, n_panggilan, jeda",
    [
        (["JARINGAN", "TIDAK_ADA_DATA"], "TIDAK_ADA_DATA", 2, [2.0]),
        (["JARINGAN", "JARINGAN", "FORMAT"], "FORMAT", 3, [2.0, 4.0]),
        (["JARINGAN", "BATAS_PERMINTAAN", "OK"], None, 3, [2.0, 60.0]),  # hasil BATAS mendapat perlakuannya sendiri
    ],
)
def test_jaringan_berhenti_dicoba_saat_hasil_bukan_jaringan(naskah, jenis_akhir, n_panggilan, jeda):
    keb, prov, tidur, _, cfg = siapkan({"AAAA": naskah})
    h = keb.ambil(prov, "AAAA", MULAI, AKHIR)
    assert h.jenis_error == jenis_akhir
    assert len(prov.panggilan) == n_panggilan
    assert jeda_lain(tidur, cfg) == jeda


@pytest.mark.parametrize("jenis", ["TIDAK_ADA_DATA", "FORMAT"])
def test_tidak_ada_data_dan_format_tanpa_coba_lagi(jenis):
    keb, prov, tidur, _, _ = siapkan({"AAAA": [jenis, "OK"]})
    h = keb.ambil(prov, "AAAA", MULAI, AKHIR)
    assert h.status == "GAGAL" and h.jenis_error == jenis
    assert len(prov.panggilan) == 1
    assert tidur.detik == []


@pytest.mark.parametrize("jeda_batas", [60, 7.5])  # 60 = nilai config; 7.5 = diubah lewat config (P4)
def test_batas_permintaan_jeda_lalu_satu_kali_coba_lagi(jeda_batas):
    keb, prov, tidur, ev, _ = siapkan(
        {"AAAA": ["BATAS_PERMINTAAN", "OK"]}, {"data.jeda_batas_permintaan_detik": jeda_batas}
    )
    h = keb.ambil(prov, "AAAA", MULAI, AKHIR)
    assert h.status == "OK"
    assert ev == [("ambil", "AAAA"), ("tidur", float(jeda_batas)), ("ambil", "AAAA")]


def test_batas_permintaan_yang_masih_kena_hanya_dicoba_lagi_satu_kali():
    keb, prov, tidur, ev, _ = siapkan({"AAAA": ["BATAS_PERMINTAAN"] * 5})
    h = keb.ambil(prov, "AAAA", MULAI, AKHIR)
    assert h.status == "GAGAL" and h.jenis_error == "BATAS_PERMINTAAN"
    assert ev == [("ambil", "AAAA"), ("tidur", 60.0), ("ambil", "AAAA")]


def _assert_diblokir(h: HasilAmbil, kode: str) -> None:
    assert h.kode == kode
    assert h.status == "GAGAL"
    assert h.jenis_error == "BATAS_PERMINTAAN"
    assert "diblokir sementara oleh Yahoo" in h.pesan
    assert h.bar is None


def test_berhenti_setelah_maks_simbol_berturut_kena_batas():
    keb, prov, tidur, _, _ = siapkan(bawaan="BATAS_PERMINTAAN")  # maks_simbol_kena_batas_berturut = 3
    kode = ["AAAA", "BBBB", "CCCC", "DDDD", "EEEE"]
    hasil = []
    for k in kode[:3]:
        hasil.append(keb.ambil(prov, k, MULAI, AKHIR))
    jeda_sebelum_blokir = list(tidur.detik)
    for k in kode[3:]:
        hasil.append(keb.ambil(prov, k, MULAI, AKHIR))
    # Tiga simbol pertama masing-masing: 1 permintaan + 1 coba lagi. Sesudahnya tidak ada permintaan lagi.
    assert prov.kode_diminta() == ["AAAA", "AAAA", "BBBB", "BBBB", "CCCC", "CCCC"]
    assert tidur.detik == jeda_sebelum_blokir  # tanpa permintaan → tanpa jeda
    for h in hasil[:3]:
        assert h.status == "GAGAL" and h.jenis_error == "BATAS_PERMINTAAN"
    for h, k in zip(hasil[3:], kode[3:], strict=True):
        _assert_diblokir(h, k)


@pytest.mark.parametrize("maks", [1, 2, 5])  # diubah lewat config (P4)
def test_maks_simbol_kena_batas_berturut_dari_config(maks):
    keb, prov, _, _, _ = siapkan(ubah={"data.maks_simbol_kena_batas_berturut": maks}, bawaan="BATAS_PERMINTAAN")
    kode = [f"K{i:03d}" for i in range(maks + 2)]
    hasil = [keb.ambil(prov, k, MULAI, AKHIR) for k in kode]
    assert sorted(set(prov.kode_diminta())) == kode[:maks]
    assert len(prov.panggilan) == 2 * maks
    for h in hasil[:maks]:
        assert "diblokir sementara oleh Yahoo" not in h.pesan
    for h, k in zip(hasil[maks:], kode[maks:], strict=True):
        _assert_diblokir(h, k)


@pytest.mark.parametrize(
    "pemutus",
    [
        ["OK"],
        ["BATAS_PERMINTAAN", "OK"],  # kena batas, lolos pada coba lagi → tidak dihitung, penghitung kembali 0
        ["TIDAK_ADA_DATA"],
        ["FORMAT"],
        ["JARINGAN"] * 4,
    ],
    ids=["ok", "batas_lalu_ok", "tidak_ada_data", "format", "jaringan"],
)
def test_hasil_lain_mengembalikan_penghitung_berturut_ke_nol(pemutus):
    """maks = 3. A, B kena batas (2) → C hasil lain (0) → D, E kena batas (2): belum diblokir, F masih
    diminta; F kena batas (3) → G tidak diminta lagi."""
    batas = ["BATAS_PERMINTAAN", "BATAS_PERMINTAAN"]
    naskah = {"A": batas, "B": batas, "C": pemutus, "D": batas, "E": batas, "F": batas, "G": ["OK"]}
    keb, prov, _, _, _ = siapkan(naskah)
    hasil = {k: keb.ambil(prov, k, MULAI, AKHIR) for k in "ABCDEFG"}
    diminta = prov.kode_diminta()
    assert "F" in diminta
    assert "diblokir sementara oleh Yahoo" not in hasil["F"].pesan
    assert "G" not in diminta
    _assert_diblokir(hasil["G"], "G")


def test_blokir_hanya_berlaku_untuk_run_itu():
    keb, prov, _, _, _ = siapkan(bawaan="BATAS_PERMINTAAN")
    for k in ("AAAA", "BBBB", "CCCC", "DDDD"):
        keb.ambil(prov, k, MULAI, AKHIR)
    assert "DDDD" not in prov.kode_diminta()
    prov.bawaan = "OK"
    run_baru = KebijakanAmbil(cfg_uji(), tidur=TidurCatat([]))
    assert run_baru.ambil(prov, "DDDD", MULAI, AKHIR).status == "OK"
    assert prov.kode_diminta()[-1] == "DDDD"


def test_jeda_bawaan_lewat_waktu_tidur_jam_virtual():
    """Tanpa stub, semua jeda lewat `waktu.tidur()` (§8.1): pada mode virtual jam maju, tanpa tidur nyata
    (`time.sleep` dilarang oleh fixture modul ini)."""
    prov = ProviderNaskah({"AAAA": ["JARINGAN", "OK"], "BBBB": ["BATAS_PERMINTAAN", "OK"]})
    keb = KebijakanAmbil(cfg_uji())
    awal = waktu.sekarang()
    assert keb.ambil(prov, "AAAA", MULAI, AKHIR).status == "OK"
    assert keb.ambil(prov, "BBBB", MULAI, AKHIR).status == "OK"
    maju = (waktu.sekarang() - awal).total_seconds()
    assert maju >= 2.0 + 0.5 + 60.0  # backoff AAAA + jeda antar-permintaan + jeda batas permintaan BBBB


# ===========================================================================
# (2) pilih_seri — §8.2 tabel "Urutan cadangan", §6.9
# ===========================================================================


@pytest.mark.parametrize("csv", ["tidak_ada", "berbeda", "rusak"])
def test_utama_ok_dipakai_apa_adanya(ctx, csv):
    utama = tulis_cache(ctx, "AAAA", bingkai(HARI))
    if csv == "berbeda":
        tulis_csv(ctx, "AAAA", bingkai(HARI, c=1100.0))
    elif csv == "rusak":
        tulis_csv_rusak(ctx, "AAAA")
    s = pilih_seri(ctx, "AAAA", t=T)
    assert s.status == "OK"
    assert s.sumber == "yfinance"
    assert not s.csv_pengganti_penuh
    assert_ohlcv(s.bar, utama)
    assert (s.bar["sumber"] == "yfinance").all()


def test_sebagian_tanpa_cadangan_tetap_sebagian_dengan_catatan(ctx):
    utama = tulis_cache(ctx, "AAAA", bingkai(HARI[:-2]))  # s/d 2026-10-13 < t
    s = pilih_seri(ctx, "AAAA", t=T)
    assert s.status == "SEBAGIAN"
    assert s.sumber == "yfinance"
    assert not s.csv_pengganti_penuh
    assert_ohlcv(s.bar, utama)
    assert s.catatan and "csv_manual" in teks_catatan(s)


def test_sambungan_sah_menambah_bar_sesudah_bar_terakhir_utama(ctx):
    utama = tulis_cache(ctx, "AAAA", bingkai(HARI[:-2]))  # s/d 2026-10-13
    # 5 bar tumpang tindih (= min_bar_tumpang_tindih_sambungan): close 0,4% lebih tinggi (dalam toleransi
    # 0,5%) dan volume berbeda jauh (volume tidak termasuk OHLC yang dicocokkan, §6.9).
    tumpang = bingkai(HARI[-7:-2], c=1004.0, v=2_500_000.0)
    baru = bingkai(HARI[-2:], o=1100.0, c=1150.0, v=3_000_000.0)
    tulis_csv(ctx, "AAAA", pd.concat([tumpang, baru]))
    s = pilih_seri(ctx, "AAAA", t=T)
    assert s.status == "OK"
    assert s.sumber == "yfinance+csv_manual"
    assert not s.csv_pengganti_penuh
    assert list(s.bar.index) == list(utama.index) + list(baru.index)
    assert_ohlcv(s.bar.loc[utama.index], utama)  # bar tumpang tindih tetap nilai seri utama
    assert_ohlcv(s.bar.loc[baru.index], baru)
    assert (s.bar.loc[utama.index, "sumber"] == "yfinance").all()  # kolom sumber per bar (§8.2 butir 4)
    assert (s.bar.loc[baru.index, "sumber"] == "csv_manual").all()


def test_sambungan_sah_tanpa_bar_t_berstatus_sebagian(ctx):
    utama = tulis_cache(ctx, "AAAA", bingkai(HARI[:-3]))  # s/d 2026-10-12
    tulis_csv(ctx, "AAAA", pd.concat([bingkai(HARI[-8:-3]), bingkai(HARI[-3:-1], c=1100.0)]))  # s/d 10-14
    s = pilih_seri(ctx, "AAAA", t=T)
    assert s.status == "SEBAGIAN"
    assert s.sumber == "yfinance+csv_manual"
    assert list(s.bar.index) == list(utama.index) + [ts(HARI[-3]), ts(HARI[-2])]


def test_sambungan_hanya_mengambil_bar_sesudah_bar_terakhir_utama(ctx):
    """Bar CSV di dalam rentang seri utama (mis. tanggal yang tidak ada di cache) tidak ikut diambil."""
    lubang = HARI[-5]
    utama = tulis_cache(ctx, "AAAA", bingkai([d for d in HARI[:-2] if d != lubang]))
    tulis_csv(ctx, "AAAA", pd.concat([bingkai(HARI[-10:-2], c=1001.0), bingkai(HARI[-2:], c=1100.0)]))
    s = pilih_seri(ctx, "AAAA", t=T)
    assert s.status == "OK" and s.sumber == "yfinance+csv_manual"
    assert ts(lubang) not in s.bar.index
    assert list(s.bar.index) == list(utama.index) + [ts(HARI[-2]), ts(HARI[-1])]


def test_sambungan_tidak_mengisi_bar_yang_hilang(ctx):
    """P1: CSV tanpa bar 10-13 dan 10-14 → seri gabungan juga tanpa bar itu (tidak dibuat/diisi)."""
    utama = tulis_cache(ctx, "AAAA", bingkai(HARI[:-3]))  # s/d 2026-10-12
    tulis_csv(ctx, "AAAA", pd.concat([bingkai(HARI[-8:-3]), bingkai(HARI[-1:], c=1100.0)]))
    s = pilih_seri(ctx, "AAAA", t=T)
    assert s.status == "OK"
    assert list(s.bar.index) == list(utama.index) + [ts(T)]
    assert len(s.bar) == len(utama) + 1


@pytest.mark.parametrize("n_min", [5, 3])  # 5 = config_uji; 3 = diubah lewat config (P4)
@pytest.mark.parametrize("selisih, sah", [(0, True), (-1, False)], ids=["tepat_minimal", "kurang_satu"])
def test_jumlah_bar_tumpang_tindih_minimal(tmp_path, n_min, selisih, sah):
    ctx = konteks(tmp_path, {"data.min_bar_tumpang_tindih_sambungan": n_min})
    n = n_min + selisih
    utama = tulis_cache(ctx, "AAAA", bingkai(HARI[:-2]))
    csv = pd.concat([bingkai(HARI[-2 - n : -2]), bingkai(HARI[-2:], c=1100.0)])
    tulis_csv(ctx, "AAAA", csv)
    s = pilih_seri(ctx, "AAAA", t=T)
    assert s.status == "OK"
    if sah:
        assert s.sumber == "yfinance+csv_manual" and not s.csv_pengganti_penuh
        assert len(s.bar) == len(utama) + 2
    else:  # sambungan ditolak; CSV memuat bar t → pengganti penuh
        assert s.sumber == "csv_manual" and s.csv_pengganti_penuh
        assert_ohlcv(s.bar, csv)


NILAI_TUMPANG = {"open": 1000.0, "high": 1200.0, "low": 800.0, "close": 1000.0}


@pytest.mark.parametrize("tol", [0.5, 1.0])  # 0.5 = config_uji; 1.0 = diubah lewat config (P4)
@pytest.mark.parametrize("kolom", ["open", "high", "low", "close"])
@pytest.mark.parametrize("arah", [1, -1], ids=["naik", "turun"])
@pytest.mark.parametrize("lebih, sah", [(0.0, True), (0.01, False)], ids=["tepat_toleransi", "di_atas_toleransi"])
def test_toleransi_ohlc_tumpang_tindih(tmp_path, tol, kolom, arah, lebih, sah):
    """Selisih terhadap nilai cache tepat `toleransi_revisi_pct` masih cocok; Rp 0,01 lebih → ditolak."""
    ctx = konteks(tmp_path, {"data.toleransi_revisi_pct": tol})
    utama = tulis_cache(ctx, "AAAA", bingkai(HARI[:-2]))
    tumpang = bingkai(HARI[-7:-2])
    a = NILAI_TUMPANG[kolom]
    tumpang.loc[tumpang.index[2], kolom] = a + arah * (a * tol / 100.0 + lebih)
    csv = pd.concat([tumpang, bingkai(HARI[-2:], c=1100.0)])
    tulis_csv(ctx, "AAAA", csv)
    s = pilih_seri(ctx, "AAAA", t=T)
    assert s.status == "OK"
    if sah:
        assert s.sumber == "yfinance+csv_manual"
        assert_ohlcv(s.bar.loc[utama.index], utama)
    else:
        assert s.sumber == "csv_manual" and s.csv_pengganti_penuh
        assert_ohlcv(s.bar, csv)


@pytest.mark.parametrize(
    "cadangan", ["tumpang_tindih_tidak_cocok", "tumpang_tindih_kurang", "tanpa_bar_baru", "format_rusak"]
)
def test_sebagian_cadangan_ditolak_dan_tidak_ok_tetap_sebagian(ctx, cadangan):
    """Baris "SEBAGIAN | lainnya": seri utama apa adanya (SEBAGIAN) + catatan alasan cadangan ditolak."""
    utama = tulis_cache(ctx, "AAAA", bingkai(HARI[:-2]))  # s/d 2026-10-13
    baru = bingkai(HARI[-2:-1], c=1100.0)  # hanya 10-14: CSV tidak memuat bar t → bukan pengganti penuh
    if cadangan == "tumpang_tindih_tidak_cocok":
        tulis_csv(ctx, "AAAA", pd.concat([bingkai(HARI[-7:-2], c=1100.0), baru]))
    elif cadangan == "tumpang_tindih_kurang":
        tulis_csv(ctx, "AAAA", pd.concat([bingkai(HARI[-6:-2]), baru]))
    elif cadangan == "tanpa_bar_baru":
        tulis_csv(ctx, "AAAA", bingkai(HARI[-10:-2]))
    else:
        tulis_csv_rusak(ctx, "AAAA")
    s = pilih_seri(ctx, "AAAA", t=T)
    assert s.status == "SEBAGIAN"
    assert s.sumber == "yfinance"
    assert not s.csv_pengganti_penuh
    assert_ohlcv(s.bar, utama)
    assert (s.bar["sumber"] == "yfinance").all()
    assert s.catatan and "csv_manual" in teks_catatan(s)


def _siapkan_utama(ctx: KonteksApp, kode: str, keadaan: str) -> None:
    """Seri utama "GAGAL / kosong" dalam berbagai bentuk."""
    if keadaan == "tanpa_cache":
        return
    if keadaan == "meta_saja_tanpa_bar":  # TIDAK_ADA_DATA tercatat, belum pernah punya data
        cache = CacheProvider(ctx.path(ctx.cfg.data.folder_cache) / "yfinance")
        cache.tulis_meta(kode, buat_meta(None, sekarang=waktu.sekarang(), versi_provider="palsu-1.0",
                                         tidak_ada_data_sejak=date(2026, 10, 1)))
        return
    if keadaan == "struktur_gagal":  # bar bertanggal Sabtu → tidak lolos validasi struktur (§8.5)
        tulis_cache(ctx, kode, bingkai(sorted([*HARI, date(2026, 10, 10)])))
        return
    tulis_cache(ctx, kode, bingkai(HARI))
    parquet, meta = _path_cache(ctx, kode)
    if keadaan == "parquet_rusak":
        parquet.write_bytes(b"bukan parquet sama sekali")
    elif keadaan == "meta_rusak":
        meta.write_text("{ rusak", encoding="utf-8")
    else:
        raise ValueError(keadaan)


KEADAAN_UTAMA_GAGAL = ["tanpa_cache", "meta_saja_tanpa_bar", "parquet_rusak", "meta_rusak", "struktur_gagal"]


@pytest.mark.parametrize("keadaan", KEADAAN_UTAMA_GAGAL)
def test_utama_gagal_atau_kosong_dengan_csv_ok_dipakai_sebagai_pengganti_penuh(ctx, keadaan):
    _siapkan_utama(ctx, "BBBB", keadaan)
    csv = bingkai(HARI, o=1100.0, h=1150.0, low=1050.0, c=1120.0)
    tulis_csv(ctx, "BBBB", csv)
    s = pilih_seri(ctx, "BBBB", t=T)
    assert s.status == "OK"
    assert s.sumber == "csv_manual"
    assert s.csv_pengganti_penuh
    assert_ohlcv(s.bar, csv)
    assert (s.bar["sumber"] == "csv_manual").all()


@pytest.mark.parametrize("keadaan", ["tanpa_cache", "parquet_rusak", "struktur_gagal"])
@pytest.mark.parametrize("csv", ["tidak_ada", "tanpa_bar_t", "format_rusak"])
def test_utama_gagal_atau_kosong_tanpa_csv_ok_berstatus_gagal(ctx, keadaan, csv):
    _siapkan_utama(ctx, "BBBB", keadaan)
    if csv == "tanpa_bar_t":
        tulis_csv(ctx, "BBBB", bingkai(HARI[:-1]))
    elif csv == "format_rusak":
        tulis_csv_rusak(ctx, "BBBB")
    s = pilih_seri(ctx, "BBBB", t=T)
    assert s.status == "GAGAL"
    assert not s.ada_data
    assert not s.csv_pengganti_penuh


def test_sebagian_dengan_sambungan_tidak_sah_dan_csv_ok_menjadi_pengganti_penuh(ctx):
    tulis_cache(ctx, "AAAA", bingkai(HARI[:-2]), split={HARI[3]: 2.0})
    csv = bingkai(HARI, c=1100.0)  # seluruh tumpang tindih 10% berbeda → sambungan tidak sah
    tulis_csv(ctx, "AAAA", csv)
    tulis_split_csv(ctx, "AAAA", {HARI[10]: 3.0})
    s = pilih_seri(ctx, "AAAA", t=T)
    assert s.status == "OK"
    assert s.sumber == "csv_manual" and s.csv_pengganti_penuh
    assert_ohlcv(s.bar, csv)
    assert (s.bar["sumber"] == "csv_manual").all()
    # Pengganti penuh: split dari berkas CSV saja, bukan split cache yang ditinggalkan.
    assert s.split.to_dict() == {ts(HARI[10]): 3.0}


def test_split_sambungan_split_utama_ditambah_split_cadangan_sesudah_bar_terakhir_utama(ctx):
    tulis_cache(ctx, "AAAA", bingkai(HARI[:-2]), split={HARI[3]: 2.0})
    tulis_csv(ctx, "AAAA", pd.concat([bingkai(HARI[-7:-2]), bingkai(HARI[-2:], c=1100.0)]))
    # HARI[10] ≤ bar terakhir utama → tidak ikut; T > bar terakhir utama → ikut.
    tulis_split_csv(ctx, "AAAA", {HARI[10]: 3.0, T: 5.0})
    s = pilih_seri(ctx, "AAAA", t=T)
    assert s.sumber == "yfinance+csv_manual"
    assert s.split.to_dict() == {ts(HARI[3]): 2.0, ts(T): 5.0}


def test_csv_manual_tidak_pernah_di_cache_dan_pilih_seri_hanya_membaca(ctx):
    tulis_cache(ctx, "AAAA", bingkai(HARI[:-2]))
    tulis_csv(ctx, "AAAA", pd.concat([bingkai(HARI[-7:-2]), bingkai(HARI[-2:], c=1100.0)]))  # sambungan
    tulis_csv(ctx, "BBBB", bingkai(HARI))  # pengganti penuh
    sebelum = potret(ctx.akar)
    assert pilih_seri(ctx, "AAAA", t=T).sumber == "yfinance+csv_manual"
    assert pilih_seri(ctx, "BBBB", t=T).sumber == "csv_manual"
    assert potret(ctx.akar) == sebelum
    assert not (ctx.path(ctx.cfg.data.folder_cache) / "csv_manual").exists()


def test_berkas_csv_yang_diubah_atau_dihapus_langsung_berlaku(ctx):
    path = tulis_csv(ctx, "BBBB", bingkai(HARI, c=1100.0))
    assert (pilih_seri(ctx, "BBBB", t=T).bar["close"] == 1100.0).all()
    tulis_csv(ctx, "BBBB", bingkai(HARI, c=1150.0))  # diperbaiki
    assert (pilih_seri(ctx, "BBBB", t=T).bar["close"] == 1150.0).all()
    path.unlink()  # dihapus
    s = pilih_seri(ctx, "BBBB", t=T)
    assert s.status == "GAGAL" and not s.ada_data


def test_sambungan_hilang_saat_berkas_csv_dihapus(ctx):
    utama = tulis_cache(ctx, "AAAA", bingkai(HARI[:-2]))
    path = tulis_csv(ctx, "AAAA", pd.concat([bingkai(HARI[-7:-2]), bingkai(HARI[-2:], c=1100.0)]))
    assert pilih_seri(ctx, "AAAA", t=T).status == "OK"
    path.unlink()
    s = pilih_seri(ctx, "AAAA", t=T)
    assert s.status == "SEBAGIAN" and s.sumber == "yfinance"
    assert_ohlcv(s.bar, utama)


@pytest.mark.parametrize(
    "jam, t, final_hari_ini",
    [("19:29:59", date(2026, 10, 14), False), ("19:30:00", T, True)],  # batas = jadwal.jam_run (belum diverifikasi)
)
def test_bar_csv_yang_belum_final_dibuang(ctx, atur_waktu, jam, t, final_hari_ini):
    atur_waktu(f"2026-10-15T{jam}+07:00")
    # Termasuk bar bertanggal sesudah hari ini (2026-10-16): tidak pernah final.
    tulis_csv(ctx, "BBBB", bingkai([*HARI, date(2026, 10, 16)]))
    s = pilih_seri(ctx, "BBBB", t=t)  # tanpa `sekarang`: memakai waktu.sekarang()
    assert s.status == "OK" and s.csv_pengganti_penuh
    assert (ts(T) in s.bar.index) is final_hari_ini
    assert ts(date(2026, 10, 16)) not in s.bar.index
    assert s.bar.index[-1] == ts(t)


@pytest.mark.parametrize("jam, final_hari_ini", [("19:29:59", False), ("19:30:00", True)])
def test_bar_csv_belum_final_tidak_ikut_sambungan(ctx, atur_waktu, jam, final_hari_ini):
    atur_waktu(f"2026-10-15T{jam}+07:00")
    utama = tulis_cache(ctx, "AAAA", bingkai(HARI[:-3]))  # s/d 2026-10-12
    tulis_csv(ctx, "AAAA", pd.concat([bingkai(HARI[-8:-3]), bingkai(HARI[-3:], c=1100.0)]))
    t = T if final_hari_ini else HARI[-2]
    s = pilih_seri(ctx, "AAAA", t=t)
    assert s.status == "OK" and s.sumber == "yfinance+csv_manual"
    harapan = list(utama.index) + [ts(HARI[-3]), ts(HARI[-2])] + ([ts(T)] if final_hari_ini else [])
    assert list(s.bar.index) == harapan


JADWAL_FIKTIF = {  # bukan jadwal BEI: 16:00 + jeda 60 menit → batas bar final 17:00
    "terverifikasi": True,
    "sumber": f"{FIKTIF} (bukan jadwal resmi)",
    "tanggal_cek": "2026-10-01",
    "jam_penutupan": "16:00",
    "jam_pra_pembukaan": "08:45",
}


@pytest.mark.parametrize("jam, final_hari_ini", [("16:59:59", False), ("17:00:00", True)])
def test_bar_csv_belum_final_dengan_jadwal_sesi_terverifikasi_fiktif(tmp_path, jam, final_hari_ini):
    ctx = konteks(tmp_path, {"pasar.jadwal_sesi": JADWAL_FIKTIF})
    tulis_csv(ctx, "BBBB", bingkai(HARI))
    sekarang = pd.Timestamp(f"2026-10-15 {jam}", tz="Asia/Jakarta").to_pydatetime()
    t = T if final_hari_ini else HARI[-2]
    s = pilih_seri(ctx, "BBBB", t=t, sekarang=sekarang)
    assert s.status == "OK"
    assert (ts(T) in s.bar.index) is final_hari_ini


@pytest.mark.parametrize(
    "nama_berkas, ubah",
    [("IHSG", None), ("INDEKS", {"data.simbol_ihsg": {"yfinance": "^JKSE", "csv_manual": "INDEKS"}})],
)
def test_ihsg_pengganti_penuh_dari_csv_tanpa_volume(tmp_path, nama_berkas, ubah):
    ctx = konteks(tmp_path, ubah)
    tulis_csv(ctx, "IHSG", bingkai(HARI, c=1010.0), volume=False, nama=nama_berkas)
    if nama_berkas != "IHSG":  # berkas bernama IHSG.csv diabaikan bila config menunjuk nama lain (P4)
        tulis_csv(ctx, "IHSG", bingkai(HARI, c=999.0), volume=False, nama="IHSG")
    s = pilih_seri(ctx, "IHSG", t=T)
    assert s.adalah_indeks
    assert s.status == "OK" and s.sumber == "csv_manual" and s.csv_pengganti_penuh
    assert (s.bar["close"] == 1010.0).all()
    assert s.bar["volume"].isna().all()  # volume IHSG opsional: kosong tetap kosong (P1)


def test_urutan_provider_dari_config_tanpa_csv_manual(tmp_path):
    ctx = konteks(tmp_path, {"data.provider_urutan": ["yfinance"]})
    tulis_csv(ctx, "BBBB", bingkai(HARI))
    assert pilih_seri(ctx, "BBBB", t=T).status == "GAGAL"
    utama = tulis_cache(ctx, "AAAA", bingkai(HARI[:-2]))
    tulis_csv(ctx, "AAAA", pd.concat([bingkai(HARI[-7:-2]), bingkai(HARI[-2:], c=1100.0)]))
    s = pilih_seri(ctx, "AAAA", t=T)
    assert s.status == "SEBAGIAN" and s.sumber == "yfinance"
    assert_ohlcv(s.bar, utama)


def test_folder_cache_dan_manual_dari_config(tmp_path):
    ubah = {"data.folder_cache": "lain/cache", "data.folder_manual": "lain/manual"}
    ctx = konteks(tmp_path, ubah)
    tulis_cache(ctx, "AAAA", bingkai(HARI))
    tulis_csv(ctx, "BBBB", bingkai(HARI))
    assert (tmp_path / "lain" / "cache" / "yfinance" / "AAAA.parquet").is_file()
    assert (tmp_path / "lain" / "manual" / "BBBB.csv").is_file()
    assert pilih_seri(ctx, "AAAA", t=T).sumber == "yfinance"
    assert pilih_seri(ctx, "BBBB", t=T).sumber == "csv_manual"
    ctx_bawaan = konteks(tmp_path)  # folder bawaan config_uji: data/cache & manual_uji → kosong
    assert pilih_seri(ctx_bawaan, "AAAA", t=T).status == "GAGAL"
    assert pilih_seri(ctx_bawaan, "BBBB", t=T).status == "GAGAL"


# ===========================================================================
# (3) muat_pasar / baris_kualitas — §8.2, §8.5, §8.7
# ===========================================================================


def masukan_uji(ctx: KonteksApp):
    m = muat_masukan(ctx)
    assert not m.ada_error(), m.daftar_error()
    return m


def test_gagal_ambil_untuk_simbol_tanpa_data_di_mana_pun(akar_uji):
    ctx = konteks(akar_uji)
    termuat = muat_pasar(ctx, masukan_uji(ctx), ["UJIKOSONG"], akhir=T)
    s = termuat.seri["UJIKOSONG"]
    assert s.status == "GAGAL" and not s.ada_data
    for d in (T, HARI[-10]):  # alasan tingkat seri: berlaku di setiap tanggal
        assert termuat.pasar.tampilan(d).alasan_data("UJIKOSONG") == "GAGAL_AMBIL"
    [b] = baris_kualitas(termuat, ["UJIKOSONG"], T)
    assert b.kode == "UJIKOSONG"
    assert b.status_ambil == "GAGAL"
    assert b.alasan_dikeluarkan == "GAGAL_AMBIL"
    assert b.bar_terakhir == ""


@pytest.mark.parametrize("min_bar", [250, 40])  # 250 = config_uji; 40 = diubah lewat config (P4)
@pytest.mark.parametrize("selisih, alasan", [(-1, "RIWAYAT_CSV_PENDEK"), (0, None)], ids=["kurang_satu", "tepat"])
def test_riwayat_csv_pengganti_penuh_pendek(akar_uji, min_bar, selisih, alasan):
    ctx = konteks(akar_uji, {"universe.min_bar_riwayat": min_bar})
    tulis_csv(ctx, "UJICSV", bingkai(hari_bursa(T, min_bar + selisih)))
    termuat = muat_pasar(ctx, masukan_uji(ctx), ["UJICSV"], akhir=T)
    s = termuat.seri["UJICSV"]
    assert s.status == "OK" and s.csv_pengganti_penuh
    assert termuat.pasar.tampilan(T).alasan_data("UJICSV") == alasan
    [b] = baris_kualitas(termuat, ["UJICSV"], T)
    assert b.sumber == "csv_manual" and b.status_ambil == "OK"
    assert b.alasan_dikeluarkan == (alasan or "")


def test_riwayat_csv_pendek_dinilai_as_of(akar_uji):
    """Tepat 40 bar s/d T: lolos as-of T, tetapi as-of T−1 baru 39 bar → RIWAYAT_CSV_PENDEK."""
    ctx = konteks(akar_uji, {"universe.min_bar_riwayat": 40})
    tulis_csv(ctx, "UJICSV", bingkai(hari_bursa(T, 40)))
    termuat = muat_pasar(ctx, masukan_uji(ctx), ["UJICSV"], akhir=T)
    assert termuat.pasar.tampilan(T).alasan_data("UJICSV") is None
    assert termuat.pasar.tampilan(HARI[-2]).alasan_data("UJICSV") == "RIWAYAT_CSV_PENDEK"
    [b] = baris_kualitas(termuat, ["UJICSV"], HARI[-2])
    assert b.alasan_dikeluarkan == "RIWAYAT_CSV_PENDEK"
    assert b.bar_terakhir == HARI[-2].isoformat()


@pytest.mark.parametrize("asal", ["yfinance", "sambungan"])
def test_riwayat_pendek_yang_bukan_pengganti_penuh_csv_berlabel_riwayat_pendek(akar_uji, asal):
    ctx = konteks(akar_uji, {"universe.min_bar_riwayat": 40})
    hari = hari_bursa(T, 39)
    if asal == "yfinance":
        tulis_cache(ctx, "UJIPENDEK", bingkai(hari))
    else:
        tulis_cache(ctx, "UJIPENDEK", bingkai(hari[:-2]))
        tulis_csv(ctx, "UJIPENDEK", pd.concat([bingkai(hari[-7:-2]), bingkai(hari[-2:], c=1010.0)]))
    termuat = muat_pasar(ctx, masukan_uji(ctx), ["UJIPENDEK"], akhir=T)
    s = termuat.seri["UJIPENDEK"]
    assert s.status == "OK" and not s.csv_pengganti_penuh
    assert termuat.pasar.tampilan(T).alasan_data("UJIPENDEK") == "RIWAYAT_PENDEK"


# Kalender turunan (§8.7): tahun 2025 tidak punya entri di holidays.csv fixture (hanya 2026-12-25, 2027-01-01).
T25 = date(2025, 4, 30)
HARI25 = [ts_.date() for ts_ in pd.bdate_range("2025-03-03", T25)]
LIBUR_FIKTIF_25 = {date(2025, 3, 31), date(2025, 4, 1)}  # tidak ada saham breadth yang bertransaksi
SETENGAH_25 = date(2025, 4, 15)  # hanya AAAA (cache) + CCCC (CSV) bertransaksi: 2/4 = 50% ≥ 50% → hari bursa
SEPEREMPAT_25 = date(2025, 4, 16)  # hanya AAAA: 1/4 = 25% < 50% → bukan hari bursa


def _siapkan_breadth_2025(ctx: KonteksApp) -> None:
    """Breadth fixture = AAAA, BBBB, CCCC, DDDD. CCCC hanya ada sebagai CSV manual (pengganti penuh)."""
    def hari_untuk(kode: str) -> list[date]:
        hasil = []
        for d in HARI25:
            if d in LIBUR_FIKTIF_25:
                continue
            if d == SETENGAH_25 and kode not in ("AAAA", "CCCC"):
                continue
            if d == SEPEREMPAT_25 and kode != "AAAA":
                continue
            hasil.append(d)
        return hasil

    for kode in ("AAAA", "BBBB", "DDDD"):
        tulis_cache(ctx, kode, bingkai(hari_untuk(kode)))
    tulis_csv(ctx, "CCCC", bingkai(hari_untuk("CCCC")))
    # Saham non-breadth & IHSG bertransaksi di semua hari kerja: tidak memengaruhi kalender turunan.
    tulis_cache(ctx, "UJILUAR", bingkai(HARI25))
    tulis_cache(ctx, "IHSG", bingkai(HARI25, v=np.nan))


def test_kalender_turunan_dari_seri_breadth_terpilih(akar_uji):
    ctx = konteks(akar_uji)
    _siapkan_breadth_2025(ctx)
    masukan = masukan_uji(ctx)
    assert masukan.breadth == ["AAAA", "BBBB", "CCCC", "DDDD"]
    termuat = muat_pasar(ctx, masukan, ["UJILUAR"], akhir=T25)
    assert termuat.seri["CCCC"].csv_pengganti_penuh  # breadth dimuat walau tidak diminta
    kal = termuat.kalender
    for d in LIBUR_FIKTIF_25:
        assert not kal.adalah_hari_bursa(d)
    assert not kal.adalah_hari_bursa(SEPEREMPAT_25)
    assert kal.adalah_hari_bursa(SETENGAH_25)  # CCCC (dari CSV manual) ikut dihitung
    assert kal.adalah_hari_bursa(date(2025, 3, 28))
    # Bar saham non-breadth & IHSG pada tanggal bukan hari bursa dibuang (BAR_HARI_NONBURSA, §8.5).
    for kode in ("UJILUAR", "IHSG"):
        mentah = termuat.data[kode].mentah.index
        for d in (*LIBUR_FIKTIF_25, SEPEREMPAT_25):
            assert ts(d) not in mentah
        assert ts(SETENGAH_25) in mentah
    tanda = termuat.pasar.tampilan(T25).tanda("UJILUAR")
    assert tanda is not None and int(tanda["bar_hilang"]) == 0


def test_tanpa_data_breadth_kalender_senin_jumat(akar_uji):
    """Pembanding: tanpa seri breadth, hari-hari di atas tetap hari bursa (kalender turunan memang dari breadth)."""
    ctx = konteks(akar_uji)
    _siapkan_breadth_2025(ctx)
    masukan = dataclasses.replace(masukan_uji(ctx), breadth=[])
    termuat = muat_pasar(ctx, masukan, ["UJILUAR"], akhir=T25)
    for d in (*LIBUR_FIKTIF_25, SEPEREMPAT_25):
        assert termuat.kalender.adalah_hari_bursa(d)
        assert ts(d) in termuat.data["UJILUAR"].mentah.index


def _pembaruan_gagal(kode: str, jenis: str, pesan: str) -> HasilPembaruan:
    return HasilPembaruan(kode, "GAGAL", jenis, pesan, IsiCache(None, None, False, ""), False, False)


def test_baris_kualitas_membawa_status_sumber_pesan_dan_alasan_as_of(akar_uji):
    ctx = konteks(akar_uji, {"universe.min_bar_riwayat": 40})
    hari60 = hari_bursa(T, 60)
    tulis_cache(ctx, "AAAA", bingkai(hari60))  # OK
    tulis_cache(ctx, "CCCC", bingkai(hari60[:-1]))  # SEBAGIAN (s/d T−1), tanpa CSV
    tulis_csv(ctx, "DDDD", bingkai(hari_bursa(T, 20)))  # pengganti penuh pendek
    tulis_cache(ctx, "IHSG", bingkai(hari_bursa(T, 30), v=np.nan))  # indeks: min_bar_riwayat tidak berlaku
    pesan_asli = f"Failed to connect to query2.finance.yahoo.com port 443 ({FIKTIF})"
    info = {"BBBB": {"yfinance": _pembaruan_gagal("BBBB", "JARINGAN", pesan_asli)}}
    kode = ["AAAA", "BBBB", "CCCC", "DDDD", "IHSG"]
    termuat = muat_pasar(ctx, masukan_uji(ctx), kode, akhir=T, info=info)
    baris = baris_kualitas(termuat, kode, T)
    assert [b.kode for b in baris] == kode
    b = {x.kode: x for x in baris}

    assert (b["AAAA"].sumber, b["AAAA"].status_ambil, b["AAAA"].jenis_error) == ("yfinance", "OK", "")
    assert b["AAAA"].alasan_dikeluarkan == ""
    assert b["AAAA"].bar_terakhir == T.isoformat()
    assert b["AAAA"].bar_hilang_60 == 0

    assert b["BBBB"].status_ambil == "GAGAL"
    assert b["BBBB"].jenis_error == "JARINGAN"
    assert pesan_asli in b["BBBB"].pesan  # teks error asli (§8.5 kolom pesan)
    assert b["BBBB"].alasan_dikeluarkan == "GAGAL_AMBIL"

    assert (b["CCCC"].sumber, b["CCCC"].status_ambil) == ("yfinance", "SEBAGIAN")
    assert b["CCCC"].alasan_dikeluarkan == "DATA_BASI"
    assert b["CCCC"].bar_terakhir == HARI[-2].isoformat()
    assert b["CCCC"].pesan  # catatan alasan cadangan ditolak

    assert (b["DDDD"].sumber, b["DDDD"].status_ambil) == ("csv_manual", "OK")
    assert b["DDDD"].alasan_dikeluarkan == "RIWAYAT_CSV_PENDEK"

    assert (b["IHSG"].sumber, b["IHSG"].status_ambil, b["IHSG"].alasan_dikeluarkan) == ("yfinance", "OK", "")

    # As-of T−1: CCCC punya bar valid → tidak dikeluarkan.
    [c1] = baris_kualitas(termuat, ["CCCC"], HARI[-2])
    assert c1.alasan_dikeluarkan == ""
    assert c1.bar_terakhir == HARI[-2].isoformat()


def test_ihsg_selalu_ikut_dimuat(akar_uji):
    ctx = konteks(akar_uji)
    tulis_cache(ctx, "AAAA", bingkai(HARI))
    tulis_cache(ctx, "IHSG", bingkai(HARI, v=np.nan))
    masukan = masukan_uji(ctx)
    for diminta in (["AAAA"], ["IHSG", "AAAA"]):
        termuat = muat_pasar(ctx, masukan, diminta, akhir=T)
        assert "IHSG" in termuat.seri and "IHSG" in termuat.data
        assert termuat.data["IHSG"].adalah_indeks
        dv = termuat.pasar.tampilan(T)
        assert dv.ihsg() is not None and dv.ihsg().kode == "IHSG"
        assert dv.kode() == ["AAAA"]  # IHSG bukan saham
        assert dv.alasan_data("IHSG") is None
        [b] = baris_kualitas(termuat, ["IHSG"], T)
        assert b.status_ambil == "OK" and b.alasan_dikeluarkan == ""


def test_ihsg_tanpa_data_gagal_ambil(akar_uji):
    ctx = konteks(akar_uji)
    tulis_cache(ctx, "AAAA", bingkai(HARI))
    termuat = muat_pasar(ctx, masukan_uji(ctx), ["AAAA"], akhir=T)
    assert termuat.seri["IHSG"].status == "GAGAL"
    assert termuat.pasar.tampilan(T).alasan_data("IHSG") == "GAGAL_AMBIL"


def test_muat_pasar_hanya_membaca(akar_uji):
    ctx = konteks(akar_uji)
    tulis_cache(ctx, "AAAA", bingkai(HARI[:-2]))
    tulis_csv(ctx, "AAAA", pd.concat([bingkai(HARI[-7:-2]), bingkai(HARI[-2:], c=1100.0)]))
    tulis_csv(ctx, "BBBB", bingkai(HARI))
    tulis_csv(ctx, "IHSG", bingkai(HARI), volume=False)
    sebelum = potret(ctx.akar)
    termuat = muat_pasar(ctx, masukan_uji(ctx), ["AAAA", "BBBB"], akhir=T)
    baris_kualitas(termuat, ["AAAA", "BBBB", "IHSG"], T)
    assert potret(ctx.akar) == sebelum
    assert not (ctx.path(ctx.cfg.data.folder_cache) / "csv_manual").exists()


@pytest.mark.xfail(
    strict=True,
    reason="BUG: sumber._dari_csv membuang HasilAmbil.catatan dari CsvManualProvider "
    "('tanpa berkas split: faktor split dianggap 1'); peringatan §6.9 tidak sampai ke SeriTerpilih/berkas kualitas",
)
def test_peringatan_csv_manual_tanpa_berkas_split(akar_uji):
    """§6.9: tanpa `<KODE>_split.csv` faktor split dianggap 1 **dan laporan memberi peringatan**. Satu-satunya
    pembaca CSV manual adalah `pilih_seri`, jadi peringatan itu harus terbawa ke seri terpilih / berkas
    kualitas."""
    ctx = konteks(akar_uji)
    tulis_csv(ctx, "BBBB", bingkai(HARI))
    termuat = muat_pasar(ctx, masukan_uji(ctx), ["BBBB"], akhir=T)
    assert termuat.seri["BBBB"].csv_pengganti_penuh
    [b] = baris_kualitas(termuat, ["BBBB"], T)
    assert "split" in b.pesan.lower()

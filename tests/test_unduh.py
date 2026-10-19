"""Uji perintah `unduh` end-to-end di dalam proses (SPEC §19.2, §8.1–§8.5, §19.3, §19.5, §20.1;
bagian T-DQ, T-CA, T-OP, T-LP, T-INP §22.1).

Pasar yfinance palsu (`PasarPalsu`): per simbol yfinance ("<KODE>.JK", IHSG = "^JKSE") satu DataFrame
harian ber-zona Asia/Jakarta berbentuk keluaran `Ticker.history(auto_adjust=False, actions=True)`,
dipotong menurut argumen `start` (inklusif) dan `end` (eksklusif) seperti yfinance. Setiap permintaan
dicatat sebagai `(simbol, start, end)`; urutan permintaan dan jeda dicatat di `kejadian`.

- `jalankan_unduh` dipanggil dengan `pabrik_ticker` palsu, `keluar` pengumpul, dan `tidur` palsu.
- `__main__.main` tidak menerima `pabrik_ticker`: pabrik yfinance asli (`_siapkan_yfinance`) diganti pasar
  palsu, dan jeda berjalan lewat jam virtual `waktu.tidur()` (tidak pernah menunggu sungguhan).
- Setiap upaya koneksi jaringan sungguhan (socket, requests, curl_cffi, `yfinance.Ticker` asli) atau
  `time.sleep` sungguhan menggagalkan uji.

Semua kode saham, harga, dan volume FIKTIF – KHUSUS UJI. Waktu default (conftest): Kamis 2026-10-15
20:00 WIB, sesudah `jadwal.jam_run` 19:30 → t = 2026-10-15. `holidays.csv` fixture hanya memuat
tanggal Desember/Januari (FIKTIF), jadi semua hari Senin–Jumat dalam rentang uji adalah hari bursa.
"""

from __future__ import annotations

import csv
import io
import json
import re
import socket
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml

from asisten_bei import waktu
from asisten_bei.__main__ import main
from asisten_bei.config import muat_config
from asisten_bei.data import yfinance_provider as yfp
from asisten_bei.data.cache import CacheProvider
from asisten_bei.inputs import InputTidakValid
from asisten_bei.operasional import KunciDipegang, kunci_proses
from asisten_bei.unduh import jalankan_unduh

ZONA_BURSA = "Asia/Jakarta"
T = date(2026, 10, 15)  # Kamis; t pada waktu default conftest
MULAI_UNDUH = date(2013, 1, 1)  # data.mulai_unduh di tests/fixtures/config_uji.yaml
END_T = date(2026, 10, 16)  # `end` yfinance eksklusif = hari ini + 1 hari
MULAI_PASAR = "2025-06-02"
AKHIR_PASAR = "2026-10-30"
SPLIT_S = "2026-06-01"  # Senin

SIMBOL_FIXTURE = ["IHSG", "AAAA", "BBBB", "CCCC", "DDDD"]  # IHSG + universe ∪ breadth fixture
PESAN_BLOKIR = "diblokir sementara oleh Yahoo"
DISCLAIMER_TEKS = "BUKAN SARAN INVESTASI"

# §8.5 "Keluaran" — urutan kolom berkas kualitas, ditulis ulang dari SPEC (bukan diimpor dari src).
KOLOM_KUALITAS_SPEC = [
    "kode", "sumber", "status_ambil", "jenis_error", "pesan", "bar_terakhir", "bar_hilang_60",
    "anomali_terakhir", "split_belum_disesuaikan", "alasan_dikeluarkan",
]
# §4.3
ENUM_STATUS_AMBIL = {"OK", "SEBAGIAN", "GAGAL"}
ENUM_JENIS_ERROR = {"BATAS_PERMINTAAN", "TIDAK_ADA_DATA", "FORMAT", "JARINGAN"}
ENUM_ALASAN = {
    "GAGAL_AMBIL", "DATA_BASI", "BAR_HILANG", "ANOMALI", "SPLIT_BELUM_DISESUAIKAN", "RIWAYAT_PENDEK",
    "RIWAYAT_CSV_PENDEK", "KEMUNGKINAN_SUSPENSI", "TIDAK_LIKUID_RATA2", "DIKECUALIKAN", "BUKAN_KONSTITUEN",
}

HEADER_PORTO = "kode,tanggal_beli,harga_beli,lot,strategi,stop_awal,sektor,harga_per_tanggal,lembar,catatan\n"
HEADER_MANUAL = "tanggal,open,high,low,close,volume\n"


def yf(kode: str) -> str:
    """Simbol yfinance (config_uji: akhiran ".JK", IHSG = "^JKSE")."""
    return "^JKSE" if kode == "IHSG" else f"{kode}.JK"


# ---------------------------------------------------------------------------
# Penjaga: tanpa jaringan, tanpa tidur sungguhan
# ---------------------------------------------------------------------------


class _JaringanDilarang(BaseException):
    """Bukan turunan Exception: tidak tertangkap `except Exception` di provider/`main`."""


@pytest.fixture(autouse=True)
def _tanpa_jaringan_tanpa_tidur(monkeypatch):
    """Setiap upaya koneksi sungguhan atau pemakaian `yfinance.Ticker` asli dicatat & digagalkan;
    `time.sleep` tidak pernah benar-benar menunggu (semua jeda wajib lewat `waktu.tidur`, §8.1)."""
    import requests
    from curl_cffi import requests as curl_requests

    upaya: list[str] = []

    def tolak(nama):
        def _tolak(*args, **kwargs):
            upaya.append(nama)
            raise _JaringanDilarang(f"akses jaringan dalam uji: {nama}")

        return _tolak

    monkeypatch.setattr(socket.socket, "connect", tolak("socket.connect"))
    monkeypatch.setattr(socket, "create_connection", tolak("socket.create_connection"))
    monkeypatch.setattr(requests.Session, "request", tolak("requests"))
    monkeypatch.setattr(curl_requests.Session, "request", tolak("curl_cffi"))
    monkeypatch.setattr(yfp, "_siapkan_yfinance", tolak("yfinance.Ticker asli"))
    tidur_nyata: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda detik: tidur_nyata.append(detik))
    yield
    assert upaya == [], f"uji mencoba mengakses jaringan: {upaya}"
    # filelock boleh polling sangat singkat; jeda unduhan (≥ 0,5 s) tidak boleh lewat time.sleep.
    assert all(d < 0.1 for d in tidur_nyata), f"time.sleep sungguhan dipanggil: {tidur_nyata}"


# ---------------------------------------------------------------------------
# Pasar yfinance palsu
# ---------------------------------------------------------------------------


def galat_batas() -> BaseException:
    from yfinance.exceptions import YFRateLimitError

    return YFRateLimitError()  # "Too Many Requests. Rate limited." (HTTP 429)


def galat_jaringan() -> BaseException:
    return ConnectionError("Connection reset by peer (FIKTIF)")


def galat_format() -> BaseException:
    return Exception("Unexpected response structure FIKTIF-777")


def seri_yf(
    mulai: str | pd.Timestamp = MULAI_PASAR,
    akhir: str | pd.Timestamp = AKHIR_PASAR,
    *,
    awal: float = 1000.0,
    langkah: float = 0.0005,
    volume: float = 2_000_000.0,
    split: tuple[str, float] | None = None,
    q_split: float | None = None,
) -> pd.DataFrame:
    """Bar Senin–Jumat berbentuk `Ticker.history` (index tengah malam WIB, ber-zona waktu).

    close naik halus (`langkah` per bar, jauh di bawah ambang anomali). `split=(s, r)`: kolom Stock Splits
    = r pada s. `q_split`: rasio close_s / close_{s−1} yang tampak di data karena harga sebelum s TIDAK
    disesuaikan sumber (None = sudah disesuaikan: tidak ada lompatan)."""
    idx = pd.bdate_range(mulai, akhir)
    n = len(idx)
    close = awal * (1.0 + langkah) ** np.arange(n, dtype="float64")
    pecah = np.zeros(n, dtype="float64")
    if split is not None:
        s, r = split
        pos = idx.get_loc(pd.Timestamp(s))
        pecah[pos] = r
        if q_split is not None:
            close[:pos] = close[:pos] / q_split
    df = pd.DataFrame(
        {
            "Open": close * 0.998,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Close": close,
            "Adj Close": close * 0.97,  # harus dibuang provider (bukan harga yang dipakai)
            "Volume": volume + np.arange(n, dtype="float64"),
            "Dividends": 0.0,
            "Stock Splits": pecah,
        },
        index=pd.DatetimeIndex(idx).tz_localize(ZONA_BURSA),
    )
    df.index.name = "Date"
    return df


def tanggal_lokal(df: pd.DataFrame) -> pd.DatetimeIndex:
    return df.index.tz_convert(ZONA_BURSA).tz_localize(None).normalize()


def revisi(df: pd.DataFrame, tanggal: str, *, harga: float = 1.0, volume: float = 1.0) -> pd.DataFrame:
    """Salinan `df` dengan OHLC (konsisten) dan/atau volume bar `tanggal` dikalikan faktor."""
    baru = df.copy()
    baris = tanggal_lokal(baru) == pd.Timestamp(tanggal)
    assert baris.sum() == 1
    for k in ("Open", "High", "Low", "Close"):
        baru.loc[baris, k] = baru.loc[baris, k] * harga
    baru.loc[baris, "Volume"] = baru.loc[baris, "Volume"] * volume
    return baru


class _TickerPalsu:
    def __init__(self, pasar: "PasarPalsu", simbol: str):
        self.pasar = pasar
        self.simbol = simbol

    def history(self, **argumen):
        p = self.pasar
        p.panggilan.append((self.simbol, argumen["start"], argumen["end"]))
        p.kejadian.append(("minta", self.simbol))
        if p.saat_minta is not None:
            p.saat_minta(self.simbol)
        galat = p.galat_untuk(self.simbol)
        if galat is not None:
            raise galat()
        df = p.data.get(self.simbol)
        if df is None:  # simbol tidak dikenal: yfinance mengembalikan tabel kosong
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Adj Close", "Volume"])
        lokal = tanggal_lokal(df)
        m = (lokal >= pd.Timestamp(argumen["start"])) & (lokal < pd.Timestamp(argumen["end"]))
        return df[m].copy()


class PasarPalsu:
    """Pengganti `yfinance.Ticker`: `pabrik(simbol)` → objek dengan `history(**argumen)`.

    - `data`: simbol yfinance → DataFrame (`seri_yf`).
    - `aturan`: simbol → pabrik galat (selalu) atau list per panggilan (None = jawab data).
    - `aturan_ke`: urutan simbol berbeda yang diminta (mulai 1) → pabrik galat (tak bergantung urutan kode).
    - `semua`: pabrik galat untuk setiap permintaan.
    """

    def __init__(self):
        self.data: dict[str, pd.DataFrame] = {}
        self.aturan: dict[str, Any] = {}
        self.aturan_ke: dict[int, Any] = {}
        self.semua: Any = None
        self.panggilan: list[tuple[str, date, date]] = []
        self.kejadian: list[tuple[str, Any]] = []
        self.saat_minta: Any = None

    def pabrik(self, simbol: str) -> _TickerPalsu:
        return _TickerPalsu(self, simbol)

    def galat_untuk(self, simbol: str):
        if self.semua is not None:
            return self.semua
        urutan = list(dict.fromkeys(s for s, _, _ in self.panggilan))
        ke = urutan.index(simbol) + 1
        if ke in self.aturan_ke:
            return self.aturan_ke[ke]
        a = self.aturan.get(simbol)
        if isinstance(a, list):
            n = sum(1 for s, _, _ in self.panggilan if s == simbol)
            return a[n - 1] if n - 1 < len(a) else None
        return a

    def diminta(self) -> list[str]:
        """Simbol berbeda yang diminta, menurut urutan permintaan pertamanya."""
        return list(dict.fromkeys(s for s, _, _ in self.panggilan))

    def mulai_per_simbol(self) -> dict[str, list[date]]:
        hasil: dict[str, list[date]] = {}
        for s, m, _ in self.panggilan:
            hasil.setdefault(s, []).append(m)
        return hasil


@pytest.fixture
def pasar() -> PasarPalsu:
    p = PasarPalsu()
    for i, kode in enumerate(SIMBOL_FIXTURE):
        p.data[yf(kode)] = seri_yf(awal=1000.0 + 250.0 * i)
    return p


# ---------------------------------------------------------------------------
# Pembantu run & berkas
# ---------------------------------------------------------------------------


@dataclass
class Run:
    hasil: Any
    keluaran: list[str]
    tidur: list[float]

    @property
    def teks(self) -> str:
        return "\n".join(self.keluaran)


def unduh(ctx, pasar: PasarPalsu, **opsi) -> Run:
    keluaran: list[str] = []
    jeda: list[float] = []

    def tidur(detik: float) -> None:
        jeda.append(float(detik))
        pasar.kejadian.append(("tidur", float(detik)))

    hasil = jalankan_unduh(ctx, pabrik_ticker=pasar.pabrik, keluar=keluaran.append, tidur=tidur, **opsi)
    return Run(hasil, keluaran, jeda)


def path_kualitas(akar: Path, stempel: str = "20261015-200000") -> Path:
    return akar / "output" / "kualitas" / f"{stempel}_unduh.csv"


def baca_kualitas(path: Path) -> tuple[str, list[str], list[dict[str, str]]]:
    """(baris pertama, header, baris data) berkas kualitas."""
    teks = path.read_text(encoding="utf-8")
    pertama, sisa = teks.split("\n", 1)
    rd = csv.reader(io.StringIO(sisa))
    header = next(rd)
    return pertama, header, [dict(zip(header, r, strict=True)) for r in rd]


def baris_per_kode(path: Path) -> dict[str, dict[str, str]]:
    _, _, rows = baca_kualitas(path)
    kode = [r["kode"] for r in rows]
    assert len(kode) == len(set(kode)), f"kode ganda di berkas kualitas: {kode}"
    periksa_enum(rows)
    return {r["kode"]: r for r in rows}


def periksa_enum(rows: list[dict[str, str]]) -> None:
    for r in rows:
        assert r["status_ambil"] in ENUM_STATUS_AMBIL, r
        assert r["jenis_error"] in ENUM_JENIS_ERROR | {""}, r
        assert r["alasan_dikeluarkan"] in ENUM_ALASAN | {""}, r


def tulis(path: Path, isi: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(isi, encoding="utf-8")


def tulis_manual(akar: Path, kode: str, df: pd.DataFrame, *, dari=None, sampai=T, kali: float = 1.0) -> Path:
    """`manual_uji/<KODE>.csv` (§6.9; config_uji `data.folder_manual: manual_uji`) dari bar `seri_yf`."""
    lokal = tanggal_lokal(df)
    m = np.ones(len(df), dtype=bool)
    if dari is not None:
        m &= lokal >= pd.Timestamp(dari)
    if sampai is not None:
        m &= lokal <= pd.Timestamp(sampai)
    nilai = df.loc[m, ["Open", "High", "Low", "Close", "Volume"]].to_numpy(dtype="float64")
    baris = [HEADER_MANUAL]
    for ts, (o, h, lo, c, v) in zip(lokal[m], nilai, strict=True):
        baris.append(f"{ts:%Y-%m-%d},{o * kali:.4f},{h * kali:.4f},{lo * kali:.4f},{c * kali:.4f},{v:.0f}\n")
    path = akar / "manual_uji" / f"{kode}.csv"
    tulis(path, "".join(baris))
    return path


def ubah_config(akar: Path, perubahan: dict[str, Any]):
    """Ubah salinan config_uji.yaml di direktori sementara (kunci bertitik), lalu muat ulang."""
    path = akar / "config_uji.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for kunci, nilai in perubahan.items():
        *jalur, akhir = kunci.split(".")
        simpul = data
        for k in jalur:
            simpul = simpul[k]
        simpul[akhir] = nilai
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return muat_config(path, akar)


def cache_yf(akar: Path) -> CacheProvider:
    return CacheProvider(akar / "data" / "cache" / "yfinance")


def ada_baris(keluaran: list[str], *potongan: str) -> bool:
    """Ada satu baris keluaran yang memuat semua potongan teks (tanpa beda huruf besar/kecil)."""
    return any(all(p.lower() in b.lower() for p in potongan) for b in keluaran)


# ---------------------------------------------------------------------------
# Simbol yang diunduh (§8.2 "Simbol yang diunduh", §19.2 --simbol)
# ---------------------------------------------------------------------------


def test_simbol_default_ihsg_dulu_lalu_universe_breadth_portofolio_riwayat(ctx_uji, akar_uji, pasar):
    # EEEE hanya di portfolio.csv (di luar universe → sektor wajib); FFFF hanya di universe_history.csv.
    tulis(akar_uji / "input_uji" / "portfolio.csv",
          HEADER_PORTO + "EEEE,2026-10-08,2850,2,MANUAL,2720,Konsumen (FIKTIF),,,\n")
    tulis(akar_uji / "input_uji" / "universe_history.csv",
          "kode,tanggal_masuk,tanggal_keluar\nFFFF,2015-01-01,2019-07-31\nAAAA,2015-01-01,\n")
    for kode in ("EEEE", "FFFF"):
        pasar.data[yf(kode)] = seri_yf(awal=500.0)

    run = unduh(ctx_uji, pasar)

    semua = [s for s, _, _ in pasar.panggilan]
    assert semua[0] == "^JKSE"  # IHSG lebih dulu
    assert len(semua) == len(set(semua)), "setiap simbol diminta tepat sekali"
    harap = {yf(k) for k in [*SIMBOL_FIXTURE, "EEEE", "FFFF"]}
    assert set(semua) == harap
    assert set(baris_per_kode(path_kualitas(akar_uji))) == {*SIMBOL_FIXTURE, "EEEE", "FFFF"}
    assert run.hasil.kode_keluar == 0


def test_simbol_default_tanpa_riwayat_dan_portofolio_kosong(ctx_uji, akar_uji, pasar):
    # Fixture: universe AAAA–CCCC, breadth AAAA–DDDD, portofolio kosong, universe_history.csv tidak ada.
    pasar.data[yf("ZZZZ")] = seri_yf()  # tersedia di Yahoo tetapi tidak ada di daftar mana pun
    unduh(ctx_uji, pasar)
    assert pasar.diminta()[0] == "^JKSE"
    assert set(pasar.diminta()) == {yf(k) for k in SIMBOL_FIXTURE}
    assert len(pasar.panggilan) == len(SIMBOL_FIXTURE)


def test_opsi_simbol_membatasi_unduhan_dan_ihsg_boleh_disebut(ctx_uji, akar_uji, pasar):
    run = unduh(ctx_uji, pasar, simbol=["CCCC", "IHSG"])
    assert sorted(s for s, _, _ in pasar.panggilan) == sorted(["CCCC.JK", "^JKSE"])
    rows = baris_per_kode(path_kualitas(akar_uji))
    assert {"CCCC", "IHSG"} <= set(rows)
    assert rows["CCCC"]["status_ambil"] == "OK" and rows["IHSG"]["status_ambil"] == "OK"
    # Kode yang ada di daftar (dan IHSG) tidak memicu peringatan "di luar daftar".
    assert not ada_baris(run.keluaran, "PERINGATAN", "CCCC")
    assert not ada_baris(run.keluaran, "PERINGATAN", "IHSG")


def test_opsi_simbol_di_luar_daftar_tetap_diunduh_dengan_peringatan(ctx_uji, akar_uji, pasar):
    pasar.data[yf("ZZZZ")] = seri_yf(awal=700.0)
    run = unduh(ctx_uji, pasar, simbol=["AAAA", "ZZZZ"])
    assert sorted(s for s, _, _ in pasar.panggilan) == ["AAAA.JK", "ZZZZ.JK"]
    assert ada_baris(run.keluaran, "PERINGATAN", "ZZZZ")
    assert not ada_baris(run.keluaran, "PERINGATAN", "AAAA")
    rows = baris_per_kode(path_kualitas(akar_uji))
    assert rows["ZZZZ"]["status_ambil"] == "OK" and rows["ZZZZ"]["sumber"] == "yfinance"
    assert cache_yf(akar_uji).path_parquet("ZZZZ").is_file()


# ---------------------------------------------------------------------------
# Cache (§8.3): lokasi, isi, inkremental, --penuh
# ---------------------------------------------------------------------------


def test_berkas_cache_ditulis_di_data_cache_yfinance(ctx_uji, akar_uji, pasar):
    unduh(ctx_uji, pasar)
    folder = akar_uji / "data" / "cache" / "yfinance"
    # Hanya <KODE>.parquet + <KODE>.meta.json (IHSG disimpan dengan kode, bukan "^JKSE"); tidak ada sisa
    # berkas sementara penulisan atomik.
    assert sorted(p.name for p in folder.iterdir()) == sorted(
        f"{k}{akhiran}" for k in SIMBOL_FIXTURE for akhiran in (".parquet", ".meta.json")
    )
    assert not (akar_uji / "data" / "cache" / "csv_manual").exists()  # csv_manual tidak di-cache (§8.2)
    cache = cache_yf(akar_uji)
    for k in SIMBOL_FIXTURE:
        isi = cache.baca(k)
        assert not isi.rusak and isi.pesan == "", (k, isi.pesan)
        sumber = pasar.data[yf(k)]
        lokal = tanggal_lokal(sumber)
        harap = sumber[lokal <= pd.Timestamp(T)]
        assert list(isi.bar.index) == list(lokal[lokal <= pd.Timestamp(T)])
        np.testing.assert_allclose(isi.bar["close"].to_numpy(), harap["Close"].to_numpy())  # Close, bukan Adj Close
        assert set(isi.bar["sumber"]) == {"yfinance"}
        assert isi.meta["bar_terakhir"] == T.isoformat()
        assert isi.meta["jumlah_bar"] == len(harap)


@pytest.mark.parametrize("mulai_unduh", [None, "2021-03-01"])
def test_unduhan_pertama_mulai_dari_data_mulai_unduh(akar_uji, pasar, mulai_unduh):
    if mulai_unduh is None:
        ctx, harap = muat_config(akar_uji / "config_uji.yaml", akar_uji), MULAI_UNDUH
    else:
        ctx, harap = ubah_config(akar_uji, {"data.mulai_unduh": mulai_unduh}), date.fromisoformat(mulai_unduh)
    unduh(ctx, pasar)
    assert sorted(pasar.panggilan) == sorted((yf(k), harap, END_T) for k in SIMBOL_FIXTURE)


@pytest.mark.parametrize(
    "bar_cek_ulang, mulai_harap",
    [
        # 10 bar terakhir s/d Rabu 14 Okt: 14, 13, 12, 9, 8, 7, 6, 5, 2, 1 Okt → mulai 1 Okt.
        (None, date(2026, 10, 1)),
        # 3 bar terakhir: 14, 13, 12 Okt → mulai 12 Okt (nilai dari config, P4).
        (3, date(2026, 10, 12)),
    ],
)
def test_run_kedua_inkremental_hanya_bar_baru_ditambah_bar_cek_ulang(
    akar_uji, pasar, atur_waktu, bar_cek_ulang, mulai_harap
):
    ctx = (muat_config(akar_uji / "config_uji.yaml", akar_uji) if bar_cek_ulang is None
           else ubah_config(akar_uji, {"data.bar_cek_ulang": bar_cek_ulang}))
    atur_waktu("2026-10-14T20:00:00+07:00")
    unduh(ctx, pasar)
    assert sorted(pasar.panggilan) == sorted((yf(k), MULAI_UNDUH, date(2026, 10, 15)) for k in SIMBOL_FIXTURE)
    jumlah_awal = {k: cache_yf(akar_uji).baca(k).meta["jumlah_bar"] for k in SIMBOL_FIXTURE}

    pasar.panggilan.clear()
    atur_waktu("2026-10-15T20:00:00+07:00")
    run = unduh(ctx, pasar)

    assert sorted(pasar.panggilan) == sorted((yf(k), mulai_harap, END_T) for k in SIMBOL_FIXTURE)
    for k in SIMBOL_FIXTURE:
        meta = cache_yf(akar_uji).baca(k).meta
        assert meta["bar_terakhir"] == T.isoformat()
        assert meta["jumlah_bar"] == jumlah_awal[k] + 1
    rows = baris_per_kode(path_kualitas(akar_uji))
    assert all(rows[k]["status_ambil"] == "OK" and rows[k]["bar_terakhir"] == T.isoformat() for k in SIMBOL_FIXTURE)
    assert run.hasil.kode_keluar == 0


def test_opsi_penuh_mengunduh_ulang_semua_dari_mulai_unduh(ctx_uji, akar_uji, pasar, atur_waktu):
    atur_waktu("2026-10-14T20:00:00+07:00")
    unduh(ctx_uji, pasar)
    pasar.panggilan.clear()
    atur_waktu("2026-10-15T20:00:00+07:00")
    unduh(ctx_uji, pasar, penuh=True)
    assert sorted(pasar.panggilan) == sorted((yf(k), MULAI_UNDUH, END_T) for k in SIMBOL_FIXTURE)
    assert all(cache_yf(akar_uji).baca(k).meta["bar_terakhir"] == T.isoformat() for k in SIMBOL_FIXTURE)


def test_revisi_close_di_tumpang_tindih_memicu_unduh_ulang_penuh(ctx_uji, akar_uji, pasar, atur_waktu):
    atur_waktu("2026-10-14T20:00:00+07:00")
    unduh(ctx_uji, pasar)
    pasar.data["AAAA.JK"] = revisi(pasar.data["AAAA.JK"], "2026-10-08", harga=1.02)  # 2% > 0,5%
    pasar.panggilan.clear()
    atur_waktu("2026-10-15T20:00:00+07:00")
    unduh(ctx_uji, pasar)
    mulai = pasar.mulai_per_simbol()
    assert mulai["AAAA.JK"] == [date(2026, 10, 1), MULAI_UNDUH]  # inkremental, lalu unduh ulang penuh
    assert all(mulai[yf(k)] == [date(2026, 10, 1)] for k in SIMBOL_FIXTURE if k != "AAAA")
    bar = cache_yf(akar_uji).baca("AAAA").bar
    assert bar.loc["2026-10-08", "close"] == pytest.approx(
        float(pasar.data["AAAA.JK"].loc[tanggal_lokal(pasar.data["AAAA.JK"]) == "2026-10-08", "Close"].iloc[0])
    )


def test_unduh_ulang_penuh_karena_revisi_close_tercantum_di_ringkasan(ctx_uji, akar_uji, pasar, atur_waktu):
    atur_waktu("2026-10-14T20:00:00+07:00")
    unduh(ctx_uji, pasar)
    pasar.data["AAAA.JK"] = revisi(pasar.data["AAAA.JK"], "2026-10-08", harga=1.02)
    atur_waktu("2026-10-15T20:00:00+07:00")
    run = unduh(ctx_uji, pasar)
    assert pasar.mulai_per_simbol()["AAAA.JK"][-1] == MULAI_UNDUH  # memang diunduh ulang penuh
    assert ada_baris(run.keluaran, "AAAA", "penuh") or ada_baris(run.keluaran, "AAAA", "disesuaikan ulang")


def test_revisi_volume_menimpa_cache_tanpa_unduh_ulang_penuh(ctx_uji, akar_uji, pasar, atur_waktu):
    atur_waktu("2026-10-14T20:00:00+07:00")
    unduh(ctx_uji, pasar)
    pasar.data["BBBB.JK"] = revisi(pasar.data["BBBB.JK"], "2026-10-08", volume=1.5)  # 50% > 5%
    pasar.panggilan.clear()
    atur_waktu("2026-10-15T20:00:00+07:00")
    unduh(ctx_uji, pasar)
    assert pasar.mulai_per_simbol()["BBBB.JK"] == [date(2026, 10, 1)]
    bar = cache_yf(akar_uji).baca("BBBB").bar
    baru = pasar.data["BBBB.JK"]
    assert bar.loc["2026-10-08", "volume"] == float(baru.loc[tanggal_lokal(baru) == "2026-10-08", "Volume"].iloc[0])


def test_revisi_volume_dicatat_di_berkas_kualitas(ctx_uji, akar_uji, pasar, atur_waktu):
    atur_waktu("2026-10-14T20:00:00+07:00")
    unduh(ctx_uji, pasar)
    pasar.data["BBBB.JK"] = revisi(pasar.data["BBBB.JK"], "2026-10-08", volume=1.5)
    atur_waktu("2026-10-15T20:00:00+07:00")
    unduh(ctx_uji, pasar)
    assert "volume" in baris_per_kode(path_kualitas(akar_uji))["BBBB"]["pesan"].lower()


# ---------------------------------------------------------------------------
# Split belum disesuaikan sumber (§8.4): unduh ulang penuh tanpa --penuh, sampai lolos
# ---------------------------------------------------------------------------


def test_split_belum_disesuaikan_di_cache_diunduh_ulang_penuh_sampai_lolos(ctx_uji, akar_uji, pasar, atur_waktu):
    rusak = seri_yf(awal=1250.0, split=(SPLIT_S, 2.0), q_split=0.5)  # q ≈ 1/r: harga lama tidak dibagi 2
    pasar.data["BBBB.JK"] = rusak

    # Run 1 (unduhan pertama): split terdeteksi; status ini didahulukan dari ANOMALI (lompatan −50%).
    atur_waktu("2026-10-14T20:00:00+07:00")
    unduh(ctx_uji, pasar)
    r1 = baris_per_kode(path_kualitas(akar_uji, "20261014-200000"))["BBBB"]
    assert r1["split_belum_disesuaikan"] == "1"
    assert r1["alasan_dikeluarkan"] == "SPLIT_BELUM_DISESUAIKAN"

    # Run 2 tanpa --penuh, sumber masih salah: BBBB diunduh ulang penuh, simbol lain inkremental.
    pasar.panggilan.clear()
    atur_waktu("2026-10-15T20:00:00+07:00")
    run2 = unduh(ctx_uji, pasar)
    mulai = pasar.mulai_per_simbol()
    assert mulai["BBBB.JK"] == [MULAI_UNDUH]
    assert all(mulai[yf(k)] == [date(2026, 10, 1)] for k in SIMBOL_FIXTURE if k != "BBBB")
    assert ada_baris(run2.keluaran, "BBBB", "penuh")  # catatan unduh ulang penuh di ringkasan (§19.2)
    assert not ada_baris(run2.keluaran, "AAAA", "penuh")
    r2 = baris_per_kode(path_kualitas(akar_uji))["BBBB"]
    assert r2["split_belum_disesuaikan"] == "1" and r2["alasan_dikeluarkan"] == "SPLIT_BELUM_DISESUAIKAN"
    assert run2.hasil.kode_keluar == 0

    # Run 3: sumber sudah menyesuaikan → masih diunduh ulang penuh (cache lama masih salah), lalu lolos.
    pasar.data["BBBB.JK"] = seri_yf(awal=1250.0, split=(SPLIT_S, 2.0))
    pasar.panggilan.clear()
    atur_waktu("2026-10-16T20:00:00+07:00")
    unduh(ctx_uji, pasar)
    assert pasar.mulai_per_simbol()["BBBB.JK"] == [MULAI_UNDUH]
    r3 = baris_per_kode(path_kualitas(akar_uji, "20261016-200000"))["BBBB"]
    assert r3["split_belum_disesuaikan"] == "0" and r3["alasan_dikeluarkan"] == ""

    # Run 4 (Senin): sudah lolos → kembali inkremental. 10 bar terakhir s/d Jumat 16 Okt → mulai 5 Okt.
    pasar.panggilan.clear()
    atur_waktu("2026-10-19T20:00:00+07:00")
    unduh(ctx_uji, pasar)
    assert pasar.mulai_per_simbol()["BBBB.JK"] == [date(2026, 10, 5)]


@pytest.mark.parametrize(
    "q, belum",
    [
        (0.69, True),  # |ln q − ln(1/2)| < |ln q|: lebih dekat ke 1/r (batas q = 1/√2 ≈ 0,707)
        (0.72, False),  # lebih dekat ke 1: dianggap sudah disesuaikan
    ],
)
def test_batas_q_split_menentukan_unduh_ulang_penuh(ctx_uji, akar_uji, pasar, atur_waktu, q, belum):
    pasar.data["BBBB.JK"] = seri_yf(awal=1250.0, split=(SPLIT_S, 2.0), q_split=q)  # lompatan < 40%: bukan anomali
    atur_waktu("2026-10-14T20:00:00+07:00")
    unduh(ctx_uji, pasar)
    r1 = baris_per_kode(path_kualitas(akar_uji, "20261014-200000"))["BBBB"]
    assert r1["split_belum_disesuaikan"] == ("1" if belum else "0")
    assert r1["alasan_dikeluarkan"] == ("SPLIT_BELUM_DISESUAIKAN" if belum else "")
    pasar.panggilan.clear()
    atur_waktu("2026-10-15T20:00:00+07:00")
    unduh(ctx_uji, pasar)
    assert pasar.mulai_per_simbol()["BBBB.JK"] == [MULAI_UNDUH if belum else date(2026, 10, 1)]


@pytest.mark.parametrize("sumber_diperbaiki", [False, True], ids=["sumber_masih_salah", "sumber_diperbaiki"])
def test_unduh_ulang_penuh_karena_split_dicatat_di_berkas_kualitas(
    ctx_uji, akar_uji, pasar, atur_waktu, sumber_diperbaiki
):
    pasar.data["BBBB.JK"] = seri_yf(awal=1250.0, split=(SPLIT_S, 2.0), q_split=0.5)
    atur_waktu("2026-10-14T20:00:00+07:00")
    unduh(ctx_uji, pasar)
    if sumber_diperbaiki:
        pasar.data["BBBB.JK"] = seri_yf(awal=1250.0, split=(SPLIT_S, 2.0))
    pasar.panggilan.clear()
    atur_waktu("2026-10-15T20:00:00+07:00")
    unduh(ctx_uji, pasar)
    assert pasar.mulai_per_simbol()["BBBB.JK"] == [MULAI_UNDUH]  # unduh ulang penuh memang terjadi
    pesan = baris_per_kode(path_kualitas(akar_uji))["BBBB"]["pesan"].lower()
    assert "split" in pesan and re.search(r"penuh|ulang", pesan), pesan


# ---------------------------------------------------------------------------
# Berkas kualitas (§8.5 "Keluaran", §19.2): format & penilaian as-of t
# ---------------------------------------------------------------------------


def test_berkas_kualitas_nama_disclaimer_header_satu_baris_per_simbol(ctx_uji, akar_uji, pasar, atur_waktu):
    atur_waktu("2026-10-15T21:07:09+07:00")  # nama berkas dari jam virtual WIB
    run = unduh(ctx_uji, pasar)
    folder = akar_uji / "output" / "kualitas"
    assert [p.name for p in folder.iterdir()] == ["20261015-210709_unduh.csv"]
    pertama, header, rows = baca_kualitas(folder / "20261015-210709_unduh.csv")
    assert pertama.startswith("# " + DISCLAIMER_TEKS)
    assert header == KOLOM_KUALITAS_SPEC
    assert sorted(r["kode"] for r in rows) == sorted(SIMBOL_FIXTURE)
    periksa_enum(rows)
    for r in rows:
        assert r["status_ambil"] == "OK" and r["sumber"] == "yfinance"
        assert r["bar_terakhir"] == T.isoformat() and r["alasan_dikeluarkan"] == ""
        assert r["split_belum_disesuaikan"] == "0" and r["bar_hilang_60"] == "0"
    assert DISCLAIMER_TEKS in run.teks
    assert run.hasil.kode_keluar == 0


def test_kualitas_status_ambil_alasan_dan_ringkasan_campuran(ctx_uji, akar_uji, pasar):
    pasar.data["BBBB.JK"] = seri_yf(akhir="2026-10-14", awal=1250.0)  # bar t belum ada di Yahoo
    pasar.data["CCCC.JK"] = seri_yf(mulai="2026-05-25", awal=1500.0)  # ±100 bar valid < min_bar_riwayat 250
    pasar.aturan["DDDD.JK"] = galat_format  # tanpa cache & tanpa CSV manual

    run = unduh(ctx_uji, pasar)
    rows = baris_per_kode(path_kualitas(akar_uji))

    assert (rows["IHSG"]["status_ambil"], rows["IHSG"]["alasan_dikeluarkan"]) == ("OK", "")
    assert (rows["AAAA"]["status_ambil"], rows["AAAA"]["alasan_dikeluarkan"]) == ("OK", "")
    assert rows["AAAA"]["bar_terakhir"] == T.isoformat()
    assert (rows["BBBB"]["status_ambil"], rows["BBBB"]["alasan_dikeluarkan"]) == ("SEBAGIAN", "DATA_BASI")
    assert rows["BBBB"]["bar_terakhir"] == "2026-10-14"
    assert (rows["CCCC"]["status_ambil"], rows["CCCC"]["alasan_dikeluarkan"]) == ("OK", "RIWAYAT_PENDEK")
    d = rows["DDDD"]
    assert (d["status_ambil"], d["jenis_error"], d["alasan_dikeluarkan"]) == ("GAGAL", "FORMAT", "GAGAL_AMBIL")
    assert "FIKTIF-777" in d["pesan"]  # pesan error asli
    assert d["bar_terakhir"] == ""

    # Ringkasan: jumlah per status_ambil + simbol gagal beserta pesan aslinya.
    assert re.search(r"\bOK\b\W{0,3}3\b", run.teks), run.teks
    assert re.search(r"\bSEBAGIAN\b\W{0,3}1\b", run.teks), run.teks
    assert re.search(r"\bGAGAL\b\W{0,3}1\b", run.teks), run.teks
    assert ada_baris(run.keluaran, "DDDD", "FIKTIF-777")
    assert run.hasil.kode_keluar == 0  # kegagalan per simbol tidak mengubah kode keluar


@pytest.mark.parametrize(
    "jam, t_harap",
    [
        ("18:00:00", date(2026, 10, 14)),  # sebelum batas bar final (jam_run 19:30) → t = kemarin
        ("19:29:59", date(2026, 10, 14)),
        ("19:30:00", T),  # tepat di batas → hari ini final
    ],
)
def test_kualitas_dinilai_as_of_t_dan_bar_belum_final_tidak_disimpan(
    ctx_uji, akar_uji, pasar, atur_waktu, jam, t_harap
):
    atur_waktu(f"2026-10-15T{jam}+07:00")
    run = unduh(ctx_uji, pasar)
    # Permintaan tetap sampai hari ini (end eksklusif = besok), tetapi bar 15 Okt yang belum final dibuang.
    assert {a for _, _, a in pasar.panggilan} == {END_T}
    rows = baris_per_kode(path_kualitas(akar_uji, "20261015-" + jam.replace(":", "")))
    for k in SIMBOL_FIXTURE:
        assert rows[k]["status_ambil"] == "OK", rows[k]
        assert rows[k]["bar_terakhir"] == t_harap.isoformat()
        assert rows[k]["alasan_dikeluarkan"] == ""
        assert cache_yf(akar_uji).baca(k).bar.index[-1].date() == t_harap
    assert t_harap.isoformat() in run.teks


# ---------------------------------------------------------------------------
# Cadangan CSV manual (§6.9, §8.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n_bar, alasan", [(249, "RIWAYAT_CSV_PENDEK"), (250, "")])
def test_csv_manual_pengganti_penuh_saat_yfinance_gagal(ctx_uji, akar_uji, pasar, n_bar, alasan):
    pasar.aturan["AAAA.JK"] = galat_format
    hari = pd.bdate_range(end=pd.Timestamp(T), periods=n_bar)
    tulis_manual(akar_uji, "AAAA", seri_yf(awal=1000.0), dari=hari[0], sampai=T)

    run = unduh(ctx_uji, pasar)

    r = baris_per_kode(path_kualitas(akar_uji))["AAAA"]
    assert r["sumber"] == "csv_manual"
    assert r["status_ambil"] == "OK"
    assert r["bar_terakhir"] == T.isoformat()
    assert r["alasan_dikeluarkan"] == alasan
    # csv_manual tidak di-cache, dan bar CSV tidak pernah masuk cache yfinance.
    assert not (akar_uji / "data" / "cache" / "csv_manual").exists()
    assert not cache_yf(akar_uji).path_parquet("AAAA").exists()
    assert run.hasil.kode_keluar == 0


@pytest.mark.parametrize(
    "dari, kali, sambung",
    [
        ("2026-10-07", 1.0, True),  # 6 bar tumpang tindih cocok → sambungan sah
        ("2026-10-08", 1.0, True),  # tepat min_bar_tumpang_tindih_sambungan = 5
        ("2026-10-09", 1.0, False),  # 4 bar < 5 → ditolak; CSV (memuat t) jadi pengganti penuh
        ("2026-10-07", 1.05, False),  # selisih 5% > toleransi_revisi_pct 0,5% → ditolak
    ],
)
def test_csv_manual_sambungan_di_atas_seri_sebagian(ctx_uji, akar_uji, pasar, dari, kali, sambung):
    lengkap = seri_yf(awal=1250.0)
    pasar.data["BBBB.JK"] = lengkap[tanggal_lokal(lengkap) <= pd.Timestamp("2026-10-14")]  # tanpa bar t
    tulis_manual(akar_uji, "BBBB", lengkap, dari=dari, sampai=T, kali=kali)

    unduh(ctx_uji, pasar)

    r = baris_per_kode(path_kualitas(akar_uji))["BBBB"]
    assert r["status_ambil"] == "OK"
    assert r["bar_terakhir"] == T.isoformat()
    if sambung:
        assert "yfinance" in r["sumber"] and "csv_manual" in r["sumber"], r["sumber"]
        assert r["alasan_dikeluarkan"] == ""
    else:
        assert r["sumber"] == "csv_manual"
        assert r["alasan_dikeluarkan"] == "RIWAYAT_CSV_PENDEK"  # pengganti penuh hanya beberapa bar
    # Cache yfinance tidak memuat bar t dari CSV (§8.2 butir 5).
    assert cache_yf(akar_uji).baca("BBBB").bar.index[-1] == pd.Timestamp("2026-10-14")


# ---------------------------------------------------------------------------
# Kebijakan coba ulang per run (§8.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("jeda", [None, 1.25])
def test_jeda_antar_permintaan_sebelum_setiap_permintaan_kecuali_pertama(akar_uji, pasar, jeda):
    ctx = (muat_config(akar_uji / "config_uji.yaml", akar_uji) if jeda is None
           else ubah_config(akar_uji, {"data.jeda_antar_permintaan_detik": jeda}))
    harap = 0.5 if jeda is None else 1.25
    unduh(ctx, pasar)
    k = pasar.kejadian
    assert len(k) == 2 * len(SIMBOL_FIXTURE) - 1
    assert all(e[0] == "minta" for e in k[0::2])  # dimulai dengan permintaan (tanpa jeda di depan)
    assert k[1::2] == [("tidur", harap)] * (len(SIMBOL_FIXTURE) - 1)


def test_jaringan_dicoba_lagi_tiga_kali_dengan_jeda_2_4_8(ctx_uji, akar_uji, pasar):
    pasar.aturan["AAAA.JK"] = galat_jaringan  # selalu putus
    pasar.aturan["BBBB.JK"] = [galat_jaringan, None]  # pulih pada percobaan kedua

    run = unduh(ctx_uji, pasar)

    k = pasar.kejadian
    a = [i for i, e in enumerate(k) if e == ("minta", "AAAA.JK")]
    assert len(a) == 4  # 1 permintaan + 3 kali coba lagi
    assert k[a[0]: a[-1] + 1] == [
        ("minta", "AAAA.JK"), ("tidur", 2.0), ("minta", "AAAA.JK"), ("tidur", 4.0),
        ("minta", "AAAA.JK"), ("tidur", 8.0), ("minta", "AAAA.JK"),
    ]
    b = [i for i, e in enumerate(k) if e == ("minta", "BBBB.JK")]
    assert len(b) == 2 and k[b[0] + 1: b[1]] == [("tidur", 2.0)]
    rows = baris_per_kode(path_kualitas(akar_uji))
    assert (rows["AAAA"]["status_ambil"], rows["AAAA"]["jenis_error"]) == ("GAGAL", "JARINGAN")
    assert rows["AAAA"]["alasan_dikeluarkan"] == "GAGAL_AMBIL"
    assert (rows["BBBB"]["status_ambil"], rows["BBBB"]["alasan_dikeluarkan"]) == ("OK", "")
    assert run.hasil.kode_keluar == 0


def test_batas_permintaan_jeda_lalu_satu_kali_coba_lagi_yang_berhasil(ctx_uji, akar_uji, pasar):
    pasar.aturan["AAAA.JK"] = [galat_batas, None]
    run = unduh(ctx_uji, pasar)
    k = pasar.kejadian
    a = [i for i, e in enumerate(k) if e == ("minta", "AAAA.JK")]
    assert len(a) == 2 and k[a[0] + 1: a[1]] == [("tidur", 60.0)]  # jeda_batas_permintaan_detik
    assert baris_per_kode(path_kualitas(akar_uji))["AAAA"]["status_ambil"] == "OK"
    assert set(pasar.diminta()) == {yf(k) for k in SIMBOL_FIXTURE}
    assert PESAN_BLOKIR not in run.teks


@pytest.mark.parametrize("maks", [None, 2])
def test_batas_permintaan_berturut_menghentikan_unduhan_sisa_memakai_cache_csv(akar_uji, pasar, maks):
    ctx = (muat_config(akar_uji / "config_uji.yaml", akar_uji) if maks is None
           else ubah_config(akar_uji, {"data.maks_simbol_kena_batas_berturut": maks}))
    n = 3 if maks is None else maks
    pasar.semua = galat_batas  # setiap permintaan kena HTTP 429
    tulis_manual(akar_uji, "DDDD", seri_yf(awal=1750.0), sampai=T)  # cadangan untuk satu simbol

    run = unduh(ctx, pasar)

    diminta = pasar.diminta()
    assert len(diminta) == n  # sesudah n simbol berturut-turut kena batas, tidak ada permintaan lagi
    assert all(sum(1 for s, _, _ in pasar.panggilan if s == x) == 2 for x in diminta)  # + satu kali coba lagi
    assert len(pasar.panggilan) == 2 * n
    assert run.tidur.count(60.0) == n
    assert PESAN_BLOKIR in run.teks

    rows = baris_per_kode(path_kualitas(akar_uji))
    assert set(rows) == set(SIMBOL_FIXTURE)
    tidak_diminta = [k for k in SIMBOL_FIXTURE if yf(k) not in diminta]
    assert len(tidak_diminta) == len(SIMBOL_FIXTURE) - n
    for k in tidak_diminta:
        assert PESAN_BLOKIR in rows[k]["pesan"], rows[k]
    for k in SIMBOL_FIXTURE:
        if k == "DDDD":
            assert (rows[k]["sumber"], rows[k]["status_ambil"]) == ("csv_manual", "OK")
        else:
            assert rows[k]["status_ambil"] == "GAGAL" and rows[k]["alasan_dikeluarkan"] == "GAGAL_AMBIL"
            assert rows[k]["jenis_error"] == "BATAS_PERMINTAAN"
    assert run.hasil.kode_keluar == 0


def test_batas_permintaan_simbol_tak_diminta_memakai_cache_lama(ctx_uji, akar_uji, pasar, atur_waktu):
    atur_waktu("2026-10-14T20:00:00+07:00")
    unduh(ctx_uji, pasar)
    pasar.panggilan.clear()
    pasar.semua = galat_batas
    atur_waktu("2026-10-15T20:00:00+07:00")
    run = unduh(ctx_uji, pasar)
    assert len(pasar.diminta()) == 3
    assert PESAN_BLOKIR in run.teks
    rows = baris_per_kode(path_kualitas(akar_uji))
    for k in SIMBOL_FIXTURE:
        # Cache tidak berubah dan tidak pernah dianggap memuat bar t (§8.2 butir 5, §20.2).
        assert cache_yf(akar_uji).baca(k).meta["bar_terakhir"] == "2026-10-14"
        assert rows[k]["sumber"] == "yfinance"
        assert rows[k]["status_ambil"] == "SEBAGIAN"
        assert rows[k]["bar_terakhir"] == "2026-10-14"
        assert rows[k]["alasan_dikeluarkan"] == "DATA_BASI"


def test_penghitung_batas_kembali_nol_setelah_hasil_lain(ctx_uji, akar_uji, pasar):
    # Simbol ke-1 & ke-2 kena batas, ke-3 berhasil, ke-4 & ke-5 kena batas: tidak pernah 3 berturut-turut.
    for ke in (1, 2, 4, 5):
        pasar.aturan_ke[ke] = galat_batas
    run = unduh(ctx_uji, pasar)
    assert set(pasar.diminta()) == {yf(k) for k in SIMBOL_FIXTURE}
    assert len(pasar.panggilan) == 2 * 4 + 1
    assert PESAN_BLOKIR not in run.teks
    rows = baris_per_kode(path_kualitas(akar_uji))
    assert sorted(r["status_ambil"] for r in rows.values()) == ["GAGAL"] * 4 + ["OK"]


def test_peringatan_blokir_walau_batas_tercapai_pada_simbol_terakhir(ctx_uji, akar_uji, pasar):
    pasar.semua = galat_batas
    run = unduh(ctx_uji, pasar, simbol=["AAAA", "BBBB", "CCCC"])
    assert len(pasar.diminta()) == 3 and len(pasar.panggilan) == 6
    assert PESAN_BLOKIR in run.teks


@pytest.mark.parametrize("n_format, saran", [(10, True), (9, True), (8, False)])
def test_saran_versi_yfinance_bila_minimal_90_persen_simbol_format(ctx_uji, akar_uji, pasar, n_format, saran):
    kode = ["IHSG", "AAAA", "BBBB", "CCCC", "DDDD", "EEEE", "FFFF", "GGGG", "HHHH", "IIII"]
    for i, k in enumerate(kode):
        pasar.data.setdefault(yf(k), seri_yf(awal=600.0 + 50.0 * i))
    for i, k in enumerate(kode[len(kode) - n_format:]):
        if i % 2:  # struktur respons berubah: kolom Close hilang
            pasar.data[yf(k)] = pasar.data[yf(k)].drop(columns=["Close"])
        else:  # exception tak dikenal dari yfinance
            pasar.aturan[yf(k)] = galat_format

    run = unduh(ctx_uji, pasar, simbol=kode)

    rows = baris_per_kode(path_kualitas(akar_uji))
    assert sum(r["jenis_error"] == "FORMAT" for r in rows.values()) == n_format
    assert ("versi yfinance" in run.teks.lower()) is saran
    assert run.hasil.kode_keluar == 0


def test_tidak_ada_data_tidak_diminta_lagi_sebelum_coba_lagi_hari(ctx_uji, akar_uji, pasar, atur_waktu):
    # ZZZZ tidak dikenal Yahoo (tabel kosong) → TIDAK_ADA_DATA, dicoba lagi paling cepat 7 hari kemudian.
    unduh(ctx_uji, pasar, simbol=["ZZZZ"])
    assert pasar.panggilan == [("ZZZZ.JK", MULAI_UNDUH, END_T)]
    r = baris_per_kode(path_kualitas(akar_uji))["ZZZZ"]
    assert (r["status_ambil"], r["jenis_error"], r["alasan_dikeluarkan"]) == ("GAGAL", "TIDAK_ADA_DATA", "GAGAL_AMBIL")

    pasar.panggilan.clear()
    atur_waktu("2026-10-21T20:00:00+07:00")  # 6 hari kemudian
    unduh(ctx_uji, pasar, simbol=["ZZZZ"])
    assert pasar.panggilan == []
    r = baris_per_kode(path_kualitas(akar_uji, "20261021-200000"))["ZZZZ"]
    assert (r["status_ambil"], r["jenis_error"]) == ("GAGAL", "TIDAK_ADA_DATA")

    atur_waktu("2026-10-22T20:00:00+07:00")  # 7 hari kemudian
    unduh(ctx_uji, pasar, simbol=["ZZZZ"])
    assert pasar.panggilan == [("ZZZZ.JK", MULAI_UNDUH, date(2026, 10, 23))]


# ---------------------------------------------------------------------------
# Kunci proses (§19.3)
# ---------------------------------------------------------------------------


def test_unduh_memegang_kunci_selama_mengunduh_lalu_melepasnya(ctx_uji, akar_uji, pasar):
    amatan: list[tuple[Any, bool]] = []

    def saat_minta(simbol: str) -> None:
        if amatan:
            return
        info = json.loads((akar_uji / "data" / ".kunci.info").read_text(encoding="utf-8"))
        try:
            with kunci_proses(akar_uji, "harian"):
                dipegang = False
        except KunciDipegang:
            dipegang = True
        amatan.append((info.get("perintah"), dipegang))

    pasar.saat_minta = saat_minta
    unduh(ctx_uji, pasar)
    assert amatan == [("unduh", True)]
    with kunci_proses(akar_uji, "harian"):  # dilepas setelah selesai
        pass


def test_input_tidak_valid_melempar_sebelum_mengunduh(ctx_uji, akar_uji, pasar):
    tulis(akar_uji / "input_uji" / "universe.csv",
          "kode,nama,sektor\nAAAA,Satu (FIKTIF),Keuangan\nAAAA,Dua (FIKTIF),Energi\n")
    with pytest.raises(InputTidakValid):
        unduh(ctx_uji, pasar)
    assert pasar.panggilan == []
    assert not (akar_uji / "data" / "cache").exists()


# ---------------------------------------------------------------------------
# CLI `python -m asisten_bei unduh` (§19.2, §20.1, §19.5, T-OP, T-LP, T-INP)
# ---------------------------------------------------------------------------


@pytest.fixture
def cli(monkeypatch, pasar, akar_uji):
    """`main([...])` dengan pabrik yfinance = pasar palsu; aliran sys dipulihkan sesudah uji."""
    monkeypatch.setattr(yfp, "_siapkan_yfinance", lambda: pasar.pabrik)
    for nama in ("stdout", "stderr", "excepthook"):  # main memasang penyaring rahasia di sini (§19.5)
        monkeypatch.setattr(sys, nama, getattr(sys, nama))

    def jalankan(*argumen: str, opsi_global_dulu: bool = False) -> tuple[int, list[str]]:
        keluaran: list[str] = []
        umum = ["--config", str(akar_uji / "config_uji.yaml"), "--akar", str(akar_uji)]
        argv = [*umum, "unduh", *argumen] if opsi_global_dulu else ["unduh", *argumen, *umum]
        return main(argv, keluar=keluaran.append), keluaran

    return jalankan


@pytest.mark.parametrize("opsi_global_dulu", [False, True])
def test_main_unduh_kode_0_opsi_global_sebelum_atau_sesudah_perintah(cli, akar_uji, pasar, opsi_global_dulu):
    kode, keluaran = cli(opsi_global_dulu=opsi_global_dulu)
    assert kode == 0
    assert set(pasar.diminta()) == {yf(k) for k in SIMBOL_FIXTURE}
    assert path_kualitas(akar_uji).is_file()
    assert DISCLAIMER_TEKS in "\n".join(keluaran)


def test_main_kode_0_walau_ada_simbol_gagal_dan_log_ditulis(cli, akar_uji, pasar):
    pasar.aturan["DDDD.JK"] = galat_format
    kode, keluaran = cli()
    assert kode == 0
    teks = "\n".join(keluaran)
    assert DISCLAIMER_TEKS in teks
    assert ada_baris(keluaran, "DDDD", "FIKTIF-777")
    log = akar_uji / "output" / "log" / "2026-10-15.log"
    assert log.is_file()
    isi = log.read_text(encoding="utf-8")
    assert any("DDDD" in b and "FIKTIF-777" in b for b in isi.splitlines()), isi  # simbol gagal + pesan asli


def test_main_log_mencatat_sumber_data_per_simbol(cli, akar_uji, pasar):
    pasar.aturan["AAAA.JK"] = galat_format
    tulis_manual(akar_uji, "AAAA", seri_yf(awal=1000.0), sampai=T)
    kode, _ = cli()
    assert kode == 0
    assert baris_per_kode(path_kualitas(akar_uji))["AAAA"]["sumber"] == "csv_manual"
    isi = (akar_uji / "output" / "log" / "2026-10-15.log").read_text(encoding="utf-8")
    assert any("AAAA" in b and "csv_manual" in b for b in isi.splitlines()), isi


def test_main_opsi_penuh(cli, pasar, atur_waktu):
    atur_waktu("2026-10-14T20:00:00+07:00")
    assert cli()[0] == 0
    pasar.panggilan.clear()
    atur_waktu("2026-10-15T20:00:00+07:00")
    kode, _ = cli("--penuh")
    assert kode == 0
    assert sorted(pasar.panggilan) == sorted((yf(k), MULAI_UNDUH, END_T) for k in SIMBOL_FIXTURE)


def test_main_opsi_simbol(cli, akar_uji, pasar):
    kode, _ = cli("--simbol", "AAAA,IHSG")
    assert kode == 0
    assert sorted(pasar.diminta()) == sorted(["AAAA.JK", "^JKSE"])


def test_main_opsi_simbol_tidak_valid_kode_3_tanpa_unduhan(cli, akar_uji, pasar):
    kode, keluaran = cli("--simbol", "AAAA.JK")
    assert kode == 3
    assert pasar.panggilan == []
    assert not (akar_uji / "output" / "kualitas").exists()


def test_main_jeda_lewat_jam_virtual_waktu_tidur(cli, pasar):
    awal = waktu.sekarang()
    kode, _ = cli()
    assert kode == 0
    assert len(pasar.panggilan) == 5
    # 4 jeda × jeda_antar_permintaan_detik 0,5 s, semuanya lewat waktu.tidur() (jam virtual maju).
    assert waktu.sekarang() - awal == timedelta(seconds=4 * 0.5)


@pytest.mark.parametrize(
    "isi_universe",
    [
        "kode,nama,sektor\nAAAA,Satu (FIKTIF),Keuangan\nAAAA,Dua (FIKTIF),Energi\n",
        "kode;nama;sektor\nAAAA;Satu (FIKTIF);Keuangan\n",
    ],
    ids=["kode_ganda", "pemisah_titik_koma"],
)
def test_main_input_tidak_valid_kode_3_tanpa_unduhan(cli, akar_uji, pasar, isi_universe):
    tulis(akar_uji / "input_uji" / "universe.csv", isi_universe)
    kode, keluaran = cli()
    assert kode == 3
    assert pasar.panggilan == []
    assert not (akar_uji / "data" / "cache").exists()
    assert not (akar_uji / "output" / "kualitas").exists()
    teks = "\n".join(keluaran)
    assert "universe.csv" in teks  # pesan menunjuk berkasnya
    assert DISCLAIMER_TEKS in teks
    with kunci_proses(akar_uji, "harian"):  # kunci tidak tertinggal
        pass


def test_main_config_tidak_valid_kode_3_tanpa_unduhan(cli, akar_uji, pasar):
    path = akar_uji / "config_uji.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "\nkunci_asing_fiktif: 1\n", encoding="utf-8")
    kode, keluaran = cli()
    assert kode == 3
    assert pasar.panggilan == []
    assert not (akar_uji / "output" / "kualitas").exists()
    assert DISCLAIMER_TEKS in "\n".join(keluaran)


def test_main_kunci_dipegang_proses_lain_kode_5(cli, akar_uji, pasar):
    with kunci_proses(akar_uji, "harian"):
        kode, keluaran = cli()
    assert kode == 5
    teks = "\n".join(keluaran)
    assert re.search(r"proses lain sedang berjalan: harian sejak .*2026-10-15.*20:00", teks), teks
    assert pasar.panggilan == []
    assert not (akar_uji / "output" / "kualitas").exists()
    assert not (akar_uji / "data" / "cache").exists()
    assert DISCLAIMER_TEKS in teks


def test_main_error_tak_terduga_kode_1_dan_kunci_dilepas(cli, akar_uji, pasar):
    # Berkas kualitas tidak dapat ditulis (bukan terkunci Excel): folder bernama sama menghalangi.
    path_kualitas(akar_uji).mkdir(parents=True)
    kode, keluaran = cli()
    assert kode == 1
    assert DISCLAIMER_TEKS in "\n".join(keluaran)
    with kunci_proses(akar_uji, "harian"):
        pass


# ---------------------------------------------------------------------------
# Perbaikan hasil tinjauan: klaim unduh ulang penuh, kegagalan yang tertolong CSV, bar terakhir SEBAGIAN
# ---------------------------------------------------------------------------


def test_unduh_ulang_penuh_split_yang_gagal_tidak_diklaim_berhasil(ctx_uji, akar_uji, pasar, atur_waktu):
    pasar.data[yf("DDDD")] = seri_yf(awal=1750.0, split=(SPLIT_S, 5.0), q_split=0.2)
    atur_waktu("2026-10-14T20:00:00+07:00")
    unduh(ctx_uji, pasar)
    atur_waktu("2026-10-15T20:00:00+07:00")
    pasar.aturan[yf("DDDD")] = galat_format
    run = unduh(ctx_uji, pasar)
    assert not any(b.startswith("Unduh ulang penuh karena split") and "DDDD" in b for b in run.keluaran)
    assert ada_baris(run.keluaran, "DDDD", "perlu unduh ulang penuh", "gagal")
    assert run.hasil.penuh_karena_split == []
    rows = baris_per_kode(path_kualitas(akar_uji))
    assert "gagal" in rows["DDDD"]["pesan"] and "split belum disesuaikan" in rows["DDDD"]["pesan"]


def test_ringkasan_mencantumkan_unduhan_gagal_yang_tertolong_csv(ctx_uji, akar_uji, pasar):
    pasar.aturan[yf("AAAA")] = galat_format
    tulis_manual(akar_uji, "AAAA", seri_yf(awal=1250.0), sampai=T)
    run = unduh(ctx_uji, pasar)
    assert run.hasil.status["OK"] == len(SIMBOL_FIXTURE)  # seri AAAA tetap OK lewat csv_manual
    assert ada_baris(run.keluaran, "yfinance", "GAGAL FORMAT", "AAAA", "dipakai csv_manual")


def test_ringkasan_seri_sebagian_menyebut_bar_terakhir(ctx_uji, akar_uji, pasar):
    pasar.data[yf("BBBB")] = seri_yf(akhir="2026-10-13", awal=1500.0)
    run = unduh(ctx_uji, pasar)
    assert ada_baris(run.keluaran, "SEBAGIAN", "BBBB", f"tanpa bar {T.isoformat()}", "bar terakhir 2026-10-13")

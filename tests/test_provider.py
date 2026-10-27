"""Uji provider data (SPEC §8.1, §6.9; bagian T-DQ §22.1): YFinanceProvider & CsvManualProvider.

Tidak ada jaringan sungguhan: YFinanceProvider selalu diberi `pabrik_ticker` palsu, atau — untuk uji
integrasi dengan yfinance asli — lapisan HTTP yfinance (`YfData.get`/`cache_get`) diganti responder
palsu dan cache zona waktu yfinance diganti versi dummy (tidak menulis ke folder pengguna).
"""

from __future__ import annotations

import errno
import inspect
import json
import socket
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from asisten_bei.config import baca_yaml, muat_config, validasi_dict
from asisten_bei.data import yfinance_provider as yfp
from asisten_bei.data.csv_provider import CATATAN_TANPA_SPLIT, CsvManualProvider
from asisten_bei.data.kualitas import validasi_struktur
from asisten_bei.data.provider import KOLOM_BAR, NAMA_INDEX
from asisten_bei.data.yfinance_provider import YFinanceProvider, klasifikasi_error
from asisten_bei.inputs import PESAN_REGIONAL
from asisten_bei.operasional import PenyaringRahasia

AKAR_PROYEK = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Fixture & pembantu
# ---------------------------------------------------------------------------


class _JaringanDilarang(BaseException):
    """Bukan turunan Exception: tidak tertangkap `except Exception` di provider/yfinance."""


@pytest.fixture(autouse=True)
def _tanpa_jaringan(monkeypatch):
    """Setiap upaya koneksi sungguhan (socket Python, requests, curl_cffi) dicatat & digagalkan."""
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
    yield
    assert upaya == [], f"uji mencoba mengakses jaringan: {upaya}"


@pytest.fixture(scope="module")
def cfg():
    """Config rilis (hanya dibaca). Provider hanya memakai `data.akhiran_yfinance` & `data.simbol_ihsg`."""
    return muat_config(AKAR_PROYEK / "tests" / "fixtures" / "config_rilis.yaml").cfg


@pytest.fixture(scope="module")
def cfg_lain():
    """Config dengan simbol berbeda: membuktikan provider membaca config, bukan nilai tertanam (P4)."""
    data = baca_yaml(AKAR_PROYEK / "tests" / "fixtures" / "config_rilis.yaml")
    data["data"]["akhiran_yfinance"] = ".XX"
    data["data"]["simbol_ihsg"] = {"yfinance": "^TESTIDX", "csv_manual": "INDEKS"}
    return validasi_dict(data)


class TickerPalsu:
    """Pengganti `yfinance.Ticker`: mencatat argumen `history()` lalu mengembalikan df / melempar exc."""

    def __init__(self, hasil=None, exc: BaseException | None = None):
        self.hasil = hasil
        self.exc = exc
        self.simbol: list[str] = []
        self.argumen: list[dict] = []

    def pabrik(self, simbol: str) -> "TickerPalsu":
        self.simbol.append(simbol)
        return self

    def history(self, **argumen):
        self.argumen.append(argumen)
        if self.exc is not None:
            raise self.exc
        return self.hasil


def df_yf(index, *, ohlc=None, volume=None, splits=None, dividends=None, tanpa=()) -> pd.DataFrame:
    """DataFrame berbentuk keluaran `Ticker.history(auto_adjust=False, actions=True)`."""
    n = len(index)
    if ohlc is None:
        ohlc = [(100.0 + i, 105.0 + i, 95.0 + i, 102.0 + i) for i in range(n)]
    o, h, low, c = (list(x) for x in zip(*ohlc)) if n else ([], [], [], [])
    data = {
        "Open": o, "High": h, "Low": low, "Close": c,
        "Adj Close": [x * 0.9 if x == x else x for x in c],
        "Volume": volume if volume is not None else [1000 * (i + 1) for i in range(n)],
        "Dividends": dividends if dividends is not None else [0.0] * n,
        "Stock Splits": splits if splits is not None else [0.0] * n,
    }
    df = pd.DataFrame({k: v for k, v in data.items() if k not in tanpa}, index=index)
    df.index.name = "Date"
    return df


def idx_wib(*tanggal: str) -> pd.DatetimeIndex:
    """Index seperti yfinance untuk bursa Jakarta: tengah malam lokal, ber-zona Asia/Jakarta."""
    return pd.DatetimeIndex(list(tanggal)).tz_localize("Asia/Jakarta")


def ambil(cfg, df=None, kode="BBCA", mulai=date(2026, 10, 1), akhir=date(2026, 10, 14), exc=None):
    palsu = TickerPalsu(df, exc)
    hasil = YFinanceProvider(cfg, pabrik_ticker=palsu.pabrik).ambil_harian(kode, mulai, akhir)
    return hasil, palsu


def tanggal_bar(hasil) -> list[date]:
    return [ts.date() for ts in hasil.bar.index]


# ---------------------------------------------------------------------------
# YFinanceProvider: argumen permintaan
# ---------------------------------------------------------------------------


def test_argumen_history_end_akhir_plus_satu_hari_dan_repair_false(cfg):
    hasil, palsu = ambil(cfg, df_yf(idx_wib("2026-10-12")), mulai=date(2026, 10, 1), akhir=date(2026, 10, 14))
    assert hasil.status == "OK"
    assert palsu.simbol == ["BBCA.JK"]
    assert len(palsu.argumen) == 1
    a = palsu.argumen[0]
    assert a["start"] == date(2026, 10, 1)
    assert a["end"] == date(2026, 10, 15)  # end yfinance eksklusif → akhir + 1 hari
    assert a["end"] - timedelta(days=1) == date(2026, 10, 14)
    assert a["repair"] is False
    assert a["auto_adjust"] is False
    assert a["actions"] is True
    assert a["interval"] == "1d"
    # Opsi yang memengaruhi angka ditulis eksplisit (default yfinance pernah berubah).
    assert a["back_adjust"] is False and a["keepna"] is False and a["rounding"] is False
    assert a["prepost"] is False


def test_raise_errors_dikirim_bila_yfinance_terpasang_menerimanya(cfg):
    # yfinance 1.7.0 masih menerima raise_errors (meski ditandai usang).
    assert yfp._history_menerima_raise_errors() is True
    _, palsu = ambil(cfg, df_yf(idx_wib("2026-10-12")))
    assert palsu.argumen[0]["raise_errors"] is True


def test_raise_errors_tidak_dikirim_bila_tidak_didukung(cfg, monkeypatch):
    monkeypatch.setattr(yfp, "_history_menerima_raise_errors", lambda: False)
    _, palsu = ambil(cfg, df_yf(idx_wib("2026-10-12")))
    assert "raise_errors" not in palsu.argumen[0]


def test_semua_argumen_diterima_tanda_tangan_yfinance_terpasang():
    from yfinance.scrapers.history import PriceHistory

    parameter = inspect.signature(PriceHistory.history).parameters
    argumen = yfp.argumen_history(date(2026, 10, 1), date(2026, 10, 14))
    assert set(argumen) <= set(parameter), set(argumen) - set(parameter)


def test_mulai_lebih_dari_akhir_gagal_tanpa_permintaan(cfg):
    hasil, palsu = ambil(cfg, df_yf(idx_wib("2026-10-12")), mulai=date(2026, 10, 15), akhir=date(2026, 10, 14))
    assert hasil.status == "GAGAL" and hasil.jenis_error == "FORMAT"
    assert "rentang tanggal tidak valid" in hasil.pesan
    assert palsu.argumen == []


def test_simbol_dari_config(cfg, cfg_lain):
    p = YFinanceProvider(cfg, pabrik_ticker=TickerPalsu().pabrik)
    assert p.nama == "yfinance"
    assert p.simbol("BBCA") == "BBCA.JK"
    assert p.simbol("IHSG") == "^JKSE"
    q = YFinanceProvider(cfg_lain, pabrik_ticker=TickerPalsu().pabrik)
    assert q.simbol("BBCA") == "BBCA.XX"
    assert q.simbol("IHSG") == "^TESTIDX"


def test_ihsg_memakai_simbol_indeks(cfg):
    _, palsu = ambil(cfg, df_yf(idx_wib("2026-10-12")), kode="IHSG")
    assert palsu.simbol == ["^JKSE"]


def test_versi_provider_dari_yfinance_terpasang(cfg):
    import yfinance

    hasil, _ = ambil(cfg, df_yf(idx_wib("2026-10-12")))
    assert hasil.versi_provider == yfinance.__version__ == "1.7.0"
    gagal_, _ = ambil(cfg, exc=RuntimeError("x"))
    assert gagal_.versi_provider == "1.7.0"


# ---------------------------------------------------------------------------
# YFinanceProvider: tanggal & zona waktu
# ---------------------------------------------------------------------------


def test_index_tengah_malam_wib_menjadi_tanggal_yang_sama(cfg):
    idx = pd.DatetimeIndex([pd.Timestamp("2026-10-12 00:00", tz="Asia/Jakarta")])
    assert str(idx[0]) == "2026-10-12 00:00:00+07:00"
    hasil, _ = ambil(cfg, df_yf(idx))
    assert hasil.status == "OK"
    assert tanggal_bar(hasil) == [date(2026, 10, 12)]
    assert hasil.bar.index.tz is None
    assert hasil.bar.index.name == NAMA_INDEX
    assert (hasil.bar.index == hasil.bar.index.normalize()).all()


def test_index_utc_dikonversi_ke_wib_bukan_tanggal_utc(cfg):
    # 2026-10-11 17:00 UTC = 2026-10-12 00:00 WIB. Tanggal UTC (10-11) salah; tanggal WIB benar.
    idx = pd.DatetimeIndex([pd.Timestamp("2026-10-11 17:00", tz="UTC")])
    assert idx.normalize().tz_localize(None)[0].date() == date(2026, 10, 11)  # jalan "lewat UTC" (salah)
    hasil, _ = ambil(cfg, df_yf(idx))
    assert hasil.status == "OK", hasil.pesan
    assert tanggal_bar(hasil) == [date(2026, 10, 12)]


def test_index_dengan_jam_bursa_dinormalkan(cfg):
    idx = pd.DatetimeIndex(["2026-10-12 09:00", "2026-10-13 15:50"]).tz_localize("Asia/Jakarta")
    hasil, _ = ambil(cfg, df_yf(idx))
    assert tanggal_bar(hasil) == [date(2026, 10, 12), date(2026, 10, 13)]
    assert (hasil.bar.index == hasil.bar.index.normalize()).all()


def test_index_zona_lain_dikonversi_ke_wib(cfg):
    # 2026-10-12 00:00 New York (EDT, UTC−4) = 2026-10-12 11:00 WIB → 2026-10-12.
    idx = pd.DatetimeIndex(["2026-10-12 00:00"]).tz_localize("America/New_York")
    hasil, _ = ambil(cfg, df_yf(idx))
    assert tanggal_bar(hasil) == [date(2026, 10, 12)]


def test_index_tanpa_zona_waktu_gagal_format(cfg):
    idx = pd.DatetimeIndex(["2026-10-12", "2026-10-13"])
    hasil, _ = ambil(cfg, df_yf(idx))
    assert hasil.status == "GAGAL" and hasil.jenis_error == "FORMAT"
    assert "tanggal/zona waktu tidak dikenali" in hasil.pesan
    assert hasil.bar is None and hasil.split is None


def test_index_bukan_tanggal_gagal_format(cfg):
    df = df_yf(idx_wib("2026-10-12")).reset_index(drop=True)
    hasil, _ = ambil(cfg, df)
    assert hasil.jenis_error == "FORMAT"
    assert "tanggal/zona waktu tidak dikenali" in hasil.pesan


def test_bar_sabtu_gagal_format(cfg):
    hasil, _ = ambil(cfg, df_yf(idx_wib("2026-10-09", "2026-10-10")))  # 10-10 = Sabtu
    assert hasil.status == "GAGAL" and hasil.jenis_error == "FORMAT"
    assert "tanggal/zona waktu tidak dikenali" in hasil.pesan
    assert "2026-10-10" in hasil.pesan


def test_zona_salah_menggeser_ke_akhir_pekan_gagal(cfg):
    # Index UTC 2026-10-09 18:00 = Sabtu 2026-10-10 01:00 WIB → seluruh seri GAGAL (bukan dibuang diam-diam).
    idx = pd.DatetimeIndex(["2026-10-08 18:00", "2026-10-09 18:00"]).tz_localize("UTC")
    hasil, _ = ambil(cfg, df_yf(idx))
    assert hasil.status == "GAGAL" and hasil.jenis_error == "FORMAT"
    assert "akhir pekan" in hasil.pesan


# ---------------------------------------------------------------------------
# YFinanceProvider: kolom, split, tanpa mengarang data
# ---------------------------------------------------------------------------


def test_kolom_dinormalkan_dan_kolom_lain_dibuang(cfg):
    idx = idx_wib("2026-10-08", "2026-10-09", "2026-10-12")
    df = df_yf(idx, volume=[1000, 0, 3000])
    df["Capital Gains"] = 0.0
    df["Repaired?"] = False
    hasil, _ = ambil(cfg, df)
    assert hasil.status == "OK" and hasil.jenis_error is None and hasil.pesan == ""
    assert hasil.sumber == "yfinance" and hasil.kode == "BBCA"
    assert list(hasil.bar.columns) == list(KOLOM_BAR)
    assert all(hasil.bar[k].dtype == np.float64 for k in KOLOM_BAR)
    assert str(hasil.bar.index.dtype) == "datetime64[ns]"
    assert hasil.bar["close"].tolist() == [102.0, 103.0, 104.0]
    assert hasil.bar["volume"].tolist() == [1000.0, 0.0, 3000.0]  # volume 0 diteruskan apa adanya
    # Adj Close tidak pernah dipakai sebagai close.
    assert hasil.bar["close"].tolist() != df["Adj Close"].tolist()


def test_split_diambil_dari_stock_splits_hanya_yang_bukan_nol(cfg):
    idx = idx_wib("2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13")
    df = df_yf(idx, splits=[0.0, 5.0, 0.0, 1.0, 0.1], dividends=[0.0, 0.0, 25.0, 0.0, 0.0])
    hasil, _ = ambil(cfg, df)
    assert hasil.split.name == "split"
    assert hasil.split.index.name == NAMA_INDEX
    assert hasil.split.dtype == np.float64
    # 0 = tidak ada split; 1.0 = tidak ada perubahan lembar → dibuang. 5.0 & 0.1 (reverse split) disimpan.
    assert hasil.split.to_dict() == {pd.Timestamp("2026-10-08"): 5.0, pd.Timestamp("2026-10-13"): 0.1}
    assert "split" not in hasil.bar.columns and "dividends" not in hasil.bar.columns


def test_tanpa_kolom_stock_splits_split_kosong(cfg):
    hasil, _ = ambil(cfg, df_yf(idx_wib("2026-10-12"), tanpa=("Stock Splits",)))
    assert hasil.status == "OK"
    assert hasil.split.empty and hasil.split.name == "split"


def test_rasio_split_negatif_gagal_format(cfg):
    hasil, _ = ambil(cfg, df_yf(idx_wib("2026-10-12"), splits=[-2.0]))
    assert hasil.jenis_error == "FORMAT" and "rasio split" in hasil.pesan


@pytest.mark.parametrize("hilang", ["Open", "High", "Low", "Close", "Volume"])
def test_kolom_wajib_hilang_gagal_format(cfg, hilang):
    hasil, _ = ambil(cfg, df_yf(idx_wib("2026-10-12"), tanpa=(hilang,)))
    assert hasil.status == "GAGAL" and hasil.jenis_error == "FORMAT"
    assert "kolom wajib hilang" in hasil.pesan and hilang in hasil.pesan


def test_ihsg_tanpa_volume_lolos_dengan_volume_kosong(cfg):
    idx = idx_wib("2026-10-12", "2026-10-13")
    hasil, palsu = ambil(cfg, df_yf(idx, tanpa=("Volume",)), kode="IHSG")
    assert palsu.simbol == ["^JKSE"]
    assert hasil.status == "OK", hasil.pesan
    assert list(hasil.bar.columns) == list(KOLOM_BAR)
    assert hasil.bar["volume"].isna().all()
    for k in ("open", "high", "low", "close"):
        assert hasil.bar[k].notna().all()


def test_ihsg_tetap_wajib_ohlc(cfg):
    hasil, _ = ambil(cfg, df_yf(idx_wib("2026-10-12"), tanpa=("Close", "Volume")), kode="IHSG")
    assert hasil.jenis_error == "FORMAT" and "Close" in hasil.pesan


def test_tipe_kolom_bukan_angka_gagal_format(cfg):
    df = df_yf(idx_wib("2026-10-12", "2026-10-13"))
    df["Close"] = df["Close"].astype(object)
    df.iloc[1, df.columns.get_loc("Close")] = "abc"
    hasil, _ = ambil(cfg, df)
    assert hasil.jenis_error == "FORMAT" and "close" in hasil.pesan


def test_kolom_bertingkat_gagal_format(cfg):
    df = df_yf(idx_wib("2026-10-12"))
    df.columns = pd.MultiIndex.from_product([df.columns, ["BBCA.JK"]])
    hasil, _ = ambil(cfg, df)
    assert hasil.jenis_error == "FORMAT"


def test_df_kosong_tidak_ada_data(cfg):
    hasil, _ = ambil(cfg, df_yf(idx_wib()))
    assert hasil.status == "GAGAL" and hasil.jenis_error == "TIDAK_ADA_DATA"
    assert hasil.bar is None


def test_respons_bukan_dataframe_gagal_format(cfg):
    hasil, _ = ambil(cfg, None)
    assert hasil.jenis_error == "FORMAT"


def test_tidak_ada_baris_dibuat_atau_diisi(cfg):
    # 10-09 hilang (tidak ada baris), 10-12 OHLC NaN tetapi volume > 0 (bar rusak dari sumber).
    idx = idx_wib("2026-10-07", "2026-10-08", "2026-10-12", "2026-10-13")
    ohlc = [(10.0, 11, 9, 10.5), (10.5, 11.5, 10, 11), (np.nan,) * 4, (11.0, 12, 10.5, 11.5)]
    hasil, _ = ambil(cfg, df_yf(idx, ohlc=ohlc, volume=[100, 200, 300, 400]))
    assert hasil.status == "OK"
    assert len(hasil.bar) == 4  # tidak ada bar 10-09 yang dibuat
    assert tanggal_bar(hasil) == [date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 12), date(2026, 10, 13)]
    assert hasil.bar.loc["2026-10-12", ["open", "high", "low", "close"]].isna().all()  # tidak diisi
    assert hasil.bar.loc["2026-10-12", "volume"] == 300.0


def test_baris_peristiwa_tanpa_harga_dibuang_split_tetap_dicatat(cfg):
    # yfinance menambah baris OHLC NaN + Volume 0 untuk dividen/split di luar rentang harga, termasuk
    # pada hari non-bursa. Baris itu bukan bar: dibuang (tidak menggagalkan seri), split tetap tercatat.
    idx = idx_wib("2026-10-08", "2026-10-09", "2026-10-10", "2026-10-14")  # 10-10 Sabtu
    ohlc = [(10.0, 11, 9, 10.5), (10.5, 11.5, 10, 11), (np.nan,) * 4, (np.nan,) * 4]
    df = df_yf(idx, ohlc=ohlc, volume=[100, 200, 0, 0], splits=[0, 0, 0, 2.0], dividends=[0, 0, 15.0, 0])
    hasil, _ = ambil(cfg, df)
    assert hasil.status == "OK", hasil.pesan
    assert tanggal_bar(hasil) == [date(2026, 10, 8), date(2026, 10, 9)]
    assert hasil.split.to_dict() == {pd.Timestamp("2026-10-14"): 2.0}
    assert any("baris peristiwa tanpa harga" in c for c in hasil.catatan)


def test_baris_ohlc_nan_tanpa_peristiwa_tidak_dibuang(cfg):
    # OHLC NaN + volume 0 tanpa dividen/split: bukan baris sintetis yfinance → diteruskan apa adanya.
    idx = idx_wib("2026-10-08", "2026-10-09")
    hasil, _ = ambil(cfg, df_yf(idx, ohlc=[(10.0, 11, 9, 10.5), (np.nan,) * 4], volume=[100, 0]))
    assert len(hasil.bar) == 2
    assert hasil.catatan == []


def test_nilai_tak_hingga_dianggap_kosong(cfg):
    idx = idx_wib("2026-10-08", "2026-10-09")
    hasil, _ = ambil(cfg, df_yf(idx, ohlc=[(10.0, 11, 9, 10.5), (10.0, np.inf, 9, 10.5)]))
    assert hasil.status == "OK"
    assert np.isnan(hasil.bar.loc["2026-10-09", "high"])
    assert len(hasil.bar) == 2
    assert any("tak hingga" in c for c in hasil.catatan)


def test_bar_di_luar_rentang_dibuang(cfg):
    idx = idx_wib("2026-09-30", "2026-10-01", "2026-10-14", "2026-10-15")
    hasil, _ = ambil(cfg, df_yf(idx, splits=[3.0, 0, 0, 2.0]), mulai=date(2026, 10, 1), akhir=date(2026, 10, 14))
    assert tanggal_bar(hasil) == [date(2026, 10, 1), date(2026, 10, 14)]  # kedua ujung inklusif
    assert hasil.split.empty
    assert any("di luar rentang" in c for c in hasil.catatan)


def test_hanya_bar_di_luar_rentang_tidak_ada_data(cfg):
    hasil, _ = ambil(cfg, df_yf(idx_wib("2026-10-15")), akhir=date(2026, 10, 14))
    assert hasil.jenis_error == "TIDAK_ADA_DATA"


def test_tanggal_ganda_diteruskan_ke_validasi_struktur(cfg):
    # Provider tidak memutuskan duplikat; aturan §8.5 (terakhir identik/berbeda) milik validasi_struktur.
    idx = pd.DatetimeIndex(["2026-10-12 00:00", "2026-10-13 00:00", "2026-10-13 09:00"]).tz_localize("Asia/Jakarta")
    ohlc = [(10.0, 11, 9, 10.5), (10.5, 11.5, 10, 11), (10.5, 11.5, 10, 11)]
    hasil, _ = ambil(cfg, df_yf(idx, ohlc=ohlc, volume=[100, 200, 200]))
    assert len(hasil.bar) == 3
    struktur = validasi_struktur(hasil.bar)
    assert struktur.ok and len(struktur.bar) == 2


def test_hasil_lolos_validasi_struktur(cfg):
    idx = idx_wib("2026-10-08", "2026-10-09", "2026-10-12")
    hasil, _ = ambil(cfg, df_yf(idx))
    struktur = validasi_struktur(hasil.bar)
    assert struktur.ok, struktur.pesan
    pd.testing.assert_frame_equal(struktur.bar, hasil.bar)


# ---------------------------------------------------------------------------
# Klasifikasi error (§8.1)
# ---------------------------------------------------------------------------


class _Respons:
    def __init__(self, status: int):
        self.status_code = status


def _requests_http(status: int):
    import requests

    return requests.exceptions.HTTPError(f"{status} Error for url: https://query2.finance.yahoo.com/x",
                                         response=_Respons(status))


def _curl_http(status: int):
    from curl_cffi.requests import exceptions as cr

    return cr.HTTPError(f"HTTP Error {status}", response=_Respons(status))


def _kasus_error():
    import requests
    import urllib3
    from curl_cffi.curl import CurlError
    from curl_cffi.requests import exceptions as cr
    from yfinance import exceptions as yfe

    berantai = RuntimeError("pembungkus")
    berantai.__cause__ = requests.exceptions.ConnectionError("koneksi putus")
    return [
        # BATAS_PERMINTAAN
        ("yf_rate_limit", yfe.YFRateLimitError(), "BATAS_PERMINTAAN"),
        ("teks_429", Exception("HTTP Error 429: Too Many Requests"), "BATAS_PERMINTAAN"),
        ("teks_too_many", Exception("Too Many Requests"), "BATAS_PERMINTAAN"),
        ("teks_rate_limit", Exception("Rate limited. Try after a while."), "BATAS_PERMINTAAN"),
        ("requests_429", _requests_http(429), "BATAS_PERMINTAAN"),
        ("curl_429", _curl_http(429), "BATAS_PERMINTAAN"),
        # TIDAK_ADA_DATA
        ("tz_missing", yfe.YFTzMissingError("XXXX.JK"), "TIDAK_ADA_DATA"),
        ("prices_missing", yfe.YFPricesMissingError("XXXX.JK", "(1d 2026-10-01 -> 2026-10-15)"), "TIDAK_ADA_DATA"),
        ("prices_missing_alasan_yahoo",
         yfe.YFPricesMissingError("XXXX.JK", "", yahoo_reason="No data found, symbol may be delisted"),
         "TIDAK_ADA_DATA"),
        ("ticker_missing", yfe.YFTickerMissingError("XXXX.JK", "no data"), "TIDAK_ADA_DATA"),
        ("teks_delisted", Exception("$XXXX.JK: possibly delisted; no price data found"), "TIDAK_ADA_DATA"),
        ("teks_no_data", Exception("No data found, symbol may be delisted"), "TIDAK_ADA_DATA"),
        ("teks_no_tz", Exception("No timezone found"), "TIDAK_ADA_DATA"),
        ("requests_404", _requests_http(404), "TIDAK_ADA_DATA"),
        # JARINGAN
        ("requests_conn", requests.exceptions.ConnectionError("Max retries exceeded"), "JARINGAN"),
        ("requests_timeout", requests.exceptions.ReadTimeout("Read timed out."), "JARINGAN"),
        ("requests_chunked", requests.exceptions.ChunkedEncodingError("putus"), "JARINGAN"),
        ("urllib3_newconn", urllib3.exceptions.NewConnectionError(None, "gagal"), "JARINGAN"),
        ("curl_conn", cr.ConnectionError("Failed to perform, curl: (7) Couldn't connect to server"), "JARINGAN"),
        ("curl_timeout", cr.Timeout("Failed to perform, curl: (28) Operation timed out"), "JARINGAN"),
        ("curl_dns", cr.DNSError("Failed to perform, curl: (6) Could not resolve host"), "JARINGAN"),
        ("curl_proxy", cr.ProxyError("Failed to perform, curl: (56) CONNECT tunnel failed, response 403"), "JARINGAN"),
        ("curl_umum", cr.RequestException("Failed to perform"), "JARINGAN"),
        ("curl_mentah", CurlError("curl: (35) TLS connect error"), "JARINGAN"),
        ("curl_503", _curl_http(503), "JARINGAN"),
        ("requests_502", _requests_http(502), "JARINGAN"),
        ("builtin_reset", ConnectionResetError("Connection reset by peer"), "JARINGAN"),
        ("builtin_timeout", TimeoutError("timed out"), "JARINGAN"),
        ("gaierror", socket.gaierror(-3, "Temporary failure in name resolution"), "JARINGAN"),
        ("oserror_errno", OSError(errno.ENETUNREACH, "Network is unreachable"), "JARINGAN"),
        ("teks_connect", Exception("Failed to connect to query2.finance.yahoo.com port 443"), "JARINGAN"),
        ("teks_resolve", Exception("Could not resolve host: query2.finance.yahoo.com"), "JARINGAN"),
        ("teks_tunnel", Exception("CONNECT tunnel failed, response 403"), "JARINGAN"),
        ("teks_timed_out", Exception("Operation timed out after 10001 milliseconds"), "JARINGAN"),
        ("teks_reset", Exception("Connection reset"), "JARINGAN"),
        ("yahoo_down", yfe.YFDataException("*** YAHOO! FINANCE IS CURRENTLY DOWN! ***"), "JARINGAN"),
        ("berantai", berantai, "JARINGAN"),
        # Kelas jaringan menang atas angka "429" yang kebetulan ada di teks.
        ("conn_429ms", cr.ConnectionError("Failed to connect after 429 ms"), "JARINGAN"),
        # FORMAT
        ("key_error", KeyError("chart"), "FORMAT"),
        ("value_error", ValueError("Unable to parse input dt"), "FORMAT"),
        ("json", json.JSONDecodeError("Expecting value", "<html>", 0), "FORMAT"),
        # JSONDecodeError requests/curl_cffi turunan OSError tetapi bukan gangguan jaringan.
        ("requests_json", requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0), "FORMAT"),
        ("curl_401", _curl_http(401), "FORMAT"),
        ("file_not_found", FileNotFoundError(errno.ENOENT, "No such file"), "FORMAT"),
        ("yf_exception", yfe.YFException("Unexpected response"), "FORMAT"),
    ]


_KASUS = _kasus_error()


@pytest.mark.parametrize("nama,exc,jenis", _KASUS, ids=[k[0] for k in _KASUS])
def test_klasifikasi_error(nama, exc, jenis):
    assert klasifikasi_error(exc) == jenis


@pytest.mark.parametrize("nama,exc,jenis", _KASUS, ids=[k[0] for k in _KASUS])
def test_exception_saat_ambil_menjadi_gagal_terklasifikasi(cfg, nama, exc, jenis):
    hasil, palsu = ambil(cfg, exc=exc)
    assert hasil.status == "GAGAL"
    assert hasil.jenis_error == jenis
    assert hasil.bar is None and hasil.split is None
    assert hasil.pesan.startswith("BBCA.JK: ")
    assert type(exc).__name__ in hasil.pesan
    assert len(palsu.argumen) == 1  # satu permintaan; retry/jeda milik orkestrator


def test_exception_saat_membuat_ticker_tertangkap(cfg):
    def pabrik(simbol):
        raise ConnectionError("tidak ada koneksi")

    hasil = YFinanceProvider(cfg, pabrik_ticker=pabrik).ambil_harian("BBCA", date(2026, 10, 1), date(2026, 10, 14))
    assert hasil.status == "GAGAL" and hasil.jenis_error == "JARINGAN"


def test_pesan_error_disensor(cfg, monkeypatch):
    penyaring = PenyaringRahasia()
    penyaring.tambah("123456:TOKEN-rahasia")
    monkeypatch.setattr(yfp, "PENYARING", penyaring)
    exc = Exception("gagal https://api.example/bot123456:TOKEN-rahasia/x dan bot123456%3ATOKEN-rahasia")
    hasil, _ = ambil(cfg, exc=exc)
    assert "TOKEN-rahasia" not in hasil.pesan
    assert "***" in hasil.pesan


def test_pesan_error_satu_baris_dan_dipotong(cfg):
    hasil, _ = ambil(cfg, exc=Exception("baris1\nbaris2\t" + "x" * 2000))
    assert "\n" not in hasil.pesan and "baris1 baris2" in hasil.pesan
    assert len(hasil.pesan) <= yfp.PANJANG_MAKS_PESAN


# ---------------------------------------------------------------------------
# Integrasi dengan yfinance asli (lapisan HTTP palsu; tanpa jaringan)
# ---------------------------------------------------------------------------


def _ts(hari: str, jam_utc: int = 2) -> int:
    return int(pd.Timestamp(f"{hari} {jam_utc:02d}:00", tz="UTC").timestamp())


def _chart_json(hari: list[str], split_pada: str | None = None) -> dict:
    """Respons chart API Yahoo minimal untuk saham BEI (bar harian bertimestamp 09:00 WIB)."""
    n = len(hari)
    periode = {"timezone": "WIB", "start": _ts(hari[-1], 2), "end": _ts(hari[-1], 9), "gmtoffset": 25200}
    hasil = {
        "meta": {
            "currency": "IDR", "symbol": "BBCA.JK", "exchangeName": "JKT", "instrumentType": "EQUITY",
            "firstTradeDate": _ts("2004-06-08"), "regularMarketTime": _ts(hari[-1], 9), "gmtoffset": 25200,
            "timezone": "WIB", "exchangeTimezoneName": "Asia/Jakarta", "regularMarketPrice": 2000.0,
            "chartPreviousClose": 1900.0, "priceHint": 2,
            "currentTradingPeriod": {"pre": periode, "regular": periode, "post": periode},
            "dataGranularity": "1d", "range": "", "validRanges": ["1d", "5d", "1mo", "max"],
        },
        "timestamp": [_ts(h) for h in hari],
        "indicators": {
            "quote": [{
                "open": [100.0 + i for i in range(n)], "high": [105.0 + i for i in range(n)],
                "low": [95.0 + i for i in range(n)], "close": [102.0 + i for i in range(n)],
                "volume": [1000 * (i + 1) for i in range(n)],
            }],
            "adjclose": [{"adjclose": [90.0 + i for i in range(n)]}],
        },
    }
    if split_pada:
        t = _ts(split_pada)
        hasil["events"] = {"splits": {str(t): {"date": t, "numerator": 5, "denominator": 1, "splitRatio": "5:1"}}}
    return {"chart": {"result": [hasil], "error": None}}


class _ResponsHttp:
    def __init__(self, data: dict, status: int = 200):
        self.status_code = status
        self.url = "https://query2.finance.yahoo.com/v8/finance/chart/x"
        self.text = json.dumps(data)

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.exceptions.HTTPError(f"{self.status_code} Client Error", response=self)


@pytest.fixture
def yf_http_palsu(monkeypatch):
    """Ganti lapisan HTTP yfinance dengan responder palsu. Kembalikan daftar permintaan & setter responder."""
    import yfinance
    import yfinance.base
    import yfinance.cache
    from yfinance.data import YfData

    permintaan: list[tuple[str, dict]] = []
    responder = {"f": None}

    def get_palsu(self, url, params=None, timeout=30):
        permintaan.append((url, dict(params or {})))
        return responder["f"](url, params or {})

    monkeypatch.setattr(YfData, "get", get_palsu)
    monkeypatch.setattr(YfData, "cache_get", get_palsu)
    # Cache zona waktu yfinance (sqlite di folder pengguna) diganti dummy; fallback `info` dimatikan.
    monkeypatch.setattr(yfinance.cache, "get_tz_cache", lambda: yfinance.cache._TzCacheDummy())
    monkeypatch.setattr(yfinance.base, "_tz_info_fetch_ctr", 2)
    sembunyi_lama = yfinance.config.debug.hide_exceptions
    yield permintaan, responder
    yfinance.config.debug.hide_exceptions = sembunyi_lama


def test_yfinance_asli_respons_palsu(cfg, yf_http_palsu):
    permintaan, responder = yf_http_palsu
    hari = ["2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13"]
    responder["f"] = lambda url, params: _ResponsHttp(_chart_json(hari, split_pada="2026-10-12"))
    p = YFinanceProvider(cfg)  # pabrik default: yfinance.Ticker asli
    hasil = p.ambil_harian("BBCA", date(2026, 10, 1), date(2026, 10, 13))
    assert hasil.status == "OK", hasil.pesan
    assert tanggal_bar(hasil) == [date.fromisoformat(h) for h in hari]
    assert list(hasil.bar.columns) == list(KOLOM_BAR)
    assert hasil.bar["close"].tolist() == [102.0, 103.0, 104.0, 105.0]  # Close, bukan Adj Close
    assert hasil.bar["volume"].tolist() == [1000.0, 2000.0, 3000.0, 4000.0]
    assert hasil.split.to_dict() == {pd.Timestamp("2026-10-12"): 5.0}
    assert hasil.versi_provider == "1.7.0"
    # Permintaan chart: period2 = 2026-10-14 00:00 WIB (akhir + 1 hari, eksklusif).
    chart = [(u, q) for u, q in permintaan if "period1" in q]
    assert len(chart) == 1
    url, q = chart[0]
    assert url.endswith("/v8/finance/chart/BBCA.JK")
    assert q["period1"] == int(pd.Timestamp("2026-10-01 00:00", tz="Asia/Jakarta").timestamp())
    assert q["period2"] == int(pd.Timestamp("2026-10-14 00:00", tz="Asia/Jakarta").timestamp())
    assert q["interval"] == "1d"


def test_yfinance_asli_hide_exceptions_dimatikan(cfg, yf_http_palsu):
    import yfinance

    _, responder = yf_http_palsu
    responder["f"] = lambda url, params: _ResponsHttp(_chart_json(["2026-10-12"]))
    yfinance.config.debug.hide_exceptions = True
    YFinanceProvider(cfg).ambil_harian("BBCA", date(2026, 10, 1), date(2026, 10, 13))
    assert yfinance.config.debug.hide_exceptions is False


def test_yfinance_asli_jaringan_putus_saat_ambil_zona_waktu(cfg, yf_http_palsu):
    # Tanpa hide_exceptions=False, yfinance hanya me-log kegagalan ini lalu melempar YFTzMissingError
    # ("possibly delisted") → simbol sehat akan salah ditandai TIDAK_ADA_DATA selama 7 hari.
    import requests

    _, responder = yf_http_palsu

    def putus(url, params):
        raise requests.exceptions.ConnectionError("[Errno -3] Temporary failure in name resolution")

    responder["f"] = putus
    hasil = YFinanceProvider(cfg).ambil_harian("BBCA", date(2026, 10, 1), date(2026, 10, 13))
    assert hasil.status == "GAGAL"
    assert hasil.jenis_error == "JARINGAN", hasil.pesan


def test_yfinance_asli_simbol_tidak_dikenal(cfg, yf_http_palsu):
    _, responder = yf_http_palsu
    galat = {"chart": {"result": None, "error": {"code": "Not Found",
                                                 "description": "No data found, symbol may be delisted"}}}
    responder["f"] = lambda url, params: _ResponsHttp(galat, 404)
    hasil = YFinanceProvider(cfg).ambil_harian("XXXX", date(2026, 10, 1), date(2026, 10, 13))
    assert hasil.status == "GAGAL" and hasil.jenis_error == "TIDAK_ADA_DATA", hasil.pesan
    assert "XXXX.JK" in hasil.pesan


def test_yfinance_asli_tidak_ada_harga_dalam_rentang(cfg, yf_http_palsu):
    _, responder = yf_http_palsu
    galat = {"chart": {"result": None, "error": {"code": "Bad Request",
                                                 "description": "Data doesn't exist for startDate = 1, endDate = 2"}}}

    def jawab(url, params):
        if "range" in params:  # permintaan zona waktu berhasil
            return _ResponsHttp(_chart_json(["2026-10-12"]))
        return _ResponsHttp(galat, 400)

    responder["f"] = jawab
    hasil = YFinanceProvider(cfg).ambil_harian("BBCA", date(2026, 10, 1), date(2026, 10, 13))
    assert hasil.status == "GAGAL" and hasil.jenis_error == "TIDAK_ADA_DATA", hasil.pesan


def test_yfinance_asli_batas_permintaan(cfg, yf_http_palsu):
    from yfinance.exceptions import YFRateLimitError

    _, responder = yf_http_palsu

    def batas(url, params):
        raise YFRateLimitError()

    responder["f"] = batas
    hasil = YFinanceProvider(cfg).ambil_harian("BBCA", date(2026, 10, 1), date(2026, 10, 13))
    assert hasil.status == "GAGAL" and hasil.jenis_error == "BATAS_PERMINTAAN"


# ---------------------------------------------------------------------------
# CsvManualProvider (§6.9)
# ---------------------------------------------------------------------------

HEADER = "tanggal,open,high,low,close,volume\n"


def tulis(folder: Path, nama: str, isi: str | bytes) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / nama
    if isinstance(isi, bytes):
        path.write_bytes(isi)
    else:
        path.write_text(isi, encoding="utf-8", newline="")
    return path


@pytest.fixture
def folder_manual(tmp_path) -> Path:
    return tmp_path / "data" / "manual"


def csv_ambil(cfg, folder, kode="ABCD", mulai=date(2026, 1, 1), akhir=date(2026, 10, 15)):
    return CsvManualProvider(cfg, folder).ambil_harian(kode, mulai, akhir)


def test_csv_dasar_dengan_bom(cfg, folder_manual):
    isi = HEADER + "2026-10-12,2850,2890,2830,2880,45231000\n2026-10-13,2880,2900,2860,2870.5,1000\n"
    tulis(folder_manual, "ABCD.csv", b"\xef\xbb\xbf" + isi.encode("utf-8"))
    p = CsvManualProvider(cfg, folder_manual)
    assert p.nama == "csv_manual"
    assert p.tersedia("ABCD") and not p.tersedia("WXYZ")
    hasil = p.ambil_harian("ABCD", date(2026, 1, 1), date(2026, 10, 15))
    assert hasil.status == "OK", hasil.pesan
    assert hasil.sumber == "csv_manual" and hasil.versi_provider == "csv_manual"
    assert hasil.jenis_error is None and hasil.pesan == ""
    assert list(hasil.bar.columns) == list(KOLOM_BAR)
    assert all(hasil.bar[k].dtype == np.float64 for k in KOLOM_BAR)
    assert hasil.bar.index.name == NAMA_INDEX and str(hasil.bar.index.dtype) == "datetime64[ns]"
    assert tanggal_bar(hasil) == [date(2026, 10, 12), date(2026, 10, 13)]
    assert hasil.bar.loc["2026-10-12"].tolist() == [2850.0, 2890.0, 2830.0, 2880.0, 45231000.0]
    assert hasil.bar.loc["2026-10-13", "close"] == 2870.5
    # Tanpa berkas split: split kosong + catatan peringatan.
    assert hasil.split.empty and hasil.split.name == "split"
    assert hasil.catatan == [CATATAN_TANPA_SPLIT]
    assert CATATAN_TANPA_SPLIT == "tanpa berkas split: faktor split dianggap 1"
    assert validasi_struktur(hasil.bar).ok


def test_csv_berkas_tidak_ada_tidak_ada_data(cfg, folder_manual):
    hasil = csv_ambil(cfg, folder_manual, kode="WXYZ")
    assert hasil.status == "GAGAL" and hasil.jenis_error == "TIDAK_ADA_DATA"
    assert "WXYZ.csv" in hasil.pesan


def test_csv_pemisah_titik_koma_format_regional(cfg, folder_manual):
    tulis(folder_manual, "ABCD.csv", "tanggal;open;high;low;close;volume\n2026-10-12;2850;2890;2830;2880;100\n")
    hasil = csv_ambil(cfg, folder_manual)
    assert hasil.status == "GAGAL" and hasil.jenis_error == "FORMAT"
    assert PESAN_REGIONAL in hasil.pesan
    assert "ABCD.csv baris 1" in hasil.pesan


def test_csv_koma_desimal_format_regional(cfg, folder_manual):
    # Tanpa kutip: kolom bertambah → ERROR regional di baris itu.
    tulis(folder_manual, "ABCD.csv", HEADER + "2026-10-12,2850,2890,2830,2880,5,100\n")
    hasil = csv_ambil(cfg, folder_manual)
    assert hasil.jenis_error == "FORMAT" and PESAN_REGIONAL in hasil.pesan
    assert "baris 2" in hasil.pesan
    # Dengan kutip: nilai "2880,5" ditolak parse_desimal.
    tulis(folder_manual, "ABCD.csv", HEADER + '2026-10-12,2850,2890,2830,"2880,5",100\n')
    hasil = csv_ambil(cfg, folder_manual)
    assert hasil.jenis_error == "FORMAT" and PESAN_REGIONAL in hasil.pesan
    assert "kolom close" in hasil.pesan


def test_csv_tanggal_format_lain_ditolak(cfg, folder_manual):
    tulis(folder_manual, "ABCD.csv", HEADER + "2026-10-12,1,2,1,2,10\n08/10/2026,1,2,1,2,10\n")
    hasil = csv_ambil(cfg, folder_manual)
    assert hasil.jenis_error == "FORMAT"
    assert PESAN_REGIONAL in hasil.pesan
    assert "baris 3" in hasil.pesan and "08/10/2026" in hasil.pesan


def test_csv_pesan_regional_hanya_sekali(cfg, folder_manual):
    baris = "".join(f"0{i}/10/2026,1,2,1,2,10\n" for i in range(1, 8))
    tulis(folder_manual, "ABCD.csv", HEADER + baris)
    hasil = csv_ambil(cfg, folder_manual)
    assert hasil.pesan.count(PESAN_REGIONAL) == 1
    assert "(dan 2 masalah lain)" in hasil.pesan


def test_csv_kolom_tak_dikenal_format(cfg, folder_manual):
    tulis(folder_manual, "ABCD.csv", "tanggal,open,high,low,close,adj_close,volume\n2026-10-12,1,2,1,2,2,10\n")
    hasil = csv_ambil(cfg, folder_manual)
    assert hasil.jenis_error == "FORMAT" and "adj_close" in hasil.pesan


def test_csv_volume_saham_wajib(cfg, folder_manual):
    tulis(folder_manual, "ABCD.csv", "tanggal,open,high,low,close\n2026-10-12,1,2,1,2\n")
    assert csv_ambil(cfg, folder_manual).jenis_error == "FORMAT"
    tulis(folder_manual, "ABCD.csv", HEADER + "2026-10-12,1,2,1,2,\n")
    hasil = csv_ambil(cfg, folder_manual)
    assert hasil.jenis_error == "FORMAT" and "kolom volume" in hasil.pesan


def test_csv_tanggal_akhir_pekan_format(cfg, folder_manual):
    tulis(folder_manual, "ABCD.csv", HEADER + "2026-10-09,1,2,1,2,10\n2026-10-10,1,2,1,2,10\n")
    hasil = csv_ambil(cfg, folder_manual)
    assert hasil.status == "GAGAL" and hasil.jenis_error == "FORMAT"
    assert "akhir pekan" in hasil.pesan and "2026-10-10 (baris 3)" in hasil.pesan


def test_csv_filter_rentang_inklusif_dan_urut(cfg, folder_manual):
    baris = ["2026-10-14,4,5,3,4,40", "2026-10-09,1,2,1,2,10", "2026-10-12,2,3,1,2,20",
             "2026-10-13,3,4,2,3,30", "2026-10-15,5,6,4,5,50"]
    tulis(folder_manual, "ABCD.csv", HEADER + "\n".join(baris) + "\n")
    hasil = csv_ambil(cfg, folder_manual, mulai=date(2026, 10, 12), akhir=date(2026, 10, 14))
    assert tanggal_bar(hasil) == [date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)]
    assert hasil.bar["volume"].tolist() == [20.0, 30.0, 40.0]


def test_csv_tidak_ada_bar_dalam_rentang(cfg, folder_manual):
    tulis(folder_manual, "ABCD.csv", HEADER + "2026-10-09,1,2,1,2,10\n")
    hasil = csv_ambil(cfg, folder_manual, mulai=date(2026, 10, 12), akhir=date(2026, 10, 14))
    assert hasil.jenis_error == "TIDAK_ADA_DATA"
    tulis(folder_manual, "ABCD.csv", HEADER)  # hanya header
    assert csv_ambil(cfg, folder_manual).jenis_error == "TIDAK_ADA_DATA"


def test_csv_tidak_mengisi_dan_meneruskan_nilai_aneh(cfg, folder_manual):
    # 10-09 tidak ada (tidak dibuat); high < low & volume 0 diteruskan → dinilai validasi per bar (§8.5).
    tulis(folder_manual, "ABCD.csv", HEADER + "2026-10-08,10,11,9,10,100\n2026-10-12,10,9,11,10,0\n")
    hasil = csv_ambil(cfg, folder_manual)
    assert hasil.status == "OK"
    assert tanggal_bar(hasil) == [date(2026, 10, 8), date(2026, 10, 12)]
    assert hasil.bar.loc["2026-10-12", "high"] == 9.0 and hasil.bar.loc["2026-10-12", "low"] == 11.0
    assert hasil.bar.loc["2026-10-12", "volume"] == 0.0


def test_csv_tanggal_ganda_diteruskan_ke_validasi_struktur(cfg, folder_manual):
    tulis(folder_manual, "ABCD.csv", HEADER + "2026-10-08,1,2,1,2,10\n2026-10-12,1,2,1,2,10\n2026-10-12,1,2,1,2,10\n")
    hasil = csv_ambil(cfg, folder_manual)
    assert len(hasil.bar) == 3
    struktur = validasi_struktur(hasil.bar)
    assert struktur.ok and len(struktur.bar) == 2


def test_csv_berkas_split(cfg, folder_manual):
    tulis(folder_manual, "ABCD.csv", HEADER + "2023-06-12,500,510,490,505,1000\n2023-06-13,505,515,495,510,1000\n")
    tulis(folder_manual, "ABCD_split.csv", "tanggal,rasio\n2023-06-13,5\n2021-01-04,1\n2022-02-01,0.5\n")
    hasil = csv_ambil(cfg, folder_manual, mulai=date(2021, 1, 1), akhir=date(2026, 10, 15))
    assert hasil.status == "OK", hasil.pesan
    assert hasil.catatan == []
    # Rasio 1 berarti tidak ada split → tidak dicatat. Urut naik.
    assert hasil.split.to_dict() == {pd.Timestamp("2022-02-01"): 0.5, pd.Timestamp("2023-06-13"): 5.0}
    assert hasil.split.name == "split" and hasil.split.index.name == NAMA_INDEX
    assert hasil.split.dtype == np.float64


def test_csv_split_di_luar_rentang_tidak_ikut(cfg, folder_manual):
    tulis(folder_manual, "ABCD.csv", HEADER + "2026-10-12,1,2,1,2,10\n")
    tulis(folder_manual, "ABCD_split.csv", "tanggal,rasio\n2023-06-13,5\n2026-10-12,2\n")
    hasil = csv_ambil(cfg, folder_manual, mulai=date(2026, 1, 1), akhir=date(2026, 10, 15))
    assert hasil.split.to_dict() == {pd.Timestamp("2026-10-12"): 2.0}


@pytest.mark.parametrize("rasio", ["0", "-5", "5:1", "1,5"])
def test_csv_split_rasio_tidak_valid(cfg, folder_manual, rasio):
    tulis(folder_manual, "ABCD.csv", HEADER + "2026-10-12,1,2,1,2,10\n")
    tulis(folder_manual, "ABCD_split.csv", f'tanggal,rasio\n2026-10-12,"{rasio}"\n')
    hasil = csv_ambil(cfg, folder_manual)
    assert hasil.status == "GAGAL" and hasil.jenis_error == "FORMAT"
    assert "ABCD_split.csv baris 2" in hasil.pesan


def test_csv_split_tanggal_ganda(cfg, folder_manual):
    tulis(folder_manual, "ABCD.csv", HEADER + "2026-10-12,1,2,1,2,10\n")
    tulis(folder_manual, "ABCD_split.csv", "tanggal,rasio\n2026-10-12,5\n2026-10-12,5\n")
    hasil = csv_ambil(cfg, folder_manual)
    assert hasil.jenis_error == "FORMAT" and "ganda" in hasil.pesan


def test_csv_ihsg_tanpa_volume(cfg, folder_manual):
    tulis(folder_manual, "IHSG.csv", "tanggal,open,high,low,close\n2026-10-12,7000,7100,6950,7050\n")
    p = CsvManualProvider(cfg, folder_manual)
    assert p.simbol("IHSG") == "IHSG" and p.tersedia("IHSG")
    hasil = p.ambil_harian("IHSG", date(2026, 1, 1), date(2026, 10, 15))
    assert hasil.status == "OK", hasil.pesan
    assert list(hasil.bar.columns) == list(KOLOM_BAR)
    assert hasil.bar["volume"].isna().all()
    assert hasil.bar.loc["2026-10-12", "close"] == 7050.0
    assert validasi_struktur(hasil.bar, adalah_indeks=True).ok


def test_csv_ihsg_volume_kosong_sebagian(cfg, folder_manual):
    tulis(folder_manual, "IHSG.csv", HEADER + "2026-10-12,7000,7100,6950,7050,\n2026-10-13,7050,7150,7000,7100,123\n")
    hasil = csv_ambil(cfg, folder_manual, kode="IHSG")
    assert hasil.status == "OK", hasil.pesan
    assert np.isnan(hasil.bar.loc["2026-10-12", "volume"])  # kosong tetap kosong
    assert hasil.bar.loc["2026-10-13", "volume"] == 123.0


def test_csv_ihsg_simbol_dari_config(cfg_lain, folder_manual):
    tulis(folder_manual, "INDEKS.csv", "tanggal,open,high,low,close\n2026-10-12,7000,7100,6950,7050\n")
    p = CsvManualProvider(cfg_lain, folder_manual)
    assert p.simbol("IHSG") == "INDEKS" and p.simbol("ABCD") == "ABCD"
    assert p.path_berkas("IHSG").name == "INDEKS.csv"
    assert p.path_split("IHSG").name == "INDEKS_split.csv"
    assert p.ambil_harian("IHSG", date(2026, 1, 1), date(2026, 10, 15)).status == "OK"


def test_csv_mulai_lebih_dari_akhir(cfg, folder_manual):
    tulis(folder_manual, "ABCD.csv", HEADER + "2026-10-12,1,2,1,2,10\n")
    hasil = csv_ambil(cfg, folder_manual, mulai=date(2026, 10, 15), akhir=date(2026, 10, 14))
    assert hasil.jenis_error == "FORMAT" and "rentang tanggal tidak valid" in hasil.pesan


def test_csv_bukan_utf8_format(cfg, folder_manual):
    tulis(folder_manual, "ABCD.csv", (HEADER + "2026-10-12,1,2,1,2,10\n").encode("utf-16"))
    hasil = csv_ambil(cfg, folder_manual)
    assert hasil.status == "GAGAL" and hasil.jenis_error == "FORMAT"

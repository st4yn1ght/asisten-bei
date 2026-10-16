"""T-IN — indikator (SPEC §9, §22.1).

Setiap indikator dibandingkan dengan hitung tangan eksplisit (angka di komentar dihitung manual) atau
dengan loop Python murni yang ditulis ulang langsung dari rumus §9, independen dari implementasi.
Toleransi 1e-9. Semua masukan dibangun langsung di uji (tanpa jaringan, tanpa berkas pengguna).
"""

from __future__ import annotations

import copy
import math
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from asisten_bei import indikator as ind
from asisten_bei.config import Config, baca_yaml, validasi_dict

AKAR_PROYEK = Path(__file__).resolve().parents[1]
TOL = 1e-9
NAN = math.nan

# Config dasar = config.yaml rilis (hanya dibaca); setiap parameter yang dipakai uji di-set eksplisit
# agar uji tidak bergantung pada nilai yang mungkin diubah pengguna.
_DASAR = baca_yaml(AKAR_PROYEK / "config.yaml")
_TETAP = {
    "indikator.atr_periode": 14,
    "likuiditas.periode_hari": 20,
    "data.pengali_volume": 1,
    "strategi.pb.sma_cepat": 20,
    "strategi.pb.sma_tren": 50,
    "strategi.pb.sma_panjang": 200,
    "strategi.pb.lookback_swing_low": 5,
    "strategi.bo.lookback_tertinggi": 20,
    "strategi.bo.lookback_volume": 20,
    "strategi.bo.sma_tren": 50,
    "strategi.bo.sma_panjang": 200,
    "strategi.mr.rsi_periode": 2,
    "strategi.mr.sma_tren": 200,
    "strategi.mr.sma_keluar": 5,
    "kondisi_pasar.sma_pendek": 50,
    "kondisi_pasar.sma_panjang": 200,
    "kondisi_pasar.breadth_sma": 50,
    "skor.rs_lookback": 60,
}


def _cfg(ubah: dict | None = None) -> Config:
    data = copy.deepcopy(_DASAR)
    for kunci, nilai in {**_TETAP, **(ubah or {})}.items():
        *jalur, akhir = kunci.split(".")
        simpul = data
        for k in jalur:
            simpul = simpul[k]
        simpul[akhir] = nilai
    return validasi_dict(data)


def _idx(tanggal: list[str]) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(tanggal, name="tanggal")


def _bar(baris: list[tuple], tanggal: list[str] | None = None) -> pd.DataFrame:
    """baris = [(open, high, low, close, volume), ...] → DataFrame bar standar (hari kerja berurutan)."""
    if tanggal is None:
        idx = pd.bdate_range("2026-03-02", periods=len(baris), name="tanggal")
    else:
        idx = _idx(tanggal)
    return pd.DataFrame(baris, columns=["open", "high", "low", "close", "volume"], index=idx, dtype="float64")


def _sama(hasil, harapan, tol: float = TOL) -> None:
    hasil = np.asarray(hasil, dtype="float64")
    harapan = np.asarray(harapan, dtype="float64")
    assert hasil.shape == harapan.shape, (hasil, harapan)
    nan_h, nan_e = np.isnan(hasil), np.isnan(harapan)
    assert (nan_h == nan_e).all(), f"pola NaN beda:\n hasil  ={hasil}\n harapan={harapan}"
    assert np.allclose(hasil[~nan_h], harapan[~nan_e], rtol=0.0, atol=tol), f"\n hasil  ={hasil}\n harapan={harapan}"


# ---------------------------------------------------------------------------
# Rujukan independen: loop Python murni, ditulis langsung dari §9
# ---------------------------------------------------------------------------


def ref_sma(x: list[float], n: int) -> list[float]:
    return [sum(x[i - n + 1 : i + 1]) / n if i + 1 >= n else NAN for i in range(len(x))]


def ref_tr(h: list[float], l: list[float], c: list[float]) -> list[float]:
    out = []
    for i in range(len(h)):
        if i == 0:
            out.append(h[i] - l[i])
        else:
            out.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    return out


def ref_atr(h, l, c, n: int) -> list[float]:
    tr = ref_tr(h, l, c)
    out = [NAN] * len(tr)
    if len(tr) >= n:
        a = sum(tr[:n]) / n
        out[n - 1] = a
        for i in range(n, len(tr)):
            a = (a * (n - 1) + tr[i]) / n
            out[i] = a
    return out


def ref_rsi(c: list[float], n: int) -> list[float]:
    out = [NAN] * len(c)
    if len(c) <= n:
        return out
    naik = [max(c[i] - c[i - 1], 0.0) for i in range(1, len(c))]
    turun = [max(c[i - 1] - c[i], 0.0) for i in range(1, len(c))]

    def nilai(an, at):
        if at == 0:
            return 100.0 if an > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + an / at)

    an = sum(naik[:n]) / n
    at = sum(turun[:n]) / n
    out[n] = nilai(an, at)
    for i in range(n, len(naik)):
        an = (an * (n - 1) + naik[i]) / n
        at = (at * (n - 1) + turun[i]) / n
        out[i + 1] = nilai(an, at)
    return out


def _acak(n_bar: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    c = 1000.0 * np.exp(np.cumsum(rng.normal(0.0, 0.025, n_bar)))
    o = c * np.exp(rng.normal(0.0, 0.01, n_bar))
    h = np.maximum(o, c) * (1.0 + np.abs(rng.normal(0.0, 0.01, n_bar)))
    l = np.minimum(o, c) * (1.0 - np.abs(rng.normal(0.0, 0.01, n_bar)))
    v = rng.integers(10_000, 5_000_000, n_bar).astype("float64")
    idx = pd.bdate_range("2025-01-06", periods=n_bar, name="tanggal")
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": v}, index=idx)


# ---------------------------------------------------------------------------
# Data hitung tangan A: 6 bar valid + 1 bar tanpa transaksi (volume 0) di tengah
# ---------------------------------------------------------------------------
#        tanggal       O     H     L     C     V
MENTAH_A = _bar(
    [
        (10.0, 11.0, 9.0, 10.0, 1000),  # 2026-03-02
        (10.0, 12.0, 10.0, 11.0, 1000),  # 2026-03-03
        (11.0, 11.5, 10.5, 11.0, 1000),  # 2026-03-04
        (50.0, 50.0, 50.0, 50.0, 0),  # 2026-03-05  bar TANPA TRANSAKSI → tidak boleh dipakai
        (11.5, 14.0, 11.5, 13.5, 1000),  # 2026-03-06  gap: |H − C_{t−1}| = |14 − 11| = 3 > H − L = 2,5
        (13.0, 13.0, 12.0, 12.5, 1000),  # 2026-03-09  |L − C_{t−1}| = 1,5 > H − L = 1
        (12.0, 12.0, 9.0, 10.0, 1000),  # 2026-03-10  |L − C_{t−1}| = 3,5 > H − L = 3
    ]
)
VALID_A = MENTAH_A[MENTAH_A["volume"] > 0]

# TR (bar valid pertama = H − L; selanjutnya pakai close bar VALID sebelumnya, bukan bar volume 0):
#   2, max(2, 2, 0)=2, max(1, .5, .5)=1, max(2.5, 3, .5)=3, max(1, .5, 1.5)=1.5, max(3, .5, 3.5)=3.5
TR_A = [2.0, 2.0, 1.0, 3.0, 1.5, 3.5]
# ATR Wilder n=3: ATR_3 = (2+2+1)/3 = 5/3; ATR_4 = (5/3·2 + 3)/3 = 19/9;
#   ATR_5 = (19/9·2 + 1.5)/3 = 51.5/27; ATR_6 = (51.5/27·2 + 3.5)/3 = 197.5/81
ATR3_A = [NAN, NAN, 5 / 3, 19 / 9, 51.5 / 27, 197.5 / 81]
# SMA(close, 3) atas bar valid: close = 10, 11, 11, 13.5, 12.5, 10
SMA3_A = [NAN, NAN, 32 / 3, 35.5 / 3, 37 / 3, 36 / 3]


class TestSMA:
    def test_hitung_tangan(self):
        x = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0])
        _sama(ind.sma(x, 3), [NAN, NAN, 11.0, 12.0, 13.0])

    def test_bar_valid_saja_dan_jeda_tanggal(self):
        # Bar volume 0 (2026-03-05, close 50) tidak masuk: SMA3 pada 2026-03-06 = (11 + 11 + 13,5)/3.
        hasil = ind.sma(VALID_A["close"], 3)
        _sama(hasil, SMA3_A)
        assert hasil.loc["2026-03-06"] == pytest.approx(35.5 / 3, abs=TOL)

    def test_nan_bila_bar_kurang(self):
        _sama(ind.sma(pd.Series([1.0, 2.0]), 3), [NAN, NAN])
        _sama(ind.sma(pd.Series([], dtype="float64"), 3), [])

    def test_rujukan_acak(self):
        df = _acak(300, 7)
        for n in (1, 5, 20, 50, 200):
            _sama(ind.sma(df["close"], n), ref_sma(df["close"].tolist(), n))

    @pytest.mark.parametrize("n", [0, -1, 2.5, True, None])
    def test_periode_tidak_valid(self, n):
        with pytest.raises(ValueError):
            ind.sma(pd.Series([1.0, 2.0]), n)


class TestHHVLLV:
    # x = 5, 3, 8, 6, 7, 9, 2 ; n = 3
    X = pd.Series([5.0, 3.0, 8.0, 6.0, 7.0, 9.0, 2.0])

    def test_hhv_sebelum_eksklusif_t(self):
        # HHV(x,3)_{t−1}: idx3 = max(5,3,8)=8; idx4 = max(3,8,6)=8; idx5 = max(8,6,7)=8 (x_5 = 9 TIDAK
        # termasuk); idx6 = max(6,7,9)=9. Butuh n bar SEBELUM t → idx0..2 NaN.
        _sama(ind.hhv_sebelum(self.X, 3), [NAN, NAN, NAN, 8.0, 8.0, 8.0, 9.0])

    def test_llv_inklusif_t(self):
        # LLV(x,3)_t: idx2 = min(5,3,8)=3; idx3 = min(3,8,6)=3; idx4 = min(8,6,7)=6; idx5 = min(6,7,9)=6;
        # idx6 = min(7,9,2)=2 (x_6 = 2 TERMASUK).
        _sama(ind.llv(self.X, 3), [NAN, NAN, 3.0, 3.0, 6.0, 6.0, 2.0])

    def test_hhv_tepat_n_bar_masih_nan(self):
        _sama(ind.hhv_sebelum(pd.Series([1.0, 2.0, 3.0]), 3), [NAN, NAN, NAN])
        _sama(ind.llv(pd.Series([3.0, 2.0, 1.0]), 3), [NAN, NAN, 1.0])

    def test_breakout_tidak_membandingkan_dengan_dirinya(self):
        # close_t > HHV(high, n)_{t−1} harus bisa benar pada hari breakout.
        high = pd.Series([10.0, 10.5, 10.2, 12.0])
        assert 12.0 > ind.hhv_sebelum(high, 3).iloc[-1]


class TestHHVSejak:
    # high: 2026-03-02..06 & 09 = 10, 15, 12, 11, 14, 13
    HIGH = pd.Series(
        [10.0, 15.0, 12.0, 11.0, 14.0, 13.0],
        index=_idx(["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06", "2026-03-09"]),
    )

    def test_mulai_inklusif(self):
        assert ind.hhv_sejak(self.HIGH, pd.Timestamp("2026-03-03").date(), pd.Timestamp("2026-03-05").date()) == 15.0

    def test_sampai_inklusif(self):
        assert ind.hhv_sejak(self.HIGH, pd.Timestamp("2026-03-04").date(), pd.Timestamp("2026-03-06").date()) == 14.0

    def test_rentang_dalam(self):
        assert ind.hhv_sejak(self.HIGH, pd.Timestamp("2026-03-04").date(), pd.Timestamp("2026-03-05").date()) == 12.0
        assert ind.hhv_sejak(self.HIGH, pd.Timestamp("2026-03-09").date(), pd.Timestamp("2026-03-09").date()) == 13.0

    def test_tanggal_tanpa_bar(self):
        sabtu, minggu = pd.Timestamp("2026-03-07").date(), pd.Timestamp("2026-03-08").date()
        assert math.isnan(ind.hhv_sejak(self.HIGH, sabtu, minggu))
        assert ind.hhv_sejak(self.HIGH, sabtu, pd.Timestamp("2026-03-09").date()) == 13.0
        # mulai > sampai → kosong
        assert math.isnan(ind.hhv_sejak(self.HIGH, pd.Timestamp("2026-03-06").date(), pd.Timestamp("2026-03-04").date()))

    def test_hanya_bar_valid(self):
        # Bar volume 0 (high 50 pada 2026-03-05) di MENTAH_A tidak terlihat bila dihitung dari bar valid.
        d1, d2 = pd.Timestamp("2026-03-04").date(), pd.Timestamp("2026-03-06").date()
        assert ind.hhv_sejak(VALID_A["high"], d1, d2) == 14.0
        assert ind.hhv_sejak(MENTAH_A["high"], d1, d2) == 50.0  # pembanding: data mentah memang beda


class TestTRdanATR:
    def test_tr_hitung_tangan(self):
        _sama(ind.true_range(VALID_A), TR_A)

    def test_tr_bar_pertama_high_minus_low(self):
        df = _bar([(100.0, 110.0, 95.0, 105.0, 1)])
        _sama(ind.true_range(df), [15.0])

    def test_atr_hitung_tangan_nilai_pertama_dan_rekursi(self):
        atr = ind.atr_wilder(VALID_A, 3)
        _sama(atr, ATR3_A)
        # Nilai pertama = rata-rata 3 TR pertama, tepat pada bar valid ke-3.
        assert math.isnan(atr.iloc[1]) and atr.iloc[2] == pytest.approx((2 + 2 + 1) / 3, abs=TOL)
        # Rekursi: ATR_t = (ATR_{t−1}·(n−1) + TR_t)/n
        for i in range(3, len(atr)):
            assert atr.iloc[i] == pytest.approx((atr.iloc[i - 1] * 2 + TR_A[i]) / 3, abs=TOL)

    def test_bar_tanpa_transaksi_mengubah_hasil_bila_ikut(self):
        # Pembanding: bila bar volume 0 (close 50) ikut, TR 2026-03-06 menjadi |11,5 − 50| = 38,5.
        assert ind.true_range(MENTAH_A).loc["2026-03-06"] == pytest.approx(38.5)
        assert ind.true_range(VALID_A).loc["2026-03-06"] == pytest.approx(3.0)

    def test_atr_n_dari_config(self):
        df = _acak(60, 11)
        h, l, c = (df[k].tolist() for k in ("high", "low", "close"))
        for n in (14, 5):
            cfg = _cfg({"indikator.atr_periode": n})
            hasil = ind.hitung_indikator(df, cfg)["atr"]
            harapan = ref_atr(h, l, c, n)
            _sama(hasil, harapan)
            tr = ref_tr(h, l, c)
            assert math.isnan(hasil.iloc[n - 2])
            assert hasil.iloc[n - 1] == pytest.approx(sum(tr[:n]) / n, abs=TOL)

    def test_atr_nol_hitung_tangan(self):
        # "ATR ≤ 0": bar datar (H = L = C) → TR = 0 → ATR = 0 tepat (aturan yang membagi ATR → tidak terpenuhi).
        df = _bar([(100.0, 100.0, 100.0, 100.0, 5)] * 5)
        _sama(ind.true_range(df), [0.0] * 5)
        atr = ind.atr_wilder(df, 3)
        _sama(atr, [NAN, NAN, 0.0, 0.0, 0.0])
        assert (atr.iloc[2:] <= 0).all()

    def test_atr_nan_bila_bar_kurang(self):
        _sama(ind.atr_wilder(VALID_A.iloc[:2], 3), [NAN, NAN])
        _sama(ind.atr_wilder(VALID_A.iloc[:0], 3), [])


class TestRSI:
    # close = 10, 11, 10.5, 12, 11, 11 → selisih +1, −0.5, +1.5, −1, 0
    CLOSE = pd.Series([10.0, 11.0, 10.5, 12.0, 11.0, 11.0])

    def test_hitung_tangan_n2(self):
        # n=2. RSI pertama pada bar ke-3 (idx2): avg_naik=(1+0)/2=0.5, avg_turun=(0+0.5)/2=0.25 → RS=2 → 200/3
        # idx3: naik=(0.5·1+1.5)/2=1.0, turun=(0.25·1+0)/2=0.125 → RS=8 → 100−100/9 = 800/9
        # idx4: naik=(1.0+0)/2=0.5, turun=(0.125+1)/2=0.5625 → RS=8/9 → 800/17
        # idx5: naik=(0.5+0)/2=0.25, turun=(0.5625+0)/2=0.28125 → RS=8/9 → 800/17
        _sama(ind.rsi_wilder(self.CLOSE, 2), [NAN, NAN, 200 / 3, 800 / 9, 800 / 17, 800 / 17])

    def test_hitung_tangan_n3(self):
        # n=3. idx3: naik=(1+0+1.5)/3=5/6, turun=(0+0.5+0)/3=1/6 → RS=5 → 100−100/6 = 250/3
        # idx4: naik=(5/6·2+0)/3=5/9, turun=(1/6·2+1)/3=4/9 → RS=1.25 → 100−100/2.25 = 500/9
        # idx5: naik=(5/9·2+0)/3=10/27, turun=(4/9·2+0)/3=8/27 → RS=1.25 → 500/9
        _sama(ind.rsi_wilder(self.CLOSE, 3), [NAN, NAN, NAN, 250 / 3, 500 / 9, 500 / 9])

    def test_rsi_pertama_pada_bar_ke_n_plus_1(self):
        r = ind.rsi_wilder(self.CLOSE, 2)
        assert math.isnan(r.iloc[1]) and not math.isnan(r.iloc[2])
        _sama(ind.rsi_wilder(self.CLOSE.iloc[:2], 2), [NAN, NAN])  # tepat n bar → semua NaN

    def test_avg_turun_nol_avg_naik_positif_100(self):
        _sama(ind.rsi_wilder(pd.Series([1.0, 2.0, 3.0, 4.0]), 2), [NAN, NAN, 100.0, 100.0])
        # naik lalu datar: avg_naik meluruh tetapi tetap > 0, avg_turun tetap 0 → 100
        _sama(ind.rsi_wilder(pd.Series([1.0, 2.0, 2.0, 2.0, 2.0]), 2), [NAN, NAN, 100.0, 100.0, 100.0])

    def test_keduanya_nol_50(self):
        _sama(ind.rsi_wilder(pd.Series([5.0, 5.0, 5.0, 5.0]), 2), [NAN, NAN, 50.0, 50.0])
        # datar lalu naik: idx3 avg_naik=(0·1+1)/2=0.5, avg_turun=0 → 100
        _sama(ind.rsi_wilder(pd.Series([5.0, 5.0, 5.0, 6.0]), 2), [NAN, NAN, 50.0, 100.0])

    def test_avg_naik_nol_avg_turun_positif_0(self):
        _sama(ind.rsi_wilder(pd.Series([4.0, 3.0, 2.0, 1.0]), 2), [NAN, NAN, 0.0, 0.0])

    def test_rujukan_acak(self):
        c = _acak(400, 3)["close"]
        for n in (2, 3, 14):
            _sama(ind.rsi_wilder(c, n), ref_rsi(c.tolist(), n))

    def test_bar_tanpa_transaksi_tidak_masuk(self):
        # Close bar volume 0 (50) akan membuat lonjakan naik palsu bila ikut.
        cfg = _cfg({"strategi.mr.rsi_periode": 2})
        hasil = ind.hitung_indikator(VALID_A, cfg)["rsi_2"]
        _sama(hasil, ref_rsi(VALID_A["close"].tolist(), 2))
        # 2026-03-06: selisih bar valid = 11→13.5 (+2.5), bukan 50→13.5.
        assert ref_rsi([10.0, 11.0, 11.0, 13.5], 2)[-1] == pytest.approx(hasil.loc["2026-03-06"], abs=TOL)


class TestCLV:
    def test_hitung_tangan(self):
        df = _bar(
            [
                (9.0, 10.0, 8.0, 9.5, 1),  # (9.5−8)/(10−8) = 0.75
                (10.0, 10.0, 10.0, 10.0, 1),  # high = low → 0.5
                (10.0, 12.0, 10.0, 12.0, 1),  # close = high → 1
                (12.0, 12.0, 10.0, 10.0, 1),  # close = low → 0
            ]
        )
        _sama(ind.clv(df), [0.75, 0.5, 1.0, 0.0])

    def test_data_a(self):
        # (C−L)/(H−L): 1/2, 1/2, .5/1, 2/2.5, .5/1, 1/3
        _sama(ind.clv(VALID_A), [0.5, 0.5, 0.5, 0.8, 0.5, 1 / 3])


class TestRasioVolRetNilai:
    def test_rasio_vol_hitung_tangan(self):
        # n=3; volume 100, 200, 300, 600, 150
        # idx3 = 600 / mean(100,200,300)=200 → 3.0 ; idx4 = 150 / mean(200,300,600)=1100/3 → 450/1100
        v = pd.Series([100.0, 200.0, 300.0, 600.0, 150.0])
        _sama(ind.rasio_vol(v, 3), [NAN, NAN, NAN, 3.0, 450 / 1100])

    def test_rasio_vol_rata_rata_sebelum_t(self):
        # Volume t tidak ikut penyebut: lonjakan 10× pada t memberi rasio tepat 10.
        v = pd.Series([100.0] * 5 + [1000.0])
        assert ind.rasio_vol(v, 5).iloc[-1] == pytest.approx(10.0, abs=TOL)

    def test_rasio_vol_bar_tanpa_transaksi_tidak_masuk(self):
        # Valid: volume 1000 semua. Bila bar volume 0 ikut, penyebut menjadi lebih kecil.
        r = ind.rasio_vol(VALID_A["volume"], 3)
        _sama(r, [NAN, NAN, NAN, 1.0, 1.0, 1.0])

    def test_ret_hitung_tangan(self):
        # n=2: idx2 = 12.1/10 − 1 = 0.21 ; idx3 = 9.68/11 − 1 = −0.12
        c = pd.Series([10.0, 11.0, 12.1, 9.68])
        _sama(ind.ret(c, 2), [NAN, NAN, 0.21, -0.12])

    def test_ret_atas_bar_valid(self):
        # Ret(2) pada 2026-03-06 = 13.5 / close bar valid ke-2 sebelumnya (11 pada 03-03) − 1.
        assert ind.ret(VALID_A["close"], 2).loc["2026-03-06"] == pytest.approx(13.5 / 11 - 1, abs=TOL)

    def test_nilai_tx_dan_rata_nilai(self):
        # close × volume × pengali: 1500·2e6 = 3e9 ; 1600·1e6 = 1.6e9 ; 1700·3e6 = 5.1e9 → rata 3 = 9.7e9/3
        df = _bar([(1500.0, 1500.0, 1500.0, 1500.0, 2e6), (1600.0, 1600.0, 1600.0, 1600.0, 1e6),
                   (1700.0, 1700.0, 1700.0, 1700.0, 3e6)])
        _sama(ind.nilai_tx(df, 1), [3e9, 1.6e9, 5.1e9], tol=1e-3)
        cfg = _cfg({"likuiditas.periode_hari": 3, "data.pengali_volume": 100})
        h = ind.hitung_indikator(df, cfg)
        _sama(h["nilai_tx"], [3e11, 1.6e11, 5.1e11], tol=1e-3)
        _sama(h["rata_nilai"], [NAN, NAN, 9.7e11 / 3], tol=1e-3)
        _sama(ind.nilai_tx(df, Decimal("100")), [3e11, 1.6e11, 5.1e11], tol=1e-3)


class TestHitungIndikator:
    def test_kolom_mengikuti_config(self):
        cfg = _cfg()
        h = ind.hitung_indikator(_acak(30, 1), cfg)
        harapan = {
            "open", "high", "low", "close", "volume", "tr", "atr", "clv", "nilai_tx", "rata_nilai",
            "sma_5", "sma_20", "sma_50", "sma_200", "rsi_2", "hhv_high_20", "llv_low_5", "rasio_vol_20", "ret_60",
        }
        assert set(h.columns) == harapan
        assert h.index.name == "tanggal"
        assert all(h[k].dtype == "float64" for k in h.columns)

    def test_p4_kunci_config_mengubah_kolom(self):
        cfg = _cfg(
            {
                "strategi.pb.sma_cepat": 7, "strategi.mr.rsi_periode": 3, "strategi.bo.lookback_tertinggi": 9,
                "strategi.pb.lookback_swing_low": 4, "strategi.bo.lookback_volume": 11, "skor.rs_lookback": 13,
                "kondisi_pasar.breadth_sma": 30, "strategi.mr.sma_keluar": 6,
            }
        )
        h = ind.hitung_indikator(_acak(80, 2), cfg)
        for k in ("sma_7", "sma_30", "sma_6", "rsi_3", "hhv_high_9", "llv_low_4", "rasio_vol_11", "ret_13"):
            assert k in h.columns, k
        for k in ("sma_20", "sma_5", "rsi_2", "hhv_high_20", "llv_low_5", "rasio_vol_20", "ret_60"):
            assert k not in h.columns, k

    def test_nilai_sama_dengan_rujukan(self):
        df = _acak(260, 5)
        cfg = _cfg()
        h = ind.hitung_indikator(df, cfg)
        c, hi, lo, v = (df[k].tolist() for k in ("close", "high", "low", "volume"))
        for n in (5, 20, 50, 200):
            _sama(h[f"sma_{n}"], ref_sma(c, n))
        _sama(h["atr"], ref_atr(hi, lo, c, 14))
        _sama(h["tr"], ref_tr(hi, lo, c))
        _sama(h["rsi_2"], ref_rsi(c, 2))
        hhv = [max(hi[i - 20 : i]) if i >= 20 else NAN for i in range(len(hi))]
        _sama(h["hhv_high_20"], hhv)
        llv = [min(lo[i - 4 : i + 1]) if i >= 4 else NAN for i in range(len(lo))]
        _sama(h["llv_low_5"], llv)
        rv = [v[i] / (sum(v[i - 20 : i]) / 20) if i >= 20 else NAN for i in range(len(v))]
        _sama(h["rasio_vol_20"], rv)
        rt = [c[i] / c[i - 60] - 1 if i >= 60 else NAN for i in range(len(c))]
        _sama(h["ret_60"], rt)
        nt = [c[i] * v[i] for i in range(len(c))]
        _sama(h["rata_nilai"], ref_sma(nt, 20), tol=1e-3)  # nilai ~1e9: toleransi absolut 1e-3 Rupiah

    def test_menolak_bar_tanpa_transaksi(self):
        with pytest.raises(ValueError, match="bar valid"):
            ind.hitung_indikator(MENTAH_A, _cfg())

    def test_menolak_harga_nan_dan_urutan_salah(self):
        rusak = VALID_A.copy()
        rusak.iloc[2, rusak.columns.get_loc("close")] = np.nan
        with pytest.raises(ValueError):
            ind.hitung_indikator(rusak, _cfg())
        with pytest.raises(ValueError):
            ind.hitung_indikator(VALID_A.iloc[::-1], _cfg())

    def test_data_a_lewat_hitung_indikator(self):
        cfg = _cfg({"indikator.atr_periode": 3, "strategi.mr.sma_keluar": 3})
        h = ind.hitung_indikator(VALID_A, cfg)
        assert "2026-03-05" not in h.index.strftime("%Y-%m-%d")
        _sama(h["tr"], TR_A)
        _sama(h["atr"], ATR3_A)
        _sama(h["sma_3"], SMA3_A)

    def test_kosong(self):
        h = ind.hitung_indikator(VALID_A.iloc[:0], _cfg())
        assert len(h) == 0 and "atr" in h.columns

    def test_tidak_mengubah_masukan(self):
        df = _acak(50, 9)
        salinan = df.copy()
        ind.hitung_indikator(df, _cfg())
        pd.testing.assert_frame_equal(df, salinan)

    def test_kolom_tambahan_diabaikan(self):
        df = _acak(30, 4).assign(sumber="yfinance")
        h = ind.hitung_indikator(df, _cfg())
        assert "sumber" not in h.columns


class TestIndikatorIndeks:
    def test_ihsg_tanpa_volume(self):
        df = _acak(260, 8).assign(volume=np.nan)
        cfg = _cfg()
        h = ind.hitung_indikator_indeks(df, cfg)
        assert list(h.columns) == ["close", "sma_50", "sma_200"]
        c = df["close"].tolist()
        _sama(h["sma_50"], ref_sma(c, 50))
        _sama(h["sma_200"], ref_sma(c, 200))

    def test_periode_dari_config(self):
        cfg = _cfg({"kondisi_pasar.sma_pendek": 10, "kondisi_pasar.sma_panjang": 40})
        h = ind.hitung_indikator_indeks(_acak(50, 8), cfg)
        assert list(h.columns) == ["close", "sma_10", "sma_40"]


def test_nama_kolom_standar():
    assert ind.k_sma(20) == "sma_20"
    assert ind.k_rsi(2) == "rsi_2"
    assert ind.k_hhv_high(20) == "hhv_high_20"
    assert ind.k_llv_low(5) == "llv_low_5"
    assert ind.k_rasio_vol(20) == "rasio_vol_20"
    assert ind.k_ret(60) == "ret_60"
    assert ind.periode_sma(_cfg()) == [5, 20, 50, 200]

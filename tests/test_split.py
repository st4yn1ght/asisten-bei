"""Uji T-SP bagian data (SPEC §8.4, §22.1) untuk `data/split.py`.

Saham fiktif dengan split 1:5 (rasio 5.0) dan 1:2, serta reverse split 5:1 (rasio 0.2). Konvensi:
harga tersesuaikan = harga nominal ÷ F(d), F(d) = Π rasio split bertanggal > d.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from asisten_bei.data.provider import NAMA_INDEX
from asisten_bei.data.split import (
    PemeriksaanSplit,
    faktor_split,
    harga_adj,
    harga_nominal,
    lembar_adj,
    lembar_nominal,
    normalisasi_split,
    periksa_split,
    seri_faktor,
    split_belum_disesuaikan,
    split_dari_kolom,
    volume_nominal,
)

S1 = date(2023, 6, 15)  # split 1:5 (Kamis)
S2 = date(2024, 3, 1)  # split 1:2 (Jumat)


def seri_split(*pasangan: tuple[date | str, float]) -> pd.Series:
    return pd.Series([r for _, r in pasangan], index=pd.to_datetime([str(d) for d, _ in pasangan]))


def bar_dari_close(tanggal: list[str], close: list[float]) -> pd.DataFrame:
    idx = pd.DatetimeIndex(pd.to_datetime(tanggal), name=NAMA_INDEX)
    c = np.asarray(close, dtype="float64")
    return pd.DataFrame(
        {"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": np.full(len(c), 1e6)}, index=idx
    )


# ---------------------------------------------------------------------------
# normalisasi_split
# ---------------------------------------------------------------------------


def test_normalisasi_none_dan_kosong():
    for s in (None, pd.Series(dtype="float64")):
        hasil = normalisasi_split(s)
        assert len(hasil) == 0
        assert hasil.name == "split"
        assert hasil.index.name == NAMA_INDEX
        assert isinstance(hasil.index, pd.DatetimeIndex)
        assert hasil.dtype == np.float64


def test_normalisasi_membuang_nol_nan_satu_negatif_inf_dan_mengurutkan():
    mentah = seri_split(
        ("2024-03-01", 2.0),
        ("2023-06-15", 5.0),
        ("2023-07-03", 0.0),  # yfinance: 0 = tidak ada split
        ("2023-07-04", np.nan),
        ("2023-07-05", 1.0),
        ("2023-07-06", -2.0),
        ("2023-07-07", np.inf),
    )
    hasil = normalisasi_split(mentah)
    assert list(hasil.index.date) == [S1, S2]
    assert hasil.tolist() == [5.0, 2.0]
    assert hasil.index.name == NAMA_INDEX and hasil.name == "split"


def test_normalisasi_index_berzona_dikonversi_ke_wib_bukan_utc():
    # 2023-06-14 17:00 UTC = 2023-06-15 00:00 WIB → tanggal 2023-06-15 (bukan 14).
    idx = pd.DatetimeIndex(["2023-06-14 17:00:00"], tz="UTC")
    hasil = normalisasi_split(pd.Series([5.0], index=idx))
    assert hasil.index.tz is None
    assert list(hasil.index.date) == [S1]
    idx_wib = pd.DatetimeIndex(["2023-06-15 00:00:00"], tz="Asia/Jakarta")
    assert list(normalisasi_split(pd.Series([5.0], index=idx_wib)).index.date) == [S1]


def test_normalisasi_index_berjam_dibulatkan_dan_tanggal_ganda_ambil_terakhir():
    idx = pd.DatetimeIndex(["2023-06-15 09:00", "2023-06-15 00:00"])
    hasil = normalisasi_split(pd.Series([4.0, 5.0], index=idx))
    assert list(hasil.index) == [pd.Timestamp(S1)]
    assert hasil.iloc[0] == 5.0


def test_split_dari_kolom_cache():
    idx = pd.DatetimeIndex(pd.to_datetime(["2023-06-14", "2023-06-15", "2023-06-16"]), name=NAMA_INDEX)
    bar = pd.DataFrame({"close": [1.0, 2.0, 3.0], "split": [0.0, 5.0, 0.0]}, index=idx)
    hasil = split_dari_kolom(bar)
    assert list(hasil.index.date) == [S1] and hasil.tolist() == [5.0]
    assert len(split_dari_kolom(bar.drop(columns="split"))) == 0
    assert len(split_dari_kolom(None)) == 0


# ---------------------------------------------------------------------------
# F(d)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "d, harapan",
    [
        (date(2021, 1, 4), 10.0),  # jauh sebelum kedua split: 5 × 2
        (date(2023, 6, 14), 10.0),  # sehari sebelum split 1:5
        (S1, 2.0),  # PADA tanggal split: harga bar itu sudah pasca-split → hanya split sesudahnya
        (date(2023, 6, 16), 2.0),
        (date(2024, 2, 29), 2.0),
        (S2, 1.0),
        (date(2026, 10, 15), 1.0),  # live: F(t) = 1
    ],
)
def test_faktor_split_beberapa_split(d, harapan):
    split = seri_split((S1, 5.0), (S2, 2.0))
    assert faktor_split(split, d) == harapan


def test_faktor_split_tanpa_split_dan_tipe_tanggal():
    assert faktor_split(None, date(2024, 1, 1)) == 1.0
    assert faktor_split(pd.Series(dtype="float64"), date(2024, 1, 1)) == 1.0
    split = seri_split((S1, 5.0))
    assert faktor_split(split, pd.Timestamp("2023-06-14")) == 5.0
    assert faktor_split(split, "2023-06-14") == 5.0
    assert faktor_split(split, pd.Timestamp("2023-06-14 15:00")) == 5.0  # jam diabaikan


def test_faktor_reverse_split():
    split = seri_split((S1, 0.2))  # reverse split 5:1 → lembar baru per lembar lama = 0.2
    assert faktor_split(split, date(2023, 6, 14)) == pytest.approx(0.2, abs=0)
    assert faktor_split(split, S1) == 1.0


def test_seri_faktor_identik_dengan_skalar():
    split = seri_split((S1, 5.0), (S2, 2.0), ("2025-05-02", 0.2))
    index = pd.DatetimeIndex(pd.bdate_range("2023-06-01", "2025-06-30"), name=NAMA_INDEX)
    seri = seri_faktor(split, index)
    assert seri.index.equals(index)
    harapan = np.array([faktor_split(split, ts) for ts in index])
    assert np.array_equal(seri.to_numpy(), harapan)
    assert seri.loc["2023-06-14"] == pytest.approx(2.0, rel=1e-15)  # 5 × 2 × 0.2
    assert seri.loc["2025-05-02"] == 1.0
    assert len(seri_faktor(None, index)) == len(index) and (seri_faktor(None, index) == 1.0).all()


# ---------------------------------------------------------------------------
# Konversi nominal ↔ tersesuaikan
# ---------------------------------------------------------------------------


def test_konversi_float_dan_kebalikannya():
    F = 5.0
    assert harga_nominal(1000.0, F) == 5000.0
    assert harga_adj(5000.0, F) == 1000.0
    assert volume_nominal(5_000_000.0, F) == 1_000_000.0  # volume tersesuaikan dikali rasio
    assert lembar_adj(100, F) == 500.0  # 1 lot nominal sebelum split 1:5 = 500 lembar tersesuaikan
    assert lembar_nominal(500.0, F) == 100.0
    # Nilai Rupiah invarian: lembar_adj × harga_adj = lembar_nominal × harga_nominal.
    assert lembar_adj(100, F) * harga_adj(5000.0, F) == 100 * 5000.0


def test_konversi_decimal_tetap_decimal_dan_tepat():
    assert harga_nominal(Decimal("1000.5"), 5.0) == Decimal("5002.50")
    assert isinstance(harga_nominal(Decimal("1000.5"), 5.0), Decimal)
    assert harga_adj(Decimal("5000"), 5.0) == Decimal("1000")
    assert harga_nominal(Decimal("990"), np.float64(2.0)) == Decimal("1980")
    assert lembar_adj(100, Decimal("5")) == Decimal("500")
    assert harga_nominal(1000, 1.0) == 1000  # int × float tetap aritmetika biasa


def test_konversi_tervektor_seri_saham_fiktif_split_1_5():
    # Harga nominal ±5.000 sebelum split 1:5, ±1.000 sesudahnya; cache menyimpan harga tersesuaikan.
    tanggal = ["2023-06-12", "2023-06-13", "2023-06-14", "2023-06-15", "2023-06-16"]
    nominal = pd.Series([5000.0, 5050.0, 4950.0, 1000.0, 1010.0], index=pd.to_datetime(tanggal))
    split = seri_split((S1, 5.0))
    F = seri_faktor(split, nominal.index)
    tersesuaikan = harga_adj(nominal, F)
    assert tersesuaikan.tolist() == [1000.0, 1010.0, 990.0, 1000.0, 1010.0]
    assert np.array_equal(harga_nominal(tersesuaikan, F).to_numpy(), nominal.to_numpy())
    vol_adj = pd.Series([5e6, 5e6, 5e6, 1e6, 1e6], index=nominal.index)
    assert volume_nominal(vol_adj, F).tolist() == [1e6] * 5


# ---------------------------------------------------------------------------
# Pemeriksaan split yang belum disesuaikan (§8.4)
# ---------------------------------------------------------------------------

TGL = ["2023-06-12", "2023-06-13", "2023-06-14", "2023-06-15", "2023-06-16"]


def test_seri_tersesuaikan_lolos():
    bar = bar_dari_close(TGL, [1000, 1010, 990, 1000, 1010])  # sudah dibagi 5 sebelum split
    assert split_belum_disesuaikan(bar, seri_split((S1, 5.0))) == []


def test_seri_mentah_dengan_lompatan_seperlima_terdeteksi():
    bar = bar_dari_close(TGL, [5000, 5050, 4950, 1000, 1010])  # belum disesuaikan: q ≈ 1/5
    assert split_belum_disesuaikan(bar, seri_split((S1, 5.0))) == [S1]
    (p,) = periksa_split(bar, seri_split((S1, 5.0)))
    assert p == PemeriksaanSplit(S1, 5.0, S1, date(2023, 6, 14), 1000 / 4950, True)


def test_reverse_split_mentah_terdeteksi_dan_tersesuaikan_lolos():
    split = seri_split((S1, 0.2))
    mentah = bar_dari_close(TGL, [100, 101, 99, 500, 505])  # q ≈ 5 = 1/r
    assert split_belum_disesuaikan(mentah, split) == [S1]
    sesuai = bar_dari_close(TGL, [500, 505, 495, 500, 505])
    assert split_belum_disesuaikan(sesuai, split) == []


def test_tanpa_bar_pada_tanggal_split_memakai_bar_valid_pertama_sesudahnya():
    # Tidak ada bar valid pada 15 Juni (mis. tanpa transaksi): close_s = bar 16 Juni.
    tgl = ["2023-06-12", "2023-06-13", "2023-06-14", "2023-06-16", "2023-06-19"]
    mentah = bar_dari_close(tgl, [5000, 5050, 4950, 1010, 1000])
    (p,) = periksa_split(mentah, seri_split((S1, 5.0)))
    assert p.tanggal_bar == date(2023, 6, 16) and p.tanggal_sebelum == date(2023, 6, 14)
    assert p.belum_disesuaikan
    sesuai = bar_dari_close(tgl, [1000, 1010, 990, 1010, 1000])
    assert split_belum_disesuaikan(sesuai, seri_split((S1, 5.0))) == []


def test_split_di_luar_rentang_bar_tidak_diperiksa():
    bar = bar_dari_close(TGL, [5000, 5050, 4950, 1000, 1010])
    split = seri_split(("2023-06-12", 5.0), ("2023-07-03", 5.0), ("2020-01-02", 2.0))
    hasil = periksa_split(bar, split)
    assert [p.q for p in hasil] == [None, None, None]
    assert split_belum_disesuaikan(bar, split) == []
    assert split_belum_disesuaikan(bar, None) == []


def test_batas_kedekatan_ketat():
    # r = 4: titik tengah (log) antara 1 dan 1/4 adalah q = 0,5 → |ln q − ln(1/r)| = |ln q| → TIDAK bermasalah.
    split = seri_split((S1, 4.0))
    assert split_belum_disesuaikan(bar_dari_close(TGL[2:4], [1000, 500]), split) == []
    assert split_belum_disesuaikan(bar_dari_close(TGL[2:4], [1000, 499]), split) == [S1]
    assert split_belum_disesuaikan(bar_dari_close(TGL[2:4], [1000, 501]), split) == []


def test_beberapa_split_hanya_yang_bermasalah_dilaporkan():
    tgl = ["2023-06-14", "2023-06-15", "2024-02-29", "2024-03-01"]
    # Split 1:5 sudah disesuaikan; split 1:2 belum (lompatan 1/2).
    bar = bar_dari_close(tgl, [1000, 1000, 1200, 600])
    assert split_belum_disesuaikan(bar, seri_split((S1, 5.0), (S2, 2.0))) == [S2]

"""Uji T-ST strategi BO (SPEC §12.2, §13.1 K_sinyal BO, §4.1 pembulatan, §8.4 F(t)) dan T-CFG untuk
kunci `strategi.bo.*` yang dipakai `Breakout.evaluasi` (§22.1).

Semua data di sini **fiktif** dan deterministik. Fraksi harga belum terverifikasi di config uji,
sehingga harga dibulatkan ke rupiah bulat (harga_maks ke bawah, stop ke atas, §4.1).

Fixture dasar (260 bar valid, posisi 0..259, t = posisi 259 = HARI[259] = 2024-12-27):

- Bar i ≤ 258: close_i = 1000 + i, open = close, high = close + 10, low = close − 10, volume 1.000.000.
  TR_i = max(20, |high_i − close_{i−1}| = 11, |low_i − close_{i−1}| = 9) = 20 → ATR_i = 20 tepat.
  Langkah close antar bar di [−10, +10] tetap memberi TR = 20 (dipakai varian BO3).
- Bar t: open 1260, high 1292,5, low 1255, close 1285, volume 2.000.000.
  TR_t = max(37,5, |1292,5 − 1258| = 34,5, |1255 − 1258| = 3) = 37,5
  ATR_t = (20 × 13 + 37,5)/14 = 297,5/14 = 21,25 (tepat dalam biner)
- BO1: HHV(high, 20)_{t−1} = max(high_239..high_258) = high_258 = 1258 + 10 = 1268; 1285 > 1268 ✓.
  (high_t = 1292,5 > close_t: bila HHV ikut memuat bar t, BO1 tidak akan pernah lolos.)
- BO2: RasioVol(20)_t = 2.000.000 / 1.000.000 = 2,0 ≥ 1,5 ✓.
- BO3: SMA50_t = (Σ_{i=210}^{258}(1000 + i) + 1285)/50 = (49.000 + 11.466 + 1.285)/50 = 61.751/50 = 1.235,02
       SMA200_t = (Σ_{i=60}^{258}(1000 + i) + 1285)/200 = (199.000 + 31.641 + 1.285)/200 = 231.926/200 = 1.159,63
       1285 > 1.235,02 > 1.159,63 ✓.
- BO4: CLV_t = (1285 − 1255)/(1292,5 − 1255) = 30/37,5 = 0,8 ≥ 0,6 ✓.
- BO5: (1285 − 1268)/21,25 = 17/21,25 = 0,8 ≤ 1,0 ✓.
- Level (ruang tersesuaikan, F(t) = 1):
  harga_maks = 1285 + 0,5 × 21,25 = 1.295,625 → bulat ke BAWAH 1.295 (bukan 1.296)
  stop       = 1285 − 2,0 × 21,25 = 1.242,5   → bulat ke ATAS 1.243 (bukan 1.242)
- K_sinyal = 0,5 × clip((2,0 − 1,5)/1,5, 0, 1) + 0,5 × 0,8 = 1/6 + 0,4 = 17/30.
- RataNilai_t = (Σ_{i=240}^{258}(1000 + i) × 10⁶ + 1285 × 2 × 10⁶)/20 = (23.731 + 2.570) × 10⁶/20 = 1.315.050.000.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from asisten_bei.aturan_bei import AturanBEI
from asisten_bei.config import Config
from asisten_bei.data.tampilan import DataPasar
from asisten_bei.strategi.breakout import Breakout
from asisten_bei.strategi.dasar import CekAturan, EvaluasiStrategi, daftar_strategi, hitung_sinyal
from bantu_pasar import HARI, KAL, cfg_uji, data_saham, tgl

KODE = "ABCD"  # saham fiktif
N = 260
T = N - 1  # posisi t di HARI
TGL_T = tgl(T)  # 2024-12-27
KODE_ATURAN = ["BO1", "BO2", "BO3", "BO4", "BO5"]


# ---------------------------------------------------------------------------
# Pembangun data
# ---------------------------------------------------------------------------


def close_dasar() -> list[float]:
    """close_i = 1000 + i untuk i = 0..258 (bar sebelum t)."""
    return [1000.0 + i for i in range(T)]


def buat_df(
    close_sebelum: Sequence[float] | None = None,
    *,
    close_t: float = 1285.0,
    high_t: float = 1292.5,
    low_t: float = 1255.0,
    open_t: float = 1260.0,
    volume_t: float = 2_000_000.0,
    high_ubah: dict[int, float] | None = None,
    volume_ubah: dict[int, float] | None = None,
    close_sesudah: Sequence[float] = (),
) -> pd.DataFrame:
    """Bar OHLCV dengan bar t selalu di HARI[T]. Bar selain t: open = close, high = close + 10,
    low = close − 10, volume 1.000.000. `high_ubah`/`volume_ubah`: {posisi relatif terhadap t (−k): nilai}.
    `close_sesudah`: bar sesudah t (untuk uji split sesudah t / tanpa look-ahead)."""
    seb = list(close_dasar() if close_sebelum is None else close_sebelum)
    ses = list(close_sesudah)
    n = len(seb)
    c = seb + [close_t] + ses
    o = seb + [open_t] + ses
    h = [x + 10 for x in seb] + [high_t] + [x + 10 for x in ses]
    lo = [x - 10 for x in seb] + [low_t] + [x - 10 for x in ses]
    v = [1_000_000.0] * n + [volume_t] + [1_000_000.0] * len(ses)
    for k, nilai in (high_ubah or {}).items():
        h[n + k] = nilai
    for k, nilai in (volume_ubah or {}).items():
        v[n + k] = nilai
    mulai = T - n
    idx = pd.DatetimeIndex(HARI[mulai : mulai + len(c)], name="tanggal").as_unit("ns")
    df = pd.DataFrame({"open": o, "high": h, "low": lo, "close": c, "volume": v}, index=idx, dtype="float64")
    df["sumber"] = "yfinance"
    return df


def evaluasi(
    df: pd.DataFrame,
    cfg: Config | None = None,
    *,
    split: pd.Series | None = None,
    as_of: date = TGL_T,
) -> EvaluasiStrategi:
    cfg = cfg or cfg_uji()
    akhir = max(as_of, df.index[-1].date())
    dp = DataPasar({KODE: data_saham(KODE, df, cfg, akhir, split=split)}, None, KAL, cfg)
    return Breakout(cfg).evaluasi(dp.tampilan(as_of), KODE, AturanBEI(cfg))


def assert_hanya_gagal(hasil: EvaluasiStrategi, aturan: list[str]) -> None:
    assert hasil.sinyal is None
    assert hasil.strategi == "BO" and hasil.kode == KODE
    # Semua aturan selalu dievaluasi dan dicantumkan, walau ada yang gagal lebih dulu.
    assert [c.kode for c in hasil.cek] == KODE_ATURAN
    assert hasil.aturan_gagal == aturan


# Varian BO3 (bar t−20..t identik dengan fixture dasar; semua langkah close di [−10, +10] → TR = 20,
# sehingga ATR_t tetap 21,25 dan BO1/BO2/BO4/BO5 tidak berubah).


def close_bo3_di_bawah_sma_tren() -> list[float]:
    """close_t ≤ SMA50_t, tetapi SMA50_t > SMA200_t.

    i 0..209: 1039 + i (1039..1248); i 210..238: tenda 1249 + 10 × min(i − 210, 238 − i) (puncak 1389);
    i 239..258: 1000 + i (sama dengan dasar).
    SMA50_t  = (29 × 1249 + 10 × 196 + 24.970 + 1285)/50 = (38.181 + 24.970 + 1.285)/50 = 64.436/50 = 1.288,72
    SMA200_t = (Σ_{60}^{209}(1039 + i) + 64.436)/200 = (176.025 + 64.436)/200 = 1.202,305
    close 1285 < 1.288,72 ✗; SMA50 1.288,72 > SMA200 1.202,305 ✓.
    """
    return [1039.0 + i if i < 210 else (1249.0 + 10 * min(i - 210, 238 - i) if i <= 238 else 1000.0 + i)
            for i in range(T)]


def close_bo3_sma_tren_di_bawah_sma_panjang() -> list[float]:
    """close_t > SMA50_t, tetapi SMA50_t ≤ SMA200_t.

    i 0..159: 1160 + 10 × (160 − i) (turun 10 per bar, 2.760..1.170); i 160..258: 1000 + i (sama dengan dasar).
    SMA50_t  = 1.235,02 (bar 210..259 sama dengan dasar)
    SMA200_t = (100 × (2160 + 1170)/2 + Σ_{160}^{258}(1000 + i) + 1285)/200
             = (166.500 + 119.691 + 1.285)/200 = 287.476/200 = 1.437,38
    close 1285 > 1.235,02 ✓; SMA50 1.235,02 > SMA200 1.437,38 ✗.
    """
    return [1160.0 + 10 * (160 - i) if i < 160 else 1000.0 + i for i in range(T)]


# ---------------------------------------------------------------------------
# Fixture lulus semua aturan
# ---------------------------------------------------------------------------


def test_lulus_semua_aturan_sinyal_dan_level_tepat():
    hasil = evaluasi(buat_df())
    assert hasil.aturan_gagal == []
    assert [c.kode for c in hasil.cek] == KODE_ATURAN
    assert all(c.lolos for c in hasil.cek)
    s = hasil.sinyal
    assert s is not None
    assert s.strategi == "BO" and s.kode == KODE and s.tanggal == TGL_T
    assert s.valid is True and s.alasan_tidak_valid is None
    assert s.faktor_split == 1.0
    assert s.harga_ref == Decimal("1285")
    assert s.atr == Decimal("21.25")
    # 1.295,625 → ke bawah (bukan 1.296); 1.242,5 → ke atas (bukan 1.242).
    assert s.harga_maks == Decimal("1295")
    assert s.stop == Decimal("1243")
    assert s.fraksi_dipakai is False  # fraksi belum terverifikasi → rupiah bulat
    assert s.target_r is None  # BO tanpa target tetap
    assert s.k_sinyal == pytest.approx(17 / 30, abs=1e-12)
    assert s.rata_nilai == pytest.approx(1_315_050_000.0, rel=1e-12)
    assert s.cek == hasil.cek


def test_uraian_memuat_nilai_aktual():
    hasil = evaluasi(buat_df())
    assert [c.teks() for c in hasil.cek] == [
        "BO1: close 1.285,00 > high tertinggi 20 bar sebelumnya 1.268,00 ✓",
        "BO2: rasio volume (20 bar sebelumnya) 2,00 ≥ 1,50 ✓",
        "BO3: close 1.285,00 > SMA50 1.235,02 dan SMA50 1.235,02 > SMA200 1.159,63 ✓",
        "BO4: posisi close dalam rentang hari ini (CLV) 0,80 ≥ 0,60 ✓",
        "BO5: (close 1.285,00 − high tertinggi 1.268,00) / ATR 21,25 = 0,80 ≤ 1,00 ✓",
    ]


def test_uraian_aturan_gagal_tetap_berisi_nilai():
    # close 1.289,5: (1289,5 − 1268)/21,25 = 21,5/21,25 = 1,0118 > 1,0.
    hasil = evaluasi(buat_df(close_t=1289.5))
    assert hasil.cek[4] == CekAturan(
        "BO5", "(close 1.289,50 − high tertinggi 1.268,00) / ATR 21,25 = 1,01 ≤ 1,00", False
    )
    assert hasil.cek[4].teks().endswith("✗")


def test_uraian_memakai_periode_dari_config():
    cfg = cfg_uji({
        "strategi.bo.lookback_tertinggi": 7, "strategi.bo.lookback_volume": 9,
        "strategi.bo.sma_tren": 30, "strategi.bo.sma_panjang": 150,
    })
    uraian = [c.uraian for c in evaluasi(buat_df(), cfg).cek]
    assert "high tertinggi 7 bar sebelumnya" in uraian[0]
    assert "rasio volume (9 bar sebelumnya)" in uraian[1]
    assert "SMA30" in uraian[2] and "SMA150" in uraian[2] and "SMA50" not in uraian[2]


# ---------------------------------------------------------------------------
# Satu varian per aturan: hanya aturan itu yang dilanggar
# ---------------------------------------------------------------------------


VARIAN_GAGAL = {
    # high_{t−5} = 1285 = close_t → HHV20 = 1285; 1285 > 1285 salah. BO5: 0/ATR = 0 ≤ 1 tetap lolos.
    "BO1": lambda: buat_df(high_ubah={-5: 1285.0}),
    # RasioVol = 1.499.999/1.000.000 = 1,499999 < 1,5.
    "BO2": lambda: buat_df(volume_t=1_499_999.0),
    # Lihat docstring pembangun close: dua sisi syarat BO3 diuji terpisah.
    "BO3-close": lambda: buat_df(close_bo3_di_bawah_sma_tren()),
    "BO3-sma": lambda: buat_df(close_bo3_sma_tren_di_bawah_sma_panjang()),
    # high_t 1305,5: CLV = 30/50,5 = 0,594 < 0,6. ATR_t = (260 + 50,5)/14 = 22,18 → BO5 = 17/22,18 ✓.
    "BO4": lambda: buat_df(high_t=1305.5),
    # close_t 1289,5: BO5 = 21,5/21,25 = 1,0118 > 1,0. CLV = 34,5/37,5 = 0,92 ✓.
    "BO5": lambda: buat_df(close_t=1289.5),
}


@pytest.mark.parametrize("nama", list(VARIAN_GAGAL))
def test_varian_hanya_melanggar_satu_aturan(nama):
    hasil = evaluasi(VARIAN_GAGAL[nama]())
    assert_hanya_gagal(hasil, [nama.split("-")[0]])


def test_varian_bo3_nilai_sma_sesuai_hitung_tangan():
    # Memastikan varian BO3 benar-benar seperti yang dihitung di docstring (bukan lolos/gagal kebetulan).
    a = evaluasi(buat_df(close_bo3_di_bawah_sma_tren())).cek[2]
    assert "close 1.285,00 > SMA50 1.288,72" in a.uraian and not a.lolos
    b = evaluasi(buat_df(close_bo3_sma_tren_di_bawah_sma_panjang())).cek[2]
    assert b.uraian == "close 1.285,00 > SMA50 1.235,02 dan SMA50 1.235,02 > SMA200 1.437,38" and not b.lolos


# ---------------------------------------------------------------------------
# Batas kedua sisi untuk setiap ambang
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "aturan, df_lolos, df_gagal",
    [
        # BO1 ketat (>): high_{t−5} 1284 → lolos; 1285 (= close) → gagal.
        ("BO1", lambda: buat_df(high_ubah={-5: 1284.0}), lambda: buat_df(high_ubah={-5: 1285.0})),
        # BO1 jendela: high 1290 (> close) di t−21 berada di luar 20 bar → lolos; di t−20 → gagal.
        ("BO1", lambda: buat_df(high_ubah={-21: 1290.0}), lambda: buat_df(high_ubah={-20: 1290.0})),
        # BO2 inklusif (≥): 1.500.000/1.000.000 = 1,5 tepat → lolos; 1.499.999 → gagal.
        ("BO2", lambda: buat_df(volume_t=1_500_000.0), lambda: buat_df(volume_t=1_499_999.0)),
        # BO2 jendela: volume 30 jt di t−21 (di luar 20 bar) → rata 1 jt, rasio 2,0 → lolos;
        # di t−20 → rata (19 + 30)/20 = 2,45 jt, rasio 0,82 → gagal.
        ("BO2", lambda: buat_df(volume_ubah={-21: 30e6}), lambda: buat_df(volume_ubah={-20: 30e6})),
        # BO4 inklusif (≥): high_t 1305 → CLV = 30/50 = 0,6 tepat → lolos; 1305,5 → 0,594 → gagal.
        ("BO4", lambda: buat_df(high_t=1305.0), lambda: buat_df(high_t=1305.5)),
        # BO5 inklusif (≤): close 1.289,25 → 21,25/21,25 = 1,0 tepat → lolos; 1.289,5 → 1,0118 → gagal.
        ("BO5", lambda: buat_df(close_t=1289.25), lambda: buat_df(close_t=1289.5)),
    ],
)
def test_batas_ambang_kedua_sisi(aturan, df_lolos, df_gagal):
    lolos = evaluasi(df_lolos())
    assert lolos.aturan_gagal == [] and lolos.sinyal is not None
    assert_hanya_gagal(evaluasi(df_gagal()), [aturan])


# ---------------------------------------------------------------------------
# K_sinyal BO (§13.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ubah, harapan",
    [
        ({"volume_t": 1_500_000.0}, 0.4),  # 0,5 × clip(0/1,5) + 0,5 × 0,8 = 0,4
        ({"volume_t": 2_000_000.0}, 17 / 30),  # 0,5 × (0,5/1,5) + 0,4
        ({"volume_t": 3_000_000.0}, 0.9),  # 0,5 × clip(1,5/1,5 = 1) + 0,4
        ({"volume_t": 6_000_000.0}, 0.9),  # 0,5 × clip(4,5/1,5 = 3 → 1) + 0,4 (dipotong di atas)
        ({"high_t": 1305.0}, 0.5 / 3 + 0.3),  # CLV 0,6: 0,5 × (1/3) + 0,5 × 0,6
    ],
)
def test_k_sinyal_hitung_tangan(ubah, harapan):
    s = evaluasi(buat_df(**ubah)).sinyal
    assert s is not None
    assert s.k_sinyal == pytest.approx(harapan, abs=1e-12)
    assert 0.0 <= s.k_sinyal <= 1.0


# ---------------------------------------------------------------------------
# NaN, riwayat pendek, ATR tidak sah, tanpa bar valid di t
# ---------------------------------------------------------------------------


def test_riwayat_100_bar_sma_panjang_nan_bo3_gagal():
    # 99 bar sebelum t (close 1159..1257 = 1000 + i untuk i 160..258) + t → SMA200 NaN.
    # ATR Wilder awal = rata-rata 14 TR pertama (TR bar pertama = high − low = 20) → ATR_t = 21,25 tetap.
    hasil = evaluasi(buat_df(close_dasar()[160:]))
    assert_hanya_gagal(hasil, ["BO3"])
    assert hasil.cek[2].uraian.endswith("SMA200 —")


def test_riwayat_15_bar_hhv_rasio_sma_nan():
    # 14 bar sebelum t: HHV20, RasioVol20, SMA50/200 NaN; ATR_t ada (21,25); CLV ada (0,8).
    hasil = evaluasi(buat_df(close_dasar()[-14:]))
    assert_hanya_gagal(hasil, ["BO1", "BO2", "BO3", "BO5"])
    assert hasil.cek[0].uraian == "close 1.285,00 > high tertinggi 20 bar sebelumnya —"
    assert hasil.cek[1].uraian == "rasio volume (20 bar sebelumnya) — ≥ 1,50"
    assert hasil.cek[3].lolos  # CLV tidak butuh riwayat
    assert "ATR 21,25" in hasil.cek[4].uraian and "tidak sah" not in hasil.cek[4].uraian


def test_riwayat_10_bar_atr_nan_bo5_tidak_terpenuhi():
    hasil = evaluasi(buat_df(close_dasar()[-9:]))
    assert_hanya_gagal(hasil, ["BO1", "BO2", "BO3", "BO5"])
    assert hasil.cek[4].uraian == "(close 1.285,00 − high tertinggi —) / ATR —: ATR tidak sah (≤ 0 atau tidak tersedia)"


def test_atr_nol_bo5_tidak_terpenuhi_tanpa_pembagian_nol():
    # Semua bar datar (open = high = low = close = 1000) → TR = 0 → ATR_t = 0. BO5 tidak boleh membagi
    # dengan 0 (ZeroDivisionError) dan dianggap tidak terpenuhi (§9).
    idx = pd.DatetimeIndex(HARI[:N], name="tanggal").as_unit("ns")
    df = pd.DataFrame(
        {"open": 1000.0, "high": 1000.0, "low": 1000.0, "close": 1000.0, "volume": [1e6] * T + [2e6]}, index=idx
    )
    df["sumber"] = "yfinance"
    hasil = evaluasi(df)
    # BO1 1000 > 1000 ✗; BO2 2,0 ✓; BO3 SMA sama ✗; BO4 CLV (high = low) 0,5 < 0,6 ✗; BO5 ATR 0 ✗.
    assert_hanya_gagal(hasil, ["BO1", "BO3", "BO4", "BO5"])
    assert hasil.cek[4].uraian == (
        "(close 1.000,00 − high tertinggi 1.000,00) / ATR 0,00: ATR tidak sah (≤ 0 atau tidak tersedia)"
    )


def _tanpa_bar_t() -> pd.DataFrame:
    return buat_df().iloc[:-1]


def _bar_t_volume_nol() -> pd.DataFrame:
    return buat_df(volume_t=0.0)


@pytest.mark.parametrize("pembuat", [_tanpa_bar_t, _bar_t_volume_nol], ids=["bar-hilang", "tanpa-transaksi"])
def test_tanpa_bar_valid_pada_t(pembuat):
    hasil = evaluasi(pembuat())
    assert hasil.sinyal is None
    assert hasil.cek == (CekAturan("DATA", "tidak ada bar valid pada 2024-12-27", False),)
    assert hasil.aturan_gagal == ["DATA"]


# ---------------------------------------------------------------------------
# Konversi nominal dengan split sesudah t, dan tanpa look-ahead
# ---------------------------------------------------------------------------


def test_split_sesudah_t_konversi_nominal_lalu_dibulatkan():
    # Seri split FIKTIF: 1:2 pada t−100 (sebelum t, tidak ikut F(t)) dan 1:3 pada t+3 → F(t) = 3.
    # Harga cache sudah tersesuaikan (kontinu), jadi level dihitung di ruang tersesuaikan lalu × 3:
    #   harga_ref  = 1285 × 3 = 3.855;  ATR = 21,25 × 3 = 63,75
    #   harga_maks = 1.295,625 × 3 = 3.886,875 → ke bawah 3.886 (bulat dulu lalu × 3 akan memberi 3.885)
    #   stop       = 1.242,5 × 3 = 3.727,5     → ke atas 3.728  (bulat dulu lalu × 3 akan memberi 3.729)
    df = buat_df(close_sesudah=[1286.0, 1287.0, 1288.0, 1289.0, 1290.0])
    split = pd.Series(
        [2.0, 3.0], index=pd.DatetimeIndex([HARI[T - 100], HARI[T + 3]], name="tanggal").as_unit("ns"), name="split"
    )
    hasil = evaluasi(df, split=split)
    s = hasil.sinyal
    assert s is not None and hasil.aturan_gagal == []
    assert s.faktor_split == 3.0
    assert s.harga_ref == Decimal("3855")
    assert s.atr == Decimal("63.75")
    assert s.harga_maks == Decimal("3886")
    assert s.stop == Decimal("3728")
    # K_sinyal dan RataNilai invarian terhadap split.
    assert s.k_sinyal == pytest.approx(17 / 30, abs=1e-12)
    assert s.rata_nilai == pytest.approx(1_315_050_000.0, rel=1e-12)
    # Cek aturan memakai harga tersesuaikan, sama persis dengan tanpa split.
    assert hasil.cek == evaluasi(buat_df()).cek


def test_bar_sesudah_t_tidak_mengubah_evaluasi():
    # Bar sesudah t (jatuh tajam, volume besar) tidak boleh terlihat dari as_of = t (P3).
    acuan = evaluasi(buat_df())
    df = buat_df(close_sesudah=[1200.0, 900.0, 1500.0])
    df.iloc[-3:, df.columns.get_loc("volume")] = [50e6, 1e5, 9e7]
    hasil = evaluasi(df)
    assert hasil.cek == acuan.cek
    assert hasil.sinyal == acuan.sinyal


def test_hitung_sinyal_memakai_breakout():
    cfg = cfg_uji()
    df = buat_df()
    dp = DataPasar({KODE: data_saham(KODE, df, cfg, TGL_T)}, None, KAL, cfg)
    sinyal = hitung_sinyal(dp.tampilan(TGL_T), [KODE], [Breakout(cfg)])
    assert len(sinyal) == 1 and sinyal[0].strategi == "BO" and sinyal[0].stop == Decimal("1243")


# ---------------------------------------------------------------------------
# T-CFG: setiap kunci yang dipakai evaluasi BO benar-benar berpengaruh (P4)
# ---------------------------------------------------------------------------

FIXTURE_CFG = {
    "dasar": buat_df,
    # high 1290 di t−21: di luar jendela 20 (lolos), di dalam jendela 21 (BO1 gagal).
    "high_t21": lambda: buat_df(high_ubah={-21: 1290.0}),
    # volume 30 jt di t−21: rata 20 bar = 1 jt (rasio 2,0); rata 21 bar = 50/21 jt (rasio 0,84 → BO2 gagal).
    "volume_t21": lambda: buat_df(volume_ubah={-21: 30e6}),
}


@pytest.mark.parametrize(
    "kunci, nilai, fixture, harapan",
    [
        ("strategi.bo.lookback_tertinggi", 21, "high_t21", {"gagal": ["BO1"]}),
        ("strategi.bo.lookback_volume", 21, "volume_t21", {"gagal": ["BO2"]}),
        ("strategi.bo.min_rasio_volume", 2.01, "dasar", {"gagal": ["BO2"]}),  # 2,0 < 2,01
        ("strategi.bo.min_posisi_close", 0.81, "dasar", {"gagal": ["BO4"]}),  # CLV 0,8 < 0,81
        ("strategi.bo.maks_lewat_level_atr", 0.79, "dasar", {"gagal": ["BO5"]}),  # 0,8 > 0,79
        # SMA250_t = (Σ_{10}^{258}(1000 + i) + 1285)/250 = 283.651/250 = 1.134,604 < SMA200 1.159,63.
        ("strategi.bo.sma_tren", 250, "dasar", {"gagal": ["BO3"]}),
        # SMA20_t = (Σ_{240}^{258}(1000 + i) + 1285)/20 = 25.016/20 = 1.250,8 > SMA50 1.235,02.
        ("strategi.bo.sma_panjang", 20, "dasar", {"gagal": ["BO3"]}),
        # stop = 1285 − 1,0 × 21,25 = 1.263,75 → ke atas 1.264.
        ("strategi.bo.stop_atr", 1.0, "dasar", {"stop": Decimal("1264")}),
        # K_sinyal = 0,5 × clip((2,0 − 1,0)/1,0) + 0,4 = 0,9.
        ("strategi.bo.min_rasio_volume", 1.0, "dasar", {"k_sinyal": 0.9}),
        # harga_maks = 1285 + 0 × ATR = 1.285.
        ("entry.gap_maks_atr", 0.0, "dasar", {"harga_maks": Decimal("1285")}),
    ],
)
def test_kunci_config_mengubah_hasil(kunci, nilai, fixture, harapan):
    df = FIXTURE_CFG[fixture]()
    awal = evaluasi(df)
    assert awal.sinyal is not None and awal.aturan_gagal == []  # default: lolos semua
    ubah = evaluasi(df, cfg_uji({kunci: nilai}))
    if "gagal" in harapan:
        assert_hanya_gagal(ubah, harapan["gagal"])
        return
    assert ubah.sinyal is not None
    for atribut, nilai_harapan in harapan.items():
        baru, lama = getattr(ubah.sinyal, atribut), getattr(awal.sinyal, atribut)
        if isinstance(nilai_harapan, float):
            assert baru == pytest.approx(nilai_harapan, abs=1e-12)
            assert not math.isclose(baru, lama)
        else:
            assert baru == nilai_harapan and baru != lama


@pytest.mark.parametrize(
    "kunci, nilai, ubah_df, gagal_awal",
    [
        # high 1290 di t−20: di dalam jendela default 20 (BO1 gagal), di luar jendela 19 (lolos).
        ("strategi.bo.lookback_tertinggi", 19, {"high_ubah": {-20: 1290.0}}, ["BO1"]),
        # volume 30 jt di t−20: rata 20 bar = 2,45 jt (rasio 0,82, BO2 gagal); rata 19 bar = 1 jt (rasio 2,0).
        ("strategi.bo.lookback_volume", 19, {"volume_ubah": {-20: 30e6}}, ["BO2"]),
    ],
)
def test_kunci_lookback_arah_sebaliknya_membuat_lolos(kunci, nilai, ubah_df, gagal_awal):
    # Arah gagal → lolos memastikan periode benar-benar dibaca dari config (kolom indikator dengan
    # periode lain tidak ada; periode tertulis di kode akan memberi NaN → tetap gagal).
    df = buat_df(**ubah_df)
    assert_hanya_gagal(evaluasi(df), gagal_awal)
    ubah = evaluasi(df, cfg_uji({kunci: nilai}))
    assert ubah.aturan_gagal == [] and ubah.sinyal is not None


# ---------------------------------------------------------------------------
# Registri: daftar_strategi
# ---------------------------------------------------------------------------

def test_daftar_strategi_memuat_bo_bila_aktif():
    cfg = cfg_uji()
    daftar = daftar_strategi(cfg)
    assert [s.kode for s in daftar] == ["PB", "BO", "MR"]
    bo = daftar[1]
    assert isinstance(bo, Breakout) and bo.kode == "BO" and bo.cfg is cfg
    hanya = daftar_strategi(cfg, hanya=["bo"])
    assert len(hanya) == 1 and isinstance(hanya[0], Breakout)


def test_daftar_strategi_tanpa_bo_bila_tidak_aktif():
    cfg = cfg_uji({"strategi.bo.aktif": False})
    daftar = daftar_strategi(cfg)
    assert [s.kode for s in daftar] == ["PB", "MR"]
    assert not any(isinstance(s, Breakout) for s in daftar)
    assert daftar_strategi(cfg, hanya=["BO"]) == []


# ---------------------------------------------------------------------------
# BO3 batas kesamaan (aturan ketat "close > SMA_tren" dan "SMA_tren > SMA_panjang"), config rilis
# ---------------------------------------------------------------------------

_T_BO3 = 259


def _evaluasi_bo3(close_seb: list[float], *, close_t, open_t, high_t, low_t):
    from asisten_bei.indikator import k_sma

    c = close_seb + [close_t]
    df = pd.DataFrame(
        {
            "open": close_seb + [open_t],
            "high": [x + 10 for x in close_seb] + [high_t],
            "low": [x - 10 for x in close_seb] + [low_t],
            "close": c,
            "volume": [1_000_000.0] * len(close_seb) + [2_000_000.0],
        },
        index=pd.DatetimeIndex(HARI[: len(c)], name="tanggal").as_unit("ns"),
        dtype="float64",
    )
    df["sumber"] = "yfinance"
    cfg = cfg_uji()
    dv = DataPasar({"BOBO": data_saham("BOBO", df, cfg, tgl(_T_BO3))}, None, KAL, cfg).tampilan(tgl(_T_BO3))
    p = cfg.strategi.bo
    baris = dv.baris("BOBO")
    return Breakout(cfg).evaluasi(dv, "BOBO", AturanBEI(cfg)), baris[k_sma(p.sma_tren)], baris[k_sma(p.sma_panjang)]


def test_bo3_sma_tren_sama_dengan_sma_panjang_tidak_lolos():
    # SMA50_t = (48 × 1000 + 985 + 1015)/50 = 1000 = SMA200_t = (198 × 1000 + 985 + 1015)/200.
    seb = [1000.0] * _T_BO3
    seb[230] = 985.0
    ev, sma_tren, sma_panjang = _evaluasi_bo3(seb, close_t=1015.0, open_t=1000.0, high_t=1020.0, low_t=1000.0)
    assert sma_tren == sma_panjang == 1000.0  # prasyarat
    assert ev.aturan_gagal == ["BO3"] and ev.sinyal is None


def test_bo3_close_sama_dengan_sma_tren_tidak_lolos():
    # SMA50_t = (28 × 1014 + 1008 + 20 × 980 + 1000)/50 = 1000 = close_t; SMA200_t = 925 < 1000.
    seb = [900.0] * 210 + [1014.0] * 29 + [980.0] * 20
    seb[220] = 1008.0
    ev, sma_tren, sma_panjang = _evaluasi_bo3(seb, close_t=1000.0, open_t=985.0, high_t=1005.0, low_t=985.0)
    assert sma_tren == 1000.0 and sma_panjang == 925.0  # prasyarat
    assert ev.aturan_gagal == ["BO3"] and ev.sinyal is None

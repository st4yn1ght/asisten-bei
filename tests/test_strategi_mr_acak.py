"""Uji T-ST strategi MR (SPEC §12.3, §13.1 K_sinyal MR) dan ACAK (§12.4), pembulatan level (§4.1),
konversi nominal F(t) (§8.4), serta T-CFG untuk kunci `strategi.mr.*` / `strategi.acak.*` yang dipakai
`Oversold.evaluasi` / `Acak.evaluasi` (§22.1).

Semua data di sini **fiktif** dan deterministik. Fraksi harga belum terverifikasi di config rilis, sehingga
harga dibulatkan ke rupiah bulat (harga_maks ke bawah, stop ke atas, §4.1). Tidak ada tabel BEI dipakai.

Fixture dasar (260 bar valid, posisi 0..259; t = posisi 259 = HARI[259] = 2024-12-27):

- Bar i ≤ 257: close_i = 1000 + i, open = close, high = close + 10, low = close − 10, volume 1.000.000.
  TR_0 = high − low = 20; TR_i = max(20, |high_i − close_{i−1}| = 11, |low_i − close_{i−1}| = 9) = 20.
  Setiap langkah close s dengan |s| ≤ 10 tetap memberi TR = 20 (dipakai varian MR1).
- Bar t−1 (i = 258): close 1251 (turun 6), high 1261, low 1241 → TR = max(20, 4, 16) = 20 → ATR = 20.
- Bar t (i = 259): close 1242 (turun 9), open 1242, high 1255, low 1228, volume 1.000.000.
  TR_t = max(27, |1255 − 1251| = 4, |1228 − 1251| = 23) = 27
  ATR_t = (20 × 13 + 27)/14 = 287/14 = 20,5 (tepat dalam biner)
- RSI(2) Wilder: semua selisih +1 sampai bar 257 → avg_naik = 1, avg_turun = 0 (tepat).
  bar 258: avg_naik = (1 + 0)/2 = 0,5;  avg_turun = (0 + 6)/2 = 3
  bar 259: avg_naik = 0,5/2 = 0,25;     avg_turun = (3 + 9)/2 = 6
  RSI = 100 − 100/(1 + 0,25/6) = 100 − 100 × 24/25 = 4 → MR2: 4 < 10 ✓
- MR1: SMA200_t = (Σ_{i=60}^{257}(1000 + i) + 1251 + 1242)/200
                = (198.000 + 317 × 99 + 2.493)/200 = (198.000 + 31.383 + 2.493)/200 = 231.876/200 = 1.159,38
       1242 > 1.159,38 ✓
- MR3: close_{t−1} − close_t = 1251 − 1242 = 9 ≤ 3,0 × 20,5 = 61,5 ✓
- Level (ruang tersesuaikan, F(t) = 1):
  harga_maks = 1242 + 0,5 × 20,5 = 1.252,25 → bulat ke BAWAH 1.252 (bukan 1.253)
  stop MR    = 1242 − 2,5 × 20,5 = 1.190,75 → bulat ke ATAS 1.191 (bukan 1.190)
  stop ACAK  = 1242 − 2,0 × 20,5 = 1.201 (bulat)
- K_sinyal MR = clip((10 − 4)/10, 0, 1) = 0,6; K_sinyal ACAK = 0,5.
- RataNilai_t = (Σ_{i=240}^{257}(1000 + i) + 1251 + 1242) × 10⁶/20 = (18.000 + 4.473 + 2.493) × 10⁶/20
              = 24.966 × 10⁶/20 = 1.248.300.000.
"""

from __future__ import annotations

import hashlib
import inspect
import math
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import date, datetime
from decimal import Decimal

import pandas as pd
import pytest

from asisten_bei.aturan_bei import AturanBEI
from asisten_bei.config import Config
from asisten_bei.data.tampilan import DataPasar
from asisten_bei.strategi import acak as modul_acak
from asisten_bei.strategi.acak import Acak, nilai_u
from asisten_bei.strategi.dasar import CekAturan, EvaluasiStrategi, Strategi, daftar_strategi
from asisten_bei.strategi.oversold import Oversold
from bantu_pasar import AKAR_PROYEK, HARI, KAL, cfg_uji, data_saham, tgl

KODE = "ABCD"  # saham fiktif
N = 260
T = N - 1  # posisi t di HARI
TGL_T = tgl(T)  # 2024-12-27
ATURAN_MR = ["MR1", "MR2", "MR3"]
SEED_DEFAULT = 12345  # strategi.acak.seed di config rilis
U_DASAR = 0.304339514001792  # u(12345, 2024-12-27, "ABCD"), lihat test_nilai_u_sesuai_rumus_spec


# ---------------------------------------------------------------------------
# Pembangun data
# ---------------------------------------------------------------------------


def close_dasar() -> list[float]:
    """close_i = 1000 + i untuk i = 0..257 (bar sebelum t−1)."""
    return [1000.0 + i for i in range(T - 1)]


def close_dataran(m: int, panjang: int, mulai: int = 60) -> list[float]:
    """Dasar + tonjolan trapesium di dalam jendela SMA200 (posisi 60..259): naik 9/bar selama m bar,
    datar `panjang` bar pada 9m, turun 9/bar selama m bar. Langkah close: +10, +1, −8 (semua |s| ≤ 10 →
    TR = 20, ATR tidak berubah). Sesudah tonjolan ≥ 100 langkah +1, sehingga avg_naik RSI kembali tepat
    1 dan avg_turun ≈ 10/2^100 (hilang saat dijumlah dengan 6) → RSI_t tetap tepat 4.
    Luas tonjolan = 9 × (m(m+1)/2 + panjang × m + m(m−1)/2) = 9m(m + panjang)."""
    c = close_dasar()
    for i in range(mulai, mulai + 2 * m + panjang + 1):
        j = i - mulai
        c[i] += 9 * j if j <= m else (9 * m if j <= m + panjang else 9 * (2 * m + panjang - j))
    return c


def buat_df(
    close_awal: Sequence[float] | None = None,
    *,
    close_kemarin: float = 1251.0,
    close_t: float = 1242.0,
    high_t: float | None = None,
    low_t: float | None = None,
    volume_t: float = 1_000_000.0,
    close_nol: float | None = None,
    close_sesudah: Sequence[float] = (),
) -> pd.DataFrame:
    """Bar OHLCV dengan bar t selalu di HARI[T]. Bar selain t: open = close, high = close + 10,
    low = close − 10, volume 1.000.000. Bar t: high = close_t + 13, low = close_t − 14 (default).
    `close_nol`: sisipkan bar tanpa transaksi (volume 0) di antara t−1 dan t.
    `close_sesudah`: bar sesudah t (uji split sesudah t)."""
    awal = list(close_dasar() if close_awal is None else close_awal)
    sebelum = awal + [close_kemarin] + ([close_nol] if close_nol is not None else [])
    sesudah = list(close_sesudah)
    c = sebelum + [close_t] + sesudah
    h = [x + 10 for x in sebelum] + [close_t + 13 if high_t is None else high_t] + [x + 10 for x in sesudah]
    lo = [x - 10 for x in sebelum] + [close_t - 14 if low_t is None else low_t] + [x - 10 for x in sesudah]
    v = [1_000_000.0] * len(sebelum) + [volume_t] + [1_000_000.0] * len(sesudah)
    if close_nol is not None:
        v[len(sebelum) - 1] = 0.0
    mulai = T - len(sebelum)
    assert mulai >= 0
    idx = pd.DatetimeIndex(HARI[mulai : mulai + len(c)], name="tanggal").as_unit("ns")
    df = pd.DataFrame({"open": c, "high": h, "low": lo, "close": c, "volume": v}, index=idx, dtype="float64")
    df["sumber"] = "yfinance"
    return df


def pasar_satu(df: pd.DataFrame, cfg: Config, *, split: pd.Series | None = None, akhir: date = TGL_T) -> DataPasar:
    akhir = max(akhir, df.index[-1].date())
    return DataPasar({KODE: data_saham(KODE, df, cfg, akhir, split=split)}, None, KAL, cfg)


def evaluasi(
    df: pd.DataFrame | None = None,
    cfg: Config | None = None,
    *,
    kelas: type[Strategi] = Oversold,
    split: pd.Series | None = None,
    as_of: date = TGL_T,
) -> EvaluasiStrategi:
    cfg = cfg or cfg_uji()
    df = buat_df() if df is None else df
    dp = pasar_satu(df, cfg, split=split, akhir=as_of)
    return kelas(cfg).evaluasi(dp.tampilan(as_of), KODE, AturanBEI(cfg))


def assert_hanya_gagal(hasil: EvaluasiStrategi, aturan: list[str]) -> None:
    assert hasil.sinyal is None
    assert hasil.strategi == "MR" and hasil.kode == KODE
    # Semua aturan selalu dievaluasi dan dicantumkan, walau ada yang gagal lebih dulu.
    assert [c.kode for c in hasil.cek] == ATURAN_MR
    assert hasil.aturan_gagal == aturan


def cfg_acak(peluang: float, **lain) -> Config:
    ubah = {"strategi.acak.peluang_sinyal_pct": peluang}
    ubah.update({f"strategi.acak.{k}": v for k, v in lain.items()})
    return cfg_uji(ubah)


def split_sesudah_t(rasio: float = 5.0) -> pd.Series:
    """Seri split FIKTIF: 1:2 pada t−100 (sebelum t, tidak ikut F(t)) dan 1:`rasio` pada t+3 → F(t) = rasio."""
    return pd.Series(
        [2.0, rasio], index=pd.DatetimeIndex([HARI[T - 100], HARI[T + 3]], name="tanggal").as_unit("ns"), name="split"
    )


SESUDAH_T = [1243.0, 1244.0, 1245.0, 1246.0, 1247.0]  # bar t+1..t+5 (ruang tersesuaikan, tanpa lonjakan)


# ===========================================================================
# MR — fixture lulus semua aturan
# ===========================================================================


def test_mr_lolos_semua_aturan_level_dan_k_sinyal():
    hasil = evaluasi()
    assert hasil.strategi == "MR" and hasil.kode == KODE
    assert hasil.aturan_gagal == []
    assert [c.kode for c in hasil.cek] == ATURAN_MR
    # Uraian memuat nilai aktual (gaya Indonesia).
    assert [c.teks() for c in hasil.cek] == [
        "MR1: close 1.242,00 > SMA200 1.159,38 ✓",
        "MR2: RSI(2) 4,00 < 10,00 ✓",
        "MR3: close kemarin 1.251,00 − close 1.242,00 = 9,00 ≤ 3,00 × ATR 20,50 = 61,50 ✓",
    ]
    s = hasil.sinyal
    assert s is not None
    assert s.tanggal == TGL_T and s.kode == KODE and s.strategi == "MR"
    assert s.cek == hasil.cek
    assert s.faktor_split == 1.0
    assert s.harga_ref == Decimal("1242")
    assert s.atr == Decimal("20.5")
    # harga_maks 1.252,25 → ke bawah; stop 1.190,75 → ke atas (§4.1).
    assert s.harga_maks == Decimal("1252")
    assert s.stop == Decimal("1191")
    assert s.k_sinyal == pytest.approx(0.6, abs=1e-12)  # (10 − 4)/10
    assert s.target_r is None  # MR tanpa target tetap
    assert s.rata_nilai == pytest.approx(1_248_300_000.0, rel=1e-12)
    assert s.valid is True and s.alasan_tidak_valid is None  # MR tidak memakai maks_jarak_stop_atr
    assert s.fraksi_dipakai is False  # fraksi belum terverifikasi → rupiah bulat


# ===========================================================================
# MR — satu varian per aturan yang hanya melanggar aturan itu
# ===========================================================================

# MR1: tonjolan trapesium m = 34, datar 20 → luas 9 × 34 × 54 = 16.524
#      SMA200_t = (231.876 + 16.524)/200 = 248.400/200 = 1.242 = close_t → "close > SMA" gagal (batas sama).
#      RSI, ATR, close_{t−1} tidak berubah (lihat close_dataran).
# MR2: close_t = 1250 (turun 1): avg_turun = (3 + 1)/2 = 2, avg_naik = 0,25
#      RSI = 100 × 0,25/2,25 = 11,11 ≥ 10 ✗. TR_t = max(27, |1263 − 1251|, |1236 − 1251|) = 27 → ATR 20,5.
#      SMA200 = (229.383 + 1251 + 1250)/200 = 1.159,42 < 1250 ✓; MR3: 1 ≤ 61,5 ✓.
# MR3: close_t = 1178 (turun 73), high 1185, low 1175:
#      TR_t = max(10, |1185 − 1251| = 66, |1175 − 1251| = 76) = 76 → ATR_t = (260 + 76)/14 = 24
#      73 > 3,0 × 24 = 72 ✗. RSI = 100 × 0,25/(0,25 + (3 + 73)/2) = 25/38,25 = 0,65 ✓;
#      SMA200 = (229.383 + 1251 + 1178)/200 = 1.159,06 < 1178 ✓.
VARIAN_MR = {
    "MR1-close-sama-dengan-SMA200": (
        lambda: buat_df(close_dataran(34, 20)),
        "MR1: close 1.242,00 > SMA200 1.242,00 ✗",
    ),
    "MR2-RSI-tidak-jenuh-jual": (
        lambda: buat_df(close_t=1250.0),
        "MR2: RSI(2) 11,11 < 10,00 ✗",
    ),
    "MR3-jatuh-ekstrem-sehari": (
        lambda: buat_df(close_t=1178.0, high_t=1185.0, low_t=1175.0),
        "MR3: close kemarin 1.251,00 − close 1.178,00 = 73,00 ≤ 3,00 × ATR 24,00 = 72,00 ✗",
    ),
}


@pytest.mark.parametrize("nama", list(VARIAN_MR))
def test_mr_varian_satu_aturan_gagal(nama):
    buat, teks_gagal = VARIAN_MR[nama]
    hasil = evaluasi(buat())
    assert_hanya_gagal(hasil, [nama.split("-")[0]])
    gagal = [c for c in hasil.cek if not c.lolos]
    assert gagal[0].teks() == teks_gagal


def test_mr1_batas_dua_sisi():
    # Sisi lolos dekat batas: datar 19 → luas 9 × 34 × 53 = 16.218 → SMA200 = 248.094/200 = 1.240,47 < 1242.
    hasil = evaluasi(buat_df(close_dataran(34, 19)))
    assert hasil.sinyal is not None
    assert hasil.cek[0].teks() == "MR1: close 1.242,00 > SMA200 1.240,47 ✓"
    # Sisi gagal tepat di batas (SMA200 = close) sudah diuji di VARIAN_MR; via config: SMA(1)_t = close_t.
    hasil = evaluasi(cfg=cfg_uji({"strategi.mr.sma_tren": 1}))
    assert_hanya_gagal(hasil, ["MR1"])
    assert hasil.cek[0].uraian == "close 1.242,00 > SMA1 1.242,00"


def test_mr2_batas_dua_sisi():
    # RSI_t = 4 tepat (lihat docstring). rsi_maks = 4 → "4 < 4" gagal; sedikit di atas 4 → lolos.
    hasil = evaluasi(cfg=cfg_uji({"strategi.mr.rsi_maks": 4.0}))
    assert_hanya_gagal(hasil, ["MR2"])
    sedikit_di_atas = math.nextafter(4.0, math.inf)
    hasil = evaluasi(cfg=cfg_uji({"strategi.mr.rsi_maks": sedikit_di_atas}))
    assert hasil.sinyal is not None
    assert 0.0 < hasil.sinyal.k_sinyal < 1e-12  # (rsi_maks − 4)/rsi_maks ≈ 0


def test_mr3_batas_dua_sisi():
    # close_t = 1179 (turun 72), high 1185, low 1175 → TR_t = 76, ATR_t = 24: 72 ≤ 3 × 24 = 72 → lolos.
    hasil = evaluasi(buat_df(close_t=1179.0, high_t=1185.0, low_t=1175.0))
    assert hasil.aturan_gagal == []
    assert hasil.cek[2].uraian == "close kemarin 1.251,00 − close 1.179,00 = 72,00 ≤ 3,00 × ATR 24,00 = 72,00"
    s = hasil.sinyal
    assert s is not None
    assert s.atr == Decimal("24")
    assert s.harga_maks == Decimal("1191")  # 1179 + 0,5 × 24
    assert s.stop == Decimal("1119")  # 1179 − 2,5 × 24
    # Turun 73 (sisi gagal) diuji di VARIAN_MR.


def test_mr3_memakai_close_bar_valid_sebelumnya():
    # Bar tanpa transaksi (volume 0, close 1400) di antara t−1 dan t tidak dihitung (§4.1 x_{t−k}).
    # Bila dipakai: 1400 − 1242 = 158 > 61,5 → MR3 gagal. Bar dasar digeser satu (i = 1..257) agar t tetap
    # di HARI[T]; jendela SMA200/RSI/ATR atas bar valid tetap sama.
    df = buat_df(close_dasar()[1:], close_nol=1400.0)
    assert df.loc[HARI[T - 1], "volume"] == 0.0
    hasil = evaluasi(df)
    assert hasil.aturan_gagal == []
    assert hasil.cek[2].uraian == "close kemarin 1.251,00 − close 1.242,00 = 9,00 ≤ 3,00 × ATR 20,50 = 61,50"
    assert hasil.sinyal is not None and hasil.sinyal.stop == Decimal("1191")


# ===========================================================================
# MR — NaN / riwayat kurang / ATR tidak sah
# ===========================================================================


def test_mr_riwayat_kurang_untuk_sma_tren():
    # 150 bar valid (close 1110..1257, lalu 1251, 1242): SMA200 NaN → MR1 gagal; ATR (14) dan RSI (2)
    # tetap 20,5 dan 4 → MR2/MR3 lolos.
    hasil = evaluasi(buat_df([1000.0 + i for i in range(110, T - 1)]))
    assert_hanya_gagal(hasil, ["MR1"])
    assert hasil.cek[0].uraian == "close 1.242,00 > SMA200 —"


def test_mr_atr_nan_membuat_mr3_gagal():
    # 12 bar valid < atr_periode 14 → ATR NaN → MR3 gagal; SMA200 NaN → MR1 gagal; RSI(2) = 4 → MR2 lolos.
    hasil = evaluasi(buat_df([1000.0 + i for i in range(248, T - 1)]))
    assert_hanya_gagal(hasil, ["MR1", "MR3"])
    assert "ATR tidak sah" in hasil.cek[2].uraian


def test_mr_dua_bar_semua_aturan_tetap_dicantumkan():
    # Hanya t−1 dan t: SMA, RSI(2) (butuh 3 bar), dan ATR NaN → ketiga aturan gagal, semua tercantum.
    hasil = evaluasi(buat_df([]))
    assert_hanya_gagal(hasil, ["MR1", "MR2", "MR3"])
    assert hasil.cek[1].uraian == "RSI(2) — < 10,00"


class _DVPalsu:
    """DataView minimal untuk ATR ≤ 0 (tidak dapat dibentuk dari bar valid yang turun)."""

    def __init__(self, cfg: Config, baris: dict[str, float], close_kemarin: float):
        self.cfg = cfg
        self.as_of = TGL_T
        self._baris = pd.Series(baris, dtype="float64")
        self._close_kemarin = close_kemarin

    def baris(self, kode):
        return self._baris

    def sebelum(self, kode, kolom, k):
        assert (kolom, k) == ("close", 1)
        return self._close_kemarin

    def faktor_split(self, kode, d=None):
        return 1.0


@pytest.mark.parametrize("atr", [0.0, -1.0, math.nan])
def test_mr3_atr_tidak_sah_tidak_terpenuhi(atr):
    # Tanpa penjagaan, ATR = 0 dan harga tidak turun memberi "0 ≤ 3 × 0" → lolos. §9: harus gagal.
    cfg = cfg_uji()
    dv = _DVPalsu(cfg, {"close": 1242.0, "atr": atr, "sma_200": 1100.0, "rsi_2": 4.0, "rata_nilai": 1e9}, 1242.0)
    hasil = Oversold(cfg).evaluasi(dv, KODE, AturanBEI(cfg))
    assert_hanya_gagal(hasil, ["MR3"])
    assert "ATR tidak sah" in hasil.cek[2].uraian


def test_mr_stop_tidak_positif_tanpa_sinyal():
    # stop_atr 61: stop = 1242 − 61 × 20,5 = −8,5 ≤ 0 → cek STOP gagal (tidak melempar error).
    hasil = evaluasi(cfg=cfg_uji({"strategi.mr.stop_atr": 61.0}))
    assert hasil.sinyal is None
    assert [c.kode for c in hasil.cek] == ATURAN_MR + ["STOP"]
    assert hasil.aturan_gagal == ["STOP"]


# ===========================================================================
# MR & ACAK — tidak ada bar valid pada t
# ===========================================================================


@pytest.mark.parametrize("kelas", [Oversold, Acak])
@pytest.mark.parametrize("kasus", ["sesudah-bar-terakhir", "bar-t-tanpa-transaksi"])
def test_tanpa_bar_valid_pada_t(kelas, kasus):
    cfg = cfg_uji({"strategi.acak.peluang_sinyal_pct": 100.0})  # ACAK: tanpa bar tetap tanpa sinyal
    if kasus == "sesudah-bar-terakhir":
        as_of = tgl(T + 1)  # 2024-12-30, data berakhir di t
        hasil = evaluasi(buat_df(), cfg, kelas=kelas, as_of=as_of)
    else:
        as_of = TGL_T
        hasil = evaluasi(buat_df(volume_t=0.0), cfg, kelas=kelas)
    assert hasil.sinyal is None
    assert hasil.cek == (CekAturan("DATA", f"tidak ada bar valid pada {as_of.isoformat()}", False),)
    assert hasil.aturan_gagal == ["DATA"]
    assert hasil.strategi == kelas.kode


# ===========================================================================
# Konversi nominal: split sesudah t → F(t) ≠ 1 (§8.4)
# ===========================================================================


def test_mr_split_sesudah_t_konversi_nominal_lalu_dibulatkan():
    # F(t) = 5 (split 1:5 pada t+3; split 1:2 pada t−100 tidak ikut).
    # harga_maks = 1.252,25 × 5 = 6.261,25 → 6.261 (bulat-dulu-lalu-kali akan memberi 1.252 × 5 = 6.260)
    # stop       = 1.190,75 × 5 = 5.953,75 → 5.954 (bulat-dulu-lalu-kali akan memberi 1.191 × 5 = 5.955)
    # harga_ref  = 1242 × 5 = 6.210; ATR nominal = 20,5 × 5 = 102,5
    tanpa_split = evaluasi(buat_df(close_sesudah=SESUDAH_T))
    hasil = evaluasi(buat_df(close_sesudah=SESUDAH_T), split=split_sesudah_t())
    s = hasil.sinyal
    assert s is not None
    assert s.faktor_split == 5.0
    assert s.harga_ref == Decimal("6210")
    assert s.atr == Decimal("102.5")
    assert s.harga_maks == Decimal("6261")
    assert s.stop == Decimal("5954")
    # K_sinyal dan RataNilai invarian terhadap split; cek aturan memakai harga tersesuaikan.
    assert s.k_sinyal == tanpa_split.sinyal.k_sinyal
    assert s.rata_nilai == tanpa_split.sinyal.rata_nilai
    assert hasil.cek == tanpa_split.cek
    # Bar sesudah t tidak memengaruhi apa pun selain F(t): sama dengan fixture tanpa bar sesudah t.
    assert hasil.cek == evaluasi().cek
    assert tanpa_split.sinyal.harga_maks == Decimal("1252") and tanpa_split.sinyal.stop == Decimal("1191")


def test_acak_split_sesudah_t_konversi_nominal():
    # stop = (1242 − 2 × 20,5) × 5 = 1201 × 5 = 6.005; harga_maks = 6.261,25 → 6.261.
    hasil = evaluasi(buat_df(close_sesudah=SESUDAH_T), cfg_acak(100.0), kelas=Acak, split=split_sesudah_t())
    s = hasil.sinyal
    assert s is not None and s.faktor_split == 5.0
    assert (s.harga_ref, s.harga_maks, s.stop, s.atr) == (
        Decimal("6210"), Decimal("6261"), Decimal("6005"), Decimal("102.5")
    )


# ===========================================================================
# T-CFG — setiap kunci strategi.mr.* yang dipakai evaluasi berpengaruh
# ===========================================================================


def _gagal(*aturan: str) -> Callable[[EvaluasiStrategi], None]:
    def periksa(h: EvaluasiStrategi) -> None:
        assert_hanya_gagal(h, list(aturan))

    return periksa


def _sinyal(**harap) -> Callable[[EvaluasiStrategi], None]:
    def periksa(h: EvaluasiStrategi) -> None:
        assert h.sinyal is not None
        for nama, nilai in harap.items():
            if isinstance(nilai, float):
                assert getattr(h.sinyal, nama) == pytest.approx(nilai, abs=1e-12), nama
            else:
                assert getattr(h.sinyal, nama) == nilai, nama

    return periksa


# Dasar: sinyal dengan harga_maks 1252, stop 1191, k_sinyal 0,6.
KASUS_CFG_MR = [
    # SMA(1)_t = close_t → "close > SMA" gagal. (Implementasi yang memakai sma_200 tetap lolos → terdeteksi.)
    ("strategi.mr.sma_tren", 1, _gagal("MR1")),
    # RSI(3): avg_naik 1 → 2/3 → 4/9; avg_turun 0 → 6/3 = 2 → (2 × 2 + 9)/3 = 13/3
    #         RSI = 100 × (4/9)/(4/9 + 13/3) = 400/43 = 9,30 < 10 → K = (10 − 400/43)/10 = 3/43
    ("strategi.mr.rsi_periode", 3, _sinyal(k_sinyal=3 / 43)),
    # RSI(5): avg_naik 1 → 0,8 → 0,64; avg_turun 0 → 6/5 = 1,2 → (1,2 × 4 + 9)/5 = 2,76
    #         RSI = 100 × 0,64/3,4 = 18,82 ≥ 10 → MR2 gagal
    ("strategi.mr.rsi_periode", 5, _gagal("MR2")),
    ("strategi.mr.rsi_maks", 3.0, _gagal("MR2")),  # 4 < 3 salah
    ("strategi.mr.rsi_maks", 20.0, _sinyal(k_sinyal=0.8)),  # (20 − 4)/20
    ("strategi.mr.maks_turun_harian_atr", 0.4, _gagal("MR3")),  # 9 > 0,4 × 20,5 = 8,2
    ("strategi.mr.stop_atr", 3.0, _sinyal(stop=Decimal("1181"))),  # 1242 − 61,5 = 1.180,5 → ke atas 1.181
    ("entry.gap_maks_atr", 1.0, _sinyal(harga_maks=Decimal("1262"))),  # 1242 + 20,5 = 1.262,5 → ke bawah
]


@pytest.mark.parametrize(
    "kunci, nilai, periksa", KASUS_CFG_MR, ids=[f"{k}={v}" for k, v, _ in KASUS_CFG_MR]
)
def test_cfg_mr_kunci_berpengaruh(kunci, nilai, periksa):
    dasar = evaluasi()
    assert dasar.sinyal is not None and dasar.sinyal.k_sinyal == pytest.approx(0.6)
    hasil = evaluasi(cfg=cfg_uji({kunci: nilai}))
    periksa(hasil)
    # Keluaran (cek atau level) berubah dibanding config rilis.
    assert (hasil.cek, hasil.sinyal) != (dasar.cek, dasar.sinyal)


def test_cfg_mr_uraian_mengikuti_periode():
    hasil = evaluasi(cfg=cfg_uji({"strategi.mr.rsi_periode": 3}))
    assert hasil.cek[1].uraian == "RSI(3) 9,30 < 10,00"
    hasil = evaluasi(cfg=cfg_uji({"strategi.mr.sma_tren": 150}))
    assert hasil.cek[0].uraian.startswith("close 1.242,00 > SMA150 ")


def test_cfg_mr_semua_kunci_sinyal_teruji():
    """Setiap kunci strategi.mr yang dipakai aturan sinyal punya kasus T-CFG (aktif: uji daftar_strategi)."""
    teruji = {k.split(".")[-1] for k, *_ in KASUS_CFG_MR if k.startswith("strategi.mr.")}
    assert teruji | {"aktif"} == {"aktif", "sma_tren", "rsi_periode", "rsi_maks", "maks_turun_harian_atr", "stop_atr"}


@pytest.mark.parametrize(
    "kunci, nilai",
    [("strategi.mr.sma_keluar", 7), ("strategi.mr.min_hari_tahan", 3), ("strategi.mr.stop_waktu_hari", 8)],
)
def test_cfg_mr_kunci_exit_tidak_mengubah_sinyal(kunci, nilai):
    # Kunci exit dipakai mesin exit (§15), bukan aturan sinyal §12.3.
    assert evaluasi(cfg=cfg_uji({kunci: nilai})) == evaluasi()


# ===========================================================================
# daftar_strategi
# ===========================================================================


def test_daftar_strategi_mr_aktif_dan_nonaktif():
    aktif = daftar_strategi(cfg_uji())
    assert [s.kode for s in aktif] == ["PB", "BO", "MR"]
    assert isinstance(aktif[2], Oversold)
    tanpa_mr = daftar_strategi(cfg_uji({"strategi.mr.aktif": False}))
    assert "MR" not in [s.kode for s in tanpa_mr]
    assert not any(isinstance(s, Oversold) for s in tanpa_mr)
    assert [type(s) for s in daftar_strategi(cfg_uji(), hanya=["mr"])] == [Oversold]
    assert daftar_strategi(cfg_uji({"strategi.mr.aktif": False}), hanya=["mr"]) == []


def test_daftar_strategi_acak_hanya_bila_diminta():
    cfg = cfg_uji()
    assert "ACAK" not in [s.kode for s in daftar_strategi(cfg)]
    assert "ACAK" not in [s.kode for s in daftar_strategi(cfg, termasuk_acak=False)]
    dengan = daftar_strategi(cfg, termasuk_acak=True)
    assert [s.kode for s in dengan] == ["PB", "BO", "MR", "ACAK"]
    assert isinstance(dengan[-1], Acak)
    assert [type(s) for s in daftar_strategi(cfg, termasuk_acak=True, hanya=["acak"])] == [Acak]
    assert daftar_strategi(cfg, termasuk_acak=False, hanya=["acak"]) == []


# ===========================================================================
# ACAK — rumus u §12.4
# ===========================================================================


def _u_mandiri(seed: int, tanggal: str, kode: str) -> float:
    """Rumus §12.4 ditulis ulang di sini (independen dari implementasi)."""
    pesan = f"{seed}|{tanggal}|{kode}".encode()
    return int.from_bytes(hashlib.sha256(pesan).digest()[:8], "big") / 2**64


def test_nilai_u_sesuai_rumus_spec():
    assert _u_mandiri(SEED_DEFAULT, "2024-12-27", "ABCD") == U_DASAR  # nilai acuan tetap
    assert nilai_u(SEED_DEFAULT, date(2024, 12, 27), "ABCD") == U_DASAR
    # Tanggal ditulis YYYY-MM-DD dengan nol di depan.
    assert nilai_u(SEED_DEFAULT, date(2024, 1, 5), "ABCD") == _u_mandiri(SEED_DEFAULT, "2024-01-05", "ABCD")
    assert nilai_u(SEED_DEFAULT, date(2024, 1, 5), "ABCD") == 0.9451075398300504
    # Hanya bagian tanggal yang dipakai (`t:%Y-%m-%d`), juga bila t berupa Timestamp/datetime.
    assert nilai_u(SEED_DEFAULT, pd.Timestamp("2024-12-27"), "ABCD") == U_DASAR
    assert nilai_u(SEED_DEFAULT, datetime(2024, 12, 27, 19, 30), "ABCD") == U_DASAR
    # Bergantung pada seed, tanggal, dan kode.
    assert nilai_u(1, date(2024, 12, 27), "ABCD") == _u_mandiri(1, "2024-12-27", "ABCD") == 0.9248530714750227
    assert nilai_u(SEED_DEFAULT, date(2024, 12, 27), "ABCE") == _u_mandiri(SEED_DEFAULT, "2024-12-27", "ABCE")
    assert len({nilai_u(SEED_DEFAULT, date(2024, 12, 27), k) for k in ("ABCD", "ABCE", "WXYZ")}) == 3


def test_acak_evaluasi_cek_memakai_u():
    # Default peluang 2% → u = 0,3043 ≥ 0,02 → tanpa sinyal.
    hasil = evaluasi(kelas=Acak)
    assert hasil.strategi == "ACAK" and hasil.kode == KODE and hasil.sinyal is None
    assert hasil.cek == (CekAturan("ACAK", "u = 0,304340 < peluang 0,020000 (2,00%, seed 12345)", False),)
    assert hasil.aturan_gagal == ["ACAK"]


def test_acak_batas_peluang_dua_sisi():
    # Sinyal ⇔ u < peluang/100 (ketat).
    di_atas = evaluasi(cfg=cfg_acak(U_DASAR * 100 * (1 + 1e-9)), kelas=Acak)
    assert di_atas.sinyal is not None and di_atas.aturan_gagal == []
    di_bawah = evaluasi(cfg=cfg_acak(U_DASAR * 100 * (1 - 1e-9)), kelas=Acak)
    assert di_bawah.sinyal is None and di_bawah.aturan_gagal == ["ACAK"]
    # Tepat di batas: peluang/100 == u (prasyarat float diperiksa) → "u < u" salah → tanpa sinyal.
    tepat = U_DASAR * 100
    assert tepat / 100 == U_DASAR
    sama = evaluasi(cfg=cfg_acak(tepat), kelas=Acak)
    assert sama.sinyal is None and sama.aturan_gagal == ["ACAK"]
    # Satu langkah float di atasnya → peluang/100 > u → sinyal.
    sedikit_di_atas = math.nextafter(tepat, math.inf)
    assert sedikit_di_atas / 100 > U_DASAR
    assert evaluasi(cfg=cfg_acak(sedikit_di_atas), kelas=Acak).sinyal is not None


def test_acak_sinyal_level_k_sinyal_target():
    hasil = evaluasi(cfg=cfg_acak(100.0), kelas=Acak)
    assert [c.kode for c in hasil.cek] == ["ACAK"] and hasil.aturan_gagal == []
    s = hasil.sinyal
    assert s is not None
    assert s.strategi == "ACAK" and s.tanggal == TGL_T and s.kode == KODE
    assert s.harga_ref == Decimal("1242")
    assert s.atr == Decimal("20.5")
    assert s.harga_maks == Decimal("1252")  # 1.252,25 → ke bawah
    assert s.stop == Decimal("1201")  # 1242 − 2,0 × 20,5
    assert s.k_sinyal == 0.5
    assert s.target_r == 2.0
    assert s.rata_nilai == pytest.approx(1_248_300_000.0, rel=1e-12)
    assert s.valid is True


def test_acak_atr_tidak_sah_tanpa_sinyal():
    # 12 bar valid < atr_periode 14 → ATR NaN; walau u < peluang (100%), stop tidak dapat dihitung.
    hasil = evaluasi(buat_df([1000.0 + i for i in range(248, T - 1)]), cfg_acak(100.0), kelas=Acak)
    assert hasil.sinyal is None
    assert [c.kode for c in hasil.cek] == ["ACAK", "STOP"]
    assert hasil.aturan_gagal == ["STOP"]
    assert "ATR tidak sah" in hasil.cek[1].uraian


@pytest.mark.parametrize("atr", [0.0, -1.0, math.nan])
def test_acak_atr_tidak_sah_dilaporkan(atr):
    # ATR = 0 akan memberi stop = close (tanpa penjagaan, gagal belakangan sebagai "stop < harga maks");
    # alasannya wajib "ATR tidak sah".
    cfg = cfg_acak(100.0)
    dv = _DVPalsu(cfg, {"close": 1242.0, "atr": atr, "rata_nilai": 1e9}, 1242.0)
    hasil = Acak(cfg).evaluasi(dv, KODE, AturanBEI(cfg))
    assert hasil.sinyal is None
    assert [c.kode for c in hasil.cek] == ["ACAK", "STOP"] and hasil.aturan_gagal == ["STOP"]
    assert "ATR tidak sah" in hasil.cek[1].uraian


def test_acak_stop_tidak_positif_tanpa_sinyal():
    # stop = 1242 − 61 × 20,5 = −8,5 ≤ 0.
    hasil = evaluasi(cfg=cfg_acak(100.0, stop_atr=61.0), kelas=Acak)
    assert hasil.sinyal is None and hasil.aturan_gagal == ["STOP"]


# Dasar ACAK untuk T-CFG: peluang 50% → u(12345) = 0,3043 < 0,5 → sinyal, stop 1201, target 2,0.
KASUS_CFG_ACAK = [
    ("peluang_sinyal_pct", 30.0, lambda h: h.sinyal is None),  # 0,3043 ≥ 0,30
    ("seed", 1, lambda h: h.sinyal is None),  # u(1, 2024-12-27, ABCD) = 0,9249 ≥ 0,5
    ("stop_atr", 2.5, lambda h: h.sinyal.stop == Decimal("1191")),  # 1.190,75 → ke atas
    ("target_r", 3.0, lambda h: h.sinyal.target_r == 3.0),
]


@pytest.mark.parametrize("kunci, nilai, periksa", KASUS_CFG_ACAK, ids=[k for k, *_ in KASUS_CFG_ACAK])
def test_cfg_acak_kunci_berpengaruh(kunci, nilai, periksa):
    dasar = evaluasi(cfg=cfg_acak(50.0), kelas=Acak)
    assert dasar.sinyal is not None and dasar.sinyal.stop == Decimal("1201") and dasar.sinyal.target_r == 2.0
    hasil = evaluasi(cfg=cfg_acak(50.0, **{kunci: nilai}), kelas=Acak)
    assert periksa(hasil)
    assert (hasil.cek, hasil.sinyal) != (dasar.cek, dasar.sinyal)


def test_cfg_acak_semua_kunci_sinyal_teruji():
    """stop_waktu_hari dipakai mesin exit (§15); kunci lain punya kasus T-CFG."""
    assert {k for k, *_ in KASUS_CFG_ACAK} == {"peluang_sinyal_pct", "seed", "stop_atr", "target_r"}
    assert evaluasi(cfg=cfg_acak(50.0, stop_waktu_hari=3), kelas=Acak) == evaluasi(cfg=cfg_acak(50.0), kelas=Acak)


# ===========================================================================
# ACAK — determinisme, frekuensi, peluang 0 dan 100
# ===========================================================================

KODE_BANYAK = [f"AC{i:02d}" for i in range(10)]  # 10 saham fiktif dengan bar identik
POSISI_BANYAK = list(range(T - 59, T + 1))  # 60 hari bursa terakhir; ATR sah di semuanya


@pytest.fixture(scope="module")
def pasar_banyak():
    def buat(cfg: Config) -> DataPasar:
        df = buat_df()
        return DataPasar({k: data_saham(k, df, cfg, TGL_T) for k in KODE_BANYAK}, None, KAL, cfg)

    return buat


def _evaluasi_banyak(dp: DataPasar, cfg: Config) -> dict[tuple[str, date], EvaluasiStrategi]:
    st, bei = Acak(cfg), AturanBEI(cfg)
    hasil = {}
    for pos in POSISI_BANYAK:
        dv = dp.tampilan(tgl(pos))
        for k in KODE_BANYAK:
            hasil[(k, tgl(pos))] = st.evaluasi(dv, k, bei)
    return hasil


def test_acak_deterministik_antar_panggilan(pasar_banyak):
    cfg = cfg_acak(20.0)
    a1 = _evaluasi_banyak(pasar_banyak(cfg), cfg)
    a2 = _evaluasi_banyak(pasar_banyak(cfg), cfg)  # DataPasar & objek strategi baru
    assert a1 == a2


def test_acak_frekuensi_sinyal_mendekati_peluang(pasar_banyak):
    # 600 pasangan (saham, tanggal), peluang 20% → harapan 120, simpangan baku √(600 × 0,2 × 0,8) ≈ 9,8.
    cfg = cfg_acak(20.0)
    hasil = _evaluasi_banyak(pasar_banyak(cfg), cfg)
    for (k, d), ev in hasil.items():
        assert (ev.sinyal is not None) == (_u_mandiri(SEED_DEFAULT, d.isoformat(), k) < 0.2), (k, d)
    jumlah = sum(ev.sinyal is not None for ev in hasil.values())
    assert 80 <= jumlah <= 160
    # Saham berbeda pada tanggal yang sama tidak selalu sama nasibnya (u bergantung pada kode).
    per_tanggal = {d: {ev.sinyal is not None for (k, dd), ev in hasil.items() if dd == d} for d in map(tgl, POSISI_BANYAK)}
    assert any(len(v) == 2 for v in per_tanggal.values())


def test_acak_frekuensi_rumus_u_seragam():
    # 100 kode × 50 tanggal = 5.000 nilai u; peluang 2% → harapan 100 (simpangan baku ≈ 9,9).
    nilai = [nilai_u(SEED_DEFAULT, tgl(p), f"K{i:03d}") for i in range(100) for p in range(50)]
    assert all(0.0 <= u < 1.0 for u in nilai)
    assert 60 <= sum(u < 0.02 for u in nilai) <= 140
    assert 0.48 <= sum(nilai) / len(nilai) <= 0.52  # rata-rata seragam [0, 1) = 0,5 (galat baku ≈ 0,004)


@pytest.mark.parametrize("peluang, harap", [(0.0, False), (100.0, True)])
def test_acak_peluang_nol_dan_seratus(pasar_banyak, peluang, harap):
    cfg = cfg_acak(peluang)
    hasil = _evaluasi_banyak(pasar_banyak(cfg), cfg)
    assert all((ev.sinyal is not None) is harap for ev in hasil.values())
    assert all(ev.aturan_gagal == ([] if harap else ["ACAK"]) for ev in hasil.values())


def test_acak_identik_antar_proses_tanpa_hash_bawaan():
    # hash() bawaan diacak per proses (PYTHONHASHSEED); u wajib sama di semua proses.
    skrip = (
        "from datetime import date; from asisten_bei.strategi.acak import nilai_u; "
        "print(repr(nilai_u(12345, date(2024, 12, 27), 'ABCD')))"
    )
    keluaran = set()
    for benih in ("0", "1", "987654"):
        env = dict(os.environ, PYTHONHASHSEED=benih)
        env["PYTHONPATH"] = os.pathsep.join([str(AKAR_PROYEK / "src"), env.get("PYTHONPATH", "")])
        hasil = subprocess.run(
            [sys.executable, "-c", skrip], env=env, capture_output=True, text=True, check=True, timeout=120
        )
        keluaran.add(hasil.stdout.strip())
    assert keluaran == {repr(U_DASAR)}


def test_acak_tidak_memakai_hash_bawaan():
    sumber = inspect.getsource(modul_acak)
    kode_saja = "\n".join(baris.split("#", 1)[0] for baris in sumber.splitlines())
    tanpa_docstring = re.sub(r'"""(.|\n)*?"""', "", kode_saja)
    assert re.search(r"(?<![\w.])hash\s*\(", tanpa_docstring) is None

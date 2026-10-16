"""Definisi indikator (SPEC §9).

Semua fungsi menerima Series/DataFrame **bar valid** milik satu saham (§4.1: bar pada hari bursa yang
lolos pemeriksaan per bar §8.5 dan volumenya > 0). Bar tanpa transaksi dan bar rusak sudah dibuang
sebelum sampai ke sini, sehingga "n bar terakhir" dan `x_{t−k}` dihitung dalam bar valid saham itu,
bukan dalam hari kalender.

Kausalitas (P3, §8.8): nilai pada `t` hanya memakai bar ≤ `t`. Yang dipakai hanya jendela bergulir
ke belakang, `shift(k)` dengan k ≥ 1 (tertinggal), dan rekursi Wilder yang berjalan maju. Tidak ada
`shift(-k)` dan tidak ada normalisasi atas seluruh sampel. Dibuktikan oleh
`tests/test_tanpa_lookahead.py` (T-LA1a, T-LA3).

Jika bar valid belum cukup, nilainya NaN (§9). NaN di masukan ikut merambat; tidak ada pengisian
atau tebakan (P1).
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd

from .config import Config
from .data.provider import KOLOM_BAR, NAMA_INDEX, ke_ts

# ---------------------------------------------------------------------------
# Nama kolom standar (dipakai strategi, skor, kondisi pasar, dan laporan)
# ---------------------------------------------------------------------------


def k_sma(n: int) -> str:
    return f"sma_{n}"


def k_rsi(n: int) -> str:
    return f"rsi_{n}"


def k_hhv_high(n: int) -> str:
    return f"hhv_high_{n}"


def k_llv_low(n: int) -> str:
    return f"llv_low_{n}"


def k_rasio_vol(n: int) -> str:
    return f"rasio_vol_{n}"


def k_ret(n: int) -> str:
    return f"ret_{n}"


def _cek_n(n: int) -> int:
    if isinstance(n, bool) or not isinstance(n, (int, np.integer)) or n < 1:
        raise ValueError(f"periode indikator harus bilangan bulat >= 1 (nilai: {n!r})")
    return int(n)


def _float(x: pd.Series) -> pd.Series:
    return x.astype("float64")


# ---------------------------------------------------------------------------
# Jendela bergulir
# ---------------------------------------------------------------------------


def sma(x: pd.Series, n: int) -> pd.Series:
    """`SMA(x, n)_t`: rata-rata `x` pada n bar valid terakhir, termasuk t. NaN bila < n bar."""
    n = _cek_n(n)
    return _float(x).rolling(n, min_periods=n).mean()


def hhv_sebelum(x: pd.Series, n: int) -> pd.Series:
    """`HHV(x, n)_{t−1}`: maksimum `x` pada n bar valid SEBELUM t (bar t tidak termasuk)."""
    n = _cek_n(n)
    return _float(x).rolling(n, min_periods=n).max().shift(1)


def llv(x: pd.Series, n: int) -> pd.Series:
    """`LLV(x, n)_t`: minimum `x` pada n bar valid terakhir, termasuk t."""
    n = _cek_n(n)
    return _float(x).rolling(n, min_periods=n).min()


def hhv_sejak(high: pd.Series, mulai: date, sampai: date) -> float:
    """`HHV_sejak_entry(high)_d`: maksimum `high` atas bar valid bertanggal mulai..sampai (inklusif
    keduanya). Pemanggil memberi `mulai` = hari entry (bar hari sinyal tidak termasuk). NaN bila tidak
    ada bar valid dalam rentang. Index `high` harus DatetimeIndex urut naik."""
    idx = pd.DatetimeIndex(high.index)
    a = int(idx.searchsorted(ke_ts(mulai), side="left"))
    b = int(idx.searchsorted(ke_ts(sampai), side="right"))
    if b <= a:
        return math.nan
    return float(high.to_numpy(dtype="float64")[a:b].max())


# ---------------------------------------------------------------------------
# Rentang & volatilitas
# ---------------------------------------------------------------------------


def true_range(df: pd.DataFrame) -> pd.Series:
    """`TR_t = max(high − low, |high − close_{t−1}|, |low − close_{t−1}|)`; bar valid pertama:
    `high − low`. `close_{t−1}` = close bar valid sebelumnya (bukan hari kalender sebelumnya)."""
    h = df["high"].to_numpy(dtype="float64")
    l = df["low"].to_numpy(dtype="float64")
    c = df["close"].to_numpy(dtype="float64")
    rentang = h - l
    tr = rentang.copy()
    if len(c) > 1:
        c_seb = c[:-1]
        # np.maximum merambatkan NaN (tidak ada nilai yang ditebak).
        tr[1:] = np.maximum(rentang[1:], np.maximum(np.abs(h[1:] - c_seb), np.abs(l[1:] - c_seb)))
    return pd.Series(tr, index=df.index, name="tr", dtype="float64")


def _wilder(nilai: np.ndarray, n: int, awal: int) -> np.ndarray:
    """Smoothing Wilder. Nilai pertama pada posisi `awal + n − 1` = rata-rata `nilai[awal:awal+n]`;
    selanjutnya `S_t = (S_{t−1} × (n − 1) + nilai_t) / n`. Posisi sebelumnya NaN."""
    hasil = np.full(len(nilai), np.nan, dtype="float64")
    pertama = awal + n - 1
    if len(nilai) <= pertama:
        return hasil
    # fsum: jumlah yang dibulatkan benar, tidak bergantung pada urutan penjumlahan internal numpy.
    s = math.fsum(nilai[awal : awal + n].tolist()) / n
    hasil[pertama] = s
    sisa = nilai[pertama + 1 :].tolist()
    for i, x in enumerate(sisa, start=pertama + 1):
        s = (s * (n - 1) + x) / n
        hasil[i] = s
    return hasil


def atr_wilder(df: pd.DataFrame, n: int) -> pd.Series:
    """`ATR_t` Wilder: nilai pertama (bar valid ke-n) = rata-rata n TR pertama; selanjutnya
    `ATR_t = (ATR_{t−1} × (n − 1) + TR_t) / n`. NaN sebelum bar ke-n."""
    n = _cek_n(n)
    tr = true_range(df).to_numpy(dtype="float64")
    return pd.Series(_wilder(tr, n, 0), index=df.index, name="atr", dtype="float64")


def _nilai_rsi(naik: float, turun: float) -> float:
    if turun == 0:
        if naik > 0:
            return 100.0
        if naik == 0:
            return 50.0
        return math.nan  # NaN di masukan
    return 100.0 - 100.0 / (1.0 + naik / turun)


def rsi_wilder(close: pd.Series, n: int) -> pd.Series:
    """`RSI(n)_t` Wilder. Selisih close (antar bar valid berurutan) dipisah naik/turun. Rata-rata
    awal = mean n selisih pertama, sehingga RSI pertama ada pada bar valid ke-(n+1); selanjutnya
    smoothing Wilder periode n. `RSI = 100 − 100/(1 + avg_naik/avg_turun)`;
    avg_turun = 0 dan avg_naik > 0 → 100; keduanya 0 → 50."""
    n = _cek_n(n)
    c = close.to_numpy(dtype="float64")
    hasil = np.full(len(c), np.nan, dtype="float64")
    if len(c) > n:
        selisih = np.diff(c)  # selisih[i − 1] = c[i] − c[i − 1]
        # np.clip merambatkan NaN; −0.0 dari clip tidak mengubah apa pun (−0.0 == 0).
        naik = np.clip(selisih, 0.0, None)
        turun = np.clip(-selisih, 0.0, None)
        avg_naik = _wilder(naik, n, 0)
        avg_turun = _wilder(turun, n, 0)
        for i in range(n - 1, len(selisih)):
            hasil[i + 1] = _nilai_rsi(avg_naik[i], avg_turun[i])
    return pd.Series(hasil, index=close.index, name=k_rsi(n), dtype="float64")


# ---------------------------------------------------------------------------
# Posisi close, nilai transaksi, volume, return
# ---------------------------------------------------------------------------


def clv(df: pd.DataFrame) -> pd.Series:
    """`CLV_t = (close − low)/(high − low)`; jika high = low: 0,5."""
    h = df["high"].to_numpy(dtype="float64")
    l = df["low"].to_numpy(dtype="float64")
    c = df["close"].to_numpy(dtype="float64")
    rentang = h - l
    with np.errstate(divide="ignore", invalid="ignore"):
        hasil = np.where(rentang == 0, 0.5, (c - l) / rentang)
    return pd.Series(hasil, index=df.index, name="clv", dtype="float64")


def nilai_tx(df: pd.DataFrame, pengali_volume) -> pd.Series:
    """`NilaiTx_t = close × volume × data.pengali_volume` (Rupiah; invarian terhadap split karena
    close tersesuaikan × volume tersesuaikan = nominal × nominal)."""
    hasil = _float(df["close"]) * _float(df["volume"]) * float(pengali_volume)
    return hasil.rename("nilai_tx")


def rasio_vol(volume: pd.Series, n: int) -> pd.Series:
    """`RasioVol(n)_t = volume_t / SMA(volume, n)_{t−1}`: rata-rata atas n bar valid SEBELUM t.
    Penyebut ≤ 0 (tidak mungkin pada bar valid) → NaN, bukan tak hingga."""
    n = _cek_n(n)
    rata_sebelum = sma(volume, n).shift(1)
    rata_sebelum = rata_sebelum.where(rata_sebelum > 0)
    return (_float(volume) / rata_sebelum).rename(k_rasio_vol(n))


def ret(close: pd.Series, n: int) -> pd.Series:
    """`Ret(n)_t = close_t / close_{t−n} − 1` (n bar valid ke belakang)."""
    n = _cek_n(n)
    c = _float(close)
    return (c / c.shift(n) - 1.0).rename(k_ret(n))


# ---------------------------------------------------------------------------
# Tabel indikator per saham
# ---------------------------------------------------------------------------


def periode_sma(cfg: Config) -> list[int]:
    """Semua periode SMA yang dipakai config (strategi & kondisi pasar), unik dan urut."""
    s = cfg.strategi
    kp = cfg.kondisi_pasar
    return sorted(
        {
            s.pb.sma_cepat, s.pb.sma_tren, s.pb.sma_panjang,
            s.bo.sma_tren, s.bo.sma_panjang,
            s.mr.sma_tren, s.mr.sma_keluar,
            kp.sma_pendek, kp.sma_panjang, kp.breadth_sma,
        }
    )


def _cek_bar_valid(valid: pd.DataFrame, kolom: tuple[str, ...], periksa_volume: bool) -> None:
    hilang = [k for k in kolom if k not in valid.columns]
    if hilang:
        raise ValueError(f"kolom bar valid hilang: {', '.join(hilang)}")
    if not isinstance(valid.index, pd.DatetimeIndex):
        raise ValueError("index bar valid harus DatetimeIndex (tanggal)")
    if not valid.index.is_monotonic_increasing or valid.index.has_duplicates:
        raise ValueError("index bar valid harus urut naik tanpa tanggal ganda")
    harga = valid[[k for k in ("open", "high", "low", "close") if k in kolom]].to_numpy(dtype="float64")
    if len(harga) and np.isnan(harga).any():
        raise ValueError("bar valid tidak boleh memuat harga NaN (bar rusak harus dibuang dulu, §8.5)")
    if periksa_volume:
        vol = valid["volume"].to_numpy(dtype="float64")
        if len(vol) and not (vol > 0).all():
            raise ValueError(
                "indikator hanya menerima bar valid: bar tanpa transaksi (volume 0/NaN) harus dibuang dulu (§4.1)"
            )


def hitung_indikator(valid: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Semua indikator §9 untuk satu saham, tervektor atas seluruh bar valid (kausal).

    Kolom: open, high, low, close, volume (salinan), tr, atr (`indikator.atr_periode`), clv, nilai_tx,
    rata_nilai (`likuiditas.periode_hari`), sma_n untuk setiap n di `periode_sma(cfg)`,
    rsi_n (`strategi.mr.rsi_periode`), hhv_high_n (`strategi.bo.lookback_tertinggi`),
    llv_low_n (`strategi.pb.lookback_swing_low`), rasio_vol_n (`strategi.bo.lookback_volume`),
    ret_n (`skor.rs_lookback`). Index = `valid.index`.

    `valid` wajib berisi bar valid saja (volume > 0, OHLC tidak NaN); selain itu ValueError —
    bar tanpa transaksi tidak boleh masuk perhitungan (§4.1).
    """
    _cek_bar_valid(valid, KOLOM_BAR, periksa_volume=True)
    s = cfg.strategi
    o = _float(valid["open"])
    h = _float(valid["high"])
    l = _float(valid["low"])
    c = _float(valid["close"])
    v = _float(valid["volume"])
    harga = pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": v}, index=valid.index)

    kolom: dict[str, pd.Series] = {"open": o, "high": h, "low": l, "close": c, "volume": v}
    kolom["tr"] = true_range(harga)
    kolom["atr"] = atr_wilder(harga, cfg.indikator.atr_periode)
    kolom["clv"] = clv(harga)
    kolom["nilai_tx"] = nilai_tx(harga, cfg.data.pengali_volume)
    kolom["rata_nilai"] = sma(kolom["nilai_tx"], cfg.likuiditas.periode_hari)
    for n in periode_sma(cfg):
        kolom[k_sma(n)] = sma(c, n)
    kolom[k_rsi(s.mr.rsi_periode)] = rsi_wilder(c, s.mr.rsi_periode)
    kolom[k_hhv_high(s.bo.lookback_tertinggi)] = hhv_sebelum(h, s.bo.lookback_tertinggi)
    kolom[k_llv_low(s.pb.lookback_swing_low)] = llv(l, s.pb.lookback_swing_low)
    kolom[k_rasio_vol(s.bo.lookback_volume)] = rasio_vol(v, s.bo.lookback_volume)
    kolom[k_ret(cfg.skor.rs_lookback)] = ret(c, cfg.skor.rs_lookback)

    hasil = pd.DataFrame({k: x.to_numpy(dtype="float64") for k, x in kolom.items()}, index=valid.index)
    hasil.index.name = NAMA_INDEX
    return hasil


def hitung_indikator_indeks(valid: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Indikator IHSG untuk kondisi pasar (§10): close, sma_{kondisi_pasar.sma_pendek},
    sma_{kondisi_pasar.sma_panjang}. Volume indeks opsional dan tidak dipakai."""
    _cek_bar_valid(valid, ("open", "high", "low", "close"), periksa_volume=False)
    kp = cfg.kondisi_pasar
    c = _float(valid["close"])
    kolom = {"close": c}
    for n in sorted({kp.sma_pendek, kp.sma_panjang}):
        kolom[k_sma(n)] = sma(c, n)
    hasil = pd.DataFrame({k: x.to_numpy(dtype="float64") for k, x in kolom.items()}, index=valid.index)
    hasil.index.name = NAMA_INDEX
    return hasil

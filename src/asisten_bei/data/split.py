"""Split saham dan harga nominal (SPEC §8.4).

Konvensi (sama dengan yfinance `auto_adjust=False` dan cache §8.3): OHLC di cache sudah DIBAGI rasio
split sesudahnya, volume DIKALI. Rasio = lembar baru per lembar lama (split 1:5 → 5.0; reverse split
5:1 → 0.2). Dividen tidak disesuaikan.

Faktor split `F(d)` = hasil kali rasio split bertanggal SETELAH `d` (split pada tanggal `d` sendiri
tidak ikut: harga bar `d` sudah harga pasca-split). Hubungannya:
- harga nominal = harga tersesuaikan × F(d)
- volume nominal = volume tersesuaikan ÷ F(d)
- lembar tersesuaikan = lembar nominal × F(d)

`F` memakai data split terkini. Ini sah (bukan look-ahead) karena hanya MEMBATALKAN penyesuaian yang
sudah tertanam di harga cache, bukan informasi harga masa depan. Di live, F(t) = 1.

Seri split standar: `pd.Series` float64 bernama "split", index tanggal (tz-naive, tengah malam, nama
"tanggal"), urut naik, hanya rasio > 0 dan ≠ 1 (rasio 0/NaN dari yfinance = tidak ada split).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from ..waktu import ZONA
from .provider import NAMA_INDEX, ke_ts, split_kosong

# ---------------------------------------------------------------------------
# Normalisasi
# ---------------------------------------------------------------------------


def normalisasi_split(split: pd.Series | None) -> pd.Series:
    """Seri split standar: buang rasio 0/NaN/inf/negatif/1.0, index tanggal WIB tz-naive, urut naik.

    Index ber-zona waktu dikonversi ke Asia/Jakarta dulu (tidak pernah lewat UTC), lalu dibulatkan ke
    tengah malam. Tanggal ganda: entri terakhir yang dipakai.
    """
    if split is None or len(split) == 0:
        return split_kosong()
    nilai = pd.to_numeric(pd.Series(split), errors="coerce").to_numpy(dtype="float64")
    idx = pd.DatetimeIndex(split.index)
    if idx.tz is not None:
        idx = idx.tz_convert(ZONA).tz_localize(None)
    idx = idx.normalize().as_unit("ns")
    s = pd.Series(nilai, index=idx, name="split", dtype="float64")
    s = s[np.isfinite(s.to_numpy()) & (s.to_numpy() > 0) & (s.to_numpy() != 1.0)]
    s = s.sort_index(kind="mergesort")
    s = s[~s.index.duplicated(keep="last")]
    s.index.name = NAMA_INDEX
    return s


def split_dari_kolom(bar: pd.DataFrame | None) -> pd.Series:
    """Seri split dari kolom `split` DataFrame cache (0.0 = tidak ada split pada bar itu)."""
    if bar is None or "split" not in bar.columns or len(bar) == 0:
        return split_kosong()
    return normalisasi_split(bar["split"])


# ---------------------------------------------------------------------------
# Faktor split F(d)
# ---------------------------------------------------------------------------


def _tabel_faktor(split: pd.Series | None) -> tuple[np.ndarray, np.ndarray]:
    """(tanggal split dalam ns, hasil kali sufiks). `sufiks[j]` = Π rasio split ke-j s/d terakhir;
    `sufiks[k]` = 1. Dipakai bersama oleh versi skalar & tervektor agar hasilnya identik bit demi bit."""
    s = normalisasi_split(split)
    tanggal = s.index.as_unit("ns").asi8
    rasio = s.to_numpy(dtype="float64")
    sufiks = np.ones(len(rasio) + 1, dtype="float64")
    for j in range(len(rasio) - 1, -1, -1):
        sufiks[j] = sufiks[j + 1] * rasio[j]
    return tanggal, sufiks


def faktor_split(split: pd.Series | None, d: date | pd.Timestamp | str) -> float:
    """F(d) = Π rasio split bertanggal > d. Tanpa split sesudah d → 1.0."""
    tanggal, sufiks = _tabel_faktor(split)
    posisi = np.searchsorted(tanggal, ke_ts(d).as_unit("ns").value, side="right")
    return float(sufiks[posisi])


def seri_faktor(split: pd.Series | None, index: pd.DatetimeIndex) -> pd.Series:
    """F per tanggal `index` (tervektor). Nilainya identik dengan `faktor_split` per tanggal."""
    tanggal, sufiks = _tabel_faktor(split)
    idx = pd.DatetimeIndex(index)
    kunci = idx.normalize().as_unit("ns").asi8
    posisi = np.searchsorted(tanggal, kunci, side="right")
    return pd.Series(sufiks[posisi], index=index, name="faktor_split", dtype="float64")


# ---------------------------------------------------------------------------
# Konversi nominal ↔ tersesuaikan
# ---------------------------------------------------------------------------
# Menerima skalar, numpy, atau Series. Bila salah satu operand Decimal (uang, §4.1), hasilnya Decimal:
# operand float dikonversi lewat representasi desimal terpendeknya (5.0 → Decimal("5.0")).


def _ke_decimal(x: Any) -> Decimal:
    if isinstance(x, Decimal):
        return x
    if isinstance(x, (int, np.integer)) and not isinstance(x, bool):
        return Decimal(int(x))
    return Decimal(str(float(x)))


def _kali(a: Any, b: Any) -> Any:
    if isinstance(a, Decimal) or isinstance(b, Decimal):
        return _ke_decimal(a) * _ke_decimal(b)
    return a * b


def _bagi(a: Any, b: Any) -> Any:
    if isinstance(a, Decimal) or isinstance(b, Decimal):
        return _ke_decimal(a) / _ke_decimal(b)
    return a / b


def harga_nominal(harga_adj: Any, F: Any) -> Any:
    """Harga nominal pada tanggal d = harga tersesuaikan × F(d)."""
    return _kali(harga_adj, F)


def harga_adj(harga_nominal: Any, F: Any) -> Any:
    """Harga tersesuaikan = harga nominal ÷ F(d)."""
    return _bagi(harga_nominal, F)


def volume_nominal(vol_adj: Any, F: Any) -> Any:
    """Volume nominal = volume tersesuaikan ÷ F(d)."""
    return _bagi(vol_adj, F)


def lembar_adj(lembar_nominal: Any, F: Any) -> Any:
    """Lembar tersesuaikan = lembar nominal × F(d)."""
    return _kali(lembar_nominal, F)


def lembar_nominal(lembar_adj: Any, F: Any) -> Any:
    """Lembar nominal = lembar tersesuaikan ÷ F(d) (kebalikan `lembar_adj`)."""
    return _bagi(lembar_adj, F)


# ---------------------------------------------------------------------------
# Pemeriksaan split yang belum disesuaikan sumber (§8.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PemeriksaanSplit:
    tanggal_split: date
    rasio: float
    tanggal_bar: date | None  # bar valid pertama ≥ tanggal_split (tempat close_s diukur)
    tanggal_sebelum: date | None  # bar valid terakhir < tanggal_split (close_{s−1})
    q: float | None  # close_s / close_{s−1}; None bila salah satu bar tidak ada
    belum_disesuaikan: bool


def periksa_split(bar_valid: pd.DataFrame, split: pd.Series | None) -> list[PemeriksaanSplit]:
    """Rincian pemeriksaan setiap split tercatat (s, r) terhadap bar VALID.

    `q = close_s / close_{s−1}`: close_s dari bar valid pada s (bila tidak ada: bar valid pertama ≥ s),
    close_{s−1} dari bar valid terakhir < s. Seri dianggap belum disesuaikan bila
    `|ln q − ln(1/r)| < |ln q|` (q lebih dekat ke 1/r daripada ke 1). Split sebelum bar pertama atau
    sesudah bar terakhir tidak bisa diperiksa (q = None, tidak bermasalah).

    Catatan kausalitas: bukti untuk split s baru tersedia pada `tanggal_bar` (bisa > s bila tidak ada
    bar valid pada s). Penanda as-of sebaiknya memakai `tanggal_bar`.
    """
    s = normalisasi_split(split)
    if len(s) == 0:
        return []
    close = pd.to_numeric(bar_valid["close"], errors="coerce") if bar_valid is not None else pd.Series(dtype="float64")
    close = close[np.isfinite(close.to_numpy(dtype="float64")) & (close.to_numpy(dtype="float64") > 0)]
    idx = pd.DatetimeIndex(close.index)
    nilai = close.to_numpy(dtype="float64")
    hasil = []
    for ts, r in s.items():
        posisi = int(idx.searchsorted(ts, side="left"))
        if posisi >= len(idx) or posisi == 0:
            tgl_bar = idx[posisi].date() if posisi < len(idx) else None
            tgl_seb = idx[posisi - 1].date() if 0 < posisi <= len(idx) else None
            hasil.append(PemeriksaanSplit(ts.date(), float(r), tgl_bar, tgl_seb, None, False))
            continue
        q = nilai[posisi] / nilai[posisi - 1]
        ln_q = float(np.log(q))
        bermasalah = abs(ln_q - float(np.log(1.0 / r))) < abs(ln_q)
        hasil.append(
            PemeriksaanSplit(
                ts.date(), float(r), idx[posisi].date(), idx[posisi - 1].date(), float(q), bool(bermasalah)
            )
        )
    return hasil


def split_belum_disesuaikan(bar_valid: pd.DataFrame, split: pd.Series | None) -> list[date]:
    """Tanggal split s yang harganya tampak belum disesuaikan sumber (status SPLIT_BELUM_DISESUAIKAN)."""
    return [p.tanggal_split for p in periksa_split(bar_valid, split) if p.belum_disesuaikan]

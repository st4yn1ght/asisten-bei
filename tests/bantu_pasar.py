"""Pembantu uji: config dengan perubahan bertitik dan `DataPasar` sintetis kecil.

Semua data di sini fiktif dan deterministik; tidak ada jaringan dan tidak ada berkas pengguna.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from asisten_bei.config import Config, baca_yaml, validasi_dict
from asisten_bei.data.tampilan import DataPasar, DataSaham, bangun_data_saham
from asisten_bei.kalender import Kalender

AKAR_PROYEK = Path(__file__).resolve().parents[1]
_DASAR = baca_yaml(AKAR_PROYEK / "config.yaml")

KAL = Kalender({})  # Senin–Jumat (tanpa libur) untuk semua tahun
HARI = KAL.indeks_hari_bursa(date(2024, 1, 1), date(2026, 12, 31))


def cfg_uji(ubah: dict | None = None) -> Config:
    """Config rilis dengan perubahan `{"kondisi_pasar.min_saham_breadth": 2, ...}`."""
    data = copy.deepcopy(_DASAR)
    for kunci, nilai in (ubah or {}).items():
        *jalur, akhir = kunci.split(".")
        simpul = data
        for k in jalur:
            simpul = simpul[k]
        simpul[akhir] = nilai
    return validasi_dict(data)


def hari(pos: int) -> pd.Timestamp:
    return HARI[pos]


def tgl(pos: int) -> date:
    return HARI[pos].date()


def bar(
    close: Sequence[float],
    *,
    mulai: int = 0,
    volume: float | Sequence[float] = 1_000_000.0,
    buang: Sequence[int] = (),
    indeks: bool = False,
) -> pd.DataFrame:
    """Bar sederhana dari deret close pada hari bursa `HARI[mulai:]`. high = close × 1,01,
    low = close × 0,99, open = close. `buang`: posisi relatif yang tidak punya baris (bar hilang)."""
    n = len(close)
    c = np.asarray(close, dtype="float64")
    vol = np.full(n, np.nan) if indeks else np.broadcast_to(np.asarray(volume, dtype="float64"), (n,)).copy()
    df = pd.DataFrame(
        {"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": vol},
        index=pd.DatetimeIndex(HARI[mulai : mulai + n], name="tanggal").as_unit("ns"),
    )
    if buang:
        df = df.drop(df.index[list(buang)])
    df["sumber"] = "yfinance"
    return df


def naik(n: int, awal: float = 1000.0, langkah: float = 0.002) -> list[float]:
    """Deret naik geometris (close selalu di atas SMA-nya)."""
    return [awal * (1 + langkah) ** i for i in range(n)]


def turun(n: int, awal: float = 1000.0, langkah: float = 0.002) -> list[float]:
    return [awal * (1 - langkah) ** i for i in range(n)]


def data_saham(
    kode: str,
    mentah: pd.DataFrame,
    cfg: Config,
    akhir: date,
    *,
    indeks: bool = False,
    split: pd.Series | None = None,
    anomali_ok: set[tuple[str, date]] | None = None,
    **lain,
) -> DataSaham:
    return bangun_data_saham(
        kode, mentah, split, adalah_indeks=indeks, kalender=KAL, cfg=cfg,
        anomali_ok=anomali_ok or set(), akhir=akhir, **lain,
    )


def pasar(
    saham: dict[str, pd.DataFrame],
    ihsg: pd.DataFrame | None,
    cfg: Config,
    akhir: date,
    *,
    anomali_ok: set[tuple[str, date]] | None = None,
) -> DataPasar:
    ds = {k: data_saham(k, df, cfg, akhir, anomali_ok=anomali_ok) for k, df in saham.items()}
    ih = data_saham("IHSG", ihsg, cfg, akhir, indeks=True, anomali_ok=anomali_ok) if ihsg is not None else None
    return DataPasar(ds, ih, KAL, cfg)

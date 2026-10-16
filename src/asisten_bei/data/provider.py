"""Antarmuka penyedia data harga (SPEC §8.1).

Konvensi DataFrame bar (dipakai seluruh lapisan data):
- index: `pd.DatetimeIndex` tz-naive bernama "tanggal", tengah malam, = tanggal WIB; urut naik.
- kolom: open, high, low, close, volume (float64). Volume dalam lembar (sebelum `pengali_volume`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Protocol

import pandas as pd

StatusAmbil = Literal["OK", "SEBAGIAN", "GAGAL"]
JenisErrorAmbil = Literal["BATAS_PERMINTAAN", "TIDAK_ADA_DATA", "FORMAT", "JARINGAN"]

KOLOM_BAR: tuple[str, ...] = ("open", "high", "low", "close", "volume")
NAMA_INDEX = "tanggal"


class PriceProvider(Protocol):
    nama: str

    def simbol(self, kode: str) -> str:  # "ABCD" -> "ABCD.JK"; "IHSG" -> "^JKSE"
        ...

    def ambil_harian(self, kode: str, mulai: date, akhir: date) -> "HasilAmbil":  # akhir inklusif
        ...


@dataclass
class HasilAmbil:
    kode: str
    sumber: str
    status: StatusAmbil
    jenis_error: JenisErrorAmbil | None
    pesan: str  # teks error asli, setelah token rahasia disensor (§19.5)
    bar: pd.DataFrame | None  # index: tanggal WIB; kolom: open, high, low, close, volume
    split: pd.Series | None  # tanggal -> rasio (lembar baru per lembar lama)
    versi_provider: str = ""
    catatan: list[str] = field(default_factory=list)


def bar_kosong() -> pd.DataFrame:
    """DataFrame bar kosong dengan skema standar."""
    idx = pd.DatetimeIndex([], name=NAMA_INDEX)
    return pd.DataFrame({k: pd.Series(dtype="float64") for k in KOLOM_BAR}, index=idx)


def split_kosong() -> pd.Series:
    return pd.Series(dtype="float64", index=pd.DatetimeIndex([], name=NAMA_INDEX), name="split")


def ke_ts(d: date | pd.Timestamp | str) -> pd.Timestamp:
    """Tanggal → Timestamp tz-naive tengah malam (kunci index standar)."""
    return pd.Timestamp(d).normalize()


def gagal(kode: str, sumber: str, jenis: JenisErrorAmbil, pesan: str, versi: str = "") -> HasilAmbil:
    return HasilAmbil(
        kode=kode, sumber=sumber, status="GAGAL", jenis_error=jenis, pesan=pesan, bar=None,
        split=None, versi_provider=versi,
    )

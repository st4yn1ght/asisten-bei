"""Sumber tunggal "sekarang" (SPEC §22.1).

Semua kode membaca waktu lewat `sekarang()` dan menunggu lewat `tidur()`.
Jika env `ASISTEN_BEI_WAKTU_SEKARANG` (ISO, WIB) diset, waktu bersifat virtual:
`sekarang()` = nilai env + total detik yang "ditidurkan", dan `tidur()` tidak benar-benar
menunggu. Dengan begitu uji tunggu-data (§8.6) deterministik dan cepat.
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ZONA = ZoneInfo("Asia/Jakarta")
ENV_WAKTU = "ASISTEN_BEI_WAKTU_SEKARANG"

_offset_virtual = timedelta(0)


def _waktu_env() -> datetime | None:
    nilai = os.environ.get(ENV_WAKTU, "").strip()
    if not nilai:
        return None
    dt = datetime.fromisoformat(nilai)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZONA)
    return dt.astimezone(ZONA)


def mode_virtual() -> bool:
    return _waktu_env() is not None


def sekarang() -> datetime:
    """Waktu sekarang, timezone-aware di Asia/Jakarta."""
    env = _waktu_env()
    if env is not None:
        return env + _offset_virtual
    return datetime.now(ZONA)


def hari_ini() -> date:
    return sekarang().date()


def tidur(detik: float) -> None:
    """Menunggu `detik`. Dalam mode virtual hanya memajukan jam virtual."""
    global _offset_virtual
    if detik <= 0:
        return
    if mode_virtual():
        _offset_virtual += timedelta(seconds=detik)
    else:
        time.sleep(detik)


def reset_virtual() -> None:
    """Mengembalikan jam virtual ke nilai env (dipakai uji)."""
    global _offset_virtual
    _offset_virtual = timedelta(0)


def ke_wib(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZONA)
    return dt.astimezone(ZONA)

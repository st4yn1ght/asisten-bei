"""Format angka untuk teks yang dibaca pengguna (gaya Indonesia, §4.1).

Titik sebagai pemisah ribuan, koma sebagai desimal, pembulatan tampilan ROUND_HALF_UP. Hanya untuk
tampilan; perhitungan selalu memakai presisi penuh.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal

STRIP = "—"  # nilai tidak tersedia


def _ke_decimal(x: Decimal | float | int) -> Decimal | None:
    if isinstance(x, Decimal):
        return x if x.is_finite() else None
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return Decimal(str(x)) if isinstance(x, float) else Decimal(x)


def angka(x: Decimal | float | int | None, desimal: int = 2) -> str:
    """`angka(7123.456)` → "7.123,46"; `angka(5, 0)` → "5"; None/NaN → "—"."""
    if x is None:
        return STRIP
    d = _ke_decimal(x)
    if d is None:
        return STRIP
    if desimal <= 0:
        q = d.quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return f"{int(q):,}".replace(",", ".")
    q = d.quantize(Decimal(1).scaleb(-desimal), rounding=ROUND_HALF_UP)
    bulat, _, pecahan = f"{q:,.{desimal}f}".partition(".")
    return bulat.replace(",", ".") + "," + pecahan


def persen(x: Decimal | float | int | None, desimal: int = 1) -> str:
    """`persen(52.345)` → "52,3%". Angka bulat tanpa desimal bila `x` bilangan bulat Decimal/int."""
    if x is None:
        return STRIP
    d = _ke_decimal(x)
    if d is None:
        return STRIP
    if d == d.to_integral_value() and not isinstance(x, float):
        return angka(d, 0) + "%"
    return angka(d, desimal) + "%"


def rupiah(x: Decimal | float | int) -> str:
    """Tampilan Rupiah (§4.1): bulat tanpa desimal, selain itu 2 desimal ROUND_HALF_UP."""
    d = _ke_decimal(x)
    if d is None:
        return "Rp " + STRIP
    if d == d.to_integral_value():
        return "Rp " + angka(d, 0)
    return "Rp " + angka(d, 2)

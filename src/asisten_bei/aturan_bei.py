"""Aturan BEI dari config: fraksi harga dan ARA/ARB (SPEC §4.1, §7 `pasar`, §15.1 langkah 1, §16.2).

**Tidak ada nilai aturan BEI di kode ini** (P4). Semua tabel diisi pengguna di `config.yaml` dari
idx.co.id. Selama belum diverifikasi, atau untuk tanggal sebelum `berlaku_mulai`:

- fraksi: harga hanya dibulatkan ke **rupiah bulat** dengan arah yang sama (laporan meminta pengguna
  membulatkan sendiri ke fraksi valid);
- ARA/ARB: tidak dimodelkan (`batas_ara`/`batas_arb` = None, bar tidak pernah dianggap terkunci).

Arah pembulatan selalu menjaga risiko ≤ anggaran (§4.1): `harga_maks` dan target ke **bawah**,
stop dan trailing ke **atas**.

Konvensi tabel (divalidasi `config.py`): baris bersambung, `harga_min` inklusif, `harga_maks` eksklusif,
baris terakhir `harga_maks` null. Harga valid = kelipatan fraksi rentang tempat harga itu berada
(diasumsikan batas rentang di tabel resmi juga kelipatan fraksinya). Harga di bawah `harga_min` baris
pertama memakai baris pertama.

Harga masuk sebagai float (indikator) dibersihkan dulu dari derau float dengan `ke_desimal_harga`
(dibulatkan ke 6 desimal) agar 2.029,9999999998 tidak dibulatkan ke bawah menjadi 2.029.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .config import BarisBatas, BarisFraksi, Config

_PRESISI = Decimal("0.000001")
_SATU = Decimal(1)
_MAKS_LANGKAH = 64


def ke_desimal_harga(x: Decimal | float | int) -> Decimal:
    """Harga sebagai Decimal tanpa derau float (6 desimal, ROUND_HALF_EVEN)."""
    if isinstance(x, Decimal):
        d = x
    elif isinstance(x, float):
        if x != x or x in (float("inf"), float("-inf")):
            raise ValueError(f"harga tidak hingga/NaN: {x!r}")
        d = Decimal(repr(x))
    else:
        d = Decimal(x)
    return d.quantize(_PRESISI, rounding=ROUND_HALF_EVEN)


def _kelipatan(x: Decimal, f: Decimal, arah: str) -> Decimal:
    return (x / f).to_integral_value(rounding=arah) * f


@dataclass(frozen=True)
class HargaBulat:
    nilai: Decimal
    fraksi_dipakai: bool  # False = hanya rupiah bulat (fraksi belum diverifikasi / belum berlaku)


class AturanBEI:
    """Akses ke aturan fraksi dan ARA/ARB untuk satu config."""

    def __init__(self, cfg: "Config"):
        self._fraksi = cfg.pasar.fraksi_harga
        self._ar = cfg.pasar.auto_reject

    # -- fraksi -------------------------------------------------------------------

    def fraksi_berlaku(self, tanggal: date) -> bool:
        f = self._fraksi
        return bool(f.terverifikasi and f.tabel and f.berlaku_mulai is not None and tanggal >= f.berlaku_mulai)

    def _baris_fraksi(self, harga: Decimal) -> "BarisFraksi":
        tabel = self._fraksi.tabel
        for b in tabel:
            if b.harga_maks is None or harga < b.harga_maks:
                return b if harga >= b.harga_min else tabel[0]
        return tabel[-1]

    def fraksi(self, harga: Decimal | float, tanggal: date) -> Decimal | None:
        """Fraksi yang berlaku untuk `harga` pada `tanggal`, atau None bila fraksi tidak berlaku."""
        if not self.fraksi_berlaku(tanggal):
            return None
        return Decimal(self._baris_fraksi(ke_desimal_harga(harga)).fraksi)

    def harga_valid(self, harga: Decimal | float, tanggal: date) -> bool:
        """Harga adalah kelipatan fraksi rentangnya (atau rupiah bulat bila fraksi tidak berlaku)."""
        h = ke_desimal_harga(harga)
        f = self.fraksi(h, tanggal)
        if f is None:
            return h == h.to_integral_value()
        return h % f == 0

    def _bulat(self, harga: Decimal | float, tanggal: date, arah: str) -> HargaBulat:
        h = ke_desimal_harga(harga)
        if h <= 0:
            raise ValueError(f"harga harus > 0 untuk dibulatkan (nilai: {h})")
        if not self.fraksi_berlaku(tanggal):
            return HargaBulat(_kelipatan(h, _SATU, arah), False)
        r = h
        for _ in range(_MAKS_LANGKAH):
            r = _kelipatan(r, Decimal(self._baris_fraksi(r).fraksi), arah)
            if r > 0 and self.harga_valid(r, tanggal):
                return HargaBulat(r, True)
            if r <= 0:  # di bawah harga terkecil: naik ke kelipatan pertama baris pertama
                r = Decimal(self._fraksi.tabel[0].fraksi)
                arah = ROUND_CEILING
        raise ValueError(f"tabel fraksi tidak menghasilkan harga valid di sekitar {h}")

    def bulat_bawah(self, harga: Decimal | float, tanggal: date) -> HargaBulat:
        """Ke bawah (untuk harga_maks dan target)."""
        return self._bulat(harga, tanggal, ROUND_FLOOR)

    def bulat_atas(self, harga: Decimal | float, tanggal: date) -> HargaBulat:
        """Ke atas, lebih dekat ke harga (untuk stop dan trailing)."""
        return self._bulat(harga, tanggal, ROUND_CEILING)

    # -- ARA/ARB ----------------------------------------------------------------------

    def auto_reject_berlaku(self, tanggal: date) -> bool:
        a = self._ar
        return bool(
            a.terverifikasi and a.ara and a.arb and a.berlaku_mulai is not None and tanggal >= a.berlaku_mulai
            and a.harga_acuan == "close_kemarin"
        )

    @staticmethod
    def _baris_batas(tabel: "list[BarisBatas]", acuan: Decimal) -> "BarisBatas":
        for b in tabel:
            if b.harga_maks is None or acuan < b.harga_maks:
                return b if acuan >= b.harga_min else tabel[0]
        return tabel[-1]

    def batas_ara(self, harga_acuan: Decimal | float, tanggal: date) -> Decimal | None:
        """Harga tertinggi yang diizinkan (acuan × (1 + batas%), dibulatkan ke bawah). None bila ARA
        tidak dimodelkan pada `tanggal`."""
        if not self.auto_reject_berlaku(tanggal):
            return None
        acuan = ke_desimal_harga(harga_acuan)
        pct = Decimal(self._baris_batas(self._ar.ara, acuan).batas_pct)
        return self.bulat_bawah(acuan * (_SATU + pct / 100), tanggal).nilai

    def batas_arb(self, harga_acuan: Decimal | float, tanggal: date) -> Decimal | None:
        """Harga terendah yang diizinkan (acuan × (1 − batas%), dibulatkan ke atas). None bila ARB
        tidak dimodelkan pada `tanggal`."""
        if not self.auto_reject_berlaku(tanggal):
            return None
        acuan = ke_desimal_harga(harga_acuan)
        pct = Decimal(self._baris_batas(self._ar.arb, acuan).batas_pct)
        bawah = acuan * (_SATU - pct / 100)
        if bawah <= 0:
            return Decimal(0)
        return self.bulat_atas(bawah, tanggal).nilai

    @staticmethod
    def _datar(o: float, h: float, l: float, c: float) -> bool:
        return o == h == l == c

    def terkunci_arb(self, o: float, h: float, l: float, c: float, harga_acuan: float, tanggal: date) -> bool:
        """Bar terkunci di ARB: open = high = low = close dan close ≤ batas ARB (§15.1 langkah 1).
        Harga dalam ruang nominal."""
        batas = self.batas_arb(harga_acuan, tanggal)
        return batas is not None and self._datar(o, h, l, c) and ke_desimal_harga(c) <= batas

    def terkunci_ara(self, o: float, h: float, l: float, c: float, harga_acuan: float, tanggal: date) -> bool:
        """Bar terkunci di ARA: open = high = low = close dan close ≥ batas ARA (§16.2 BATAL_ARA)."""
        batas = self.batas_ara(harga_acuan, tanggal)
        return batas is not None and self._datar(o, h, l, c) and ke_desimal_harga(c) >= batas

    # -- peringatan untuk laporan -------------------------------------------------------

    def peringatan(self, tanggal: date) -> list[str]:
        """Item aturan BEI yang belum berlaku pada `tanggal` (ditampilkan di laporan §18.1 butir 11)."""
        hasil = []
        if not self.fraksi_berlaku(tanggal):
            hasil.append(
                "Fraksi harga belum diverifikasi: harga hanya dibulatkan ke rupiah. Saat memasang order, "
                "bulatkan harga limit beli dan target ke BAWAH, stop ke ATAS, ke fraksi valid (cek idx.co.id)."
            )
        if not self.auto_reject_berlaku(tanggal):
            hasil.append("Batas ARA/ARB belum diverifikasi: tidak dimodelkan di backtest/jurnal (cek idx.co.id).")
        return hasil

"""Aturan BEI dari config: fraksi harga dan ARA/ARB (SPEC §4.1, §7 `pasar`, §15.1 langkah 1, §16.2).

**Tidak ada nilai aturan BEI di kode ini** (P4). Semua tabel diisi pengguna di `config.yaml` dari
idx.co.id. Selama belum diverifikasi, atau untuk tanggal sebelum `berlaku_mulai`:

- fraksi: harga hanya dibulatkan ke **rupiah bulat** dengan arah yang sama (laporan meminta pengguna
  membulatkan sendiri ke fraksi valid);
- ARA/ARB: tidak dimodelkan (`batas_ara`/`batas_arb` = None, bar tidak pernah dianggap terkunci).

Arah pembulatan selalu menjaga risiko ≤ anggaran (§4.1): `harga_maks` dan target ke **bawah**,
stop dan trailing ke **atas**.

Konvensi tabel (divalidasi `config.py`): baris bersambung, `harga_min` inklusif, `harga_maks` eksklusif,
baris terakhir `harga_maks` null. Harga valid = kelipatan fraksi rentang tempat harga itu berada. Harga
di bawah `harga_min` baris pertama memakai baris pertama. Pembulatan memberi harga valid **terdekat**
pada arahnya, melintasi batas rentang bila perlu — juga untuk tabel yang batas rentangnya bukan
kelipatan fraksi.

Harga masuk sebagai float (indikator) dibersihkan dulu dari derau float dengan `ke_desimal_harga`
(dibulatkan ke 6 desimal) agar 2.029,9999999998 tidak dibulatkan ke bawah menjadi 2.029.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from typing import TYPE_CHECKING

from .biaya import ke_decimal

if TYPE_CHECKING:  # pragma: no cover
    from .config import BarisBatas, BarisFraksi, Config

_PRESISI = Decimal("0.000001")
_SATU = Decimal(1)
_NOL = Decimal(0)


def ke_desimal_harga(x: Decimal | float | int) -> Decimal:
    """Harga sebagai Decimal tanpa derau float (6 desimal, ROUND_HALF_EVEN). Menerima juga skalar
    numpy/pandas (`biaya.ke_decimal`); NaN/tak hingga → ValueError."""
    return ke_decimal(x).quantize(_PRESISI, rounding=ROUND_HALF_EVEN)


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

    def _indeks_baris(self, harga: Decimal) -> int:
        """Indeks baris rentang `harga` (tabel bersambung; di bawah baris pertama → baris pertama)."""
        tabel = self._fraksi.tabel
        for i, b in enumerate(tabel):
            if b.harga_maks is None or harga < b.harga_maks:
                return i
        return len(tabel) - 1

    def _baris_fraksi(self, harga: Decimal) -> "BarisFraksi":
        return self._fraksi.tabel[self._indeks_baris(harga)]

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

    def _valid_bawah(self, h: Decimal) -> Decimal:
        """Harga valid terbesar ≤ h (0 bila tidak ada harga valid > 0 di bawah h)."""
        tabel = self._fraksi.tabel
        i = self._indeks_baris(h)
        for j in range(i, -1, -1):
            b = tabel[j]
            f = Decimal(b.fraksi)
            if j == i:
                c = _kelipatan(h, f, ROUND_FLOOR)
            else:  # kelipatan terbesar yang < harga_maks (eksklusif) baris j
                c = _kelipatan(Decimal(b.harga_maks), f, ROUND_CEILING) - f
            bawah = Decimal(b.harga_min) if j > 0 else _NOL
            if c >= bawah and c > 0:
                return c
        return _NOL

    def _valid_atas(self, h: Decimal) -> Decimal:
        """Harga valid terkecil ≥ h."""
        tabel = self._fraksi.tabel
        i = self._indeks_baris(h)
        for j in range(i, len(tabel)):
            b = tabel[j]
            c = _kelipatan(h if j == i else Decimal(b.harga_min), Decimal(b.fraksi), ROUND_CEILING)
            if b.harga_maks is None or c < Decimal(b.harga_maks):
                return c
        raise AssertionError("baris terakhir tabel fraksi wajib tanpa batas atas")  # pragma: no cover

    def _bulat(self, harga: Decimal | float, tanggal: date, arah: str) -> HargaBulat:
        h = ke_desimal_harga(harga)
        if h <= 0:
            raise ValueError(f"harga harus > 0 untuk dibulatkan (nilai: {h})")
        if not self.fraksi_berlaku(tanggal):
            return HargaBulat(_kelipatan(h, _SATU, arah), False)
        return HargaBulat(self._valid_bawah(h) if arah == ROUND_FLOOR else self._valid_atas(h), True)

    def bulat_bawah(self, harga: Decimal | float, tanggal: date) -> HargaBulat:
        """Ke bawah (untuk harga_maks dan target). 0 bila tidak ada harga valid > 0 ≤ harga (pemanggil
        memperlakukannya sebagai level tidak sah, mis. cek STOP gagal)."""
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

    @staticmethod
    def _selisih(baris: "BarisBatas", acuan: Decimal) -> Decimal:
        """Jarak batas dari harga acuan (§7): `acuan × batas/100` (pct) atau `batas` rupiah (abs)."""
        batas = Decimal(baris.batas)
        return acuan * batas / 100 if baris.jenis == "pct" else batas

    def batas_ara(self, harga_acuan: Decimal | float, tanggal: date) -> Decimal | None:
        """Harga tertinggi yang diizinkan (acuan + selisih baris ARA, dibulatkan ke bawah). None bila ARA
        tidak dimodelkan pada `tanggal`."""
        if not self.auto_reject_berlaku(tanggal):
            return None
        acuan = ke_desimal_harga(harga_acuan)
        atas = acuan + self._selisih(self._baris_batas(self._ar.ara, acuan), acuan)
        return self.bulat_bawah(atas, tanggal).nilai

    def batas_arb(self, harga_acuan: Decimal | float, tanggal: date) -> Decimal | None:
        """Harga terendah yang diizinkan (acuan − selisih baris ARB, dibulatkan ke atas; ≤ 0 → 0). None
        bila ARB tidak dimodelkan pada `tanggal`."""
        if not self.auto_reject_berlaku(tanggal):
            return None
        acuan = ke_desimal_harga(harga_acuan)
        bawah = acuan - self._selisih(self._baris_batas(self._ar.arb, acuan), acuan)
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
        """Item aturan BEI yang belum berlaku pada `tanggal` (ditampilkan di laporan §18.1 butir 11).
        Aturan yang sudah diverifikasi tetapi `berlaku_mulai`-nya belum tiba disebut demikian, bukan
        "belum diverifikasi"."""
        hasil = []
        f, ar = self._fraksi, self._ar
        if not self.fraksi_berlaku(tanggal):
            if f.terverifikasi and f.berlaku_mulai is not None and tanggal < f.berlaku_mulai:
                awal = f"Fraksi harga terverifikasi, tetapi baru berlaku mulai {f.berlaku_mulai.isoformat()}"
            else:
                awal = "Fraksi harga belum diverifikasi"
            hasil.append(
                f"{awal}: harga hanya dibulatkan ke rupiah. Saat memasang order, bulatkan harga limit beli dan "
                "target ke BAWAH, stop ke ATAS, ke fraksi valid (cek idx.co.id)."
            )
        if not self.auto_reject_berlaku(tanggal):
            if (
                ar.terverifikasi and ar.berlaku_mulai is not None and tanggal < ar.berlaku_mulai
                and ar.harga_acuan == "close_kemarin"
            ):
                awal = f"Batas ARA/ARB terverifikasi, tetapi baru berlaku mulai {ar.berlaku_mulai.isoformat()}"
            else:
                awal = "Batas ARA/ARB belum diverifikasi"
            hasil.append(f"{awal}: tidak dimodelkan di backtest/jurnal (cek idx.co.id).")
        return hasil

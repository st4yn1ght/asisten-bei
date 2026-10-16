"""Ukuran posisi (SPEC §14.1).

Semua masukan harga adalah **harga nominal** yang sudah dibulatkan (`harga_maks` ke bawah, `stop` ke
atas); semua hitungan memakai `Decimal`. Dengan L = `pasar.lot_lembar`, P = harga_maks, S = stop:

    biaya_beli(n) = n·L·P + fee_beli(n·L·P)              (fee = max(pct × nilai, minimum), lalu pembulatan)
    hasil_stop(n) = n·L·S(1−slip) − fee_jual(n·L·S(1−slip))
    risiko(n)     = biaya_beli(n) − hasil_stop(n)

    lot_risiko  = n terbesar dengan risiko(n) ≤ anggaran_risiko
    lot_alokasi = n terbesar dengan biaya_beli(n) ≤ alokasi_maks
    lot_kas     = n terbesar dengan biaya_beli(n) ≤ kas_tersedia
    lot_likuid  = floor(maks_pct_order_dari_nilai_harian/100 × RataNilai / (P × L))
    lot         = min(semuanya)

`risiko(n)` dan `biaya_beli(n)` naik monoton terhadap n (selisih antar-n ≥ L·(P − S(1−slip)) > 0,
ditambah fee yang tidak turun), sehingga n terbesar dicari dengan pencarian biner di atas batas atas
tanpa fee — tepat juga saat ada fee minimum atau pembulatan fee.

Jika `risiko.izinkan_satu_lot_melebihi_risiko` dan lot_risiko = 0, lot_risiko = 1 dan hasil diberi
tanda `risiko_di_atas_anggaran` (§13.2). Batas alokasi, likuiditas, dan kas tetap berlaku.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING, Literal

from . import biaya

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config

Pembatas = Literal["risiko", "alokasi", "likuid", "kas"]


def _d(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    if isinstance(x, float):
        return Decimal(repr(x))
    return Decimal(x)


def _floor(x: Decimal) -> int:
    return int(x.to_integral_value(rounding=ROUND_FLOOR))


def n_terbesar(memenuhi: Callable[[int], bool], batas_atas: int) -> int:
    """n terbesar dalam [0, batas_atas] dengan `memenuhi(n)` benar, untuk predikat monoton turun
    (benar untuk n kecil, salah sesudah titik tertentu). `memenuhi(0)` dianggap benar."""
    lo, hi = 0, max(int(batas_atas), 0)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if memenuhi(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


@dataclass(frozen=True)
class HasilSizing:
    lot: int
    lembar: int
    lot_risiko: int
    lot_alokasi: int
    lot_kas: int
    lot_likuid: int
    pembatas: Pembatas | None  # batas yang menentukan `lot` (urutan cek: risiko, alokasi, likuid, kas); None bila lot ≥ 1 tak terbatas oleh satu pun yang 0
    harga_maks: Decimal
    stop: Decimal
    risiko_lembar: Decimal  # tanpa fee minimum
    anggaran_risiko: Decimal
    alokasi_maks: Decimal
    kas_tersedia: Decimal
    risiko_1_lot: Decimal  # risiko(1) termasuk fee minimum
    biaya_1_lot: Decimal  # biaya_beli(1)
    nilai_idr: Decimal  # lembar × harga_maks
    biaya_beli_idr: Decimal  # biaya_beli(lot)
    fee_beli_idr: Decimal
    risiko_idr: Decimal  # risiko(lot)
    risiko_di_atas_anggaran: bool

    @property
    def sisa_anggaran_per_lembar(self) -> Decimal | None:
        """(anggaran − risiko) / lembar: ruang pembulatan manual yang tersisa (§14.3)."""
        if self.lembar == 0:
            return None
        return (self.anggaran_risiko - self.risiko_idr) / Decimal(self.lembar)


def hitung_sizing(
    harga_maks: Decimal,
    stop: Decimal,
    *,
    anggaran_risiko: Decimal,
    alokasi_maks: Decimal,
    kas_tersedia: Decimal,
    rata_nilai: Decimal | float,
    cfg: "Config",
) -> HasilSizing:
    """Lot untuk satu kandidat (§14.1). Lempar ValueError bila stop ≥ harga_maks atau harga ≤ 0."""
    P, S = _d(harga_maks), _d(stop)
    if P <= 0 or S <= 0:
        raise ValueError(f"harga_maks dan stop harus > 0 (P={P}, S={S})")
    s_slip = biaya.slip(cfg)
    if not P > S * (1 - s_slip):
        raise ValueError(f"stop harus di bawah harga_maks (P={P}, S={S})")
    L = int(cfg.pasar.lot_lembar)
    anggaran, alokasi, kas = _d(anggaran_risiko), _d(alokasi_maks), _d(kas_tersedia)

    def risiko(n: int) -> Decimal:
        return biaya.risiko_posisi(n * L, P, S, cfg)

    def beli(n: int) -> Decimal:
        return biaya.biaya_beli(n * L, P, cfg)

    # Batas atas tanpa fee: risiko(n) ≥ n·L·(P − S(1−slip)), biaya_beli(n) ≥ n·L·P.
    per_lot_tanpa_fee = Decimal(L) * (P - S * (1 - s_slip))
    atas_risiko = _floor(max(anggaran, Decimal(0)) / per_lot_tanpa_fee) + 1
    lot_risiko = n_terbesar(lambda n: risiko(n) <= anggaran, atas_risiko)
    lot_alokasi = n_terbesar(lambda n: beli(n) <= alokasi, _floor(max(alokasi, Decimal(0)) / (Decimal(L) * P)) + 1)
    lot_kas = n_terbesar(lambda n: beli(n) <= kas, _floor(max(kas, Decimal(0)) / (Decimal(L) * P)) + 1)
    pct_likuid = _d(cfg.likuiditas.maks_pct_order_dari_nilai_harian) / 100
    lot_likuid = max(_floor(pct_likuid * _d(rata_nilai) / (P * Decimal(L))), 0)

    di_atas = False
    if lot_risiko == 0 and cfg.risiko.izinkan_satu_lot_melebihi_risiko:
        lot_risiko, di_atas = 1, True

    lot = min(lot_risiko, lot_alokasi, lot_likuid, lot_kas)
    pembatas: Pembatas | None = None
    for nama, nilai in (("risiko", lot_risiko), ("alokasi", lot_alokasi), ("likuid", lot_likuid), ("kas", lot_kas)):
        if nilai == lot:
            pembatas = nama  # type: ignore[assignment]
            break

    lembar = lot * L
    nilai_idr = Decimal(lembar) * P
    return HasilSizing(
        lot=lot,
        lembar=lembar,
        lot_risiko=lot_risiko,
        lot_alokasi=lot_alokasi,
        lot_kas=lot_kas,
        lot_likuid=lot_likuid,
        pembatas=pembatas,
        harga_maks=P,
        stop=S,
        risiko_lembar=biaya.risiko_lembar(P, S, cfg),
        anggaran_risiko=anggaran,
        alokasi_maks=alokasi,
        kas_tersedia=kas,
        risiko_1_lot=risiko(1),
        biaya_1_lot=beli(1),
        nilai_idr=nilai_idr,
        biaya_beli_idr=beli(lot) if lot else Decimal(0),
        fee_beli_idr=biaya.fee_beli(nilai_idr, cfg) if lot else Decimal(0),
        risiko_idr=risiko(lot) if lot else Decimal(0),
        risiko_di_atas_anggaran=di_atas,
    )

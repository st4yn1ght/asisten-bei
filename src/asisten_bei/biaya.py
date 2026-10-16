"""Fee dan slippage (SPEC §4.1, §7 `biaya`, §14.1, §15.1).

Semua uang memakai `decimal.Decimal`. Fee per transaksi = `max(nilai × pct, fee_minimum_idr)`, lalu
dibulatkan menurut `biaya.pembulatan_fee` (null = tidak dibulatkan; "rupiah_terdekat" = ROUND_HALF_UP
ke rupiah bulat; "rupiah_ke_atas" = selalu ke atas). Fee minimum berlaku **per transaksi** (sekali per
order beli atau jual), bukan per lot.

Slippage (`biaya.slippage_pct`, per sisi):
- beli: harga isi = min(open × (1 + slip), harga_maks) — order limit tidak pernah terisi di atas limit;
- jual karena stop/jual terjadwal/gap: harga × (1 − slip);
- jual target (limit): tanpa slippage.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config

SATU = Decimal(1)
SERATUS = Decimal(100)


def _d(x: Decimal | int | float | str) -> Decimal:
    if isinstance(x, Decimal):
        return x
    if isinstance(x, float):
        return Decimal(repr(x))
    return Decimal(x)


def fb(cfg: "Config") -> Decimal:
    """Fee beli sebagai pecahan (0,15% → 0,0015)."""
    return _d(cfg.biaya.fee_beli_pct) / SERATUS


def fj(cfg: "Config") -> Decimal:
    """Fee jual sebagai pecahan."""
    return _d(cfg.biaya.fee_jual_pct) / SERATUS


def slip(cfg: "Config") -> Decimal:
    """Slippage per sisi sebagai pecahan."""
    return _d(cfg.biaya.slippage_pct) / SERATUS


def bulatkan_fee(fee: Decimal, cfg: "Config") -> Decimal:
    mode = cfg.biaya.pembulatan_fee
    if mode is None:
        return fee
    if mode == "rupiah_terdekat":
        return fee.quantize(SATU, rounding=ROUND_HALF_UP)
    if mode == "rupiah_ke_atas":
        return fee.quantize(SATU, rounding=ROUND_CEILING)
    raise ValueError(f"pembulatan_fee tidak dikenal: {mode!r}")


def _fee(nilai: Decimal, pct: Decimal, cfg: "Config") -> Decimal:
    nilai = _d(nilai)
    if nilai < 0:
        raise ValueError(f"nilai transaksi negatif: {nilai}")
    if nilai == 0:
        return Decimal(0)
    return bulatkan_fee(max(nilai * pct, _d(cfg.biaya.fee_minimum_idr)), cfg)


def fee_beli(nilai: Decimal, cfg: "Config") -> Decimal:
    """Fee satu order beli bernilai `nilai` (Rupiah)."""
    return _fee(nilai, fb(cfg), cfg)


def fee_jual(nilai: Decimal, cfg: "Config") -> Decimal:
    """Fee satu order jual bernilai `nilai` (Rupiah), termasuk pajak penjualan bila sudah ada di pct."""
    return _fee(nilai, fj(cfg), cfg)


def biaya_beli(lembar: int, harga: Decimal, cfg: "Config") -> Decimal:
    """Kas keluar untuk membeli `lembar` lembar di `harga` (nilai + fee beli)."""
    nilai = Decimal(int(lembar)) * _d(harga)
    return nilai + fee_beli(nilai, cfg)


def hasil_jual(lembar: int, harga: Decimal, cfg: "Config") -> Decimal:
    """Kas masuk dari menjual `lembar` lembar di `harga` (nilai − fee jual). `harga` sudah termasuk
    slippage bila berlaku."""
    nilai = Decimal(int(lembar)) * _d(harga)
    return nilai - fee_jual(nilai, cfg)


def harga_isi_beli(open_: Decimal, harga_maks: Decimal, cfg: "Config") -> Decimal:
    """Harga isi order beli limit di `harga_maks` pada pembukaan: min(open × (1 + slip), harga_maks).
    Pemanggil wajib sudah memastikan open ≤ harga_maks (selain itu order tidak terisi: BATAL_GAP)."""
    return min(_d(open_) * (SATU + slip(cfg)), _d(harga_maks))


def harga_isi_jual(harga: Decimal, cfg: "Config", slippage: bool = True) -> Decimal:
    """Harga isi jual. `slippage=False` untuk order target (limit)."""
    harga = _d(harga)
    return harga * (SATU - slip(cfg)) if slippage else harga


def risiko_lembar(harga_maks: Decimal, stop: Decimal, cfg: "Config") -> Decimal:
    """`P(1 + fb) − S(1 − slip)(1 − fj)` per lembar, tanpa fee minimum (§4.1, §14.1)."""
    return _d(harga_maks) * (SATU + fb(cfg)) - _d(stop) * (SATU - slip(cfg)) * (SATU - fj(cfg))


def risiko_posisi(lembar: int, harga_maks: Decimal, stop: Decimal, cfg: "Config") -> Decimal:
    """`risiko(n)` §14.1 untuk `lembar` lembar: biaya beli di harga_maks − hasil jual di stop dengan
    slippage. Memperhitungkan fee minimum dan pembulatan fee."""
    harga_stop = harga_isi_jual(stop, cfg, slippage=True)
    return biaya_beli(lembar, harga_maks, cfg) - hasil_jual(lembar, harga_stop, cfg)


def pnl_bersih(lembar: int, harga_beli: Decimal, harga_jual: Decimal, cfg: "Config") -> Decimal:
    """Hasil jual setelah fee − biaya beli termasuk fee. Harga sudah termasuk slippage bila ada."""
    return hasil_jual(lembar, harga_jual, cfg) - biaya_beli(lembar, harga_beli, cfg)


def rr_bersih(target: Decimal, harga_maks: Decimal, stop: Decimal, cfg: "Config") -> Decimal:
    """Perkiraan R:R bersih (§14.2): (target × (1 − fj) − harga_maks × (1 + fb)) / risiko_lembar."""
    rl = risiko_lembar(harga_maks, stop, cfg)
    if rl <= 0:
        raise ValueError("risiko per lembar harus > 0 (stop harus di bawah harga_maks)")
    return (_d(target) * (SATU - fj(cfg)) - _d(harga_maks) * (SATU + fb(cfg))) / rl

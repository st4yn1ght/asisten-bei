"""Universe dan eligibilitas (SPEC §11).

Saham **eligible** pada t (live, jurnal kertas, backtest) jika lolos syarat 1–5 secara berurutan:

1. anggota universe pada t (live: `universe.csv`; backtest: `universe_history.csv` bila ada) →
   selain itu `BUKAN_KONSTITUEN`;
2. tidak dikecualikan `exclude.csv` pada t (backtest: hanya bila `backtest.pakai_exclude`) →
   `DIKECUALIKAN`;
3. lolos validasi data as-of t (§8.4–§8.5) → alasan data (`DATA_BASI`, `BAR_HILANG`, `ANOMALI`,
   `SPLIT_BELUM_DISESUAIKAN`, `RIWAYAT_PENDEK`, `RIWAYAT_CSV_PENDEK`); simbol tanpa data sama sekali →
   `GAGAL_AMBIL` (atau alasan tingkat seri);
4. `RataNilai_t ≥ likuiditas.nilai_transaksi_min_idr` (batas inklusif) → `TIDAK_LIKUID_RATA2`;
5. bar mentah bervolume 0 dalam `likuiditas.periode_hari` hari bursa terakhir ≤
   `maks_hari_volume_nol` → `KEMUNGKINAN_SUSPENSI`.

Sinyal dan skor hanya bergantung pada syarat 1–5, tidak pada isi portofolio; saham yang sedang
dipegang ditangani saat seleksi (§13.2, `SUDAH_DIPEGANG`). Semua data dibaca lewat `DataView` (P3).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

import pandas as pd

from . import teks

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config
    from .data.tampilan import DataView
    from .inputs import BarisExclude, Masukan

from .inputs import sektor_tidak_diketahui

KonteksUniverse = Literal["live", "backtest"]

# Alasan data dari `kualitas.alasan_data_asof` yang dinilai sebagai syarat 3 (sebelum likuiditas).
# KEMUNGKINAN_SUSPENSI adalah syarat 5, sehingga dinilai sesudah likuiditas (syarat 4).
_SUSPENSI = "KEMUNGKINAN_SUSPENSI"


@dataclass(frozen=True)
class AturanUniverse:
    """Siapa kandidat, siapa anggota/dikecualikan pada tanggal tertentu, dan sektornya."""

    konteks: KonteksUniverse
    kandidat: tuple[str, ...]
    anggota_pada: Callable[[str, date], bool]
    exclude_pada: Callable[[str, date], "BarisExclude | None"]
    sektor: Callable[[str], str]
    pakai_exclude: bool = True
    _catatan: tuple[str, ...] = field(default=())

    @classmethod
    def live(cls, masukan: "Masukan") -> "AturanUniverse":
        """Run harian: anggota = `universe.csv` saat ini; `exclude.csv` selalu diterapkan."""
        kode = tuple(dict.fromkeys(masukan.kode_universe()))
        anggota = set(kode)
        return cls(
            konteks="live",
            kandidat=kode,
            anggota_pada=lambda k, d: k in anggota,
            exclude_pada=masukan.dikecualikan_pada,
            sektor=masukan.sektor,
            pakai_exclude=True,
        )

    @classmethod
    def backtest(cls, masukan: "Masukan", cfg: "Config") -> "AturanUniverse":
        """Backtest: `universe_history.csv` menggantikan `universe.csv` bila ada (§6.3);
        `exclude.csv` hanya bila `backtest.pakai_exclude` (§6.4)."""
        if masukan.riwayat is not None:
            kode = tuple(sorted({r.kode for r in masukan.riwayat}))
            catatan: tuple[str, ...] = ("universe_history.csv dipakai",)
        else:
            kode = tuple(dict.fromkeys(masukan.kode_universe()))
            catatan = ("universe_history.csv tidak ada: universe saat ini dipakai untuk seluruh periode",)
        pakai = bool(cfg.backtest.pakai_exclude)
        # §6.3: sektor hanya dari universe.csv; selain itu TIDAK_DIKETAHUI:<KODE>. Kolom sektor
        # portfolio.csv (§6.5) khusus posisi live dan tidak boleh memengaruhi hasil backtest.
        peta_sektor = {s.kode: s.sektor for s in masukan.universe}
        return cls(
            konteks="backtest",
            kandidat=kode,
            anggota_pada=masukan.konstituen_pada,
            exclude_pada=masukan.dikecualikan_pada if pakai else (lambda k, d: None),
            sektor=lambda k: peta_sektor.get(k) or sektor_tidak_diketahui(k),
            pakai_exclude=pakai,
            _catatan=catatan,
        )

    @property
    def catatan(self) -> tuple[str, ...]:
        return self._catatan


@dataclass(frozen=True)
class StatusSaham:
    kode: str
    eligible: bool
    alasan: str | None  # enum alasan_dikeluarkan §4.3; None bila eligible
    rincian: str  # teks untuk bagian "Dikeluarkan" di laporan
    sektor: str
    rata_nilai: float | None


@dataclass(frozen=True)
class HasilEligibilitas:
    tanggal: date
    status: dict[str, StatusSaham]

    @property
    def eligible(self) -> list[str]:
        return [k for k, s in self.status.items() if s.eligible]

    @property
    def dikeluarkan(self) -> list[StatusSaham]:
        return [s for s in self.status.values() if not s.eligible]

    def alasan(self, kode: str) -> str | None:
        s = self.status.get(kode)
        return None if s is None else s.alasan


def _int(x: object, default: int = 0) -> int:
    try:
        v = int(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return v


def _rincian_data(dv: "DataView", kode: str, alasan: str) -> str:
    cfg = dv.cfg
    v = cfg.data.validasi
    baris = dv.tanda(kode)
    nilai = (lambda kolom, default=0: _int(baris.get(kolom, default))) if baris is not None else (
        lambda kolom, default=0: default
    )
    if alasan == "DATA_BASI":
        return f"tidak ada bar valid pada {dv.as_of.isoformat()}"
    if alasan == "BAR_HILANG":
        return (
            f"{nilai('bar_hilang')} bar hilang dalam {v.jendela_kelengkapan_bar} hari bursa terakhir"
            f" > {v.maks_bar_hilang}"
        )
    if alasan == "ANOMALI":
        a = baris.get("anomali_terakhir") if baris is not None else None
        tgl = pd.Timestamp(a).date().isoformat() if a is not None and not pd.isna(a) else "?"
        return f"anomali data sejak {tgl}; periksa lalu catat di anomali_ok.csv bila data benar"
    if alasan == "SPLIT_BELUM_DISESUAIKAN":
        return "split belum disesuaikan sumber data; diunduh ulang penuh pada run berikutnya"
    if alasan == "RIWAYAT_PENDEK":
        return f"{nilai('jumlah_bar_valid')} bar valid < {cfg.universe.min_bar_riwayat}"
    if alasan == "RIWAYAT_CSV_PENDEK":
        return "riwayat CSV manual terlalu pendek"
    if alasan == _SUSPENSI:
        return (
            f"{nilai('volume_nol')} hari bervolume 0 dalam {cfg.likuiditas.periode_hari} hari bursa"
            f" terakhir > {cfg.likuiditas.maks_hari_volume_nol}; kemungkinan suspensi — cek idx.co.id"
        )
    return alasan


def nilai_eligibilitas(dv: "DataView", aturan: AturanUniverse) -> HasilEligibilitas:
    """Status eligibilitas setiap kandidat pada `dv.as_of` (§11 syarat 1–5)."""
    d = dv.as_of
    cfg = dv.cfg
    ambang = Decimal(cfg.likuiditas.nilai_transaksi_min_idr)
    hasil: dict[str, StatusSaham] = {}
    for kode in aturan.kandidat:
        sektor = aturan.sektor(kode)

        def keluar(alasan: str, rincian: str, rata: float | None = None) -> None:
            hasil[kode] = StatusSaham(kode, False, alasan, rincian, sektor, rata)

        # 1. Konstituen.
        if not aturan.anggota_pada(kode, d):
            keluar("BUKAN_KONSTITUEN", f"bukan konstituen universe pada {d.isoformat()}")
            continue
        # 2. exclude.csv.
        ex = aturan.exclude_pada(kode, d)
        if ex is not None:
            keluar("DIKECUALIKAN", f"exclude.csv: {ex.alasan}")
            continue
        # 3. Data as-of.
        if not dv.ada(kode):
            keluar("GAGAL_AMBIL", "data tidak tersedia dari sumber mana pun")
            continue
        alasan_data = dv.alasan_data(kode)
        tampil = dv.seri(kode)
        if alasan_data is not None and alasan_data != _SUSPENSI:
            rincian = tampil.pesan if (tampil.alasan_seri and tampil.pesan) else _rincian_data(dv, kode, alasan_data)
            keluar(alasan_data, rincian)
            continue
        # 4. Likuiditas (batas inklusif).
        baris = dv.baris(kode)
        rata = None if baris is None else float(baris.get("rata_nilai", math.nan))
        if rata is None or math.isnan(rata) or rata < float(ambang):
            nilai = "belum terdefinisi" if rata is None or math.isnan(rata) else teks.rupiah(Decimal(str(rata)))
            keluar(
                "TIDAK_LIKUID_RATA2",
                f"rata-rata nilai transaksi {cfg.likuiditas.periode_hari} hari {nilai} < {teks.rupiah(ambang)}",
                None if rata is None or math.isnan(rata) else rata,
            )
            continue
        # 5. Volume nol (kemungkinan suspensi).
        if alasan_data == _SUSPENSI:
            keluar(_SUSPENSI, _rincian_data(dv, kode, _SUSPENSI), rata)
            continue
        hasil[kode] = StatusSaham(kode, True, None, "", sektor, rata)
    return HasilEligibilitas(d, hasil)


def kode_eligible(hasil: HasilEligibilitas) -> list[str]:
    """Kode eligible urut kode (dipakai K_rs §13.1 dan pembangkitan sinyal)."""
    return sorted(hasil.eligible)


def gabung_kandidat(*daftar: Iterable[str]) -> list[str]:
    """Gabungan unik dengan urutan stabil (mis. universe ∪ breadth ∪ portofolio untuk unduhan)."""
    return list(dict.fromkeys(k for xs in daftar for k in xs))

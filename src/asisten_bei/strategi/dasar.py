"""Dasar strategi: tipe sinyal, pembangun level harga, dan registri (SPEC §12, §13.1, §4.1, §8.4).

Kontrak setiap strategi (`Strategi.evaluasi`):

- Hanya membaca data lewat `DataView` (P3): `dv.baris(kode)` untuk bar t, `dv.sebelum(kode, kolom, k)`
  untuk `x_{t−k}` (bar valid ke-k sebelum t). Kolom indikator memakai nama dari `indikator.k_*` dengan
  periode dari config (P4) — tidak ada angka aturan yang ditulis langsung di kode.
- Mengembalikan `EvaluasiStrategi` berisi SEMUA cek aturan (lolos atau tidak) dengan nilai aktual, agar
  laporan bisa menulis "PB4: close 2.000 > high kemarin 1.985 ✓" dan uji bisa memeriksa alasan gagal.
- Nilai NaN, atau ATR ≤ 0/NaN pada aturan yang membagi dengan ATR, membuat aturan **tidak terpenuhi** (§9).
- Sinyal hanya dibuat bila semua aturan lolos; level harga dibangun lewat `Strategi._bangun_sinyal`.

Level harga (§4.1, §8.4): dihitung di ruang tersesuaikan (data cache), dikonversi ke nominal dengan
`F(t)` (`dv.faktor_split`), lalu dibulatkan: `harga_maks` ke bawah, `stop` ke atas (`AturanBEI`). Di run
harian F(t) = 1. `harga_maks = close_t + entry.gap_maks_atr × ATR_t`.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar

from .. import teks
from ..aturan_bei import AturanBEI, ke_desimal_harga

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config
    from ..data.tampilan import DataView

URUTAN_STRATEGI: tuple[str, ...] = ("PB", "BO", "MR", "ACAK")  # pemecah nilai sama §13.2 langkah 4


@dataclass(frozen=True)
class CekAturan:
    kode: str  # "PB1".."PB5", "BO1".., "MR1".., "ACAK", "STOP"
    uraian: str  # dengan nilai aktual, mis. "close 2.000 > high kemarin 1.985"
    lolos: bool

    @property
    def tanda(self) -> str:
        return "✓" if self.lolos else "✗"

    def teks(self) -> str:
        return f"{self.kode}: {self.uraian} {self.tanda}"


@dataclass(frozen=True)
class Sinyal:
    """Satu sinyal strategi pada close `tanggal` untuk order beli di pembukaan hari bursa berikutnya."""

    tanggal: date
    kode: str
    strategi: str  # "PB" | "BO" | "MR" | "ACAK"
    cek: tuple[CekAturan, ...]
    harga_ref: Decimal  # close_t nominal
    harga_maks: Decimal  # nominal, dibulatkan ke bawah
    stop: Decimal  # nominal, dibulatkan ke atas
    atr: Decimal  # ATR_t nominal
    faktor_split: float  # F(t) yang dipakai untuk konversi ke nominal
    k_sinyal: float  # §13.1, dalam [0, 1]
    target_r: float | None  # kelipatan R untuk target (PB, ACAK); None untuk BO/MR
    rata_nilai: float  # RataNilai_t (Rupiah, invarian split)
    fraksi_dipakai: bool  # False = harga hanya rupiah bulat → peringatan pembulatan manual
    valid: bool = True  # False → STOP_TERLALU_JAUH (§13.2 langkah 2)
    alasan_tidak_valid: str | None = None

    @property
    def jarak_stop_pct(self) -> float:
        """(harga_maks − stop)/harga_maks × 100, setelah pembulatan (§13.1 K_stop)."""
        return float((self.harga_maks - self.stop) / self.harga_maks * 100)

    @property
    def urutan_strategi(self) -> int:
        return URUTAN_STRATEGI.index(self.strategi)


@dataclass(frozen=True)
class EvaluasiStrategi:
    kode: str
    strategi: str
    cek: tuple[CekAturan, ...]
    sinyal: Sinyal | None

    @property
    def aturan_gagal(self) -> list[str]:
        return [c.kode for c in self.cek if not c.lolos]


def nilai_baris(baris, kolom: str) -> float:
    """Nilai float dari baris indikator; NaN bila baris None atau kolom tidak ada."""
    if baris is None or kolom not in baris.index:
        return math.nan
    return float(baris[kolom])


def atr_sah(atr: float) -> bool:
    """ATR dapat dipakai sebagai pembagi (§9: ATR ≤ 0 atau NaN → aturan tidak terpenuhi)."""
    return not math.isnan(atr) and atr > 0


def a(x: float, desimal: int = 2) -> str:
    """Format angka untuk uraian cek (gaya Indonesia; NaN → "—")."""
    return teks.angka(x, desimal)


def hitung_target(harga_isi: Decimal, stop: Decimal, target_r: float, bei: AturanBEI, tanggal: date) -> Decimal:
    """Target = harga isi + target_r × (harga isi − stop), dibulatkan ke bawah (§12.1, §4.1).
    Harga nominal. Lempar ValueError bila harga isi ≤ stop (tidak ada target)."""
    isi, s = ke_desimal_harga(harga_isi), ke_desimal_harga(stop)
    if isi <= s:
        raise ValueError(f"harga isi {isi} ≤ stop {s}: target tidak terdefinisi")
    r = Decimal(repr(float(target_r)))
    return bei.bulat_bawah(isi + r * (isi - s), tanggal).nilai


class Strategi(ABC):
    """Basis semua strategi. Subkelas menetapkan `kode` dan mengimplementasikan `evaluasi`."""

    kode: ClassVar[str]

    def __init__(self, cfg: "Config"):
        self.cfg = cfg

    @abstractmethod
    def evaluasi(self, dv: "DataView", kode: str, bei: AturanBEI) -> EvaluasiStrategi:
        """Evaluasi semua aturan strategi untuk `kode` pada `dv.as_of`."""

    # -- pembantu bersama -----------------------------------------------------------

    def tanpa_sinyal(self, kode: str, cek: Sequence[CekAturan]) -> EvaluasiStrategi:
        return EvaluasiStrategi(kode, self.kode, tuple(cek), None)

    def _bangun_sinyal(
        self,
        dv: "DataView",
        kode: str,
        cek: Sequence[CekAturan],
        *,
        close: float,
        atr: float,
        stop_tersesuaikan: float,
        k_sinyal: float,
        target_r: float | None,
        rata_nilai: float,
        maks_jarak_stop_atr: float | None = None,
    ) -> EvaluasiStrategi:
        """Bangun `Sinyal` dari nilai tersesuaikan (semua cek aturan sudah lolos).

        - harga_maks = bulat_bawah((close + gap_maks_atr × ATR) × F), stop = bulat_atas(stop × F).
        - Bila stop ≥ harga_maks setelah pembulatan (ATR sangat kecil), aturan "STOP" gagal → tanpa sinyal.
        - Bila `maks_jarak_stop_atr` diisi (PB): (harga_maks − stop)/ATR_nominal > batas → sinyal
          `valid=False` (`STOP_TERLALU_JAUH`), tetap dikembalikan agar dicatat di jurnal.
        """
        cek = list(cek)
        t = dv.as_of
        bei = AturanBEI(self.cfg)
        f = float(dv.faktor_split(kode))
        gap = float(self.cfg.entry.gap_maks_atr)
        maks = bei.bulat_bawah((close + gap * atr) * f, t)
        stop = bei.bulat_atas(stop_tersesuaikan * f, t)
        atr_nom = ke_desimal_harga(atr * f)
        if not stop.nilai < maks.nilai:
            cek.append(
                CekAturan("STOP", f"stop {a(float(stop.nilai))} < harga maks {a(float(maks.nilai))}", False)
            )
            return self.tanpa_sinyal(kode, cek)
        valid, alasan = True, None
        if maks_jarak_stop_atr is not None:
            jarak_atr = (maks.nilai - stop.nilai) / atr_nom
            if jarak_atr > Decimal(repr(float(maks_jarak_stop_atr))):
                valid = False
                alasan = (
                    f"jarak stop {a(float(jarak_atr))} ATR > {a(float(maks_jarak_stop_atr))} ATR "
                    f"(harga maks {a(float(maks.nilai), 0)}, stop {a(float(stop.nilai), 0)})"
                )
        sinyal = Sinyal(
            tanggal=t,
            kode=kode,
            strategi=self.kode,
            cek=tuple(cek),
            harga_ref=ke_desimal_harga(close * f),
            harga_maks=maks.nilai,
            stop=stop.nilai,
            atr=atr_nom,
            faktor_split=f,
            k_sinyal=min(max(float(k_sinyal), 0.0), 1.0),
            target_r=None if target_r is None else float(target_r),
            rata_nilai=float(rata_nilai),
            fraksi_dipakai=maks.fraksi_dipakai and stop.fraksi_dipakai,
            valid=valid,
            alasan_tidak_valid=alasan,
        )
        return EvaluasiStrategi(kode, self.kode, tuple(cek), sinyal)


def daftar_strategi(cfg: "Config", termasuk_acak: bool = False, hanya: Iterable[str] | None = None) -> list[Strategi]:
    """Strategi aktif (urutan PB, BO, MR), ditambah ACAK bila diminta (backtest saja).
    `hanya`: batasi ke kode tertentu (mis. backtest per strategi); kode tak dikenal → ValueError."""
    from .acak import Acak
    from .breakout import Breakout
    from .oversold import Oversold
    from .pullback import Pullback

    semua: dict[str, Strategi] = {}
    s = cfg.strategi
    if s.pb.aktif:
        semua["PB"] = Pullback(cfg)
    if s.bo.aktif:
        semua["BO"] = Breakout(cfg)
    if s.mr.aktif:
        semua["MR"] = Oversold(cfg)
    if termasuk_acak:
        semua["ACAK"] = Acak(cfg)
    if hanya is not None:
        diminta = [k.upper() for k in hanya]
        tak_dikenal = [k for k in diminta if k not in URUTAN_STRATEGI]
        if tak_dikenal:
            raise ValueError(f"strategi tidak dikenal: {', '.join(tak_dikenal)}")
        return [semua[k] for k in URUTAN_STRATEGI if k in diminta and k in semua]
    return [semua[k] for k in URUTAN_STRATEGI if k in semua]


def evaluasi_semua(
    dv: "DataView", kode_eligible: Iterable[str], strategi: Sequence[Strategi], bei: AturanBEI | None = None
) -> list[EvaluasiStrategi]:
    """Evaluasi setiap strategi atas setiap kode eligible (urut kode, lalu urutan strategi)."""
    bei = bei or AturanBEI(dv.cfg)
    hasil = []
    for kode in sorted(set(kode_eligible)):
        for st in strategi:
            hasil.append(st.evaluasi(dv, kode, bei))
    return hasil


def hitung_sinyal(
    dv: "DataView", kode_eligible: Iterable[str], strategi: Sequence[Strategi], bei: AturanBEI | None = None
) -> list[Sinyal]:
    """Semua sinyal (valid maupun `STOP_TERLALU_JAUH`) pada `dv.as_of`, urut kode lalu strategi."""
    return [e.sinyal for e in evaluasi_semua(dv, kode_eligible, strategi, bei) if e.sinyal is not None]

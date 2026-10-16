"""Seleksi kandidat (SPEC §13.2) — identik di live, jurnal kertas, dan backtest.

Masukan: sinyal berskor pada t, status pasar t, keadaan portofolio (live: portfolio.csv/akun.yaml;
backtest/kertas: posisi simulasi), konteks risiko (`config.resolusi_konteks`), dan dua saklar:

- `strategi_lolos`: himpunan strategi yang lolos gerbang (§16.6). None = langkah gerbang dilewati
  (backtest per strategi).
- `deduplikasi`: False untuk backtest per strategi (hanya satu strategi).

Langkah:
1–2. Sinyal tidak valid → `STOP_TERLALU_JAUH`.
3.   Strategi tidak lolos gerbang → `BAYANGAN_GERBANG` (tidak ikut deduplikasi, tidak menggugurkan sinyal lain).
4.   Deduplikasi per saham: skor tertinggi; seri → urutan PB, BO, MR, ACAK. Sisanya `DUPLIKAT_STRATEGI`.
5.   Urutan: skor ↓, RataNilai ↓, kode ↑ (lalu urutan strategi).
6.   Saham yang dipegang → `SUDAH_DIPEGANG`. 6a (live): portofolio diblokir → `DIBLOKIR_PORTOFOLIO`, selesai.
7.   JANGAN BELI → `BAYANGAN_PASAR` (daftar pantau mengikuti urutan langkah 5), selesai.
8.   Slot = maks_posisi − (posisi terbuka − jual terjadwal bila `slot_bebas_dari_jual_terjadwal`);
     kas tersedia = kas (+ estimasi hasil jual terjadwal bila `dana_jual_langsung_dipakai`).
9.   Per kandidat, status pertama yang berlaku: SLOT_PENUH, BATAS_ENTRY_HARIAN, BATAS_SEKTOR,
     TIDAK_TERJANGKAU (risiko), TIDAK_TERJANGKAU (alokasi), TIDAK_LIKUID, KAS_KURANG, REKOMENDASI.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from . import teks
from .kondisi_pasar import JANGAN_BELI, TAMPILAN_STATUS, parameter_entry
from .sizing import HasilSizing, hitung_sizing
from .skor import SinyalBerskor
from .strategi.dasar import URUTAN_STRATEGI

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config, KonteksRisiko
    from .kondisi_pasar import StatusPasar

REKOMENDASI = "REKOMENDASI"
SUDAH_DIPEGANG = "SUDAH_DIPEGANG"
DIBLOKIR_PORTOFOLIO = "DIBLOKIR_PORTOFOLIO"
SLOT_PENUH = "SLOT_PENUH"
BATAS_ENTRY_HARIAN = "BATAS_ENTRY_HARIAN"
BATAS_SEKTOR = "BATAS_SEKTOR"
TIDAK_TERJANGKAU = "TIDAK_TERJANGKAU"
TIDAK_LIKUID = "TIDAK_LIKUID"
KAS_KURANG = "KAS_KURANG"
BAYANGAN_GERBANG = "BAYANGAN_GERBANG"
BAYANGAN_PASAR = "BAYANGAN_PASAR"
STOP_TERLALU_JAUH = "STOP_TERLALU_JAUH"
DUPLIKAT_STRATEGI = "DUPLIKAT_STRATEGI"


@dataclass(frozen=True)
class PosisiTerbuka:
    kode: str
    sektor: str
    dijadwalkan_jual: bool = False  # akan dijual di pembukaan t+1 (saran JUAL / jual terjadwal simulasi)


@dataclass(frozen=True)
class KeadaanPortofolio:
    posisi: tuple[PosisiTerbuka, ...]
    kas_idr: Decimal
    ekuitas_idr: Decimal
    estimasi_hasil_jual_terjadwal_idr: Decimal = Decimal(0)
    blokir: str | None = None  # §13.2 6a: alasan portofolio tidak dapat dievaluasi (hanya live)

    @property
    def kode_dipegang(self) -> frozenset[str]:
        return frozenset(p.kode for p in self.posisi)


@dataclass(frozen=True)
class KandidatSeleksi:
    sinyal: SinyalBerskor
    status: str  # enum status_sinyal §4.3
    alasan: str  # "" untuk REKOMENDASI
    sizing: HasilSizing | None = None
    bergantung_jual: tuple[str, ...] = ()  # jual terjadwal yang membebaskan slot/sektor kandidat ini

    @property
    def kode(self) -> str:
        return self.sinyal.kode

    @property
    def strategi(self) -> str:
        return self.sinyal.strategi


@dataclass(frozen=True)
class HasilSeleksi:
    tanggal: date
    status_pasar: str
    kandidat: tuple[KandidatSeleksi, ...]  # urutan langkah 5 untuk semua sinyal
    anggaran_risiko: Decimal
    alokasi_maks: Decimal
    slot_awal: int
    kas_awal: Decimal
    catatan: tuple[str, ...] = field(default=())

    @property
    def rekomendasi(self) -> list[KandidatSeleksi]:
        return [k for k in self.kandidat if k.status == REKOMENDASI]

    @property
    def daftar_pantau(self) -> list[KandidatSeleksi]:
        return [k for k in self.kandidat if k.status == BAYANGAN_PASAR]

    def status(self, kode: str, strategi: str) -> str | None:
        for k in self.kandidat:
            if k.kode == kode and k.strategi == strategi:
                return k.status
        return None


def kunci_urutan(s: SinyalBerskor) -> tuple:
    """Langkah 5: skor ↓, RataNilai ↓, kode ↑, lalu urutan strategi (presisi penuh)."""
    return (-s.skor, -s.sinyal.rata_nilai, s.kode, URUTAN_STRATEGI.index(s.strategi))


def _pct(bagian: Decimal, total: Decimal) -> str:
    if total <= 0:
        return teks.STRIP
    return teks.persen(bagian * 100 / total, 2)


def seleksi(
    sinyal: Sequence[SinyalBerskor],
    *,
    status_pasar: "StatusPasar",
    portofolio: KeadaanPortofolio,
    konteks: "KonteksRisiko",
    cfg: "Config",
    strategi_lolos: Collection[str] | None,
    deduplikasi: bool,
    sektor_dari: Callable[[str], str],
) -> HasilSeleksi:
    """Status §13.2 untuk setiap sinyal. `sektor_dari(kode)`: sektor kandidat (mis. `AturanUniverse.sektor`)."""
    tanggal = sinyal[0].sinyal.tanggal if sinyal else date.min
    status: dict[int, tuple[str, str]] = {}  # id(sinyal) → (status, alasan)
    sizing_per: dict[int, HasilSizing] = {}
    bergantung: dict[int, tuple[str, ...]] = {}

    # 1–2. Validasi strategi.
    antre: list[SinyalBerskor] = []
    for s in sinyal:
        if not s.sinyal.valid:
            status[id(s)] = (STOP_TERLALU_JAUH, s.sinyal.alasan_tidak_valid or "")
        else:
            antre.append(s)

    # 3. Gerbang.
    if strategi_lolos is not None:
        lolos = {x.upper() for x in strategi_lolos}
        sisa = []
        for s in antre:
            if s.strategi not in lolos:
                status[id(s)] = (BAYANGAN_GERBANG, f"strategi {s.strategi} belum lolos gerbang backtest")
            else:
                sisa.append(s)
        antre = sisa

    # 4. Deduplikasi.
    if deduplikasi:
        terbaik: dict[str, SinyalBerskor] = {}
        for s in antre:
            lama = terbaik.get(s.kode)
            if lama is None or (-s.skor, URUTAN_STRATEGI.index(s.strategi)) < (
                -lama.skor, URUTAN_STRATEGI.index(lama.strategi)
            ):
                terbaik[s.kode] = s
        sisa = []
        for s in antre:
            menang = terbaik[s.kode]
            if menang is s:
                sisa.append(s)
            else:
                status[id(s)] = (
                    DUPLIKAT_STRATEGI,
                    f"kalah dari sinyal {menang.strategi} untuk saham yang sama (skor {menang.komponen.tampilan})",
                )
        antre = sisa

    # 5. Urutkan.
    antre.sort(key=kunci_urutan)

    # 6. Sudah dipegang.
    dipegang = portofolio.kode_dipegang
    sisa = []
    for s in antre:
        if s.kode in dipegang:
            status[id(s)] = (SUDAH_DIPEGANG, "saham sedang dipegang; tidak ada penambahan posisi")
        else:
            sisa.append(s)
    antre = sisa

    pe = parameter_entry(status_pasar, cfg)
    ekuitas = Decimal(portofolio.ekuitas_idr)
    anggaran = ekuitas * Decimal(cfg.risiko.risiko_per_transaksi_pct) / 100 * pe.pengali_risiko
    alokasi = konteks.alokasi_maks(ekuitas) if ekuitas > 0 else Decimal(0)
    n_buka = len(portofolio.posisi)
    jual = [p for p in portofolio.posisi if p.dijadwalkan_jual]
    bebas = bool(cfg.risiko.slot_bebas_dari_jual_terjadwal)
    slot_tanpa_jual = konteks.maks_posisi - n_buka
    slot = konteks.maks_posisi - (n_buka - (len(jual) if bebas else 0))
    kas = Decimal(portofolio.kas_idr)
    if cfg.risiko.dana_jual_langsung_dipakai:
        kas += Decimal(portofolio.estimasi_hasil_jual_terjadwal_idr)
    kas_awal = kas

    def selesai(sisa_status: str, alasan: str) -> HasilSeleksi:
        for s in antre:
            status[id(s)] = (sisa_status, alasan)
        return _hasil()

    def _hasil() -> HasilSeleksi:
        semua = sorted(sinyal, key=kunci_urutan)
        kandidat = tuple(
            KandidatSeleksi(s, *status[id(s)], sizing_per.get(id(s)), bergantung.get(id(s), ()))
            for s in semua
        )
        return HasilSeleksi(tanggal, status_pasar, kandidat, anggaran, alokasi, max(slot, 0), kas_awal)

    # 6a. Portofolio diblokir (live).
    if portofolio.blokir:
        return selesai(DIBLOKIR_PORTOFOLIO, f"PORTOFOLIO TIDAK DAPAT DIEVALUASI: {portofolio.blokir}")

    # 7. JANGAN BELI.
    if status_pasar == JANGAN_BELI:
        return selesai(BAYANGAN_PASAR, "status pasar JANGAN BELI (daftar pantau)")

    # 8–9. Slot, kuota harian, sektor, sizing.
    sektor_buka = Counter(p.sektor for p in portofolio.posisi)
    sektor_jual: dict[str, list[str]] = {}
    for p in jual:
        sektor_jual.setdefault(p.sektor, []).append(p.kode)
    sektor_terpakai = Counter(sektor_buka)
    if bebas:
        for sek, kode in sektor_jual.items():
            sektor_terpakai[sek] -= len(kode)
    maks_sektor = konteks.maks_per_sektor
    diterima = 0
    label = TAMPILAN_STATUS.get(status_pasar, status_pasar)

    for s in antre:
        sg = s.sinyal
        sektor = sektor_dari(s.kode)
        if diterima >= slot:
            status[id(s)] = (SLOT_PENUH, f"slot posisi penuh ({konteks.maks_posisi} posisi)")
            continue
        if pe.maks_entry_baru_per_hari is not None and diterima >= pe.maks_entry_baru_per_hari:
            status[id(s)] = (
                BATAS_ENTRY_HARIAN,
                f"batas entry baru per hari tercapai ({pe.maks_entry_baru_per_hari}, status {label})",
            )
            continue
        if maks_sektor is not None and sektor_terpakai[sektor] >= maks_sektor:
            status[id(s)] = (BATAS_SEKTOR, f"sektor {sektor} sudah mencapai {maks_sektor} posisi")
            continue
        hs = hitung_sizing(
            sg.harga_maks, sg.stop, anggaran_risiko=anggaran, alokasi_maks=alokasi, kas_tersedia=kas,
            rata_nilai=sg.rata_nilai, cfg=cfg,
        )
        sizing_per[id(s)] = hs
        if hs.lot_risiko < 1:
            status[id(s)] = (
                TIDAK_TERJANGKAU,
                f"risiko 1 lot = {teks.rupiah(hs.risiko_1_lot)} ({_pct(hs.risiko_1_lot, ekuitas)} ekuitas)"
                f" > anggaran {teks.rupiah(anggaran)} ({_pct(anggaran, ekuitas)}, status {label})",
            )
            continue
        if hs.lot_alokasi < 1:
            status[id(s)] = (
                TIDAK_TERJANGKAU,
                f"nilai 1 lot {teks.rupiah(hs.biaya_1_lot)} > alokasi maks {teks.rupiah(alokasi)}",
            )
            continue
        if hs.lot_likuid < 1:
            status[id(s)] = (
                TIDAK_LIKUID,
                f"1 lot > {teks.persen(cfg.likuiditas.maks_pct_order_dari_nilai_harian)} rata-rata nilai"
                f" transaksi harian ({teks.rupiah(Decimal(repr(sg.rata_nilai)))})",
            )
            continue
        if hs.lot_kas < 1:
            status[id(s)] = (KAS_KURANG, f"biaya 1 lot {teks.rupiah(hs.biaya_1_lot)} > kas tersedia {teks.rupiah(kas)}")
            continue
        # h. REKOMENDASI.
        alasan = "risiko 1 lot di atas anggaran (diizinkan config)" if hs.risiko_di_atas_anggaran else ""
        status[id(s)] = (REKOMENDASI, alasan)
        diterima += 1
        kas -= hs.biaya_beli_idr
        sektor_terpakai[sektor] += 1
        dep: list[str] = []
        if bebas and diterima > slot_tanpa_jual:
            dep.extend(p.kode for p in jual)
        if bebas and maks_sektor is not None and sektor_terpakai[sektor] + len(sektor_jual.get(sektor, ())) > maks_sektor:
            dep.extend(sektor_jual.get(sektor, ()))
        if dep:
            bergantung[id(s)] = tuple(dict.fromkeys(dep))

    return _hasil()

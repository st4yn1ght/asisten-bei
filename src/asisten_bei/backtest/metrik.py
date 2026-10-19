"""Metrik backtest (SPEC §16.5, termasuk "Definisi rinci", dan pembanding beli-dan-tahan IHSG).

Semua fungsi di modul ini murni dan deterministik (kecuali `close_ihsg_dari_pasar`, yang hanya membaca
data lewat `DataView`): masukannya baris ekuitas `mesin.BarisEkuitas` dan posisi `exit.Posisi`, keluarannya
dataclass beku. Uang (P&L, fee, slippage, ekuitas) memakai `Decimal`; rasio (CAGR, Sharpe, persen) boleh
float.

Konvensi:

- **Seri ekuitas**: baris pertama = titik awal periode (`E_awal`, close hari bursa terakhir sebelum
  periode, §16.4); baris berikutnya = hari simulasi. Drawdown dihitung dari titik awal; exposure dan modal
  terpakai hanya atas hari simulasi (tanpa baris titik awal).
- **Transaksi tertutup**: `Posisi.keluar` terisi dan alasannya bukan `TERBUKA`. Posisi yang masih terbuka
  pada tanggal penilaian sebuah periode dibawa sebagai `TransaksiTerbuka` (dinilai pada close, tanpa fee
  jual) dan tidak masuk PF/win rate.
- **PF / PF_R tanpa kerugian**: `NilaiPF.nilai is None` dengan `NilaiPF.na` True — ditampilkan
  "n/a (tanpa kerugian)". Untuk perbandingan dengan ambang, n/a dengan ≥ 1 transaksi menang bernilai
  tak hingga; n/a tanpa transaksi menang (tanpa transaksi sama sekali) tidak melewati ambang apa pun.
- Menang = P&L bersih > 0 (sama dengan R > 0, karena `risiko_awal_idr` > 0); kalah = P&L bersih < 0.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from .. import exit as ex
from .. import teks
from ..biaya import ke_decimal

if TYPE_CHECKING:  # pragma: no cover
    from ..data.tampilan import DataPasar
    from ..kalender import Kalender
    from .mesin import BarisEkuitas, SinyalDilewati

TEKS_NA_TANPA_KERUGIAN = "n/a (tanpa kerugian)"
TEKS_NA_TANPA_TRANSAKSI = "n/a (tanpa transaksi)"
TEKS_NA = "n/a"

PEMBATAS_NON_RISIKO = ("alokasi", "kas", "likuid")  # §16.5 "Pembatas lot" (bukan risiko)
HARI_BURSA_PER_TAHUN_DEFAULT = 252  # §16.5 Sharpe: tanpa tahun kalender penuh
HARI_PER_TAHUN = 365.25  # §16.5 CAGR

# Urutan tampilan status sinyal dilewati: status_sinyal §4.3, lalu status_isi §4.3.
URUTAN_STATUS_DILEWATI: tuple[str, ...] = (
    "SUDAH_DIPEGANG", "DIBLOKIR_PORTOFOLIO", "SLOT_PENUH", "BATAS_ENTRY_HARIAN", "BATAS_SEKTOR",
    "TIDAK_TERJANGKAU", "TIDAK_LIKUID", "KAS_KURANG", "BAYANGAN_GERBANG", "BAYANGAN_PASAR",
    "STOP_TERLALU_JAUH", "DUPLIKAT_STRATEGI",
    "MENUNGGU", "BATAL_GAP", "BATAL_DATA", "BATAL_ARA", "BATAL_SLOT_SAAT_OPEN", "BATAL_SEKTOR_SAAT_OPEN",
    "BATAL_KAS_SAAT_OPEN",
)
URUTAN_ALASAN_EXIT: tuple[str, ...] = (
    ex.STOP, ex.STOP_GAP, ex.STOP_GAP_HARI_ENTRY, ex.TRAILING, ex.TRAILING_GAP, ex.TARGET, ex.TARGET_GAP,
    ex.KELUAR_SMA, ex.STOP_WAKTU, ex.PAKSA_JUAL_PASAR, ex.DATA_BERAKHIR,
)

NOL = Decimal(0)


def _d(x: Any) -> Decimal:
    return ke_decimal(x)


def _rata(nilai: Sequence[Decimal]) -> Decimal | None:
    return sum(nilai, NOL) / len(nilai) if nilai else None


# ---------------------------------------------------------------------------
# Profit factor (Rupiah dan R)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NilaiPF:
    """`Σ menang / |Σ kalah|`. `nilai` None = n/a (tanpa kerugian); lihat docstring modul."""

    nilai: Decimal | None
    total_menang: Decimal
    total_kalah: Decimal  # |Σ kalah| ≥ 0
    n_menang: int
    n_kalah: int

    @property
    def na(self) -> bool:
        """True bila tidak ada transaksi kalah (PF tidak terdefinisi)."""
        return self.nilai is None

    @property
    def tak_hingga(self) -> bool:
        """n/a dengan ≥ 1 transaksi menang: dibandingkan dengan ambang sebagai tak hingga."""
        return self.nilai is None and self.n_menang > 0

    def minimal(self, ambang: Any) -> bool:
        """PF ≥ ambang (n/a: hanya bila ada transaksi menang)."""
        if self.nilai is None:
            return self.tak_hingga
        return self.nilai >= _d(ambang)

    def lebih_dari(self, ambang: Any) -> bool:
        """PF > ambang (n/a: hanya bila ada transaksi menang)."""
        if self.nilai is None:
            return self.tak_hingga
        return self.nilai > _d(ambang)

    def teks(self, desimal: int = 2) -> str:
        if self.nilai is not None:
            return teks.angka(self.nilai, desimal)
        return TEKS_NA_TANPA_KERUGIAN if self.n_menang > 0 else TEKS_NA_TANPA_TRANSAKSI


def profit_factor(nilai: Iterable[Decimal]) -> NilaiPF:
    """PF atas deret hasil (P&L bersih Rp atau R). Nilai 0 bukan menang dan bukan kalah."""
    menang = kalah = NOL
    n_m = n_k = 0
    for x in nilai:
        x = _d(x)
        if x > 0:
            menang += x
            n_m += 1
        elif x < 0:
            kalah += -x
            n_k += 1
    return NilaiPF(menang / kalah if n_k else None, menang, kalah, n_m, n_k)


# ---------------------------------------------------------------------------
# Seri ekuitas: CAGR, drawdown, Sharpe
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Drawdown:
    """Max drawdown `max_d (1 − E_d / max_{s≤d} E_s)` beserta tanggalnya (§16.5)."""

    maks: Decimal  # pecahan (0,25 = 25%); 0 bila tidak pernah turun dari puncak
    tanggal_puncak: date | None  # puncak sebelum lembah drawdown terbesar
    tanggal_lembah: date | None
    tanggal_pulih: date | None  # hari pertama sesudah lembah dengan E ≥ puncak; None = belum pulih
    durasi_terpanjang: int  # hari bursa (baris) berturut-turut di bawah puncak, terpanjang di seri

    @property
    def maks_pct(self) -> float:
        return float(self.maks * 100)


def drawdown(seri: Sequence[tuple[date, Decimal]]) -> Drawdown:
    """Drawdown seri (tanggal, nilai) urut naik. Baris pertama ikut sebagai puncak awal."""
    if not seri:
        return Drawdown(NOL, None, None, None, 0)
    puncak_nilai = _d(seri[0][1])
    puncak_tgl = seri[0][0]
    maks = NOL
    hasil_puncak = hasil_lembah = None
    acuan = NOL  # nilai puncak drawdown terbesar
    i_lembah = -1
    durasi = durasi_maks = 0
    for i, (tgl, nilai) in enumerate(seri):
        nilai = _d(nilai)
        if nilai >= puncak_nilai:
            puncak_nilai, puncak_tgl = nilai, tgl
            durasi = 0
            continue
        durasi += 1
        durasi_maks = max(durasi_maks, durasi)
        dd = 1 - nilai / puncak_nilai if puncak_nilai > 0 else NOL
        if dd > maks:
            maks, hasil_puncak, hasil_lembah, acuan, i_lembah = dd, puncak_tgl, tgl, puncak_nilai, i
    pulih = None
    if hasil_lembah is not None:
        pulih = next((t for t, v in seri[i_lembah + 1 :] if _d(v) >= acuan), None)
    return Drawdown(maks, hasil_puncak, hasil_lembah, pulih, durasi_maks)


def cagr(e_awal: Decimal, e_akhir: Decimal, hari_kalender: int) -> float | None:
    """`(E_akhir / E_awal)^(365,25 / hari_kalender) − 1`. None bila hari_kalender ≤ 0 atau E_awal ≤ 0."""
    e_awal, e_akhir = _d(e_awal), _d(e_akhir)
    if hari_kalender <= 0 or e_awal <= 0:
        return None
    rasio = float(e_akhir / e_awal)
    if rasio <= 0:
        return -1.0
    return rasio ** (HARI_PER_TAHUN / hari_kalender) - 1


def hari_bursa_per_tahun(
    tanggal_awal: date,
    tanggal_hari: Sequence[date],
    akhir: date | None = None,
    kal: "Kalender | None" = None,
) -> float:
    """Rata-rata jumlah hari bursa per tahun kalender penuh dalam periode (§16.5 Sharpe).

    `tanggal_hari`: hari simulasi (hari bursa) urut naik; `tanggal_awal`: titik awal (hari bursa terakhir
    sebelum periode). Tahun Y penuh bila titik awal < 1 Januari Y (semua hari bursa Y ≥ awal periode) dan
    akhir periode (default hari simulasi terakhir) ≥ hari bursa terakhir Y menurut `kal`, atau ≥ 31 Desember
    Y bila `kal` tidak diberikan. Tanpa tahun penuh → 252."""
    if not tanggal_hari:
        return float(HARI_BURSA_PER_TAHUN_DEFAULT)
    akhir = tanggal_hari[-1] if akhir is None else akhir
    per_tahun = Counter(d.year for d in tanggal_hari)

    def akhir_tahun(y: int) -> date:
        return date(y, 12, 31) if kal is None else kal.hari_bursa_sebelum(date(y + 1, 1, 1))

    penuh = [n for y, n in sorted(per_tahun.items()) if tanggal_awal < date(y, 1, 1) and akhir >= akhir_tahun(y)]
    if not penuh:
        return float(HARI_BURSA_PER_TAHUN_DEFAULT)
    return sum(penuh) / len(penuh)


def sharpe(nilai: Sequence[Decimal], hari_per_tahun: float) -> float | None:
    """`mean(r_harian) / std(r_harian) × √hari_per_tahun`, rf = 0, std sampel (ddof = 1).
    None bila < 2 return harian atau std = 0."""
    r = []
    for a, b in zip(nilai, nilai[1:]):
        a, b = _d(a), _d(b)
        if a <= 0:
            return None
        r.append(float(b / a) - 1)
    if len(r) < 2:
        return None
    rata = sum(r) / len(r)
    var = sum((x - rata) ** 2 for x in r) / (len(r) - 1)
    if var <= 0:
        return None
    return rata / math.sqrt(var) * math.sqrt(hari_per_tahun)


@dataclass(frozen=True)
class RingkasanSeri:
    """Metrik dasar sebuah seri nilai (strategi atau pembanding IHSG)."""

    tanggal_awal: date
    tanggal_akhir: date
    e_awal: Decimal
    e_akhir: Decimal
    hari_kalender: int  # dari tanggal titik awal
    imbal_hasil: float | None  # E_akhir / E_awal − 1
    cagr: float | None
    drawdown: Drawdown

    @property
    def calmar(self) -> float | None:
        if self.cagr is None or self.drawdown.maks <= 0:
            return None
        return self.cagr / float(self.drawdown.maks)


def ringkas_seri(seri: Sequence[tuple[date, Decimal]]) -> RingkasanSeri:
    """CAGR, imbal hasil, dan drawdown seri (tanggal, nilai); baris pertama = titik awal."""
    if not seri:
        raise ValueError("seri kosong: titik awal periode wajib ada")
    (t0, e0), (t1, e1) = seri[0], seri[-1]
    e0, e1 = _d(e0), _d(e1)
    hari = (t1 - t0).days
    imbal = float(e1 / e0) - 1 if e0 > 0 else None
    return RingkasanSeri(t0, t1, e0, e1, hari, imbal, cagr(e0, e1, hari), drawdown(seri))


@dataclass(frozen=True)
class MetrikEkuitas:
    seri: RingkasanSeri
    sharpe: float | None
    hari_bursa_per_tahun: float
    hari_simulasi: int  # baris tanpa titik awal
    exposure_pct: float | None  # % hari simulasi dengan ≥ 1 posisi
    modal_terpakai_pct: float | None  # rata-rata Σ nilai posisi ÷ ekuitas (%)
    hari_perkiraan: int  # hari simulasi dengan ekuitas "≈" (§14.1)

    # pintasan
    @property
    def cagr(self) -> float | None:
        return self.seri.cagr

    @property
    def drawdown(self) -> Drawdown:
        return self.seri.drawdown

    @property
    def calmar(self) -> float | None:
        return self.seri.calmar

    @property
    def e_awal(self) -> Decimal:
        return self.seri.e_awal

    @property
    def e_akhir(self) -> Decimal:
        return self.seri.e_akhir


def metrik_ekuitas(
    baris: Sequence["BarisEkuitas"], *, hari_per_tahun: float | None = None, kal: "Kalender | None" = None
) -> MetrikEkuitas:
    """Metrik ekuitas sebuah periode dari `mesin.BarisEkuitas` (baris pertama = titik awal).
    `hari_per_tahun` None → dihitung dari periode ini sendiri (`hari_bursa_per_tahun`, dengan `kal` bila
    diberikan)."""
    if not baris:
        raise ValueError("baris ekuitas kosong: titik awal periode wajib ada")
    seri = [(b.tanggal, b.ekuitas) for b in baris]
    hari = list(baris[1:])
    if hari_per_tahun is None:
        hpt = hari_bursa_per_tahun(baris[0].tanggal, [b.tanggal for b in hari], kal=kal)
    else:
        hpt = float(hari_per_tahun)
    exposure = modal = None
    if hari:
        exposure = sum(1 for b in hari if b.n_posisi >= 1) * 100 / len(hari)
        rasio = [float(b.nilai_posisi / b.ekuitas) for b in hari if b.ekuitas > 0]
        modal = sum(rasio) * 100 / len(rasio) if rasio else None
    return MetrikEkuitas(
        seri=ringkas_seri(seri),
        sharpe=sharpe([b.ekuitas for b in baris], hpt),
        hari_bursa_per_tahun=hpt,
        hari_simulasi=len(hari),
        exposure_pct=exposure,
        modal_terpakai_pct=modal,
        hari_perkiraan=sum(1 for b in hari if b.perkiraan),
    )


# ---------------------------------------------------------------------------
# Pembanding beli-dan-tahan IHSG
# ---------------------------------------------------------------------------


def close_ihsg_dari_pasar(
    pasar: "DataPasar", tanggal: Iterable[date], *, hanya_hari_itu: bool = False
) -> dict[date, Decimal]:
    """Close IHSG pada setiap tanggal, dibaca lewat `DataView(as_of=d)`.

    Default: close yang **berlaku** = close bar valid terakhir ≤ d — aturan penilaian yang sama dengan nilai
    posisi (§14.1), dipakai untuk ekuitas pembanding. `hanya_hari_itu=True`: hanya tanggal yang punya bar
    IHSG valid pada d itu sendiri (untuk kolom `ihsg_close` berkas ekuitas; tidak ada nilai yang diisi, P1).
    Tanggal tanpa close yang memenuhi tidak dimuat."""
    import pandas as pd

    hasil: dict[date, Decimal] = {}
    for d in tanggal:
        tampil = pasar.tampilan(d).ihsg()
        if tampil is None:
            continue
        valid = tampil.valid()
        if not len(valid) or (hanya_hari_itu and valid.index[-1] != pd.Timestamp(d)):
            continue
        hasil[d] = _d(float(valid["close"].iloc[-1]))
    return hasil


def ekuitas_ihsg(tanggal: Sequence[date], close: Mapping[date, Decimal], e_awal: Decimal) -> list[Decimal | None]:
    """Nilai beli-dan-tahan IHSG: `e_awal × close_d / close_titik_awal` (tanpa fee, §16.5). Tanggal pertama =
    titik awal. Tanpa close pada titik awal → semua None; tanpa close pada d → None (tidak dikarang)."""
    if not tanggal:
        return []
    awal = close.get(tanggal[0])
    if awal is None or awal <= 0:
        return [None] * len(tanggal)
    e_awal = _d(e_awal)
    return [e_awal * close[d] / awal if d in close else None for d in tanggal]


@dataclass(frozen=True)
class MetrikPembanding:
    """Beli-dan-tahan IHSG dari titik awal periode yang sama dengan strategi (§16.4–§16.5)."""

    seri: RingkasanSeri
    close_awal: Decimal
    close_akhir: Decimal
    selisih_cagr: float | None  # CAGR strategi − CAGR IHSG

    @property
    def cagr(self) -> float | None:
        return self.seri.cagr

    @property
    def drawdown(self) -> Drawdown:
        return self.seri.drawdown

    @property
    def imbal_hasil(self) -> float | None:
        return self.seri.imbal_hasil


def metrik_ihsg(
    baris: Sequence["BarisEkuitas"], close: Mapping[date, Decimal], cagr_strategi: float | None = None
) -> MetrikPembanding | None:
    """Pembanding IHSG atas tanggal-tanggal `baris` (baris pertama = titik awal; modal = E_awal strategi).
    None bila close IHSG pada titik awal tidak ada. Tanggal tanpa close dilewati (tidak diisi)."""
    if not baris:
        return None
    tanggal = [b.tanggal for b in baris]
    nilai = ekuitas_ihsg(tanggal, close, baris[0].ekuitas)
    if nilai[0] is None:
        return None
    seri = [(t, v) for t, v in zip(tanggal, nilai) if v is not None]
    ringkas = ringkas_seri(seri)
    selisih = None if cagr_strategi is None or ringkas.cagr is None else cagr_strategi - ringkas.cagr
    return MetrikPembanding(ringkas, close[tanggal[0]], close[seri[-1][0]], selisih)


# ---------------------------------------------------------------------------
# Transaksi
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransaksiTerbuka:
    """Posisi yang masih terbuka pada tanggal penilaian sebuah periode (`TERBUKA`): nilai pada close valid
    terakhir ≤ tanggal itu, tanpa fee jual; P&L = nilai − biaya beli (§16.1, §16.4, §16.9)."""

    posisi: ex.Posisi
    tanggal_nilai: date
    nilai_idr: Decimal
    pnl_idr: Decimal
    r: Decimal
    tanda: frozenset[str]  # tanda posisi ∪ tanda tampilan periode (mis. MELEWATI_HOLDOUT)


def transaksi_terbuka(
    pos: ex.Posisi,
    tanggal: date,
    nilai: Decimal,
    tanda_tambahan: Iterable[str] = (),
    tanda_dasar: Iterable[str] | None = None,
) -> TransaksiTerbuka:
    """`tanda_dasar`: tanda posisi pada `tanggal` (None = `pos.tanda` saat ini)."""
    nilai = _d(nilai)
    pnl = nilai - pos.biaya_beli_idr
    dasar = frozenset(pos.tanda if tanda_dasar is None else tanda_dasar)
    return TransaksiTerbuka(pos, tanggal, nilai, pnl, pnl / pos.risiko_awal_idr, dasar | set(tanda_tambahan))


def urut_exit(transaksi: Iterable[ex.Posisi]) -> list[ex.Posisi]:
    """Urutan tanggal exit (pemecah: tanggal entry, tanggal sinyal, kode, strategi) — deterministik."""
    return sorted(
        transaksi, key=lambda p: (p.keluar.tanggal, p.tanggal_entry, p.tanggal_sinyal, p.kode, p.strategi)
    )


def max_drawdown_r(transaksi: Iterable[ex.Posisi]) -> Decimal:
    """Drawdown terbesar (dalam R, ≥ 0) pada kurva R kumulatif transaksi tertutup yang diurutkan menurut
    tanggal exit. Kurva dimulai dari 0 sebelum transaksi pertama."""
    kum = puncak = maks = NOL
    for p in urut_exit(transaksi):
        kum += p.keluar.r
        puncak = max(puncak, kum)
        maks = max(maks, puncak - kum)
    return maks


def kerugian_beruntun_terpanjang(transaksi: Iterable[ex.Posisi]) -> int:
    """Jumlah transaksi kalah (P&L bersih < 0) berturut-turut terbanyak, urutan tanggal exit."""
    terpanjang = sekarang = 0
    for p in urut_exit(transaksi):
        if p.keluar.pnl_idr < 0:
            sekarang += 1
            terpanjang = max(terpanjang, sekarang)
        else:
            sekarang = 0
    return terpanjang


def hitung_dilewati(dilewati: Iterable["SinyalDilewati"]) -> dict[str, int]:
    """Jumlah sinyal dilewati per status (§16.5): `status_isi` bila order sudah dibuat (BATAL_*,
    MENUNGGU), selain itu `status_sinyal`. Urutan enum §4.3; status lain di belakang (abjad)."""
    hitung = Counter((s.status_isi or s.status_sinyal) for s in dilewati)
    urutan = [k for k in URUTAN_STATUS_DILEWATI if k in hitung] + sorted(k for k in hitung if k not in URUTAN_STATUS_DILEWATI)
    return {k: hitung[k] for k in urutan}


@dataclass(frozen=True)
class MetrikTransaksi:
    n_tertutup: int
    n_terbuka: int
    n_menang: int
    n_kalah: int
    win_rate_pct: float | None
    pf_idr: NilaiPF
    pf_r: NilaiPF
    max_drawdown_r: Decimal
    total_pnl_idr: Decimal
    total_r: Decimal
    ekspektansi_idr: Decimal | None
    ekspektansi_r: Decimal | None
    rata_menang_idr: Decimal | None
    rata_kalah_idr: Decimal | None
    rata_menang_r: Decimal | None
    rata_kalah_r: Decimal | None
    kerugian_beruntun_terpanjang: int
    rata_lama_menahan_hari: float | None  # hari bursa (hari entry = 1), §4.1
    fee_idr: Decimal
    slippage_idr: Decimal
    laba_kotor_idr: Decimal  # Σ (P&L bersih + fee + slippage)
    biaya_pct_laba_kotor: float | None  # (fee + slippage) ÷ laba kotor × 100; None = n/a (laba kotor ≤ 0)
    pembatas_lot_pct: float | None  # % transaksi yang lot-nya dibatasi alokasi/kas/likuid
    pembatas_lot: dict[str, int]  # jumlah per pembatas (risiko, alokasi, likuid, kas)
    risiko_terealisasi_pct: float | None  # rata-rata risiko_awal_idr ÷ ekuitas tanggal sinyal (%)
    per_alasan_exit: dict[str, int]
    per_tanda: dict[str, int]  # tertutup + terbuka, urutan enum tanda_transaksi; semua kunci ada
    pnl_terbuka_idr: Decimal  # Σ (nilai − biaya beli) transaksi TERBUKA
    tanpa_anomali: "MetrikTransaksi | None" = field(default=None)

    @property
    def biaya_total_idr(self) -> Decimal:
        return self.fee_idr + self.slippage_idr


def _cek_tertutup(p: ex.Posisi) -> None:
    if p.keluar is None or p.keluar.alasan == ex.TERBUKA:
        raise ValueError(f"{p.kode} {p.tanggal_entry}: bukan transaksi tertutup")


def metrik_transaksi(
    tertutup: Sequence[ex.Posisi],
    kal: "Kalender",
    *,
    terbuka: Sequence[TransaksiTerbuka] = (),
    dengan_tanpa_anomali: bool = True,
) -> MetrikTransaksi:
    """Metrik transaksi §16.5 atas transaksi tertutup sebuah periode (urutan bebas; diurutkan sendiri).
    `kal.nomor_hari(entry, exit)` = lama menahan. Juga dihitung ulang tanpa transaksi bertanda `ANOMALI`
    (`tanpa_anomali`) bila `dengan_tanpa_anomali`."""
    for p in tertutup:
        _cek_tertutup(p)
    urut = urut_exit(tertutup)
    pnl = [p.keluar.pnl_idr for p in urut]
    r = [p.keluar.r for p in urut]
    pf_idr, pf_r = profit_factor(pnl), profit_factor(r)
    n = len(urut)
    menang = [p for p in urut if p.keluar.pnl_idr > 0]
    kalah = [p for p in urut if p.keluar.pnl_idr < 0]
    fee = sum((p.fee_beli_idr + p.keluar.fee_jual_idr for p in urut), NOL)
    slip = sum((p.slippage_beli_idr + p.keluar.slippage_idr for p in urut), NOL)
    total = sum(pnl, NOL)
    laba_kotor = total + fee + slip
    lama = [kal.nomor_hari(p.tanggal_entry, p.keluar.tanggal) for p in urut]
    dengan_pembatas = [p for p in urut if "pembatas" in p.meta]
    pembatas = Counter(str(p.meta["pembatas"]) for p in dengan_pembatas)
    risiko = [
        p.risiko_awal_idr / _d(p.meta["ekuitas_sinyal"]) * 100
        for p in urut
        if p.meta.get("ekuitas_sinyal") is not None and _d(p.meta["ekuitas_sinyal"]) > 0
    ]
    alasan = Counter(p.keluar.alasan for p in urut)
    tanda = Counter()
    for p in urut:
        tanda.update(p.tanda)
    for t in terbuka:
        tanda.update(t.tanda)
    tanpa = None
    if dengan_tanpa_anomali:
        tanpa = metrik_transaksi(
            [p for p in urut if ex.ANOMALI not in p.tanda], kal,
            terbuka=[t for t in terbuka if ex.ANOMALI not in t.tanda], dengan_tanpa_anomali=False,
        )
    return MetrikTransaksi(
        n_tertutup=n,
        n_terbuka=len(terbuka),
        n_menang=len(menang),
        n_kalah=len(kalah),
        win_rate_pct=len(menang) * 100 / n if n else None,
        pf_idr=pf_idr,
        pf_r=pf_r,
        max_drawdown_r=max_drawdown_r(urut),
        total_pnl_idr=total,
        total_r=sum(r, NOL),
        ekspektansi_idr=_rata(pnl),
        ekspektansi_r=_rata(r),
        rata_menang_idr=_rata([p.keluar.pnl_idr for p in menang]),
        rata_kalah_idr=_rata([p.keluar.pnl_idr for p in kalah]),
        rata_menang_r=_rata([p.keluar.r for p in menang]),
        rata_kalah_r=_rata([p.keluar.r for p in kalah]),
        kerugian_beruntun_terpanjang=kerugian_beruntun_terpanjang(urut),
        rata_lama_menahan_hari=sum(lama) / n if n else None,
        fee_idr=fee,
        slippage_idr=slip,
        laba_kotor_idr=laba_kotor,
        biaya_pct_laba_kotor=float((fee + slip) * 100 / laba_kotor) if laba_kotor > 0 else None,
        pembatas_lot_pct=(
            sum(pembatas[k] for k in PEMBATAS_NON_RISIKO) * 100 / len(dengan_pembatas) if dengan_pembatas else None
        ),
        pembatas_lot={k: pembatas[k] for k in ("risiko", *PEMBATAS_NON_RISIKO) if pembatas[k]},
        risiko_terealisasi_pct=float(sum(risiko, NOL) / len(risiko)) if risiko else None,
        per_alasan_exit={k: alasan[k] for k in URUTAN_ALASAN_EXIT if alasan[k]},
        per_tanda={k: tanda[k] for k in ex.URUTAN_TANDA},
        pnl_terbuka_idr=sum((t.pnl_idr for t in terbuka), NOL),
        tanpa_anomali=tanpa,
    )

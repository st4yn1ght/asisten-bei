"""Mesin exit bersama (SPEC §15.1, §15.2) — satu mesin untuk backtest, jurnal kertas, jalur per sinyal,
dan pemutaran ulang posisi `portfolio.csv` (P2).

Ruang harga: semua level posisi (harga beli, stop/trailing, target) disimpan di **ruang tersesuaikan**
(harga cache, §8.4) sebagai `Decimal`. Level dihitung/dibulatkan di ruang nominal hari itu (× `F(d)`)
lalu dikembalikan (÷ `F(d)`). Arus Rupiah = `lembar_adj × harga_adj` (nilainya sama dengan nominal).

Langkah per posisi per hari bursa `d` (dipanggil berurutan oleh pemakai, §16.2):

- `buka` (langkah 0–4): bar tidak ada/tanpa transaksi → tidak ada eksekusi (jual terjadwal ditunda,
  `TERTUNDA_TANPA_TRANSAKSI`; `DATA_BERAKHIR` bila sudah `maks_hari_tanpa_bar` hari); bar terkunci ARB
  (terverifikasi) → semua jual ditunda (`TERTUNDA_ARB`); jual terjadwal di open × (1 − slip); gap di bawah
  stop → open × (1 − slip); gap di atas target → open (limit, tanpa slippage).
- `intraday` (langkah 5–6): stop `low ≤ S` → `S × (1 − slip)`, didahulukan dari target; target `high ≥ T`
  → `T`. Di hari entry hanya bila `entry.stop_aktif_hari_entry`.
- `tutup` (langkah 7): perbarui trailing BO (tidak pernah turun, dibulatkan ke atas, berlaku besok), lalu
  aturan berbasis close untuk dijadwalkan jual di open berikutnya, prioritas
  `PAKSA_JUAL_PASAR` > `KELUAR_SMA` > `STOP_WAKTU`.

Hanya membaca data lewat `DataView(as_of=d)` (P3). Tidak ada nilai yang dikarang (P1): tanpa bar valid,
tidak ada eksekusi dan nilai posisi = close valid terakhir.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from . import biaya
from .aturan_bei import AturanBEI, ke_desimal_harga
from .biaya import ke_decimal
from .indikator import k_sma
from .kondisi_pasar import JANGAN_BELI
from .strategi.dasar import hitung_target

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config
    from .data.tampilan import DataView
    from .kalender import Kalender

# alasan_exit (§4.3)
STOP = "STOP"
STOP_GAP = "STOP_GAP"
STOP_GAP_HARI_ENTRY = "STOP_GAP_HARI_ENTRY"
TRAILING = "TRAILING"
TRAILING_GAP = "TRAILING_GAP"
TARGET = "TARGET"
TARGET_GAP = "TARGET_GAP"
KELUAR_SMA = "KELUAR_SMA"
STOP_WAKTU = "STOP_WAKTU"
PAKSA_JUAL_PASAR = "PAKSA_JUAL_PASAR"
DATA_BERAKHIR = "DATA_BERAKHIR"
TERBUKA = "TERBUKA"

# tanda_transaksi (§4.3)
ANOMALI = "ANOMALI"
TERTUNDA_ARB = "TERTUNDA_ARB"
TERTUNDA_TANPA_TRANSAKSI = "TERTUNDA_TANPA_TRANSAKSI"
SUMBER_MANUAL = "SUMBER_MANUAL"
MELEWATI_HOLDOUT = "MELEWATI_HOLDOUT"
URUTAN_TANDA = (ANOMALI, TERTUNDA_ARB, TERTUNDA_TANPA_TRANSAKSI, SUMBER_MANUAL, MELEWATI_HOLDOUT)

PRIORITAS_JADWAL = (PAKSA_JUAL_PASAR, KELUAR_SMA, STOP_WAKTU)  # §15.1 langkah 7
STRATEGI_TRAILING = frozenset({"BO"})  # exit pada level stop berlabel TRAILING (§4.3 catatan)


def _d(x) -> Decimal:
    return ke_decimal(x)


def _f(x) -> Decimal:
    """Faktor split sebagai Decimal (representasi terpendek; 5.0 → 5)."""
    return ke_decimal(float(x))


# ---------------------------------------------------------------------------
# Bar satu hari
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BarHari:
    """Bar `kode` pada `tanggal` dari sudut pandang `DataView(as_of=tanggal)`."""

    kode: str
    tanggal: date
    ada: bool  # ada bar mentah pada tanggal (hari bursa)
    valid: bool  # bar valid: tidak rusak dan volume > 0 (§4.1)
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    faktor: Decimal  # F(tanggal)
    close_acuan: Decimal | None  # close valid sebelumnya (adj), acuan ARA/ARB "close_kemarin"
    sumber: str = ""

    def nominal(self, harga_adj: Decimal) -> Decimal:
        return harga_adj * self.faktor

    def ke_adj(self, harga_nominal: Decimal) -> Decimal:
        return harga_nominal / self.faktor


def bar_hari(dv: "DataView", kode: str) -> BarHari:
    """Bar `kode` pada `dv.as_of` (mentah, dengan tanda bar §8.5) dan close valid sebelumnya."""
    import pandas as pd

    d = dv.as_of
    ts = pd.Timestamp(d)
    f = _f(dv.faktor_split(kode))
    mentah = dv.mentah(kode)
    valid = dv.valid(kode)
    ada = len(mentah) > 0 and mentah.index[-1] == ts
    sebelum = valid[valid.index < ts]
    acuan = _d(float(sebelum["close"].iloc[-1])) if len(sebelum) else None
    if not ada:
        return BarHari(kode, d, False, False, None, None, None, None, f, acuan)
    r = mentah.iloc[-1]
    ok = str(r.get("tanda_bar", "")) == ""

    def angka(k: str) -> Decimal | None:
        x = float(r[k])
        return None if math.isnan(x) else _d(x)

    return BarHari(kode, d, True, ok, angka("open"), angka("high"), angka("low"), angka("close"), f, acuan,
                   str(r.get("sumber", "") or ""))


def terkunci_arb(bar: BarHari, bei: AturanBEI) -> bool:
    """Bar terkunci ARB (§15.1 langkah 1): hanya bila auto_reject terverifikasi & tanggal ≥ berlaku_mulai."""
    if not bar.valid or bar.close_acuan is None:
        return False
    n = bar.nominal
    return bei.terkunci_arb(n(bar.open), n(bar.high), n(bar.low), n(bar.close), n(bar.close_acuan), bar.tanggal)


def terkunci_ara(bar: BarHari, bei: AturanBEI) -> bool:
    """Bar terkunci ARA (§16.2 BATAL_ARA): hanya bila auto_reject terverifikasi & tanggal ≥ berlaku_mulai."""
    if not bar.valid or bar.close_acuan is None:
        return False
    n = bar.nominal
    return bei.terkunci_ara(n(bar.open), n(bar.high), n(bar.low), n(bar.close), n(bar.close_acuan), bar.tanggal)


# ---------------------------------------------------------------------------
# Posisi & hasil keluar
# ---------------------------------------------------------------------------


@dataclass
class Keluar:
    tanggal: date
    alasan: str  # alasan_exit §4.3
    harga: Decimal  # harga isi jual (adj), sudah termasuk slippage bila berlaku
    harga_nominal: Decimal  # harga isi jual nominal pada tanggal keluar
    nilai_idr: Decimal  # lembar × harga
    fee_jual_idr: Decimal
    hasil_jual_idr: Decimal  # nilai − fee
    pnl_idr: Decimal  # hasil jual − biaya beli
    r: Decimal  # pnl ÷ risiko_awal_idr
    slippage_idr: Decimal = Decimal(0)  # lembar × (harga acuan tanpa slippage − harga isi) ≥ 0


@dataclass
class Posisi:
    """Satu posisi terbuka. Harga & level di ruang tersesuaikan (Decimal); `lembar` = lembar_adj."""

    kode: str
    strategi: str  # PB | BO | MR | ACAK
    sektor: str
    tanggal_sinyal: date
    tanggal_entry: date
    lembar: Decimal
    harga_beli: Decimal
    biaya_beli_idr: Decimal
    fee_beli_idr: Decimal
    stop_awal: Decimal
    stop: Decimal  # S yang berlaku untuk hari berikutnya (stop tetap atau trailing BO)
    target: Decimal | None
    risiko_awal_idr: Decimal  # risiko rencana §4.1 (> 0)
    harga_maks: Decimal  # adj (rencana)
    faktor_entry: Decimal
    slippage_beli_idr: Decimal = Decimal(0)  # lembar × (harga isi − min(open, harga_maks)) ≥ 0
    jual_terjadwal: str | None = None  # alasan exit berbasis close yang dijadwalkan untuk open berikutnya
    tanggal_jadwal: date | None = None
    tanda: set[str] = field(default_factory=set)
    tanggal_valid_terakhir: date | None = None
    close_valid_terakhir: Decimal | None = None  # adj
    keluar: Keluar | None = None
    meta: dict = field(default_factory=dict)  # data rencana (skor, pembatas lot, dsb.) untuk laporan

    @property
    def terbuka(self) -> bool:
        return self.keluar is None

    @property
    def label_stop(self) -> str:
        return TRAILING if self.strategi in STRATEGI_TRAILING else STOP

    @property
    def label_gap(self) -> str:
        return TRAILING_GAP if self.strategi in STRATEGI_TRAILING else STOP_GAP

    def nilai(self) -> Decimal:
        """Nilai pasar pada close valid terakhir (§14.1); harga beli bila belum ada close valid."""
        harga = self.close_valid_terakhir if self.close_valid_terakhir is not None else self.harga_beli
        return self.lembar * harga

    def estimasi_hasil_stop(self, cfg: "Config") -> Decimal:
        """`stop_aktif × lembar × (1 − slip) × (1 − fj)` (§13.2 langkah 8, `dana_jual_langsung_dipakai`)."""
        return self.lembar * self.stop * (1 - biaya.slip(cfg)) * (1 - biaya.fj(cfg))


def _tutup(
    pos: Posisi, tanggal: date, alasan: str, harga_adj: Decimal, faktor: Decimal, cfg: "Config",
    acuan: Decimal | None = None,
) -> Keluar:
    """Tutup posisi di `harga_adj`. `acuan` = harga sebelum slippage (open atau level stop) untuk
    metrik biaya slippage §16.5; None = tanpa slippage (target, DATA_BERAKHIR)."""
    nilai = pos.lembar * harga_adj
    fee = biaya.fee_jual(nilai, cfg)
    hasil = nilai - fee
    pnl = hasil - pos.biaya_beli_idr
    slip = pos.lembar * (acuan - harga_adj) if acuan is not None else Decimal(0)
    k = Keluar(tanggal, alasan, harga_adj, harga_adj * faktor, nilai, fee, hasil, pnl, pnl / pos.risiko_awal_idr, slip)
    pos.keluar = k
    pos.jual_terjadwal = None
    return k


def tutup_terbuka(pos: Posisi, tanggal: date, cfg: "Config") -> Keluar:
    """Penilaian posisi yang masih terbuka (`TERBUKA`): nilai pada close valid terakhir, tanpa fee jual —
    "P&L" = nilai − biaya beli (§16.9 identitas akuntansi). Posisi TIDAK diubah."""
    harga = pos.close_valid_terakhir if pos.close_valid_terakhir is not None else pos.harga_beli
    nilai = pos.lembar * harga
    pnl = nilai - pos.biaya_beli_idr
    return Keluar(tanggal, TERBUKA, harga, harga, nilai, Decimal(0), nilai, pnl, pnl / pos.risiko_awal_idr)


# ---------------------------------------------------------------------------
# Entry (§15.1 "Hari entry", §16.2 langkah 2 bagian data/harga)
# ---------------------------------------------------------------------------

BATAL_DATA = "BATAL_DATA"
BATAL_GAP = "BATAL_GAP"
BATAL_ARA = "BATAL_ARA"
TERISI = "TERISI"


@dataclass(frozen=True)
class Order:
    """Order beli tertunda dari seleksi pada close `tanggal_sinyal` (§13–§14). Harga nominal pada
    tanggal sinyal; `faktor_sinyal` = F(tanggal_sinyal)."""

    kode: str
    strategi: str
    sektor: str
    tanggal_sinyal: date
    lembar: int  # lembar nominal (lot × lot_lembar) pada tanggal sinyal
    harga_maks: Decimal  # nominal, dibulatkan ke bawah
    stop: Decimal  # nominal, dibulatkan ke atas
    target_r: float | None
    faktor_sinyal: Decimal
    skor: float = 0.0
    meta: dict = field(default_factory=dict)


def periksa_isi(order: Order, bar: BarHari, bei: AturanBEI) -> str | None:
    """Pembatalan di open yang hanya memakai fakta bar `d` (§16.2): BATAL_DATA, BATAL_GAP, BATAL_ARA.
    None = boleh diisi (cek slot/sektor/kas dilakukan pemakai)."""
    if not bar.valid or bar.open is None:
        return BATAL_DATA
    # Di ruang tersesuaikan: harga_maks nominal pada tanggal sinyal ÷ F(tanggal sinyal). Sama dengan
    # perbandingan nominal di hari d, juga bila split terjadi tepat pada d.
    if bar.open > order.harga_maks / order.faktor_sinyal:
        return BATAL_GAP
    if terkunci_ara(bar, bei):
        return BATAL_ARA
    return None


def harga_isi(order: Order, bar: BarHari, cfg: "Config") -> Decimal:
    """`min(open × (1 + slip), harga_maks)` di ruang tersesuaikan (§4.1, §15.1)."""
    return biaya.harga_isi_beli(bar.open, order.harga_maks / order.faktor_sinyal, cfg)


def biaya_isi(order: Order, bar: BarHari, cfg: "Config") -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """(lembar_adj, harga isi adj, fee beli, biaya beli total) untuk order pada bar `d`."""
    lembar = Decimal(order.lembar) * order.faktor_sinyal
    harga = harga_isi(order, bar, cfg)
    nilai = lembar * harga
    fee = biaya.fee_beli(nilai, cfg)
    return lembar, harga, fee, nilai + fee


def isi_order(order: Order, bar: BarHari, cfg: "Config", bei: AturanBEI) -> Posisi:
    """Buat posisi dari order yang terisi pada open `bar.tanggal` (pemakai sudah memastikan lolos semua
    cek). Target PB/ACAK = harga beli + target_r × (harga beli − stop), nominal hari itu, ke bawah."""
    lembar, harga, fee, total = biaya_isi(order, bar, cfg)
    stop_adj = order.stop / order.faktor_sinyal
    acuan_beli = min(bar.open, order.harga_maks / order.faktor_sinyal)
    target = None
    if order.target_r is not None and harga > stop_adj:  # harga isi ≤ stop: langsung STOP_GAP_HARI_ENTRY
        t_nom = hitung_target(harga * bar.faktor, stop_adj * bar.faktor, order.target_r, bei, bar.tanggal)
        target = t_nom / bar.faktor
    risiko = Decimal(order.lembar) * biaya.risiko_lembar(order.harga_maks, order.stop, cfg)
    return Posisi(
        kode=order.kode, strategi=order.strategi, sektor=order.sektor,
        tanggal_sinyal=order.tanggal_sinyal, tanggal_entry=bar.tanggal,
        lembar=lembar, harga_beli=harga, biaya_beli_idr=total, fee_beli_idr=fee,
        stop_awal=stop_adj, stop=stop_adj, target=target, risiko_awal_idr=risiko,
        harga_maks=order.harga_maks / order.faktor_sinyal, faktor_entry=bar.faktor,
        slippage_beli_idr=lembar * (harga - acuan_beli),
        tanggal_valid_terakhir=bar.tanggal, close_valid_terakhir=bar.close, meta=dict(order.meta),
    )


def keluar_hari_entry(pos: Posisi, bar: BarHari, cfg: "Config", bei: AturanBEI) -> Keluar | None:
    """Open ≤ stop di hari entry → langsung keluar di open × (1 − slip) `STOP_GAP_HARI_ENTRY`, kecuali
    bar terkunci ARB (jual ditunda, `TERTUNDA_ARB`, dan dijadwalkan untuk bar valid berikutnya)."""
    if bar.open is None or bar.open > pos.stop:
        return None
    if terkunci_arb(bar, bei):
        pos.tanda.add(TERTUNDA_ARB)
        pos.jual_terjadwal = STOP_GAP_HARI_ENTRY
        pos.tanggal_jadwal = bar.tanggal
        return None
    return _tutup(pos, bar.tanggal, STOP_GAP_HARI_ENTRY, biaya.harga_isi_jual(bar.open, cfg), bar.faktor, cfg,
                  acuan=bar.open)


# ---------------------------------------------------------------------------
# Langkah 0–4: pembukaan
# ---------------------------------------------------------------------------


def hari_tanpa_bar_valid(pos: Posisi, d: date, kal: "Kalender") -> int:
    """Jumlah hari bursa berturut-turut sampai `d` (inklusif) tanpa bar valid sejak bar valid terakhir."""
    terakhir = pos.tanggal_valid_terakhir or pos.tanggal_entry
    if d <= terakhir:
        return 0
    return len(kal.daftar_hari_bursa(terakhir, d)) - (1 if kal.adalah_hari_bursa(terakhir) else 0)


def buka(pos: Posisi, bar: BarHari, cfg: "Config", bei: AturanBEI, kal: "Kalender") -> Keluar | None:
    """Langkah 0–4 §15.1 untuk posisi lama (bukan hari entry) pada pembukaan `bar.tanggal`."""
    d = bar.tanggal
    # 0. Bar tidak ada / tanpa transaksi / rusak.
    if not bar.valid:
        if pos.jual_terjadwal is not None:
            pos.tanda.add(TERTUNDA_TANPA_TRANSAKSI)
        if hari_tanpa_bar_valid(pos, d, kal) >= int(cfg.backtest.maks_hari_tanpa_bar):
            potong = 1 - _d(cfg.backtest.potongan_data_berakhir_pct) / 100
            harga = (pos.close_valid_terakhir or pos.harga_beli) * potong
            return _tutup(pos, d, DATA_BERAKHIR, harga, bar.faktor, cfg)
        return None
    # 1. Bar terkunci ARB → semua jual hari ini ditunda.
    if terkunci_arb(bar, bei):
        if pos.jual_terjadwal is not None or bar.open <= pos.stop or bar.low <= pos.stop:
            pos.tanda.add(TERTUNDA_ARB)
        pos.meta["_terkunci_arb"] = d
        return None
    # 2. Jual terjadwal.
    if pos.jual_terjadwal is not None:
        return _tutup(pos, d, pos.jual_terjadwal, biaya.harga_isi_jual(bar.open, cfg), bar.faktor, cfg,
                      acuan=bar.open)
    # 3. Gap di bawah stop.
    if bar.open <= pos.stop:
        return _tutup(pos, d, pos.label_gap, biaya.harga_isi_jual(bar.open, cfg), bar.faktor, cfg, acuan=bar.open)
    # 4. Gap di atas target (limit, tanpa slippage).
    if pos.target is not None and bar.open >= pos.target:
        return _tutup(pos, d, TARGET_GAP, bar.open, bar.faktor, cfg)
    return None


# ---------------------------------------------------------------------------
# Langkah 5–6: sepanjang hari
# ---------------------------------------------------------------------------


def intraday(pos: Posisi, bar: BarHari, cfg: "Config", hari_entry: bool) -> Keluar | None:
    """Langkah 5–6 §15.1. Di hari entry hanya bila `entry.stop_aktif_hari_entry`. Bar tidak valid atau
    terkunci ARB (ditandai `buka`) → tidak ada eksekusi."""
    if not pos.terbuka or not bar.valid:
        return None
    if hari_entry and not cfg.entry.stop_aktif_hari_entry:
        return None
    if pos.meta.get("_terkunci_arb") == bar.tanggal:
        return None
    if bar.low <= pos.stop:
        return _tutup(pos, bar.tanggal, pos.label_stop, biaya.harga_isi_jual(pos.stop, cfg), bar.faktor, cfg,
                      acuan=pos.stop)
    if pos.target is not None and bar.high >= pos.target:
        return _tutup(pos, bar.tanggal, TARGET, pos.target, bar.faktor, cfg)
    return None


# ---------------------------------------------------------------------------
# Langkah 7: penutupan
# ---------------------------------------------------------------------------


def perbarui_nilai(pos: Posisi, bar: BarHari) -> None:
    """Close valid `bar` menjadi dasar nilai posisi (§14.1); tanpa bar valid, close valid terakhir tetap."""
    if bar.valid and bar.close is not None:
        pos.tanggal_valid_terakhir = bar.tanggal
        pos.close_valid_terakhir = bar.close


def _param_strategi(cfg: "Config", strategi: str):
    s = cfg.strategi
    return {"PB": s.pb, "BO": s.bo, "MR": s.mr, "ACAK": s.acak}[strategi]


def tutup(
    pos: Posisi,
    bar: BarHari,
    dv: "DataView",
    cfg: "Config",
    bei: AturanBEI,
    kal: "Kalender",
    status_pasar: str | None,
) -> str | None:
    """Langkah 7 §15.1 pada close `bar.tanggal`: nilai, trailing BO, lalu jadwal jual berbasis close.
    Mengembalikan alasan yang dijadwalkan (atau None). Bar tidak valid → tidak ada pembaruan.
    Nilai posisi diperbarui terpisah lewat `perbarui_nilai` (sebelum ekuitas dihitung, §16.2 langkah 4)."""
    if not pos.terbuka or not bar.valid:
        return None
    d = bar.tanggal
    perbarui_nilai(pos, bar)
    p = _param_strategi(cfg, pos.strategi)

    # Trailing BO: trail_d = max(trail_{d−1}, HHV_sejak_entry(high)_d − k × ATR_d), ke atas, berlaku besok.
    if pos.strategi in STRATEGI_TRAILING:
        baris = dv.baris(pos.kode)
        atr = float(baris["atr"]) if baris is not None else math.nan
        hhv = dv.hhv_sejak(pos.kode, pos.tanggal_entry, d)
        if not math.isnan(atr) and atr > 0 and not math.isnan(hhv):
            kandidat_nom = (_d(hhv) - _d(float(p.trailing_atr)) * _d(atr)) * bar.faktor
            if kandidat_nom > 0:
                kandidat = bei.bulat_atas(ke_desimal_harga(kandidat_nom), d).nilai / bar.faktor
                if kandidat > pos.stop:
                    pos.stop = kandidat

    if pos.jual_terjadwal is not None:  # jadwal lama yang tertunda tetap berlaku dengan alasan aslinya
        return pos.jual_terjadwal
    alasan: list[str] = []
    if cfg.kondisi_pasar.jangan_beli_paksa_jual and status_pasar == JANGAN_BELI:
        alasan.append(PAKSA_JUAL_PASAR)
    hari_ke = kal.nomor_hari(pos.tanggal_entry, d)
    if pos.strategi == "MR" and hari_ke >= int(p.min_hari_tahan):
        baris = dv.baris(pos.kode)
        sma = float(baris[k_sma(int(p.sma_keluar))]) if baris is not None else math.nan
        if not math.isnan(sma) and float(bar.close) > sma:
            alasan.append(KELUAR_SMA)
    n = getattr(p, "stop_waktu_hari", None)
    if n is not None and hari_ke >= int(n):
        alasan.append(STOP_WAKTU)
    if not alasan:
        return None
    pilih = min(alasan, key=PRIORITAS_JADWAL.index)
    pos.jual_terjadwal = pilih
    pos.tanggal_jadwal = d
    return pilih

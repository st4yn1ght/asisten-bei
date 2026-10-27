"""Posisi `portfolio.csv` di run harian (SPEC §6.5, §15.3, §15.4).

Untuk setiap baris:

1. **Ruang harga (§15.3).** `harga_beli`, `stop_awal`, dan `lot`/`lembar` bernilai nominal per
   `harga_per_tanggal` (kosong = `tanggal_beli`). Nilai itu dibawa ke skala harga cache pada `tanggal_beli`:
   `harga_adj = harga × Π r(s ∈ (tanggal_beli, acuan]) ÷ F(tanggal_beli)` dan
   `lembar_adj = lembar × F(tanggal_beli) ÷ Π r(s ∈ (tanggal_beli, acuan]) `, dengan Π atas semua split yang
   tercatat dan `F` dari data split terkini. Untuk split yang sudah disesuaikan sumber, ini sama dengan
   `harga ÷ F(acuan)` dan `lembar × F(acuan)`; split yang belum disesuaikan sumber (tidak ikut `F`, §8.4)
   dikonversi oleh mesin exit pada tanggal split, persis seperti posisi backtest. Dengan begitu baris yang
   sudah diperbarui ke nilai pasca-split dan baris yang dibiarkan sama-sama benar, tanpa penyesuaian ganda.
2. **Pemutaran ulang (§15.4).** Mesin exit bersama (`exit.py`, §15.1) diputar dari `tanggal_beli` sampai `t`
   dengan `harga_beli` sebagai harga isi: hari entry = `keluar_hari_entry` → `intraday(hari_entry=True)` →
   `tutup`; hari berikutnya = `buka` → `intraday` → `tutup`. Target PB = harga beli + `target_r` × (harga beli
   − stop_awal), dibulatkan ke bawah pada tanggal beli. Risiko rencana = `lembar × risiko_lembar(harga_beli,
   stop_awal)` (§4.1: harga_beli menggantikan harga_maks); ≤ 0 → R "n/a".
3. **Saran**, kondisi pertama yang cocok (§15.4 #1–#7): TIDAK_DAPAT_DIEVALUASI, INFO_MANUAL, JUAL (stop_awal ≥
   harga_beli), JUAL (seharusnya sudah keluar), CEK_ORDER, JUAL (aturan close `t`), TAHAN.

**TIDAK DAPAT DIEVALUASI karena §15.3:** data split tidak tersedia untuk bar yang dipakai (bar dari CSV manual
tanpa berkas split, §6.9) sementara ada lonjakan > `lonjakan_mencurigakan_pct` sejak `tanggal_beli` (kecuali
tanggal di `anomali_ok.csv`). Posisi itu memblokir semua rekomendasi beli (§13.2 langkah 6a).

Ekuitas = kas `akun.yaml` + Σ nilai posisi pada close valid terakhir ≤ t (§14.1); bila ada posisi tanpa close
valid `t`, ekuitas ditandai "≈". Semua uang `Decimal`; data hanya lewat `DataView` (P3); tidak ada nilai yang
dikarang (P1).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from . import biaya, teks
from . import exit as ex
from .aturan_bei import AturanBEI, ke_desimal_harga
from .indikator import k_sma
from .seleksi import KeadaanPortofolio, PosisiTerbuka

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config
    from .data.tampilan import DataPasar, DataView
    from .inputs import BarisPortofolio, Masukan

# saran_posisi (§4.3)
TAHAN = "TAHAN"
JUAL = "JUAL"
CEK_ORDER = "CEK_ORDER"
TIDAK_DAPAT_DIEVALUASI = "TIDAK_DAPAT_DIEVALUASI"
INFO_MANUAL = "INFO_MANUAL"

MANUAL = "MANUAL"
STRATEGI_DIPUTAR = frozenset({"PB", "BO", "MR"})

# Exit oleh order otomatis pada t (§15.4 #5); exit lain pada t berasal dari jual terjadwal (#4).
EXIT_ORDER_OTOMATIS = frozenset({
    ex.STOP, ex.STOP_GAP, ex.TRAILING, ex.TRAILING_GAP, ex.TARGET, ex.TARGET_GAP, ex.STOP_GAP_HARI_ENTRY,
})
_EXIT_DI_OPEN = frozenset({ex.STOP_GAP, ex.TRAILING_GAP, ex.TARGET_GAP, ex.STOP_GAP_HARI_ENTRY})

BANNER_BLOKIR = "PORTOFOLIO TIDAK DAPAT DIEVALUASI — tidak ada rekomendasi beli"
BANNER_SPLIT = "SPLIT TERDETEKSI"
TEKS_PERBARUI = "Jika sudah dijual, perbarui portfolio.csv dan akun.yaml."


def _d(x) -> Decimal:
    return biaya.ke_decimal(x)


def _rp(x: Decimal | None) -> str:
    return teks.STRIP if x is None else teks.rupiah(x)


def _h(x: Decimal | float | None) -> str:
    """Harga untuk teks: rupiah bulat tanpa desimal, selain itu 2 desimal (gaya Indonesia)."""
    if x is None:
        return teks.STRIP
    d = _d(x)
    return teks.angka(d, 0 if d == d.to_integral_value() else 2)


# ---------------------------------------------------------------------------
# Hasil
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InfoSplit:
    """Padanan pasca-split baris portfolio.csv pada `t` (banner SPLIT TERDETEKSI, §15.3)."""

    split: tuple[tuple[date, Decimal], ...]  # (tanggal, rasio) dalam (acuan, t]
    lembar: Decimal
    harga_beli: Decimal
    stop: Decimal | None
    target: Decimal | None

    def teks(self, kode: str) -> str:
        daftar = ", ".join(f"{s.isoformat()} rasio {teks.angka(r, 4).rstrip('0').rstrip(',')}" for s, r in self.split)
        bagian = [f"lembar {teks.angka(self.lembar, 0 if self.lembar == self.lembar.to_integral_value() else 4)}",
                  f"harga beli {_h(self.harga_beli)}"]
        if self.stop is not None:
            bagian.append(f"stop {_h(self.stop)}")
        if self.target is not None:
            bagian.append(f"target {_h(self.target)}")
        return (f"{kode}: split {daftar} sesudah tanggal acuan baris. Padanan pasca-split: " + ", ".join(bagian)
                + ". Cocokkan dengan aplikasi broker dan periksa ulang order otomatis.")


@dataclass
class SaranPosisi:
    baris: "BarisPortofolio"
    sektor: str
    saran: str  # saran_posisi §4.3
    nomor: int  # baris §15.4 yang cocok (1–7)
    alasan: str
    lembar: Decimal  # lembar nominal pada t
    harga_beli: Decimal  # nominal pada t
    stop_besok: Decimal | None = None  # level stop/trailing untuk t+1 (nominal pada t)
    stop_hari_ini: Decimal | None = None  # level yang berlaku pada t
    target: Decimal | None = None  # nominal pada t
    hari_ke: int | None = None
    stop_waktu_hari: int | None = None
    close: Decimal | None = None  # close valid terakhir (nominal pada t)
    tanggal_close: date | None = None
    perkiraan: bool = False  # tanpa close valid t
    nilai_idr: Decimal = Decimal(0)
    biaya_beli_idr: Decimal = Decimal(0)
    pnl_idr: Decimal = Decimal(0)  # belum terealisasi, sebelum fee jual
    pnl_pct: Decimal | None = None
    risiko_awal_idr: Decimal | None = None  # None bila ≤ 0 (R "n/a")
    r: Decimal | None = None
    keluar: ex.Keluar | None = None  # exit menurut pemutaran ulang
    level_order: Decimal | None = None  # CEK_ORDER: level order otomatis (nominal pada t)
    jadwal: str | None = None  # alasan jual terjadwal untuk t+1 (#6)
    blokir: bool = False  # §15.3 lonjakan tanpa data split
    split: InfoSplit | None = None
    peringatan: list[str] = field(default_factory=list)
    estimasi_hasil_stop_idr: Decimal = Decimal(0)

    @property
    def kode(self) -> str:
        return self.baris.kode

    @property
    def strategi(self) -> str:
        return self.baris.strategi

    @property
    def dijadwalkan_jual(self) -> bool:
        """Saran JUAL (#3, #4, #6) dihitung "dijadwalkan jual" untuk slot (§15.4); CEK_ORDER tetap terisi."""
        return self.saran == JUAL

    @property
    def teks_saran(self) -> str:
        if self.saran == JUAL:
            return "JUAL di pembukaan t+1"
        if self.saran == INFO_MANUAL:
            return "INFO (MANUAL)"
        if self.saran == TIDAK_DAPAT_DIEVALUASI:
            return "TIDAK DAPAT DIEVALUASI"
        if self.saran == CEK_ORDER:
            return "CEK ORDER"
        return self.saran

    @property
    def teks_r(self) -> str:
        return "n/a" if self.r is None else teks.angka(self.r, 2) + "R"


@dataclass
class HasilPortofolio:
    t: date
    saran: list[SaranPosisi]
    kas_idr: Decimal
    nilai_posisi_idr: Decimal
    ekuitas_idr: Decimal
    perkiraan: bool  # ≈: ada posisi tanpa close valid t
    keadaan: KeadaanPortofolio
    akun_dari_berkas: bool
    pengingat: list[str] = field(default_factory=list)

    @property
    def blokir(self) -> str | None:
        return self.keadaan.blokir

    @property
    def banner_split(self) -> list[str]:
        return [s.split.teks(s.kode) for s in self.saran if s.split is not None]


# ---------------------------------------------------------------------------
# Ruang harga (§15.3)
# ---------------------------------------------------------------------------


def _split(dv: "DataView", kode: str) -> list[tuple[date, Decimal]]:
    """Semua split tercatat ≤ as_of: (tanggal, rasio)."""
    s = dv.seri(kode).split()
    return [(ts.date(), _d(float(r))) for ts, r in s.items()]


def _hasil_kali(split: Iterable[tuple[date, Decimal]], awal: date, akhir: date) -> Decimal:
    """Π rasio split dengan tanggal dalam (awal, akhir]."""
    hasil = Decimal(1)
    for s, r in split:
        if awal < s <= akhir:
            hasil *= r
    return hasil


def bangun_posisi(baris: "BarisPortofolio", dv: "DataView", sektor: str, cfg: "Config", bei: AturanBEI) -> ex.Posisi:
    """`Posisi` mesin exit dari baris portfolio.csv, di skala harga cache pada `tanggal_beli` (§15.3).
    `dv` = DataView(as_of=t); hanya metadata split dan `F(d)` (d ≤ t) yang dibaca."""
    kode = baris.kode
    tb = baris.tanggal_beli
    acuan = baris.tanggal_acuan()
    split = _split(dv, kode) if dv.ada(kode) else []
    pi = _hasil_kali(split, tb, acuan)
    f_tb = ex._f(dv.faktor_split(kode, tb)) if dv.ada(kode) else Decimal(1)
    lembar_nom = Decimal(baris.lembar_nominal(int(cfg.pasar.lot_lembar)))
    harga = _d(baris.harga_beli) * pi / f_tb
    stop = _d(baris.stop_awal) * pi / f_tb
    lembar = lembar_nom * f_tb / pi
    nilai = lembar * harga
    fee = biaya.fee_beli(nilai, cfg)
    risiko = lembar_nom * biaya.risiko_lembar(_d(baris.harga_beli), _d(baris.stop_awal), cfg)
    target = None
    if baris.strategi == "PB" and _d(baris.harga_beli) > _d(baris.stop_awal):
        from .strategi.dasar import hitung_target

        isi_nom, stop_nom = ex._nom(harga, f_tb), ex._nom(stop, f_tb)
        target = hitung_target(isi_nom, stop_nom, float(cfg.strategi.pb.target_r), bei, tb) / f_tb
    return ex.Posisi(
        kode=kode, strategi=baris.strategi, sektor=sektor, tanggal_sinyal=tb, tanggal_entry=tb,
        lembar=lembar, harga_beli=harga, biaya_beli_idr=nilai + fee, fee_beli_idr=fee,
        stop_awal=stop, stop=stop, target=target, risiko_awal_idr=risiko, harga_maks=harga, faktor_entry=f_tb,
    )


def _close_valid_terakhir(dv: "DataView", kode: str) -> tuple[date, Decimal] | None:
    if not dv.ada(kode):
        return None
    v = dv.valid(kode)
    if len(v) == 0:
        return None
    return v.index[-1].date(), _d(float(v["close"].iloc[-1]))


# ---------------------------------------------------------------------------
# Lonjakan tanpa data split (§15.3)
# ---------------------------------------------------------------------------


def lonjakan_tanpa_split(
    dv: "DataView", kode: str, sejak: date, ambang_pct: float, anomali_ok: set[tuple[str, date]]
) -> date | None:
    """Tanggal lonjakan pertama |close_a / close_{a−1} − 1| > ambang pada bar valid a ∈ (sejak, as_of] yang
    berasal dari CSV manual (tanpa berkas split), kecuali tanggal di `anomali_ok.csv`. None bila tidak ada."""
    if not dv.ada(kode):
        return None
    v = dv.valid(kode)
    if len(v) < 2 or "sumber" not in v.columns:
        return None
    close = v["close"].to_numpy(dtype="float64")
    sumber = v["sumber"].astype(str).to_numpy()
    for i in range(1, len(v)):
        a = v.index[i].date()
        if a <= sejak or not sumber[i].startswith("csv_manual") or (kode, a) in anomali_ok:
            continue
        sebelum = close[i - 1]
        if sebelum > 0 and abs(close[i] / sebelum - 1) * 100 > float(ambang_pct):
            return a
    return None


# ---------------------------------------------------------------------------
# Pemutaran ulang (§15.4)
# ---------------------------------------------------------------------------


@dataclass
class _Putar:
    keluar: ex.Keluar | None = None
    jadwal_open: bool = False  # exit di open hari keluar berasal dari jual terjadwal
    level_order: Decimal | None = None  # adj, untuk CEK_ORDER
    stop_hari_ini: Decimal | None = None  # S yang berlaku pada t (adj)
    jadwal_t: str | None = None  # jual terjadwal dari close t
    bar_t: ex.BarHari | None = None


def putar_ulang(
    pos: ex.Posisi,
    pasar: "DataPasar",
    t: date,
    *,
    bei: AturanBEI,
    status_pasar_pada: Callable[[date], str | None] | None = None,
) -> _Putar:
    """Putar §15.1 untuk `pos` dari `tanggal_entry` sampai `t` (inklusif). `pos` diubah di tempat."""
    cfg = pasar.cfg
    kal = pasar.kalender
    hasil = _Putar()
    status = status_pasar_pada or (lambda d: None)
    for d in kal.daftar_hari_bursa(pos.tanggal_entry, t):
        dv = pasar.tampilan(d)
        hari_entry = d == pos.tanggal_entry
        if not hari_entry:
            ex.sesuaikan_posisi_split(pos, dv)
        b = ex.bar_hari(dv, pos.kode)
        if d == t:
            hasil.bar_t = b
            hasil.stop_hari_ini = pos.stop
        stop_lama, target_lama, jadwal_lama = pos.stop, pos.target, pos.jual_terjadwal
        if hari_entry:
            k = ex.keluar_hari_entry(pos, b, cfg, bei) if b.valid else None
            if b.valid:
                ex.perbarui_nilai(pos, b)
        else:
            k = ex.buka(pos, b, cfg, bei, kal)
        if k is None:
            k = ex.intraday(pos, b, cfg, hari_entry=hari_entry)
        if k is not None:
            hasil.keluar = k
            hasil.jadwal_open = jadwal_lama is not None and k.alasan == jadwal_lama and not hari_entry
            if k.alasan in (ex.STOP, ex.TRAILING):
                hasil.level_order = stop_lama
            elif k.alasan == ex.TARGET:
                hasil.level_order = target_lama
            elif k.alasan in _EXIT_DI_OPEN and b.open is not None:
                hasil.level_order = b.open
            return hasil
        ex.perbarui_nilai(pos, b)
        jadwal = ex.tutup(pos, b, dv, cfg, bei, kal, status(d))
        if d == t:
            hasil.jadwal_t = jadwal
    return hasil


def _alasan_jadwal(alasan: str, pos: ex.Posisi, dv: "DataView", cfg: "Config", hari_ke: int, f_t: Decimal) -> str:
    """Teks alasan jual terjadwal dari close t (§15.4 #6)."""
    if alasan == ex.KELUAR_SMA:
        n = int(cfg.strategi.mr.sma_keluar)
        baris = dv.baris(pos.kode)
        sma = float(baris[k_sma(n)]) if baris is not None else math.nan
        close = pos.close_valid_terakhir
        teks_sma = teks.STRIP if math.isnan(sma) else _h(ex._nom(_d(sma), f_t))
        return f"MR: close {_h(None if close is None else ex._nom(close, f_t))} > SMA{n} {teks_sma}"
    if alasan == ex.STOP_WAKTU:
        p = ex._param_strategi(cfg, pos.strategi)
        return f"stop waktu: hari ke-{hari_ke} ≥ {int(p.stop_waktu_hari)} hari ({pos.strategi})"
    if alasan == ex.PAKSA_JUAL_PASAR:
        return "status pasar JANGAN BELI dan kondisi_pasar.jangan_beli_paksa_jual aktif"
    return alasan


# ---------------------------------------------------------------------------
# Evaluasi seluruh portofolio
# ---------------------------------------------------------------------------


def _nom(x: Decimal | None, f: Decimal) -> Decimal | None:
    return None if x is None else ex._nom(x, f)


def evaluasi_baris(
    baris: "BarisPortofolio",
    pasar: "DataPasar",
    t: date,
    *,
    sektor: str,
    bei: AturanBEI,
    anomali_ok: set[tuple[str, date]],
    tanpa_data_split: Callable[[str], bool],
    status_pasar_pada: Callable[[date], str | None] | None = None,
) -> SaranPosisi:
    """Saran §15.4 untuk satu baris portfolio.csv pada `t`."""
    cfg = pasar.cfg
    kal = pasar.kalender
    dv = pasar.tampilan(t)
    kode = baris.kode
    ada = dv.ada(kode)
    acuan = baris.tanggal_acuan()
    pos = bangun_posisi(baris, dv, sektor, cfg, bei)
    f_tb = pos.faktor_entry
    target_tb = pos.target
    f_t = ex._f(dv.faktor_split(kode)) if ada else Decimal(1)
    semua_split = _split(dv, kode) if ada else []
    pi_t = _hasil_kali(semua_split, acuan, t)  # nominal acuan → nominal t
    risiko = pos.risiko_awal_idr if pos.risiko_awal_idr > 0 else None
    peringatan: list[str] = []

    def nom_t(adj_tb: Decimal | None) -> Decimal | None:
        """Level di skala cache tanggal beli (belum dikonversi mesin) → nominal pada t."""
        if adj_tb is None:
            return None
        return ke_desimal_harga(adj_tb * f_tb / _hasil_kali(semua_split, baris.tanggal_beli, t))

    # §15.3 lonjakan tanpa data split.
    lonjakan = None
    if ada and tanpa_data_split(kode):
        lonjakan = lonjakan_tanpa_split(dv, kode, baris.tanggal_beli,
                                        float(cfg.data.validasi.lonjakan_mencurigakan_pct), anomali_ok)

    # Pemutaran ulang: PB/BO/MR dengan risiko > 0, data ada, dan bukan §15.3.
    putar: _Putar | None = None
    if ada and baris.strategi in STRATEGI_DIPUTAR and risiko is not None and lonjakan is None:
        putar = putar_ulang(pos, pasar, t, bei=bei, status_pasar_pada=status_pasar_pada)
    bar_t = (putar.bar_t if putar is not None and putar.bar_t is not None else ex.bar_hari(dv, kode)) if ada else None
    terbuka = putar is not None and putar.keluar is None

    # Nilai & P&L pada t: lembar dan harga nominal pada t; close valid terakhir ≤ t dibawa ke skala t.
    lembar_t = Decimal(baris.lembar_nominal(int(cfg.pasar.lot_lembar))) * pi_t
    harga_beli_t = ke_desimal_harga(_d(baris.harga_beli) / pi_t)
    cv = _close_valid_terakhir(dv, kode)
    close_t = None
    if cv is not None:
        tgl_c, close_adj = cv
        close_t = close_adj * ex._f(dv.faktor_split(kode, tgl_c)) / _hasil_kali(semua_split, tgl_c, t)
    nilai = lembar_t * (close_t if close_t is not None else _d(baris.harga_beli) / pi_t)
    biaya_beli = pos.biaya_beli_idr
    pnl = nilai - biaya_beli
    if terbuka:
        stop_besok, target = _nom(pos.stop, f_t), _nom(pos.target, f_t)
    else:
        stop_besok, target = ke_desimal_harga(_d(baris.stop_awal) / pi_t), nom_t(target_tb)
    sp = SaranPosisi(
        baris=baris, sektor=sektor, saran=TAHAN, nomor=7, alasan="",
        lembar=lembar_t, harga_beli=harga_beli_t, stop_besok=stop_besok, target=target,
        close=None if close_t is None else ke_desimal_harga(close_t),
        tanggal_close=cv[0] if cv is not None else None,
        perkiraan=bar_t is None or not bar_t.valid, nilai_idr=nilai, biaya_beli_idr=biaya_beli, pnl_idr=pnl,
        pnl_pct=pnl * 100 / biaya_beli if biaya_beli > 0 else None,
        risiko_awal_idr=risiko, r=None if risiko is None else pnl / risiko,
        peringatan=peringatan,
        estimasi_hasil_stop_idr=lembar_t * (stop_besok or Decimal(0)) * (1 - biaya.slip(cfg)) * (1 - biaya.fj(cfg)),
    )
    if baris.strategi in STRATEGI_DIPUTAR:
        sp.hari_ke = kal.nomor_hari(baris.tanggal_beli, t)
        n = getattr(ex._param_strategi(cfg, baris.strategi), "stop_waktu_hari", None)
        sp.stop_waktu_hari = None if n is None else int(n)

    # Split sesudah tanggal acuan (§15.3) → padanan pasca-split; peringatan data.
    if ada:
        sesudah = tuple((s, r) for s, r in semua_split if acuan < s <= t)
        if sesudah:
            sp.split = InfoSplit(sesudah, sp.lembar, sp.harga_beli, sp.stop_besok, sp.target)
        if any(s > baris.tanggal_beli for s, _r in dv.split_belum_disesuaikan(kode)):
            peringatan.append("split belum disesuaikan sumber data sejak tanggal beli: nilai dikonversi otomatis (§8.4)")
        tanda = dv.tanda(kode)
        an = str(tanda.get("anomali_terakhir", ""))[:10] if tanda is not None else ""
        if an[:2].isdigit() and an >= baris.tanggal_beli.isoformat():
            peringatan.append(f"ANOMALI data pada {an} sejak tanggal beli — periksa harga di sumber resmi")
        if bar_t is not None and bar_t.ada and bar_t.sumber.startswith("csv_manual"):
            peringatan.append("bar t dari CSV manual")

    def tetapkan(saran: str, nomor: int, alasan: str) -> SaranPosisi:
        sp.saran, sp.nomor, sp.alasan = saran, nomor, alasan
        return sp

    # 1. Data t tidak ada/rusak, atau §15.3 tidak bisa dievaluasi.
    if lonjakan is not None:
        sp.blokir = True
        return tetapkan(TIDAK_DAPAT_DIEVALUASI, 1,
                        f"lonjakan > {teks.persen(_d(cfg.data.validasi.lonjakan_mencurigakan_pct))} pada "
                        f"{lonjakan.isoformat()} tanpa data split (bar dari CSV manual tanpa berkas split); "
                        "periksa aksi korporasi lalu perbarui baris ini")
    if bar_t is None or not bar_t.valid:
        alasan = "tidak ada data saham ini" if cv is None else f"bar {t.isoformat()} tidak ada, rusak, atau tanpa transaksi"
        if cv is not None:
            alasan += f"; nilai memakai close valid terakhir {cv[0].isoformat()}"
        if baris.strategi in STRATEGI_DIPUTAR and sp.stop_besok is not None:
            alasan += f"; level stop terakhir yang valid {_h(sp.stop_besok)}"
        return tetapkan(TIDAK_DAPAT_DIEVALUASI, 1, alasan)

    # 2. MANUAL: info saja (tanpa aturan exit sistem).
    if baris.strategi == MANUAL:
        jarak = None if close_t is None or close_t <= 0 else (close_t - sp.stop_besok) * 100 / close_t
        bagian = [f"jarak ke stop_awal {_h(sp.stop_besok)}: {teks.persen(jarak, 1) if jarak is not None else teks.STRIP}"]
        if jarak is not None and jarak <= 0:
            bagian.append("harga sudah di bawah stop_awal")
        bagian.append(f"P&L belum terealisasi {_rp(pnl)} (sebelum fee jual)")
        bagian.append("tanpa aturan exit sistem")
        return tetapkan(INFO_MANUAL, 2, "; ".join(bagian))

    # 3. stop_awal ≥ harga_beli.
    if _d(baris.stop_awal) >= _d(baris.harga_beli):
        return tetapkan(JUAL, 3, "posisi sudah berada di bawah stop sejak dibeli")

    assert putar is not None
    sp.stop_hari_ini = _nom(putar.stop_hari_ini, f_t)
    k = putar.keluar
    # 4. Exit sebelum t, atau jual terjadwal di pembukaan t.
    if k is not None and (k.tanggal < t or putar.jadwal_open or k.alasan not in EXIT_ORDER_OTOMATIS):
        sp.keluar = k
        sp.stop_besok = None
        return tetapkan(JUAL, 4, f"menurut aturan, posisi seharusnya keluar pada {k.tanggal.isoformat()} "
                                 f"({k.alasan}). {TEKS_PERBARUI}")
    # 5. Exit oleh order otomatis pada t.
    if k is not None:
        sp.keluar = k
        sp.stop_besok = None
        sp.level_order = _nom(putar.level_order, f_t)
        return tetapkan(CEK_ORDER, 5, f"order otomatis seharusnya terisi hari ini di {_h(sp.level_order)} "
                                      f"({k.alasan}). Jika masih dipegang → JUAL di pembukaan t+1. {TEKS_PERBARUI}")
    # 6. Aturan berbasis close t menjadwalkan jual di t+1.
    if putar.jadwal_t is not None:
        sp.jadwal = putar.jadwal_t
        return tetapkan(JUAL, 6, _alasan_jadwal(putar.jadwal_t, pos, dv, cfg, sp.hari_ke or 0, f_t))
    # 7. TAHAN.
    bagian = [f"stop besok {_h(sp.stop_besok)}"]
    if sp.stop_hari_ini is not None and sp.stop_besok is not None and sp.stop_besok != sp.stop_hari_ini:
        bagian.append(f"naik dari {_h(sp.stop_hari_ini)}")
    if sp.target is not None:
        bagian.append(f"target {_h(sp.target)}")
    if sp.stop_waktu_hari is not None:
        bagian.append(f"hari ke-{sp.hari_ke} dari {sp.stop_waktu_hari} (stop waktu)")
    else:
        bagian.append(f"hari ke-{sp.hari_ke}")
    return tetapkan(TAHAN, 7, "; ".join(bagian))


def evaluasi_portofolio(
    pasar: "DataPasar",
    masukan: "Masukan",
    t: date,
    *,
    baris: Sequence["BarisPortofolio"] | None = None,
    tanpa_data_split: Callable[[str], bool] | None = None,
    status_pasar_pada: Callable[[date], str | None] | None = None,
) -> HasilPortofolio:
    """Saran semua baris portfolio.csv (`baris`, default semua) pada `t`, ekuitas, dan keadaan untuk seleksi."""
    cfg = pasar.cfg
    bei = AturanBEI(cfg)
    daftar = list(masukan.portofolio if baris is None else baris)
    ok = masukan.anomali_ok_set()
    tanpa = tanpa_data_split or (lambda k: False)
    saran = [
        evaluasi_baris(b, pasar, t, sektor=masukan.sektor(b.kode), bei=bei, anomali_ok=ok,
                       tanpa_data_split=tanpa, status_pasar_pada=status_pasar_pada)
        for b in daftar
    ]
    kas = _d(masukan.akun.kas_idr)
    nilai = sum((s.nilai_idr for s in saran), Decimal(0))
    blokir = [s for s in saran if s.blokir]
    teks_blokir = None
    if blokir:
        teks_blokir = ", ".join(f"{s.kode} ({s.alasan})" for s in blokir)
    keadaan = KeadaanPortofolio(
        posisi=tuple(PosisiTerbuka(s.kode, s.sektor, s.dijadwalkan_jual) for s in saran),
        kas_idr=kas,
        ekuitas_idr=kas + nilai,
        estimasi_hasil_jual_terjadwal_idr=sum(
            (s.estimasi_hasil_stop_idr for s in saran if s.dijadwalkan_jual), Decimal(0)
        ),
        blokir=teks_blokir,
    )
    hasil = HasilPortofolio(
        t=t, saran=saran, kas_idr=kas, nilai_posisi_idr=nilai, ekuitas_idr=kas + nilai,
        perkiraan=any(s.perkiraan for s in saran), keadaan=keadaan, akun_dari_berkas=masukan.akun.dari_berkas,
    )
    for s in saran:
        if s.nomor == 4 and s.keluar is not None and s.keluar.tanggal < t:
            hasil.pengingat.append(f"Posisi {s.kode} menurut data seharusnya sudah keluar "
                                   f"({s.keluar.tanggal.isoformat()}, {s.keluar.alasan}).")
    return hasil


# ---------------------------------------------------------------------------
# Pengingat: REKOMENDASI kemarin yang seharusnya terisi (§15.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RekomendasiLalu:
    """REKOMENDASI yang ditampilkan untuk hari bursa `t` (dari jurnal). Harga nominal pada tanggal sinyal."""

    kode: str
    strategi: str
    tanggal_sinyal: date
    harga_maks: Decimal
    stop: Decimal
    lot: int


def pengingat_rekomendasi(
    dv: "DataView", rekomendasi: Iterable[RekomendasiLalu], kode_dipegang: Iterable[str], cfg: "Config"
) -> list[str]:
    """"REKOMENDASI kemarin menurut data seharusnya terisi (open ≤ harga_maks), tetapi tidak ada di
    portfolio.csv." untuk setiap rekomendasi yang open `dv.as_of`-nya ≤ harga_maks (nominal, 6 desimal)."""
    bei = AturanBEI(cfg)
    dipegang = set(kode_dipegang)
    hasil = []
    for r in rekomendasi:
        if r.kode in dipegang or not dv.ada(r.kode):
            continue
        order = ex.Order(r.kode, r.strategi, "", r.tanggal_sinyal, r.lot * int(cfg.pasar.lot_lembar), r.harga_maks,
                         r.stop, None, ex._f(dv.faktor_split(r.kode, r.tanggal_sinyal)))
        b = ex.bar_hari(dv, r.kode)
        if ex.periksa_isi(order, b, bei) is None:
            hasil.append(
                f"REKOMENDASI kemarin {r.kode} ({r.strategi}, limit {_h(r.harga_maks)}) menurut data seharusnya "
                f"terisi (open {_h(ex._nom(b.open, b.faktor))} ≤ harga_maks), tetapi tidak ada di portfolio.csv."
            )
    return hasil


__all__ = [
    "BANNER_BLOKIR", "BANNER_SPLIT", "CEK_ORDER", "HasilPortofolio", "INFO_MANUAL", "InfoSplit", "JUAL",
    "RekomendasiLalu", "SaranPosisi", "TAHAN", "TIDAK_DAPAT_DIEVALUASI", "bangun_posisi", "evaluasi_baris",
    "evaluasi_portofolio", "lonjakan_tanpa_split", "pengingat_rekomendasi", "putar_ulang",
]


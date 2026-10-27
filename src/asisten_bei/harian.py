"""Perintah `harian` (SPEC §18, §8.6, §8.7, §13.2, §15.4, §17.3, §19.2–§19.3) dan `jurnal` (§17).

`harian [--jadwal] [--offline] [--tanggal D] [--tanpa-notifikasi] [--catat] [--catat-ulang]`:

1. Baca input (ERROR → kode 3). Tentukan `t` (§8.6): `--tanggal D` (replay; harus hari bursa ≤ t sekarang), atau
   hari bursa terakhir yang barnya final. `--jadwal` yang mulai sebelum batas bar final pada hari bursa menunggu
   sampai batas itu (maks `operasional.batas_waktu_run_jam`), lalu keluar 0 ("tidak ada data baru") bila jurnal
   sudah memuat run `LENGKAP` untuk `t`.
2. Pegang kunci (kode 5). Tanpa `--offline`: perbarui cache universe ∪ breadth ∪ portofolio ∪ IHSG (§8.2).
   Muat pasar as-of `t`; `--jadwal`: tunggu bila bar IHSG `t` belum ada atau cakupan universe < ambang
   (`data.tunggu_data_hari_ini`).
3. `status_data_run` (§8.6): `BELUM_TERSEDIA` (cakupan < ambang; kode 2), `TIDAK_LENGKAP` (IHSG/K0 → kode 2;
   sebagian saham tanpa bar `t` → kode 0), `LENGKAP`.
4. Status pasar, eligibilitas (`AturanUniverse.live`), sinyal strategi aktif, gerbang (`alasan_tidak_lolos_ctx`),
   saran posisi (§15.4), seleksi §13.2 (dengan ketergantungan jual terjadwal). `BELUM_TERSEDIA`: tanpa seleksi.
5. Jurnal (run biasa, atau replay dengan `--catat`/`--catat-ulang`): hari yang belum final (§17.3) — susulan
   dengan `DataView(as_of=d)` — dicatat; revisi bar ditandai; `hasil_sinyal`/`kertas_*` dihitung ulang;
   setiap tabel diekspor. Jurnal gagal → kode 6, laporan tetap dibuat dengan peringatan.
6. Laporan `output/laporan/<t>/run_<HHMMSS>/` (tanpa menimpa versi sebelumnya) + salinan terbaru di
   `output/laporan/<t>/`, termasuk `kualitas_data.csv` (§8.5). Notifikasi Telegram: tahap 7.

Kode keluar §20.1 digabung dengan `operasional.gabung_kode_keluar`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from . import DISCLAIMER, DISCLAIMER_SINGKAT, __version__, teks, waktu
from . import jurnal as jr
from .aturan_bei import AturanBEI
from .backtest import gerbang as gb
from .backtest import mesin
from .config import KonteksApp, resolusi_konteks
from .data.sumber import muat_pasar
from .data.csv_provider import CATATAN_TANPA_SPLIT
from .inputs import (
    KODE_IHSG,
    InputTidakValid,
    Masukan,
    Masalah,
    cek_portofolio_kalender,
    muat_masukan,
)
from .kalender import Kalender, banner_keterlambatan, batas_bar_final, tentukan_t, tentukan_t1
from .kondisi_pasar import hitung_kondisi_pasar
from .operasional import KODE_DATA, KODE_JURNAL, KODE_OK, gabung_kode_keluar, kunci_proses
from .portofolio import evaluasi_portofolio
from .seleksi import HasilSeleksi, KeadaanPortofolio, seleksi
from .strategi.dasar import daftar_strategi
from .universe import AturanUniverse

log = logging.getLogger("asisten_bei.harian")

LENGKAP = "LENGKAP"
TIDAK_LENGKAP = "TIDAK_LENGKAP"
BELUM_TERSEDIA = "BELUM_TERSEDIA"
STRATEGI_LIVE: tuple[str, ...] = ("PB", "BO", "MR")
PETUNJUK_LIBUR = "mungkin hari libur yang belum ada di holidays.csv"


class TanggalTidakSah(InputTidakValid):
    """`--tanggal` bukan hari bursa atau sesudah t (kode keluar 3)."""

    def __init__(self, pesan: str):
        super().__init__([Masalah("ERROR", "--tanggal", None, pesan)])


@dataclass(frozen=True)
class OpsiHarian:
    jadwal: bool = False
    offline: bool = False
    tanggal: date | None = None
    tanpa_notifikasi: bool = False
    catat: bool = False
    catat_ulang: bool = False

    @property
    def replay(self) -> bool:
        return self.tanggal is not None

    @property
    def mencatat(self) -> bool:
        """Run biasa selalu mencatat; replay `--tanggal` hanya dengan `--catat`/`--catat-ulang` (§17.3 #8)."""
        return not self.replay or self.catat or self.catat_ulang


@dataclass
class StatusData:
    """Kesegaran data §8.6."""

    status: str
    penyebut: int
    pembilang: int
    cakupan_pct: Decimal
    ihsg_ada: bool
    k0: bool
    basi: list[str] = field(default_factory=list)  # universe/portofolio dengan bar t−1 tetapi tanpa bar t
    kemungkinan_libur: bool = False
    alasan: str = ""

    @property
    def kode_keluar(self) -> int:
        if self.status == BELUM_TERSEDIA or (self.status == TIDAK_LENGKAP and (not self.ihsg_ada or self.k0)):
            return KODE_DATA
        return KODE_OK


@dataclass
class HasilHarian:
    kode_keluar: int = KODE_OK
    t: date | None = None
    status_data_run: str | None = None
    laporan: object | None = None
    folder: Path | None = None
    folder_run: Path | None = None
    berkas: dict[str, Path] = field(default_factory=dict)
    pesan: str = ""
    jurnal_gagal: str | None = None
    tidak_ada_data_baru: bool = False
    seleksi: HasilSeleksi | None = None
    catat: jr.HasilCatat | None = None


# ---------------------------------------------------------------------------
# t dan menunggu (§8.6)
# ---------------------------------------------------------------------------


def tunggu_batas_final(cfg, kal: Kalender, keluar: Callable[[str], None]) -> None:
    """`--jadwal` pada hari bursa sebelum batas bar final: tunggu sampai batas (maks `batas_waktu_run_jam`)."""
    now = waktu.sekarang()
    if not kal.adalah_hari_bursa_manual(now.date()):
        return
    batas = batas_bar_final(cfg, now.date())
    if now >= batas:
        return
    maks = timedelta(hours=float(cfg.operasional.batas_waktu_run_jam))
    tunggu = min(batas - now, maks)
    keluar(f"Menunggu batas bar final {batas:%Y-%m-%d %H:%M} WIB ({int(tunggu.total_seconds() // 60)} menit) ...")
    log.info("menunggu batas bar final %s (%.0f s)", batas.isoformat(), tunggu.total_seconds())
    waktu.tidur(tunggu.total_seconds())


def tentukan_tanggal(cfg, kal: Kalender, opsi: OpsiHarian, keluar: Callable[[str], None]) -> date:
    if opsi.jadwal and not opsi.replay:
        tunggu_batas_final(cfg, kal, keluar)
    t_kini = tentukan_t(cfg, kal, waktu.sekarang())
    if not opsi.replay:
        return t_kini
    d = opsi.tanggal
    if not kal.adalah_hari_bursa_manual(d):
        raise TanggalTidakSah(f"{d.isoformat()} bukan hari bursa (akhir pekan atau libur di holidays.csv)")
    if d > t_kini:
        raise TanggalTidakSah(f"{d.isoformat()} sesudah tanggal data terakhir yang final ({t_kini.isoformat()})")
    return d


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


def daftar_simbol_harian(masukan: Masukan) -> list[str]:
    """Simbol run harian (§8.2): universe ∪ breadth ∪ portofolio (IHSG dimuat `muat_pasar`)."""
    kode = {s.kode for s in masukan.universe} | set(masukan.breadth) | {p.kode for p in masukan.portofolio}
    kode.discard(KODE_IHSG)
    return sorted(kode)


def _valid_pada(dv, kode: str, d: date) -> bool:
    return dv.ada(kode) and dv.seri(kode).baris(d) is not None


def nilai_status_data(pasar, masukan: Masukan, t: date, kal: Kalender, kode_portofolio: Sequence[str]) -> StatusData:
    """Cakupan universe, IHSG, dan `status_data_run` (§8.6). K0 dinilai terpisah (`kondisi_pasar`)."""
    cfg = pasar.cfg
    dv = pasar.tampilan(t)
    sebelum = kal.hari_bursa_sebelum(t, manual=True)
    universe = [s.kode for s in masukan.universe]
    penyebut = [k for k in universe if _valid_pada(dv, k, sebelum)]
    pembilang = [k for k in penyebut if _valid_pada(dv, k, t)]
    if penyebut:
        cakupan = Decimal(len(pembilang)) * 100 / Decimal(len(penyebut))
    else:
        ada_t = any(_valid_pada(dv, k, t) for k in universe)
        cakupan = Decimal(100) if (ada_t or not universe) else Decimal(0)
    ihsg = dv.ihsg()
    ihsg_ada = ihsg is not None and ihsg.ada_bar_valid()
    diharapkan = sorted(set(penyebut) | {k for k in kode_portofolio if _valid_pada(dv, k, sebelum)})
    basi = [k for k in diharapkan if not _valid_pada(dv, k, t)]
    ambang = Decimal(cfg.data.validasi.min_cakupan_universe_pct)
    if cakupan < ambang:
        ihsg_kemarin = ihsg is not None and ihsg.baris(sebelum) is not None
        libur = not ihsg_ada and ihsg_kemarin
        alasan = (f"cakupan universe {len(pembilang)}/{len(penyebut)} = {cakupan:.1f}% < "
                  f"{cfg.data.validasi.min_cakupan_universe_pct}%")
        return StatusData(BELUM_TERSEDIA, len(penyebut), len(pembilang), cakupan, ihsg_ada, False, basi, libur, alasan)
    status = LENGKAP if ihsg_ada and not basi else TIDAK_LENGKAP
    alasan = "" if status == LENGKAP else (
        "bar IHSG t tidak ada" if not ihsg_ada else f"{len(basi)} saham tanpa bar t (data basi)")
    return StatusData(status, len(penyebut), len(pembilang), cakupan, ihsg_ada, False, basi, False, alasan)


def _tanpa_data_split(termuat) -> Callable[[str], bool]:
    """Seri yang memakai bar CSV manual tanpa berkas split (§6.9, §15.3)."""
    kode = {k for k, s in termuat.seri.items()
            if "csv_manual" in (s.sumber or "") and any(CATATAN_TANPA_SPLIT in c for c in s.catatan)}
    return lambda k: k in kode


# ---------------------------------------------------------------------------
# Perintah harian
# ---------------------------------------------------------------------------


def jalankan_harian(
    ctx: KonteksApp,
    opsi: OpsiHarian = OpsiHarian(),
    *,
    keluar: Callable[[str], None] = print,
    pabrik_ticker: Callable | None = None,
) -> HasilHarian:
    cfg = ctx.cfg
    masukan = muat_masukan(ctx)
    for m in masukan.daftar_peringatan():
        log.warning(str(m))
    if masukan.ada_error():
        raise InputTidakValid(masukan.daftar_error())
    kal_manual = Kalender(masukan.libur)
    t = tentukan_tanggal(cfg, kal_manual, opsi, keluar)
    hasil = HasilHarian(t=t)

    # Baris portfolio.csv terhadap t (§6.5): replay mengecualikan baris yang dibeli sesudah t.
    catatan: list[str] = []
    baris_porto = list(masukan.portofolio)
    if opsi.replay:
        sesudah = [b for b in baris_porto if b.tanggal_beli > t]
        if sesudah:
            catatan.append("replay: baris portfolio.csv yang dibeli sesudah tanggal replay tidak ikut: "
                           + ", ".join(b.kode for b in sesudah))
        baris_porto = [b for b in baris_porto if b.tanggal_beli <= t]
    galat = [m for m in cek_portofolio_kalender(baris_porto, kal_manual, t, _rel(ctx, cfg.universe.file_portfolio))
             if m.level == "ERROR"]
    if galat:
        raise InputTidakValid(galat)

    path_jurnal = ctx.path(cfg.jurnal.file)
    if opsi.jadwal and not opsi.replay and path_jurnal.exists():
        try:
            with jr.Jurnal(path_jurnal, baca_saja=True) as j:
                sudah = j.ada_run_lengkap(t)
        except jr.JurnalGagal as e:
            log.warning("jurnal tidak terbaca untuk cek run LENGKAP: %s", e)
            sudah = False
        if sudah:
            hasil.tidak_ada_data_baru = True
            hasil.pesan = f"tidak ada data baru: run LENGKAP untuk {t.isoformat()} sudah tercatat"
            keluar(f"Tidak ada data baru: run LENGKAP untuk {t.isoformat()} sudah tercatat di jurnal.")
            keluar(DISCLAIMER_SINGKAT)
            return hasil

    with kunci_proses(ctx.akar, "harian"):
        return _jalankan_terkunci(ctx, opsi, masukan, baris_porto, kal_manual, t, hasil, catatan, keluar, pabrik_ticker)


def _rel(ctx: KonteksApp, rel: str) -> str:
    p = ctx.path(rel)
    try:
        return p.resolve().relative_to(ctx.akar.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def _muat(ctx, masukan, kode, t, kal_manual, opsi, pabrik_ticker, keluar):
    sekarang = waktu.sekarang()
    info = None
    if not opsi.offline:
        from .unduh import perbarui_cache_simbol

        keluar(f"Memperbarui cache {len(kode) + 1} simbol ...")
        hu = perbarui_cache_simbol(ctx, [KODE_IHSG, *kode], kal=kal_manual, sekarang=sekarang, t=t,
                                   pabrik_ticker=pabrik_ticker)
        info = hu.pembaruan
    return muat_pasar(ctx, masukan, kode, akhir=t, t=t, sekarang=sekarang, info=info), info


def _jalankan_terkunci(ctx, opsi, masukan, baris_porto, kal_manual, t, hasil, catatan, keluar, pabrik_ticker):
    cfg = ctx.cfg
    mulai = waktu.sekarang()
    kode = daftar_simbol_harian(masukan)
    kode_porto = [b.kode for b in baris_porto]
    termuat, info = _muat(ctx, masukan, kode, t, kal_manual, opsi, pabrik_ticker, keluar)
    status = nilai_status_data(termuat.pasar, masukan, t, kal_manual, kode_porto)
    if opsi.jadwal and not opsi.replay:
        tunggu = cfg.data.tunggu_data_hari_ini
        ambang = Decimal(cfg.data.validasi.min_cakupan_universe_pct)
        for i in range(int(tunggu.jumlah)):
            if status.ihsg_ada and status.cakupan_pct >= ambang:
                break
            keluar(f"Data {t.isoformat()} belum lengkap ({status.alasan or 'bar IHSG t belum ada'}); menunggu "
                   f"{teks.angka(tunggu.jeda_menit, 0)} menit (percobaan {i + 1}/{tunggu.jumlah}) ...")
            waktu.tidur(float(tunggu.jeda_menit) * 60)
            termuat, info = _muat(ctx, masukan, kode, t, kal_manual, opsi, pabrik_ticker, keluar)
            status = nilai_status_data(termuat.pasar, masukan, t, kal_manual, kode_porto)
    pasar = termuat.pasar
    log.info("muat pasar & status data: %.1f s (%s)", (waktu.sekarang() - mulai).total_seconds(), status.status)
    dv = pasar.tampilan(t)

    # Keputusan close t.
    objek = daftar_strategi(cfg)
    aktif = [s.kode for s in objek]
    aturan_live = AturanUniverse.live(masukan)

    def keputusan_pada(d: date, aturan: AturanUniverse) -> mesin.KeputusanHari:
        return mesin.siapkan_keputusan(pasar, [d], aturan, masukan.breadth, objek)[0]

    kh = keputusan_pada(t, aturan_live)
    kondisi = kh.kondisi
    if status.status != BELUM_TERSEDIA and kondisi.aturan == "K0":
        status.k0 = True
        if status.status == LENGKAP:
            status.status = TIDAK_LENGKAP
        status.alasan = "; ".join(x for x in (status.alasan, kondisi.alasan) if x)
    hasil.status_data_run = status.status

    # Gerbang (§16.6 run harian).
    try:
        gate = gb.baca_gate_status(gb.path_gate_status(ctx)) or {}
    except gb.GateStatusRusak:
        gate = {}
    alasan_gerbang = {s: gb.alasan_tidak_lolos_ctx(ctx, s, t) for s in STRATEGI_LIVE}
    lolos = frozenset(s for s in aktif if alasan_gerbang[s] is None)

    # Portofolio (§15.4).
    status_cache: dict[date, str] = {t: kondisi.status}

    def status_pasar_pada(d: date) -> str | None:
        if not cfg.kondisi_pasar.jangan_beli_paksa_jual:
            return None
        if d not in status_cache:
            status_cache[d] = hitung_kondisi_pasar(pasar.tampilan(d), masukan.breadth).status
        return status_cache[d]

    hp = evaluasi_portofolio(pasar, masukan, t, baris=baris_porto, tanpa_data_split=_tanpa_data_split(termuat),
                             status_pasar_pada=status_pasar_pada)
    konteks = resolusi_konteks(cfg, "live")

    def seleksi_hari(k: mesin.KeputusanHari, porto: KeadaanPortofolio) -> HasilSeleksi:
        return seleksi(list(k.sinyal), status_pasar=k.status_pasar, portofolio=porto, konteks=konteks, cfg=cfg,
                       strategi_lolos=lolos, deduplikasi=True, sektor_dari=masukan.sektor, hitung_ketergantungan=True)

    hs = None if status.status == BELUM_TERSEDIA else seleksi_hari(kh, hp.keadaan)
    hasil.seleksi = hs

    # Jurnal.
    sekarang = waktu.sekarang()
    t1 = tentukan_t1(kal_manual, t)
    banner_telat = banner_keterlambatan(cfg, kal_manual, t, sekarang)
    gerbang_snapshot = {
        s: {"lolos": alasan_gerbang[s] is None and s in aktif, "alasan": alasan_gerbang[s] or "",
            "hash": str((gate.get(s) or {}).get("hash_aturan") or ""),
            "tanggal_data_terakhir": str((gate.get(s) or {}).get("tanggal_data_terakhir") or "")}
        for s in STRATEGI_LIVE
    }
    ringkasan_jurnal = None
    rekom_kemarin = []
    path_jurnal = ctx.path(cfg.jurnal.file)
    try:
        if opsi.mencatat:
            ringkasan_jurnal, hasil.catat = _proses_jurnal(
                ctx, opsi, masukan, pasar, kal_manual, t, t1, sekarang, status, kondisi, hp, keputusan_pada,
                seleksi_hari, aturan_live, gerbang_snapshot, gate, aktif, banner_telat is not None, catatan,
            )
        elif path_jurnal.exists():
            with jr.Jurnal(path_jurnal, baca_saja=True) as j:
                ringkasan_jurnal = jr.ringkas(j, t, cfg=cfg, gate=gate, path_run=ctx.path, strategi=aktif)
                ringkasan_jurnal.catatan.append("replay tanpa --catat: jurnal tidak diperbarui")
        if path_jurnal.exists():
            with jr.Jurnal(path_jurnal, baca_saja=True) as j:
                rekom_kemarin = j.rekomendasi_ditampilkan(kal_manual.hari_bursa_sebelum(t, manual=True))
    except jr.JurnalGagal as e:
        log.error("jurnal gagal: %s", e)
        hasil.jurnal_gagal = str(e)
        keluar(f"PERINGATAN: jurnal gagal — {e}")

    from .portofolio import pengingat_rekomendasi

    hp.pengingat.extend(pengingat_rekomendasi(dv, rekom_kemarin, [b.kode for b in baris_porto], cfg))

    # Laporan.
    from .laporan import bangun_laporan, tulis_laporan

    lap = bangun_laporan(
        ctx=ctx, t=t, t1=t1, sekarang=sekarang, masukan=masukan, termuat=termuat, info=info, status=status,
        kondisi=kondisi, eligibilitas=kh.eligibilitas, seleksi_t=hs, portofolio=hp, gate=gate,
        alasan_gerbang=alasan_gerbang, aktif=aktif, banner_telat=banner_telat, jurnal=ringkasan_jurnal,
        jurnal_gagal=hasil.jurnal_gagal, replay=opsi.replay, catatan=catatan, kal_manual=kal_manual,
        versi=str(__version__),
    )
    hasil.laporan = lap
    berkas, folder_run, peringatan_berkas = tulis_laporan(ctx, lap, termuat, sekarang)
    hasil.berkas, hasil.folder_run = berkas, folder_run
    hasil.folder = ctx.path(cfg.laporan.folder) / t.isoformat()
    for p in peringatan_berkas:
        keluar(f"PERINGATAN: {p}")
    if "terminal" in cfg.laporan.format:
        from .laporan.terminal import render_terminal

        for baris in render_terminal(lap):
            keluar(baris)
    keluar(f"Laporan: {_rel(ctx, str(hasil.folder))}/ (salinan run: {folder_run.name}/)")
    if not opsi.tanpa_notifikasi and cfg.notifikasi.telegram.aktif:
        keluar("Catatan: notifikasi Telegram tersedia mulai tahap 7; tidak ada pesan yang dikirim.")
    keluar(DISCLAIMER)
    kode = [status.kode_keluar]
    if hasil.jurnal_gagal:
        kode.append(KODE_JURNAL)
    hasil.kode_keluar = gabung_kode_keluar(kode)
    return hasil


def _proses_jurnal(ctx, opsi, masukan, pasar, kal_manual, t, t1, sekarang, status, kondisi, hp, keputusan_pada,
                   seleksi_hari, aturan_live, gerbang_snapshot, gate, aktif, terlambat, catatan):
    """Catat run & sinyal (§17.3), tandai revisi, hitung ulang turunan, ekspor. Mengembalikan ringkasan."""
    cfg = ctx.cfg
    with jr.Jurnal(ctx.path(cfg.jurnal.file)) as j:
        diproses = j.hari_diproses()
        if opsi.catat_ulang:
            diproses = {d: f for d, f in diproses.items() if d != t}
        hari = jr.hari_untuk_diproses(diproses, kal_manual, t, cfg.jurnal.tanggal_mulai_kertas)
        if status.status == BELUM_TERSEDIA:
            hari = []
        info_run = jr.InfoRun(
            waktu_run=sekarang, tanggal_data=t, untuk_tanggal=t1, status_data_run=status.status,
            status_pasar=kondisi.status, alasan_pasar=kondisi.alasan, hash_aturan=gb.hash_aturan_global(cfg),
            hash_input=gb.hash_input(ctx), versi_program=str(__version__), gerbang_snapshot=gerbang_snapshot,
            snapshot_input=jr.snapshot_input([(s.kode, s.sektor) for s in masukan.universe], masukan.exclude,
                                             hari or [t]),
            peringatan_data=[status.alasan] if status.alasan else [], terlambat=terlambat, catatan=list(catatan),
        )
        hasil_catat = None
        if hari or status.status == BELUM_TERSEDIA or opsi.catat_ulang:
            proses = []
            for d in hari:
                k = keputusan_pada(d, aturan_live)
                hs = seleksi_hari(k, hp.keadaan)
                proses.append(jr.HariDiproses(d, k.status_pasar, hs.kandidat, pasar.tampilan(d)))
            hasil_catat = jr.catat(j, info_run, proses, kal=kal_manual, cfg=cfg, catat_ulang=opsi.catat_ulang)
        else:
            hasil_catat = jr.HasilCatat(sudah_tercatat=True)
        n_revisi = jr.tandai_revisi(j, pasar, t)
        turunan = None
        if status.status != BELUM_TERSEDIA:
            turunan = jr.hitung_ulang_turunan(
                j, pasar, t, keputusan_pada=keputusan_pada, hash_aturan=gb.hash_aturan_global(cfg),
                konteks_kertas=resolusi_konteks(cfg, "realistis"), strategi_aktif=aktif,
                tanggal_mulai_kertas=cfg.jurnal.tanggal_mulai_kertas,
            )
        jr.ekspor(j, ctx.path(cfg.jurnal.folder_ekspor))
        ringkas = jr.ringkas(j, t, cfg=cfg, gate=gate, path_run=ctx.path, strategi=aktif, hasil_catat=hasil_catat,
                             hasil_turunan=turunan, n_revisi=n_revisi)
        return ringkas, hasil_catat


# ---------------------------------------------------------------------------
# Perintah jurnal (§17, §19.2)
# ---------------------------------------------------------------------------


@dataclass
class HasilPerintahJurnal:
    kode_keluar: int = KODE_OK
    berkas: list[Path] = field(default_factory=list)
    selisih: list[jr.Selisih] | None = None


def jalankan_jurnal(
    ctx: KonteksApp, *, ekspor: bool = False, rekonsiliasi: bool = False, keluar: Callable[[str], None] = print
) -> HasilPerintahJurnal:
    """`jurnal [--ekspor] [--rekonsiliasi]`. Tanpa opsi: ringkasan periode kertas. Selisih rekonsiliasi tetap
    kode 0 (informasi, §20.1); jurnal terkunci/rusak → kode 6."""
    cfg = ctx.cfg
    hasil = HasilPerintahJurnal()
    masukan = muat_masukan(ctx)
    if masukan.ada_error():
        raise InputTidakValid(masukan.daftar_error())
    path = ctx.path(cfg.jurnal.file)
    with kunci_proses(ctx.akar, "jurnal"):
        try:
            with jr.Jurnal(path, baca_saja=True) as j:
                runs = j.runs()
                keluar(f"Jurnal {_rel(ctx, cfg.jurnal.file)}: {len(runs)} run, {len(j.sinyal())} sinyal.")
                per = j.periode_berjalan()
                if per is not None:
                    keluar(f"Periode kertas {per['periode_id']} mulai {per['mulai']} (modal Rp {per['modal_awal']}).")
                if ekspor:
                    hasil.berkas, peringatan = jr.ekspor(j, ctx.path(cfg.jurnal.folder_ekspor))
                    for p in peringatan:
                        keluar(f"PERINGATAN: {p}")
                    keluar(f"Ekspor: {len(hasil.berkas)} berkas di {_rel(ctx, cfg.jurnal.folder_ekspor)}/")
                if rekonsiliasi:
                    hasil.selisih = _rekonsiliasi(ctx, masukan, j, keluar)
        except jr.JurnalGagal as e:
            keluar(f"ERROR jurnal: {e}")
            keluar(DISCLAIMER_SINGKAT)
            hasil.kode_keluar = KODE_JURNAL
            return hasil
    keluar(DISCLAIMER)
    return hasil


def _rekonsiliasi(ctx: KonteksApp, masukan: Masukan, j: jr.Jurnal, keluar) -> list[jr.Selisih]:
    cfg = ctx.cfg
    tanggal = [date.fromisoformat(r["tanggal_data"]) for r in j.runs()]
    if not tanggal:
        keluar("Rekonsiliasi: belum ada run di jurnal.")
        return []
    kal = Kalender(masukan.libur)
    t = min(max(tanggal), tentukan_t(cfg, kal, waktu.sekarang()))
    kode = daftar_simbol_harian(masukan)
    pasar = muat_pasar(ctx, masukan, kode, akhir=t, t=t).pasar
    objek = daftar_strategi(cfg)

    def keputusan_pada(d: date, aturan: AturanUniverse) -> mesin.KeputusanHari:
        return mesin.siapkan_keputusan(pasar, [d], aturan, masukan.breadth, objek)[0]

    selisih = jr.rekonsiliasi(j, keputusan_pada, hash_aturan=gb.hash_aturan_global(cfg), bei=AturanBEI(cfg),
                              strategi_aktif=[s.kode for s in objek], sampai=t)
    keluar(f"Rekonsiliasi sampai {t.isoformat()}: {len(selisih)} selisih.")
    for s in selisih:
        keluar(f"  {s.tanggal.isoformat()} {s.kode} {s.strategi} {s.jenis}: {s.rincian} (petunjuk: {s.petunjuk})")
    return selisih


__all__ = [
    "BELUM_TERSEDIA", "HasilHarian", "LENGKAP", "OpsiHarian", "StatusData", "TIDAK_LENGKAP", "TanggalTidakSah",
    "daftar_simbol_harian", "jalankan_harian", "jalankan_jurnal", "nilai_status_data", "tentukan_tanggal",
]


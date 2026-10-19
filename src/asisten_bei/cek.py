"""Perintah `cek` (SPEC §19.2): validasi config + input, item belum diverifikasi, kalender, holdout,
`.env`, resolusi rujukan, dan `--silang KODE TANGGAL`.

Hanya membaca: tidak mengunduh, tidak memegang kunci, tidak menulis apa pun kecuali log. Config yang
tidak valid sudah ditangani `__main__` (kode 3) sebelum modul ini dipanggil.

Kode keluar: 3 bila ada ERROR (input/silang), selain itu 0 — peringatan tidak mengubah kode keluar.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from . import DISCLAIMER, teks, waktu
from .config import KonteksApp, resolusi_konteks
from .data.kualitas import TANDA_HARI_NONBURSA
from .data.provider import ke_ts
from .data.sumber import muat_pasar, pilih_seri
from .inputs import (
    ERROR,
    KODE_IHSG,
    PERINGATAN,
    Masalah,
    cek_portofolio_kalender,
    cek_umur_akun,
    muat_masukan,
    parse_kode,
    parse_tanggal,
)
from .kalender import Kalender, bandingkan_kalender, peringatan_libur, tentukan_t
from .operasional import KODE_INPUT, KODE_OK, cek_env

log = logging.getLogger("asisten_bei.cek")

BELUM_DIVERIFIKASI = "BELUM DIVERIFIKASI"
PATH_HOLDOUT_LOG = "output/backtest/holdout_log.csv"
MAKS_PESAN_KALENDER = 20


@dataclass
class HasilCek:
    error: list[str] = field(default_factory=list)
    peringatan: list[str] = field(default_factory=list)
    belum_diverifikasi: list[str] = field(default_factory=list)

    @property
    def kode_keluar(self) -> int:
        return KODE_INPUT if self.error else KODE_OK


def _rel(ctx: KonteksApp, rel: str) -> str:
    p = ctx.path(rel)
    try:
        return p.resolve().relative_to(ctx.akar.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


# ---------------------------------------------------------------------------
# Item belum diverifikasi (§24 butir 6–10)
# ---------------------------------------------------------------------------


def _sumber(sumber: str, tanggal_cek: date | None) -> str:
    bagian = [f"sumber: {sumber or '(kosong)'}"]
    if tanggal_cek is not None:
        bagian.append(f"dicek {tanggal_cek.isoformat()}")
    return ", ".join(bagian)


def item_verifikasi(ctx: KonteksApp, hari_ini: date) -> tuple[list[str], list[str]]:
    """(belum diverifikasi, sudah diverifikasi) — teks per item config yang harus Anda cek sendiri."""
    cfg = ctx.cfg
    belum: list[str] = []
    sudah: list[str] = []
    js = cfg.pasar.jadwal_sesi
    if js.terverifikasi:
        sudah.append(f"pasar.jadwal_sesi: penutupan {js.jam_penutupan}, pra-pembukaan {js.jam_pra_pembukaan} WIB "
                     f"({_sumber(js.sumber, js.tanggal_cek)})")
    else:
        belum.append(f"pasar.jadwal_sesi — batas bar final = jadwal.jam_run {cfg.jadwal.jam_run} WIB; setiap run "
                     "pada tanggal t+1 diberi banner. Cek jadwal perdagangan di idx.co.id.")
    fr = cfg.pasar.fraksi_harga
    if fr.terverifikasi:
        catatan = f"{len(fr.tabel)} baris, berlaku mulai {fr.berlaku_mulai.isoformat()}"
        if fr.berlaku_mulai > hari_ini:
            catatan += " (belum berlaku hari ini)"
        sudah.append(f"pasar.fraksi_harga: {catatan} ({_sumber(fr.sumber, fr.tanggal_cek)})")
    else:
        belum.append("pasar.fraksi_harga — harga hanya dibulatkan ke rupiah bulat (harga limit & target ke BAWAH, "
                     "stop ke ATAS). Cek peraturan fraksi harga di idx.co.id.")
    ar = cfg.pasar.auto_reject
    if ar.terverifikasi:
        catatan = (f"ARA {len(ar.ara)} baris, ARB {len(ar.arb)} baris, acuan {ar.harga_acuan}, berlaku mulai "
                   f"{ar.berlaku_mulai.isoformat()}")
        if ar.berlaku_mulai > hari_ini:
            catatan += " (belum berlaku hari ini)"
        if ar.papan_berlaku:
            catatan += f", papan: {ar.papan_berlaku}"
        sudah.append(f"pasar.auto_reject: {catatan} ({_sumber(ar.sumber, ar.tanggal_cek)})")
    else:
        belum.append("pasar.auto_reject — batas ARA/ARB tidak dimodelkan di backtest/jurnal. Cek peraturan "
                     "auto rejection di idx.co.id.")
    b = cfg.biaya
    if b.terverifikasi:
        sudah.append(f"biaya: beli {teks.persen(b.fee_beli_pct, 2)}, jual {teks.persen(b.fee_jual_pct, 2)}, "
                     f"minimum {teks.rupiah(Decimal(b.fee_minimum_idr))}")
    else:
        belum.append(f"biaya — fee beli {teks.persen(b.fee_beli_pct, 2)}, jual {teks.persen(b.fee_jual_pct, 2)}, "
                     f"minimum {teks.rupiah(Decimal(b.fee_minimum_idr))}, pembulatan "
                     f"{b.pembulatan_fee or 'tidak ada'}: cocokkan dengan trade confirmation broker.")
    d = cfg.data
    if d.pengali_volume_terverifikasi:
        sudah.append(f"data.pengali_volume = {d.pengali_volume}")
    else:
        belum.append(f"data.pengali_volume = {d.pengali_volume} — cocokkan volume dengan sumber resmi lewat "
                     "cek --silang (SPEC §22.3 langkah 3).")
    return belum, sudah


def _peringatan_config(ctx: KonteksApp) -> list[str]:
    """Kombinasi config yang sah tetapi patut diperiksa."""
    cfg = ctx.cfg
    hasil = []
    ar = cfg.pasar.auto_reject
    if ar.terverifikasi and ar.ara:
        tertinggi = max(Decimal(b.batas_pct) for b in ar.ara)
        lonjakan = Decimal(cfg.data.validasi.lonjakan_mencurigakan_pct)
        if lonjakan <= tertinggi:
            hasil.append(
                f"data.validasi.lonjakan_mencurigakan_pct ({lonjakan}) tidak di atas batas ARA tertinggi "
                f"({tertinggi}%): gerakan sah satu hari bisa dianggap anomali"
            )
    return hasil


# ---------------------------------------------------------------------------
# Holdout (§16.4)
# ---------------------------------------------------------------------------


def cek_holdout(ctx: KonteksApp) -> list[str]:
    """Peringatan bila `backtest.holdout_mulai` berbeda dari baris pertama `holdout_log.csv`."""
    path = ctx.path(PATH_HOLDOUT_LOG)
    if not path.is_file():
        return []
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            baris = [b for b in f if not b.startswith("#")]
        data = list(csv.DictReader(baris))
    except (OSError, csv.Error) as e:
        return [f"{_rel(ctx, PATH_HOLDOUT_LOG)} tidak terbaca: {e}"]
    if not data or "holdout_mulai" not in data[0]:
        return []
    pertama = (data[0].get("holdout_mulai") or "").strip()
    sekarang = ctx.cfg.backtest.holdout_mulai.isoformat()
    if pertama and pertama != sekarang:
        return [
            f"backtest.holdout_mulai {sekarang} berbeda dari nilai pertama di {_rel(ctx, PATH_HOLDOUT_LOG)} "
            f"({pertama}): holdout yang sudah pernah dilihat tidak lagi murni (SPEC §16.4)"
        ]
    return []


# ---------------------------------------------------------------------------
# --silang (§22.3 langkah 3)
# ---------------------------------------------------------------------------


def _angka(x: float, desimal: int = 2) -> str:
    return teks.angka(x, desimal)


def silang(ctx: KonteksApp, kode_teks: str, tanggal_teks: str, keluar: Callable[[str], None], hasil: HasilCek) -> None:
    try:
        kode = parse_kode(kode_teks)
        d = parse_tanggal(tanggal_teks)
    except ValueError as e:
        hasil.error.append(f"--silang: {e}")
        keluar(f"ERROR --silang: {e}")
        return
    masukan = muat_masukan(ctx)
    sekarang = waktu.sekarang()
    t = tentukan_t(ctx.cfg, Kalender(masukan.libur), sekarang)
    keluar(f"Cek silang {kode} {d.isoformat()} (t = {t.isoformat()})")
    if d > t:
        hasil.error.append(f"--silang: tanggal {d.isoformat()} sesudah t ({t.isoformat()})")
        keluar(f"ERROR: tanggal {d.isoformat()} sesudah tanggal data terakhir yang final (t = {t.isoformat()})")
        return
    termuat = muat_pasar(ctx, masukan, [kode], akhir=t, t=t, sekarang=sekarang)
    s = termuat.seri[kode]
    keluar(f"  Seri: status {s.status}, sumber {s.sumber or '-'}"
           + (f" ({'; '.join(s.catatan)})" if s.catatan else ""))
    if not s.ada_data:
        keluar(f"  Tidak ada data untuk {kode}: {s.pesan or 'tidak ada data'}. Jalankan unduh dulu.")
        return
    ds = termuat.data[kode]
    ts = ke_ts(d)  # index seri = Timestamp tengah malam; datetime.date tidak pernah sama dengan Timestamp
    if ts not in s.bar.index:
        keluar(f"  Tidak ada bar {kode} pada {d.isoformat()} di seri terpilih (tidak dicari tanggal terdekat).")
        return
    bar = s.bar.loc[ts]
    if ts in ds.mentah.index:
        tanda = str(ds.mentah.loc[ts, "tanda_bar"]) or "valid"
    else:
        tanda = f"{TANDA_HARI_NONBURSA} (dibuang: bukan hari bursa menurut kalender)"
    dv = termuat.pasar.tampilan(d)
    f = dv.faktor_split(kode)
    pengali = float(ctx.cfg.data.pengali_volume)
    tak_ikut = set() if ds.split_faktor is None else set(ds.split.index) - set(ds.split_faktor.index)
    sesudah = [
        f"{i.date().isoformat()} ×{r:g}"
        + (" (belum disesuaikan sumber: tidak ikut F; harga cache sebelum tanggal ini sudah nominal — "
           "SPLIT_BELUM_DISESUAIKAN)" if i in tak_ikut else "")
        for i, r in s.split.items() if i.date() > d
    ]
    judul = "Bar cache (tersesuaikan split" + (", kecuali split belum disesuaikan sumber)" if tak_ikut else ")")
    keluar(f"  {judul}: open {_angka(bar['open'])}, high {_angka(bar['high'])}, "
           f"low {_angka(bar['low'])}, close {_angka(bar['close'])}, volume {_angka(bar['volume'], 0)}")
    keluar(f"  Sumber bar: {bar.get('sumber', '') or '-'}; tanda bar: {tanda}")
    keluar(f"  F({d.isoformat()}) = {f:g}" + (f" (split sesudahnya: {', '.join(sesudah)})" if sesudah else " (tidak ada split sesudahnya)"))
    if kode != KODE_IHSG:
        vol_nom = float(bar["volume"]) / f
        keluar(f"  Nominal: open {_angka(bar['open'] * f)}, high {_angka(bar['high'] * f)}, low {_angka(bar['low'] * f)}, "
               f"close {_angka(bar['close'] * f)}, volume {_angka(vol_nom * pengali, 0)} lembar "
               f"(volume ÷ F × pengali_volume {pengali:g})")
        keluar(f"  Nilai transaksi ≈ {teks.rupiah(Decimal(repr(float(bar['close']) * float(bar['volume']) * pengali)))}")
    keluar("  Bandingkan dengan ringkasan perdagangan resmi (idx.co.id atau aplikasi broker). Harga harus sama.")


# ---------------------------------------------------------------------------
# cek
# ---------------------------------------------------------------------------


def _cetak_masalah(ms: list[Masalah], hasil: HasilCek, keluar: Callable[[str], None]) -> None:
    for m in ms:
        if m.level == ERROR:
            hasil.error.append(str(m))
        elif m.level == PERINGATAN:
            hasil.peringatan.append(str(m))
        keluar(f"  {m}")


def jalankan_cek(
    ctx: KonteksApp,
    *,
    silang_kode: tuple[str, str] | None = None,
    keluar: Callable[[str], None] = print,
) -> HasilCek:
    cfg = ctx.cfg
    hasil = HasilCek()
    sekarang = waktu.sekarang()
    hari_ini = waktu.ke_wib(sekarang).date()

    keluar(f"Config: {ctx.path_config} — valid (versi_config {cfg.versi_config}); akar: {ctx.akar}")
    for p in _peringatan_config(ctx):
        hasil.peringatan.append(p)
        keluar(f"  PERINGATAN config: {p}")

    # 2. Input.
    keluar("")
    keluar("Input:")
    masukan = muat_masukan(ctx)
    kal = Kalender(masukan.libur)
    t = tentukan_t(cfg, kal, sekarang)
    ms = list(masukan.masalah)
    ms += cek_portofolio_kalender(masukan.portofolio, kal, t, _rel(ctx, cfg.universe.file_portfolio))
    umur = cek_umur_akun(masukan.akun, kal, hari_ini, cfg.akun.maks_umur_hari_bursa, _rel(ctx, cfg.akun.file_akun))
    if umur is not None:
        ms.append(umur)
    if ms:
        _cetak_masalah(ms, hasil, keluar)
    else:
        keluar("  semua berkas input sah")
    keluar(f"  universe {len(masukan.universe)} saham, breadth {len(masukan.breadth)}, portofolio "
           f"{len(masukan.portofolio)} posisi, exclude {len(masukan.exclude)} baris, libur {len(masukan.libur)} tanggal"
           + ("" if masukan.riwayat is None else f", riwayat konstituen {len(masukan.riwayat)} baris"))
    keluar(f"  tanggal data t = {t.isoformat()} (sekarang {sekarang.isoformat(timespec='minutes')} WIB)")

    # 3. Belum diverifikasi.
    keluar("")
    belum, sudah = item_verifikasi(ctx, hari_ini)
    keluar(f"Item {BELUM_DIVERIFIKASI} ({len(belum)}):" if belum else "Semua item aturan BEI, biaya, dan volume sudah diverifikasi.")
    for b in belum:
        hasil.belum_diverifikasi.append(b)
        keluar(f"  {BELUM_DIVERIFIKASI}: {b}")
    for b in sudah:
        keluar(f"  terverifikasi: {b}")

    # 4. Kalender.
    keluar("")
    keluar("Kalender:")
    for p in peringatan_libur(kal, sekarang):
        hasil.peringatan.append(p.teks)
        keluar(f"  {p.level}: {p.teks}")
    if not masukan.breadth:
        keluar("  perbandingan kalender dilewati: breadth_universe.csv kosong")
    else:
        breadth = {}
        for k in masukan.breadth:
            s = pilih_seri(ctx, k, t=t, sekarang=sekarang)
            if s.ada_data:
                breadth[k] = s.bar
        if not breadth:
            keluar("  perbandingan kalender dilewati: belum ada data cache breadth (jalankan unduh dulu)")
        else:
            basi = sorted(k for k, bar in breadth.items() if bar.index[-1] < ke_ts(t))
            if basi:
                # Saham yang datanya berakhir sebelum t tidak ikut menilai tanggal sesudahnya (§8.7).
                p = (f"cache breadth belum sampai t ({t.isoformat()}) untuk {len(basi)} dari {len(breadth)} saham: "
                     + ", ".join(basi[:10]) + (" ..." if len(basi) > 10 else "") + " — jalankan unduh")
                hasil.peringatan.append(p)
                keluar(f"  PERINGATAN: {p}")
            pesan = bandingkan_kalender(kal, breadth, float(cfg.data.validasi.min_hari_kalender_historis_pct), t)
            if not pesan:
                keluar(f"  kalender manual cocok dengan data breadth ({len(breadth)} saham)")
            for p in pesan[:MAKS_PESAN_KALENDER]:
                hasil.peringatan.append(p)
                keluar(f"  PERINGATAN: {p}")
            if len(pesan) > MAKS_PESAN_KALENDER:
                keluar(f"  ... dan {len(pesan) - MAKS_PESAN_KALENDER} ketidakcocokan lain")

    # 5. Holdout.
    keluar("")
    keluar(f"Backtest: holdout_mulai {cfg.backtest.holdout_mulai.isoformat()} (tetapkan sekali; jangan digeser)")
    for p in cek_holdout(ctx):
        hasil.peringatan.append(p)
        keluar(f"  PERINGATAN: {p}")

    # 6. .env.
    keluar("")
    keluar("Telegram/.env:")
    env = cek_env(ctx.akar, cfg.notifikasi.telegram.aktif)
    if not env:
        keluar("  " + ("siap" if cfg.notifikasi.telegram.aktif else "notifikasi Telegram nonaktif"))
    for level, pesan in env:
        (hasil.error if level == ERROR else hasil.peringatan).append(pesan)
        keluar(f"  {level}: {pesan}")

    # 7. Resolusi rujukan.
    keluar("")
    keluar("Resolusi rujukan (§7):")
    for nama in ("live", "edge", "realistis"):
        k = resolusi_konteks(cfg, nama)
        keluar(f"  {k.uraian()} (pada modal awal: {teks.rupiah(k.alokasi_maks(k.modal_idr))})")

    if silang_kode is not None:
        keluar("")
        silang(ctx, silang_kode[0], silang_kode[1], keluar, hasil)

    keluar("")
    keluar(f"Hasil cek: {len(hasil.error)} ERROR, {len(hasil.peringatan)} PERINGATAN, "
           f"{len(hasil.belum_diverifikasi)} {BELUM_DIVERIFIKASI}")
    if hasil.error:
        keluar("Perbaiki semua ERROR di atas, lalu jalankan cek lagi.")
    keluar("")
    keluar(DISCLAIMER)
    log.info("cek selesai: %d ERROR, %d PERINGATAN", len(hasil.error), len(hasil.peringatan))
    return hasil

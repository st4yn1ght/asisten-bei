"""Perintah `unduh` (SPEC §19.2, §8.1–§8.5).

1. Baca input; ERROR → kode keluar 3 (tidak ada unduhan dengan daftar simbol yang salah).
2. Pegang kunci proses (§19.3); kunci dipegang proses lain → `KunciDipegang` (kode 5, oleh `__main__`).
3. Untuk setiap provider jaringan di `data.provider_urutan` (biasanya yfinance) dan setiap simbol
   (universe ∪ breadth ∪ portofolio ∪ universe_history ∪ IHSG, atau `--simbol`): perbarui cache
   (§8.3) lewat satu `KebijakanAmbil` per provider (§8.1). Seri cache yang memuat split belum
   disesuaikan sumber diunduh ulang penuh (§8.4) — juga tanpa `--penuh`.
4. Nilai seri terpilih (§8.2) as-of `t` (§8.6), tulis berkas kualitas
   `output/kualitas/<YYYYMMDD-HHMMSS>_unduh.csv`, dan cetak ringkasan.

Kegagalan per simbol tidak mengubah kode keluar (0); semuanya dilaporkan dengan pesan error asli.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from . import DISCLAIMER, waktu
from .config import KonteksApp
from .data.cache import HasilPembaruan, perbarui_cache
from .data.kualitas import path_berkas_kualitas, tulis_berkas_kualitas, validasi_bar, validasi_struktur
from .data.split import split_belum_disesuaikan
from .data.sumber import (
    PESAN_DIBLOKIR,
    PROVIDER_LOKAL,
    KebijakanAmbil,
    baris_kualitas,
    buat_provider,
    cache_provider,
    muat_pasar,
)
from .inputs import KODE_IHSG, InputTidakValid, Masukan, muat_masukan, parse_kode
from .kalender import Kalender, tentukan_t
from .operasional import KODE_OK, kunci_proses

log = logging.getLogger("asisten_bei.unduh")

AMBANG_SARAN_VERSI_PCT = 90  # §8.1: ≥ 90% simbol FORMAT → sarankan cek versi yfinance


def daftar_simbol(masukan: Masukan) -> list[str]:
    """Simbol `unduh` (§8.2): IHSG lalu universe ∪ breadth ∪ portofolio ∪ universe_history (urut)."""
    kode = {s.kode for s in masukan.universe} | set(masukan.breadth) | {p.kode for p in masukan.portofolio}
    if masukan.riwayat is not None:
        kode |= {r.kode for r in masukan.riwayat}
    kode.discard(KODE_IHSG)
    return [KODE_IHSG, *sorted(kode)]


def parse_daftar_simbol(teks: str) -> list[str]:
    """`--simbol K1,K2` → daftar kode (huruf besar, tanpa .JK). Kode tidak sah → ValueError."""
    hasil: list[str] = []
    for bagian in teks.split(","):
        bagian = bagian.strip()
        if not bagian:
            continue
        kode = parse_kode(bagian)
        if kode not in hasil:
            hasil.append(kode)
    if not hasil:
        raise ValueError("--simbol kosong")
    return hasil


def _perlu_unduh_penuh(isi, kode: str, kal: Kalender) -> bool:
    """Seri cache memuat split yang belum disesuaikan sumber (§8.4) → unduh ulang penuh sampai lolos."""
    if isi.bar is None or len(isi.bar) == 0 or kode == KODE_IHSG:
        return False
    st = validasi_struktur(isi.bar)
    if not st.ok:
        return False  # struktur rusak ditangani perbarui_cache (diperlakukan apa adanya)
    seri = validasi_bar(st.bar, None, kode, False, kal)
    return bool(split_belum_disesuaikan(seri.valid, seri.split))


@dataclass
class HasilUnduh:
    t: date
    simbol: list[str]
    pembaruan: dict[str, dict[str, HasilPembaruan]] = field(default_factory=dict)  # kode → provider → hasil
    penuh_karena_split: list[str] = field(default_factory=list)
    diblokir: dict[str, list[str]] = field(default_factory=dict)  # provider → kode yang tidak diminta
    path_kualitas: object | None = None
    status: Counter = field(default_factory=Counter)
    peringatan: list[str] = field(default_factory=list)
    kode_keluar: int = KODE_OK


def jalankan_unduh(
    ctx: KonteksApp,
    *,
    penuh: bool = False,
    simbol: Sequence[str] | None = None,
    pabrik_ticker: Callable | None = None,
    keluar: Callable[[str], None] = print,
    tidur: Callable[[float], None] | None = None,
) -> HasilUnduh:
    """Jalankan `unduh`. `pabrik_ticker`/`tidur` hanya untuk uji (mock yfinance, tanpa jeda nyata)."""
    cfg = ctx.cfg
    masukan = muat_masukan(ctx)
    for m in masukan.daftar_peringatan():
        log.warning(str(m))
    if masukan.ada_error():
        raise InputTidakValid(masukan.daftar_error())

    sekarang = waktu.sekarang()
    kal_manual = Kalender(masukan.libur)
    t = tentukan_t(cfg, kal_manual, sekarang)
    semua = daftar_simbol(masukan)
    if simbol:
        luar = [k for k in simbol if k not in semua]
        for k in luar:
            keluar(f"PERINGATAN: {k} tidak ada di universe/breadth/portofolio/riwayat; tetap diunduh atas permintaan")
        daftar = list(simbol)
    else:
        daftar = semua
    hasil = HasilUnduh(t=t, simbol=daftar)

    with kunci_proses(ctx.akar, "unduh"):
        log.info("unduh: %d simbol, t = %s, penuh = %s", len(daftar), t.isoformat(), penuh)
        keluar(f"Mengunduh {len(daftar)} simbol (t = {t.isoformat()}){' — unduh ulang penuh' if penuh else ''} ...")
        for nama in cfg.data.provider_urutan:
            if nama in PROVIDER_LOKAL:
                continue
            _unduh_provider(ctx, nama, daftar, penuh, kal_manual, sekarang, hasil, pabrik_ticker, tidur)

        termuat = muat_pasar(ctx, masukan, daftar, akhir=t, t=t, sekarang=sekarang, info=hasil.pembaruan)
        baris = baris_kualitas(termuat, daftar, t)
        hasil.status = Counter(termuat.seri[k].status for k in daftar)
        hasil.path_kualitas = tulis_berkas_kualitas(
            path_berkas_kualitas(ctx.akar, "unduh", sekarang), baris, hasil.peringatan
        )
        _cetak_ringkasan(ctx, hasil, termuat, baris, keluar)
    return hasil


def _unduh_provider(
    ctx: KonteksApp,
    nama: str,
    daftar: Iterable[str],
    penuh: bool,
    kal: Kalender,
    sekarang: datetime,
    hasil: HasilUnduh,
    pabrik_ticker: Callable | None,
    tidur: Callable[[float], None] | None,
) -> None:
    cfg = ctx.cfg
    provider = buat_provider(nama, ctx, pabrik_ticker=pabrik_ticker)
    cache = cache_provider(ctx, nama)
    kebijakan = KebijakanAmbil(cfg) if tidur is None else KebijakanAmbil(cfg, tidur=tidur)
    versi = getattr(provider, "versi_provider", "")
    for kode in daftar:
        sebelum_blokir = kebijakan.diblokir
        penuh_k = penuh
        if not penuh_k and _perlu_unduh_penuh(cache.baca(kode), kode, kal):
            penuh_k = True
            hasil.penuh_karena_split.append(kode)
        h = perbarui_cache(
            lambda k, m, a: kebijakan.ambil(provider, k, m, a),
            cache,
            kode,
            cfg=cfg,
            kal=kal,
            sekarang=sekarang,
            penuh=penuh_k,
            adalah_indeks=(kode == KODE_IHSG),
            versi_provider=versi,
        )
        if kode in hasil.penuh_karena_split:
            h.catatan.insert(0, "split belum disesuaikan sumber di cache: unduh ulang penuh")
        if sebelum_blokir:
            hasil.diblokir.setdefault(nama, []).append(kode)
        hasil.pembaruan.setdefault(kode, {})[nama] = h
        _log_pembaruan(nama, h)


def _log_pembaruan(nama: str, h: HasilPembaruan) -> None:
    if h.status == "GAGAL":
        log.warning("%s %s GAGAL %s: %s", nama, h.kode, h.jenis_error, h.pesan)
    else:
        log.info("%s %s %s%s", nama, h.kode, h.status, " (unduh penuh)" if h.unduh_penuh else "")
    for c in h.catatan:
        log.info("%s %s: %s", nama, h.kode, c)


def _cetak_ringkasan(ctx: KonteksApp, hasil: HasilUnduh, termuat, baris, keluar: Callable[[str], None]) -> None:
    n = len(hasil.simbol)
    st = hasil.status
    keluar("")
    keluar(f"Ringkasan unduh (t = {hasil.t.isoformat()}): {n} simbol — OK {st.get('OK', 0)}, "
           f"SEBAGIAN {st.get('SEBAGIAN', 0)}, GAGAL {st.get('GAGAL', 0)}")
    for nama, kode in hasil.diblokir.items():
        keluar(f"PERINGATAN: {nama} {PESAN_DIBLOKIR}; {len(kode)} simbol tidak diminta dan memakai cache/CSV: "
               + ", ".join(kode[:20]) + (" ..." if len(kode) > 20 else ""))
    jenis = Counter()
    for kode in hasil.simbol:
        for h in hasil.pembaruan.get(kode, {}).values():
            if h.status == "GAGAL" and not h.dilewati:
                jenis[h.jenis_error] += 1
    if n and jenis.get("FORMAT", 0) * 100 >= AMBANG_SARAN_VERSI_PCT * n:
        keluar("PERINGATAN: hampir semua simbol gagal FORMAT — periksa versi yfinance (SPEC §22.3) sebelum upgrade/downgrade.")
    if hasil.penuh_karena_split:
        keluar("Unduh ulang penuh karena split belum disesuaikan sumber: " + ", ".join(hasil.penuh_karena_split))
    for kode in hasil.simbol:
        s = termuat.seri[kode]
        if s.status == "OK":
            continue
        rinci = "; ".join(x for x in (s.pesan, *s.catatan) if x) or "tanpa keterangan"
        keluar(f"  {s.status:8s} {kode}: {rinci}")
    alasan = Counter(b.alasan_dikeluarkan for b in baris if b.alasan_dikeluarkan)
    if alasan:
        keluar("Dikeluarkan karena data (as-of t): " + ", ".join(f"{k} {v}" for k, v in sorted(alasan.items())))
    for p in hasil.peringatan:
        keluar(f"PERINGATAN: {p}")
    keluar(f"Berkas kualitas: {hasil.path_kualitas}")
    if not ctx.cfg.data.pengali_volume_terverifikasi:
        keluar("Catatan: data.pengali_volume belum diverifikasi — jalankan cek --silang (SPEC §22.3 langkah 3).")
    keluar("")
    keluar(DISCLAIMER)

"""Perintah `backtest` (SPEC §16, §19.2 `backtest [--strategi pb,bo,mr,acak] [--mode edge,realistis]`).

Urutan:

1. Baca input; ERROR → `InputTidakValid` (kode keluar 3, oleh `__main__`).
2. Pegang kunci proses (§19.3); dipegang proses lain → `KunciDipegang` (kode 5, oleh `__main__`).
3. Muat pasar dari cache (`data.sumber.muat_pasar`, seri terpilih §8.2) untuk universe ∪ universe_history ∪
   breadth (+ IHSG), dengan tanda kualitas data dihitung sampai `t` (§8.6) untuk semua simbol. Akhir data
   backtest (§16.1) = `backtest.selesai`, atau hari bursa terakhir ≤ t yang punya bar IHSG valid. Berkas
   kualitas `output/kualitas/<YYYYMMDD-HHMMSS>_backtest.csv` dinilai as-of akhir data.
4. Keputusan harian (status pasar, eligibilitas `AturanUniverse.backtest`, sinyal, skor) dihitung sekali
   (`mesin.siapkan_keputusan`) untuk semua strategi yang diperlukan — PB/BO/MR/ACAK dibangun langsung, juga
   yang `aktif: false`, karena run per strategi tetap menjalankannya.
5. Run (§16.1 "Run yang dijalankan", §16.3): per strategi yang diminta × mode yang diminta; tanpa
   `--strategi` juga `gabungan_aktif` (strategi `aktif`, tanpa ACAK) dan `gabungan_lolos` (strategi aktif yang
   lolos gerbang edge pada run yang sama; memerlukan mode edge, dilewati dengan catatan bila tidak ada).
   Identitas akuntansi gagal → kode 1, tanpa ringkasan (tidak ada keluaran parsial yang menyesatkan).
6. Gerbang (PB/BO/MR, mode edge), subset terjangkau, jalur per sinyal, log holdout, `gate_status.json`
   (digabung, hanya dengan mode edge), dan semua berkas folder run (`laporan_bt`).

Waktu hanya dari `waktu.sekarang()`; satu `saat` (waktu mulai) menamai folder run, berkas kualitas, baris log
holdout, dan `dibuat_pada`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .. import DISCLAIMER, __version__, waktu
from .. import exit as ex
from ..config import KonteksApp, resolusi_konteks
from ..data.kualitas import path_berkas_kualitas, tulis_berkas_kualitas
from ..data.sumber import baris_kualitas, muat_pasar
from ..inputs import KODE_IHSG, InputTidakValid, Masukan, muat_masukan
from ..kalender import Kalender, tentukan_t
from ..operasional import KODE_DATA, KODE_ERROR, KODE_OK, kunci_proses
from ..strategi.dasar import URUTAN_STRATEGI, Strategi
from ..universe import AturanUniverse
from . import gerbang as gb
from . import jendela as jd
from . import laporan_bt as lb
from . import mesin
from . import metrik as mt

log = logging.getLogger("asisten_bei.backtest")

STRATEGI_CLI: tuple[str, ...] = ("pb", "bo", "mr", "acak")  # enum strategi, huruf kecil (§4.3)
MODE_CLI: tuple[str, ...] = ("edge", "realistis")  # enum mode (§4.3)
STRATEGI_GABUNGAN: tuple[str, ...] = ("PB", "BO", "MR")  # ACAK tidak pernah ikut run gabungan (§12.4)
LANGKAH_KEMAJUAN_PCT = 10


# ---------------------------------------------------------------------------
# Opsi CLI
# ---------------------------------------------------------------------------


def _parse_daftar(teks: str, sah: Sequence[str], nama: str) -> tuple[str, ...]:
    bagian = [b.strip().lower() for b in str(teks).split(",")]
    bagian = [b for b in bagian if b]
    if not bagian:
        raise ValueError(f"daftar {nama} kosong (pilihan: {','.join(sah)})")
    salah = [b for b in bagian if b not in sah]
    if salah:
        raise ValueError(f"{nama} tidak dikenal: {', '.join(salah)} (pilihan: {','.join(sah)})")
    return tuple(x for x in sah if x in set(bagian))  # urutan kanonik, tanpa duplikat


def parse_daftar_strategi(teks: str) -> tuple[str, ...]:
    """`--strategi pb,bo,mr,acak` → kode strategi huruf kecil, urutan kanonik. Nilai tak dikenal → ValueError."""
    return _parse_daftar(teks, STRATEGI_CLI, "strategi")


def parse_daftar_mode(teks: str) -> tuple[str, ...]:
    """`--mode edge,realistis` → mode, urutan kanonik. Nilai tak dikenal → ValueError."""
    return _parse_daftar(teks, MODE_CLI, "mode")


# ---------------------------------------------------------------------------
# Pembantu
# ---------------------------------------------------------------------------


def daftar_simbol_backtest(masukan: Masukan) -> list[str]:
    """Saham yang dimuat backtest: universe ∪ universe_history ∪ breadth (IHSG dimuat `muat_pasar`)."""
    kode = {s.kode for s in masukan.universe} | set(masukan.breadth)
    if masukan.riwayat is not None:
        kode |= {r.kode for r in masukan.riwayat}
    kode.discard(KODE_IHSG)
    return sorted(kode)


def _buat_strategi(cfg, kode: Sequence[str]) -> list[Strategi]:
    """Objek strategi untuk `kode` (urutan PB, BO, MR, ACAK), tanpa memandang `aktif`."""
    from ..strategi.acak import Acak
    from ..strategi.breakout import Breakout
    from ..strategi.oversold import Oversold
    from ..strategi.pullback import Pullback

    kelas = {"PB": Pullback, "BO": Breakout, "MR": Oversold, "ACAK": Acak}
    return [kelas[k](cfg) for k in URUTAN_STRATEGI if k in set(kode)]


def tentukan_akhir(cfg, pasar, t: date) -> tuple[date | None, str | None]:
    """Akhir data backtest (§16.1) dan catatan bila ada penyesuaian. None = tidak ada bar IHSG valid ≤ t."""
    ihsg = pasar.tampilan(t).ihsg()
    valid = ihsg.valid() if ihsg is not None else None
    akhir_ihsg = valid.index[-1].date() if valid is not None and len(valid) else None
    selesai = cfg.backtest.selesai
    if selesai is None or akhir_ihsg is None:  # tanpa bar IHSG valid sama sekali: tidak ada yang bisa dinilai
        return akhir_ihsg, None
    if selesai > t:
        catatan = (f"backtest.selesai {selesai.isoformat()} sesudah tanggal data t {t.isoformat()}; akhir data "
                   "memakai hari bursa terakhir ≤ t yang punya bar IHSG valid")
        return akhir_ihsg, catatan
    kal = pasar.kalender
    akhir = selesai if kal.adalah_hari_bursa(selesai) else kal.hari_bursa_sebelum(selesai)
    return akhir, None


@dataclass
class HasilBacktest:
    kode_keluar: int = KODE_OK
    t: date | None = None
    akhir: date | None = None
    folder_run: Path | None = None
    path_kualitas: Path | None = None
    berkas: dict[str, Path] = field(default_factory=dict)
    laporan: lb.LaporanBacktest | None = None
    pesan: str = ""


def _kemajuan(keluar: Callable[[str], None], label: str) -> Callable[[int, int], None]:
    batas = [0]

    def lapor(i: int, n: int) -> None:
        pct = i * 100 // n if n else 100
        if pct >= batas[0] or i == n:
            keluar(f"  {label}: {i}/{n} hari ({pct}%)")
            batas[0] = (pct // LANGKAH_KEMAJUAN_PCT + 1) * LANGKAH_KEMAJUAN_PCT

    return lapor


def _durasi(langkah: str, mulai: datetime) -> None:
    log.info("langkah %s: %.1f s", langkah, (waktu.sekarang() - mulai).total_seconds())


# ---------------------------------------------------------------------------
# Perintah
# ---------------------------------------------------------------------------


def jalankan_backtest(
    ctx: KonteksApp,
    *,
    strategi: Sequence[str] | None = None,
    mode: Sequence[str] | None = None,
    keluar: Callable[[str], None] = print,
) -> HasilBacktest:
    """Jalankan perintah backtest. `strategi`/`mode`: hasil `parse_daftar_*` (None = semua). Kode keluar §20.1:
    0 OK; 1 identitas akuntansi gagal; 2 tidak ada data untuk dibacktest. Input tidak valid dan kunci dipegang
    dilempar sebagai exception (`__main__` → 3 / 5)."""
    cfg = ctx.cfg
    per_strategi = tuple(s.upper() for s in (strategi or STRATEGI_CLI))
    gabungan = not strategi
    modes = tuple(mode or MODE_CLI)
    teks_config = ctx.path_config.read_text(encoding="utf-8-sig") if ctx.path_config.is_file() else ""

    masukan = muat_masukan(ctx)
    for m in masukan.daftar_peringatan():
        log.warning(str(m))
    if masukan.ada_error():
        raise InputTidakValid(masukan.daftar_error())

    sekarang = waktu.sekarang()
    t = tentukan_t(cfg, Kalender(masukan.libur), sekarang)
    hasil = HasilBacktest(t=t)
    with kunci_proses(ctx.akar, "backtest"):
        log.info("backtest: strategi %s, mode %s, gabungan %s, t = %s", per_strategi, modes, gabungan, t)
        keluar(f"Backtest: strategi {','.join(s.lower() for s in per_strategi)}; mode {','.join(modes)}"
               f"{'; termasuk run gabungan' if gabungan else ''} (t = {t.isoformat()})")
        mulai = waktu.sekarang()
        kode = daftar_simbol_backtest(masukan)
        termuat = muat_pasar(ctx, masukan, kode, akhir=t, t=t, sekarang=sekarang)
        pasar = termuat.pasar
        akhir, catatan_akhir = tentukan_akhir(cfg, pasar, t)
        _durasi("muat pasar", mulai)

        peringatan: list[str] = []
        hasil.path_kualitas = tulis_berkas_kualitas(
            path_berkas_kualitas(ctx.akar, "backtest", sekarang),
            baris_kualitas(termuat, [KODE_IHSG, *kode], akhir or t), peringatan,
        )
        if akhir is None:
            return _tanpa_data(hasil, keluar, f"tidak ada bar IHSG valid sampai {t.isoformat()} di cache — "
                                              "jalankan unduh.bat terlebih dahulu")
        hasil.akhir = akhir
        tanggal_awal, hari = mesin.hari_simulasi(pasar, cfg.backtest.mulai, akhir)
        if not hari:
            return _tanpa_data(hasil, keluar, f"akhir data {akhir.isoformat()} sebelum backtest.mulai "
                                              f"{cfg.backtest.mulai.isoformat()}")

        aktif = tuple(k for k in STRATEGI_GABUNGAN if getattr(cfg.strategi, k.lower()).aktif)
        perlu = set(per_strategi) | (set(aktif) if gabungan else set())
        objek = _buat_strategi(cfg, sorted(perlu))
        aturan = AturanUniverse.backtest(masukan, cfg)
        keluar(f"Data {tanggal_awal.isoformat()} … {akhir.isoformat()} ({len(hari)} hari bursa simulasi, "
               f"{len(aturan.kandidat)} saham universe, {len(masukan.breadth)} saham breadth)")
        mulai = waktu.sekarang()
        keputusan = mesin.siapkan_keputusan(
            pasar, hari, aturan, masukan.breadth, objek,
            kemajuan=_kemajuan(keluar, "Keputusan harian (status pasar, eligibilitas, sinyal "
                                       + ", ".join(s.kode for s in objek) + ")"),
        )
        _durasi("keputusan harian", mulai)

        kal = pasar.kalender
        semua_tanggal = [tanggal_awal, *hari]
        close_ihsg = mt.close_ihsg_dari_pasar(pasar, semua_tanggal)
        close_hari_itu = mt.close_ihsg_dari_pasar(pasar, semua_tanggal, hanya_hari_itu=True)
        konteks = {m: resolusi_konteks(cfg, m) for m in modes}
        penilaian = jd.tanggal_penilaian(kal, cfg)
        catatan: list[str] = [c for c in (catatan_akhir,) if c]

        def jalankan_run(spec: mesin.SpesifikasiRun, m: str) -> lb.RunMode:
            mulai_run = waktu.sekarang()
            h = mesin.simulasikan(
                keputusan, pasar, spec, mode=m, konteks=konteks[m], sektor_dari=aturan.sektor,
                tanggal_awal=tanggal_awal, tanggal_penilaian=penilaian,
            )
            jd.tandai_melewati_holdout(h, cfg)
            pembagian = jd.bagi_periode(h, cfg, kal)
            metrik = jd.metrik_semua_periode(pembagian, kal, cfg, close_ihsg=close_ihsg)
            _durasi(f"run {spec.nama} {m}", mulai_run)
            keluar(f"  Run {spec.nama} ({m}): {len(h.tertutup)} transaksi tertutup, {len(h.terbuka)} terbuka")
            return lb.RunMode(spec.nama, m, tuple(sorted(spec.strategi, key=URUTAN_STRATEGI.index)), h, pembagian,
                              metrik)

        runs: list[lb.RunMode] = []
        gerbang: dict[str, gb.HasilGerbang] = {}
        try:
            for kode_s in per_strategi:
                for m in modes:
                    runs.append(jalankan_run(mesin.SpesifikasiRun.per_strategi(kode_s), m))
            if "edge" in modes:
                for rm in runs:
                    s = rm.strategi[0]
                    if rm.mode == "edge" and s in gb.STRATEGI_GERBANG:
                        gerbang[s] = gb.nilai_gerbang_dari_metrik(s, rm.metrik, cfg)
            if gabungan:
                if aktif:
                    for m in modes:
                        runs.append(jalankan_run(mesin.SpesifikasiRun.gabungan_aktif(aktif), m))
                else:
                    catatan.append("gabungan_aktif dilewati: tidak ada strategi aktif")
                if "edge" not in modes:
                    catatan.append("gabungan_lolos dilewati: memerlukan mode edge dalam run yang sama")
                else:
                    lolos = tuple(k for k in aktif if k in gerbang and gerbang[k].lolos)
                    lolos_nonaktif = [k for k, g in gerbang.items() if g.lolos and k not in aktif]
                    if lolos_nonaktif:
                        catatan.append(f"strategi lolos tetapi tidak aktif (tidak ikut gabungan_lolos): "
                                       f"{', '.join(lolos_nonaktif)}")
                    if not lolos:
                        catatan.append("gabungan_lolos dilewati: tidak ada strategi aktif yang lolos gerbang")
                    else:
                        for m in modes:
                            runs.append(jalankan_run(mesin.SpesifikasiRun.gabungan_lolos(aktif, lolos), m))
        except mesin.IdentitasAkuntansiGagal as e:
            log.error("%s", e)
            hasil.kode_keluar = KODE_ERROR
            hasil.pesan = str(e)
            keluar(f"ERROR identitas akuntansi backtest (§16.9): {e}")
            keluar("Backtest dihentikan; ringkasan, gate_status.json, dan holdout_log.csv tidak ditulis.")
            keluar("")
            keluar(DISCLAIMER)
            return hasil

        # Hash, folder run, gerbang.
        ha = gb.hash_aturan_global(cfg)
        hi = gb.hash_input(ctx)
        h8 = gb.hash8(ha, hi)
        folder_bt = ctx.path(gb.FOLDER_BACKTEST)
        nama_folder = gb.nama_folder_run(h8, sekarang)
        folder_run = folder_bt / nama_folder
        folder_rel = (gb.FOLDER_BACKTEST / nama_folder).as_posix()
        subset: dict[str, gb.SubsetTerjangkau] = {}
        entri: dict[str, dict] = {}
        for s, g in gerbang.items():
            rm = next(r for r in runs if r.nama == s.lower() and r.mode == "edge")
            subset[s] = gb.subset_terjangkau(rm.pembagian.holdout.tertutup, cfg)
            ident = gb.identitas_hash(ctx, s)
            entri[s] = gb.entri_gate(
                g, hash_aturan=ident.hash_aturan, hash_input=ident.hash_input, tanggal_data_terakhir=akhir,
                holdout_mulai=cfg.backtest.holdout_mulai, subset_terjangkau_lemah=subset[s].lemah,
                folder_run=folder_rel, rincian=ident.rincian, dibuat_pada=sekarang,
            )
        mulai = waktu.sekarang()
        jalur = {s: mesin.jalur_per_sinyal(keputusan, pasar, s, aturan.sektor) for s in per_strategi}
        _durasi("jalur per sinyal", mulai)

        riwayat_tanpa = ()
        if masukan.riwayat is not None:
            riwayat_tanpa = tuple(sorted(
                {r.kode for r in masukan.riwayat if not termuat.seri[r.kode].ada_data}
            ))
        lap = lb.LaporanBacktest(
            cfg=cfg, kalender=kal, dibuat_pada=sekarang, versi_program=str(__version__), t=t, akhir=akhir,
            tanggal_awal=tanggal_awal, hash_aturan=ha, hash_input=hi, hash8=h8, folder_run=folder_rel,
            strategi_diminta=per_strategi, mode_diminta=modes, runs=runs, konteks=konteks,
            close_ihsg=close_ihsg, close_ihsg_hari_itu=close_hari_itu,
            faktor=lambda k, d: ex._f(pasar.tampilan(d).faktor_split(k)),
            teks_config=teks_config, nama_config=ctx.path_config.name,
            gerbang=gerbang, entri_gate=entri, subset=subset, jalur=jalur,
            riwayat_ada=masukan.riwayat is not None, riwayat_tanpa_data=riwayat_tanpa,
            breadth_tahunan=lb.breadth_per_tahun(keputusan), catatan=catatan, peringatan=peringatan,
            path_kualitas=_relatif(hasil.path_kualitas, ctx.akar),
        )

        # Berkas gerbang & log holdout (setelah semua simulasi lolos identitas akuntansi).
        mulai = waktu.sekarang()
        folder_run.mkdir(parents=True, exist_ok=True)
        hasil.berkas[gb.NAMA_GERBANG_RUN] = gb.tulis_gerbang_run(folder_run, entri)
        baris_log = [
            gb.baris_holdout_log(
                holdout_mulai=cfg.backtest.holdout_mulai, hash_aturan=ha, hash_input=hi, strategi=rm.nama,
                mode=rm.mode, holdout=rm.periode(jd.HOLDOUT).transaksi, saat=sekarang,
            )
            for rm in lb.urut_runs(runs) if rm.ada_holdout
        ]
        path_log = folder_bt / gb.NAMA_HOLDOUT_LOG
        if baris_log:
            _ditulis, peringatan_log = gb.tambah_holdout_log(folder_bt, baris_log)
            if peringatan_log:
                log.warning(peringatan_log)
                lap.peringatan.append(peringatan_log)
                keluar(f"PERINGATAN: {peringatan_log}")
        lap.holdout_log = gb.baca_holdout_log(path_log)
        path_gate = gb.gabung_gate_status(folder_bt, entri, modes)
        lap.gate_status_ditulis = path_gate is not None
        hasil.berkas.update(lb.tulis_semua(folder_run, lap))
        _durasi("tulis laporan", mulai)
        hasil.folder_run = folder_run
        hasil.laporan = lap
        for baris in lb.ringkasan_terminal(lap, folder_rel):
            keluar(baris)
    return hasil


def _relatif(path: Path | None, akar: Path) -> str:
    if path is None:
        return ""
    try:
        return Path(path).resolve().relative_to(Path(akar).resolve()).as_posix()
    except ValueError:
        return str(path)


def _tanpa_data(hasil: HasilBacktest, keluar: Callable[[str], None], alasan: str) -> HasilBacktest:
    log.warning("backtest tidak dijalankan: %s", alasan)
    hasil.kode_keluar = KODE_DATA
    hasil.pesan = alasan
    keluar(f"DATA BELUM TERSEDIA untuk backtest: {alasan}.")
    if hasil.path_kualitas is not None:
        keluar(f"Berkas kualitas: {hasil.path_kualitas}")
    keluar("")
    keluar(DISCLAIMER)
    return hasil


__all__ = [
    "HasilBacktest", "MODE_CLI", "STRATEGI_CLI", "daftar_simbol_backtest", "jalankan_backtest",
    "parse_daftar_mode", "parse_daftar_strategi", "tentukan_akhir",
]

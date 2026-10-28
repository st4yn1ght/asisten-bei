"""Laporan harian (SPEC §18): `bangun_laporan` mengisi `LaporanHarian`, `tulis_laporan` menulis berkasnya.

Berkas (§18.2) di `output/laporan/<t>/run_<HHMMSS>/` (tidak pernah menimpa run sebelumnya) dan salinan terbaru
di `output/laporan/<t>/`: `laporan.md`, `laporan.html`, `kandidat.csv`, `rencana.xlsx` (menurut
`laporan.format`), dan selalu `kualitas_data.csv` (§8.5). Berkas tujuan yang terkunci (dibuka di Excel) ditulis
ke `<nama>_<HHMMSS>.<ext>` dengan peringatan (§18.2), dan run berlanjut.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import teks
from ..aturan_bei import AturanBEI
from ..backtest import gerbang as gb
from ..inputs import KODE_IHSG, cek_umur_akun
from ..kalender import banner_t1_tanpa_kalender, peringatan_libur
from ..operasional import tulis_atomik, tulis_keluaran
from ..portofolio import BANNER_BLOKIR, BANNER_SPLIT
from .model import (
    BANNER_BELUM_TERSEDIA,
    BANNER_TIDAK_LENGKAP,
    Kualitas,
    LaporanHarian,
    StatusStrategi,
    rencana_beli,
    ringkasan_aksi,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..config import KonteksApp

TEKS_SUBSET_LEMAH = "keunggulan belum terbukti pada saham yang terjangkau modal Anda"

NAMA_MD = "laporan.md"
NAMA_HTML = "laporan.html"
NAMA_KANDIDAT = "kandidat.csv"
NAMA_XLSX = "rencana.xlsx"
NAMA_KUALITAS = "kualitas_data.csv"


def _pf_holdout(e: Mapping) -> str:
    g2 = ((e.get("syarat") or {}).get("G2") or {}) if isinstance(e.get("syarat"), Mapping) else {}
    nilai = g2.get("nilai")
    if nilai is None or nilai == "":
        return teks.STRIP
    try:
        return teks.angka(float(nilai), 2)
    except (TypeError, ValueError):
        return str(nilai)


def bangun_laporan(
    *,
    ctx: "KonteksApp",
    t: date,
    t1: date | None,
    sekarang: datetime,
    masukan,
    termuat,
    info,
    status,
    kondisi,
    eligibilitas,
    seleksi_t,
    portofolio,
    gate: Mapping[str, Mapping],
    alasan_gerbang: Mapping[str, str | None],
    aktif: Sequence[str],
    banner_telat: str | None,
    jurnal,
    jurnal_gagal: str | None,
    replay: bool,
    catatan: Sequence[str],
    kal_manual,
    versi: str,
) -> LaporanHarian:
    cfg = ctx.cfg
    bei = AturanBEI(cfg)
    pasar = termuat.pasar
    dv = pasar.tampilan(t)
    lap = LaporanHarian(
        cfg=cfg, t=t, t1=t1, dibuat_pada=sekarang, hash8=gb.hash8(gb.hash_aturan_global(cfg), gb.hash_input(ctx)),
        versi_program=versi, status_data_run=status.status, kondisi=kondisi, portofolio=portofolio,
        jurnal=jurnal, jurnal_peringatan=jurnal_gagal, replay=replay, catatan=list(catatan),
    )
    lap.nama = {s.kode: s.nama for s in masukan.universe}
    lap.sektor = {s.kode: s.sektor for s in masukan.universe}

    # Banner (§18.1 #1).
    if status.status == "BELUM_TERSEDIA":
        b = f"{BANNER_BELUM_TERSEDIA}: {status.alasan}. Tidak ada rekomendasi beli dan tidak ada sinyal dicatat."
        if status.kemungkinan_libur:
            b += " IHSG dan hampir semua saham tidak punya bar t padahal t−1 lengkap: mungkin hari libur yang belum ada di holidays.csv."
        lap.banner.append(b)
    elif status.status == "TIDAK_LENGKAP":
        lap.banner.append(f"{BANNER_TIDAK_LENGKAP}: {status.alasan}"
                          + (" — status pasar JANGAN BELI (K0)." if status.k0 or not status.ihsg_ada else "."))
    if banner_telat:
        lap.banner.append(banner_telat)
    for s in portofolio.banner_split:
        lap.banner.append(f"{BANNER_SPLIT} — {s}")
    if portofolio.blokir:
        lap.banner.append(f"{BANNER_BLOKIR}: {portofolio.blokir}")
    if t1 is not None:
        b = banner_t1_tanpa_kalender(kal_manual, t1)
        if b:
            lap.banner.append(f"{b} (t+1 = {t1.isoformat()}).")
    for p in peringatan_libur(kal_manual, sekarang):
        (lap.banner if p.level == "BANNER" else lap.peringatan).append(p.teks)
    if not masukan.akun.dari_berkas:
        lap.banner.append(f"akun.yaml tidak ada: kas memakai akun.modal_awal_idr {teks.rupiah(masukan.akun.kas_idr)} "
                          "— buat input/akun.yaml berisi kas Anda yang sebenarnya.")
    if replay:
        lap.banner.append(f"REPLAY --tanggal {t.isoformat()}: laporan dibuat ulang dari data ≤ {t.isoformat()}.")

    # Peringatan.
    for m in masukan.daftar_peringatan():
        lap.peringatan.append(str(m))
    umur = cek_umur_akun(masukan.akun, kal_manual, sekarang.date(), int(cfg.akun.maks_umur_hari_bursa),
                         _rel(ctx, cfg.akun.file_akun))
    if umur is not None:
        lap.peringatan.append(f"akun basi: {umur.pesan}")
    for s in ("PB", "BO", "MR"):
        e = gate.get(s) or {}
        if s in aktif and alasan_gerbang.get(s) is None and e.get("subset_terjangkau_lemah"):
            lap.peringatan.append(f"{s}: {TEKS_SUBSET_LEMAH}.")
    lap.peringatan.extend(portofolio.pengingat)
    for sp in portofolio.saran:
        for p in sp.peringatan:
            lap.peringatan.append(f"{sp.kode}: {p}")
    if jurnal_gagal:
        lap.peringatan.append(f"Jurnal gagal: {jurnal_gagal}. Bagian jurnal diganti peringatan ini (kode keluar 6).")

    # Status strategi (§18.1 #4).
    for s in ("PB", "BO", "MR"):
        e = gate.get(s) or {}
        lap.status_strategi.append(StatusStrategi(
            kode=s, aktif=s in aktif, lolos=s in aktif and alasan_gerbang.get(s) is None,
            alasan=("strategi tidak aktif di config" if s not in aktif else (alasan_gerbang.get(s) or "")),
            tanggal_backtest=str(e.get("tanggal_data_terakhir") or teks.STRIP), pf_r_holdout=_pf_holdout(e),
            subset_lemah=bool(e.get("subset_terjangkau_lemah")),
        ))

    # Akun & seleksi.
    if seleksi_t is not None:
        lap.anggaran_risiko = seleksi_t.anggaran_risiko
        lap.alokasi_maks = seleksi_t.alokasi_maks
        lap.slot_kosong = seleksi_t.slot_awal
        lap.kandidat = list(seleksi_t.kandidat)
    lap.akun_diperbarui = masukan.akun.diperbarui if masukan.akun.dari_berkas else None
    for k in lap.rekomendasi:
        b = dv.seri(k.kode).bar_mentah() if dv.ada(k.kode) else None
        manual = b is not None and str(b.get("sumber", "")).startswith("csv_manual")
        lap.rencana.append(rencana_beli(k, nama=lap.nama.get(k.kode, ""), sektor=masukan.sektor(k.kode),
                                        ekuitas=portofolio.ekuitas_idr, cfg=cfg, bei=bei, sumber_manual=manual))

    # Kualitas data (§18.1 #10).
    kode_univ = [s.kode for s in masukan.universe]
    semua = [KODE_IHSG, *sorted(set(termuat.data) - {KODE_IHSG})]
    st = Counter(termuat.seri[k].status for k in semua if k in termuat.seri)
    alasan = {k: dv.alasan_data(k) for k in kode_univ if dv.ada(k)}
    dikeluarkan = [(s.kode, s.alasan or "", s.rincian) for s in eligibilitas.dikeluarkan]
    revisi = []
    for kode_s, per in (info or {}).items():
        for nama_p, hp_ in per.items():
            for c in getattr(hp_, "catatan", []):
                if "revisi" in c or "unduh ulang penuh" in c or "dihapus" in c:
                    revisi.append(f"{kode_s} ({nama_p}): {c}")
    if jurnal is not None and jurnal.data_direvisi:
        revisi.append(f"jurnal: {jurnal.data_direvisi} sinyal tercatat ditandai data_direvisi")
    lap.kualitas = Kualitas(
        status_ambil={k: st.get(k, 0) for k in ("OK", "SEBAGIAN", "GAGAL")},
        basi=sum(1 for v in alasan.values() if v == "DATA_BASI"),
        anomali=sum(1 for v in alasan.values() if v == "ANOMALI"),
        sumber_manual=sum(1 for k in semua if "csv_manual" in (termuat.seri[k].sumber or "")),
        dikeluarkan=dikeluarkan[: int(cfg.laporan.maks_baris_dikeluarkan)],
        dikeluarkan_total=len(dikeluarkan),
        revisi=revisi,
        catatan=[f"{k}: {', '.join(s.catatan)}" for k, s in sorted(termuat.seri.items())
                 if s.catatan and (k in kode_univ or k == KODE_IHSG)][:20],
    )

    # Item belum diverifikasi (§18.1 #11).
    from ..cek import item_verifikasi

    lap.belum_verifikasi = list(item_verifikasi(ctx, sekarang.date())[0])
    for p in peringatan_libur(kal_manual, sekarang):
        lap.belum_verifikasi.append(p.teks)
    if umur is not None:
        lap.belum_verifikasi.append(f"akun.yaml: {umur.pesan}")

    lap.ringkasan_aksi = ringkasan_aksi(lap)
    level = {}
    for r in lap.rencana:
        level[r.kode] = {"harga_maks": r.harga_maks, "stop": r.stop,
                         "target": r.target[-1][2] if r.target else None}
    for sp in portofolio.saran:
        level.setdefault(sp.kode, {"harga_maks": None, "stop": sp.stop_besok, "target": sp.target})
    lap.grafik = {"pasar": pasar, "breadth": list(masukan.breadth), "level": level}
    return lap


def _rel(ctx, rel) -> str:
    p = ctx.path(rel)
    try:
        return p.resolve().relative_to(Path(ctx.akar).resolve()).as_posix()
    except ValueError:
        return p.as_posix()


# ---------------------------------------------------------------------------
# Penulisan
# ---------------------------------------------------------------------------


def isi_berkas(lap: LaporanHarian) -> dict[str, bytes]:
    """Isi setiap berkas laporan menurut `laporan.format` (nama → byte). Teks melewati penyaring rahasia (§19.5)."""
    from ..operasional import PENYARING

    hasil = _isi_berkas(lap)
    for nama in (NAMA_MD, NAMA_HTML, NAMA_KANDIDAT):
        if nama in hasil:
            hasil[nama] = PENYARING.sensor(hasil[nama].decode("utf-8")).encode("utf-8")
    return hasil


def _isi_berkas(lap: LaporanHarian) -> dict[str, bytes]:
    fmt = set(lap.cfg.laporan.format)
    hasil: dict[str, bytes] = {}
    if "markdown" in fmt:
        from .markdown import render_markdown

        hasil[NAMA_MD] = render_markdown(lap).encode("utf-8")
    if "html" in fmt:
        from .html import render_html

        hasil[NAMA_HTML] = render_html(lap).encode("utf-8")
    if "csv" in fmt:
        from .tabel import teks_kandidat_csv

        hasil[NAMA_KANDIDAT] = teks_kandidat_csv(lap).encode("utf-8")
    if "xlsx" in fmt:
        from .tabel import bytes_xlsx

        hasil[NAMA_XLSX] = bytes_xlsx(lap)
    return hasil


def tulis_laporan(ctx: "KonteksApp", lap: LaporanHarian, termuat: Any, sekarang: datetime) -> tuple[dict[str, Path], Path, list[str]]:
    """Tulis berkas ke folder run (baru) dan salinan terbaru di `output/laporan/<t>/`. Mengembalikan
    (nama → path salinan terbaru yang benar-benar ditulis, folder run, peringatan berkas terkunci)."""
    import pandas as pd

    from ..data.kualitas import KOLOM_KUALITAS
    from ..data.sumber import baris_kualitas
    from ..operasional import teks_csv

    folder_t = ctx.path(lap.cfg.laporan.folder) / lap.t.isoformat()
    dasar = folder_t / f"run_{sekarang:%H%M%S}"
    folder_run, n = dasar, 1
    while folder_run.exists():
        n += 1
        folder_run = dasar.with_name(f"{dasar.name}_{n}")
    isi = isi_berkas(lap)
    kode = [KODE_IHSG, *sorted(set(termuat.data) - {KODE_IHSG})]
    baris = baris_kualitas(termuat, kode, lap.t)
    df = pd.DataFrame([b.sebagai_dict() for b in baris], columns=KOLOM_KUALITAS, dtype=object)
    isi[NAMA_KUALITAS] = teks_csv(df).encode("utf-8")
    berkas: dict[str, Path] = {}
    peringatan: list[str] = []
    for nama, data in isi.items():
        tulis_atomik(folder_run / nama, data)
        path, pesan = tulis_keluaran(folder_t / nama, data)
        berkas[nama] = path
        if pesan:
            peringatan.append(pesan)
    if peringatan:  # §18.2: peringatan berkas terkunci juga tercantum di laporan itu sendiri
        lap.peringatan.extend(peringatan)
        for nama, data in isi_berkas(lap).items():
            tulis_atomik(folder_run / nama, data)
            tulis_atomik(berkas[nama], data)
    return berkas, folder_run, peringatan


def tulis_ulang(lap: LaporanHarian, berkas: dict[str, Path], folder_run: Path) -> list[str]:
    """Render ulang berkas laporan (mis. setelah notifikasi gagal, §18.3) ke folder run dan salinan terbaru yang
    sudah ditulis; `kualitas_data.csv` tidak berubah. Berkas yang kini terkunci dilewati; pesannya dikembalikan."""
    gagal: list[str] = []
    for nama, data in isi_berkas(lap).items():
        for path in (Path(folder_run) / nama, berkas.get(nama)):
            if path is None:
                continue
            try:
                tulis_atomik(path, data)
            except OSError as ex:
                gagal.append(f"{path.name} tidak bisa ditulis ulang: {ex}")
    return gagal


__all__ = ["LaporanHarian", "bangun_laporan", "isi_berkas", "tulis_laporan", "tulis_ulang"]

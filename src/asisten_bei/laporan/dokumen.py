"""Struktur dokumen laporan harian (SPEC §18.1) yang dirender sama oleh Markdown dan HTML.

`bangun_dokumen(lap)` → daftar `Bagian` berurutan §18.1 #1–#13. Setiap bagian berisi blok `Paragraf`, `Daftar`,
`Tabel`, atau `Grafik` (HTML: PNG base64; Markdown: catatan teks).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .. import teks
from ..kondisi_pasar import TAMPILAN_STATUS
from .model import (
    GLOSARIUM,
    KETERBATASAN_SINGKAT,
    LaporanHarian,
    h,
    rp,
    status_pasar_ringkas,
    teks_kandidat,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..jurnal import RingkasanJurnal


@dataclass
class Paragraf:
    teks: str
    gaya: str = ""  # "" | "banner" | "peringatan" | "kecil" | "tebal"


@dataclass
class Daftar:
    butir: list[str]


@dataclass
class Tabel:
    kolom: list[str]
    baris: list[list[str]]


@dataclass
class Grafik:
    jenis: str  # "saham" | "ihsg" | "breadth"
    kode: str = ""
    judul: str = ""


@dataclass
class Bagian:
    judul: str
    blok: list = field(default_factory=list)
    id: str = ""


def _kepala(lap: LaporanHarian) -> Bagian:
    b = Bagian(lap.judul, id="kepala")
    for x in lap.kepala():
        b.blok.append(Paragraf(x, "tebal" if x == "BUKAN SARAN INVESTASI" else ""))
    for x in lap.banner:
        b.blok.append(Paragraf(x, "banner"))
    return b


def _ringkasan_aksi(lap: LaporanHarian) -> Bagian:
    return Bagian("Ringkasan aksi", [Daftar(list(lap.ringkasan_aksi))], "aksi")


def _status_pasar(lap: LaporanHarian) -> Bagian:
    b = Bagian("Status pasar", id="pasar")
    b.blok.append(Paragraf(status_pasar_ringkas(lap.kondisi), "tebal"))
    k = lap.kondisi
    if k is not None:
        b.blok.append(Tabel(["Syarat", "Kondisi", "Nilai", "Ambang", "✓/✗"],
                            [[s.aturan, s.uraian, s.nilai, s.ambang, s.tanda] for s in k.syarat]))
        br = k.breadth
        b.blok.append(Paragraf(
            f"Breadth: {br.n_di_atas_sma}/{br.n_terhitung} di atas SMA{lap.cfg.kondisi_pasar.breadth_sma} "
            f"({teks.persen(br.breadth_pct, 1) if br.breadth_pct is not None else teks.STRIP}); cakupan "
            f"{br.n_terhitung}/{br.n_tercatat} ({teks.persen(br.cakupan_pct, 1) if br.cakupan_pct is not None else teks.STRIP}).",
            "kecil"))
        b.blok.append(Grafik("ihsg", judul="IHSG dengan SMA pendek/panjang"))
        b.blok.append(Grafik("breadth", judul="Breadth 120 hari bursa"))
    return b


def _status_strategi(lap: LaporanHarian) -> Bagian:
    baris = []
    for s in lap.status_strategi:
        status = "LOLOS" if s.lolos else ("TIDAK AKTIF" if not s.aktif else "BAYANGAN_GERBANG")
        baris.append([s.kode, status, s.tanggal_backtest, s.pf_r_holdout,
                      "ya" if s.subset_lemah else "tidak", s.alasan or "—"])
    return Bagian("Status strategi", [Tabel(["Strategi", "Status", "Data backtest s/d", "PF_R holdout",
                                             "Subset terjangkau lemah", "Alasan"], baris)], "strategi")


def _akun(lap: LaporanHarian) -> Bagian:
    return Bagian("Akun", [Tabel(["Item", "Nilai"], [[a, b] for a, b in lap.akun_baris()])], "akun")


def _posisi(lap: LaporanHarian) -> Bagian:
    b = Bagian("Posisi dipegang", id="posisi")
    if not lap.posisi:
        b.blok.append(Paragraf("Tidak ada posisi di portfolio.csv."))
        return b
    baris = []
    for s in lap.posisi:
        hari = teks.STRIP if s.hari_ke is None else (
            f"{s.hari_ke}" + (f"/{s.stop_waktu_hari}" if s.stop_waktu_hari else ""))
        baris.append([
            s.kode, s.strategi, s.teks_saran, teks.angka(s.lembar, 0), h(s.harga_beli),
            ("≈ " if s.perkiraan else "") + h(s.close), h(s.stop_besok), h(s.target), hari,
            f"{rp(s.pnl_idr)} ({teks.persen(s.pnl_pct, 2) if s.pnl_pct is not None else teks.STRIP}; {s.teks_r})",
        ])
    b.blok.append(Tabel(["Kode", "Strategi", "Saran", "Lembar", "Harga beli", "Close", "Stop t+1 (order otomatis)",
                         "Target", "Hari ke", "P&L belum terealisasi"], baris))
    b.blok.append(Daftar([f"{s.kode}: {s.teks_saran} — {s.alasan}" for s in lap.posisi]))
    b.blok.append(Paragraf("P&L belum terealisasi = nilai pada close valid terakhir − biaya beli (sebelum fee jual). "
                           "Level stop/target berlaku untuk order otomatis di t+1.", "kecil"))
    for s in lap.posisi:
        if s.saran in ("TAHAN", "JUAL", "CEK_ORDER"):
            b.blok.append(Grafik("saham", s.kode, f"{s.kode} ({s.strategi}, dipegang)"))
    return b


def _rencana(lap: LaporanHarian) -> Bagian:
    b = Bagian("Rencana beli", id="rencana")
    if lap.status_data_run == "BELUM_TERSEDIA":
        b.blok.append(Paragraf("DATA BELUM TERSEDIA: tidak ada rencana beli.", "peringatan"))
        return b
    if not lap.rencana:
        b.blok.append(Paragraf("Tidak ada REKOMENDASI untuk t+1."))
        return b
    for r in lap.rencana:
        b.blok.append(Paragraf(f"{r.kode} — {r.nama} ({r.sektor}), {r.strategi}, skor {r.skor}", "tebal"))
        isi = [
            ["Alasan sinyal", "; ".join(r.alasan_sinyal)],
            ["Harga referensi (close t)", h(r.harga_ref)],
            ["Instruksi masuk", r.instruksi_masuk],
        ]
        for g in r.ketergantungan:
            isi.append(["Ketergantungan jual terjadwal", g])
        isi += [
            ["Stop loss", f"{h(r.stop)} ({r.metode_stop}); jarak {teks.persen(r.jarak_stop_pct, 2)}"],
            ["Target", r.teks_target + ("; " + "; ".join(f"isi {h(i)} → target {h(tg)}" for _l, i, tg in r.target)
                                        if r.target else "")],
            ["Lot / nilai / fee beli", f"{r.lot} lot ({r.lembar} lembar) / {rp(r.nilai_idr)} / {rp(r.fee_beli_idr)}"],
            ["Risiko", f"{rp(r.risiko_idr)} ({r.risiko_pct} ekuitas); sisa anggaran per lembar "
                       f"{rp(r.sisa_anggaran_per_lembar) if r.sisa_anggaran_per_lembar is not None else teks.STRIP}"],
        ]
        if r.rr_bersih is not None:
            isi.append(["R:R bersih (isi di harga_maks)", teks.angka(r.rr_bersih, 2)])
        isi.append(["Langkah setelah terisi", r.langkah_setelah])
        for p in r.peringatan:
            isi.append(["Peringatan", p])
        b.blok.append(Tabel(["Item", "Isi"], isi))
        b.blok.append(Grafik("saham", r.kode, f"{r.kode} ({r.strategi}, rencana)"))
    b.blok.append(Paragraf(lap.catatan_skor, "kecil"))
    return b


def _kandidat_lain(lap: LaporanHarian) -> Bagian:
    b = Bagian("Kandidat lain", id="kandidat")
    lain = lap.kandidat_lain
    if not lain:
        b.blok.append(Paragraf("Tidak ada kandidat lain."))
        return b
    b.blok.append(Tabel(["Kode", "Strategi", "Skor", "Status", "Alasan"],
                        [[k.kode, k.strategi, k.sinyal.komponen.tampilan, k.status, k.alasan or "—"] for k in lain]))
    if lap.n_kandidat_lain > len(lain):
        b.blok.append(Paragraf(f"… {lap.n_kandidat_lain - len(lain)} kandidat lain hanya di kandidat.csv.", "kecil"))
    return b


def _pantau(lap: LaporanHarian) -> Bagian:
    b = Bagian("Daftar pantau", id="pantau")
    if not lap.daftar_pantau:
        b.blok.append(Paragraf("Tidak ada (daftar pantau hanya saat JANGAN BELI)."))
        return b
    b.blok.append(Paragraf(f"Status pasar {TAMPILAN_STATUS.get(lap.status_pasar or '', '—')}: sinyal berikut hanya dipantau "
                           "(BAYANGAN_PASAR), urut skor."))
    b.blok.append(Daftar([teks_kandidat(k) for k in lap.daftar_pantau]))
    return b


def _kualitas(lap: LaporanHarian) -> Bagian:
    b = Bagian("Kualitas data", id="kualitas")
    q = lap.kualitas
    if q is None:
        return b
    b.blok.append(Paragraf(
        f"Seri: OK {q.status_ambil.get('OK', 0)}, SEBAGIAN {q.status_ambil.get('SEBAGIAN', 0)}, GAGAL "
        f"{q.status_ambil.get('GAGAL', 0)}; data basi {q.basi}; anomali {q.anomali}; sumber CSV manual "
        f"{q.sumber_manual}. Rincian per simbol: kualitas_data.csv."))
    if q.dikeluarkan:
        b.blok.append(Tabel(["Kode", "Alasan", "Rincian"], [list(x) for x in q.dikeluarkan]))
        if q.dikeluarkan_total > len(q.dikeluarkan):
            b.blok.append(Paragraf(f"… {q.dikeluarkan_total - len(q.dikeluarkan)} lainnya hanya di kualitas_data.csv.",
                                   "kecil"))
    else:
        b.blok.append(Paragraf("Tidak ada saham universe yang dikeluarkan."))
    if q.revisi:
        b.blok.append(Paragraf("Revisi data:", "tebal"))
        b.blok.append(Daftar(list(q.revisi)))
    return b


def _belum(lap: LaporanHarian) -> Bagian:
    b = Bagian("Item yang belum diverifikasi", id="verifikasi")
    b.blok.append(Daftar(list(lap.belum_verifikasi)) if lap.belum_verifikasi else Paragraf("Semua item sudah diverifikasi."))
    return b


def _baris_kertas(r: dict) -> list[str]:
    return [r.get("kode") or "", r.get("strategi") or "", r.get("tanggal_entry") or "", r.get("harga_beli_nominal") or "",
            r.get("tanggal_exit") or "", r.get("alasan_exit") or "", r.get("r") or "", r.get("pnl_idr") or ""]


def _jurnal(lap: LaporanHarian) -> Bagian:
    b = Bagian("Jurnal kertas", id="jurnal")
    if lap.jurnal_peringatan:
        b.blok.append(Paragraf(f"Jurnal gagal: {lap.jurnal_peringatan}. Bagian ini tidak tersedia.", "peringatan"))
        return b
    j: "RingkasanJurnal | None" = lap.jurnal
    if j is None:
        b.blok.append(Paragraf("Jurnal belum ada (belum ada run harian yang mencatat)."))
        return b
    for c in j.catatan:
        b.blok.append(Paragraf(c, "kecil"))
    if j.sudah_tercatat:
        b.blok.append(Paragraf("Sinyal tanggal ini sudah tercatat; tidak ada baris baru."))
    b.blok.append(Paragraf(f"{j.hari_dikejar} hari dikejar; {j.sinyal_susulan} sinyal susulan tidak pernah dilaporkan."))
    if j.periode_id is not None:
        b.blok.append(Paragraf(
            f"Periode kertas {j.periode_id} mulai {j.periode_mulai.isoformat() if j.periode_mulai else '—'}, modal "
            f"{rp(j.modal_awal)}; ekuitas {'≈ ' if j.perkiraan else ''}{rp(j.ekuitas)} pada "
            f"{j.tanggal_ekuitas.isoformat() if j.tanggal_ekuitas else '—'}. Ini BUKAN portofolio nyata Anda."))
    if j.periode_ditutup is not None:
        b.blok.append(Paragraf(f"hash_aturan berubah: periode {j.periode_ditutup} ditutup dan dibekukan; periode baru dimulai.",
                               "peringatan"))
    if j.transaksi_berubah:
        b.blok.append(Paragraf(f"{j.transaksi_berubah} transaksi kertas berubah karena revisi data.", "peringatan"))
    if j.hari_gerbang_ditandai:
        b.blok.append(Paragraf("Gerbang untuk " + ", ".join(d.isoformat() for d in j.hari_gerbang_ditandai)
                               + " memakai snapshot run yang memproses hari itu (tidak ada run sebelumnya).", "kecil"))
    kolom = ["Kode", "Strategi", "Entry", "Harga beli", "Exit", "Alasan", "R", "P&L (Rp)"]
    if j.terbuka:
        b.blok.append(Paragraf("Posisi kertas terbuka:", "tebal"))
        b.blok.append(Tabel(kolom, [_baris_kertas(r) for r in j.terbuka]))
    if j.menunggu:
        b.blok.append(Paragraf("Order kertas MENUNGGU (bar t+1 belum ada): "
                               + ", ".join(f"{r['kode']} {r['strategi']}" for r in j.menunggu)))
    if j.tertutup_30:
        b.blok.append(Paragraf("Transaksi kertas tertutup 30 hari terakhir:", "tebal"))
        b.blok.append(Tabel(kolom, [_baris_kertas(r) for r in j.tertutup_30]))
    if j.statistik:
        b.blok.append(Tabel(
            ["Strategi", "n jurnal", "Rata-rata R", "PF_R", "n holdout", "Rata-rata R holdout", "PF_R holdout"],
            [[s.strategi, str(s.n), teks.angka(s.rata_r, 2), s.pf_r,
              teks.STRIP if s.n_holdout is None else str(s.n_holdout), teks.angka(s.rata_r_holdout, 2), s.pf_r_holdout]
             for s in j.statistik]))
    for p in j.penyimpangan:
        b.blok.append(Paragraf(f"Penyimpangan {p.strategi}: {p.catatan}", "peringatan" if p.peringatan else "kecil"))
    return b


def _peringatan(lap: LaporanHarian) -> Bagian:
    b = Bagian("Peringatan", id="peringatan")
    b.blok.append(Daftar(list(lap.peringatan + lap.catatan)) if (lap.peringatan or lap.catatan)
                  else Paragraf("Tidak ada peringatan."))
    return b


def _penutup(lap: LaporanHarian) -> Bagian:
    b = Bagian("Penutup", id="penutup")
    b.blok.append(Paragraf(lap.disclaimer, "tebal"))
    b.blok.append(Paragraf("Keterbatasan singkat:"))
    b.blok.append(Daftar(list(KETERBATASAN_SINGKAT)))
    return b


def glosarium() -> Bagian:
    return Bagian("Glosarium", [Tabel(["Istilah", "Penjelasan"], [[a, b] for a, b in GLOSARIUM])], "glosarium")


def bangun_dokumen(lap: LaporanHarian) -> list[Bagian]:
    """Bagian §18.1 berurutan (#1–#13); peringatan umum diletakkan sebelum penutup."""
    return [
        _kepala(lap), _ringkasan_aksi(lap), _status_pasar(lap), _status_strategi(lap), _akun(lap), _posisi(lap),
        _rencana(lap), _kandidat_lain(lap), _pantau(lap), _kualitas(lap), _belum(lap), _jurnal(lap), _peringatan(lap),
        _penutup(lap),
    ]

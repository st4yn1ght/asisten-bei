"""Ringkasan terminal laporan harian (SPEC §18.2): `rich`, tanpa warna (aman untuk `terjadwal.log`).

Isi: kepala, banner, ringkasan aksi, status pasar + tabel syarat, posisi, tabel rencana, peringatan. Hasil berupa
daftar baris teks agar dicetak lewat fungsi `keluar` yang sama dengan perintah lain.
"""

from __future__ import annotations

import io

from rich import box
from rich.console import Console
from rich.table import Table

from .model import LaporanHarian, h, rp, status_pasar_ringkas

LEBAR = 120


def _tabel(judul: str, kolom: list[str]) -> Table:
    t = Table(title=judul, box=box.SIMPLE_HEAD, title_justify="left", show_lines=False, pad_edge=False)
    for k in kolom:
        t.add_column(k, overflow="fold")
    return t


def render_terminal(lap: LaporanHarian) -> list[str]:
    buf = io.StringIO()
    con = Console(file=buf, width=LEBAR, color_system=None, force_terminal=False, legacy_windows=False,
                  highlight=False, emoji=False, soft_wrap=False)
    for b in lap.kepala():
        con.print(b, markup=False)
    for b in lap.banner:
        con.print(f"!! {b}", markup=False)
    con.print("")
    con.print("Ringkasan aksi:", markup=False)
    for a in lap.ringkasan_aksi:
        con.print(f"  - {a}", markup=False)
    con.print("")
    con.print(f"Status pasar: {status_pasar_ringkas(lap.kondisi)}", markup=False)
    if lap.kondisi is not None:
        t = _tabel("", ["Syarat", "Kondisi", "Nilai", "Ambang", ""])
        for s in lap.kondisi.syarat:
            t.add_row(s.aturan, s.uraian, s.nilai, s.ambang, s.tanda)
        con.print(t)
    if lap.posisi:
        t = _tabel("Posisi dipegang", ["Kode", "Strategi", "Saran", "Stop t+1", "Target", "P&L", "Alasan"])
        for s in lap.posisi:
            t.add_row(s.kode, s.strategi, s.teks_saran, h(s.stop_besok), h(s.target),
                      f"{rp(s.pnl_idr)} ({s.teks_r})", s.alasan)
        con.print(t)
    if lap.rencana:
        t = _tabel("Rencana beli (limit di harga_maks; tarik bila tidak terisi saat pembukaan)",
                   ["Kode", "Strategi", "Skor", "Lot", "Limit", "Stop", "Target", "Risiko", "Nilai"])
        for r in lap.rencana:
            t.add_row(r.kode, r.strategi, r.skor, str(r.lot), h(r.harga_maks), h(r.stop), r.tampilan_target,
                      f"{rp(r.risiko_idr)} ({r.risiko_pct})", rp(r.nilai_idr))
        con.print(t)
    elif lap.status_data_run != "BELUM_TERSEDIA":
        con.print("Rencana beli: tidak ada REKOMENDASI.", markup=False)
    if lap.daftar_pantau:
        con.print(f"Daftar pantau (JANGAN BELI): {', '.join(f'{k.kode} {k.strategi}' for k in lap.daftar_pantau[:10])}",
                  markup=False)
    if lap.peringatan:
        con.print("")
        con.print("Peringatan:", markup=False)
        for p in lap.peringatan:
            con.print(f"  - {p}", markup=False)
    if lap.jurnal is not None:
        j = lap.jurnal
        if j.sudah_tercatat:
            con.print("Jurnal: sudah tercatat (tidak ada baris baru).", markup=False)
        if j.hari_dikejar:
            con.print(f"Jurnal: {j.hari_dikejar} hari dikejar; {j.sinyal_susulan} sinyal susulan tidak pernah dilaporkan.",
                      markup=False)
        if j.ekuitas is not None:
            con.print(f"Jurnal kertas: ekuitas {'≈ ' if j.perkiraan else ''}{rp(j.ekuitas)} "
                      f"({len(j.terbuka)} posisi terbuka).", markup=False)
    if lap.kandidat:
        con.print(lap.catatan_skor, markup=False)
    return [baris.rstrip() for baris in buf.getvalue().splitlines()]

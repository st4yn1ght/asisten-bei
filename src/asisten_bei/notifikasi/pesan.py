"""Isi pesan Telegram (SPEC §18.3): status pasar + satu baris alasan, ringkasan aksi (termasuk pengingat menarik
order yang tidak terisi saat pembukaan), rencana beli singkat, perubahan level stop, dan peringatan penting.
Semua teks dinamis di-escape; setiap pesan diakhiri "BUKAN SARAN INVESTASI." (ditambahkan `telegram.pecah`)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import teks
from ..kondisi_pasar import TAMPILAN_STATUS
from .telegram import BARIS_DISCLAIMER, baris_butir, e

if TYPE_CHECKING:  # pragma: no cover
    from ..laporan.model import LaporanHarian

MAKS_PERINGATAN = 6
PENGINGAT_TARIK = "Tarik order beli yang tidak terisi saat pembukaan; jangan biarkan antre sepanjang hari."


def _h(x) -> str:
    from ..laporan.model import h

    return h(x)


def pesan_harian(lap: "LaporanHarian", *, kode_keluar: int = 0) -> str:
    untuk = lap.t1.isoformat() if lap.t1 else teks.STRIP
    baris = [f"<b>Asisten BEI — data {e(lap.t.isoformat())}, untuk {e(untuk)}</b>"]
    if lap.replay:
        baris.append("<i>REPLAY (--tanggal)</i>")
    baris.append(f"Status data: {e(lap.status_data_run)}" + (f" (kode keluar {kode_keluar})" if kode_keluar else ""))
    for b in lap.banner:
        baris.append(f"<b>{e(b)}</b>")
    k = lap.kondisi
    if k is not None:
        baris.append(f"<b>Status pasar: {e(TAMPILAN_STATUS.get(k.status, k.status))}</b> — {e(k.alasan)}")
    baris.append("")
    baris.append("<b>Ringkasan aksi</b>")
    baris += baris_butir(lap.ringkasan_aksi)
    if lap.rencana:
        baris.append("")
        baris.append("<b>Rencana beli</b>")
        for r in lap.rencana:
            target = _h(r.target[-1][2]) if r.target else ("trailing" if r.strategi == "BO" else "keluar SMA")
            baris.append(f"• {e(r.kode)} {e(r.strategi)}: {r.lot} lot, limit <code>{e(_h(r.harga_maks))}</code>, "
                         f"stop <code>{e(_h(r.stop))}</code>, target {e(target)}, risiko {e(teks.rupiah(r.risiko_idr))}")
            for g in r.ketergantungan:
                baris.append(f"  {e(g)}")
        baris.append(f"<i>{e(PENGINGAT_TARIK)}</i>")
    stop = [s for s in lap.posisi if s.stop_besok is not None and s.stop_hari_ini is not None
            and s.stop_besok != s.stop_hari_ini]
    if stop:
        baris.append("")
        baris.append("<b>Perubahan level stop (order otomatis)</b>")
        for s in stop:
            baris.append(f"• {e(s.kode)} {e(s.strategi)}: {e(_h(s.stop_hari_ini))} → <code>{e(_h(s.stop_besok))}</code>")
    if lap.peringatan or lap.jurnal_peringatan:
        baris.append("")
        baris.append("<b>Peringatan</b>")
        daftar = list(lap.peringatan)
        if lap.jurnal_peringatan and not any("Jurnal gagal" in p for p in daftar):
            daftar.insert(0, f"Jurnal gagal: {lap.jurnal_peringatan}")
        baris += baris_butir(daftar, MAKS_PERINGATAN)
    baris.append("")
    baris.append("<i>Rincian lengkap: laporan.html</i>")
    baris.append(BARIS_DISCLAIMER)
    return "\n".join(baris)


def pesan_error(judul: str, rincian: str | list[str]) -> str:
    """Pesan error (§18.3 "Wajib"): kode 1, 2, 3, 5, 6 dalam mode --jadwal."""
    daftar = [rincian] if isinstance(rincian, str) else list(rincian)
    return "\n".join([f"<b>Asisten BEI: {e(judul)}</b>", *(e(x) for x in daftar[:20]), BARIS_DISCLAIMER])


def pesan_info(judul: str, rincian: str = "") -> str:
    return "\n".join(x for x in (f"<b>Asisten BEI: {e(judul)}</b>", e(rincian) if rincian else "", BARIS_DISCLAIMER) if x)

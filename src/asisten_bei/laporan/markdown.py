"""`laporan.md` (SPEC §18.2): semua bagian §18.1. Grafik hanya tersedia di HTML (catatan teks di sini)."""

from __future__ import annotations

from .. import DISCLAIMER
from .dokumen import Daftar, Grafik, Paragraf, Tabel, bangun_dokumen, glosarium
from .model import LaporanHarian


def _sel(x: str) -> str:
    return " ".join(str(x).split()).replace("|", "\\|")


def _blok(b) -> list[str]:
    if isinstance(b, Paragraf):
        if b.gaya == "banner":
            return [f"> **{b.teks}**", ""]
        if b.gaya == "peringatan":
            return [f"> {b.teks}", ""]
        if b.gaya == "tebal":
            return [f"**{b.teks}**", ""]
        if b.gaya == "kecil":
            return [f"_{b.teks}_", ""]
        return [b.teks, ""]
    if isinstance(b, Daftar):
        return [f"- {x}" for x in b.butir] + [""]
    if isinstance(b, Tabel):
        baris = ["| " + " | ".join(_sel(k) for k in b.kolom) + " |",
                 "|" + "|".join("---" for _ in b.kolom) + "|"]
        baris += ["| " + " | ".join(_sel(x) for x in r) + " |" for r in b.baris]
        return baris + [""]
    if isinstance(b, Grafik):
        return [f"_(Grafik {b.judul or b.kode}: lihat laporan.html)_", ""]
    raise TypeError(type(b))  # pragma: no cover


def render_markdown(lap: LaporanHarian) -> str:
    baris = ["BUKAN SARAN INVESTASI", ""]
    for i, bagian in enumerate([*bangun_dokumen(lap), glosarium()]):
        baris.append(("# " if i == 0 else "## ") + bagian.judul)
        baris.append("")
        for b in bagian.blok:
            baris.extend(_blok(b))
    baris.append(DISCLAIMER)
    return "\n".join(baris).rstrip("\n") + "\n"

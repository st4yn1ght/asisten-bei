"""`laporan.html` (SPEC §18.2): berkas mandiri — CSS inline, grafik PNG base64, tanpa JavaScript atau URL
eksternal — dirender dengan jinja2 (`templates/laporan.html.j2`, autoescape aktif). Memuat semua bagian §18.1
ditambah glosarium (§4.2)."""

from __future__ import annotations

import base64

from jinja2 import Environment, PackageLoader, select_autoescape

from .. import DISCLAIMER, DISCLAIMER_SINGKAT
from ..backtest.laporan_bt import _CSS
from .dokumen import Daftar, Grafik, Paragraf, Tabel, bangun_dokumen, glosarium
from .model import LaporanHarian

_KELAS = {"banner": "banner", "peringatan": "peringatan", "kecil": "catatan", "tebal": "disclaimer"}
_ENV: Environment | None = None


def _env() -> Environment:
    global _ENV
    if _ENV is None:
        _ENV = Environment(loader=PackageLoader("asisten_bei.laporan", "templates"),
                           autoescape=select_autoescape(["html", "j2"]), keep_trailing_newline=True,
                           trim_blocks=True, lstrip_blocks=True)
    return _ENV


def _png(lap: LaporanHarian, g: Grafik) -> bytes | None:
    from . import grafik

    data = lap.grafik
    pasar = data.get("pasar")
    if pasar is None:
        return None
    if g.jenis == "saham":
        return grafik.grafik_saham(pasar, g.kode, lap.t, data.get("level", {}).get(g.kode, {}), g.judul)
    if g.jenis == "ihsg":
        return grafik.grafik_ihsg(pasar, lap.t)
    if g.jenis == "breadth":
        return grafik.grafik_breadth(pasar, lap.t, data.get("breadth", []))
    return None


def _blok(lap: LaporanHarian, b) -> dict | None:
    if isinstance(b, Paragraf):
        return {"jenis": "paragraf", "teks": b.teks, "kelas": _KELAS.get(b.gaya, "")}
    if isinstance(b, Daftar):
        return {"jenis": "daftar", "butir": b.butir}
    if isinstance(b, Tabel):
        return {"jenis": "tabel", "kolom": b.kolom, "baris": b.baris}
    if isinstance(b, Grafik):
        png = _png(lap, b)
        if png is None:
            return {"jenis": "paragraf", "teks": f"Grafik {b.judul}: data tidak cukup.", "kelas": "catatan"}
        return {"jenis": "grafik", "judul": b.judul, "data": base64.b64encode(png).decode("ascii")}
    raise TypeError(type(b))  # pragma: no cover


def render_html(lap: LaporanHarian) -> str:
    bagian = []
    sudah: set[tuple[str, str]] = set()
    for bg in [*bangun_dokumen(lap), glosarium()]:
        blok = []
        for b in bg.blok:
            if isinstance(b, Grafik):
                kunci = (b.jenis, b.kode)
                if kunci in sudah:
                    continue
                sudah.add(kunci)
            x = _blok(lap, b)
            if x is not None:
                blok.append(x)
        bagian.append({"judul": bg.judul, "id": bg.id, "blok": blok})
    return _env().get_template("laporan.html.j2").render(
        judul=lap.judul, singkat=DISCLAIMER_SINGKAT, css=_CSS, bagian=bagian, disclaimer=DISCLAIMER,
    )

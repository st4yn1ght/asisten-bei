"""Grafik laporan harian (SPEC §18.2), PNG statis untuk `laporan.html` (matplotlib, backend Agg tanpa pyplot).

- Saham (setiap `REKOMENDASI` dan posisi dipegang): candlestick `laporan.bar_grafik` bar valid terakhir ≤ t
  (harga nominal pada t = harga cache × F(t)), SMA20/50/200 dari close valid (jendela ke belakang, kausal), dan
  garis harga_maks / stop / target berlabel langsung.
- IHSG: close + SMA `kondisi_pasar.sma_pendek` / `sma_panjang`.
- Breadth 120 hari bursa: persentase saham breadth terhitung di atas SMA `breadth_sma` (§10), dengan garis
  ambang BOLEH/JANGAN BELI.

Warna: lilin netral (naik = tinta redup, turun = tinta sekunder), seri garis memakai slot kategorikal tetap
(biru, jingga, aqua), level stop/target memakai warna status dengan label teks (tidak pernah warna saja).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

import pandas as pd

from .. import teks
from ..backtest.laporan_bt import _gambar_png, _legenda, _sumbu
from ..kondisi_pasar import hitung_breadth

if TYPE_CHECKING:  # pragma: no cover
    from ..data.tampilan import DataPasar

SERI = ("#2a78d6", "#eb6834", "#1baf7a")  # slot kategorikal 1–3
NAIK = "#898781"  # tinta redup
TURUN = "#52514e"  # tinta sekunder
STOP = "#d03b3b"  # status critical
TARGET = "#0ca30c"  # status good
LIMIT = "#52514e"
N_BREADTH = 120


def _sumbu_tanggal(ax, tanggal: list[date]) -> None:
    n = len(tanggal)
    if n == 0:
        return
    langkah = max(n // 6, 1)
    posisi = list(range(0, n, langkah))
    ax.set_xticks(posisi)
    ax.set_xticklabels([tanggal[i].strftime("%d-%m-%y") for i in posisi])
    ax.set_xlim(-1, n)


def grafik_saham(pasar: "DataPasar", kode: str, t: date, level: dict, judul: str) -> bytes | None:
    from matplotlib.figure import Figure
    from matplotlib.patches import Rectangle

    dv = pasar.tampilan(t)
    if not dv.ada(kode):
        return None
    valid = dv.valid(kode)
    if len(valid) == 0:
        return None
    f = float(dv.faktor_split(kode))
    n = int(pasar.cfg.laporan.bar_grafik)
    close_semua = valid["close"].astype("float64") * f
    sma = {p: close_semua.rolling(p, min_periods=p).mean() for p in (20, 50, 200)}
    v = valid.iloc[-n:]
    tanggal = [ts.date() for ts in v.index]
    o, hi, lo, c = (v[k].astype("float64").to_numpy() * f for k in ("open", "high", "low", "close"))
    fig = Figure(figsize=(9, 3.8))
    ax = fig.add_subplot(1, 1, 1)
    _sumbu(ax, judul)
    for i in range(len(v)):
        warna = NAIK if c[i] >= o[i] else TURUN
        ax.vlines(i, lo[i], hi[i], color=warna, linewidth=0.8)
        tinggi = abs(c[i] - o[i]) or (hi[i] - lo[i]) * 0.02 or 0.01
        ax.add_patch(Rectangle((i - 0.3, min(o[i], c[i])), 0.6, tinggi, facecolor=warna, edgecolor=warna,
                               linewidth=0.5))
    x = list(range(len(v)))
    for (p, s), warna in zip(sma.items(), SERI):
        y = s.iloc[-n:].to_numpy()
        ax.plot(x, y, color=warna, linewidth=1.4, label=f"SMA{p}")
    for nama, warna, gaya in (("harga_maks", LIMIT, "--"), ("stop", STOP, "-"), ("target", TARGET, "-")):
        nilai = level.get(nama)
        if nilai is None:
            continue
        y = float(Decimal(nilai))
        ax.axhline(y, color=warna, linewidth=1.0, linestyle=gaya)
        ax.annotate(f"{nama} {teks.angka(Decimal(nilai), 0 if Decimal(nilai) == Decimal(nilai).to_integral_value() else 2)}",
                    xy=(1, y), xycoords=("axes fraction", "data"), xytext=(-4, 3), textcoords="offset points",
                    ha="right", fontsize=8, color="#0b0b0b",
                    bbox={"boxstyle": "round,pad=0.2", "facecolor": "#fcfcfb", "edgecolor": "none", "alpha": 0.9})
    _sumbu_tanggal(ax, tanggal)
    _legenda(ax)
    fig.tight_layout()
    return _gambar_png(fig)


def grafik_ihsg(pasar: "DataPasar", t: date) -> bytes | None:
    from matplotlib.figure import Figure

    dv = pasar.tampilan(t)
    ih = dv.ihsg()
    if ih is None or len(ih.valid()) == 0:
        return None
    cfg = pasar.cfg
    n = int(cfg.laporan.bar_grafik)
    close = ih.valid()["close"].astype("float64")
    pendek, panjang = int(cfg.kondisi_pasar.sma_pendek), int(cfg.kondisi_pasar.sma_panjang)
    s1 = close.rolling(pendek, min_periods=pendek).mean()
    s2 = close.rolling(panjang, min_periods=panjang).mean()
    tanggal = [ts.date() for ts in close.index[-n:]]
    x = list(range(len(tanggal)))
    fig = Figure(figsize=(9, 3.2))
    ax = fig.add_subplot(1, 1, 1)
    _sumbu(ax, f"IHSG: close, SMA{pendek}, SMA{panjang}")
    ax.plot(x, close.iloc[-n:].to_numpy(), color="#0b0b0b", linewidth=1.2, label="close")
    ax.plot(x, s1.iloc[-n:].to_numpy(), color=SERI[0], linewidth=1.4, label=f"SMA{pendek}")
    ax.plot(x, s2.iloc[-n:].to_numpy(), color=SERI[1], linewidth=1.4, label=f"SMA{panjang}")
    _sumbu_tanggal(ax, tanggal)
    _legenda(ax)
    fig.tight_layout()
    return _gambar_png(fig)


def grafik_breadth(pasar: "DataPasar", t: date, kode_breadth: list[str]) -> bytes | None:
    from matplotlib.figure import Figure

    kal = pasar.kalender
    hari: list[date] = []
    d = t
    while len(hari) < N_BREADTH:
        if kal.adalah_hari_bursa(d):
            hari.append(d)
        try:
            d = kal.hari_bursa_sebelum(d)
        except ValueError:
            break
    hari = sorted(set(hari))
    nilai = []
    for d in hari:
        b = hitung_breadth(pasar.tampilan(d), kode_breadth).breadth_pct
        nilai.append(float(b) if b is not None else float("nan"))
    if not nilai or all(pd.isna(nilai)):
        return None
    cfg = pasar.cfg.kondisi_pasar
    fig = Figure(figsize=(9, 2.8))
    ax = fig.add_subplot(1, 1, 1)
    _sumbu(ax, f"Breadth: % saham terhitung di atas SMA{cfg.breadth_sma} ({len(hari)} hari bursa)")
    x = list(range(len(hari)))
    ax.plot(x, nilai, color=SERI[0], linewidth=1.6)
    for ambang, label in ((cfg.breadth_boleh_min_pct, "BOLEH BELI ≥"), (cfg.breadth_jangan_maks_pct, "JANGAN BELI <")):
        ax.axhline(float(ambang), color=LIMIT, linewidth=0.9, linestyle="--")
        ax.annotate(f"{label} {teks.angka(float(ambang), 0)}%", xy=(0, float(ambang)), xycoords=("axes fraction", "data"),
                    xytext=(4, 3), textcoords="offset points", fontsize=8, color="#0b0b0b")
    ax.set_ylim(0, 100)
    _sumbu_tanggal(ax, hari)
    fig.tight_layout()
    return _gambar_png(fig)

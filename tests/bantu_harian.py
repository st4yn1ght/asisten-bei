"""Pembantu uji run harian & jurnal (tahap 6): proyek sintetis berbasis `test_backtest_cli` (data acak berseed
2024-01-01 … 2025-06-30, indikator pendek, gerbang longgar), dengan satu run backtest `--mode edge` agar
`gate_status.json` ada. Proyek dasar dibangun sekali per sesi uji lalu disalin per uji.

Semua kode saham dan harga FIKTIF; tanpa jaringan dan tanpa tidur sungguhan (`test_backtest_cli._isolasi_mp`).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pytest
import yaml

import test_backtest_cli as tb
from asisten_bei import waktu
from asisten_bei.__main__ import main
from asisten_bei.data.cache import CacheProvider, buat_meta

WAKTU_BT = tb.WAKTU_BT
UBAH_HARIAN = {"laporan.format": ["terminal", "markdown", "csv"]}  # tanpa html/xlsx agar uji cepat


@dataclass
class HasilCLI:
    kode: int
    baris: list[str]

    @property
    def teks(self) -> str:
        return "\n".join(self.baris)


class ProyekHarian(tb.Proyek):
    def harian(self, saat: str, *opsi: str, config: Path | None = None, notifikasi: bool = False) -> HasilCLI:
        """`harian --offline [--tanpa-notifikasi] [opsi]` pada waktu virtual `saat` (ISO WIB; tanggal saja = 20:00).
        `notifikasi=True`: tanpa `--tanpa-notifikasi` (uji Telegram dengan server tiruan)."""
        bendera = () if notifikasi else ("--tanpa-notifikasi",)
        return self.cli(saat, "harian", "--offline", *bendera, *opsi, config=config)

    def cli(self, saat: str, *argv: str, config: Path | None = None) -> HasilCLI:
        if "T" not in saat:
            saat = f"{saat}T20:00:00+07:00"
        os.environ[waktu.ENV_WAKTU] = saat
        waktu.reset_virtual()
        baris: list[str] = []
        kode = main(["--config", str(config or self.config), "--akar", str(self.akar), *argv], keluar=baris.append)
        return HasilCLI(kode, baris)

    # -- jurnal ---------------------------------------------------------------------------

    @property
    def path_jurnal(self) -> Path:
        return self.akar / "data" / "jurnal.sqlite"

    def tabel(self, nama: str, where: str = "", param=()) -> list[dict]:
        con = sqlite3.connect(self.path_jurnal)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(f"SELECT * FROM {nama} {where}", param)]
        finally:
            con.close()

    def dump(self) -> dict[str, list[tuple]]:
        con = sqlite3.connect(self.path_jurnal)
        try:
            return {t: con.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()
                    for t in ("run", "sinyal", "hasil_sinyal", "kertas_periode", "kertas_transaksi", "kertas_ekuitas")}
        finally:
            con.close()

    # -- cache ----------------------------------------------------------------------------

    @property
    def cache(self) -> CacheProvider:
        return CacheProvider(self.akar / "data" / "cache" / "yfinance")

    def bar(self, kode: str) -> pd.DataFrame:
        return self.cache.baca(kode).bar.copy()

    def tulis_bar(self, kode: str, df: pd.DataFrame) -> None:
        self.cache.tulis(kode, df, buat_meta(df, sekarang=waktu.sekarang(), versi_provider="palsu-1.0"))

    def hapus_bar(self, kode: str, *tanggal: str) -> None:
        df = self.bar(kode)
        self.tulis_bar(kode, df.drop([pd.Timestamp(t) for t in tanggal]))

    def ubah_bar(self, kode: str, tanggal: str, **kolom: float) -> None:
        df = self.bar(kode)
        for k, v in kolom.items():
            df.loc[pd.Timestamp(tanggal), k] = v
        self.tulis_bar(kode, df)

    # -- config & input -------------------------------------------------------------------

    def ubah_config(self, ubah: dict) -> None:
        data = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        for kunci, nilai in ubah.items():
            *jalur, akhir = kunci.split(".")
            simpul = data
            for k in jalur:
                simpul = simpul[k]
            simpul[akhir] = nilai
        self.config.write_text("# BUKAN SARAN INVESTASI. Config uji (FIKTIF – KHUSUS UJI).\n"
                               + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def tulis_input(self, nama: str, teks: str) -> None:
        (self.akar / "input_uji" / nama).write_text(teks, encoding="utf-8")

    @property
    def folder_laporan(self) -> Path:
        return self.akar / "output" / "laporan"


def bangun_dasar(akar: Path) -> Path:
    """Proyek dasar + backtest mode edge (gate_status.json) pada `WAKTU_BT`."""
    with pytest.MonkeyPatch.context() as mp:
        tb._isolasi_mp(mp)
        mp.setenv(waktu.ENV_WAKTU, WAKTU_BT)
        mp.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        waktu.reset_virtual()
        p = tb.buat_proyek(akar, UBAH_HARIAN)
        baris: list[str] = []
        kode = main(p.argv("--mode", "edge"), keluar=baris.append)
        assert kode == 0, "\n".join(baris)
        assert (akar / "output" / "backtest" / "gate_status.json").is_file()
    return akar


def salin(dasar: Path, tujuan: Path) -> ProyekHarian:
    shutil.copytree(dasar, tujuan)
    return ProyekHarian(tujuan)

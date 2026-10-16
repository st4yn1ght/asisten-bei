"""Provider CSV manual: data cadangan di `data/manual/` (SPEC §6.9, §8.1).

Format `<KODE>.csv`: `tanggal,open,high,low,close,volume` (volume opsional untuk IHSG). Harga dan
volume harus sudah disesuaikan split dengan konvensi cache (§8.4); volume dalam lembar. Split dicatat
di berkas opsional `<KODE>_split.csv` (`tanggal,rasio`); tanpa berkas itu faktor split dianggap 1
dan hasil membawa catatan peringatan.

Aturan CSV umum §6 berlaku (lewat `inputs.baca_csv_ketat`): BOM diterima, pemisah `;`, koma desimal,
atau tanggal selain `YYYY-MM-DD` → GAGAL FORMAT dengan `PESAN_REGIONAL`. Kesalahan format menunjuk
nomor baris. Nilai yang terbaca tetapi tidak masuk akal (high < low, harga ≤ 0, ...) diteruskan apa
adanya: penilaiannya milik validasi per bar (§8.5). Tidak ada baris yang dibuat atau diisi (P1).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from ..inputs import KODE_IHSG, PESAN_REGIONAL, Masalah, baca_csv_ketat, parse_desimal, parse_tanggal
from .provider import KOLOM_BAR, NAMA_INDEX, HasilAmbil, gagal, ke_ts, split_kosong

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config

VERSI_CSV = "csv_manual"
CATATAN_TANPA_SPLIT = "tanpa berkas split: faktor split dianggap 1"
MAKS_MASALAH_DI_PESAN = 5

KOLOM_SPLIT_CSV: tuple[str, ...] = ("tanggal", "rasio")


def _ringkas_masalah(masalah: Sequence[Masalah]) -> str:
    """Gabungkan beberapa masalah menjadi satu pesan; PESAN_REGIONAL hanya ditulis sekali di akhir."""
    regional = any(PESAN_REGIONAL in m.pesan for m in masalah)
    bagian: list[str] = []
    for m in masalah[:MAKS_MASALAH_DI_PESAN]:
        teks = m.pesan.replace(PESAN_REGIONAL, "").strip()
        lokasi = m.berkas if m.baris is None else f"{m.berkas} baris {m.baris}"
        bagian.append(f"{lokasi}: {teks}")
    pesan = "; ".join(bagian)
    if len(masalah) > MAKS_MASALAH_DI_PESAN:
        pesan += f" (dan {len(masalah) - MAKS_MASALAH_DI_PESAN} masalah lain)"
    if regional:
        pesan += f". {PESAN_REGIONAL}"
    return pesan


class CsvManualProvider:
    """Membaca `<folder_manual>/<simbol>.csv` dengan validasi yang sama seperti provider lain."""

    nama = "csv_manual"

    def __init__(self, cfg: "Config", folder_manual: Path):
        self.cfg = cfg
        self.folder = Path(folder_manual)

    @property
    def versi_provider(self) -> str:
        return VERSI_CSV

    def simbol(self, kode: str) -> str:
        """"IHSG" → `data.simbol_ihsg.csv_manual`; saham memakai kodenya sendiri."""
        if kode == KODE_IHSG:
            return self.cfg.data.simbol_ihsg.csv_manual
        return kode

    def path_berkas(self, kode: str) -> Path:
        return self.folder / f"{self.simbol(kode)}.csv"

    def path_split(self, kode: str) -> Path:
        return self.folder / f"{self.simbol(kode)}_split.csv"

    def tersedia(self, kode: str) -> bool:
        return self.path_berkas(kode).is_file()

    def ambil_harian(self, kode: str, mulai: date, akhir: date) -> HasilAmbil:
        """Bar `mulai ≤ tanggal ≤ akhir` (akhir inklusif) dari berkas manual. Status "OK" bila sukses."""
        path = self.path_berkas(kode)
        nama = path.as_posix()
        if mulai > akhir:
            return gagal(kode, self.nama, "FORMAT",
                         f"{nama}: rentang tanggal tidak valid (mulai {mulai} > akhir {akhir})", VERSI_CSV)
        if not path.is_file():
            return gagal(kode, self.nama, "TIDAK_ADA_DATA", f"{nama}: berkas tidak ada", VERSI_CSV)

        adalah_indeks = kode == KODE_IHSG
        wajib = ["tanggal"] + [k for k in KOLOM_BAR if not (adalah_indeks and k == "volume")]
        opsional = ["volume"] if adalah_indeks else []
        baris, masalah = baca_csv_ketat(path, wajib, opsional, nama_berkas=nama)
        error = [m for m in masalah if m.level == "ERROR"]

        tanggal: list[date] = []
        nilai: dict[str, list[float]] = {k: [] for k in KOLOM_BAR}
        nomor_baris: list[int] = []
        for data in baris:
            ok = True
            try:
                d = parse_tanggal(data["tanggal"])
            except ValueError as e:
                error.append(Masalah("ERROR", nama, data["_baris"], f"kolom tanggal: {e}"))
                ok = False
            angka: dict[str, float] = {}
            for k in KOLOM_BAR:
                teks = data.get(k, "")
                if adalah_indeks and k == "volume" and teks == "":
                    angka[k] = np.nan  # volume IHSG opsional: kosong tetap kosong, tidak diisi
                    continue
                try:
                    angka[k] = float(parse_desimal(teks))
                except ValueError as e:
                    error.append(Masalah("ERROR", nama, data["_baris"], f"kolom {k}: {e}"))
                    ok = False
            if not ok:
                continue
            tanggal.append(d)
            nomor_baris.append(data["_baris"])
            for k in KOLOM_BAR:
                nilai[k].append(angka[k])

        akhir_pekan = [(d, n) for d, n in zip(tanggal, nomor_baris) if d.weekday() >= 5]
        if akhir_pekan:
            contoh = ", ".join(f"{d.isoformat()} (baris {n})" for d, n in akhir_pekan[:3])
            error.append(Masalah("ERROR", nama, None,
                                 f"tanggal/zona waktu tidak dikenali (bar bertanggal akhir pekan: {contoh})"))
        if error:
            return gagal(kode, self.nama, "FORMAT", _ringkas_masalah(error), VERSI_CSV)

        split, catatan, masalah_split = self._baca_split(kode)
        if masalah_split:
            return gagal(kode, self.nama, "FORMAT", _ringkas_masalah(masalah_split), VERSI_CSV)

        index = pd.DatetimeIndex(pd.to_datetime(tanggal), name=NAMA_INDEX).as_unit("ns")
        bar = pd.DataFrame({k: np.asarray(nilai[k], dtype="float64") for k in KOLOM_BAR}, index=index)
        awal_ts, akhir_ts = ke_ts(mulai), ke_ts(akhir)
        bar = bar[(bar.index >= awal_ts) & (bar.index <= akhir_ts)]
        # Urutan berkas boleh acak; duplikat dibiarkan untuk dinilai validasi_struktur (§8.5).
        bar = bar.sort_index(kind="mergesort")
        split = split[(split.index >= awal_ts) & (split.index <= akhir_ts)]
        if bar.empty:
            return gagal(kode, self.nama, "TIDAK_ADA_DATA",
                         f"{nama}: tidak ada bar dalam rentang {mulai}..{akhir}", VERSI_CSV)
        return HasilAmbil(
            kode=kode, sumber=self.nama, status="OK", jenis_error=None, pesan="", bar=bar,
            split=split, versi_provider=VERSI_CSV, catatan=catatan,
        )

    def _baca_split(self, kode: str) -> tuple[pd.Series, list[str], list[Masalah]]:
        """Seri split dari `<simbol>_split.csv`: (split, catatan, masalah ERROR).

        Rasio = lembar baru per lembar lama, harus > 0; rasio 1 berarti tidak ada split (dilewati).
        Tanggal split ganda → ERROR.
        """
        path = self.path_split(kode)
        nama = path.as_posix()
        if not path.is_file():
            return split_kosong(), [CATATAN_TANPA_SPLIT], []
        baris, masalah = baca_csv_ketat(path, list(KOLOM_SPLIT_CSV), nama_berkas=nama)
        error = [m for m in masalah if m.level == "ERROR"]
        isi: dict[date, float] = {}
        for data in baris:
            try:
                d = parse_tanggal(data["tanggal"])
                rasio = parse_desimal(data["rasio"])
            except ValueError as e:
                error.append(Masalah("ERROR", nama, data["_baris"], str(e)))
                continue
            if rasio <= 0:
                error.append(Masalah("ERROR", nama, data["_baris"], f"rasio split harus > 0 (tertulis {rasio})"))
                continue
            if d in isi:
                error.append(Masalah("ERROR", nama, data["_baris"], f"tanggal split {d.isoformat()} ganda"))
                continue
            isi[d] = float(rasio)
        if error:
            return split_kosong(), [], error
        pasangan = sorted((d, r) for d, r in isi.items() if r != 1.0)
        index = pd.DatetimeIndex(pd.to_datetime([d for d, _ in pasangan]), name=NAMA_INDEX).as_unit("ns")
        split = pd.Series([r for _, r in pasangan], index=index, name="split", dtype="float64")
        return split, [], []

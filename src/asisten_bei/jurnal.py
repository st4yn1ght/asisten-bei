"""Jurnal kertas (SPEC §17): SQLite `data/jurnal.sqlite`, aturan pembaruan, jalur per sinyal, portofolio
kertas, ekspor, rekonsiliasi, dan pemantauan penyimpangan.

**Tabel** (§17.1). Harga disimpan sebagai harga **nominal pada tanggalnya** (teks desimal), Rupiah 2 desimal,
R 4 desimal — format yang sama dengan berkas backtest (`laporan_bt.csv_*`), sehingga tidak ada derau float.

- `run`: satu baris per run harian yang mencatat (termasuk `BELUM_TERSEDIA`, tanpa sinyal). Kolom tambahan
  `hari_diproses` (JSON `[{"tanggal", "final", "susulan"}]`) mencatat hari yang diproses run itu (§17.3 #1–#2).
- `sinyal`: kunci unik `(tanggal_sinyal, kode, strategi)`; baris tidak pernah dihapus diam-diam (hanya
  `--catat-ulang`, yang dicatat di `run.catatan`). `target_harga` = target untuk harga isi = `harga_maks`.
  `bar_saat_sinyal` = OHLC × F(d) dan volume ÷ F(d) (sebelum `pengali_volume`) dari data saat dicatat.
- `hasil_sinyal`, `kertas_*`: turunan, dihitung ulang dari awal setiap run dari data terkini (§17.3 #5).
  `kertas_transaksi` = kolom `transaksi_*.csv` + `status_isi` (`TERISI`, atau `MENUNGGU` untuk order yang bar
  hari berikutnya belum ada). `kertas_ekuitas` + `perkiraan` (≈) dan `gerbang_dari_pemroses` (1 = tidak ada run
  sebelum `d+1` 00:00 WIB, sehingga gerbang memakai snapshot run yang memproses `d`, §17.2).

**Hari diproses** (§17.3 #1): hari bursa `d` dalam [awal, t] yang belum final; awal = hari pertama yang pernah
diproses, atau `jurnal.tanggal_mulai_kertas`, atau `t` pada run pertama. Final = diproses run `LENGKAP`, atau
≥ `maks_hari_tunggu_data` hari bursa sudah berlalu sejak `d`. `d < t` yang belum pernah diproses = susulan.

Semua penulisan satu run berada dalam satu transaksi SQLite: gagal di tengah tidak meninggalkan sebagian baris.
Berkas terkunci/rusak → `JurnalGagal` (kode keluar 6, §20.1).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from . import exit as ex
from . import teks
from .aturan_bei import AturanBEI
from .backtest import mesin
from .backtest.laporan_bt import KOLOM_TRANSAKSI, csv_harga, csv_r, csv_rupiah, csv_tanda, csv_tanggal
from .biaya import ke_decimal
from .inputs import BarisExclude, sektor_tidak_diketahui
from .kondisi_pasar import JANGAN_BELI
from .operasional import tulis_csv
from .seleksi import REKOMENDASI, STOP_TERLALU_JAUH, KandidatSeleksi
from .universe import AturanUniverse
from .waktu import ZONA

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config, KonteksRisiko
    from .data.tampilan import DataPasar, DataView
    from .kalender import Kalender
    from .portofolio import RekomendasiLalu

log = logging.getLogger("asisten_bei.jurnal")

TABEL: tuple[str, ...] = ("run", "sinyal", "hasil_sinyal", "kertas_periode", "kertas_transaksi", "kertas_ekuitas")
VERSI_SKEMA = "1"
BATAS_TUNGGU_KUNCI_DETIK = 5.0

KOLOM_KERTAS_TRANSAKSI: tuple[str, ...] = ("periode_id", *KOLOM_TRANSAKSI, "status_isi")
KOLOM_KERTAS_EKUITAS: tuple[str, ...] = (
    "periode_id", "tanggal", "kas", "nilai_posisi", "ekuitas", "perkiraan", "gerbang_dari_pemroses",
)

_SKEMA = f"""
CREATE TABLE IF NOT EXISTS meta (kunci TEXT PRIMARY KEY, nilai TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    waktu_run TEXT NOT NULL,
    tanggal_data TEXT NOT NULL,
    untuk_tanggal TEXT,
    status_data_run TEXT NOT NULL,
    status_pasar TEXT,
    alasan_pasar TEXT,
    hash_aturan TEXT,
    hash_input TEXT,
    versi_program TEXT,
    gerbang_snapshot TEXT,
    snapshot_input TEXT,
    peringatan_data TEXT,
    terlambat INTEGER NOT NULL DEFAULT 0,
    catatan TEXT,
    hari_diproses TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS sinyal (
    sinyal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    tanggal_sinyal TEXT NOT NULL,
    kode TEXT NOT NULL,
    strategi TEXT NOT NULL,
    skor REAL,
    status_pasar TEXT,
    status_live TEXT NOT NULL,
    alasan_live TEXT,
    lot_live INTEGER,
    risiko_live_idr TEXT,
    ditampilkan INTEGER NOT NULL DEFAULT 0,
    harga_ref TEXT,
    harga_maks TEXT,
    stop_awal TEXT,
    target_harga TEXT,
    rincian_aturan TEXT,
    bar_saat_sinyal TEXT,
    hash_aturan TEXT,
    susulan INTEGER NOT NULL DEFAULT 0,
    sementara INTEGER NOT NULL DEFAULT 0,
    data_direvisi INTEGER NOT NULL DEFAULT 0,
    tidak_muncul_di_data_final INTEGER NOT NULL DEFAULT 0,
    UNIQUE (tanggal_sinyal, kode, strategi)
);
CREATE TABLE IF NOT EXISTS hasil_sinyal (
    sinyal_id INTEGER PRIMARY KEY,
    status_isi TEXT,
    tanggal_entry TEXT,
    harga_entry TEXT,
    alasan_exit TEXT,
    tanggal_exit TEXT,
    harga_exit TEXT,
    r_bersih TEXT,
    hari INTEGER,
    tanda_transaksi TEXT
);
CREATE TABLE IF NOT EXISTS kertas_periode (
    periode_id INTEGER PRIMARY KEY AUTOINCREMENT,
    mulai TEXT NOT NULL,
    hash_aturan TEXT NOT NULL,
    modal_awal TEXT NOT NULL,
    selesai TEXT
);
CREATE TABLE IF NOT EXISTS kertas_transaksi (
    {", ".join(f"{k} {'INTEGER NOT NULL' if k == 'periode_id' else 'TEXT'}" for k in KOLOM_KERTAS_TRANSAKSI)}
);
CREATE TABLE IF NOT EXISTS kertas_ekuitas (
    periode_id INTEGER NOT NULL,
    tanggal TEXT NOT NULL,
    kas TEXT,
    nilai_posisi TEXT,
    ekuitas TEXT,
    perkiraan INTEGER NOT NULL DEFAULT 0,
    gerbang_dari_pemroses INTEGER NOT NULL DEFAULT 0
);
"""


class JurnalGagal(Exception):
    """`jurnal.sqlite` terkunci, rusak, atau tidak dapat ditulis (kode keluar 6)."""


def _tgl(s: str | None) -> date | None:
    return None if not s else date.fromisoformat(str(s)[:10])


def _d(x: Any) -> Decimal | None:
    if x is None or x == "":
        return None
    return Decimal(str(x))


# ---------------------------------------------------------------------------
# Koneksi
# ---------------------------------------------------------------------------


class Jurnal:
    """Koneksi ke `jurnal.sqlite`. Semua error SQLite diubah menjadi `JurnalGagal`."""

    def __init__(self, path: Path, *, timeout: float | None = None, baca_saja: bool = False):
        self.path = Path(path)
        self.baca_saja = baca_saja
        timeout = BATAS_TUNGGU_KUNCI_DETIK if timeout is None else timeout
        try:
            if baca_saja:
                if not self.path.exists():
                    raise JurnalGagal(f"{self.path.name} belum ada (belum ada run harian yang mencatat)")
                uri = f"file:{self.path.as_posix()}?mode=ro"
                self.con = sqlite3.connect(uri, uri=True, timeout=timeout, isolation_level=None)
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.con = sqlite3.connect(str(self.path), timeout=timeout, isolation_level=None)
            self.con.row_factory = sqlite3.Row
            if not baca_saja:
                self.con.executescript(_SKEMA)
                self.con.execute("INSERT OR IGNORE INTO meta (kunci, nilai) VALUES ('versi_skema', ?)", (VERSI_SKEMA,))
            else:
                self.con.execute("SELECT count(*) FROM run").fetchone()
        except sqlite3.Error as e:
            raise JurnalGagal(_pesan_sqlite(self.path, e)) from None

    def __enter__(self) -> "Jurnal":
        return self

    def __exit__(self, *exc) -> None:
        self.tutup()

    def tutup(self) -> None:
        try:
            self.con.close()
        except sqlite3.Error:  # pragma: no cover
            pass

    def q(self, sql: str, param: Sequence = ()) -> list[sqlite3.Row]:
        try:
            return self.con.execute(sql, param).fetchall()
        except sqlite3.Error as e:
            raise JurnalGagal(_pesan_sqlite(self.path, e)) from None

    def transaksi(self, kerja: Callable[[sqlite3.Connection], Any]) -> Any:
        """Jalankan `kerja` dalam satu transaksi (BEGIN IMMEDIATE … COMMIT; error → ROLLBACK + JurnalGagal)."""
        if self.baca_saja:
            raise JurnalGagal("jurnal dibuka baca-saja")
        try:
            self.con.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as e:
            raise JurnalGagal(_pesan_sqlite(self.path, e)) from None
        try:
            hasil = kerja(self.con)
            self.con.execute("COMMIT")
            return hasil
        except BaseException as e:
            try:
                self.con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if isinstance(e, sqlite3.Error):
                raise JurnalGagal(_pesan_sqlite(self.path, e)) from None
            raise

    # -- pertanyaan --------------------------------------------------------------------

    def runs(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM run ORDER BY run_id")

    def ada_run_lengkap(self, t: date) -> bool:
        return bool(self.q("SELECT 1 FROM run WHERE tanggal_data = ? AND status_data_run = 'LENGKAP' LIMIT 1",
                           (t.isoformat(),)))

    def hari_diproses(self) -> dict[date, bool]:
        """Hari yang pernah diproses → final (True bila salah satu pemrosesannya final)."""
        hasil: dict[date, bool] = {}
        for r in self.runs():
            for h in json.loads(r["hari_diproses"] or "[]"):
                d = date.fromisoformat(h["tanggal"])
                hasil[d] = hasil.get(d, False) or bool(h.get("final"))
        return hasil

    def sinyal(self, where: str = "", param: Sequence = ()) -> list[sqlite3.Row]:
        return self.q(f"SELECT * FROM sinyal {where} ORDER BY tanggal_sinyal, kode, strategi", param)

    def rekomendasi_ditampilkan(self, tanggal_sinyal: date) -> list["RekomendasiLalu"]:
        from .portofolio import RekomendasiLalu

        rows = self.sinyal("WHERE tanggal_sinyal = ? AND ditampilkan = 1 AND status_live = ?",
                           (tanggal_sinyal.isoformat(), REKOMENDASI))
        return [RekomendasiLalu(r["kode"], r["strategi"], tanggal_sinyal, Decimal(r["harga_maks"]),
                                Decimal(r["stop_awal"]), int(r["lot_live"] or 0)) for r in rows]

    def periode_berjalan(self) -> sqlite3.Row | None:
        rows = self.q("SELECT * FROM kertas_periode WHERE selesai IS NULL ORDER BY periode_id DESC LIMIT 1")
        return rows[0] if rows else None


def _pesan_sqlite(path: Path, e: sqlite3.Error) -> str:
    teks_e = str(e)
    if "locked" in teks_e or "busy" in teks_e:
        return f"{path.name} terkunci oleh proses lain ({teks_e}); tutup aplikasi yang membukanya"
    if "not a database" in teks_e or "malformed" in teks_e or "corrupt" in teks_e:
        return f"{path.name} rusak ({teks_e}); pindahkan berkas itu agar jurnal baru dibuat"
    return f"{path.name}: {type(e).__name__}: {teks_e}"


# ---------------------------------------------------------------------------
# Hari yang diproses (§17.3 #1–#2)
# ---------------------------------------------------------------------------


def hari_untuk_diproses(
    diproses: Mapping[date, bool], kal: "Kalender", t: date, tanggal_mulai: date | None
) -> list[date]:
    """Hari bursa (kalender manual) dalam [awal, t] yang belum final. Awal = min(hari yang pernah diproses,
    `tanggal_mulai_kertas`), atau `t` bila belum ada keduanya."""
    awal_kandidat = [d for d in (min(diproses) if diproses else None, tanggal_mulai) if d is not None]
    awal = min(awal_kandidat) if awal_kandidat else t
    if awal > t:
        return [] if diproses.get(t) else [t]
    return [d for d in kal.daftar_hari_bursa(awal, t, manual=True) if not diproses.get(d, False)]


def final_pada(d: date, t: date, status_run: str, kal: "Kalender", maks_hari_tunggu: int) -> bool:
    """Himpunan sinyal `d` final: diproses run `LENGKAP`, atau ≥ `maks_hari_tunggu` hari bursa sudah berlalu."""
    if status_run == "LENGKAP":
        return True
    berlalu = len(kal.daftar_hari_bursa(d + timedelta(days=1), t, manual=True)) if d < t else 0
    return berlalu >= int(maks_hari_tunggu)


# ---------------------------------------------------------------------------
# Snapshot input & gerbang
# ---------------------------------------------------------------------------


def snapshot_input(universe: Iterable[tuple[str, str]], exclude: Iterable[BarisExclude], hari: Iterable[date]) -> dict:
    """`snapshot_input` (§17.1): kode + sektor universe, dan baris exclude yang aktif pada salah satu hari yang
    diproses run itu (lengkap dengan rentang tanggalnya; keaktifan per hari dinilai saat dipakai)."""
    hari = list(hari)
    aktif = [b for b in exclude if any(b.aktif_pada(d) for d in hari)]
    return {
        "universe": [{"kode": k, "sektor": s} for k, s in universe],
        "exclude": [
            {"kode": b.kode, "alasan": b.alasan, "tanggal_mulai": csv_tanggal(b.tanggal_mulai),
             "tanggal_akhir": csv_tanggal(b.tanggal_akhir)}
            for b in aktif
        ],
    }


def aturan_dari_snapshot(snap: Mapping) -> AturanUniverse:
    """`AturanUniverse` live dari `snapshot_input` sebuah run (§17.2: eligibilitas kertas per tanggal)."""
    uni = [(str(u["kode"]), str(u["sektor"])) for u in snap.get("universe", [])]
    kode = tuple(dict.fromkeys(k for k, _s in uni))
    anggota = set(kode)
    sektor = dict(uni)
    excl: dict[str, list[BarisExclude]] = {}
    for e in snap.get("exclude", []):
        b = BarisExclude(str(e["kode"]), str(e.get("alasan", "")), _tgl(e.get("tanggal_mulai")),
                         _tgl(e.get("tanggal_akhir")))
        excl.setdefault(b.kode, []).append(b)

    def exclude_pada(k: str, d: date) -> BarisExclude | None:
        return next((b for b in excl.get(k, ()) if b.aktif_pada(d)), None)

    return AturanUniverse(
        konteks="live", kandidat=kode, anggota_pada=lambda k, d: k in anggota, exclude_pada=exclude_pada,
        sektor=lambda k: sektor.get(k) or sektor_tidak_diketahui(k), pakai_exclude=True,
    )


@dataclass(frozen=True)
class SnapshotHari:
    """Snapshot yang berlaku untuk hari `d` dalam simulasi kertas (§17.2)."""

    aturan: AturanUniverse
    lolos: frozenset[str]
    gerbang_dari_pemroses: bool


def snapshot_per_hari(jurnal: Jurnal) -> Callable[[date], SnapshotHari | None]:
    """Fungsi `d` → snapshot: eligibilitas dari run terakhir yang memproses `d` (atau run terdekat sebelumnya);
    gerbang dari run terakhir dengan `waktu_run` < `d+1` 00:00 WIB, atau dari run pemroses `d` (ditandai)."""
    runs = [r for r in jurnal.runs() if r["snapshot_input"]]
    pemroses: dict[date, sqlite3.Row] = {}
    for r in runs:
        for h in json.loads(r["hari_diproses"] or "[]"):
            pemroses[date.fromisoformat(h["tanggal"])] = r  # run yang lebih akhir menang
    urut_waktu = sorted(runs, key=lambda r: (datetime.fromisoformat(r["waktu_run"]), r["run_id"]))
    cache: dict[int, AturanUniverse] = {}

    def aturan(r: sqlite3.Row) -> AturanUniverse:
        if r["run_id"] not in cache:
            cache[r["run_id"]] = aturan_dari_snapshot(json.loads(r["snapshot_input"]))
        return cache[r["run_id"]]

    def lolos(r: sqlite3.Row) -> frozenset[str]:
        g = json.loads(r["gerbang_snapshot"] or "{}")
        return frozenset(k for k, v in g.items() if isinstance(v, Mapping) and v.get("lolos") is True)

    def pada(d: date) -> SnapshotHari | None:
        if not runs:
            return None
        r_d = pemroses.get(d)
        if r_d is None:
            sebelum = [x for x in pemroses if x <= d]
            r_d = pemroses[max(sebelum)] if sebelum else urut_waktu[0]
        batas = datetime.combine(d + timedelta(days=1), time(0), tzinfo=ZONA)
        sebelum_batas = [r for r in urut_waktu if datetime.fromisoformat(r["waktu_run"]) < batas]
        if sebelum_batas:
            return SnapshotHari(aturan(r_d), lolos(sebelum_batas[-1]), False)
        return SnapshotHari(aturan(r_d), lolos(r_d), True)

    return pada


# ---------------------------------------------------------------------------
# Pencatatan sinyal (§17.2 #1, §17.3)
# ---------------------------------------------------------------------------


@dataclass
class HariDiproses:
    """Satu hari yang diproses run ini: hasil seleksi §13.2 (status_live) dengan DataView(as_of=tanggal)."""

    tanggal: date
    status_pasar: str
    kandidat: Sequence[KandidatSeleksi]
    dv: "DataView"


@dataclass
class InfoRun:
    waktu_run: datetime
    tanggal_data: date
    untuk_tanggal: date | None
    status_data_run: str
    status_pasar: str | None
    alasan_pasar: str | None
    hash_aturan: str
    hash_input: str
    versi_program: str
    gerbang_snapshot: Mapping[str, Mapping]
    snapshot_input: Mapping | None
    peringatan_data: Sequence[str] = ()
    terlambat: bool = False
    catatan: list[str] = field(default_factory=list)


@dataclass
class HasilCatat:
    run_id: int | None = None
    sudah_tercatat: bool = False
    hari: list[date] = field(default_factory=list)
    hari_dikejar: list[date] = field(default_factory=list)  # susulan
    sinyal_baru: int = 0
    sinyal_susulan: int = 0
    dihapus: int = 0


def _bar_json(dv: "DataView", kode: str) -> str | None:
    if not dv.ada(kode):
        return None
    r = dv.seri(kode).bar_mentah()
    if r is None:
        return None
    f = ke_decimal(dv.faktor_split(kode))
    isi = {}
    for k in ("open", "high", "low", "close"):
        x = float(r[k])
        isi[k] = None if np.isnan(x) else csv_harga(ke_decimal(x) * f)
    v = float(r["volume"])
    isi["volume"] = None if np.isnan(v) else csv_harga(ke_decimal(v) / f)
    isi["sumber"] = str(r.get("sumber", "") or "")
    return json.dumps(isi, sort_keys=True, ensure_ascii=False)


def _rincian(k: KandidatSeleksi) -> str:
    sg = k.sinyal.sinyal
    c = k.sinyal.komponen
    isi = {
        "cek": [{"kode": x.kode, "uraian": x.uraian, "lolos": x.lolos} for x in sg.cek],
        "skor": {"k_sinyal": round(c.k_sinyal, 6), "k_rs": round(c.k_rs, 6), "k_likuid": round(c.k_likuid, 6),
                 "k_stop": round(c.k_stop, 6), "skor": round(c.skor, 6)},
        "alasan_tidak_valid": sg.alasan_tidak_valid,
        "fraksi_dipakai": sg.fraksi_dipakai,
    }
    return json.dumps(isi, sort_keys=True, ensure_ascii=False)


def target_untuk(sg, bei: AturanBEI) -> Decimal | None:
    """Target PB untuk harga isi = harga_maks (nominal pada tanggal sinyal); None tanpa target_r."""
    if sg.target_r is None or sg.harga_maks <= sg.stop:
        return None
    from .strategi.dasar import hitung_target

    return hitung_target(sg.harga_maks, sg.stop, sg.target_r, bei, sg.tanggal)


def _nilai_baris(k: KandidatSeleksi, h: HariDiproses, run_id: int, hash_aturan: str, bei: AturanBEI,
                 ditampilkan: bool, susulan: bool, sementara: bool) -> dict:
    sg = k.sinyal.sinyal
    sz = k.sizing
    return {
        "run_id": run_id, "tanggal_sinyal": h.tanggal.isoformat(), "kode": sg.kode, "strategi": sg.strategi,
        "skor": float(k.sinyal.skor), "status_pasar": h.status_pasar, "status_live": k.status,
        "alasan_live": k.alasan, "lot_live": int(sz.lot) if sz is not None else None,
        "risiko_live_idr": csv_rupiah(sz.risiko_idr) if sz is not None and sz.lot > 0 else None,
        "ditampilkan": int(ditampilkan), "harga_ref": csv_harga(sg.harga_ref), "harga_maks": csv_harga(sg.harga_maks),
        "stop_awal": csv_harga(sg.stop), "target_harga": csv_harga(target_untuk(sg, bei)) or None,
        "rincian_aturan": _rincian(k), "bar_saat_sinyal": _bar_json(h.dv, sg.kode), "hash_aturan": hash_aturan,
        "susulan": int(susulan), "sementara": int(sementara), "data_direvisi": 0, "tidak_muncul_di_data_final": 0,
    }


def catat(
    jurnal: Jurnal,
    info: InfoRun,
    hari: Sequence[HariDiproses],
    *,
    kal: "Kalender",
    cfg: "Config",
    catat_ulang: bool = False,
) -> HasilCatat:
    """Catat run dan sinyal hari-hari yang diproses (§17.3), dalam satu transaksi.

    - Sinyal yang sudah tercatat dipertahankan; yang tidak muncul lagi → `tidak_muncul_di_data_final = 1`.
    - `ditampilkan = 1` hanya untuk sinyal `t` yang muncul di laporan `t` saat `t` pertama kali diproses (atau
      dibangkitkan ulang dengan `--catat-ulang`); susulan dan sinyal tambahan bernilai 0.
    - `BELUM_TERSEDIA`: hanya baris `run`, tanpa sinyal (§17.3 #3)."""
    t = info.tanggal_data
    bei = AturanBEI(cfg)
    diproses_lama = jurnal.hari_diproses()
    hasil = HasilCatat()
    belum_tersedia = info.status_data_run == "BELUM_TERSEDIA"
    if belum_tersedia:
        hari = []

    def kerja(con: sqlite3.Connection) -> HasilCatat:
        catatan = list(info.catatan)
        if catat_ulang and not belum_tersedia:
            ids = [r[0] for r in con.execute("SELECT sinyal_id FROM sinyal WHERE tanggal_sinyal = ?", (t.isoformat(),))]
            if ids:
                con.executemany("DELETE FROM hasil_sinyal WHERE sinyal_id = ?", [(i,) for i in ids])
                con.execute("DELETE FROM sinyal WHERE tanggal_sinyal = ?", (t.isoformat(),))
            hasil.dihapus = len(ids)
            catatan.append(f"--catat-ulang: {len(ids)} baris sinyal tanggal {t.isoformat()} dihapus lalu dibangkitkan ulang")
        hari_json = []
        for h in hari:
            d = h.tanggal
            pertama = d not in diproses_lama
            final = final_pada(d, t, info.status_data_run, kal, int(cfg.jurnal.maks_hari_tunggu_data))
            susulan = d < t and pertama
            hari_json.append({"tanggal": d.isoformat(), "final": int(final), "susulan": int(susulan)})
            hasil.hari.append(d)
            if susulan:
                hasil.hari_dikejar.append(d)
        cur = con.execute(
            "INSERT INTO run (waktu_run, tanggal_data, untuk_tanggal, status_data_run, status_pasar, alasan_pasar, "
            "hash_aturan, hash_input, versi_program, gerbang_snapshot, snapshot_input, peringatan_data, terlambat, "
            "catatan, hari_diproses) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                info.waktu_run.isoformat(timespec="seconds"), t.isoformat(),
                csv_tanggal(info.untuk_tanggal) or None, info.status_data_run, info.status_pasar, info.alasan_pasar,
                info.hash_aturan, info.hash_input, info.versi_program,
                json.dumps(info.gerbang_snapshot, sort_keys=True, ensure_ascii=False, default=str),
                json.dumps(info.snapshot_input, sort_keys=True, ensure_ascii=False) if info.snapshot_input else None,
                "\n".join(info.peringatan_data) or None, int(info.terlambat), "\n".join(catatan) or None,
                json.dumps(hari_json, sort_keys=True),
            ),
        )
        run_id = int(cur.lastrowid)
        hasil.run_id = run_id
        for h, hj in zip(hari, hari_json):
            d = h.tanggal
            final, susulan = bool(hj["final"]), bool(hj["susulan"])
            pertama = d not in diproses_lama or (catat_ulang and d == t)
            ada = {(r[0], r[1]): r[2] for r in con.execute(
                "SELECT kode, strategi, sinyal_id FROM sinyal WHERE tanggal_sinyal = ?", (d.isoformat(),))}
            baru = {(k.kode, k.strategi) for k in h.kandidat}
            for k in h.kandidat:
                if (k.kode, k.strategi) in ada:
                    continue
                nilai = _nilai_baris(k, h, run_id, info.hash_aturan, bei, ditampilkan=(d == t and pertama),
                                     susulan=susulan, sementara=not final)
                kolom = ", ".join(nilai)
                con.execute(f"INSERT INTO sinyal ({kolom}) VALUES ({', '.join('?' * len(nilai))})", list(nilai.values()))
                hasil.sinyal_baru += 1
                hasil.sinyal_susulan += int(susulan)
            for kunci, sid in ada.items():
                con.execute("UPDATE sinyal SET sementara = ?, tidak_muncul_di_data_final = ? WHERE sinyal_id = ?",
                            (int(not final), int(kunci not in baru), sid))
        return hasil

    return jurnal.transaksi(kerja)


# ---------------------------------------------------------------------------
# Revisi bar (§17.3 #6)
# ---------------------------------------------------------------------------


def _berbeda(a: float, b: float, toleransi_pct: float) -> bool:
    if np.isnan(a) or np.isnan(b):
        return np.isnan(a) != np.isnan(b)
    return abs(b - a) * 100.0 > toleransi_pct * abs(a)


def bar_direvisi(bar_json: str | None, dv: "DataView", kode: str, cfg: "Config", d: date) -> bool:
    """`bar_saat_sinyal` (nominal pada saat dicatat) ÷ F_terkini(d) berbeda dari bar `d` data terkini melewati
    toleransi (`toleransi_revisi_pct` untuk OHLC, `toleransi_revisi_volume_pct` untuk volume), atau barnya hilang."""
    if not bar_json:
        return False
    lama = json.loads(bar_json)
    if not dv.ada(kode):
        return True
    r = dv.seri(kode).bar_mentah()
    if r is None:
        return True
    f = float(dv.faktor_split(kode, d))
    tol, tol_v = float(cfg.data.toleransi_revisi_pct), float(cfg.data.toleransi_revisi_volume_pct)
    for k in ("open", "high", "low", "close"):
        if lama.get(k) is None:
            continue
        if _berbeda(float(lama[k]) / f, float(r[k]), tol):
            return True
    if lama.get("volume") is not None and _berbeda(float(lama["volume"]) * f, float(r["volume"]), tol_v):
        return True
    return False


def tandai_revisi(jurnal: Jurnal, pasar: "DataPasar", t: date) -> int:
    """Tandai `data_direvisi = 1` (tidak pernah dikembalikan ke 0) untuk sinyal ≤ t yang barnya direvisi."""
    cfg = pasar.cfg
    rows = jurnal.sinyal("WHERE data_direvisi = 0 AND tanggal_sinyal <= ?", (t.isoformat(),))
    ubah = []
    for r in rows:
        d = date.fromisoformat(r["tanggal_sinyal"])
        if bar_direvisi(r["bar_saat_sinyal"], pasar.tampilan(d), r["kode"], cfg, d):
            ubah.append(r["sinyal_id"])
    if ubah:
        jurnal.transaksi(lambda con: con.executemany(
            "UPDATE sinyal SET data_direvisi = 1 WHERE sinyal_id = ?", [(i,) for i in ubah]))
    return len(ubah)


# ---------------------------------------------------------------------------
# Turunan: hasil_sinyal & kertas_* (§17.2 #2–#3, §17.3 #5)
# ---------------------------------------------------------------------------


@dataclass
class HasilTurunan:
    periode_id: int | None = None
    periode_baru: bool = False
    periode_ditutup: int | None = None
    transaksi_berubah: int = 0  # transaksi kertas tertutup yang berubah/hilang karena revisi data
    hari_gerbang_ditandai: list[date] = field(default_factory=list)
    n_hasil_sinyal: int = 0


def _baris_kertas(periode_id: int, pos: ex.Posisi, cfg: "Config", kal: "Kalender", pasar: "DataPasar",
                  akhir: date) -> dict:
    k = pos.keluar
    if k is None:
        k = ex.tutup_terbuka(pos, akhir, cfg, ex._f(pasar.tampilan(akhir).faktor_split(pos.kode)))
    f = pos.faktor_entry
    return {
        "periode_id": periode_id, "kode": pos.kode, "strategi": pos.strategi, "sektor": pos.sektor,
        "tanggal_sinyal": csv_tanggal(pos.tanggal_sinyal), "tanggal_entry": csv_tanggal(pos.tanggal_entry),
        "tanggal_exit": csv_tanggal(k.tanggal), "alasan_exit": k.alasan, "tanda_transaksi": csv_tanda(pos.tanda),
        "lembar": csv_harga(pos.lembar / f), "harga_beli": csv_harga(pos.harga_beli),
        "harga_beli_nominal": csv_harga(pos.harga_beli * f), "harga_exit": csv_harga(k.harga),
        "harga_exit_nominal": csv_harga(k.harga_nominal), "stop_awal": csv_harga(pos.stop_awal),
        "stop_awal_nominal": csv_harga(pos.stop_awal * f), "target": csv_harga(pos.target),
        "target_nominal": csv_harga(None if pos.target is None else pos.target * f),
        "biaya_beli_idr": csv_rupiah(pos.biaya_beli_idr), "fee_beli_idr": csv_rupiah(pos.fee_beli_idr),
        "fee_jual_idr": csv_rupiah(k.fee_jual_idr), "slippage_idr": csv_rupiah(pos.slippage_beli_idr + k.slippage_idr),
        "hasil_jual_idr": csv_rupiah(k.hasil_jual_idr), "pnl_idr": csv_rupiah(k.pnl_idr),
        "risiko_awal_idr": csv_rupiah(pos.risiko_awal_idr), "r": csv_r(k.r),
        "hari": str(kal.nomor_hari(pos.tanggal_entry, k.tanggal)), "skor": csv_harga(pos.meta.get("skor")),
        "status_pasar_sinyal": str(pos.meta.get("status_pasar") or ""), "pembatas_lot": str(pos.meta.get("pembatas") or ""),
        "status_isi": ex.TERISI,
    }


def _baris_menunggu(periode_id: int, s: mesin.SinyalDilewati) -> dict:
    isi = {k: "" for k in KOLOM_KERTAS_TRANSAKSI}
    isi.update({"periode_id": periode_id, "kode": s.kode, "strategi": s.strategi, "tanggal_sinyal": csv_tanggal(s.tanggal),
                "skor": csv_harga(s.skor), "status_isi": mesin.MENUNGGU})
    return isi


def _kunci_kertas(r: Mapping) -> tuple:
    return (r["kode"], r["strategi"], r["tanggal_sinyal"])


def hitung_ulang_turunan(
    jurnal: Jurnal,
    pasar: "DataPasar",
    t: date,
    *,
    keputusan_pada: Callable[[date, AturanUniverse], mesin.KeputusanHari],
    hash_aturan: str,
    konteks_kertas: "KonteksRisiko",
    strategi_aktif: Sequence[str],
    tanggal_mulai_kertas: date | None,
) -> HasilTurunan:
    """Hitung ulang `hasil_sinyal` (semua sinyal kecuali STOP_TERLALU_JAUH) dan `kertas_*` periode berjalan
    dari data terkini (§17.3 #5). `keputusan_pada(d, aturan)`: keputusan close hari `d` dengan eligibilitas
    `aturan` (DataView(as_of=d) saja)."""
    cfg = pasar.cfg
    kal = pasar.kalender
    hasil = HasilTurunan()
    snap = snapshot_per_hari(jurnal)
    cache: dict[date, mesin.KeputusanHari] = {}

    def keputusan(d: date) -> mesin.KeputusanHari | None:
        if d not in cache:
            s = snap(d)
            if s is None:
                return None
            cache[d] = keputusan_pada(d, s.aturan)
        return cache[d]

    # -- hasil_sinyal ---------------------------------------------------------------
    rows = [r for r in jurnal.sinyal("WHERE tanggal_sinyal <= ?", (t.isoformat(),)) if r["status_live"] != STOP_TERLALU_JAUH]
    baris_hasil: list[tuple] = []
    if rows:
        mulai = date.fromisoformat(rows[0]["tanggal_sinyal"])
        hari = [d for d in kal.daftar_hari_bursa(mulai, t)]
        kep = [k for k in (keputusan(d) for d in hari) if k is not None]
        peta: dict[tuple, mesin.JalurSinyal] = {}
        for s in sorted({r["strategi"] for r in rows}):
            for j in mesin.jalur_per_sinyal(kep, pasar, s, lambda k: ""):
                peta[(j.tanggal_sinyal.isoformat(), j.kode, j.strategi)] = j
        for r in rows:
            j = peta.get((r["tanggal_sinyal"], r["kode"], r["strategi"]))
            if j is None:
                baris_hasil.append((r["sinyal_id"], None, None, None, None, None, None, None, None, None))
                continue
            baris_hasil.append((
                r["sinyal_id"], j.status_isi, csv_tanggal(j.tanggal_entry) or None, csv_harga(j.harga_entry) or None,
                j.alasan_exit, csv_tanggal(j.tanggal_exit) or None, csv_harga(j.harga_exit) or None,
                csv_r(j.r) or None, j.hari, csv_tanda(j.tanda) or None,
            ))
    hasil.n_hasil_sinyal = len(baris_hasil)

    # -- kertas ---------------------------------------------------------------------
    periode = jurnal.periode_berjalan()
    tutup_id = None
    if periode is not None and periode["hash_aturan"] != hash_aturan:
        tutup_id = int(periode["periode_id"])
        periode = None
    modal = Decimal(konteks_kertas.modal_idr)
    if periode is None:
        if tutup_id is not None:
            mulai_kertas = t
        else:
            diproses = jurnal.hari_diproses()
            kandidat = [d for d in (tanggal_mulai_kertas, min(diproses) if diproses else None) if d is not None]
            mulai_kertas = min(kandidat) if kandidat else t
        periode_id, periode_mulai = None, mulai_kertas
    else:
        periode_id, periode_mulai = int(periode["periode_id"]), date.fromisoformat(periode["mulai"])
        modal = Decimal(periode["modal_awal"])

    kertas_rows: list[dict] = []
    ekuitas_rows: list[tuple] = []
    hari_kertas = kal.daftar_hari_bursa(periode_mulai, t) if periode_mulai <= t else []
    kep_kertas = [k for k in (keputusan(d) for d in hari_kertas) if k is not None]
    tanda_gerbang: dict[date, bool] = {}
    if kep_kertas:
        aktif = frozenset(strategi_aktif)

        def lolos_pada(d: date) -> frozenset[str]:
            s = snap(d)
            tanda_gerbang[d] = bool(s and s.gerbang_dari_pemroses)
            return (s.lolos if s else frozenset()) & aktif

        def sektor_pada(d: date) -> Callable[[str], str]:
            s = snap(d)
            return s.aturan.sektor if s else sektor_tidak_diketahui

        run = mesin.SpesifikasiRun("gabungan_lolos", aktif, True, frozenset())
        kons = replace(konteks_kertas, modal_idr=modal)
        h = mesin.simulasikan(
            kep_kertas, pasar, run, mode="realistis", konteks=kons, sektor_dari=sektor_tidak_diketahui,
            tanggal_awal=kal.hari_bursa_sebelum(kep_kertas[0].tanggal), lolos_pada=lolos_pada, sektor_pada=sektor_pada,
        )
        hasil.hari_gerbang_ditandai = sorted(d for d, v in tanda_gerbang.items() if v)
        for pos in h.transaksi:
            kertas_rows.append(_baris_kertas(0, pos, cfg, kal, pasar, kep_kertas[-1].tanggal))
        for s in h.dilewati:
            if s.status_isi == mesin.MENUNGGU:
                kertas_rows.append(_baris_menunggu(0, s))
        for b in h.ekuitas:
            ekuitas_rows.append((b.tanggal.isoformat(), csv_rupiah(b.kas), csv_rupiah(b.nilai_posisi),
                                 csv_rupiah(b.ekuitas), int(b.perkiraan), int(tanda_gerbang.get(b.tanggal, False))))

    def kerja(con: sqlite3.Connection) -> None:
        nonlocal periode_id
        con.execute("DELETE FROM hasil_sinyal")
        con.executemany("INSERT INTO hasil_sinyal VALUES (?,?,?,?,?,?,?,?,?,?)", baris_hasil)
        if tutup_id is not None:
            con.execute("UPDATE kertas_periode SET selesai = ? WHERE periode_id = ?", (t.isoformat(), tutup_id))
            hasil.periode_ditutup = tutup_id
        if periode_id is None:
            cur = con.execute("INSERT INTO kertas_periode (mulai, hash_aturan, modal_awal, selesai) VALUES (?,?,?,NULL)",
                              (periode_mulai.isoformat(), hash_aturan, csv_rupiah(modal)))
            periode_id = int(cur.lastrowid)
            hasil.periode_baru = True
        hasil.periode_id = periode_id
        lama = [dict(r) for r in con.execute("SELECT * FROM kertas_transaksi WHERE periode_id = ?", (periode_id,))]
        baru = [{**r, "periode_id": periode_id} for r in kertas_rows]
        peta_baru = {_kunci_kertas(r): r for r in baru}
        for r in lama:
            if r.get("status_isi") != ex.TERISI or r.get("alasan_exit") == ex.TERBUKA:
                continue
            n = peta_baru.get(_kunci_kertas(r))
            if n is None or any(str(n.get(k) or "") != str(r.get(k) or "") for k in KOLOM_KERTAS_TRANSAKSI):
                hasil.transaksi_berubah += 1
        con.execute("DELETE FROM kertas_transaksi WHERE periode_id = ?", (periode_id,))
        con.execute("DELETE FROM kertas_ekuitas WHERE periode_id = ?", (periode_id,))
        kolom = ", ".join(KOLOM_KERTAS_TRANSAKSI)
        con.executemany(f"INSERT INTO kertas_transaksi ({kolom}) VALUES ({', '.join('?' * len(KOLOM_KERTAS_TRANSAKSI))})",
                        [[r.get(k) or None if k != "periode_id" else r[k] for k in KOLOM_KERTAS_TRANSAKSI] for r in baru])
        con.executemany("INSERT INTO kertas_ekuitas VALUES (?,?,?,?,?,?,?)", [(periode_id, *e) for e in ekuitas_rows])

    jurnal.transaksi(kerja)
    return hasil


# ---------------------------------------------------------------------------
# Ekspor (§17.1)
# ---------------------------------------------------------------------------

_URUT = {
    "run": "run_id", "sinyal": "sinyal_id", "hasil_sinyal": "sinyal_id", "kertas_periode": "periode_id",
    "kertas_transaksi": "periode_id, tanggal_entry, kode, strategi, tanggal_sinyal", "kertas_ekuitas": "periode_id, tanggal",
}


def tabel_df(jurnal: Jurnal, nama: str) -> pd.DataFrame:
    try:
        cur = jurnal.con.execute(f"SELECT * FROM {nama} ORDER BY {_URUT[nama]}")
        kolom = [c[0] for c in cur.description]
        rows = cur.fetchall()
    except sqlite3.Error as e:
        raise JurnalGagal(_pesan_sqlite(jurnal.path, e)) from None
    return pd.DataFrame([tuple(r) for r in rows], columns=kolom, dtype=object)


def ekspor(jurnal: Jurnal, folder: Path) -> tuple[list[Path], list[str]]:
    """`<folder>/<tabel>.csv` untuk setiap tabel, baris pertama disclaimer. (path, peringatan berkas terkunci)."""
    hasil, peringatan = [], []
    for nama in TABEL:
        path, pesan = tulis_csv(Path(folder) / f"{nama}.csv", tabel_df(jurnal, nama).fillna(""))
        hasil.append(path)
        if pesan:
            peringatan.append(pesan)
    return hasil, peringatan


# ---------------------------------------------------------------------------
# Rekonsiliasi (§17.3 #9)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Selisih:
    tanggal: date
    kode: str
    strategi: str
    jenis: str  # "tambah" | "hilang" | "level"
    rincian: str
    petunjuk: str


def rekonsiliasi(
    jurnal: Jurnal,
    keputusan_pada: Callable[[date, AturanUniverse], mesin.KeputusanHari],
    *,
    hash_aturan: str,
    bei: AturanBEI,
    strategi_aktif: Sequence[str],
    sampai: date,
) -> list[Selisih]:
    """Bandingkan sinyal yang ditampilkan live (`ditampilkan = 1`) dengan sinyal yang dibangkitkan ulang dari
    data terkini pada tanggal yang sama (eligibilitas dari `snapshot_input`, strategi aktif config ini)."""
    snap = snapshot_per_hari(jurnal)
    tampil = jurnal.sinyal("WHERE ditampilkan = 1 AND tanggal_sinyal <= ?", (sampai.isoformat(),))
    tanggal = sorted({date.fromisoformat(r["tanggal_sinyal"]) for r in tampil} | {
        date.fromisoformat(r["tanggal_data"]) for r in jurnal.runs()
        if r["status_data_run"] != "BELUM_TERSEDIA" and r["hari_diproses"] not in (None, "[]")
        and date.fromisoformat(r["tanggal_data"]) <= sampai
    })
    semua = {(r["tanggal_sinyal"], r["kode"], r["strategi"]): r for r in jurnal.sinyal()}
    aktif = set(strategi_aktif)
    hasil: list[Selisih] = []
    for d in tanggal:
        s = snap(d)
        if s is None:
            continue
        kep = keputusan_pada(d, s.aturan)
        ulang = {(x.kode, x.strategi): x.sinyal for x in kep.sinyal if x.strategi in aktif}
        lama = {(r["kode"], r["strategi"]): r for r in tampil if r["tanggal_sinyal"] == d.isoformat()}

        def petunjuk(r: Mapping | None) -> str:
            if r is None:
                return ""
            bagian = []
            if r["sementara"]:
                bagian.append("sementara")
            if r["data_direvisi"]:
                bagian.append("data_direvisi")
            if r["hash_aturan"] != hash_aturan:
                bagian.append("hash berbeda")
            return ", ".join(bagian) or "tidak diketahui"

        for kunci, r in lama.items():
            sg = ulang.get(kunci)
            if sg is None:
                hasil.append(Selisih(d, *kunci, "hilang", "tidak muncul di data terkini", petunjuk(r)))
                continue
            beda = []
            for nama, nilai in (("harga_maks", sg.harga_maks), ("stop_awal", sg.stop),
                                ("target_harga", target_untuk(sg, bei))):
                if (csv_harga(nilai) or None) != (r[nama] or None):
                    beda.append(f"{nama} {r[nama] or '—'} → {csv_harga(nilai) or '—'}")
            if beda:
                hasil.append(Selisih(d, *kunci, "level", "; ".join(beda), petunjuk(r)))
        for kunci in sorted(set(ulang) - set(lama)):
            r = semua.get((d.isoformat(), *kunci))
            hasil.append(Selisih(d, *kunci, "tambah",
                                 "muncul di data terkini tetapi tidak ditampilkan live",
                                 petunjuk(r) if r is not None else "tidak tercatat di jurnal"))
    return hasil


# ---------------------------------------------------------------------------
# Statistik & pemantauan penyimpangan (§17.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Penyimpangan:
    strategi: str
    n_kertas: int
    rata_r_kertas: Decimal | None
    n_holdout: int
    persentil: float | None  # persentil ke-`persentil_peringatan` rata-rata bootstrap
    peringatan: bool
    catatan: str


def sampel_kertas(jurnal: Jurnal, strategi: str, *, hash_aturan: str, setelah: date | None) -> list[Decimal]:
    """R unit TERISI yang sudah tertutup (§17.4): hash periode berjalan, tanggal sinyal sesudah tanggal data
    gerbang, tanpa JANGAN_BELI dan STOP_TERLALU_JAUH. Tanpa deduplikasi."""
    rows = jurnal.q(
        "SELECT s.tanggal_sinyal, h.r_bersih FROM sinyal s JOIN hasil_sinyal h ON h.sinyal_id = s.sinyal_id "
        "WHERE s.strategi = ? AND s.hash_aturan = ? AND s.status_pasar != ? AND s.status_live != ? "
        "AND h.status_isi = ? AND h.alasan_exit IS NOT NULL AND h.alasan_exit != ? AND h.r_bersih IS NOT NULL "
        "ORDER BY s.tanggal_sinyal, s.kode",
        (strategi, hash_aturan, JANGAN_BELI, STOP_TERLALU_JAUH, ex.TERISI, ex.TERBUKA),
    )
    return [Decimal(r["r_bersih"]) for r in rows if setelah is None or date.fromisoformat(r["tanggal_sinyal"]) > setelah]


def sampel_holdout(path_sinyal_r: Path, holdout_mulai: date | None) -> list[Decimal] | None:
    """R holdout dari `sinyal_r_<strategi>.csv` dengan filter yang sama (TERISI tertutup, bukan JANGAN_BELI).
    None bila berkas tidak ada/tidak terbaca."""
    try:
        df = pd.read_csv(path_sinyal_r, comment="#", dtype=str, keep_default_na=False)
    except (OSError, ValueError):
        return None
    hasil = []
    for _i, r in df.iterrows():
        if r.get("status_isi") != ex.TERISI or r.get("status_pasar") == JANGAN_BELI:
            continue
        if not r.get("r") or r.get("alasan_exit") in ("", ex.TERBUKA):
            continue
        if holdout_mulai is not None and date.fromisoformat(r["tanggal_sinyal"]) < holdout_mulai:
            continue
        hasil.append(Decimal(r["r"]))
    return hasil


def uji_penyimpangan(
    r_kertas: Sequence[Decimal], r_holdout: Sequence[Decimal], cfg: "Config", strategi: str
) -> Penyimpangan:
    """Bootstrap (§17.4): rata-rata R jurnal < persentil ke-`persentil_peringatan` dari rata-rata `n_kertas`
    sampel dengan pengembalian dari distribusi holdout (`bootstrap_iterasi` kali, `bootstrap_seed`)."""
    j = cfg.jurnal
    n = len(r_kertas)
    rata = (sum(r_kertas, Decimal(0)) / n) if n else None
    if n < int(j.min_transaksi_peringatan):
        return Penyimpangan(strategi, n, rata, len(r_holdout), None, False,
                            f"sampel jurnal {n} < {int(j.min_transaksi_peringatan)}: belum diuji")
    if not r_holdout:
        return Penyimpangan(strategi, n, rata, 0, None, False, "distribusi holdout tidak tersedia")
    rng = np.random.default_rng(int(j.bootstrap_seed))
    dist = np.array([float(x) for x in r_holdout], dtype="float64")
    idx = rng.integers(0, len(dist), size=(int(j.bootstrap_iterasi), n))
    rata_boot = dist[idx].mean(axis=1)
    p = float(np.percentile(rata_boot, float(j.persentil_peringatan)))
    peringatan = float(rata) < p
    catatan = (f"rata-rata R jurnal {teks.angka(rata, 2)} < persentil ke-{teks.angka(j.persentil_peringatan, 0)} "
               f"bootstrap holdout {teks.angka(p, 2)}") if peringatan else (
               f"rata-rata R jurnal {teks.angka(rata, 2)} ≥ persentil ke-{teks.angka(j.persentil_peringatan, 0)} "
               f"bootstrap holdout {teks.angka(p, 2)}")
    return Penyimpangan(strategi, n, rata, len(r_holdout), p, peringatan, catatan)


# ---------------------------------------------------------------------------
# Ringkasan untuk laporan harian (§18.1 #12)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatistikStrategi:
    strategi: str
    n: int
    rata_r: Decimal | None
    pf_r: str
    n_holdout: int | None  # None = sinyal_r holdout tidak tersedia
    rata_r_holdout: Decimal | None
    pf_r_holdout: str


@dataclass
class RingkasanJurnal:
    periode_id: int | None = None
    periode_mulai: date | None = None
    modal_awal: Decimal | None = None
    tanggal_ekuitas: date | None = None
    ekuitas: Decimal | None = None
    kas: Decimal | None = None
    perkiraan: bool = False
    terbuka: list[dict] = field(default_factory=list)
    menunggu: list[dict] = field(default_factory=list)
    tertutup_30: list[dict] = field(default_factory=list)
    statistik: list[StatistikStrategi] = field(default_factory=list)
    penyimpangan: list[Penyimpangan] = field(default_factory=list)
    hari_dikejar: int = 0
    sinyal_susulan: int = 0
    sudah_tercatat: bool = False
    transaksi_berubah: int = 0
    data_direvisi: int = 0
    hari_gerbang_ditandai: list[date] = field(default_factory=list)
    periode_ditutup: int | None = None
    catatan: list[str] = field(default_factory=list)


def ringkas(
    jurnal: Jurnal,
    t: date,
    *,
    cfg: "Config",
    gate: Mapping[str, Mapping] | None,
    path_run: Callable[[str], Path],
    strategi: Sequence[str],
    hasil_catat: HasilCatat | None = None,
    hasil_turunan: HasilTurunan | None = None,
    n_revisi: int = 0,
) -> RingkasanJurnal:
    """Isi bagian jurnal laporan: posisi kertas terbuka, transaksi tertutup 30 hari terakhir, statistik per
    strategi vs holdout, penyimpangan (§17.4), dan jumlah hari yang dikejar. `gate`: entri gate_status.json;
    `path_run(folder_run)`: path folder run backtest (relatif akar)."""
    r = RingkasanJurnal()
    if hasil_catat is not None:
        r.hari_dikejar = len(hasil_catat.hari_dikejar)
        r.sinyal_susulan = hasil_catat.sinyal_susulan
        r.sudah_tercatat = hasil_catat.sudah_tercatat
    if hasil_turunan is not None:
        r.transaksi_berubah = hasil_turunan.transaksi_berubah
        r.hari_gerbang_ditandai = list(hasil_turunan.hari_gerbang_ditandai)
        r.periode_ditutup = hasil_turunan.periode_ditutup
    r.data_direvisi = n_revisi
    periode = jurnal.periode_berjalan()
    hash_periode = None
    if periode is not None:
        r.periode_id = int(periode["periode_id"])
        r.periode_mulai = date.fromisoformat(periode["mulai"])
        r.modal_awal = Decimal(periode["modal_awal"])
        hash_periode = periode["hash_aturan"]
        eq = jurnal.q("SELECT * FROM kertas_ekuitas WHERE periode_id = ? ORDER BY tanggal DESC LIMIT 1", (r.periode_id,))
        if eq:
            r.tanggal_ekuitas = date.fromisoformat(eq[0]["tanggal"])
            r.ekuitas, r.kas = Decimal(eq[0]["ekuitas"]), Decimal(eq[0]["kas"])
            r.perkiraan = bool(eq[0]["perkiraan"])
        batas = (t - timedelta(days=30)).isoformat()
        for x in jurnal.q("SELECT * FROM kertas_transaksi WHERE periode_id = ? "
                          "ORDER BY tanggal_entry, kode, strategi, tanggal_sinyal", (r.periode_id,)):
            d = dict(x)
            if d["status_isi"] == mesin.MENUNGGU:
                r.menunggu.append(d)
            elif d["alasan_exit"] == ex.TERBUKA:
                r.terbuka.append(d)
            elif (d["tanggal_exit"] or "") >= batas:
                r.tertutup_30.append(d)
    from .backtest.metrik import profit_factor

    for s in strategi:
        e = (gate or {}).get(s) or {}
        tgl_data = _tgl(e.get("tanggal_data_terakhir")) if isinstance(e, Mapping) else None
        kertas = sampel_kertas(jurnal, s, hash_aturan=hash_periode, setelah=tgl_data) if hash_periode else []
        holdout = None
        if isinstance(e, Mapping) and e.get("folder_run"):
            holdout = sampel_holdout(path_run(str(e["folder_run"])) / f"sinyal_r_{s.lower()}.csv",
                                     _tgl(e.get("holdout_mulai")))
        r.statistik.append(StatistikStrategi(
            s, len(kertas), (sum(kertas, Decimal(0)) / len(kertas)) if kertas else None, profit_factor(kertas).teks(),
            None if holdout is None else len(holdout),
            (sum(holdout, Decimal(0)) / len(holdout)) if holdout else None,
            profit_factor(holdout or []).teks() if holdout is not None else teks.STRIP,
        ))
        r.penyimpangan.append(uji_penyimpangan(kertas, holdout or [], cfg, s))
    return r


__all__ = [
    "HariDiproses", "HasilCatat", "RingkasanJurnal", "StatistikStrategi", "ringkas", "HasilTurunan", "InfoRun", "Jurnal", "JurnalGagal", "Penyimpangan", "Selisih",
    "TABEL", "aturan_dari_snapshot", "bar_direvisi", "catat", "ekspor", "final_pada", "hari_untuk_diproses",
    "hitung_ulang_turunan", "rekonsiliasi", "sampel_holdout", "sampel_kertas", "snapshot_input",
    "snapshot_per_hari", "tabel_df", "tandai_revisi", "uji_penyimpangan",
]

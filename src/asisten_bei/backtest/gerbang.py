"""Gerbang backtest (SPEC §16.6), hash (§16.1), `gate_status.json`, log holdout (§16.4), dan subset
terjangkau (§16.7).

**Hash.** Config dibuang dulu bagian yang tidak mengubah hasil simulasi (§16.1, `BAGIAN_DIKECUALIKAN`,
`KUNCI_DIKECUALIKAN`, kunci `data.jeda_*`, dan semua kunci path berkas/folder), lalu diserialisasi kanonik
(kunci urut, `Decimal` dinormalkan, tanggal ISO) sehingga hash deterministik dan tidak bergantung pada
format penulisan YAML. `hash_aturan[X]` hanya memuat `strategi.X` dari bagian `strategi`; `hash_aturan`
global memuat semua strategi kecuali `acak`. Keduanya memuat `versi_program`. `hash_input` = SHA-256 dari
isi berkas input (`berkas_input`).

**Gerbang.** G1–G6 dinilai dari metrik mode `edge` run strategi itu sendiri; setiap syarat mencatat nilai,
ambang, lolos, dan uraian. ACAK tidak pernah dinilai gerbang.

**Berkas.** `gerbang_run.json` (folder run) dan `output/backtest/gate_status.json` (digabung per
strategi; hanya ditulis bila run memuat mode `edge`) memakai JSON deterministik (kunci urut, indent 2,
UTF-8, baris baru di akhir) dengan kunci tetap `catatan` (disclaimer); dua run yang sama hanya berbeda di
`dibuat_pada`. `holdout_log.csv` ditambah satu baris per run × mode yang menghitung holdout; baris pertama
berkas = disclaimer. Semua penulisan atomik (`operasional.tulis_atomik`).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import DISCLAIMER, DISCLAIMER_CSV, teks, waktu
from .. import exit as ex
from ..biaya import ke_decimal
from ..config import resolusi_konteks
from ..kondisi_pasar import parameter_entry
from ..operasional import tulis_atomik
from ..sizing import hitung_sizing
from . import metrik as mt

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config, KonteksApp
    from .jendela import MetrikPeriode

log = logging.getLogger(__name__)

STRATEGI_GERBANG: tuple[str, ...] = ("PB", "BO", "MR")  # ACAK tidak pernah dinilai gerbang (§12.4)
STRATEGI_SEMUA: tuple[str, ...] = ("PB", "BO", "MR", "ACAK")
MODE_GERBANG = "edge"

FOLDER_BACKTEST = Path("output") / "backtest"
NAMA_GATE_STATUS = "gate_status.json"
NAMA_GERBANG_RUN = "gerbang_run.json"
NAMA_HOLDOUT_LOG = "holdout_log.csv"
KOLOM_HOLDOUT_LOG: tuple[str, ...] = (
    "waktu", "holdout_mulai", "hash_aturan", "hash_input", "strategi", "mode", "pf_r_holdout", "n_holdout",
)

PESAN_JALANKAN = "Jalankan backtest.bat terlebih dahulu"
PANJANG_RINCIAN = 16  # karakter heksadesimal per hash rincian (hanya untuk menyebut bagian yang berubah)
DESIMAL_JSON = 6

# ---------------------------------------------------------------------------
# Serialisasi kanonik
# ---------------------------------------------------------------------------


def _teks_decimal(x: Decimal) -> str:
    """Decimal kanonik: 1.0, 1.00, dan 1 → "1"; 100 → "100" (bukan "1E+2")."""
    if not x.is_finite():
        raise ValueError(f"Decimal tidak hingga/NaN tidak bisa di-hash: {x}")
    n = x.normalize()
    return format(n, "f") if n != 0 else "0"


def ke_kanonik(obj: Any) -> Any:
    """Bentuk JSON kanonik: Decimal → teks ternormalisasi, date/datetime → ISO, set → daftar urut,
    model pydantic → dict. Float NaN/tak hingga ditolak."""
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            raise ValueError(f"float tidak hingga/NaN tidak bisa di-hash: {obj}")
        return obj
    if isinstance(obj, Decimal):
        return _teks_decimal(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Mapping):
        return {str(k): ke_kanonik(v) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        return sorted(ke_kanonik(v) for v in obj)
    if isinstance(obj, (list, tuple)):
        return [ke_kanonik(v) for v in obj]
    if hasattr(obj, "model_dump"):
        return ke_kanonik(obj.model_dump(mode="python"))
    raise TypeError(f"tipe tidak didukung untuk serialisasi kanonik: {type(obj).__name__}")


def serial_kanonik(obj: Any) -> str:
    return json.dumps(ke_kanonik(obj), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def sha256_teks(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def json_deterministik(obj: Any) -> str:
    """JSON keluaran: kunci urut, indent 2, UTF-8 apa adanya, diakhiri baris baru."""
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


# ---------------------------------------------------------------------------
# Hash aturan (config)
# ---------------------------------------------------------------------------

BAGIAN_DIKECUALIKAN: tuple[str, ...] = ("laporan", "notifikasi", "jurnal", "operasional", "jadwal")
KUNCI_DIKECUALIKAN: tuple[str, ...] = (
    "akun.maks_umur_hari_bursa",
    "pasar.jadwal_sesi",
    "pasar.fraksi_harga.sumber",
    "pasar.fraksi_harga.tanggal_cek",
    "pasar.fraksi_harga.papan_berlaku",
    "pasar.auto_reject.sumber",
    "pasar.auto_reject.tanggal_cek",
    "pasar.auto_reject.papan_berlaku",
    "biaya.terverifikasi",
    "data.pengali_volume_terverifikasi",
    "data.maks_simbol_kena_batas_berturut",
    "data.coba_lagi_tidak_ada_data_hari",
    "data.tunggu_data_hari_ini",
    "backtest.maks_pf_r_acak",
    "gerbang.kedaluwarsa_hari",
)
AWALAN_DIKECUALIKAN: tuple[str, ...] = ("data.jeda_",)


def _kunci_path(nama: str) -> bool:
    """Kunci path berkas/folder (§16.1: dikecualikan dari semua hash)."""
    return nama in ("file", "folder") or nama.startswith(("file_", "folder_"))


def _buang(simpul: Any, jalur: str) -> Any:
    if not isinstance(simpul, dict):
        return simpul
    hasil = {}
    for k, v in simpul.items():
        kunci = f"{jalur}.{k}" if jalur else str(k)
        if not jalur and k in BAGIAN_DIKECUALIKAN:
            continue
        if kunci in KUNCI_DIKECUALIKAN or kunci.startswith(AWALAN_DIKECUALIKAN) or _kunci_path(str(k)):
            continue
        hasil[k] = _buang(v, kunci)
    return hasil


def config_terhash(cfg: "Config") -> dict:
    """Config (bentuk kanonik) tanpa bagian yang dikecualikan §16.1, termasuk seluruh `strategi`."""
    return ke_kanonik(_buang(cfg.model_dump(mode="python"), ""))


def _versi_program() -> str:
    import asisten_bei

    return str(asisten_bei.__version__)


def _isi_aturan(cfg: "Config", strategi: str | None) -> dict:
    sisa = config_terhash(cfg)
    semua = sisa.pop("strategi")
    if strategi is None:
        sisa["strategi"] = {k: v for k, v in semua.items() if k != "acak"}
    else:
        x = strategi.lower()
        if x not in semua:
            raise ValueError(f"strategi tidak dikenal: {strategi!r}")
        sisa["strategi"] = {x: semua[x]}
    return {"config": sisa, "versi_program": _versi_program()}


def hash_aturan_strategi(cfg: "Config", strategi: str) -> str:
    """`hash_aturan[X]` (§16.1): config tersisa dengan hanya `strategi.X`, plus versi program."""
    return sha256_teks(serial_kanonik(_isi_aturan(cfg, strategi)))


def hash_aturan_global(cfg: "Config") -> str:
    """`hash_aturan` global (periode kertas & log holdout): semua strategi kecuali `acak`."""
    return sha256_teks(serial_kanonik(_isi_aturan(cfg, None)))


def rincian_aturan(cfg: "Config", strategi: str | None = None) -> dict[str, str]:
    """Hash pendek per bagian config yang ikut hash (bagian tingkat atas; `strategi.<x>` per strategi;
    `versi_program`) — untuk menyebut bagian yang berubah (§16.6 run harian)."""
    isi = _isi_aturan(cfg, strategi)
    hasil = {}
    for bagian, nilai in isi["config"].items():
        if bagian == "strategi":
            for x, v in nilai.items():
                hasil[f"strategi.{x}"] = sha256_teks(serial_kanonik(v))[:PANJANG_RINCIAN]
        else:
            hasil[bagian] = sha256_teks(serial_kanonik(nilai))[:PANJANG_RINCIAN]
    hasil["versi_program"] = sha256_teks(isi["versi_program"])[:PANJANG_RINCIAN]
    return dict(sorted(hasil.items()))


# ---------------------------------------------------------------------------
# Hash input (berkas)
# ---------------------------------------------------------------------------

BERKAS_INPUT_HASH: tuple[str, ...] = (
    "universe.csv", "universe_history.csv", "breadth_universe.csv", "anomali_ok.csv", "holidays.csv", "exclude.csv",
)


def berkas_input(ctx: "KonteksApp") -> dict[str, Path]:
    """Berkas yang ikut `hash_input` (§16.1), nama peran → path: universe.csv, breadth_universe.csv,
    holidays.csv (selalu); universe_history.csv dan anomali_ok.csv bila ada; exclude.csv bila
    `backtest.pakai_exclude`."""
    cfg = ctx.cfg
    hasil = {
        "universe.csv": ctx.path(cfg.universe.file),
        "breadth_universe.csv": ctx.path(cfg.kondisi_pasar.file_breadth_universe),
        "holidays.csv": ctx.path(cfg.pasar.file_libur),
    }
    for nama, rel in (("universe_history.csv", cfg.universe.file_riwayat), ("anomali_ok.csv", cfg.data.file_anomali_ok)):
        p = ctx.path(rel)
        if p.is_file():
            hasil[nama] = p
    if cfg.backtest.pakai_exclude:
        hasil["exclude.csv"] = ctx.path(cfg.universe.file_exclude)
    return {k: hasil[k] for k in BERKAS_INPUT_HASH if k in hasil}


def _hash_berkas(ctx: "KonteksApp") -> dict[str, str | None]:
    hasil: dict[str, str | None] = {}
    for nama, p in berkas_input(ctx).items():
        try:
            hasil[nama] = hashlib.sha256(p.read_bytes()).hexdigest()
        except FileNotFoundError:
            hasil[nama] = None  # berkas wajib tidak ada: tercatat sebagai "tidak ada"
    return hasil


def hash_input(ctx: "KonteksApp") -> str:
    """`hash_input` (§16.1): SHA-256 atas isi setiap berkas `berkas_input` (byte apa adanya)."""
    return sha256_teks(serial_kanonik({"berkas": _hash_berkas(ctx)}))


def rincian_input(ctx: "KonteksApp") -> dict[str, str | None]:
    return {k: (v[:PANJANG_RINCIAN] if v else None) for k, v in _hash_berkas(ctx).items()}


def hash8(hash_aturan: str, hash_input_: str) -> str:
    """8 karakter pertama SHA-256 gabungan `hash_aturan` (global) dan `hash_input` (§16.1)."""
    return sha256_teks(hash_aturan + hash_input_)[:8]


def nama_folder_run(hash8_: str, saat: datetime | None = None) -> str:
    """`<YYYYMMDD-HHMMSS>_<hash8>` (§16.1), jam WIB dari `waktu.sekarang()`."""
    saat = waktu.sekarang() if saat is None else waktu.ke_wib(saat)
    return f"{saat:%Y%m%d-%H%M%S}_{hash8_}"


@dataclass(frozen=True)
class IdentitasHash:
    hash_aturan: str  # hash_aturan[X], atau global bila strategi None
    hash_input: str
    rincian: dict  # {"aturan": {bagian: hash}, "input": {berkas: hash}}


def identitas_hash(ctx: "KonteksApp", strategi: str | None) -> IdentitasHash:
    """Hash aturan (per strategi, atau global bila None), hash input, dan rinciannya untuk kondisi saat ini."""
    cfg = ctx.cfg
    ha = hash_aturan_global(cfg) if strategi is None else hash_aturan_strategi(cfg, strategi)
    return IdentitasHash(ha, hash_input(ctx), {"aturan": rincian_aturan(cfg, strategi), "input": rincian_input(ctx)})


# ---------------------------------------------------------------------------
# Gerbang G1–G6
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HasilSyarat:
    kode: str  # G1..G6
    nilai: Any  # int / Decimal / NilaiPF / None
    ambang: Any
    lolos: bool
    uraian: str


@dataclass(frozen=True)
class HasilGerbang:
    strategi: str
    syarat: tuple[HasilSyarat, ...]

    @property
    def lolos(self) -> bool:
        return all(s.lolos for s in self.syarat)

    @property
    def gagal(self) -> list[HasilSyarat]:
        return [s for s in self.syarat if not s.lolos]

    def alasan(self) -> str:
        """Uraian syarat yang gagal, dipisah "; " (kosong bila lolos)."""
        return "; ".join(s.uraian for s in self.gagal)


def _a(x: Any, desimal: int = 2) -> str:
    return teks.angka(x, desimal)


def _tanda(ok: bool, ya: str, tidak: str) -> str:
    return ya if ok else tidak


def nilai_gerbang(
    strategi: str,
    *,
    holdout: mt.MetrikTransaksi,
    in_sample: mt.MetrikTransaksi,
    jendela: Sequence[mt.MetrikTransaksi],
    penuh: mt.MetrikTransaksi,
    cfg: "Config",
) -> HasilGerbang:
    """G1–G6 (§16.6) dari metrik mode `edge` run strategi itu sendiri. `jendela`: metrik transaksi SEMUA
    jendela in-sample; yang memenuhi syarat (≥ `min_transaksi_per_jendela`) dipilih di sini.

    - G2: PF_R n/a (tanpa kerugian) lolos jika dan hanya jika G1 lolos.
    - G4: PF_R n/a dengan ≥ 1 transaksi menang dianggap tak hingga (lolos); tanpa transaksi → gagal.
    - G5: PF_R jendela n/a dengan ≥ 1 transaksi menang dihitung > 1; tanpa jendela memenuhi syarat → gagal.
    """
    s = strategi.upper()
    if s not in STRATEGI_GERBANG:
        raise ValueError(f"strategi {strategi!r} tidak dinilai gerbang (hanya {', '.join(STRATEGI_GERBANG)})")
    g = cfg.gerbang
    d = ke_decimal
    hasil = []

    n, a1 = holdout.n_tertutup, int(g.min_transaksi_holdout)
    g1 = n >= a1
    hasil.append(HasilSyarat("G1", n, a1, g1, f"G1 holdout: transaksi tertutup {n} {_tanda(g1, '≥', '<')} {a1}"))

    pf, a2 = holdout.pf_r, g.min_pf_r_holdout
    if pf.na:
        g2 = g1
        u2 = f"G2 holdout: PF_R {pf.teks()} — {'dianggap lolos karena G1 lolos' if g1 else 'tidak lolos karena G1 gagal'}"
    else:
        g2 = pf.nilai >= d(a2)
        u2 = f"G2 holdout: PF_R {pf.teks()} {_tanda(g2, '≥', '<')} {_a(a2)}"
    hasil.append(HasilSyarat("G2", pf, a2, g2, u2))

    e, a3 = holdout.ekspektansi_r, g.min_ekspektansi_r_holdout
    g3 = e is not None and e > d(a3)
    u3 = (
        "G3 holdout: ekspektansi R tidak tersedia (tanpa transaksi tertutup)"
        if e is None
        else f"G3 holdout: ekspektansi {_a(e, 4)}R {_tanda(g3, '>', '≤')} {_a(a3, 4)}R"
    )
    hasil.append(HasilSyarat("G3", e, a3, g3, u3))

    pf_is, a4 = in_sample.pf_r, g.min_pf_r_insample
    g4 = pf_is.minimal(a4)
    hasil.append(HasilSyarat("G4", pf_is, a4, g4, f"G4 in-sample: PF_R {pf_is.teks()} {_tanda(g4, '≥', '<')} {_a(a4)}"))

    min_tx, a5 = int(g.min_transaksi_per_jendela), g.min_pct_jendela_pf_di_atas_1
    syarat = [m for m in jendela if m.n_tertutup >= min_tx]
    if syarat:
        k = sum(1 for m in syarat if m.pf_r.lebih_dari(1))
        pct = Decimal(k * 100) / Decimal(len(syarat))
        g5 = pct >= d(a5)
        u5 = (
            f"G5 in-sample: {k} dari {len(syarat)} jendela (≥ {min_tx} transaksi) ber-PF_R > 1 = "
            f"{_a(pct)}% {_tanda(g5, '≥', '<')} {_a(a5)}%"
        )
    else:
        pct, g5 = None, False
        u5 = f"G5 in-sample: tidak ada jendela dengan ≥ {min_tx} transaksi tertutup (gagal)"
    hasil.append(HasilSyarat("G5", pct, a5, g5, u5))

    dd, a6 = penuh.max_drawdown_r, g.maks_drawdown_r
    g6 = dd <= d(a6)
    hasil.append(HasilSyarat("G6", dd, a6, g6, f"G6 penuh: max drawdown {_a(dd)}R {_tanda(g6, '≤', '>')} {_a(a6)}R"))
    return HasilGerbang(s, tuple(hasil))


def nilai_gerbang_dari_metrik(strategi: str, metrik: Sequence["MetrikPeriode"], cfg: "Config") -> HasilGerbang:
    """`nilai_gerbang` dari keluaran `jendela.metrik_semua_periode` (mode `edge`)."""
    from .jendela import HOLDOUT, IN_SAMPLE, JENDELA, PENUH

    per = {m.periode.jenis: m.transaksi for m in metrik if m.periode.jenis != JENDELA}
    return nilai_gerbang(
        strategi, holdout=per[HOLDOUT], in_sample=per[IN_SAMPLE], penuh=per[PENUH],
        jendela=[m.transaksi for m in metrik if m.periode.jenis == JENDELA], cfg=cfg,
    )


# ---------------------------------------------------------------------------
# Subset terjangkau (§16.7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubsetTerjangkau:
    n: int  # transaksi tertutup holdout yang terjangkau modal realistis
    n_holdout: int  # semua transaksi tertutup holdout edge
    pf_r: mt.NilaiPF
    ekspektansi_r: Decimal | None
    lemah: bool  # PF_R < min_pf_r_holdout atau n < min_transaksi_holdout
    transaksi: tuple[ex.Posisi, ...]


def terjangkau_realistis(pos: ex.Posisi, cfg: "Config") -> bool:
    """1 lot terjangkau menurut aturan mode `realistis` pada tanggal sinyal (§16.7): harga nominal
    (`meta["harga_maks_nominal"]`, `meta["stop_nominal"]`), anggaran risiko dan alokasi maks dari modal
    realistis yang sudah diresolusi, dengan status pasar hari itu (`meta["status_pasar"]`). Memakai sizing
    yang sama dengan live (§14.1, termasuk fee minimum dan `izinkan_satu_lot_melebihi_risiko`)."""
    try:
        harga_maks, stop, status = pos.meta["harga_maks_nominal"], pos.meta["stop_nominal"], pos.meta["status_pasar"]
    except KeyError as e:
        raise ValueError(f"{pos.kode} {pos.tanggal_sinyal}: meta rencana {e} tidak ada (posisi bukan dari mesin)") from None
    konteks = resolusi_konteks(cfg, "realistis")
    modal = Decimal(konteks.modal_idr)
    pe = parameter_entry(status, cfg)
    if not pe.boleh_entry:
        return False
    anggaran = modal * ke_decimal(cfg.risiko.risiko_per_transaksi_pct) / 100 * pe.pengali_risiko
    hs = hitung_sizing(
        ke_decimal(harga_maks), ke_decimal(stop), anggaran_risiko=anggaran, alokasi_maks=konteks.alokasi_maks(modal),
        kas_tersedia=modal, rata_nilai=Decimal(0), cfg=cfg,
    )
    return hs.lot_risiko >= 1 and hs.lot_alokasi >= 1


def subset_terjangkau(holdout_tertutup: Sequence[ex.Posisi], cfg: "Config") -> SubsetTerjangkau:
    """Subset transaksi holdout mode `edge` (tertutup) yang terjangkau modal realistis (§16.7)."""
    pilih = [p for p in holdout_tertutup if terjangkau_realistis(p, cfg)]
    for p in pilih:
        if p.keluar is None or p.keluar.alasan == ex.TERBUKA:
            raise ValueError(f"{p.kode} {p.tanggal_entry}: bukan transaksi tertutup")
    r = [p.keluar.r for p in pilih]
    pf = mt.profit_factor(r)
    eksp = sum(r, Decimal(0)) / len(r) if r else None
    g = cfg.gerbang
    lemah = len(pilih) < int(g.min_transaksi_holdout) or not pf.minimal(g.min_pf_r_holdout)
    return SubsetTerjangkau(len(pilih), len(holdout_tertutup), pf, eksp, lemah, tuple(pilih))


# ---------------------------------------------------------------------------
# gate_status.json & gerbang_run.json
# ---------------------------------------------------------------------------


def _angka_json(x: Any) -> Any:
    """Angka untuk JSON keluaran: Decimal/float dibulatkan 6 desimal (ROUND_HALF_UP), PF n/a → teks."""
    if x is None or isinstance(x, (bool, int, str)):
        return x
    if isinstance(x, mt.NilaiPF):
        return x.teks() if x.nilai is None else _angka_json(x.nilai)
    if isinstance(x, float):
        x = ke_decimal(x)
    if isinstance(x, Decimal):
        q = x.quantize(Decimal(1).scaleb(-DESIMAL_JSON), rounding=ROUND_HALF_UP)
        return int(q) if q == q.to_integral_value() else float(q)
    raise TypeError(f"tipe angka JSON tidak didukung: {type(x).__name__}")


def entri_gate(
    hasil: HasilGerbang,
    *,
    hash_aturan: str,
    hash_input: str,
    tanggal_data_terakhir: date,
    holdout_mulai: date,
    subset_terjangkau_lemah: bool,
    folder_run: str | Path,
    rincian: Mapping | None = None,
    dibuat_pada: datetime | None = None,
) -> dict:
    """Satu entri strategi `gate_status.json` (§16.6). `folder_run`: folder run asal (disarankan relatif
    terhadap --akar, mis. "output/backtest/20261015-200000_ab12cd34"). `rincian`: `IdentitasHash.rincian`
    (untuk menyebut bagian config/berkas yang berubah)."""
    saat = waktu.sekarang() if dibuat_pada is None else waktu.ke_wib(dibuat_pada)
    entri = {
        "lolos": hasil.lolos,
        "syarat": {
            s.kode: {"nilai": _angka_json(s.nilai), "ambang": _angka_json(s.ambang), "lolos": s.lolos, "uraian": s.uraian}
            for s in hasil.syarat
        },
        "hash_aturan": hash_aturan,
        "hash_input": hash_input,
        "tanggal_data_terakhir": tanggal_data_terakhir.isoformat(),
        "dibuat_pada": saat.isoformat(timespec="seconds"),
        "holdout_mulai": holdout_mulai.isoformat(),
        "subset_terjangkau_lemah": bool(subset_terjangkau_lemah),
        "folder_run": Path(folder_run).as_posix() if isinstance(folder_run, Path) else str(folder_run),
    }
    if rincian is not None:
        entri["rincian_hash"] = ke_kanonik(rincian)
    return entri


def konten_gate(entri: Mapping[str, Mapping]) -> dict:
    """Isi berkas gerbang: `{"catatan": disclaimer, "strategi": {KODE: entri}}`."""
    return {"catatan": DISCLAIMER, "strategi": {k.upper(): dict(v) for k, v in sorted(entri.items())}}


def tulis_gerbang_run(folder_run: Path, entri: Mapping[str, Mapping]) -> Path:
    """`<folder_run>/gerbang_run.json`: hasil gerbang run ini sebelum digabung (§16.1)."""
    path = Path(folder_run) / NAMA_GERBANG_RUN
    tulis_atomik(path, json_deterministik(konten_gate(entri)))
    return path


class GateStatusRusak(ValueError):
    """`gate_status.json` ada tetapi tidak terbaca/tidak sah."""


def baca_gate_status(path: Path) -> dict[str, dict] | None:
    """Entri per strategi dari `gate_status.json`; None bila berkas tidak ada; `GateStatusRusak` bila rusak."""
    path = Path(path)
    try:
        isi = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise GateStatusRusak(f"{path.name} tidak terbaca: {e}") from None
    if not isinstance(isi, dict) or not isinstance(isi.get("strategi"), dict):
        raise GateStatusRusak(f"{path.name} tidak sah: kunci 'strategi' tidak ada")
    return {str(k): v for k, v in isi["strategi"].items() if isinstance(v, dict)}


def gabung_gate_status(
    folder_bt: Path, entri: Mapping[str, Mapping], mode_dijalankan: Collection[str]
) -> Path | None:
    """Gabungkan `entri` (strategi → entri) ke `<folder_bt>/gate_status.json` (§16.6). Hanya ditulis bila
    `mode_dijalankan` memuat `edge`; strategi yang tidak ada di `entri` mempertahankan entri lamanya.
    Berkas lama yang rusak diganti (dengan peringatan log). Mengembalikan path, atau None bila tidak ditulis."""
    if MODE_GERBANG not in set(mode_dijalankan):
        return None
    for k in entri:
        if k.upper() not in STRATEGI_GERBANG:
            raise ValueError(f"strategi {k!r} tidak dinilai gerbang")
    path = Path(folder_bt) / NAMA_GATE_STATUS
    try:
        lama = baca_gate_status(path) or {}
    except GateStatusRusak as e:
        log.warning("%s; berkas ditulis ulang tanpa entri lama", e)
        lama = {}
    gabung = dict(lama)
    gabung.update({k.upper(): dict(v) for k, v in entri.items()})
    tulis_atomik(path, json_deterministik(konten_gate(gabung)))
    return path


# ---------------------------------------------------------------------------
# Run harian: apakah strategi dianggap lolos (§16.6)
# ---------------------------------------------------------------------------


def _berubah(lama: Mapping | None, kini: Mapping | None) -> list[str]:
    if not isinstance(lama, Mapping) or not isinstance(kini, Mapping):
        return []
    return sorted(k for k in set(lama) | set(kini) if lama.get(k) != kini.get(k))


def alasan_tidak_lolos(
    status: Mapping[str, Mapping] | None,
    strategi: str,
    *,
    hash_aturan: str,
    hash_input: str,
    t: date,
    kedaluwarsa_hari: int,
    rincian: Mapping | None = None,
) -> str | None:
    """Alasan run harian menganggap strategi TIDAK lolos (→ `BAYANGAN_GERBANG`), atau None bila lolos.

    `status`: hasil `baca_gate_status` (None = berkas tidak ada). `hash_aturan` = `hash_aturan[X]` dan
    `hash_input` kondisi saat ini; `rincian` = `IdentitasHash.rincian` saat ini (untuk menyebut bagian config
    atau berkas input yang berubah). Urutan cek §16.6: entri tidak ada → hash berbeda → kedaluwarsa →
    `lolos: false`."""
    s = strategi.upper()
    if status is None:
        return f"gate_status.json tidak ada — {PESAN_JALANKAN}"
    e = status.get(s)
    if not isinstance(e, Mapping):
        return f"strategi {s} belum ada di gate_status.json — {PESAN_JALANKAN}"
    lama_rinci = e.get("rincian_hash") if isinstance(e.get("rincian_hash"), Mapping) else {}
    kini_rinci = rincian or {}
    bagian = []
    if e.get("hash_aturan") != hash_aturan:
        berubah = _berubah(lama_rinci.get("aturan"), kini_rinci.get("aturan"))
        bagian.append(
            "bagian config berubah sejak backtest: " + ", ".join(berubah)
            if berubah
            else "aturan (config atau versi program) berubah sejak backtest"
        )
    if e.get("hash_input") != hash_input:
        berubah = _berubah(lama_rinci.get("input"), kini_rinci.get("input"))
        bagian.append(
            "berkas input berubah sejak backtest: " + ", ".join(berubah) if berubah else "berkas input berubah sejak backtest"
        )
    if bagian:
        return f"strategi {s}: " + "; ".join(bagian) + " — jalankan ulang backtest.bat"
    try:
        tgl = date.fromisoformat(str(e.get("tanggal_data_terakhir")))
    except ValueError:
        return f"strategi {s}: tanggal_data_terakhir di gate_status.json tidak sah — {PESAN_JALANKAN}"
    umur = (t - tgl).days
    if umur > int(kedaluwarsa_hari):
        return (
            f"strategi {s}: hasil gerbang kedaluwarsa (data backtest terakhir {tgl.isoformat()}, {umur} hari "
            f"kalender sebelum {t.isoformat()} > {int(kedaluwarsa_hari)} hari) — jalankan ulang backtest.bat"
        )
    if e.get("lolos") is not True:
        syarat = e.get("syarat") if isinstance(e.get("syarat"), Mapping) else {}
        gagal = [
            str(v.get("uraian") or k) for k, v in sorted(syarat.items()) if isinstance(v, Mapping) and not v.get("lolos")
        ]
        rinci = ("; ".join(gagal)) if gagal else "lolos: false"
        return f"strategi {s} tidak lolos gerbang backtest: {rinci}"
    return None


def path_gate_status(ctx: "KonteksApp") -> Path:
    return ctx.path(FOLDER_BACKTEST) / NAMA_GATE_STATUS


def alasan_tidak_lolos_ctx(ctx: "KonteksApp", strategi: str, t: date) -> str | None:
    """`alasan_tidak_lolos` dengan hash kondisi saat ini dan `output/backtest/gate_status.json` dari `ctx`."""
    try:
        status = baca_gate_status(path_gate_status(ctx))
    except GateStatusRusak as e:
        return f"{e} — {PESAN_JALANKAN}"
    ident = identitas_hash(ctx, strategi)
    return alasan_tidak_lolos(
        status, strategi, hash_aturan=ident.hash_aturan, hash_input=ident.hash_input, t=t,
        kedaluwarsa_hari=int(ctx.cfg.gerbang.kedaluwarsa_hari), rincian=ident.rincian,
    )


# ---------------------------------------------------------------------------
# holdout_log.csv (§16.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BarisHoldoutLog:
    waktu: str  # ISO WIB
    holdout_mulai: str
    hash_aturan: str  # global
    hash_input: str
    strategi: str  # nama run (enum run_backtest, mis. "pb", "gabungan_aktif")
    mode: str
    pf_r_holdout: str  # 4 desimal; "n/a" = tanpa kerugian; kosong = tanpa transaksi
    n_holdout: int

    def sebagai_daftar(self) -> list[str]:
        return [self.waktu, self.holdout_mulai, self.hash_aturan, self.hash_input, self.strategi, self.mode,
                self.pf_r_holdout, str(self.n_holdout)]


def teks_pf_csv(pf: mt.NilaiPF) -> str:
    if pf.nilai is not None:
        return format(pf.nilai.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), "f")
    return mt.TEKS_NA if pf.n_menang > 0 else ""


def baris_holdout_log(
    *,
    holdout_mulai: date,
    hash_aturan: str,
    hash_input: str,
    strategi: str,
    mode: str,
    holdout: mt.MetrikTransaksi,
    saat: datetime | None = None,
) -> BarisHoldoutLog:
    """Satu baris log untuk satu run × mode yang menghitung holdout. `hash_aturan` = global. Semua baris
    dari satu perintah backtest wajib memakai `saat` yang sama (mis. waktu mulai run, yang juga menamai
    folder run), karena N di `baca_holdout_log` = banyaknya nilai `waktu` berbeda."""
    saat = waktu.sekarang() if saat is None else waktu.ke_wib(saat)
    return BarisHoldoutLog(
        saat.isoformat(timespec="seconds"), holdout_mulai.isoformat(), hash_aturan, hash_input, strategi, mode,
        teks_pf_csv(holdout.pf_r), holdout.n_tertutup,
    )


def _baris_csv(nilai: Sequence[str]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow(nilai)
    return buf.getvalue()


def tambah_holdout_log(folder_bt: Path, baris: Iterable[BarisHoldoutLog]) -> Path:
    """Tambahkan baris ke `<folder_bt>/holdout_log.csv`. Berkas baru (atau kosong) diawali baris disclaimer
    dan header. Penulisan atomik (isi lama + baris baru)."""
    path = Path(folder_bt) / NAMA_HOLDOUT_LOG
    try:
        lama = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        lama = ""
    if not lama.strip():
        lama = DISCLAIMER_CSV + "\n" + _baris_csv(KOLOM_HOLDOUT_LOG)
    if not lama.endswith("\n"):
        lama += "\n"
    baru = "".join(_baris_csv(b.sebagai_daftar()) for b in baris)
    tulis_atomik(path, lama + baru)
    return path


@dataclass(frozen=True)
class RingkasanHoldoutLog:
    baris: tuple[dict[str, str], ...]
    n_dilihat: int  # N: banyaknya run backtest (nilai `waktu` berbeda) yang menghitung holdout
    m_konfigurasi: int  # M: banyaknya hash_aturan berbeda
    holdout_mulai_pertama: str | None

    @property
    def n_baris(self) -> int:
        return len(self.baris)

    @property
    def murni(self) -> bool:
        return self.m_konfigurasi <= 1

    def teks(self) -> str:
        """"Holdout telah dilihat N kali dengan M konfigurasi aturan berbeda." (+ peringatan bila M > 1)."""
        t = f"Holdout telah dilihat {self.n_dilihat} kali dengan {self.m_konfigurasi} konfigurasi aturan berbeda."
        if not self.murni:
            t += " PERINGATAN: holdout tidak lagi murni karena sudah dilihat dengan lebih dari satu konfigurasi aturan."
        return t


def baca_holdout_log(path: Path) -> RingkasanHoldoutLog:
    """Baca `holdout_log.csv` (baris berawalan "#" dilewati). Berkas tidak ada → ringkasan kosong."""
    path = Path(path)
    try:
        teks_berkas = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return RingkasanHoldoutLog((), 0, 0, None)
    baris = [b for b in teks_berkas.splitlines() if b.strip() and not b.startswith("#")]
    rekaman = list(csv.DictReader(baris))
    waktu_unik = {r.get("waktu", "") for r in rekaman}
    hash_unik = {r.get("hash_aturan", "") for r in rekaman}
    pertama = rekaman[0].get("holdout_mulai") if rekaman else None
    return RingkasanHoldoutLog(tuple(rekaman), len(waktu_unik), len(hash_unik), pertama)

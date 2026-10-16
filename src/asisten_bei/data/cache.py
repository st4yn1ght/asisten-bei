"""Cache harga per provider (SPEC §8.3): parquet + meta JSON, pembaruan inkremental, deteksi revisi,
penulisan atomik.

Lokasi: `<folder_cache>/<provider>/<KODE>.parquet` + `<KODE>.meta.json`.
- parquet: index `tanggal` (datetime64[ns], tz-naive, = tanggal WIB, urut naik, unik); kolom open, high,
  low, close, volume (float64, sudah disesuaikan split §8.4), sumber (str), split (float64; 0.0 = tidak
  ada split pada bar itu).
- meta: diambil_pada (ISO WIB), bar_pertama, bar_terakhir (YYYY-MM-DD|null), jumlah_bar (int),
  versi_provider (str), split_terakhir (YYYY-MM-DD|null), tidak_ada_data_sejak (YYYY-MM-DD|null).

Cache menyimpan bar MENTAH dari provider (bar rusak / volume 0 disimpan apa adanya); validasi per bar
(§8.5) dilakukan saat data dipakai. Tidak ada bar yang dibuat atau diisi (P1). Bar hari ini tidak pernah
disimpan sebelum batas bar final (§8.6), sehingga cache tidak pernah memasok bar `t` yang belum final.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import Config
from ..kalender import Kalender, tanggal_final_terakhir
from ..operasional import tulis_atomik
from ..waktu import ke_wib
from .kualitas import validasi_struktur
from .provider import KOLOM_BAR, NAMA_INDEX, HasilAmbil, JenisErrorAmbil, StatusAmbil, ke_ts
from .split import normalisasi_split

KOLOM_CACHE: tuple[str, ...] = (*KOLOM_BAR, "sumber", "split")
KUNCI_META: tuple[str, ...] = (
    "bar_pertama",
    "bar_terakhir",
    "diambil_pada",
    "jumlah_bar",
    "split_terakhir",
    "tidak_ada_data_sejak",
    "versi_provider",
)
_KUNCI_TANGGAL_META = ("bar_pertama", "bar_terakhir", "split_terakhir", "tidak_ada_data_sejak")
_POLA_TANGGAL = re.compile(r"\d{4}-\d{2}-\d{2}")

CATATAN_DISESUAIKAN_ULANG = "riwayat disesuaikan ulang"


# ---------------------------------------------------------------------------
# Baca/tulis berkas cache
# ---------------------------------------------------------------------------


@dataclass
class IsiCache:
    bar: pd.DataFrame | None  # kolom KOLOM_CACHE; None bila tidak ada / rusak
    meta: dict | None
    rusak: bool  # parquet/meta tidak terbaca → diperlakukan kosong (diunduh ulang penuh)
    pesan: str = ""  # alasan rusak, atau catatan ketidakcocokan meta (tidak rusak)

    @property
    def kosong(self) -> bool:
        return self.bar is None or len(self.bar) == 0


def _tanggal_atau_none(nilai, kunci: str) -> date | None:
    """Nilai tanggal meta: None atau teks YYYY-MM-DD yang valid; selain itu ValueError."""
    if nilai is None:
        return None
    if not isinstance(nilai, str) or not _POLA_TANGGAL.fullmatch(nilai):
        raise ValueError(f"meta {kunci} bukan tanggal YYYY-MM-DD")
    return date.fromisoformat(nilai)


def _periksa_meta(meta) -> dict:
    """Meta harus dict dengan tepat KUNCI_META dan tipe yang benar; selain itu ValueError (= rusak)."""
    if not isinstance(meta, dict):
        raise ValueError("meta bukan objek JSON")
    hilang = [k for k in KUNCI_META if k not in meta]
    if hilang:
        raise ValueError(f"kunci meta hilang: {', '.join(hilang)}")
    asing = sorted(k for k in meta if k not in KUNCI_META)
    if asing:
        raise ValueError(f"kunci meta tidak dikenal: {', '.join(asing)}")
    for k in _KUNCI_TANGGAL_META:
        _tanggal_atau_none(meta[k], k)
    jumlah = meta["jumlah_bar"]
    if isinstance(jumlah, bool) or not isinstance(jumlah, int) or jumlah < 0:
        raise ValueError("meta jumlah_bar bukan bilangan bulat ≥ 0")
    if not isinstance(meta["diambil_pada"], str) or not isinstance(meta["versi_provider"], str):
        raise ValueError("meta diambil_pada/versi_provider bukan teks")
    datetime.fromisoformat(meta["diambil_pada"])
    return meta


def teks_meta(meta: dict) -> str:
    return json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def buat_meta(
    bar: pd.DataFrame | None,
    *,
    sekarang: datetime,
    versi_provider: str,
    tidak_ada_data_sejak: date | None = None,
) -> dict:
    """Meta untuk DataFrame cache `bar` (boleh None/kosong)."""
    ada = bar is not None and len(bar) > 0
    split_terakhir = None
    if ada and "split" in bar.columns:
        rasio = bar["split"].to_numpy(dtype="float64")
        bertanda = bar.index[(rasio != 0.0) & ~np.isnan(rasio)]
        if len(bertanda):
            split_terakhir = bertanda.max().date().isoformat()
    return {
        "bar_pertama": bar.index[0].date().isoformat() if ada else None,
        "bar_terakhir": bar.index[-1].date().isoformat() if ada else None,
        "diambil_pada": ke_wib(sekarang).isoformat(timespec="seconds"),
        "jumlah_bar": int(len(bar)) if ada else 0,
        "split_terakhir": split_terakhir,
        "tidak_ada_data_sejak": tidak_ada_data_sejak.isoformat() if tidak_ada_data_sejak else None,
        "versi_provider": versi_provider,
    }


def _rapikan_bingkai(df: pd.DataFrame) -> pd.DataFrame:
    """Skema cache standar: urutan kolom KOLOM_CACHE, float64, index datetime64[ns] bernama tanggal.
    Melempar ValueError bila skema/urutan tidak sah (tidak ada perbaikan diam-diam)."""
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("index bukan tanggal")
    if df.index.tz is not None:
        raise ValueError("index ber-zona waktu")
    hilang = [k for k in KOLOM_CACHE if k not in df.columns]
    if hilang:
        raise ValueError(f"kolom hilang: {', '.join(hilang)}")
    if len(df) and not (df.index == df.index.normalize()).all():
        raise ValueError("index memuat jam")
    if not df.index.is_unique:
        raise ValueError("tanggal duplikat")
    if not df.index.is_monotonic_increasing:
        raise ValueError("tanggal tidak urut")
    hasil = df.loc[:, list(KOLOM_CACHE)].copy()
    for k in (*KOLOM_BAR, "split"):
        if not pd.api.types.is_numeric_dtype(hasil[k]):
            raise ValueError(f"tipe kolom {k} bukan angka")
        hasil[k] = hasil[k].astype("float64")
    hasil["sumber"] = hasil["sumber"].astype("str")
    # Tanpa `freq`: hari bursa tidak beraturan, dan bingkai di memori harus identik dengan hasil baca.
    hasil.index = pd.DatetimeIndex(np.asarray(hasil.index, dtype="datetime64[ns]"), name=NAMA_INDEX)
    return hasil


def _bytes_parquet(df: pd.DataFrame) -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq

    tabel = pa.Table.from_pandas(df, preserve_index=True)
    buf = pa.BufferOutputStream()
    pq.write_table(tabel, buf, compression="zstd")
    return buf.getvalue().to_pybytes()


def _baca_parquet(path: Path) -> pd.DataFrame:
    import pyarrow.parquet as pq

    df = pq.read_table(path).to_pandas()
    if not isinstance(df.index, pd.DatetimeIndex) and NAMA_INDEX in df.columns:
        df = df.set_index(NAMA_INDEX)  # parquet tanpa metadata pandas: index tersimpan sebagai kolom
    return _rapikan_bingkai(df)


class CacheProvider:
    """Cache satu provider: `folder` = `<folder_cache>/<nama_provider>`; `nama` = nama folder."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.nama = self.folder.name

    def path_parquet(self, kode: str) -> Path:
        return self.folder / f"{kode}.parquet"

    def path_meta(self, kode: str) -> Path:
        return self.folder / f"{kode}.meta.json"

    def baca(self, kode: str) -> IsiCache:
        """Isi cache. Parquet/meta tak terbaca → `rusak=True`, `bar=None` (diperlakukan kosong).

        Hanya meta tanpa parquet sah bila `jumlah_bar` = 0 (mis. simbol TIDAK_ADA_DATA yang belum pernah
        punya data). Ketidakcocokan meta vs parquet (mis. proses terhenti di antara dua penulisan atomik)
        tidak dianggap rusak: parquet adalah sumber kebenaran, ketidakcocokan dicatat di `pesan`.
        """
        p, m = self.path_parquet(kode), self.path_meta(kode)
        ada_p, ada_m = p.exists(), m.exists()
        if not ada_p and not ada_m:
            return IsiCache(None, None, False, "")
        if not ada_m:
            return IsiCache(None, None, True, f"meta {m.name} tidak ada")
        try:
            meta = _periksa_meta(json.loads(m.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            return IsiCache(None, None, True, f"meta {m.name} tidak terbaca: {e}")
        if not ada_p:
            if meta["jumlah_bar"] > 0:
                return IsiCache(None, None, True, f"berkas {p.name} hilang (meta mencatat {meta['jumlah_bar']} bar)")
            return IsiCache(None, meta, False, "")
        try:
            bar = _baca_parquet(p)
        except Exception as e:  # berkas sampah/terpotong/skema salah: semuanya = tidak terbaca
            return IsiCache(None, None, True, f"parquet {p.name} tidak terbaca: {type(e).__name__}: {e}")
        pesan = ""
        terakhir = bar.index[-1].date().isoformat() if len(bar) else None
        if meta["jumlah_bar"] != len(bar) or meta["bar_terakhir"] != terakhir:
            pesan = (
                f"meta {m.name} tidak cocok dengan parquet (meta: {meta['jumlah_bar']} bar s/d "
                f"{meta['bar_terakhir']}; parquet: {len(bar)} bar s/d {terakhir}); data parquet dipakai"
            )
        return IsiCache(bar, meta, False, pesan)

    def tulis(self, kode: str, bar: pd.DataFrame, meta: dict) -> None:
        """Tulis parquet lalu meta, masing-masing atomik (berkas sementara di folder yang sama +
        `os.replace`, lewat `operasional.tulis_atomik`). Gagal di tengah → berkas lama tetap utuh."""
        df = _rapikan_bingkai(bar)
        _periksa_meta(meta)
        isi = _bytes_parquet(df)
        tulis_atomik(self.path_parquet(kode), isi)
        tulis_atomik(self.path_meta(kode), teks_meta(meta))

    def tulis_meta(self, kode: str, meta: dict) -> None:
        """Hanya meta (dipakai untuk mencatat TIDAK_ADA_DATA); atomik."""
        _periksa_meta(meta)
        tulis_atomik(self.path_meta(kode), teks_meta(meta))

    def daftar_kode(self) -> list[str]:
        """Kode yang punya berkas cache (parquet dan/atau meta), urut."""
        if not self.folder.is_dir():
            return []
        kode = set()
        for p in self.folder.iterdir():
            if p.name.startswith(".") or not p.is_file():
                continue
            if p.name.endswith(".meta.json"):
                kode.add(p.name[: -len(".meta.json")])
            elif p.suffix == ".parquet":
                kode.add(p.stem)
        return sorted(kode)


# ---------------------------------------------------------------------------
# Pembaruan cache (§8.3)
# ---------------------------------------------------------------------------


@dataclass
class HasilPembaruan:
    kode: str
    status: StatusAmbil
    jenis_error: JenisErrorAmbil | None
    pesan: str
    isi: IsiCache  # isi cache SETELAH pembaruan (atau isi lama bila gagal)
    unduh_penuh: bool
    dilewati: bool  # tidak mengunduh (TIDAK_ADA_DATA belum waktunya dicoba lagi)
    catatan: list[str] = field(default_factory=list)
    revisi_volume: list[tuple[date, float, float]] = field(default_factory=list)  # (tanggal, lama, baru)


@dataclass
class _Unduhan:
    bar: pd.DataFrame  # open..volume, hanya bar final dalam [mulai, hari ini]
    split: pd.Series  # normalisasi_split
    versi: str


def _tgl(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


def _angka(x: float) -> str:
    return "kosong" if pd.isna(x) else f"{x:g}"


def _petakan_split(
    split: pd.Series, index: pd.DatetimeIndex, batas_bawah: pd.Timestamp
) -> tuple[np.ndarray, list[str]]:
    """Kolom `split` untuk `index`: rasio split s dicatat pada bar pertama ≥ s (biasanya bar s itu sendiri).

    Hasilnya identik untuk F(d) di setiap tanggal bar. Split < `batas_bawah` diabaikan (sudah tercakup
    bagian cache lama / sebelum data). Split sesudah bar terakhir belum punya bar final → tidak disimpan
    (tercatat lagi pada pembaruan berikutnya, lalu memicu unduh ulang penuh sebagai split baru).
    """
    kolom = np.zeros(len(index), dtype="float64")
    catatan: list[str] = []
    for ts, r in split.items():
        if ts < batas_bawah:
            continue
        posisi = int(index.searchsorted(ts, side="left"))
        if posisi >= len(index):
            catatan.append(f"split {_tgl(ts)} (rasio {r:g}) belum punya bar final: belum disimpan")
            continue
        if index[posisi] != ts:
            catatan.append(
                f"split {_tgl(ts)} (rasio {r:g}) tanpa bar pada tanggalnya: dicatat pada bar {_tgl(index[posisi])}"
            )
        kolom[posisi] = r if kolom[posisi] == 0.0 else kolom[posisi] * r
    return kolom, catatan


def _melebihi(lama: np.ndarray, baru: np.ndarray, toleransi_pct: float) -> np.ndarray:
    """|baru/lama − 1| × 100 > toleransi, dihitung tanpa pembagian (|baru − lama| × 100 > tol × |lama|)
    agar selisih tepat sebesar toleransi tidak lolos karena galat pembulatan float.
    Keduanya kosong (NaN) → bukan revisi; satu kosong → revisi; lama = 0 → revisi bila baru ≠ 0."""
    with np.errstate(invalid="ignore"):
        lebih = np.abs(baru - lama) * 100.0 > toleransi_pct * np.abs(lama)
    satu_kosong = np.isnan(lama) ^ np.isnan(baru)
    return np.where(satu_kosong, True, np.nan_to_num(lebih, nan=False).astype(bool))


def perbarui_cache(
    ambil: Callable[[str, date, date], HasilAmbil],
    cache: CacheProvider,
    kode: str,
    *,
    cfg: Config,
    kal: Kalender,
    sekarang: datetime,
    penuh: bool = False,
    adalah_indeks: bool = False,
    versi_provider: str = "",
) -> HasilPembaruan:
    """Perbarui cache satu simbol dari `ambil(kode, mulai, akhir)` (akhir inklusif) — §8.3.

    1. Cache rusak → dianggap kosong, unduh penuh.
    2. Meta `tidak_ada_data_sejak` belum lewat `coba_lagi_tidak_ada_data_hari` hari → dilewati.
    3. Kosong/`penuh` → mulai = `data.mulai_unduh`; selain itu mulai = bar ke-`bar_cek_ulang` dari belakang
       (tepat `bar_cek_ulang` bar tumpang tindih). akhir = hari ini (WIB).
    4. GAGAL → cache tidak diubah (TIDAK_ADA_DATA: meta `tidak_ada_data_sejak` diisi bila belum).
    5. Struktur tidak lolos `validasi_struktur` → GAGAL FORMAT, cache tidak diubah.
    6. Bar hari ini dibuang bila `sekarang` < batas bar final (§8.6); bar bertanggal sesudah hari ini selalu.
    7. Tumpang tindih: revisi close > `toleransi_revisi_pct` atau split baru → unduh ulang penuh
       ("riwayat disesuaikan ulang"); revisi volume > `toleransi_revisi_volume_pct` → `revisi_volume`;
       bar cache yang hilang dari unduhan → dihapus. Bar tumpang tindih selalu ditimpa nilai baru.
    8. Gabung cache[< mulai] + unduhan, tulis atomik bersama meta baru.

    `kal` tidak dipakai dalam aturan saat ini (antarmuka desain; batas bar final hanya bergantung config).
    Kegagalan menulis berkas (OSError) diteruskan ke pemanggil; berkas lama tetap utuh.
    """
    dcfg = cfg.data
    now = ke_wib(sekarang)
    hari = now.date()
    catatan: list[str] = []
    revisi_volume: list[tuple[date, float, float]] = []

    isi = cache.baca(kode)
    if isi.rusak:
        catatan.append(f"cache {kode} rusak ({isi.pesan}): diperlakukan kosong, diunduh ulang penuh")
        lama, meta_lama = None, None
    else:
        lama, meta_lama = isi.bar, isi.meta
        if isi.pesan:
            catatan.append(isi.pesan)

    # 2. TIDAK_ADA_DATA: jangan dicoba lagi sebelum N hari.
    sejak = _tanggal_atau_none(meta_lama.get("tidak_ada_data_sejak"), "tidak_ada_data_sejak") if meta_lama else None
    n_coba = dcfg.coba_lagi_tidak_ada_data_hari
    if sejak is not None and (hari - sejak).days < n_coba:
        coba = sejak + timedelta(days=n_coba)
        return HasilPembaruan(
            kode, "GAGAL", "TIDAK_ADA_DATA",
            f"tidak ada data sejak {sejak.isoformat()}; dicoba lagi paling cepat {coba.isoformat()}",
            isi, False, True, catatan, revisi_volume,
        )

    # 3. Rentang unduhan.
    kosong = lama is None or len(lama) == 0
    unduh_penuh = bool(penuh or kosong)
    if unduh_penuh:
        mulai = dcfg.mulai_unduh
        if penuh and not kosong:
            catatan.append("unduh ulang penuh atas permintaan")
    else:
        n = dcfg.bar_cek_ulang
        mulai = lama.index[-n if len(lama) >= n else 0].date()

    # Bar bertanggal ≥ batas_ts belum final (§8.3): tanggal final terakhir + 1 hari.
    batas_ts = ke_ts(tanggal_final_terakhir(cfg, now)) + pd.Timedelta(days=1)

    def gagal_ambil(jenis: JenisErrorAmbil, pesan: str) -> HasilPembaruan:
        isi_ret = isi
        if jenis == "TIDAK_ADA_DATA":
            meta = dict(meta_lama) if meta_lama else buat_meta(
                lama, sekarang=now, versi_provider=versi_provider
            )
            if not meta.get("tidak_ada_data_sejak"):
                meta["tidak_ada_data_sejak"] = hari.isoformat()
                cache.tulis_meta(kode, meta)
                coba = hari + timedelta(days=n_coba)
                catatan.append(f"tidak ada data: dicoba lagi paling cepat {coba.isoformat()}")
            isi_ret = IsiCache(lama, meta, isi.rusak, isi.pesan) if not isi.rusak else isi
        return HasilPembaruan(kode, "GAGAL", jenis, pesan, isi_ret, unduh_penuh, False, catatan, revisi_volume)

    def unduh(dari: date) -> _Unduhan | HasilPembaruan:
        hasil = ambil(kode, dari, hari)
        if hasil.status == "GAGAL":
            return gagal_ambil(hasil.jenis_error or "FORMAT", hasil.pesan)
        st = validasi_struktur(hasil.bar, adalah_indeks=adalah_indeks)
        if not st.ok:
            return gagal_ambil("FORMAT", f"struktur data tidak valid: {st.pesan}")
        catatan.extend(hasil.catatan)
        catatan.extend(st.catatan)
        bar = st.bar.loc[:, list(KOLOM_BAR)]
        bar = bar[bar.index >= ke_ts(dari)]  # defensif: provider yang mengabaikan `mulai`
        belum_final = bar.index[bar.index >= batas_ts]
        for ts in belum_final:
            if ts.date() <= hari:  # termasuk kemarin bila batas bar final lewat tengah malam
                catatan.append(f"bar {_tgl(ts)} belum final (sebelum batas bar final): tidak disimpan")
            else:
                catatan.append(f"bar {_tgl(ts)} bertanggal sesudah hari ini: dibuang")
        bar = bar[bar.index < batas_ts]
        return _Unduhan(bar, normalisasi_split(hasil.split), hasil.versi_provider)

    u = unduh(mulai)
    if isinstance(u, HasilPembaruan):
        return u
    if len(u.bar) == 0:
        catatan.append("tidak ada bar final dalam unduhan: cache tidak diubah")
        return HasilPembaruan(kode, "OK", None, "", isi, unduh_penuh, False, catatan, revisi_volume)

    mulai_ts = ke_ts(mulai)
    if unduh_penuh:
        batas_bawah = u.bar.index[0]
    else:
        batas_bawah = mulai_ts
    kolom_split, catatan_split = _petakan_split(u.split, u.bar.index, batas_bawah)

    # 7. Bandingkan tumpang tindih (hanya inkremental).
    if not unduh_penuh:
        tt = lama[lama.index >= mulai_ts]
        bersama = tt.index.intersection(u.bar.index)
        alasan_penuh: list[str] = []

        cl = tt.loc[bersama, "close"].to_numpy(dtype="float64")
        cb = u.bar.loc[bersama, "close"].to_numpy(dtype="float64")
        tol = float(dcfg.toleransi_revisi_pct)
        for i in np.flatnonzero(_melebihi(cl, cb, tol)):
            alasan_penuh.append(
                f"revisi close {_tgl(bersama[i])}: {_angka(cl[i])} → {_angka(cb[i])} (melebihi {tol:g}%)"
            )

        vl = tt.loc[bersama, "volume"].to_numpy(dtype="float64")
        vb = u.bar.loc[bersama, "volume"].to_numpy(dtype="float64")
        tol_v = float(dcfg.toleransi_revisi_volume_pct)
        for i in np.flatnonzero(_melebihi(vl, vb, tol_v)):
            revisi_volume.append((bersama[i].date(), float(vl[i]), float(vb[i])))
            catatan.append(
                f"revisi volume {_tgl(bersama[i])}: {_angka(vl[i])} → {_angka(vb[i])} (melebihi {tol_v:g}%)"
            )

        split_lama = {ts: float(r) for ts, r in tt["split"].items() if r != 0.0 and not pd.isna(r)}
        split_baru = {ts: float(r) for ts, r in zip(u.bar.index, kolom_split, strict=True) if r != 0.0}
        beda_split = set(split_lama) != set(split_baru) or any(
            not np.isclose(split_lama[k], split_baru[k], rtol=1e-9, atol=0.0) for k in split_lama
        )
        if beda_split:
            uraian = ", ".join(f"{_tgl(k)}={v:g}" for k, v in sorted(split_baru.items())) or "tidak ada"
            alasan_penuh.append(f"split baru/berubah dalam unduhan ({uraian})")

        for ts in tt.index.difference(u.bar.index):
            catatan.append(f"bar {_tgl(ts)} dihapus (hilang dari unduhan)")

        if alasan_penuh:
            catatan.extend(alasan_penuh)
            catatan.append(CATATAN_DISESUAIKAN_ULANG)
            unduh_penuh = True
            u = unduh(dcfg.mulai_unduh)
            if isinstance(u, HasilPembaruan):
                u.catatan.append("unduh ulang penuh gagal: cache tidak diubah")
                return u
            if len(u.bar) == 0:
                catatan.append("unduh ulang penuh tanpa bar final: cache tidak diubah")
                return HasilPembaruan(kode, "OK", None, "", isi, unduh_penuh, False, catatan, revisi_volume)
            kolom_split, catatan_split = _petakan_split(u.split, u.bar.index, u.bar.index[0])

    catatan.extend(catatan_split)

    # 8. Gabung & tulis.
    baru = u.bar.copy()
    baru["sumber"] = cache.nama
    baru["split"] = kolom_split
    if unduh_penuh:
        gabungan = baru
    else:
        bagian_lama = lama[lama.index < mulai_ts]
        gabungan = pd.concat([bagian_lama, baru]) if len(bagian_lama) else baru
    gabungan = _rapikan_bingkai(gabungan)
    meta = buat_meta(gabungan, sekarang=now, versi_provider=versi_provider or u.versi)
    cache.tulis(kode, gabungan, meta)
    return HasilPembaruan(
        kode, "OK", None, "", IsiCache(gabungan, meta, False, ""), unduh_penuh, False, catatan, revisi_volume
    )

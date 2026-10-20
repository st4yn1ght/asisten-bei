"""Memuat & memvalidasi berkas `input/` (SPEC §6, §11, §15.3).

Aturan umum CSV (§6): dibaca `utf-8-sig` (BOM Excel diterima), pemisah koma, header di baris
pertama, angka dengan titik desimal tanpa pemisah ribuan, tanggal hanya `YYYY-MM-DD`, kode saham
huruf besar tanpa `.JK`. Format **tidak pernah ditebak**: pemisah `;`, koma desimal, atau format
tanggal lain adalah ERROR dengan pesan khusus `PESAN_REGIONAL`.

Setiap masalah menunjuk berkas dan nomor baris (header = baris 1). ERROR → input tidak valid
(kode keluar 3, §20.1); PERINGATAN tidak menghentikan run.
"""

from __future__ import annotations

import codecs
import csv
import io
import math
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml

if TYPE_CHECKING:  # hanya untuk anotasi; hindari impor saat modul dimuat
    from .config import KonteksApp
    from .kalender import Kalender

# Pesan khusus SPEC §6 (persis).
PESAN_REGIONAL = (
    "Berkas tampaknya disimpan Excel dengan format regional Indonesia. "
    "Simpan ulang sebagai CSV UTF-8 (koma) atau edit dengan Notepad."
)

LevelMasalah = Literal["ERROR", "PERINGATAN"]
ERROR: LevelMasalah = "ERROR"
PERINGATAN: LevelMasalah = "PERINGATAN"

KODE_IHSG = "IHSG"
STRATEGI_PORTOFOLIO: tuple[str, ...] = ("PB", "BO", "MR", "MANUAL")  # enum §4.3 untuk portfolio.csv
AWALAN_SEKTOR_TIDAK_DIKETAHUI = "TIDAK_DIKETAHUI"

# Skema kolom per berkas: (wajib, opsional). Kolom opsional yang tidak ada di header dianggap kosong.
KOLOM_UNIVERSE = (["kode", "nama", "sektor"], [])
KOLOM_BREADTH = (["kode"], [])
KOLOM_RIWAYAT = (["kode", "tanggal_masuk"], ["tanggal_keluar"])
KOLOM_EXCLUDE = (["kode", "alasan"], ["tanggal_mulai", "tanggal_akhir"])
KOLOM_PORTOFOLIO = (
    ["kode", "tanggal_beli", "harga_beli", "lot", "strategi", "stop_awal"],
    ["sektor", "harga_per_tanggal", "lembar", "catatan"],
)
KOLOM_LIBUR = (["tanggal"], ["keterangan"])
KOLOM_ANOMALI_OK = (["kode", "tanggal"], ["catatan"])
KUNCI_AKUN = ("kas_idr", "diperbarui")


def sektor_tidak_diketahui(kode: str) -> str:
    """Sektor khusus §6.3: satu sektor tersendiri per kode."""
    return f"{AWALAN_SEKTOR_TIDAK_DIKETAHUI}:{kode}"


# ---------------------------------------------------------------------------
# Masalah & exception
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Masalah:
    level: LevelMasalah
    berkas: str  # path relatif terhadap akar bila memungkinkan
    baris: int | None  # nomor baris di berkas (header = 1); None bila tidak relevan
    pesan: str

    def __str__(self) -> str:
        lokasi = self.berkas if self.baris is None else f"{self.berkas} baris {self.baris}"
        return f"{self.level} {lokasi}: {self.pesan}"


def _error(berkas: str, baris: int | None, pesan: str) -> Masalah:
    return Masalah(ERROR, berkas, baris, pesan)


def _peringatan(berkas: str, baris: int | None, pesan: str) -> Masalah:
    return Masalah(PERINGATAN, berkas, baris, pesan)


class InputTidakValid(Exception):
    """Ada ERROR pada berkas input. `masalah` berisi daftar ERROR (kode keluar 3)."""

    KODE_KELUAR = 3

    def __init__(self, masalah: Iterable[Masalah]):
        self.masalah = list(masalah)
        super().__init__("Input tidak valid:\n" + "\n".join(f"  - {m}" for m in self.masalah))


# ---------------------------------------------------------------------------
# Baris data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SahamUniverse:
    kode: str
    nama: str
    sektor: str


@dataclass(frozen=True)
class RiwayatKonstituen:
    kode: str
    tanggal_masuk: date
    tanggal_keluar: date | None  # None = masih konstituen

    def aktif_pada(self, d: date) -> bool:
        """Konstituen pada `d`: masuk ≤ d ≤ keluar (kedua batas inklusif, keluar kosong = terbuka)."""
        return self.tanggal_masuk <= d and (self.tanggal_keluar is None or d <= self.tanggal_keluar)


@dataclass(frozen=True)
class BarisExclude:
    kode: str
    alasan: str
    tanggal_mulai: date | None  # None = tanpa batas bawah
    tanggal_akhir: date | None  # None = tanpa batas atas

    def aktif_pada(self, d: date) -> bool:
        """Kedua batas inklusif; tanggal kosong berarti tanpa batas (§6.4)."""
        if self.tanggal_mulai is not None and d < self.tanggal_mulai:
            return False
        if self.tanggal_akhir is not None and d > self.tanggal_akhir:
            return False
        return True


@dataclass(frozen=True)
class BarisPortofolio:
    """Satu baris portfolio.csv. Harga, stop, lot/lembar bernilai nominal per `tanggal_acuan()` (§15.3)."""

    kode: str
    tanggal_beli: date
    harga_beli: Decimal
    lot: int
    strategi: str  # PB / BO / MR / MANUAL
    stop_awal: Decimal
    sektor: str | None
    harga_per_tanggal: date | None
    lembar: int | None
    catatan: str
    baris: int

    def lembar_nominal(self, lot_lembar: int) -> int:
        """`lembar` jika diisi; selain itu `lot × lot_lembar` (§6.5)."""
        if self.lembar is not None:
            return self.lembar
        return self.lot * int(lot_lembar)

    def tanggal_acuan(self) -> date:
        """Tanggal acuan nilai nominal: `harga_per_tanggal`, atau `tanggal_beli` bila kosong."""
        return self.harga_per_tanggal if self.harga_per_tanggal is not None else self.tanggal_beli


@dataclass(frozen=True)
class Akun:
    kas_idr: Decimal
    diperbarui: date | None
    dari_berkas: bool  # False = akun.yaml tidak ada, kas = akun.modal_awal_idr


@dataclass(frozen=True)
class AnomaliOk:
    kode: str
    tanggal: date
    catatan: str


@dataclass
class Masukan:
    """Semua input yang sudah dibaca, beserta masalahnya.

    Indeks pencarian (sektor, riwayat, exclude) dibangun malas dan dibangun ulang bila daftar
    sumbernya diganti; anggap daftar tidak diubah di tempat setelah dipakai.
    """

    universe: list[SahamUniverse]
    breadth: list[str]
    riwayat: list[RiwayatKonstituen] | None  # None jika universe_history.csv tidak ada
    exclude: list[BarisExclude]
    portofolio: list[BarisPortofolio]
    akun: Akun
    libur: dict[date, str] = field(default_factory=dict)
    anomali_ok: list[AnomaliOk] = field(default_factory=list)
    masalah: list[Masalah] = field(default_factory=list)
    _cache: dict[str, tuple[Any, int, Any]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def _indeks(self, nama: str, sumber: Any, bangun: Callable[[Any], Any]) -> Any:
        simpan = self._cache.get(nama)
        if simpan is None or simpan[0] is not sumber or simpan[1] != len(sumber):
            simpan = (sumber, len(sumber), bangun(sumber))
            self._cache[nama] = simpan
        return simpan[2]

    # -- ringkasan masalah ----------------------------------------------------------

    def ada_error(self) -> bool:
        return any(m.level == ERROR for m in self.masalah)

    def daftar_error(self) -> list[Masalah]:
        return [m for m in self.masalah if m.level == ERROR]

    def daftar_peringatan(self) -> list[Masalah]:
        return [m for m in self.masalah if m.level == PERINGATAN]

    # -- universe & sektor ----------------------------------------------------------

    def kode_universe(self) -> list[str]:
        return [s.kode for s in self.universe]

    def sektor(self, kode: str) -> str:
        """Sektor dari universe.csv → kolom sektor portfolio.csv → `TIDAK_DIKETAHUI:<KODE>` (§6.3, §6.5)."""
        peta = self._indeks("sektor_universe", self.universe, lambda xs: {s.kode: s.sektor for s in xs})
        if kode in peta:
            return peta[kode]
        for p in self.portofolio:
            if p.kode == kode and p.sektor:
                return p.sektor
        return sektor_tidak_diketahui(kode)

    def konstituen_pada(self, kode: str, d: date) -> bool:
        """Konstituen pada `d` menurut universe_history.csv (kedua ujung inklusif, keluar kosong =
        terbuka; satu kode boleh keluar-masuk). Tanpa berkas riwayat: anggota universe.csv saat ini."""
        if self.riwayat is None:
            anggota = self._indeks("set_universe", self.universe, lambda xs: {s.kode for s in xs})
            return kode in anggota
        peta = self._indeks("riwayat", self.riwayat, _kelompokkan_per_kode)
        return any(r.aktif_pada(d) for r in peta.get(kode, ()))

    def dikecualikan_pada(self, kode: str, d: date) -> BarisExclude | None:
        """Baris exclude.csv pertama yang aktif untuk `kode` pada `d`, atau None (§11 syarat 2)."""
        peta = self._indeks("exclude", self.exclude, _kelompokkan_per_kode)
        for baris in peta.get(kode, ()):
            if baris.aktif_pada(d):
                return baris
        return None

    def anomali_ok_set(self) -> set[tuple[str, date]]:
        return {(a.kode, a.tanggal) for a in self.anomali_ok}


def _kelompokkan_per_kode(baris: Iterable[Any]) -> dict[str, list[Any]]:
    hasil: dict[str, list[Any]] = {}
    for b in baris:
        hasil.setdefault(b.kode, []).append(b)
    return hasil


# ---------------------------------------------------------------------------
# Pengurai nilai (ketat; tidak pernah menebak)
# ---------------------------------------------------------------------------

_POLA_TANGGAL_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_POLA_DESIMAL = re.compile(r"^-?\d+(\.\d+)?$")
_POLA_INT = re.compile(r"^-?\d+$")
_POLA_RIBUAN_TITIK = re.compile(r"^-?\d{1,3}(\.\d{3}){2,}$")  # 1.000.000
_POLA_RIBUAN_SATU = re.compile(r"^-?\d{1,3}\.\d{3}$")  # 2.850: sah sebagai desimal, tetapi mungkin ribuan


def saran_titik_ribuan(kolom: str, mentah: str) -> str | None:
    """PERINGATAN §6 untuk angka seperti `2.850` (dibaca 2,85), atau None."""
    mentah = str(mentah).strip()
    if not _POLA_RIBUAN_SATU.match(mentah):
        return None
    tanpa_titik = mentah.replace(".", "")
    return (f"{kolom} {mentah} dibaca sebagai {mentah.replace('.', ',')}; bila maksud Anda {mentah} rupiah "
            f"(pemisah ribuan), tulis {tanpa_titik} tanpa titik")
_POLA_KODE = re.compile(r"^[A-Z0-9]{1,12}$")


def parse_tanggal(s: str) -> date:
    """Hanya `YYYY-MM-DD` dan tanggal yang ada di kalender. Format lain (08/10/2026, 08-10-2026,
    8 Okt 2026, ...) → ValueError berisi PESAN_REGIONAL; tidak pernah ditebak."""
    s = s.strip()
    if not s:
        raise ValueError("tanggal kosong")
    if _POLA_TANGGAL_ISO.match(s):
        try:
            return date.fromisoformat(s)
        except ValueError:
            raise ValueError(f"tanggal {s!r} tidak ada di kalender (bulan/hari di luar rentang)") from None
    if any(c.isdigit() for c in s):
        raise ValueError(
            f"format tanggal {s!r} tidak dikenali; hanya YYYY-MM-DD yang diterima (mis. 2026-01-31). "
            f"{PESAN_REGIONAL}"
        )
    raise ValueError(f"{s!r} bukan tanggal; gunakan format YYYY-MM-DD (mis. 2026-01-31)")


def parse_desimal(s: str) -> Decimal:
    """Angka bertitik desimal tanpa pemisah ribuan (`^-?\\d+(\\.\\d+)?$`). Koma desimal → ValueError
    berisi PESAN_REGIONAL; titik ribuan (1.000.000) → ValueError."""
    s = s.strip()
    if not s:
        raise ValueError("angka kosong")
    if _POLA_DESIMAL.match(s):
        return Decimal(s)
    if "," in s:
        raise ValueError(
            f"angka {s!r} memakai koma (koma desimal atau pemisah ribuan); tulis dengan titik desimal "
            f"tanpa pemisah ribuan (mis. 2850 atau 2850.5). {PESAN_REGIONAL}"
        )
    if _POLA_RIBUAN_TITIK.match(s):
        raise ValueError(
            f"angka {s!r} memakai titik sebagai pemisah ribuan; tulis tanpa pemisah (mis. 1000000). "
            f"{PESAN_REGIONAL}"
        )
    raise ValueError(f"{s!r} bukan angka; gunakan titik desimal tanpa pemisah ribuan (mis. 2850 atau 2850.5)")


def parse_int(s: str) -> int:
    """Bilangan bulat (`^-?\\d+$`). Koma → ValueError berisi PESAN_REGIONAL."""
    s = s.strip()
    if not s:
        raise ValueError("angka kosong")
    if _POLA_INT.match(s):
        return int(s)
    if "," in s:
        raise ValueError(f"{s!r} harus bilangan bulat tanpa koma. {PESAN_REGIONAL}")
    raise ValueError(f"{s!r} harus bilangan bulat (tanpa titik desimal atau pemisah ribuan)")


def parse_kode(s: str) -> str:
    """Kode saham huruf besar tanpa akhiran (`^[A-Z0-9]{1,12}$`). Tidak pernah diubah otomatis."""
    s = s.strip()
    if not s:
        raise ValueError("kode kosong")
    if s.upper().endswith(".JK"):
        raise ValueError(f"tulis kode tanpa akhiran .JK (mis. {s[:-3].upper()!r}, bukan {s!r})")
    if s != s.upper():
        raise ValueError(f"kode harus huruf besar (mis. {s.upper()!r}, bukan {s!r})")
    if not _POLA_KODE.match(s):
        raise ValueError(f"kode {s!r} tidak valid: hanya huruf besar A–Z dan angka, maksimal 12 karakter")
    return s


def _kode_saham(s: str) -> str:
    """Kode saham untuk universe/breadth/riwayat/exclude/portfolio: IHSG (indeks) ditolak."""
    kode = parse_kode(s)
    if kode == KODE_IHSG:
        raise ValueError("IHSG adalah kode indeks, bukan saham, dan tidak boleh dipakai di berkas ini")
    return kode


def _teks(s: str) -> str:
    return s


def _rupiah(x: Decimal) -> str:
    """Tampilan Rupiah gaya Indonesia (§4.1): bulat tanpa desimal, selain itu 2 desimal ROUND_HALF_UP."""
    x = Decimal(x)
    if x == x.to_integral_value():
        return "Rp " + f"{int(x):,}".replace(",", ".")
    q = x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    bulat, _, pecahan = f"{q:,.2f}".partition(".")
    return "Rp " + bulat.replace(",", ".") + "," + pecahan


# ---------------------------------------------------------------------------
# Pembaca CSV tingkat rendah (dipakai juga oleh data/csv_provider.py)
# ---------------------------------------------------------------------------

_SARAN_UTF8 = "Simpan ulang sebagai CSV UTF-8 (koma) atau edit dengan Notepad."


def _nama(path: Path | str, nama_berkas: str | None) -> str:
    return nama_berkas if nama_berkas is not None else Path(path).as_posix()


def _nama_relatif(path: Path, akar: Path) -> str:
    """Path relatif terhadap akar bila berada di bawahnya; selain itu path apa adanya."""
    try:
        return Path(path).resolve().relative_to(Path(akar).resolve()).as_posix()
    except ValueError:
        return Path(path).as_posix()


def _pesan_jumlah_kolom(n: int, n_header: int, rekaman: Sequence[str]) -> str:
    dasar = f"baris berisi {n} kolom, sedangkan header {n_header} kolom"
    if n > n_header or any(";" in v for v in rekaman):
        return (
            f"{dasar}; kemungkinan ada koma desimal atau pemisah ';'. {PESAN_REGIONAL} "
            "Teks yang memuat koma harus diapit tanda kutip ganda."
        )
    return f"{dasar}; kolom kosong tetap harus ditulis dengan koma (mis. 'ABCD,2026-01-31,,')"


def baca_csv_ketat(
    path: Path | str,
    kolom_wajib: Sequence[str],
    kolom_opsional: Sequence[str] = (),
    nama_berkas: str | None = None,
) -> tuple[list[dict[str, Any]], list[Masalah]]:
    """Baca CSV input secara ketat (§6).

    - `utf-8-sig` (BOM diterima); byte non-UTF-8 → ERROR.
    - Header wajib di baris 1, pemisah koma. `;` di header → ERROR PESAN_REGIONAL.
    - Kolom wajib hilang, kolom tak dikenal, kolom ganda/tanpa nama → ERROR (tidak ada baris dikembalikan).
      Kolom opsional yang tidak ada di header diisi "" di setiap baris.
    - Jumlah kolom tiap baris harus sama dengan header; lebih banyak → kemungkinan koma desimal →
      ERROR PESAN_REGIONAL. Baris itu dilewati, baris lain tetap dibaca.
    - Baris kosong (atau semua nilai kosong) dilewati; nilai di-strip.
    Setiap dict punya kunci tambahan `_baris` (int, nomor baris berkas; header = 1).
    """
    path = Path(path)
    nama = _nama(path, nama_berkas)
    try:
        mentah = path.read_bytes()
    except FileNotFoundError:
        return [], [_error(nama, None, "berkas tidak ditemukan")]
    except OSError as e:
        return [], [_error(nama, None, f"berkas tidak dapat dibaca (mungkin sedang dibuka program lain): {e}")]

    if mentah.startswith(codecs.BOM_UTF8):
        mentah = mentah[len(codecs.BOM_UTF8):]
    try:
        teks = mentah.decode("utf-8")
    except UnicodeDecodeError as e:
        baris_rusak = mentah[: e.start].count(b"\n") + 1
        masalah = [_error(nama, baris_rusak, f"berkas bukan teks UTF-8. {_SARAN_UTF8}")]
        if b";" in mentah.split(b"\n", 1)[0]:
            masalah.append(_error(nama, 1, f"pemisah ';' terdeteksi di header. {PESAN_REGIONAL}"))
        return [], masalah

    wajib, opsional = list(kolom_wajib), list(kolom_opsional)
    contoh_header = ",".join(wajib + opsional)
    baris_pertama = teks.split("\n", 1)[0].rstrip("\r")
    if not teks.strip():
        return [], [_error(nama, None, f"berkas kosong: header tidak ada (baris pertama harus: {contoh_header})")]
    if not baris_pertama.strip():
        return [], [_error(nama, 1, f"baris pertama harus header ({contoh_header}), bukan baris kosong")]
    if ";" in baris_pertama:
        return [], [_error(nama, 1, f"pemisah ';' terdeteksi di header. {PESAN_REGIONAL}")]
    if "\t" in baris_pertama:
        return [], [_error(nama, 1, f"pemisah tab terdeteksi di header; gunakan koma. {_SARAN_UTF8}")]

    pembaca = csv.reader(io.StringIO(teks, newline=""), strict=True, skipinitialspace=True)
    masalah: list[Masalah] = []
    try:
        header = [h.strip() for h in next(pembaca)]
    except csv.Error as e:
        return [], [_error(nama, 1, f"header CSV rusak: {e}")]

    salah_header: list[str] = []
    if any(h == "" for h in header):
        posisi = ", ".join(str(i + 1) for i, h in enumerate(header) if h == "")
        salah_header.append(f"header memuat kolom tanpa nama (kolom ke-{posisi})")
    ganda = sorted({h for h in header if h and header.count(h) > 1})
    if ganda:
        salah_header.append(f"kolom ganda di header: {', '.join(ganda)}")
    hilang = [k for k in wajib if k not in header]
    if hilang:
        salah_header.append(f"kolom wajib tidak ada: {', '.join(hilang)}")
    dikenal = set(wajib) | set(opsional)
    asing = [h for h in header if h and h not in dikenal]
    if asing:
        salah_header.append(f"kolom tidak dikenal: {', '.join(asing)}")
    if salah_header:
        return [], [
            _error(nama, 1, f"{p} (header yang benar: {contoh_header})") for p in salah_header
        ]

    hasil: list[dict[str, Any]] = []
    n_kolom = len(header)
    baris_terakhir = pembaca.line_num
    try:
        for rekaman in pembaca:
            nomor = baris_terakhir + 1  # baris awal rekaman (rekaman berkutip bisa > 1 baris)
            baris_terakhir = pembaca.line_num
            nilai = [v.strip() for v in rekaman]
            if not any(nilai):
                continue
            if len(nilai) != n_kolom:
                masalah.append(_error(nama, nomor, _pesan_jumlah_kolom(len(nilai), n_kolom, rekaman)))
                continue
            data: dict[str, Any] = dict(zip(header, nilai))
            for k in opsional:
                data.setdefault(k, "")
            data["_baris"] = nomor
            hasil.append(data)
    except csv.Error as e:
        masalah.append(_error(nama, pembaca.line_num, f"format CSV rusak (tanda kutip tidak seimbang?): {e}"))
    return hasil, masalah


class _Baris:
    """Pembantu parsing satu baris CSV: kumpulkan semua ERROR per kolom, jangan berhenti di yang pertama."""

    def __init__(self, data: dict[str, Any], nama: str, masalah: list[Masalah]):
        self.data = data
        self.nama = nama
        self.masalah = masalah
        self.baris: int = data["_baris"]
        self.ok = True

    def error(self, pesan: str) -> None:
        self.masalah.append(_error(self.nama, self.baris, pesan))
        self.ok = False

    def peringatan(self, pesan: str) -> None:
        self.masalah.append(_peringatan(self.nama, self.baris, pesan))

    def ambil(self, kolom: str, fungsi: Callable[[str], Any], wajib: bool = True) -> Any:
        nilai = self.data.get(kolom, "")
        if nilai == "":
            if wajib:
                self.error(f"kolom {kolom} wajib diisi")
            return None
        try:
            return fungsi(nilai)
        except ValueError as e:
            self.error(f"kolom {kolom}: {e}")
            return None


def _kode_ganda(
    b: _Baris, kode: str, pertama: dict[str, int], sebagai_error: bool, akibat: str = ""
) -> bool:
    """True bila `kode` sudah muncul; mencatat ERROR/PERINGATAN. Selain itu daftarkan kode."""
    if kode in pertama:
        pesan = f"kode {kode} ganda (sudah ada di baris {pertama[kode]}){akibat}"
        if sebagai_error:
            b.error(pesan)
        else:
            b.peringatan(pesan)
        return True
    pertama[kode] = b.baris
    return False


# ---------------------------------------------------------------------------
# Pembaca per berkas
# ---------------------------------------------------------------------------


def baca_universe(path: Path | str, nama_berkas: str | None = None) -> tuple[list[SahamUniverse], list[Masalah]]:
    """universe.csv (§6.1): wajib ada; kode ganda = ERROR; sektor wajib diisi."""
    nama = _nama(path, nama_berkas)
    data_baris, masalah = baca_csv_ketat(path, *KOLOM_UNIVERSE, nama_berkas=nama)
    hasil: list[SahamUniverse] = []
    pertama: dict[str, int] = {}
    for data in data_baris:
        b = _Baris(data, nama, masalah)
        kode = b.ambil("kode", _kode_saham)
        sektor = b.ambil("sektor", _teks)
        if kode is not None and _kode_ganda(b, kode, pertama, sebagai_error=True):
            continue
        if not b.ok:
            continue
        hasil.append(SahamUniverse(kode=kode, nama=data["nama"], sektor=sektor))
    if not hasil and not any(m.level == ERROR for m in masalah):
        masalah.append(_peringatan(nama, None, "tidak berisi saham: tidak ada saham yang bisa direkomendasikan"))
    return hasil, masalah


def baca_breadth(path: Path | str, nama_berkas: str | None = None) -> tuple[list[str], list[Masalah]]:
    """breadth_universe.csv (§6.2): wajib ada; kode ganda = PERINGATAN (dipakai sekali)."""
    nama = _nama(path, nama_berkas)
    data_baris, masalah = baca_csv_ketat(path, *KOLOM_BREADTH, nama_berkas=nama)
    hasil: list[str] = []
    pertama: dict[str, int] = {}
    for data in data_baris:
        b = _Baris(data, nama, masalah)
        kode = b.ambil("kode", _kode_saham)
        if not b.ok or _kode_ganda(b, kode, pertama, sebagai_error=False, akibat="; dipakai sekali"):
            continue
        hasil.append(kode)
    if not hasil and not any(m.level == ERROR for m in masalah):
        masalah.append(
            _peringatan(nama, None, "tidak berisi saham: breadth tidak bisa dihitung (kondisi pasar K0)")
        )
    return hasil, masalah


def baca_riwayat(
    path: Path | str, nama_berkas: str | None = None
) -> tuple[list[RiwayatKonstituen] | None, list[Masalah]]:
    """universe_history.csv (§6.3), opsional: tidak ada → (None, []). tanggal_masuk > tanggal_keluar
    = ERROR. Satu kode boleh punya beberapa baris (keluar lalu masuk lagi)."""
    path = Path(path)
    nama = _nama(path, nama_berkas)
    if not path.exists():
        return None, []
    data_baris, masalah = baca_csv_ketat(path, *KOLOM_RIWAYAT, nama_berkas=nama)
    hasil: list[RiwayatKonstituen] = []
    for data in data_baris:
        b = _Baris(data, nama, masalah)
        kode = b.ambil("kode", _kode_saham)
        masuk = b.ambil("tanggal_masuk", parse_tanggal)
        keluar = b.ambil("tanggal_keluar", parse_tanggal, wajib=False)
        if b.ok and keluar is not None and masuk > keluar:
            b.error(f"tanggal_masuk {masuk.isoformat()} setelah tanggal_keluar {keluar.isoformat()}")
        if b.ok:
            hasil.append(RiwayatKonstituen(kode=kode, tanggal_masuk=masuk, tanggal_keluar=keluar))
    # Rentang tumpang tindih untuk kode yang sama tidak salah secara logika, tetapi mencurigakan.
    for kode, rentang in _kelompokkan_per_kode(hasil).items():
        urut = sorted(rentang, key=lambda r: r.tanggal_masuk)
        for a, b2 in zip(urut, urut[1:]):
            if a.tanggal_keluar is None or b2.tanggal_masuk <= a.tanggal_keluar:
                masalah.append(
                    _peringatan(
                        nama, None,
                        f"kode {kode}: rentang konstituen tumpang tindih (masuk {a.tanggal_masuk.isoformat()} "
                        f"dan {b2.tanggal_masuk.isoformat()})",
                    )
                )
    if not hasil and not any(m.level == ERROR for m in masalah):
        masalah.append(
            _peringatan(nama, None, "berkas ada tetapi kosong: backtest tidak punya konstituen sama sekali")
        )
    return hasil, masalah


def baca_exclude(path: Path | str, nama_berkas: str | None = None) -> tuple[list[BarisExclude], list[Masalah]]:
    """exclude.csv (§6.4): berkas wajib (§5; boleh hanya header). Tanggal kosong = tanpa batas;
    tanggal_mulai > tanggal_akhir = ERROR. Satu kode boleh punya beberapa baris."""
    nama = _nama(path, nama_berkas)
    data_baris, masalah = baca_csv_ketat(path, *KOLOM_EXCLUDE, nama_berkas=nama)
    hasil: list[BarisExclude] = []
    for data in data_baris:
        b = _Baris(data, nama, masalah)
        kode = b.ambil("kode", _kode_saham)
        mulai = b.ambil("tanggal_mulai", parse_tanggal, wajib=False)
        akhir = b.ambil("tanggal_akhir", parse_tanggal, wajib=False)
        if b.ok and mulai is not None and akhir is not None and mulai > akhir:
            b.error(f"tanggal_mulai {mulai.isoformat()} setelah tanggal_akhir {akhir.isoformat()}")
        if b.ok:
            hasil.append(BarisExclude(kode=kode, alasan=data["alasan"], tanggal_mulai=mulai, tanggal_akhir=akhir))
    return hasil, masalah


def _strategi(s: str) -> str:
    if s in STRATEGI_PORTOFOLIO:
        return s
    if s.upper() in STRATEGI_PORTOFOLIO:
        raise ValueError(f"tulis dengan huruf besar: {s.upper()!r}")
    if s.upper() == "ACAK":
        raise ValueError("ACAK khusus backtest; di portfolio.csv gunakan PB, BO, MR, atau MANUAL")
    raise ValueError(f"{s!r} tidak dikenal; gunakan PB, BO, MR, atau MANUAL")


def _positif(fungsi: Callable[[str], Any], nama: str) -> Callable[[str], Any]:
    def _cek(s: str) -> Any:
        x = fungsi(s)
        if x <= 0:
            raise ValueError(f"{nama} harus > 0 (nilai: {s})")
        return x

    return _cek


def baca_portofolio(
    path: Path | str, kode_universe: set[str], nama_berkas: str | None = None
) -> tuple[list[BarisPortofolio], list[Masalah]]:
    """portfolio.csv (§6.5, §15.3). Berkas wajib (§5; boleh hanya header).

    - kode ganda = ERROR (satu kode = satu baris); lot bilangan bulat ≥ 1; strategi PB/BO/MR/MANUAL;
      harga_beli > 0; stop_awal > 0.
    - stop_awal ≥ harga_beli → PERINGATAN (bukan ERROR; saran posisinya JUAL, §15.4 #3).
    - sektor kosong dan kode di luar universe → ERROR.
    - lembar opsional (bilangan bulat ≥ 1); harga_per_tanggal opsional, tidak boleh sebelum tanggal_beli.
    Pemeriksaan terhadap kalender & `t` ada di `cek_portofolio_kalender`.
    """
    nama = _nama(path, nama_berkas)
    data_baris, masalah = baca_csv_ketat(path, *KOLOM_PORTOFOLIO, nama_berkas=nama)
    hasil: list[BarisPortofolio] = []
    pertama: dict[str, int] = {}
    for data in data_baris:
        b = _Baris(data, nama, masalah)
        kode = b.ambil("kode", _kode_saham)
        tanggal_beli = b.ambil("tanggal_beli", parse_tanggal)
        harga_beli = b.ambil("harga_beli", _positif(parse_desimal, "harga_beli"))
        lot = b.ambil("lot", _positif(parse_int, "lot"))
        strategi = b.ambil("strategi", _strategi)
        stop_awal = b.ambil("stop_awal", _positif(parse_desimal, "stop_awal"))
        sektor = b.ambil("sektor", _teks, wajib=False)
        harga_per_tanggal = b.ambil("harga_per_tanggal", parse_tanggal, wajib=False)
        lembar = b.ambil("lembar", _positif(parse_int, "lembar"), wajib=False)
        if kode is not None and kode not in kode_universe and not sektor:
            b.error(f"kode {kode} tidak ada di universe.csv: kolom sektor wajib diisi")
        if (
            tanggal_beli is not None
            and harga_per_tanggal is not None
            and harga_per_tanggal < tanggal_beli
        ):
            b.error(
                f"harga_per_tanggal {harga_per_tanggal.isoformat()} sebelum tanggal_beli "
                f"{tanggal_beli.isoformat()}"
            )
        if kode is not None and _kode_ganda(
            b, kode, pertama, sebagai_error=True,
            akibat="; satu kode = satu baris, gabungkan menjadi harga rata-rata",
        ):
            continue
        if not b.ok:
            continue
        for kolom in ("harga_beli", "stop_awal"):
            saran = saran_titik_ribuan(kolom, data[kolom])
            if saran:
                b.peringatan(saran)
        if stop_awal >= harga_beli:
            b.peringatan(
                f"stop_awal {stop_awal} ≥ harga_beli {harga_beli}: posisi sudah berada di bawah stop "
                "sejak dibeli (saran: JUAL)"
            )
        hasil.append(
            BarisPortofolio(
                kode=kode, tanggal_beli=tanggal_beli, harga_beli=harga_beli, lot=lot, strategi=strategi,
                stop_awal=stop_awal, sektor=sektor, harga_per_tanggal=harga_per_tanggal, lembar=lembar,
                catatan=data["catatan"], baris=b.baris,
            )
        )
    return hasil, masalah


# -- akun.yaml -------------------------------------------------------------------


class _PemuatYamlKetat(yaml.SafeLoader):
    """SafeLoader yang menolak kunci ganda (PyYAML diam-diam memakai yang terakhir)."""


def _pemetaan_tanpa_ganda(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> Any:
    terlihat: set[Any] = set()
    for kunci_node, _ in node.value:
        kunci = loader.construct_object(kunci_node, deep=deep)
        if kunci in terlihat:
            raise yaml.constructor.ConstructorError(
                None, None, f"kunci ganda: {kunci}", kunci_node.start_mark
            )
        terlihat.add(kunci)
    return loader.construct_mapping(node, deep=deep)


_PemuatYamlKetat.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _pemetaan_tanpa_ganda)


def _baris_kunci_yaml(teks: str) -> dict[Any, int]:
    """Nomor baris (1-based) setiap kunci tingkat atas, untuk pesan error."""
    try:
        node = yaml.compose(teks, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return {}
    if not isinstance(node, yaml.MappingNode):
        return {}
    return {k.value: k.start_mark.line + 1 for k, _ in node.value}


def _kas_dari_yaml(v: Any) -> Decimal:
    if v is None:
        raise ValueError("wajib diisi")
    if isinstance(v, bool):
        raise ValueError(f"harus angka (nilai: {v!r})")
    if isinstance(v, int):
        return Decimal(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ValueError(f"harus angka terhingga (nilai: {v!r})")
        return Decimal(str(v))
    if isinstance(v, str):
        return parse_desimal(v)
    raise ValueError(f"harus angka (nilai: {v!r})")


def _tanggal_dari_yaml(v: Any) -> date:
    if v is None:
        raise ValueError("wajib diisi (YYYY-MM-DD)")
    if isinstance(v, datetime):
        raise ValueError(f"harus tanggal saja YYYY-MM-DD, tanpa jam (nilai: {v})")
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        return parse_tanggal(v)
    raise ValueError(f"harus tanggal YYYY-MM-DD (nilai: {v!r})")


def baca_akun(
    path: Path | str, modal_awal_idr: Decimal, nama_berkas: str | None = None
) -> tuple[Akun, list[Masalah]]:
    """akun.yaml (§6.6): `{kas_idr: angka, diperbarui: YYYY-MM-DD}`.

    Kunci tak dikenal/hilang/ganda = ERROR; kas boleh 0, tidak boleh negatif. Angka dibaca sebagai
    `Decimal(str(v))`. Berkas tidak ada → kas = `akun.modal_awal_idr` + PERINGATAN mencolok.
    Bila ada ERROR, akun yang dikembalikan berisi kas 0 (arah aman; run dihentikan oleh ERROR).
    """
    path = Path(path)
    nama = _nama(path, nama_berkas)
    modal = Decimal(str(modal_awal_idr))
    if not path.exists():
        return Akun(kas_idr=modal, diperbarui=None, dari_berkas=False), [
            _peringatan(
                nama, None,
                f"berkas tidak ditemukan — kas dianggap akun.modal_awal_idr ({_rupiah(modal)}). "
                "Buat berkas ini dan perbarui setiap ada transaksi atau setoran.",
            )
        ]
    gagal = Akun(kas_idr=Decimal(0), diperbarui=None, dari_berkas=True)
    try:
        mentah = path.read_bytes()
    except OSError as e:
        return gagal, [_error(nama, None, f"berkas tidak dapat dibaca: {e}")]
    try:
        teks = mentah.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        return gagal, [_error(nama, mentah[: e.start].count(b"\n") + 1, "berkas bukan teks UTF-8; simpan dengan Notepad")]
    try:
        data = yaml.load(teks, Loader=_PemuatYamlKetat)
    except yaml.YAMLError as e:
        tanda = getattr(e, "problem_mark", None)
        baris = tanda.line + 1 if tanda is not None else None
        masalah_yaml = getattr(e, "problem", None) or str(e)
        return gagal, [_error(nama, baris, f"YAML tidak valid: {masalah_yaml}")]
    if not isinstance(data, dict):
        return gagal, [_error(nama, None, "isi harus berupa pasangan 'kas_idr: <angka>' dan 'diperbarui: YYYY-MM-DD'")]

    baris_kunci = _baris_kunci_yaml(teks)
    masalah: list[Masalah] = []
    for k in data:
        if k not in KUNCI_AKUN:
            masalah.append(_error(nama, baris_kunci.get(k), f"kunci tidak dikenal: {k} (yang diizinkan: kas_idr, diperbarui)"))
    for k in KUNCI_AKUN:
        if k not in data:
            masalah.append(_error(nama, None, f"kunci wajib tidak ada: {k}"))

    kas: Decimal | None = None
    diperbarui: date | None = None
    if "kas_idr" in data:
        mentah = re.search(r"^kas_idr\s*:\s*([^#\s]+)", teks, flags=re.MULTILINE)
        saran = saran_titik_ribuan("kas_idr", mentah.group(1)) if mentah else None
        if saran:
            masalah.append(_peringatan(nama, baris_kunci.get("kas_idr"), saran))
        try:
            kas = _kas_dari_yaml(data["kas_idr"])
            if kas < 0:
                masalah.append(_error(nama, baris_kunci.get("kas_idr"), f"kas_idr tidak boleh negatif (nilai: {kas})"))
        except ValueError as e:
            masalah.append(_error(nama, baris_kunci.get("kas_idr"), f"kas_idr {e}"))
    if "diperbarui" in data:
        try:
            diperbarui = _tanggal_dari_yaml(data["diperbarui"])
        except ValueError as e:
            masalah.append(_error(nama, baris_kunci.get("diperbarui"), f"diperbarui {e}"))
    if any(m.level == ERROR for m in masalah):
        return gagal, masalah
    return Akun(kas_idr=kas, diperbarui=diperbarui, dari_berkas=True), masalah


def baca_libur(path: Path | str, nama_berkas: str | None = None) -> tuple[dict[date, str], list[Masalah]]:
    """holidays.csv (§6.7): berkas wajib (§5). Tanggal ganda = PERINGATAN (dipakai sekali);
    tanggal akhir pekan = PERINGATAN (tidak berpengaruh)."""
    nama = _nama(path, nama_berkas)
    data_baris, masalah = baca_csv_ketat(path, *KOLOM_LIBUR, nama_berkas=nama)
    libur: dict[date, str] = {}
    pertama: dict[date, int] = {}
    for data in data_baris:
        b = _Baris(data, nama, masalah)
        tgl = b.ambil("tanggal", parse_tanggal)
        if not b.ok:
            continue
        if tgl in pertama:
            b.peringatan(f"tanggal {tgl.isoformat()} ganda (sudah ada di baris {pertama[tgl]}); dipakai sekali")
            continue
        pertama[tgl] = b.baris
        if tgl.weekday() >= 5:
            b.peringatan(f"tanggal {tgl.isoformat()} jatuh pada akhir pekan; tidak berpengaruh pada kalender")
        libur[tgl] = data["keterangan"]
    return libur, masalah


def baca_anomali_ok(path: Path | str, nama_berkas: str | None = None) -> tuple[list[AnomaliOk], list[Masalah]]:
    """anomali_ok.csv (§6.8), opsional: tidak ada → ([], []). Kode `IHSG` diizinkan.
    Pasangan (kode, tanggal) ganda = PERINGATAN."""
    path = Path(path)
    nama = _nama(path, nama_berkas)
    if not path.exists():
        return [], []
    data_baris, masalah = baca_csv_ketat(path, *KOLOM_ANOMALI_OK, nama_berkas=nama)
    hasil: list[AnomaliOk] = []
    pertama: dict[tuple[str, date], int] = {}
    for data in data_baris:
        b = _Baris(data, nama, masalah)
        kode = b.ambil("kode", parse_kode)
        tgl = b.ambil("tanggal", parse_tanggal)
        if not b.ok:
            continue
        if (kode, tgl) in pertama:
            b.peringatan(f"{kode} {tgl.isoformat()} ganda (sudah ada di baris {pertama[(kode, tgl)]})")
            continue
        pertama[(kode, tgl)] = b.baris
        hasil.append(AnomaliOk(kode=kode, tanggal=tgl, catatan=data["catatan"]))
    return hasil, masalah


# ---------------------------------------------------------------------------
# Pemuatan semua input & pemeriksaan silang
# ---------------------------------------------------------------------------


def _normal_sektor(s: str) -> str:
    return " ".join(s.split()).casefold()


def _cek_sektor_silang(
    universe: list[SahamUniverse],
    portofolio: list[BarisPortofolio],
    nama_universe: str,
    nama_portofolio: str,
) -> list[Masalah]:
    """PERINGATAN bila sektor portfolio berbeda dari universe (yang dipakai: universe), dan bila
    satu sektor ditulis dengan ejaan berbeda (huruf besar/kecil, spasi) — batas sektor membandingkan
    teks persis (§6.1: penamaan harus konsisten)."""
    hasil: list[Masalah] = []
    peta = {s.kode: s.sektor for s in universe}
    for p in portofolio:
        if p.sektor and p.kode in peta and p.sektor != peta[p.kode]:
            hasil.append(
                _peringatan(
                    nama_portofolio, p.baris,
                    f"sektor {p.sektor!r} berbeda dengan universe.csv ({peta[p.kode]!r}); yang dipakai: "
                    f"{peta[p.kode]!r}",
                )
            )
    ejaan: dict[str, list[str]] = {}
    for s in [u.sektor for u in universe] + [p.sektor for p in portofolio if p.sektor]:
        daftar = ejaan.setdefault(_normal_sektor(s), [])
        if s not in daftar:
            daftar.append(s)
    for daftar in ejaan.values():
        if len(daftar) > 1:
            hasil.append(
                _peringatan(
                    nama_universe, None,
                    "sektor ditulis dengan ejaan berbeda dan dianggap sektor berbeda: "
                    + ", ".join(repr(s) for s in daftar) + " — samakan penulisannya",
                )
            )
    return hasil


def muat_masukan(ctx: KonteksApp) -> Masukan:
    """Baca semua berkas input dari path di config (relatif terhadap `ctx.akar`). Tidak melempar
    exception untuk input yang salah: semua masalah dikumpulkan di `Masukan.masalah`
    (lihat `pastikan_valid`)."""
    cfg = ctx.cfg

    def lokasi(rel: str) -> tuple[Path, str]:
        p = ctx.path(rel)
        return p, _nama_relatif(p, ctx.akar)

    masalah: list[Masalah] = []
    p_univ, n_univ = lokasi(cfg.universe.file)
    universe, m = baca_universe(p_univ, n_univ)
    masalah += m
    breadth, m = baca_breadth(*lokasi(cfg.kondisi_pasar.file_breadth_universe))
    masalah += m
    riwayat, m = baca_riwayat(*lokasi(cfg.universe.file_riwayat))
    masalah += m
    exclude, m = baca_exclude(*lokasi(cfg.universe.file_exclude))
    masalah += m
    p_port, n_port = lokasi(cfg.universe.file_portfolio)
    portofolio, m = baca_portofolio(p_port, {s.kode for s in universe}, n_port)
    masalah += m
    p_akun, n_akun = lokasi(cfg.akun.file_akun)
    akun, m = baca_akun(p_akun, Decimal(cfg.akun.modal_awal_idr), n_akun)
    masalah += m
    libur, m = baca_libur(*lokasi(cfg.pasar.file_libur))
    masalah += m
    anomali_ok, m = baca_anomali_ok(*lokasi(cfg.data.file_anomali_ok))
    masalah += m
    masalah += _cek_sektor_silang(universe, portofolio, n_univ, n_port)
    return Masukan(
        universe=universe, breadth=breadth, riwayat=riwayat, exclude=exclude, portofolio=portofolio,
        akun=akun, libur=libur, anomali_ok=anomali_ok, masalah=masalah,
    )


def pastikan_valid(m: Masukan) -> None:
    """Lempar `InputTidakValid` (kode keluar 3) jika ada ERROR."""
    galat = m.daftar_error()
    if galat:
        raise InputTidakValid(galat)


def cek_portofolio_kalender(
    portofolio: Iterable[BarisPortofolio],
    kal: Kalender,
    t: date,
    nama_berkas: str = "input/portfolio.csv",
) -> list[Masalah]:
    """§6.5: tanggal_beli harus hari bursa (kalender manual) dan ≤ t; harga_per_tanggal ≤ t.
    Pelanggaran = ERROR."""
    hasil: list[Masalah] = []
    for p in portofolio:
        if p.tanggal_beli > t:
            hasil.append(
                _error(nama_berkas, p.baris,
                       f"tanggal_beli {p.tanggal_beli.isoformat()} setelah tanggal data t ({t.isoformat()})")
            )
        if not kal.adalah_hari_bursa_manual(p.tanggal_beli):
            hasil.append(
                _error(nama_berkas, p.baris,
                       f"tanggal_beli {p.tanggal_beli.isoformat()} bukan hari bursa "
                       "(akhir pekan atau libur di holidays.csv)")
            )
        if p.harga_per_tanggal is not None and p.harga_per_tanggal > t:
            hasil.append(
                _error(nama_berkas, p.baris,
                       f"harga_per_tanggal {p.harga_per_tanggal.isoformat()} setelah tanggal data t "
                       f"({t.isoformat()})")
            )
    return hasil


def cek_umur_akun(
    akun: Akun,
    kal: Kalender,
    hari_ini: date,
    maks_hari_bursa: int,
    nama_berkas: str = "input/akun.yaml",
) -> Masalah | None:
    """§6.6: PERINGATAN bila jumlah hari bursa (kalender manual) dalam (diperbarui, hari_ini]
    melebihi `maks_hari_bursa`, atau bila `diperbarui` di masa depan. Akun tanpa berkas → None
    (sudah diperingatkan oleh `baca_akun`)."""
    if akun.diperbarui is None:
        return None
    if akun.diperbarui > hari_ini:
        return _peringatan(
            nama_berkas, None,
            f"diperbarui {akun.diperbarui.isoformat()} berada di masa depan (hari ini {hari_ini.isoformat()})",
        )
    jumlah = len(kal.daftar_hari_bursa(akun.diperbarui + timedelta(days=1), hari_ini, manual=True))
    if jumlah > maks_hari_bursa:
        return _peringatan(
            nama_berkas, None,
            f"kas terakhir diperbarui {akun.diperbarui.isoformat()} ({jumlah} hari bursa lalu, batas "
            f"{maks_hari_bursa}) — perbarui kas_idr dan diperbarui",
        )
    return None

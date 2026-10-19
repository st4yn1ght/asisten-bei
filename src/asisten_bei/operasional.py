"""Operasional: kunci proses, tulis atomik, penyaring rahasia, log, kode keluar (SPEC §19–§20).

Modul ini sengaja hanya mengimpor pustaka standar di tingkat modul: `pasang_penyaring_rahasia()`
adalah langkah PERTAMA di `__main__` (§19.5), jadi harus bisa dipasang walaupun pandas/filelock
gagal diimpor (venv rusak). Dependensi pihak ketiga diimpor malas di dalam fungsi.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
import traceback
import urllib.parse
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import DISCLAIMER_CSV, waktu

if TYPE_CHECKING:  # pragma: no cover
    import pandas as pd

    from .config import KonteksApp

# ---------------------------------------------------------------------------
# Kode keluar (§20.1)
# ---------------------------------------------------------------------------

KODE_OK = 0  # termasuk "tidak ada data baru" dan `cek` yang hanya berisi peringatan
KODE_ERROR = 1  # error tak terduga, atau identitas akuntansi backtest gagal
KODE_DATA = 2  # BELUM_TERSEDIA, atau TIDAK_LENGKAP karena IHSG/K0
KODE_INPUT = 3  # config/input tidak valid
KODE_NOTIFIKASI = 4  # laporan dibuat, notifikasi gagal
KODE_KUNCI = 5  # proses lain sedang berjalan
KODE_JURNAL = 6  # jurnal gagal (terkunci/rusak)

# Prioritas bila beberapa kondisi terjadi bersamaan: 3 > 1 > 5 > 2 > 6 > 4 > 0.
URUTAN_PRIORITAS_KODE: tuple[int, ...] = (3, 1, 5, 2, 6, 4, 0)


def gabung_kode_keluar(kode: Iterable[int]) -> int:
    """Kode keluar akhir dari beberapa kondisi: yang prioritasnya tertinggi menang. Kosong → 0."""
    daftar = list(kode)
    for k in daftar:
        if k not in URUTAN_PRIORITAS_KODE:
            raise ValueError(f"kode keluar tidak dikenal: {k!r}")
    if not daftar:
        return KODE_OK
    return min(daftar, key=URUTAN_PRIORITAS_KODE.index)


# ---------------------------------------------------------------------------
# Kunci proses (§19.3)
# ---------------------------------------------------------------------------

BERKAS_KUNCI = Path("data") / ".kunci"
BERKAS_KUNCI_INFO = Path("data") / ".kunci.info"


class KunciDipegang(Exception):
    """Kunci `data/.kunci` sedang dipegang proses lain (kode keluar 5).

    `pesan`: "proses lain sedang berjalan: <perintah> sejak <waktu>" (dibaca dari `.kunci.info`).
    `info`: isi `.kunci.info` (dict) atau None bila tidak terbaca.
    """

    kode_keluar = KODE_KUNCI

    def __init__(self, pesan: str, info: dict | None = None):
        self.pesan = pesan
        self.info = info
        super().__init__(pesan)


def _baca_info_kunci(path: Path) -> dict | None:
    try:
        isi = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # belum ditulis / sedang diganti / rusak: pesan memakai "tidak diketahui"
    return isi if isinstance(isi, dict) else None


def _format_waktu_info(nilai: Any) -> str:
    """ISO WIB dari `.kunci.info` → "YYYY-MM-DD HH:MM:SS WIB" (teks asli bila tidak terbaca)."""
    if not isinstance(nilai, str) or not nilai:
        return "(waktu tidak diketahui)"
    try:
        dt = waktu.ke_wib(datetime.fromisoformat(nilai))
    except ValueError:
        return nilai
    return dt.strftime("%Y-%m-%d %H:%M:%S") + " WIB"


def pesan_kunci_dipegang(info: dict | None) -> str:
    info = info or {}
    perintah = info.get("perintah") or "(perintah tidak diketahui)"
    return f"proses lain sedang berjalan: {perintah} sejak {_format_waktu_info(info.get('waktu_mulai'))}"


@contextmanager
def kunci_proses(akar: Path, perintah: str) -> Iterator[dict]:
    """Pegang kunci tingkat OS `akar/data/.kunci` selama blok `with` (§19.3).

    Kunci `filelock` lepas otomatis bila proses mati. Setelah kunci didapat, PID, perintah, dan
    waktu mulai (WIB, dari `waktu.sekarang()`) ditulis ke `data/.kunci.info` — berkas terpisah yang
    tidak dikunci sehingga bisa dibaca proses lain. Gagal mendapat kunci → `KunciDipegang`.
    Saat lepas: `.kunci.info` dihapus dulu, baru kunci dilepas (proses lain tidak pernah membaca
    info basi milik pemegang yang sudah selesai normal).
    Menghasilkan dict info yang ditulis.
    """
    from filelock import FileLock, Timeout

    akar = Path(akar)
    path_kunci = akar / BERKAS_KUNCI
    path_info = akar / BERKAS_KUNCI_INFO
    path_kunci.parent.mkdir(parents=True, exist_ok=True)
    kunci = FileLock(str(path_kunci), timeout=0)
    try:
        kunci.acquire()
    except Timeout:
        info_lain = _baca_info_kunci(path_info)
        raise KunciDipegang(pesan_kunci_dipegang(info_lain), info_lain) from None
    try:
        info = {
            "pid": os.getpid(),
            "perintah": perintah,
            "waktu_mulai": waktu.sekarang().isoformat(timespec="seconds"),
        }
        # Info basi dari pemegang yang mati (SIGKILL) langsung tertimpa di sini.
        tulis_atomik(path_info, json.dumps(info, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        yield info
    finally:
        try:
            path_info.unlink(missing_ok=True)
        except OSError:
            pass  # tidak fatal: info basi akan tertimpa pemegang berikutnya
        kunci.release()


# ---------------------------------------------------------------------------
# Penulisan berkas (atomik; nama alternatif bila terkunci §18.2)
# ---------------------------------------------------------------------------


def tulis_atomik(path: Path, isi: str | bytes, encoding: str = "utf-8") -> None:
    """Tulis berkas secara atomik: berkas sementara di folder yang sama lalu `os.replace`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = isi.encode(encoding) if isinstance(isi, str) else isi
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def nama_alternatif(path: Path, saat: datetime | None = None) -> Path:
    """`<stem>_<HHMMSS><suffix>` di folder yang sama; jam WIB dari `waktu.sekarang()` (§18.2)."""
    path = Path(path)
    saat = waktu.sekarang() if saat is None else waktu.ke_wib(saat)
    return path.with_name(f"{path.stem}_{saat:%H%M%S}{path.suffix}")


def tulis_keluaran(path: Path, isi: str | bytes, encoding: str = "utf-8") -> tuple[Path, str | None]:
    """Seperti `tulis_atomik`, tetapi bila berkas tujuan terkunci (mis. sedang dibuka di Excel,
    `PermissionError`), menulis ke `nama_alternatif(path)` dan mengembalikan teks peringatan.

    Mengembalikan (path yang benar-benar ditulis, peringatan atau None). Bila nama alternatif juga
    gagal ditulis, `PermissionError` diteruskan ke pemanggil.
    """
    path = Path(path)
    try:
        tulis_atomik(path, isi, encoding)
        return path, None
    except PermissionError:
        alt = nama_alternatif(path)
        tulis_atomik(alt, isi, encoding)
        return alt, (
            f"Berkas {path.name} sedang terkunci (mungkin dibuka di Excel); "
            f"keluaran ditulis ke {alt.name}"
        )


def teks_csv(df: "pd.DataFrame", index: bool = False) -> str:
    """Isi CSV keluaran: baris pertama disclaimer (`# ...`), lalu tabel dengan akhir baris "\\n"."""
    return DISCLAIMER_CSV + "\n" + df.to_csv(index=index, lineterminator="\n")


def tulis_csv(
    path: Path, df: "pd.DataFrame", index: bool = False, encoding: str = "utf-8"
) -> tuple[Path, str | None]:
    """CSV keluaran (disclaimer di baris pertama), atomik, dengan nama alternatif bila terkunci.
    Dibaca kembali dengan `pd.read_csv(path, comment="#")` atau `skiprows=1`."""
    return tulis_keluaran(path, teks_csv(df, index=index), encoding=encoding)


# ---------------------------------------------------------------------------
# Penyaring rahasia (§19.5)
# ---------------------------------------------------------------------------

PANJANG_MIN_RAHASIA = 6
GANTI_RAHASIA = "***"
ENV_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_CHAT_ID = "TELEGRAM_CHAT_ID"


def _hex_kecil(teks: str) -> str:
    """Varian percent-encoding dengan digit heksadesimal huruf kecil (`%3A` → `%3a`)."""
    return re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), teks)


class PenyaringRahasia:
    """Mengganti setiap nilai rahasia (dan bentuk URL-encoded-nya) dengan "***".

    Satu instans modul: `PENYARING`. Rahasia ditambahkan dari env (`pasang_penyaring_rahasia`)
    dan dari `.env` (`muat_env`). Nilai kosong atau lebih pendek dari 6 karakter diabaikan agar
    tidak menyensor teks biasa.
    """

    def __init__(self) -> None:
        self._rahasia: set[str] = set()
        self._varian: tuple[str, ...] = ()

    def tambah(self, nilai: str | None) -> None:
        if not nilai:
            return
        nilai = str(nilai).strip()
        if len(nilai) < PANJANG_MIN_RAHASIA or nilai in self._rahasia:
            return
        self._rahasia.add(nilai)
        varian: set[str] = set(self._varian)
        for v in (nilai, urllib.parse.quote(nilai), urllib.parse.quote(nilai, safe=""), urllib.parse.quote_plus(nilai)):
            varian.add(v)
            varian.add(_hex_kecil(v))
        # Terpanjang dulu: rahasia yang memuat rahasia lain tersensor utuh.
        self._varian = tuple(sorted(varian, key=lambda v: (-len(v), v)))

    def ada(self) -> bool:
        return bool(self._rahasia)

    def sensor(self, teks: str) -> str:
        if not self._varian or not isinstance(teks, str) or not teks:
            return teks
        for v in self._varian:
            if v in teks:
                teks = teks.replace(v, GANTI_RAHASIA)
        return teks


PENYARING = PenyaringRahasia()


class _PenulisTersensor:
    """Pembungkus aliran teks (`sys.stdout`/`sys.stderr`) yang menyensor rahasia sebelum menulis.

    Atribut lain (encoding, errors, isatty, fileno, buffer, reconfigure, ...) diteruskan ke aliran
    asli. Catatan: tulisan langsung ke `.buffer` (byte) tidak melewati penyaring.
    """

    def __init__(self, asli: Any):
        self._asli = asli

    def write(self, s: Any) -> int:
        if isinstance(s, str):
            self._asli.write(PENYARING.sensor(s))
            return len(s)
        return self._asli.write(s)

    def writelines(self, baris: Iterable[str]) -> None:
        for b in baris:
            self.write(b)

    def flush(self) -> None:
        flush = getattr(self._asli, "flush", None)
        if flush is not None:
            flush()

    def __getattr__(self, nama: str) -> Any:
        return getattr(self._asli, nama)


def _excepthook_tersensor(jenis, nilai, tb) -> None:
    """`sys.excepthook` pengganti: traceback (termasuk rantai __cause__/__context__) disensor
    sebelum ditulis ke stderr."""
    teks = "".join(traceback.format_exception(jenis, nilai, tb))
    tujuan = sys.stderr if sys.stderr is not None else sys.__stderr__
    if tujuan is None:  # pragma: no cover - pythonw tanpa konsol
        return
    tujuan.write(PENYARING.sensor(teks))
    try:
        tujuan.flush()
    except Exception:  # pragma: no cover
        pass


def _ganti_karakter_tak_tertampil(aliran) -> None:
    """Konsol/berkas bukan UTF-8 (mis. cp1252 Windows tanpa PYTHONUTF8): karakter yang tak bisa ditulis
    diganti `?`, bukan UnicodeEncodeError yang menghentikan program (§19.2)."""
    enc = (getattr(aliran, "encoding", None) or "").lower().replace("-", "").replace("_", "")
    if enc in ("utf8", "utf8sig") or not hasattr(aliran, "reconfigure"):
        return
    try:
        aliran.reconfigure(errors="replace")
    except (AttributeError, ValueError, OSError):  # aliran tertutup/tak bisa dikonfigurasi: biarkan
        pass


def pasang_penyaring_rahasia() -> None:
    """Langkah PERTAMA di `__main__` (§19.5): daftarkan `TELEGRAM_BOT_TOKEN` dari env, bungkus
    `sys.stdout`/`sys.stderr`, dan ganti `sys.excepthook`. Idempoten."""
    PENYARING.tambah(os.environ.get(ENV_TOKEN))
    for nama in ("stdout", "stderr"):
        aliran = getattr(sys, nama)
        if aliran is not None and not isinstance(aliran, _PenulisTersensor):
            _ganti_karakter_tak_tertampil(aliran)
            setattr(sys, nama, _PenulisTersensor(aliran))
    if sys.excepthook is not _excepthook_tersensor:
        sys.excepthook = _excepthook_tersensor


def muat_env(akar: Path) -> dict[str, str]:
    """Baca `akar/.env` (python-dotenv, tanpa interpolasi `$`); BOM dari Notepad diterima.
    Tidak ada berkas → {}. `TELEGRAM_BOT_TOKEN` didaftarkan ke `PENYARING`."""
    path = Path(akar) / ".env"
    if not path.is_file():
        return {}
    from dotenv import dotenv_values

    mentah = dotenv_values(path, interpolate=False, encoding="utf-8-sig")
    hasil = {str(k): ("" if v is None else str(v).strip()) for k, v in mentah.items()}
    PENYARING.tambah(hasil.get(ENV_TOKEN))
    return hasil


def cek_env(akar: Path, telegram_aktif: bool) -> list[tuple[str, str]]:
    """Pemeriksaan `.env` untuk `cek` (§19.2, §19.4). Mengembalikan daftar (level, teks)."""
    akar = Path(akar)
    hasil: list[tuple[str, str]] = []
    if (akar / ".env.txt").exists():
        hasil.append(("PERINGATAN", "ganti nama .env.txt menjadi .env (Windows menyembunyikan ekstensi)"))
    if not telegram_aktif:
        return hasil
    if not (akar / ".env").is_file():
        hasil.append(("PERINGATAN", "Telegram belum disiapkan (.env tidak ada)"))
        return hasil
    try:
        env = muat_env(akar)
    except (UnicodeDecodeError, OSError) as e:
        hasil.append(("ERROR", f".env tidak terbaca ({type(e).__name__}): simpan ulang dengan Notepad sebagai UTF-8"))
        return hasil
    kosong = [k for k in (ENV_TOKEN, ENV_CHAT_ID) if not env.get(k)]
    if kosong:
        hasil.append(("PERINGATAN", f"Telegram belum disiapkan: {', '.join(kosong)} di .env masih kosong"))
    return hasil


# ---------------------------------------------------------------------------
# Log (§19.5)
# ---------------------------------------------------------------------------

NAMA_LOGGER = "asisten_bei"
FOLDER_LOG = Path("output") / "log"
_POLA_LOG_HARIAN = re.compile(r"^(\d{4}-\d{2}-\d{2})\.log$")


class FilterRahasiaLog(logging.Filter):
    """Menyensor pesan, traceback (`exc_text`), dan `stack_info` sebuah record sebelum diformat.

    Pasang pada HANDLER (bukan logger) agar berlaku juga untuk record dari logger anak.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            pesan = record.getMessage()
        except Exception:  # argumen format tidak cocok: tetap sensor teks mentahnya
            pesan = str(record.msg)
        record.msg = PENYARING.sensor(pesan)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = PENYARING.sensor(record.exc_text)
        if record.stack_info:
            record.stack_info = PENYARING.sensor(record.stack_info)
        return True


class _FormatterWIB(logging.Formatter):
    """Stempel waktu log dari `waktu.sekarang()` (WIB; virtual saat uji)."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return waktu.sekarang().strftime(datefmt or "%Y-%m-%d %H:%M:%S")


def hapus_log_lama(folder: Path, hari_ini: date, simpan_hari: int) -> list[Path]:
    """Hapus `YYYY-MM-DD.log` yang umurnya > `simpan_hari` hari. Berkas lain (mis. terjadwal.log)
    tidak disentuh. Mengembalikan daftar berkas yang dihapus."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    dihapus = []
    for p in sorted(folder.iterdir()):
        m = _POLA_LOG_HARIAN.match(p.name)
        if not m or not p.is_file():
            continue
        try:
            tgl = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if (hari_ini - tgl).days > simpan_hari:
            try:
                p.unlink()
                dihapus.append(p)
            except OSError:
                pass  # mis. sedang dibuka; dicoba lagi pada run berikutnya
    return dihapus


def siapkan_log(ctx: "KonteksApp") -> logging.Logger:
    """Logger "asisten_bei" → `output/log/<YYYY-MM-DD>.log` (tanggal dari `waktu.sekarang()`, UTF-8,
    tersensor). Log lebih tua dari `operasional.simpan_log_hari` dihapus. Idempoten: tidak menambah
    handler ganda; handler milik berkas lain (akar/tanggal berbeda) diganti."""
    folder = ctx.path(FOLDER_LOG)
    folder.mkdir(parents=True, exist_ok=True)
    hari = waktu.hari_ini()
    path = folder / f"{hari.isoformat()}.log"
    logger = logging.getLogger(NAMA_LOGGER)
    logger.setLevel(logging.INFO)

    sasaran = os.path.abspath(path)
    sudah_ada = False
    for h in list(logger.handlers):
        if not getattr(h, "_asisten_bei", False):
            continue
        if not sudah_ada and getattr(h, "baseFilename", None) == sasaran:
            sudah_ada = True
            continue
        logger.removeHandler(h)
        h.close()
    if not sudah_ada:
        handler = logging.FileHandler(path, encoding="utf-8")
        handler._asisten_bei = True  # penanda handler milik modul ini
        handler.setLevel(logging.INFO)
        handler.setFormatter(_FormatterWIB("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler.addFilter(FilterRahasiaLog())
        logger.addHandler(handler)
    hapus_log_lama(folder, hari, ctx.cfg.operasional.simpan_log_hari)
    return logger


def tutup_log() -> None:
    """Lepas dan tutup handler berkas milik `siapkan_log` (akhir run / akhir uji)."""
    logger = logging.getLogger(NAMA_LOGGER)
    for h in list(logger.handlers):
        if getattr(h, "_asisten_bei", False):
            logger.removeHandler(h)
            h.close()

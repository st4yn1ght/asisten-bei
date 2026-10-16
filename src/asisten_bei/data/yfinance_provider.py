"""Provider yfinance (SPEC §8.1).

- Satu permintaan `Ticker(simbol).history(...)` per simbol. Parameter `end` di yfinance bersifat
  eksklusif → `akhir + 1 hari` (akhir di antarmuka kita inklusif).
- `repair=False` (perbaikan yfinance mengubah harga → melanggar P1) dan `auto_adjust=False`
  (OHLC hanya disesuaikan split; dividen tidak). Opsi lain yang memengaruhi angka ditulis eksplisit
  agar perubahan default di versi yfinance berikutnya tidak diam-diam mengubah data.
- Tanggal bar = tanggal lokal WIB dari index ber-zona waktu (`tz_convert("Asia/Jakarta")`), **tidak
  pernah lewat UTC**. Index tanpa zona waktu atau bar bertanggal Sabtu/Minggu → seri GAGAL FORMAT.
- Exception diklasifikasikan ke enum `jenis_error_ambil` (§4.3). Retry, jeda antar-permintaan, dan
  penghentian setelah batas permintaan berturut-turut diatur pemanggil (orkestrator unduhan).

yfinance diimpor malas: modul ini bisa diimpor dan diuji dengan `pabrik_ticker` palsu tanpa
menyentuh jaringan.
"""

from __future__ import annotations

import errno
import functools
import inspect
import json
import re
import socket
import ssl
import urllib.error
import warnings
from collections.abc import Callable
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from ..inputs import KODE_IHSG
from ..operasional import PENYARING
from .provider import (
    KOLOM_BAR,
    NAMA_INDEX,
    HasilAmbil,
    JenisErrorAmbil,
    gagal,
    ke_ts,
    split_kosong,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config

ZONA_BURSA = "Asia/Jakarta"
PESAN_ZONA = "tanggal/zona waktu tidak dikenali"
PANJANG_MAKS_PESAN = 500  # pesan error dipotong agar berkas kualitas tetap terbaca

_KOLOM_HARGA: tuple[str, ...] = ("open", "high", "low", "close")
# Kolom peristiwa yfinance (huruf kecil). Dividen & capital gains dibuang; split disimpan terpisah.
_KOLOM_PERISTIWA: tuple[str, ...] = ("dividends", "stock splits", "capital gains")
_KOLOM_SPLIT = "stock splits"

# ---------------------------------------------------------------------------
# Klasifikasi error (§8.1)
# ---------------------------------------------------------------------------

_POLA_BATAS = re.compile(r"\b429\b|too many requests|rate[ _-]?limit", re.IGNORECASE)
_POLA_TIDAK_ADA = re.compile(
    r"possibly delisted|no data found|no timezone found|symbol may be delisted|no price data found"
    r"|quote not found",
    re.IGNORECASE,
)
_POLA_JARINGAN = re.compile(
    r"failed to connect|could not resolve|couldn'?t resolve|connect tunnel failed|timed out"
    r"|connection reset|connection refused|connection aborted|network is unreachable"
    r"|temporary failure in name resolution|name or service not known|currently down",
    re.IGNORECASE,
)
# Nama kelas yang dianggap error transien oleh yfinance sendiri (data._is_transient_error).
_NAMA_KELAS_JARINGAN = frozenset(
    {"Timeout", "TimeoutError", "ConnectionError", "ConnectTimeout", "ReadTimeout",
     "ChunkedEncodingError", "RemoteDisconnected"}
)
_ERRNO_JARINGAN = frozenset(
    getattr(errno, n)
    for n in ("ECONNREFUSED", "ECONNRESET", "ECONNABORTED", "ENETUNREACH", "ENETDOWN",
              "ENETRESET", "EHOSTUNREACH", "EHOSTDOWN", "ETIMEDOUT", "EPIPE")
    if hasattr(errno, n)
)


@functools.cache
def _kelas_yfinance() -> tuple[tuple[type, ...], tuple[type, ...]]:
    """(kelas batas permintaan, kelas tidak ada data) dari `yfinance.exceptions` yang terpasang.

    Nama diambil dengan getattr agar versi yfinance yang tidak punya salah satu kelas tetap jalan.
    """
    try:
        from yfinance import exceptions as yfe
    except Exception:  # pragma: no cover - yfinance selalu terpasang di venv
        return (), ()
    batas = tuple(k for k in (getattr(yfe, "YFRateLimitError", None),) if isinstance(k, type))
    tidak_ada = tuple(
        k
        for k in (getattr(yfe, n, None) for n in ("YFTzMissingError", "YFPricesMissingError",
                                                  "YFTickerMissingError"))
        if isinstance(k, type)
    )
    return batas, tidak_ada


@functools.cache
def _kelas_http_json() -> tuple[tuple[type, ...], tuple[type, ...]]:
    """(kelas HTTPError, kelas error JSON) dari requests/curl_cffi/urllib (impor opsional).

    Di curl_cffi dan requests, HTTPError dan JSONDecodeError adalah turunan OSError/RequestException;
    keduanya **bukan** gangguan jaringan sehingga harus disisihkan sebelum pemeriksaan jaringan.
    """
    http: list[type] = [urllib.error.HTTPError]
    js: list[type] = [json.JSONDecodeError]
    try:
        import requests.exceptions as rq

        http.append(rq.HTTPError)
        js.append(rq.InvalidJSONError)
    except Exception:  # pragma: no cover
        pass
    try:
        from curl_cffi.requests import exceptions as cr

        http.append(cr.HTTPError)
        js.append(cr.InvalidJSONError)
    except Exception:  # pragma: no cover
        pass
    return tuple(http), tuple(js)


@functools.cache
def _kelas_jaringan() -> tuple[type, ...]:
    """Kelas exception gangguan jaringan (impor opsional requests/urllib3/curl_cffi)."""
    kelas: list[type] = [ConnectionError, TimeoutError, socket.gaierror, socket.herror, ssl.SSLError]
    try:
        import requests.exceptions as rq

        kelas += [rq.ConnectionError, rq.Timeout, rq.ChunkedEncodingError]
    except Exception:  # pragma: no cover
        pass
    try:
        import urllib3.exceptions as u3

        kelas += [u3.ProtocolError, u3.NewConnectionError, u3.MaxRetryError, u3.TimeoutError]
    except Exception:  # pragma: no cover
        pass
    try:
        from curl_cffi.curl import CurlError
        from curl_cffi.requests import exceptions as cr

        # RequestsError/CurlError umum: HTTPError & JSON sudah disisihkan lebih dulu.
        kelas += [cr.RequestException, CurlError]
    except Exception:  # pragma: no cover
        pass
    return tuple(kelas)


def _status_http(exc: BaseException) -> int | None:
    """Kode status HTTP bila exception membawanya (requests/curl_cffi `.response`, urllib `.code`)."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code if isinstance(exc.code, int) else None
    kode = getattr(getattr(exc, "response", None), "status_code", None)
    return kode if isinstance(kode, int) and not isinstance(kode, bool) else None


def _rantai(exc: BaseException, maks: int = 8) -> list[BaseException]:
    """exc beserta penyebabnya (`__cause__`, atau `__context__` bila tidak disembunyikan)."""
    hasil: list[BaseException] = []
    e: BaseException | None = exc
    while e is not None and len(hasil) < maks and all(e is not x for x in hasil):
        hasil.append(e)
        e = e.__cause__ if e.__cause__ is not None else (None if e.__suppress_context__ else e.__context__)
    return hasil


def _klasifikasi_tipe(e: BaseException) -> JenisErrorAmbil | None:
    """Klasifikasi berdasarkan kelas & status HTTP saja; None = belum dapat diputuskan."""
    batas, tidak_ada = _kelas_yfinance()
    if batas and isinstance(e, batas):
        return "BATAS_PERMINTAAN"
    status = _status_http(e)
    if status == 429:
        return "BATAS_PERMINTAAN"
    if tidak_ada and isinstance(e, tidak_ada):
        return "TIDAK_ADA_DATA"
    if status == 404:
        return "TIDAK_ADA_DATA"
    if status is not None and 500 <= status <= 599:
        return "JARINGAN"  # gangguan sisi server bersifat sementara → layak dicoba lagi
    http, js = _kelas_http_json()
    if status is not None or isinstance(e, http) or isinstance(e, js):
        return None  # respons diterima tetapi tidak dikenali: bukan jaringan
    if isinstance(e, _kelas_jaringan()) or type(e).__name__ in _NAMA_KELAS_JARINGAN:
        return "JARINGAN"
    if isinstance(e, OSError) and e.errno in _ERRNO_JARINGAN:
        return "JARINGAN"
    return None


def klasifikasi_error(exc: BaseException) -> JenisErrorAmbil:
    """Petakan exception dari yfinance ke `jenis_error_ambil` (§8.1).

    Urutan: kelas/status HTTP di seluruh rantai exception lebih dulu (paling andal), lalu teks:
    - BATAS_PERMINTAAN: `YFRateLimitError`, HTTP 429, teks "429"/"Too Many Requests"/"rate limit"
    - TIDAK_ADA_DATA: `YFTzMissingError`/`YFPricesMissingError`/`YFTickerMissingError`, HTTP 404,
      teks "possibly delisted"/"No data found"/"No timezone found"/"symbol may be delisted"
    - JARINGAN: koneksi/timeout (builtin, socket, ssl, requests, urllib3, curl_cffi), OSError dengan
      errno jaringan, HTTP 5xx, teks "Failed to connect"/"Could not resolve"/"CONNECT tunnel
      failed"/"timed out"/"Connection reset"/"currently down"
    - selainnya: FORMAT
    """
    rantai = _rantai(exc)
    for e in rantai:
        jenis = _klasifikasi_tipe(e)
        if jenis is not None:
            return jenis
    teks = " | ".join(f"{type(e).__name__}: {e}" for e in rantai)
    if _POLA_BATAS.search(teks):
        return "BATAS_PERMINTAAN"
    if _POLA_TIDAK_ADA.search(teks):
        return "TIDAK_ADA_DATA"
    if _POLA_JARINGAN.search(teks):
        return "JARINGAN"
    return "FORMAT"


def pesan_error(simbol: str, exc: BaseException, awalan: str = "") -> str:
    """Teks error asli untuk pengguna/log: satu baris, dipotong, token rahasia disensor (§19.5)."""
    teks = " ".join(str(exc).split()) or "(tanpa pesan)"
    pesan = f"{simbol}: {awalan}{type(exc).__name__}: {teks}"
    pesan = PENYARING.sensor(pesan)
    if len(pesan) > PANJANG_MAKS_PESAN:
        pesan = pesan[: PANJANG_MAKS_PESAN - 1] + "…"
    return pesan


# ---------------------------------------------------------------------------
# yfinance terpasang (impor malas)
# ---------------------------------------------------------------------------


@functools.cache
def versi_yfinance() -> str:
    """`yfinance.__version__` (dicatat di meta cache); "" bila yfinance tidak dapat diimpor."""
    try:
        import yfinance

        return str(getattr(yfinance, "__version__", ""))
    except Exception:  # pragma: no cover
        return ""


@functools.cache
def _history_menerima_raise_errors() -> bool:
    """Apakah `history()` versi terpasang menerima `raise_errors`.

    `Ticker.history` di yfinance 1.x hanya `(*args, **kwargs)` dan meneruskan ke
    `scrapers.history.PriceHistory.history`, jadi tanda tangan itu yang diperiksa.
    """
    fungsi: Any = None
    try:
        from yfinance.scrapers.history import PriceHistory

        fungsi = PriceHistory.history
    except Exception:
        try:
            import yfinance

            fungsi = yfinance.Ticker.history
        except Exception:  # pragma: no cover
            return False
    try:
        return "raise_errors" in inspect.signature(fungsi).parameters
    except (TypeError, ValueError):  # pragma: no cover
        return False


def _siapkan_yfinance() -> Callable[[str], Any]:
    """Pabrik `Ticker` asli. Matikan penyembunyian exception yfinance (`config.debug.hide_exceptions`).

    Tanpa ini, kegagalan jaringan saat yfinance mengambil zona waktu hanya di-log lalu muncul sebagai
    `YFTzMissingError` ("possibly delisted") → salah diklasifikasikan TIDAK_ADA_DATA dan simbol tidak
    dicoba lagi selama `coba_lagi_tidak_ada_data_hari` hari.
    """
    import yfinance

    try:
        yfinance.config.debug.hide_exceptions = False
    except AttributeError:  # pragma: no cover - versi lama tanpa yfinance.config
        pass
    return yfinance.Ticker


def argumen_history(mulai: date, akhir: date) -> dict[str, Any]:
    """Argumen `Ticker.history` (§8.1). `end` eksklusif di yfinance → `akhir + 1 hari`."""
    argumen: dict[str, Any] = {
        "start": mulai,
        "end": akhir + timedelta(days=1),
        "interval": "1d",
        "auto_adjust": False,
        "back_adjust": False,
        "actions": True,
        "repair": False,
        "keepna": False,
        "prepost": False,
        "rounding": False,
    }
    if _history_menerima_raise_errors():
        argumen["raise_errors"] = True
    return argumen


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class YFinanceProvider:
    """Penyedia data harian dari Yahoo Finance lewat pustaka yfinance."""

    nama = "yfinance"

    def __init__(self, cfg: "Config", pabrik_ticker: Callable[[str], Any] | None = None):
        self.cfg = cfg
        self._pabrik = pabrik_ticker  # None → yfinance.Ticker (diimpor saat pertama dipakai)

    @property
    def versi_provider(self) -> str:
        return versi_yfinance()

    def simbol(self, kode: str) -> str:
        """"IHSG" → `data.simbol_ihsg.yfinance`; selain itu kode + `data.akhiran_yfinance`."""
        if kode == KODE_IHSG:
            return self.cfg.data.simbol_ihsg.yfinance
        return kode + self.cfg.data.akhiran_yfinance

    def _ticker(self, simbol: str) -> Any:
        if self._pabrik is None:
            self._pabrik = _siapkan_yfinance()
        return self._pabrik(simbol)

    def ambil_harian(self, kode: str, mulai: date, akhir: date) -> HasilAmbil:
        """Bar harian `mulai ≤ tanggal ≤ akhir` (akhir inklusif). Status "OK" bila sukses; penilaian
        OK/SEBAGIAN terhadap `t` dilakukan orkestrator."""
        simbol = self.simbol(kode)
        versi = self.versi_provider
        if mulai > akhir:
            return gagal(kode, self.nama, "FORMAT",
                         f"{simbol}: rentang tanggal tidak valid (mulai {mulai} > akhir {akhir})", versi)
        argumen = argumen_history(mulai, akhir)
        try:
            ticker = self._ticker(simbol)
            with warnings.catch_warnings():
                # yfinance 1.x menandai raise_errors usang (DeprecationWarning) tetapi tetap menerimanya.
                warnings.simplefilter("ignore", DeprecationWarning)
                df = ticker.history(**argumen)
        except Exception as e:
            return gagal(kode, self.nama, klasifikasi_error(e), pesan_error(simbol, e), versi)
        try:
            return self._olah(kode, simbol, df, mulai, akhir, versi)
        except (TypeError, ValueError, KeyError, AttributeError, IndexError) as e:
            # Struktur respons di luar dugaan → FORMAT (≥ 90% simbol FORMAT → cek versi yfinance, §22.3).
            return gagal(kode, self.nama, "FORMAT", pesan_error(simbol, e, "respons tidak dikenali: "), versi)

    # -- normalisasi respons ---------------------------------------------------

    def _olah(self, kode: str, simbol: str, df: Any, mulai: date, akhir: date, versi: str) -> HasilAmbil:
        adalah_indeks = kode == KODE_IHSG

        def _gagal(jenis: JenisErrorAmbil, pesan: str) -> HasilAmbil:
            return gagal(kode, self.nama, jenis, PENYARING.sensor(f"{simbol}: {pesan}"), versi)

        if not isinstance(df, pd.DataFrame):
            return _gagal("FORMAT", f"respons yfinance bukan tabel ({type(df).__name__})")
        if df.empty:
            return _gagal("TIDAK_ADA_DATA", f"yfinance tidak mengembalikan bar untuk {mulai}..{akhir}")
        if isinstance(df.columns, pd.MultiIndex):
            return _gagal("FORMAT", "kolom bertingkat (MultiIndex) tidak dikenali")

        nama_kolom = [str(c).strip().lower() for c in df.columns]
        ganda = sorted({k for k in nama_kolom if nama_kolom.count(k) > 1})
        if ganda:
            return _gagal("FORMAT", f"kolom ganda: {', '.join(ganda)}")
        df = df.set_axis(nama_kolom, axis=1)
        wajib = [k for k in KOLOM_BAR if not (adalah_indeks and k == "volume")]
        hilang = [k for k in wajib if k not in df.columns]
        if hilang:
            return _gagal("FORMAT", f"kolom wajib hilang: {', '.join(k.capitalize() for k in hilang)}")

        idx = df.index
        if not isinstance(idx, pd.DatetimeIndex):
            return _gagal("FORMAT", f"{PESAN_ZONA} (index bukan tanggal)")
        if idx.tz is None:
            return _gagal("FORMAT", f"{PESAN_ZONA} (index tanpa zona waktu)")
        # Tanggal lokal bursa: konversi langsung ke WIB, lalu ambil tanggalnya. Tidak lewat UTC.
        tanggal = idx.tz_convert(ZONA_BURSA).normalize().tz_localize(None).as_unit("ns")

        kolom: dict[str, np.ndarray] = {}
        for k in KOLOM_BAR:
            if k not in df.columns:  # hanya volume IHSG
                kolom[k] = np.full(len(df), np.nan)
                continue
            asli = df[k]
            angka = pd.to_numeric(asli, errors="coerce")
            if (angka.isna() & asli.notna()).any():
                return _gagal("FORMAT", f"tipe kolom {k} salah (bukan angka)")
            kolom[k] = angka.to_numpy(dtype="float64", na_value=np.nan)
        bar = pd.DataFrame(kolom, index=pd.DatetimeIndex(tanggal, name=NAMA_INDEX))
        catatan: list[str] = []

        tak_hingga = np.isinf(bar.to_numpy())
        if tak_hingga.any():
            # Bukan angka harga yang sah: ditandai kosong agar validasi per bar menganggapnya rusak.
            bar = bar.mask(tak_hingga)
            catatan.append(f"{int(tak_hingga.any(axis=1).sum())} bar memuat nilai tak hingga (dianggap kosong)")

        # Baris peristiwa tanpa harga: yfinance menambah baris OHLC NaN untuk dividen/split di luar
        # rentang harga. Itu bukan bar perdagangan → dibuang (split-nya tetap dicatat di seri split).
        peristiwa = np.zeros(len(df), dtype=bool)
        for k in _KOLOM_PERISTIWA:
            if k in df.columns:
                v = pd.to_numeric(df[k], errors="coerce").to_numpy(dtype="float64", na_value=np.nan)
                peristiwa |= ~np.isnan(v) & (v != 0)
        harga_kosong = bar[list(_KOLOM_HARGA)].isna().all(axis=1).to_numpy()
        vol = bar["volume"].to_numpy()
        tanpa_volume = np.isnan(vol) | (vol == 0)
        buang = peristiwa & harga_kosong & tanpa_volume
        if buang.any():
            daftar = ", ".join(d.strftime("%Y-%m-%d") for d in bar.index[buang][:5])
            catatan.append(f"baris peristiwa tanpa harga dari yfinance dibuang: {daftar}")
            bar = bar[~buang]

        akhir_pekan = bar.index[bar.index.dayofweek >= 5]
        if len(akhir_pekan):
            contoh = ", ".join(d.strftime("%Y-%m-%d") for d in akhir_pekan[:3])
            return _gagal("FORMAT", f"{PESAN_ZONA} (bar bertanggal akhir pekan: {contoh})")

        split = split_kosong()
        if _KOLOM_SPLIT in df.columns:
            asli = df[_KOLOM_SPLIT]
            rasio = pd.to_numeric(asli, errors="coerce")
            if (rasio.isna() & asli.notna()).any():
                return _gagal("FORMAT", "tipe kolom Stock Splits salah (bukan angka)")
            r = rasio.to_numpy(dtype="float64", na_value=np.nan)
            ada = ~np.isnan(r) & (r != 0)  # 0/NaN = tidak ada split pada bar itu
            if (~np.isfinite(r[ada]) | (r[ada] < 0)).any():
                return _gagal("FORMAT", "rasio split tidak valid (negatif atau tak hingga)")
            ada &= r != 1.0
            split = pd.Series(r[ada], index=pd.DatetimeIndex(tanggal[ada], name=NAMA_INDEX),
                              name="split", dtype="float64")
            if split.index.has_duplicates:
                return _gagal("FORMAT", "tanggal split ganda dalam satu respons")
            split = split.sort_index()

        awal_ts, akhir_ts = ke_ts(mulai), ke_ts(akhir)
        dalam = (bar.index >= awal_ts) & (bar.index <= akhir_ts)
        if not dalam.all():
            catatan.append(f"{int((~dalam).sum())} bar di luar rentang {mulai}..{akhir} dibuang")
            bar = bar[dalam]
        split = split[(split.index >= awal_ts) & (split.index <= akhir_ts)]
        if bar.empty:
            return _gagal("TIDAK_ADA_DATA", f"tidak ada bar harga untuk {mulai}..{akhir}")

        bar = bar.sort_index(kind="mergesort")  # duplikat dibiarkan: dinilai validasi_struktur (§8.5)
        return HasilAmbil(
            kode=kode, sumber=self.nama, status="OK", jenis_error=None, pesan="", bar=bar,
            split=split, versi_provider=versi, catatan=catatan,
        )

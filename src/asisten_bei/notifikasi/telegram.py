"""Notifikasi Telegram (SPEC §18.3, §19.4).

- Rahasia: `TELEGRAM_BOT_TOKEN` dan `TELEGRAM_CHAT_ID` dari `.env` (`operasional.muat_env`, yang juga mendaftarkan
  token ke penyaring rahasia). Semua teks error/log melewati `operasional.PENYARING`: exception `requests` yang
  memuat URL API ber-token tidak pernah membocorkan token.
- Isi: parse mode HTML; semua teks dinamis di-escape (`e`), hanya tag `<b>`, `<i>`, `<code>`, `<pre>`.
- Pengiriman: pesan dipecah per baris setelah dirakit (`pecah`). Setiap bagian tag-seimbang, diakhiri baris
  "BUKAN SARAN INVESTASI.", dan ≤ 4.000 karakter termasuk baris itu. Baris yang sendirian terlalu panjang
  dipotong sebagai teks polos (tanpa memotong entitas HTML).
- Balasan 400 (parse) → kirim ulang sebagai teks polos (tag dibuang, entitas di-unescape). Balasan 429 → tunggu
  `retry_after` (lewat `waktu.tidur`), lalu coba sekali lagi.
- Lampiran `laporan.html` (`sendDocument`) bila ukurannya ≤ `maks_lampiran_mb`, dengan caption "Laporan <t> —
  BUKAN SARAN INVESTASI."; bila lebih besar, diganti pesan pemberitahuan.

HTTP lewat `HTTP` (default `requests.post`); uji menggantinya dengan tiruan, tanpa jaringan.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import DISCLAIMER_SINGKAT, waktu
from ..operasional import ENV_CHAT_ID, ENV_TOKEN, PENYARING, muat_env

log = logging.getLogger("asisten_bei.telegram")

API = "https://api.telegram.org"
MAKS_PESAN = 4000
BARIS_DISCLAIMER = DISCLAIMER_SINGKAT  # "BUKAN SARAN INVESTASI."
TAG_DIIZINKAN = frozenset({"b", "i", "code", "pre"})
BATAS_WAKTU_DETIK = 30
_POLA_TAG = re.compile(r"<(/?)([a-zA-Z]+)>")


class GagalKirim(Exception):
    """Pengiriman Telegram gagal. Pesan sudah disensor."""


def e(teks: Any) -> str:
    """Escape teks dinamis untuk parse mode HTML (`<`, `>`, `&`)."""
    return html.escape(str(teks), quote=False)


def teks_polos(isi: str) -> str:
    """Buang tag, unescape entitas (fallback setelah balasan 400)."""
    return html.unescape(_POLA_TAG.sub("", isi))


def seimbang(isi: str) -> bool:
    """Tag hanya dari `TAG_DIIZINKAN` dan seimbang (berpasangan, bersarang benar)."""
    tumpuk: list[str] = []
    for m in _POLA_TAG.finditer(isi):
        tutup, nama = m.group(1) == "/", m.group(2).lower()
        if nama not in TAG_DIIZINKAN:
            return False
        if not tutup:
            tumpuk.append(nama)
        elif not tumpuk or tumpuk.pop() != nama:
            return False
    return not tumpuk


def _potong_polos(baris: str, maks: int) -> list[str]:
    """Baris terlalu panjang → potongan ≤ maks sebagai teks polos ter-escape, tanpa memotong entitas."""
    sisa = e(teks_polos(baris))
    hasil = []
    while len(sisa) > maks:
        potong = maks
        amp = sisa.rfind("&", 0, potong)
        if amp != -1 and ";" not in sisa[amp:potong]:
            potong = amp
        hasil.append(sisa[:potong])
        sisa = sisa[potong:]
    if sisa:
        hasil.append(sisa)
    return hasil


def pecah(isi: str, maks: int = MAKS_PESAN) -> list[str]:
    """Pecah pesan per baris menjadi bagian ≤ `maks` karakter, masing-masing tag-seimbang dan diakhiri baris
    disclaimer. Baris yang tidak seimbang dikirim sebagai teks polos."""
    akhir = "\n" + BARIS_DISCLAIMER
    ruang = maks - len(akhir)
    baris: list[str] = []
    for b in isi.rstrip("\n").split("\n"):
        if b.strip() == BARIS_DISCLAIMER:
            continue  # ditambahkan di akhir setiap bagian
        if not seimbang(b):
            b = e(teks_polos(b))
        baris.extend([b] if len(b) <= ruang else _potong_polos(b, ruang))
    bagian: list[str] = []
    kini: list[str] = []
    panjang = 0
    for b in baris:
        tambah = len(b) + (1 if kini else 0)
        if kini and panjang + tambah > ruang:
            bagian.append("\n".join(kini))
            kini, panjang = [], 0
            tambah = len(b)
        kini.append(b)
        panjang += tambah
    if kini or not bagian:
        bagian.append("\n".join(kini))
    return [(x + akhir) if x else BARIS_DISCLAIMER for x in bagian]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _http_requests(url: str, data: dict, files: dict | None, timeout: float) -> tuple[int, dict]:
    import requests

    r = requests.post(url, data=data, files=files, timeout=timeout)
    try:
        isi = r.json()
    except ValueError:
        isi = {}
    return r.status_code, isi if isinstance(isi, dict) else {}


HTTP: Callable[[str, dict, dict | None, float], tuple[int, dict]] = _http_requests


@dataclass
class HasilKirim:
    bagian: int = 0
    lampiran: bool = False
    teks_polos: int = 0  # bagian yang dikirim ulang sebagai teks polos (400)
    galat: list[str] = field(default_factory=list)

    @property
    def berhasil(self) -> bool:
        return not self.galat


class Telegram:
    """Klien Bot API minimal. Token tidak pernah masuk pesan error/log (disensor)."""

    def __init__(self, token: str, chat_id: str, *, tidur: Callable[[float], None] | None = None):
        self.token = str(token).strip()
        self.chat_id = str(chat_id).strip()
        PENYARING.tambah(self.token)
        self._tidur = tidur or waktu.tidur

    def _post(self, metode: str, data: dict, files: dict | None = None) -> dict:
        url = f"{API}/bot{self.token}/{metode}"
        for percobaan in range(2):
            try:
                status, isi = HTTP(url, data, files, BATAS_WAKTU_DETIK)
            except Exception as ex:  # jaringan, timeout, dsb.: pesan bisa memuat URL ber-token
                raise GagalKirim(PENYARING.sensor(f"{metode}: {type(ex).__name__}: {ex}")) from None
            if status == 429 and percobaan == 0:
                tunggu = float(((isi.get("parameters") or {}).get("retry_after")) or 1)
                log.warning("Telegram %s: 429, menunggu %.0f s lalu coba sekali lagi", metode, tunggu)
                self._tidur(tunggu)
                continue
            if status == 200 and isi.get("ok"):
                return isi
            raise _Balasan(status, PENYARING.sensor(str(isi.get("description") or f"HTTP {status}")))
        raise AssertionError("tidak tercapai")  # pragma: no cover

    def kirim_pesan(self, isi_html: str, hasil: HasilKirim | None = None) -> HasilKirim:
        hasil = hasil or HasilKirim()
        for bagian in pecah(PENYARING.sensor(isi_html)):
            try:
                self._kirim_bagian(bagian, hasil)
                hasil.bagian += 1
            except GagalKirim as ex:
                hasil.galat.append(str(ex))
                log.error("Telegram gagal: %s", ex)
        return hasil

    def _kirim_bagian(self, bagian: str, hasil: HasilKirim) -> None:
        data = {"chat_id": self.chat_id, "text": bagian, "parse_mode": "HTML", "disable_web_page_preview": "true"}
        try:
            self._post("sendMessage", data)
        except _Balasan as ex:
            if ex.status != 400:
                raise GagalKirim(f"sendMessage: {ex}") from None
            log.warning("Telegram 400 (%s): kirim ulang sebagai teks polos", ex)
            polos = {"chat_id": self.chat_id, "text": teks_polos(bagian), "disable_web_page_preview": "true"}
            try:
                self._post("sendMessage", polos)
            except _Balasan as ex2:
                raise GagalKirim(f"sendMessage (teks polos): {ex2}") from None
            hasil.teks_polos += 1

    def kirim_dokumen(self, path: Path, caption: str, hasil: HasilKirim | None = None) -> HasilKirim:
        hasil = hasil or HasilKirim()
        caption = PENYARING.sensor(caption)
        if BARIS_DISCLAIMER not in caption:
            caption = f"{caption} {BARIS_DISCLAIMER}"
        try:
            with open(path, "rb") as f:
                self._post("sendDocument", {"chat_id": self.chat_id, "caption": caption},
                           files={"document": (Path(path).name, f, "text/html")})
            hasil.lampiran = True
        except (_Balasan, GagalKirim, OSError) as ex:
            teks = PENYARING.sensor(f"sendDocument: {ex}")
            hasil.galat.append(teks)
            log.error("Telegram gagal: %s", teks)
        return hasil

    def chat_dari_update(self) -> list[tuple[str, str]]:
        """(chat_id, nama) unik dari `getUpdates` (§19.4 langkah 3)."""
        try:
            isi = self._post("getUpdates", {})
        except _Balasan as ex:
            raise GagalKirim(f"getUpdates: {ex}") from None
        hasil: dict[str, str] = {}
        for u in isi.get("result") or []:
            pesan = u.get("message") or u.get("channel_post") or u.get("edited_message") or {}
            chat = pesan.get("chat") or {}
            if "id" in chat:
                nama = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x)
                nama = nama or chat.get("username") or ""
                hasil.setdefault(str(chat["id"]), str(nama))
        return sorted(hasil.items())


class _Balasan(Exception):
    def __init__(self, status: int, deskripsi: str):
        self.status = status
        super().__init__(f"HTTP {status}: {deskripsi}")


# ---------------------------------------------------------------------------
# Kredensial & pengiriman tingkat tinggi
# ---------------------------------------------------------------------------


class TelegramBelumDisiapkan(GagalKirim):
    """`.env` tidak ada, atau token/chat ID kosong atau masih nilai contoh."""


def dari_env(akar: Path, *, perlu_chat: bool = True) -> Telegram:
    if not (Path(akar) / ".env").is_file():
        raise TelegramBelumDisiapkan("Telegram belum disiapkan: berkas .env tidak ada (salin .env.example menjadi .env; "
                                     "contoh isian: contoh/env.contoh)")
    env = muat_env(akar)
    token, chat = env.get(ENV_TOKEN, ""), env.get(ENV_CHAT_ID, "")
    kosong = [k for k, v in ((ENV_TOKEN, token), (ENV_CHAT_ID, chat)) if not v and (perlu_chat or k == ENV_TOKEN)]
    if kosong:
        raise TelegramBelumDisiapkan(f"Telegram belum disiapkan: {', '.join(kosong)} di .env masih kosong")
    if "CONTOH" in token.upper():
        raise TelegramBelumDisiapkan(f"Telegram belum disiapkan: {ENV_TOKEN} di .env masih nilai contoh")
    return Telegram(token, chat)


def kirim(
    akar: Path,
    isi_html: str,
    *,
    lampiran: Path | None = None,
    caption: str = "",
    maks_lampiran_mb: float | None = None,
) -> HasilKirim:
    """Kirim pesan (dan lampiran opsional). Kegagalan apa pun → `HasilKirim.galat` (tidak melempar)."""
    try:
        tg = dari_env(akar)
    except GagalKirim as ex:
        log.error("%s", ex)
        return HasilKirim(galat=[str(ex)])
    hasil = tg.kirim_pesan(isi_html)
    if lampiran is not None:
        ukuran = Path(lampiran).stat().st_size / (1024 * 1024) if Path(lampiran).exists() else None
        if ukuran is None:
            hasil.galat.append(f"lampiran {Path(lampiran).name} tidak ada")
        elif maks_lampiran_mb is not None and ukuran > float(maks_lampiran_mb):
            tg.kirim_pesan(
                f"{e(Path(lampiran).name)} ({ukuran:.1f} MB) lebih besar dari batas lampiran "
                f"{float(maks_lampiran_mb):g} MB; buka berkasnya di folder output/laporan.", hasil)
        else:
            tg.kirim_dokumen(Path(lampiran), caption, hasil)
    return hasil


def baris_butir(butir: Iterable[str], maks: int | None = None) -> list[str]:
    daftar = list(butir)
    hasil = [f"• {e(x)}" for x in (daftar if maks is None else daftar[:maks])]
    if maks is not None and len(daftar) > maks:
        hasil.append(f"• … {len(daftar) - maks} lainnya (lihat laporan)")
    return hasil


__all__ = [
    "API", "BARIS_DISCLAIMER", "GagalKirim", "HTTP", "HasilKirim", "MAKS_PESAN", "Telegram", "TelegramBelumDisiapkan",
    "baris_butir", "dari_env", "e", "kirim", "pecah", "seimbang", "teks_polos",
]


# ---------------------------------------------------------------------------
# Perintah uji-telegram (§18.3, §19.4)
# ---------------------------------------------------------------------------


def jalankan_uji_telegram(ctx, *, cari_chat_id: bool = False, keluar: Callable[[str], None] = print) -> int:
    """`uji-telegram`: kirim pesan uji (kode 0; gagal kirim → 4). `--cari-chat-id`: cetak chat ID dari
    `getUpdates`. `.env` belum disiapkan → kode 3 (input tidak valid), dengan petunjuk §19.4."""
    from .. import DISCLAIMER
    from ..operasional import KODE_INPUT, KODE_NOTIFIKASI, KODE_OK

    if not ctx.cfg.notifikasi.telegram.aktif and not cari_chat_id:
        keluar("Catatan: notifikasi.telegram.aktif = false (run harian tidak mengirim); pesan uji tetap dikirim.")
    try:
        tg = dari_env(ctx.akar, perlu_chat=not cari_chat_id)
    except TelegramBelumDisiapkan as ex:
        keluar(f"ERROR: {ex}.")
        keluar("Langkah: buat bot lewat @BotFather, salin .env.example menjadi .env (bukan .env.txt), isi token,")
        keluar("kirim pesan apa pun ke bot Anda, lalu jalankan uji-telegram --cari-chat-id (SPEC §19.4).")
        keluar(DISCLAIMER)
        return KODE_INPUT
    if cari_chat_id:
        try:
            chat = tg.chat_dari_update()
        except GagalKirim as ex:
            keluar(f"ERROR Telegram: {ex}")
            keluar(DISCLAIMER)
            return KODE_NOTIFIKASI
        if not chat:
            keluar("Belum ada pesan untuk bot ini. Kirim pesan apa pun ke bot Anda di aplikasi Telegram, lalu ulangi.")
        for cid, nama in chat:
            keluar(f"Chat ID: {cid}" + (f" ({nama})" if nama else "") + f" — tulis di .env sebagai {ENV_CHAT_ID}={cid}")
        keluar(DISCLAIMER)
        return KODE_OK
    saat = waktu.sekarang()
    hasil = tg.kirim_pesan(
        f"<b>Uji Telegram asisten-bei</b>\nPesan uji dikirim {e(saat.strftime('%Y-%m-%d %H:%M:%S'))} WIB. "
        "Jika pesan ini sampai, notifikasi harian siap.\n" + BARIS_DISCLAIMER)
    if hasil.berhasil:
        keluar(f"Pesan uji terkirim ({hasil.bagian} bagian). Periksa aplikasi Telegram Anda.")
        keluar(DISCLAIMER)
        return KODE_OK
    for g in hasil.galat:
        keluar(f"ERROR Telegram: {g}")
    keluar(DISCLAIMER)
    return KODE_NOTIFIKASI


def kirim_error(akar: Path, judul: str, rincian: str | list[str]) -> HasilKirim:
    """Pesan error (§18.3: kode 1, 2, 3, 5, 6 dalam mode --jadwal). Tidak pernah melempar."""
    from .pesan import pesan_error

    try:
        return kirim(akar, pesan_error(judul, rincian))
    except Exception as ex:  # pragma: no cover - jaring terakhir
        teks = PENYARING.sensor(f"{type(ex).__name__}: {ex}")
        log.error("Telegram gagal: %s", teks)
        return HasilKirim(galat=[teks])

"""T-TG: Telegram (SPEC §18.3, §19.4) dan T-LP bagian Telegram. Tanpa jaringan: `telegram.HTTP` diganti server
tiruan yang memvalidasi HTML seperti Bot API (hanya tag b/i/code/pre, seimbang, entitas sah). Token & chat ID
FIKTIF – KHUSUS UJI.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from asisten_bei import operasional, waktu
from asisten_bei.notifikasi import telegram as tg
from asisten_bei.notifikasi.pesan import pesan_error
from asisten_bei.operasional import PenyaringRahasia

TOKEN = "123456789:FIKTIF_tokenUjiTelegram_abcdEFGH"
EKOR = "FIKTIF_tokenUjiTelegram_abcdEFGH"
CHAT = "987654321"
DISCLAIMER = "BUKAN SARAN INVESTASI"

_TAG = re.compile(r"<(/?)([a-zA-Z]+)>")
_ENTITAS = re.compile(r"&(lt|gt|amp|quot|#\d+|#x[0-9a-fA-F]+);")


def html_sah(teks: str) -> bool:
    """Validasi gaya Bot API: tag hanya b/i/code/pre & seimbang; `<` lepas dan `&` lepas ditolak."""
    sisa = _TAG.sub("", teks)
    if "<" in sisa or ">" in sisa:
        return False
    if "&" in _ENTITAS.sub("", teks):
        return False
    return tg.seimbang(teks)


@dataclass
class Panggilan:
    metode: str
    data: dict
    files: dict | None


@dataclass
class ServerPalsu:
    """Tiruan Bot API. `skrip`: daftar balasan khusus (status, json) yang dipakai lebih dulu, berurutan."""

    skrip: list = field(default_factory=list)
    panggilan: list[Panggilan] = field(default_factory=list)
    gagal: Exception | None = None
    update: list = field(default_factory=list)

    def __call__(self, url, data, files, timeout):
        assert url.startswith(tg.API + "/bot" + TOKEN + "/")
        metode = url.rsplit("/", 1)[1]
        self.panggilan.append(Panggilan(metode, dict(data), files))
        if self.gagal is not None:
            raise self.gagal
        if self.skrip:
            return self.skrip.pop(0)
        if metode == "getUpdates":
            return 200, {"ok": True, "result": self.update}
        if metode == "sendMessage":
            teks = data["text"]
            if len(teks) > 4096:
                return 400, {"ok": False, "description": "Bad Request: message is too long"}
            if data.get("parse_mode") == "HTML" and not html_sah(teks):
                return 400, {"ok": False, "description": "Bad Request: can't parse entities"}
        return 200, {"ok": True, "result": {}}

    def teks(self, metode="sendMessage") -> list[str]:
        return [p.data.get("text", p.data.get("caption", "")) for p in self.panggilan if p.metode == metode]


@pytest.fixture
def server(monkeypatch):
    s = ServerPalsu()
    monkeypatch.setattr(tg, "HTTP", s)
    monkeypatch.setattr(operasional, "PENYARING", PenyaringRahasia())
    monkeypatch.setattr(tg, "PENYARING", operasional.PENYARING)
    return s


@pytest.fixture
def akar_env(tmp_path):
    (tmp_path / ".env").write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID={CHAT}\n", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Pemecahan & escape
# ---------------------------------------------------------------------------


def test_alasan_dengan_karakter_khusus_terkirim_sebagai_html_sah(server):
    t = tg.Telegram(TOKEN, CHAT)
    pesan = pesan_error("uji", ["alasan: close 1.000 < SMA20 & SMA50 > SMA200 <b>bukan tag</b>"])
    h = t.kirim_pesan(pesan)
    assert h.berhasil and h.teks_polos == 0
    (teks,) = server.teks()
    assert html_sah(teks) and "&lt;" in teks and "&amp;" in teks and "&gt;" in teks
    assert teks.rstrip().endswith(DISCLAIMER + ".")


def test_pecah_tag_seimbang_maks_4000_diakhiri_disclaimer():
    baris = [f"<b>Baris {i}</b> isi {tg.e('a < b & c')} " + "x" * 90 for i in range(200)]
    bagian = tg.pecah("\n".join(baris))
    assert len(bagian) > 1
    for b in bagian:
        assert len(b) <= tg.MAKS_PESAN and tg.seimbang(b) and html_sah(b)
        assert b.split("\n")[-1] == "BUKAN SARAN INVESTASI."
    isi = [x for b in bagian for x in b.split("\n")[:-1]]
    assert isi == baris  # tidak ada baris hilang atau terpotong


def test_baris_sangat_panjang_dipotong_tanpa_memutus_entitas():
    panjang = "<b>" + tg.e("R&D <x> " * 1500) + "</b>"
    bagian = tg.pecah(panjang)
    assert len(bagian) >= 3
    for b in bagian:
        assert len(b) <= tg.MAKS_PESAN and html_sah(b) and b.endswith("BUKAN SARAN INVESTASI.")


def test_baris_tidak_seimbang_dikirim_sebagai_teks_polos():
    (b,) = tg.pecah("<b>terbuka tanpa penutup")
    assert html_sah(b) and "<b>" not in b


# ---------------------------------------------------------------------------
# Balasan 400 / 429, lampiran, token
# ---------------------------------------------------------------------------


def test_400_kirim_ulang_sebagai_teks_polos(server):
    server.skrip = [(400, {"ok": False, "description": "Bad Request: can't parse entities"})]
    h = tg.Telegram(TOKEN, CHAT).kirim_pesan("<b>Status</b> &amp; aksi")
    assert h.berhasil and h.teks_polos == 1
    pertama, kedua = server.panggilan
    assert pertama.data["parse_mode"] == "HTML" and "parse_mode" not in kedua.data
    assert kedua.data["text"] == "Status & aksi\nBUKAN SARAN INVESTASI."


def test_429_tunggu_retry_after_lalu_coba_sekali_lagi(server):
    server.skrip = [(429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 7}})]
    awal = waktu.sekarang()
    h = tg.Telegram(TOKEN, CHAT).kirim_pesan("halo")
    assert h.berhasil and len(server.panggilan) == 2
    assert (waktu.sekarang() - awal).total_seconds() == 7


def test_429_dua_kali_gagal(server):
    batas = (429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 1}})
    server.skrip = [batas, batas]
    h = tg.Telegram(TOKEN, CHAT).kirim_pesan("halo")
    assert not h.berhasil and "429" in h.galat[0]


def test_lampiran_dengan_caption_disclaimer(server, akar_env):
    lap = akar_env / "laporan.html"
    lap.write_text("<html>BUKAN SARAN INVESTASI</html>", encoding="utf-8")
    h = tg.kirim(akar_env, "pesan", lampiran=lap, caption="Laporan 2025-09-10 — BUKAN SARAN INVESTASI.",
                 maks_lampiran_mb=45)
    assert h.berhasil and h.lampiran
    (dok,) = [p for p in server.panggilan if p.metode == "sendDocument"]
    assert dok.data["caption"] == "Laporan 2025-09-10 — BUKAN SARAN INVESTASI."
    assert dok.files["document"][0] == "laporan.html"


def test_lampiran_terlalu_besar_diganti_pemberitahuan(server, akar_env):
    lap = akar_env / "laporan.html"
    lap.write_bytes(b"x" * 300_000)
    h = tg.kirim(akar_env, "pesan", lampiran=lap, caption="Laporan", maks_lampiran_mb=0.1)
    assert h.berhasil and not h.lampiran
    assert not [p for p in server.panggilan if p.metode == "sendDocument"]
    pemberitahuan = server.teks()[-1]
    assert "lebih besar dari batas lampiran" in pemberitahuan and pemberitahuan.endswith(DISCLAIMER + ".")


def test_exception_dengan_url_bertoken_tidak_membocorkan_token(server, caplog):
    server.gagal = ConnectionError(
        f"HTTPSConnectionPool(host='api.telegram.org'): Max retries exceeded with url: /bot{TOKEN}/sendMessage")
    caplog.set_level(logging.DEBUG)
    h = tg.Telegram(TOKEN, CHAT).kirim_pesan("halo")
    assert not h.berhasil
    semua = " ".join(h.galat) + caplog.text
    assert TOKEN not in semua and EKOR not in semua and "***" in semua


def test_env_belum_disiapkan(server, tmp_path):
    h = tg.kirim(tmp_path, "halo")
    assert not h.berhasil and ".env tidak ada" in h.galat[0] and not server.panggilan
    for isi in ("", "TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\n"):  # berkas kosong / salinan .env.example
        (tmp_path / ".env").write_text(isi, encoding="utf-8")
        h = tg.kirim(tmp_path, "halo")
        assert not h.berhasil and "masih kosong" in h.galat[0] and not server.panggilan
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=123:CONTOH_FIKTIF\nTELEGRAM_CHAT_ID=1\n", encoding="utf-8")
    h = tg.kirim(tmp_path, "halo")
    assert not h.berhasil and "nilai contoh" in h.galat[0] and not server.panggilan


def test_setiap_bagian_dan_caption_memuat_disclaimer(server, akar_env):
    lap = akar_env / "laporan.html"
    lap.write_text("isi", encoding="utf-8")
    tg.kirim(akar_env, "\n".join(f"baris {i} " + "y" * 200 for i in range(60)), lampiran=lap, caption="Laporan X",
             maks_lampiran_mb=45)
    teks = server.teks() + server.teks("sendDocument")
    assert len(teks) >= 4
    assert all(DISCLAIMER in t for t in teks)


def test_chat_dari_update(server):
    server.update = [
        {"update_id": 1, "message": {"chat": {"id": 987654321, "first_name": "Pengguna", "last_name": "Fiktif"}}},
        {"update_id": 2, "message": {"chat": {"id": 987654321, "first_name": "Pengguna"}}},
        {"update_id": 3, "channel_post": {"chat": {"id": -100123, "title": "Kanal Fiktif"}}},
    ]
    assert tg.Telegram(TOKEN, CHAT).chat_dari_update() == [("-100123", "Kanal Fiktif"),
                                                           ("987654321", "Pengguna Fiktif")]


def test_html_sah_tiruan_menolak_html_rusak():
    assert not html_sah("a < b") and not html_sah("R&D") and not html_sah("<u>x</u>") and not html_sah("<b>x")
    assert html_sah("<b>x</b> &lt; &amp;") and html.unescape("&lt;") == "<"


# ---------------------------------------------------------------------------
# Integrasi: harian, uji-telegram, pesan error --jadwal (proyek sintetis bantu_harian)
# ---------------------------------------------------------------------------


@pytest.fixture
def proyek_tg(proyek, server):
    proyek.ubah_config({"notifikasi.telegram.aktif": True, "laporan.format": ["terminal", "markdown", "html"]})
    (proyek.akar / ".env").write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID={CHAT}\n", encoding="utf-8")
    return proyek


def test_harian_mengirim_pesan_dan_lampiran(proyek_tg, server):
    h = proyek_tg.harian("2025-06-30", notifikasi=True)
    assert h.kode == 0, h.teks
    (pesan,) = server.teks()
    assert "<b>Status pasar: BOLEH BELI</b>" in pesan and "BELI AAAA" in pesan
    assert "Tarik order beli yang tidak terisi saat pembukaan" in pesan
    assert pesan.endswith("BUKAN SARAN INVESTASI.") and html_sah(pesan)
    (cap,) = server.teks("sendDocument")
    assert cap == "Laporan 2025-06-30 — BUKAN SARAN INVESTASI."
    assert "Telegram: 1 pesan terkirim, laporan.html terlampir." in h.teks
    assert TOKEN not in h.teks


def test_harian_notifikasi_gagal_kode_4_dan_tercatat_di_laporan(proyek_tg, server):
    server.gagal = ConnectionError(f"gagal ke https://api.telegram.org/bot{TOKEN}/sendMessage")
    h = proyek_tg.harian("2025-06-30", notifikasi=True)
    assert h.kode == 4
    md = (proyek_tg.folder_laporan / "2025-06-30" / "laporan.md").read_text(encoding="utf-8")
    assert "Notifikasi Telegram gagal" in md and TOKEN not in md and EKOR not in md
    assert TOKEN not in h.teks


def test_notifikasi_gagal_dan_laporan_terkunci_saat_ditulis_ulang_tetap_kode_4(proyek_tg, server, monkeypatch):
    import asisten_bei.laporan as laporan_mod

    server.gagal = ConnectionError("jaringan putus")
    terkunci = proyek_tg.folder_laporan / "2025-06-30" / "laporan.md"
    asli = laporan_mod.tulis_atomik

    def tulis(path, isi, encoding="utf-8"):  # hanya penulisan ulang (salinan terbaru ditulis lewat tulis_keluaran)
        if Path(path) == terkunci:
            raise PermissionError("terkunci")
        return asli(path, isi, encoding)

    monkeypatch.setattr(laporan_mod, "tulis_atomik", tulis)
    h = proyek_tg.harian("2025-06-30", notifikasi=True)
    assert h.kode == 4, h.teks
    assert "PERINGATAN: laporan.md tidak bisa ditulis ulang" in h.teks
    (run,) = [p for p in terkunci.parent.iterdir() if p.is_dir()]
    assert "Notifikasi Telegram gagal" in (run / "laporan.md").read_text(encoding="utf-8")
    assert "Notifikasi Telegram gagal" not in terkunci.read_text(encoding="utf-8")


def test_kode_data_mengalahkan_kode_notifikasi(proyek_tg, server):
    server.gagal = ConnectionError("jaringan putus")
    for k in ("IHSG", "AAAA", "BBBB", "CCCC", "DDDD"):
        proyek_tg.hapus_bar(k, "2025-06-30")
    assert proyek_tg.harian("2025-06-30", notifikasi=True).kode == 2  # 2 > 4


@pytest.mark.parametrize("opsi, aktif", [(("--tanpa-notifikasi",), True), ((), False)])
def test_tanpa_notifikasi_atau_nonaktif_tidak_mengirim(proyek_tg, server, opsi, aktif):
    proyek_tg.ubah_config({"notifikasi.telegram.aktif": aktif})
    server.gagal = ConnectionError("tidak boleh dipanggil")
    assert proyek_tg.harian("2025-06-30", *opsi, notifikasi=True).kode == 0
    assert not server.panggilan


@pytest.mark.parametrize("kirim", [False, True])
def test_jadwal_hari_libur_t_sudah_lengkap_pesan_hanya_bila_kirim_pesan_saat_libur(proyek_tg, server, kirim):
    proyek_tg.ubah_config({"jadwal.kirim_pesan_saat_libur": kirim})
    assert proyek_tg.harian("2025-06-27", notifikasi=True).kode == 0  # Jumat
    server.panggilan.clear()
    h = proyek_tg.harian("2025-06-28T19:45:00+07:00", "--jadwal", notifikasi=True)  # Sabtu → t = Jumat, sudah LENGKAP
    assert h.kode == 0 and "Tidak ada data baru" in h.teks
    if not kirim:
        assert not server.panggilan
        return
    (pesan,) = server.teks()
    assert "tidak ada data baru" in pesan and pesan.endswith(DISCLAIMER + ".")


def test_jadwal_input_tidak_valid_kode_3_mengirim_pesan_error(proyek_tg, server):
    proyek_tg.tulis_input("universe.csv", "kode,nama,sektor\naaaa,salah,X\n")
    h = proyek_tg.harian("2025-06-30", "--jadwal", notifikasi=True)
    assert h.kode == 3
    (pesan,) = server.teks()
    assert "kode keluar 3" in pesan and "input tidak valid" in pesan and pesan.endswith(DISCLAIMER + ".")


def test_jadwal_kunci_dipegang_kode_5_mengirim_pesan_error(proyek_tg, server):
    from asisten_bei.operasional import kunci_proses

    with kunci_proses(proyek_tg.akar, "unduh"):
        h = proyek_tg.harian("2025-06-30", "--jadwal", notifikasi=True)
    assert h.kode == 5
    (pesan,) = server.teks()
    assert "kode keluar 5" in pesan and "proses lain sedang berjalan" in pesan


def test_tanpa_jadwal_tidak_mengirim_pesan_error(proyek_tg, server):
    proyek_tg.tulis_input("universe.csv", "kode,nama,sektor\naaaa,salah,X\n")
    assert proyek_tg.harian("2025-06-30", notifikasi=True).kode == 3
    assert not server.panggilan


def test_uji_telegram_berhasil_gagal_dan_belum_disiapkan(proyek_tg, server):
    h = proyek_tg.cli("2025-06-30T20:00:00+07:00", "uji-telegram")
    assert h.kode == 0 and "Pesan uji terkirim" in h.teks
    (pesan,) = server.teks()
    assert "Uji Telegram asisten-bei" in pesan and pesan.endswith(DISCLAIMER + ".")
    server.gagal = ConnectionError("putus")
    assert proyek_tg.cli("2025-06-30T20:00:00+07:00", "uji-telegram").kode == 4
    (proyek_tg.akar / ".env").unlink()
    h = proyek_tg.cli("2025-06-30T20:00:00+07:00", "uji-telegram")
    assert h.kode == 3 and "--cari-chat-id" in h.teks and DISCLAIMER in h.teks


def test_uji_telegram_cari_chat_id(proyek_tg, server):
    (proyek_tg.akar / ".env").write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=\n", encoding="utf-8")
    server.update = [{"update_id": 1, "message": {"chat": {"id": 555000111, "first_name": "Fiktif"}}}]
    h = proyek_tg.cli("2025-06-30T20:00:00+07:00", "uji-telegram", "--cari-chat-id")
    assert h.kode == 0 and "Chat ID: 555000111 (Fiktif)" in h.teks and "TELEGRAM_CHAT_ID=555000111" in h.teks
    assert TOKEN not in h.teks

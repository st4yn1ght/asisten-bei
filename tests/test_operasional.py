"""Uji T-OP (SPEC §22.1): kunci proses, kode keluar, .env, penyaring rahasia, log, tulis atomik.

Uji lintas proses memakai subprocess Python kecil (interpreter yang sama dengan pytest,
PYTHONPATH = src). Waktu virtual diteruskan lewat env `ASISTEN_BEI_WAKTU_SEKARANG`.
"""

from __future__ import annotations

import io
import itertools
import json
import logging
import os
import queue
import subprocess
import sys
import textwrap
import threading
import urllib.parse
from pathlib import Path

import pandas as pd
import pytest
import yaml

from asisten_bei import DISCLAIMER_CSV, operasional, waktu
from asisten_bei.config import KonteksApp, validasi_dict
from asisten_bei.operasional import (
    KunciDipegang,
    PenyaringRahasia,
    URUTAN_PRIORITAS_KODE,
    gabung_kode_keluar,
    kunci_proses,
)

AKAR_PROYEK = Path(__file__).resolve().parents[1]
SRC = AKAR_PROYEK / "src"
WAKTU_UJI = "2026-10-15T20:00:00+07:00"
# Token palsu berbentuk token bot Telegram (angka:huruf); memuat karakter yang di-URL-encode.
TOKEN_PALSU = "987654321:AAFakeTokenKhususUji-xyz_ABC"
BATAS_TUNGGU_DETIK = 60


# ---------------------------------------------------------------------------
# Pembantu subprocess
# ---------------------------------------------------------------------------


def _env_anak(**tambahan: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env[waktu.ENV_WAKTU] = WAKTU_UJI
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(tambahan)
    return env


def _jalankan(kode: str, cwd: Path, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(kode)],
        cwd=cwd, env=_env_anak(**env), capture_output=True, text=True, encoding="utf-8",
        timeout=BATAS_TUNGGU_DETIK,
    )


class _ProsesPemegang:
    """Subprocess yang memegang kunci lalu menunggu tanpa batas sampai dimatikan."""

    def __init__(self, akar: Path, perintah: str):
        kode = f"""
            import sys, time
            from pathlib import Path
            from asisten_bei import operasional
            with operasional.kunci_proses(Path({str(akar)!r}), {perintah!r}):
                print("SIAP", flush=True)
                while True:
                    time.sleep(0.2)
        """
        self.proc = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(kode)],
            env=_env_anak(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8",
        )
        self._baris: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._baca, daemon=True).start()

    def _baca(self) -> None:
        for baris in self.proc.stdout:
            self._baris.put(baris)
        self._baris.put("")  # EOF

    def tunggu_siap(self) -> None:
        try:
            baris = self._baris.get(timeout=BATAS_TUNGGU_DETIK)
        except queue.Empty:
            pytest.fail("proses pemegang kunci tidak siap dalam batas waktu")
        if baris.strip() != "SIAP":
            self.proc.kill()
            pytest.fail(f"proses pemegang kunci gagal: {baris!r} {self.proc.stderr.read()}")

    def bunuh(self) -> None:
        self.proc.kill()  # POSIX: SIGKILL (tanpa kesempatan membersihkan .kunci.info)
        self.proc.wait(timeout=BATAS_TUNGGU_DETIK)

    def tutup(self) -> None:
        if self.proc.poll() is None:
            self.bunuh()
        for f in (self.proc.stdout, self.proc.stderr):
            if f:
                f.close()


@pytest.fixture
def pemegang(tmp_path):
    daftar: list[_ProsesPemegang] = []

    def _buat(perintah: str = "harian") -> _ProsesPemegang:
        p = _ProsesPemegang(tmp_path, perintah)
        daftar.append(p)
        p.tunggu_siap()
        return p

    yield _buat
    for p in daftar:
        p.tutup()


# ---------------------------------------------------------------------------
# Kunci proses (§19.3)
# ---------------------------------------------------------------------------


def test_kunci_menulis_info_dan_menghapusnya_saat_lepas(tmp_path):
    info_path = tmp_path / "data" / ".kunci.info"
    assert not (tmp_path / "data").exists()
    with kunci_proses(tmp_path, "unduh") as info:
        assert (tmp_path / "data" / ".kunci").exists()  # folder data dibuat bila perlu
        isi = json.loads(info_path.read_text(encoding="utf-8"))
        assert isi == {"pid": os.getpid(), "perintah": "unduh", "waktu_mulai": WAKTU_UJI}
        assert info == isi
    assert not info_path.exists()
    # Bisa dipegang lagi setelah dilepas.
    with kunci_proses(tmp_path, "backtest"):
        pass


def test_kunci_waktu_mulai_dari_waktu_virtual(tmp_path, atur_waktu):
    atur_waktu("2026-10-16T19:31:05+07:00")
    with kunci_proses(tmp_path, "harian") as info:
        assert info["waktu_mulai"] == "2026-10-16T19:31:05+07:00"


def test_kunci_dilepas_walau_blok_melempar_exception(tmp_path):
    with pytest.raises(RuntimeError):
        with kunci_proses(tmp_path, "harian"):
            raise RuntimeError("gagal di tengah")
    assert not (tmp_path / "data" / ".kunci.info").exists()
    with kunci_proses(tmp_path, "harian"):
        pass


def test_kunci_kedua_di_proses_yang_sama_ditolak(tmp_path):
    with kunci_proses(tmp_path, "jurnal"):
        with pytest.raises(KunciDipegang) as e:
            with kunci_proses(tmp_path, "harian"):
                pass
    assert e.value.pesan == "proses lain sedang berjalan: jurnal sejak 2026-10-15 20:00:00 WIB"


def test_kunci_dipegang_proses_lain_memberi_pesan_dari_info(tmp_path, pemegang):
    p = pemegang("harian --jadwal")
    with pytest.raises(KunciDipegang) as e:
        with kunci_proses(tmp_path, "unduh"):
            pytest.fail("kunci seharusnya tidak didapat")
    assert e.value.pesan == "proses lain sedang berjalan: harian --jadwal sejak 2026-10-15 20:00:00 WIB"
    assert e.value.kode_keluar == 5
    assert e.value.info["pid"] == p.proc.pid
    # Gagal mendapat kunci tidak boleh mengusik info milik pemegang.
    info = json.loads((tmp_path / "data" / ".kunci.info").read_text(encoding="utf-8"))
    assert info["perintah"] == "harian --jadwal"


def test_kunci_lepas_setelah_proses_pemegang_dimatikan(tmp_path, pemegang):
    p = pemegang("backtest")
    with pytest.raises(KunciDipegang):
        with kunci_proses(tmp_path, "harian"):
            pass
    p.bunuh()
    # Info basi tertinggal (proses mati tanpa membersihkan), tetapi kunci OS sudah lepas.
    assert (tmp_path / "data" / ".kunci.info").exists()
    with kunci_proses(tmp_path, "harian") as info:
        isi = json.loads((tmp_path / "data" / ".kunci.info").read_text(encoding="utf-8"))
        assert isi["pid"] == os.getpid() and isi["perintah"] == "harian"  # info basi tertimpa
        assert info == isi


def test_run_kedua_di_proses_lain_keluar_kode_5(tmp_path):
    """Proses uji memegang kunci; proses anak yang mencoba mendapat KunciDipegang → kode 5."""
    kode = f"""
        import sys
        from pathlib import Path
        from asisten_bei import operasional
        try:
            with operasional.kunci_proses(Path({str(tmp_path)!r}), "harian"):
                print("DAPAT")
        except operasional.KunciDipegang as e:
            print(e.pesan)
            sys.exit(e.kode_keluar)
    """
    with kunci_proses(tmp_path, "backtest"):
        hasil = _jalankan(kode, tmp_path)
    assert hasil.returncode == 5, hasil.stderr
    assert hasil.stdout.strip() == "proses lain sedang berjalan: backtest sejak 2026-10-15 20:00:00 WIB"
    # Setelah dilepas, proses anak mendapat kunci.
    hasil2 = _jalankan(kode, tmp_path)
    assert hasil2.returncode == 0, hasil2.stderr
    assert hasil2.stdout.strip() == "DAPAT"


def test_pesan_kunci_info_tidak_terbaca(tmp_path):
    assert operasional.pesan_kunci_dipegang(None) == (
        "proses lain sedang berjalan: (perintah tidak diketahui) sejak (waktu tidak diketahui)"
    )
    with kunci_proses(tmp_path, "harian"):
        (tmp_path / "data" / ".kunci.info").write_text("{rusak", encoding="utf-8")
        with pytest.raises(KunciDipegang) as e:
            with kunci_proses(tmp_path, "unduh"):
                pass
    assert e.value.info is None
    assert "(perintah tidak diketahui)" in e.value.pesan


def test_pesan_kunci_waktu_zona_lain_dikonversi_ke_wib():
    info = {"perintah": "unduh", "waktu_mulai": "2026-10-15T12:00:00+00:00"}
    assert operasional.pesan_kunci_dipegang(info) == (
        "proses lain sedang berjalan: unduh sejak 2026-10-15 19:00:00 WIB"
    )


# ---------------------------------------------------------------------------
# Kode keluar (§20.1)
# ---------------------------------------------------------------------------


def test_urutan_prioritas_sesuai_spec():
    assert URUTAN_PRIORITAS_KODE == (3, 1, 5, 2, 6, 4, 0)


@pytest.mark.parametrize(
    "kode, harapan",
    [
        ([], 0),
        ([0], 0),
        ([0, 0], 0),
        ([4, 0], 4),
        ([4, 6], 6),
        ([6, 2], 2),
        ([2, 5], 5),
        ([5, 1], 1),
        ([1, 3], 3),
        ([0, 4, 6, 2, 5, 1, 3], 3),
        ([2, 4, 6], 2),
        ([4, 2, 6, 5], 5),
    ],
)
def test_gabung_kode_keluar(kode, harapan):
    assert gabung_kode_keluar(kode) == harapan


def test_gabung_kode_keluar_semua_pasangan_dan_generator():
    peringkat = {k: i for i, k in enumerate(URUTAN_PRIORITAS_KODE)}
    for a, b in itertools.product(URUTAN_PRIORITAS_KODE, repeat=2):
        harapan = a if peringkat[a] <= peringkat[b] else b
        assert gabung_kode_keluar([a, b]) == harapan
        assert gabung_kode_keluar(iter([b, a])) == harapan


@pytest.mark.parametrize("buruk", [7, -1, 99])
def test_gabung_kode_keluar_tidak_dikenal(buruk):
    with pytest.raises(ValueError):
        gabung_kode_keluar([0, buruk])


# ---------------------------------------------------------------------------
# .env (§19.2, §19.4)
# ---------------------------------------------------------------------------


@pytest.fixture
def penyaring_baru(monkeypatch):
    """PENYARING modul diganti instans kosong agar uji tidak saling memengaruhi."""
    baru = PenyaringRahasia()
    monkeypatch.setattr(operasional, "PENYARING", baru)
    return baru


def test_env_txt_terdeteksi(tmp_path, penyaring_baru):
    (tmp_path / ".env.txt").write_text(f"TELEGRAM_BOT_TOKEN={TOKEN_PALSU}\n", encoding="utf-8")
    for aktif in (True, False):
        hasil = operasional.cek_env(tmp_path, telegram_aktif=aktif)
        assert ("PERINGATAN", "ganti nama .env.txt menjadi .env (Windows menyembunyikan ekstensi)") in hasil
    # Telegram aktif dan .env tidak ada → peringatan tambahan.
    hasil = operasional.cek_env(tmp_path, telegram_aktif=True)
    assert ("PERINGATAN", "Telegram belum disiapkan (.env tidak ada)") in hasil
    assert len(hasil) == 2


def test_env_tidak_ada_telegram_nonaktif_tanpa_peringatan(tmp_path, penyaring_baru):
    assert operasional.cek_env(tmp_path, telegram_aktif=False) == []


def test_env_tidak_ada_telegram_aktif(tmp_path, penyaring_baru):
    assert operasional.cek_env(tmp_path, telegram_aktif=True) == [
        ("PERINGATAN", "Telegram belum disiapkan (.env tidak ada)")
    ]


def test_env_isi_kosong(tmp_path, penyaring_baru):
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=12345\n", encoding="utf-8")
    hasil = operasional.cek_env(tmp_path, telegram_aktif=True)
    assert len(hasil) == 1
    level, teks = hasil[0]
    assert level == "PERINGATAN" and "TELEGRAM_BOT_TOKEN" in teks and "TELEGRAM_CHAT_ID" not in teks
    # Contoh .env.example yang disalin apa adanya: keduanya kosong.
    (tmp_path / ".env").write_text("# komentar\nTELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\n", encoding="utf-8")
    (_, teks), = operasional.cek_env(tmp_path, telegram_aktif=True)
    assert "TELEGRAM_BOT_TOKEN" in teks and "TELEGRAM_CHAT_ID" in teks
    assert operasional.cek_env(tmp_path, telegram_aktif=False) == []


def test_env_lengkap_dengan_bom_notepad(tmp_path, penyaring_baru):
    isi = f"TELEGRAM_BOT_TOKEN={TOKEN_PALSU}\nTELEGRAM_CHAT_ID=123456789\n"
    (tmp_path / ".env").write_bytes(b"\xef\xbb\xbf" + isi.encode("utf-8"))
    assert operasional.cek_env(tmp_path, telegram_aktif=True) == []
    env = operasional.muat_env(tmp_path)
    assert env["TELEGRAM_BOT_TOKEN"] == TOKEN_PALSU
    assert env["TELEGRAM_CHAT_ID"] == "123456789"


def test_muat_env_mendaftarkan_token_ke_penyaring(tmp_path, penyaring_baru):
    assert operasional.muat_env(tmp_path) == {}
    assert not penyaring_baru.ada()
    # `$` tidak diinterpolasi; spasi di tepi dibuang.
    token = "111222333:AA$HOME-rahasia"
    (tmp_path / ".env").write_text(f"TELEGRAM_BOT_TOKEN={token}  \nTELEGRAM_CHAT_ID=42\n", encoding="utf-8")
    env = operasional.muat_env(tmp_path)
    assert env["TELEGRAM_BOT_TOKEN"] == token
    assert penyaring_baru.sensor(f"url bot{token}/getMe") == "url bot***/getMe"


# ---------------------------------------------------------------------------
# Penyaring rahasia (§19.5)
# ---------------------------------------------------------------------------


def test_penyaring_mengganti_rahasia_dan_varian_url():
    p = PenyaringRahasia()
    p.tambah(TOKEN_PALSU)
    teks = (
        f"a {TOKEN_PALSU} b "
        f"https://api.telegram.org/bot{urllib.parse.quote(TOKEN_PALSU, safe='')}/sendMessage "
        f"q={urllib.parse.quote_plus(TOKEN_PALSU)} "
        f"r={urllib.parse.quote(TOKEN_PALSU).replace('%3A', '%3a')}"
    )
    hasil = p.sensor(teks)
    assert "FakeToken" not in hasil
    assert hasil == "a *** b https://api.telegram.org/bot***/sendMessage q=*** r=***"


def test_penyaring_mengabaikan_nilai_kosong_dan_pendek():
    p = PenyaringRahasia()
    for nilai in (None, "", "   ", "abc", "12345"):
        p.tambah(nilai)
    assert not p.ada()
    assert p.sensor("abc 12345 teks biasa") == "abc 12345 teks biasa"
    p.tambah("123456")  # tepat 6 karakter: disensor
    assert p.sensor("kode 123456.") == "kode ***."


def test_penyaring_beberapa_rahasia_terpanjang_dulu():
    p = PenyaringRahasia()
    p.tambah("rahasia-pendek")
    p.tambah("rahasia-pendek-dan-panjang")
    assert p.sensor("x rahasia-pendek-dan-panjang y rahasia-pendek z") == "x *** y *** z"
    p.tambah("  rahasia-pendek  ")  # spasi tepi dibuang; tidak menggandakan
    assert p.sensor("rahasia-pendek") == "***"


def test_penyaring_teks_tanpa_rahasia_tidak_berubah():
    p = PenyaringRahasia()
    assert p.sensor("tanpa rahasia") == "tanpa rahasia"
    p.tambah(TOKEN_PALSU)
    assert p.sensor("") == ""
    assert p.sensor("BUKAN SARAN INVESTASI.") == "BUKAN SARAN INVESTASI."


def test_penulis_tersensor_meneruskan_atribut(penyaring_baru):
    penyaring_baru.tambah(TOKEN_PALSU)
    asli = io.StringIO()
    w = operasional._PenulisTersensor(asli)
    assert w.write(f"token={TOKEN_PALSU}\n") == len(f"token={TOKEN_PALSU}\n")
    w.writelines([TOKEN_PALSU, "\n"])
    w.flush()
    assert asli.getvalue() == "token=***\n***\n"
    assert w.isatty() is False  # diteruskan ke aliran asli
    assert w.getvalue() == asli.getvalue()


def test_pasang_penyaring_dalam_proses_idempoten(monkeypatch, penyaring_baru):
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN_PALSU)
    operasional.pasang_penyaring_rahasia()
    pertama = sys.stdout
    operasional.pasang_penyaring_rahasia()
    assert sys.stdout is pertama and sys.stdout._asli is out  # tidak dibungkus ganda
    assert sys.stderr._asli is err
    assert sys.excepthook is operasional._excepthook_tersensor
    print(f"halo {TOKEN_PALSU}")
    try:
        raise RuntimeError(f"bot{TOKEN_PALSU}/sendMessage")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())
    assert out.getvalue() == "halo ***\n"
    assert TOKEN_PALSU not in err.getvalue() and "RuntimeError: bot***/sendMessage" in err.getvalue()


def test_token_tersensor_di_stdout_stderr_traceback(tmp_path):
    kode = """
        import logging, sys, urllib.parse
        from asisten_bei import operasional
        operasional.pasang_penyaring_rahasia()
        import os
        tok = os.environ["TELEGRAM_BOT_TOKEN"]
        print("stdout:", tok)
        print("url: https://api.telegram.org/bot" + urllib.parse.quote(tok, safe="") + "/sendMessage")
        sys.stdout.write("tulis langsung " + tok + "\\n")
        sys.stderr.write("stderr: " + tok + "\\n")
        logging.basicConfig(stream=sys.stderr)
        logging.getLogger("x").error("log konsol %s", tok)
        try:
            raise ConnectionError("Failed to connect https://api.telegram.org/bot" + tok + "/getMe")
        except ConnectionError as e:
            raise RuntimeError("kirim gagal: " + tok) from e
    """
    hasil = _jalankan(kode, tmp_path, TELEGRAM_BOT_TOKEN=TOKEN_PALSU)
    assert hasil.returncode == 1
    gabungan = hasil.stdout + hasil.stderr
    assert TOKEN_PALSU not in gabungan
    assert "FakeToken" not in gabungan
    assert urllib.parse.quote(TOKEN_PALSU, safe="") not in gabungan
    assert "stdout: ***" in hasil.stdout
    assert "url: https://api.telegram.org/bot***/sendMessage" in hasil.stdout
    assert "tulis langsung ***" in hasil.stdout
    assert "stderr: ***" in hasil.stderr
    assert "log konsol ***" in hasil.stderr
    # Traceback lengkap (dengan rantai exception) tetap ada, hanya tokennya yang disensor.
    assert "Traceback (most recent call last)" in hasil.stderr
    assert "ConnectionError: Failed to connect https://api.telegram.org/bot***/getMe" in hasil.stderr
    assert "RuntimeError: kirim gagal: ***" in hasil.stderr


def test_token_dari_env_file_tersensor_setelah_muat_env(tmp_path):
    """Token yang hanya ada di .env (bukan env proses) tersensor setelah `muat_env`."""
    (tmp_path / ".env").write_text(f"TELEGRAM_BOT_TOKEN={TOKEN_PALSU}\nTELEGRAM_CHAT_ID=1\n", encoding="utf-8")
    kode = f"""
        from pathlib import Path
        from asisten_bei import operasional
        operasional.pasang_penyaring_rahasia()
        env = operasional.muat_env(Path({str(tmp_path)!r}))
        print("token", env["TELEGRAM_BOT_TOKEN"])
        raise SystemExit(env["TELEGRAM_BOT_TOKEN"])
    """
    hasil = _jalankan(kode, tmp_path)
    assert TOKEN_PALSU not in hasil.stdout + hasil.stderr
    assert "token ***" in hasil.stdout


def test_tanpa_token_keluaran_tidak_berubah(tmp_path):
    kode = """
        from asisten_bei import operasional
        operasional.pasang_penyaring_rahasia()
        print("BUKAN SARAN INVESTASI. Rp 1.000.000 — ≈")
    """
    hasil = _jalankan(kode, tmp_path)
    assert hasil.returncode == 0, hasil.stderr
    assert hasil.stdout == "BUKAN SARAN INVESTASI. Rp 1.000.000 — ≈\n"


def test_filter_log_menyensor_pesan_argumen_dan_traceback(penyaring_baru):
    penyaring_baru.tambah(TOKEN_PALSU)
    aliran = io.StringIO()
    h = logging.StreamHandler(aliran)
    h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    h.addFilter(operasional.FilterRahasiaLog())
    log = logging.getLogger("uji_filter_rahasia")
    log.propagate = False
    log.addHandler(h)
    try:
        log.warning("token %s di url bot%s/x", TOKEN_PALSU, TOKEN_PALSU)
        try:
            raise ValueError(f"https://api.telegram.org/bot{TOKEN_PALSU}/sendMessage")
        except ValueError:
            log.exception("gagal kirim")
        log.info("tumpukan", stack_info=True, extra={})
        log.error("format salah %s %s", "satu")  # argumen kurang: tetap tidak meledak
    finally:
        log.removeHandler(h)
    isi = aliran.getvalue()
    assert TOKEN_PALSU not in isi
    assert "WARNING token *** di url bot***/x" in isi
    assert "ValueError: https://api.telegram.org/bot***/sendMessage" in isi
    assert "Traceback (most recent call last)" in isi


# ---------------------------------------------------------------------------
# Log harian (§19.5)
# ---------------------------------------------------------------------------


def _ctx(akar: Path, **ubah_operasional) -> KonteksApp:
    data = yaml.safe_load((AKAR_PROYEK / "config.yaml").read_text(encoding="utf-8"))
    data["operasional"].update(ubah_operasional)
    return KonteksApp(cfg=validasi_dict(data), akar=akar, path_config=akar / "config.yaml")


@pytest.fixture
def log_bersih():
    operasional.tutup_log()
    yield
    operasional.tutup_log()


def _handler_milik():
    return [h for h in logging.getLogger("asisten_bei").handlers if getattr(h, "_asisten_bei", False)]


def test_siapkan_log_menulis_berkas_harian_tersensor(tmp_path, log_bersih, penyaring_baru):
    penyaring_baru.tambah(TOKEN_PALSU)
    log = operasional.siapkan_log(_ctx(tmp_path))
    assert log.name == "asisten_bei"
    log.info("mulai unduh")
    logging.getLogger("asisten_bei.data").warning("simbol gagal: bot%s", TOKEN_PALSU)  # logger anak
    try:
        raise ConnectionError(f"https://api.telegram.org/bot{TOKEN_PALSU}/sendMessage")
    except ConnectionError:
        log.exception("Telegram gagal")
    operasional.tutup_log()
    path = tmp_path / "output" / "log" / "2026-10-15.log"
    isi = path.read_text(encoding="utf-8")
    assert TOKEN_PALSU not in isi
    assert "2026-10-15 20:00:00 INFO asisten_bei: mulai unduh" in isi
    assert "WARNING asisten_bei.data: simbol gagal: bot***" in isi
    assert "ConnectionError: https://api.telegram.org/bot***/sendMessage" in isi


def test_siapkan_log_idempoten_dan_ganti_hari(tmp_path, log_bersih, atur_waktu):
    ctx = _ctx(tmp_path)
    operasional.siapkan_log(ctx)
    operasional.siapkan_log(ctx)
    assert len(_handler_milik()) == 1
    logging.getLogger("asisten_bei").info("satu kali")
    atur_waktu("2026-10-16T19:30:00+07:00")
    operasional.siapkan_log(ctx)
    hs = _handler_milik()
    assert len(hs) == 1 and hs[0].baseFilename.endswith("2026-10-16.log")
    logging.getLogger("asisten_bei").info("hari berikut")
    operasional.tutup_log()
    assert _handler_milik() == []
    folder = tmp_path / "output" / "log"
    assert (folder / "2026-10-15.log").read_text(encoding="utf-8").count("satu kali") == 1
    assert "hari berikut" in (folder / "2026-10-16.log").read_text(encoding="utf-8")


def test_log_lama_dihapus_terjadwal_log_dipertahankan(tmp_path, log_bersih):
    folder = tmp_path / "output" / "log"
    folder.mkdir(parents=True)
    # 2026-10-15 − 90 hari = 2026-07-17 (dipertahankan); 2026-07-16 = 91 hari (dihapus).
    nama = ["2026-07-16.log", "2026-07-17.log", "2025-01-01.log", "2026-10-14.log",
            "terjadwal.log", "catatan.txt", "2026-13-45.log", "2026-07-01.log.bak"]
    for n in nama:
        (folder / n).write_text("x", encoding="utf-8")
    operasional.siapkan_log(_ctx(tmp_path, simpan_log_hari=90))
    tersisa = sorted(p.name for p in folder.iterdir())
    assert tersisa == sorted(
        ["2026-07-17.log", "2026-10-14.log", "2026-10-15.log", "terjadwal.log", "catatan.txt",
         "2026-13-45.log", "2026-07-01.log.bak"]
    )


def test_hapus_log_lama_batas_hari(tmp_path):
    from datetime import date

    for n in ("2026-10-05.log", "2026-10-04.log", "2026-10-16.log"):
        (tmp_path / n).write_text("", encoding="utf-8")
    dihapus = operasional.hapus_log_lama(tmp_path, date(2026, 10, 15), 10)
    # 2026-10-04 berumur 11 hari → dihapus; 10 hari dipertahankan; tanggal masa depan tidak disentuh.
    assert [p.name for p in dihapus] == ["2026-10-04.log"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-10-05.log", "2026-10-16.log"]
    assert operasional.hapus_log_lama(tmp_path / "tidak_ada", date(2026, 10, 15), 10) == []


# ---------------------------------------------------------------------------
# Tulis atomik & nama alternatif (§18.2)
# ---------------------------------------------------------------------------


def _sisa_tmp(folder: Path) -> list[str]:
    return [p.name for p in folder.iterdir() if p.name.endswith(".tmp")]


def test_tulis_atomik_tanpa_berkas_sementara(tmp_path):
    path = tmp_path / "sub" / "dalam" / "hasil.json"
    operasional.tulis_atomik(path, '{"a": "ü"}')
    assert path.read_text(encoding="utf-8") == '{"a": "ü"}'
    operasional.tulis_atomik(path, b"\x00\x01")
    assert path.read_bytes() == b"\x00\x01"
    operasional.tulis_atomik(path, "é", encoding="latin-1")
    assert path.read_bytes() == b"\xe9"
    assert _sisa_tmp(path.parent) == []
    assert sorted(p.name for p in path.parent.iterdir()) == ["hasil.json"]


def test_tulis_atomik_gagal_di_tengah_berkas_lama_utuh(tmp_path, monkeypatch):
    path = tmp_path / "jurnal.csv"
    path.write_text("lama", encoding="utf-8")

    def replace_gagal(src, dst):
        raise OSError("disk penuh (simulasi)")

    monkeypatch.setattr(operasional.os, "replace", replace_gagal)
    with pytest.raises(OSError, match="disk penuh"):
        operasional.tulis_atomik(path, "baru")
    assert path.read_text(encoding="utf-8") == "lama"
    assert _sisa_tmp(tmp_path) == []


def test_tulis_atomik_gagal_saat_menulis_tidak_meninggalkan_tmp(tmp_path, monkeypatch):
    path = tmp_path / "a.txt"

    def fsync_gagal(fd):
        raise OSError("I/O error (simulasi)")

    monkeypatch.setattr(operasional.os, "fsync", fsync_gagal)
    with pytest.raises(OSError):
        operasional.tulis_atomik(path, "isi")
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def tujuan_terkunci(monkeypatch):
    """Simulasi berkas dibuka Excel: `os.replace` ke path tertentu → PermissionError."""
    terkunci: set[str] = set()
    asli = os.replace

    def replace_palsu(src, dst, *a, **k):
        if os.path.abspath(dst) in terkunci:
            raise PermissionError(13, "The process cannot access the file", str(dst))
        return asli(src, dst, *a, **k)

    monkeypatch.setattr(operasional.os, "replace", replace_palsu)
    return lambda p: terkunci.add(os.path.abspath(p))


def test_tulis_keluaran_normal(tmp_path):
    path, peringatan = operasional.tulis_keluaran(tmp_path / "laporan.md", "# isi")
    assert path == tmp_path / "laporan.md" and peringatan is None
    assert path.read_text(encoding="utf-8") == "# isi"


def test_tulis_keluaran_terkunci_menulis_nama_alternatif(tmp_path, tujuan_terkunci):
    tujuan = tmp_path / "rencana.xlsx"
    tujuan.write_bytes(b"lama")
    tujuan_terkunci(tujuan)
    path, peringatan = operasional.tulis_keluaran(tujuan, b"baru")
    assert path == tmp_path / "rencana_200000.xlsx"  # HHMMSS dari waktu virtual 20:00:00 WIB
    assert path.read_bytes() == b"baru"
    assert tujuan.read_bytes() == b"lama"
    assert peringatan is not None and "rencana.xlsx" in peringatan and "rencana_200000.xlsx" in peringatan
    assert "terkunci" in peringatan
    assert _sisa_tmp(tmp_path) == []


def test_tulis_keluaran_alternatif_juga_terkunci_meneruskan_error(tmp_path, tujuan_terkunci):
    tujuan = tmp_path / "kandidat.csv"
    tujuan_terkunci(tujuan)
    tujuan_terkunci(tmp_path / "kandidat_200000.csv")
    with pytest.raises(PermissionError):
        operasional.tulis_keluaran(tujuan, "x")
    assert _sisa_tmp(tmp_path) == []


def test_nama_alternatif_memakai_jam_wib(tmp_path, atur_waktu):
    atur_waktu("2026-10-15T07:05:09+00:00")  # = 14:05:09 WIB
    assert operasional.nama_alternatif(tmp_path / "laporan.html").name == "laporan_140509.html"
    waktu.tidur(2)
    assert operasional.nama_alternatif(tmp_path / "laporan.html").name == "laporan_140511.html"


def test_tulis_csv_disclaimer_baris_pertama(tmp_path, tujuan_terkunci):
    df = pd.DataFrame({"kode": ["AAAA", "BBBB"], "nilai": [1.5, 2.0], "catatan": ["—", "≈ a,b"]})
    path, peringatan = operasional.tulis_csv(tmp_path / "kandidat.csv", df)
    assert peringatan is None
    mentah = path.read_bytes()
    assert b"\r\n" not in mentah
    baris = mentah.decode("utf-8").split("\n")
    assert baris[0] == DISCLAIMER_CSV
    assert "BUKAN SARAN INVESTASI" in baris[0]
    assert baris[1] == "kode,nilai,catatan"
    kembali = pd.read_csv(path, skiprows=1)
    pd.testing.assert_frame_equal(kembali, df)
    # Terkunci → nama alternatif juga berdisclaimer.
    tujuan_terkunci(path)
    path2, peringatan2 = operasional.tulis_csv(path, df)
    assert path2.name == "kandidat_200000.csv" and peringatan2
    assert path2.read_text(encoding="utf-8").split("\n")[0] == DISCLAIMER_CSV


def test_operasional_tidak_mengimpor_pandas_saat_dimuat(tmp_path):
    """Penyaring harus bisa dipasang walau pandas/filelock rusak (§19.5 langkah pertama)."""
    kode = """
        import sys
        from asisten_bei import operasional
        operasional.pasang_penyaring_rahasia()
        print(sorted(m for m in ("pandas", "filelock", "dotenv", "yaml") if m in sys.modules))
    """
    hasil = _jalankan(kode, tmp_path)
    assert hasil.returncode == 0, hasil.stderr
    assert hasil.stdout.strip() == "[]"

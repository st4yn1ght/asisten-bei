"""Uji sumber waktu tunggal `waktu.py` (SPEC §22.1 "Isolasi dan determinisme")."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from asisten_bei import waktu
from asisten_bei.waktu import ZONA


@pytest.fixture
def tanpa_tidur_nyata(monkeypatch):
    """Gagal bila `time.sleep` sungguhan terpanggil dalam mode virtual."""

    def jangan(_detik):
        raise AssertionError("time.sleep tidak boleh dipanggil dalam mode virtual")

    monkeypatch.setattr(waktu.time, "sleep", jangan)


def test_default_conftest_virtual_wib():
    assert waktu.mode_virtual()
    s = waktu.sekarang()
    assert s == datetime(2026, 10, 15, 20, 0, tzinfo=ZONA)
    assert s.tzinfo is ZONA and str(s.tzinfo) == "Asia/Jakarta"
    assert s.utcoffset() == timedelta(hours=7)
    assert waktu.hari_ini() == date(2026, 10, 15)


def test_env_iso_aware_wib(atur_waktu):
    atur_waktu("2026-10-16T19:31:05+07:00")
    assert waktu.sekarang() == datetime(2026, 10, 16, 19, 31, 5, tzinfo=ZONA)


def test_env_iso_naive_dianggap_wib(atur_waktu):
    atur_waktu("2026-10-15T19:30:00")
    s = waktu.sekarang()
    assert s.tzinfo is ZONA
    assert (s.year, s.month, s.day, s.hour, s.minute) == (2026, 10, 15, 19, 30)
    assert s.astimezone(timezone.utc) == datetime(2026, 10, 15, 12, 30, tzinfo=timezone.utc)
    atur_waktu("2026-10-15")  # tanggal saja → tengah malam WIB
    assert waktu.sekarang() == datetime(2026, 10, 15, 0, 0, tzinfo=ZONA)


@pytest.mark.parametrize(
    "iso, harapan",
    [
        ("2026-10-15T13:00:00+00:00", datetime(2026, 10, 15, 20, 0, tzinfo=ZONA)),
        ("2026-10-15T17:30:00Z", datetime(2026, 10, 16, 0, 30, tzinfo=ZONA)),  # tanggal WIB berganti
        ("2026-10-15T08:00:00-04:00", datetime(2026, 10, 15, 19, 0, tzinfo=ZONA)),
    ],
)
def test_env_iso_zona_lain_dikonversi_ke_wib(atur_waktu, iso, harapan):
    atur_waktu(iso)
    s = waktu.sekarang()
    assert s == harapan and s.tzinfo is ZONA
    assert (s.hour, s.minute) == (harapan.hour, harapan.minute)  # jam dinding WIB, bukan zona asal
    assert waktu.hari_ini() == harapan.date()


def test_env_dengan_spasi_tepi(monkeypatch):
    monkeypatch.setenv(waktu.ENV_WAKTU, "  2026-10-15T20:00:00+07:00 \n")
    assert waktu.sekarang() == datetime(2026, 10, 15, 20, 0, tzinfo=ZONA)


def test_env_format_salah_error(monkeypatch):
    monkeypatch.setenv(waktu.ENV_WAKTU, "15/10/2026 20:00")
    with pytest.raises(ValueError):
        waktu.sekarang()


@pytest.mark.parametrize("nilai", [None, "", "   "])
def test_tanpa_env_mode_nyata(monkeypatch, nilai):
    if nilai is None:
        monkeypatch.delenv(waktu.ENV_WAKTU, raising=False)
    else:
        monkeypatch.setenv(waktu.ENV_WAKTU, nilai)
    assert not waktu.mode_virtual()
    sebelum = datetime.now(ZONA)
    s = waktu.sekarang()
    sesudah = datetime.now(ZONA)
    assert s.tzinfo is ZONA
    assert sebelum <= s <= sesudah


def test_tidur_virtual_memajukan_sekarang(tanpa_tidur_nyata):
    awal = waktu.sekarang()
    waktu.tidur(15 * 60)
    assert waktu.sekarang() == awal + timedelta(minutes=15)
    waktu.tidur(0.5)
    waktu.tidur(29.5)
    assert waktu.sekarang() == awal + timedelta(minutes=15, seconds=30)
    # Tidak bergantung pada jam nyata: dua pembacaan berturut-turut identik.
    assert waktu.sekarang() == waktu.sekarang()


def test_tidur_nol_atau_negatif_tidak_berbuat_apa_apa(tanpa_tidur_nyata):
    awal = waktu.sekarang()
    waktu.tidur(0)
    waktu.tidur(-30)
    assert waktu.sekarang() == awal


def test_tidur_virtual_melewati_tengah_malam(atur_waktu, tanpa_tidur_nyata):
    atur_waktu("2026-10-15T23:59:30+07:00")
    waktu.tidur(60)
    assert waktu.sekarang() == datetime(2026, 10, 16, 0, 0, 30, tzinfo=ZONA)
    assert waktu.hari_ini() == date(2026, 10, 16)


def test_tunggu_berulang_seperti_tunggu_data(atur_waktu, tanpa_tidur_nyata):
    """Pola §8.6: tunggu `jumlah` × `jeda_menit` → jam virtual maju tepat sebesar itu."""
    atur_waktu("2026-10-15T19:30:00+07:00")
    for _ in range(3):
        waktu.tidur(15 * 60)
    assert waktu.sekarang() == datetime(2026, 10, 15, 20, 15, tzinfo=ZONA)


def test_reset_virtual(monkeypatch, tanpa_tidur_nyata):
    waktu.tidur(3600)
    assert waktu.sekarang() == datetime(2026, 10, 15, 21, 0, tzinfo=ZONA)
    waktu.reset_virtual()
    assert waktu.sekarang() == datetime(2026, 10, 15, 20, 0, tzinfo=ZONA)
    # Offset tidur tetap berlaku saat env diganti tanpa reset (fixture `atur_waktu` selalu reset).
    waktu.tidur(60)
    monkeypatch.setenv(waktu.ENV_WAKTU, "2026-10-16T08:00:00+07:00")
    assert waktu.sekarang() == datetime(2026, 10, 16, 8, 1, tzinfo=ZONA)
    waktu.reset_virtual()
    assert waktu.sekarang() == datetime(2026, 10, 16, 8, 0, tzinfo=ZONA)


def test_atur_waktu_mereset_offset(atur_waktu, tanpa_tidur_nyata):
    waktu.tidur(600)
    atur_waktu("2026-10-17T10:00:00+07:00")
    assert waktu.sekarang() == datetime(2026, 10, 17, 10, 0, tzinfo=ZONA)


def test_tidur_mode_nyata_memanggil_time_sleep(monkeypatch):
    dipanggil = []
    monkeypatch.setattr(waktu.time, "sleep", dipanggil.append)
    monkeypatch.delenv(waktu.ENV_WAKTU)
    waktu.tidur(1.5)
    waktu.tidur(0)
    assert dipanggil == [1.5]
    # Mode nyata tidak mengubah offset virtual.
    monkeypatch.setenv(waktu.ENV_WAKTU, "2026-10-15T20:00:00+07:00")
    assert waktu.sekarang() == datetime(2026, 10, 15, 20, 0, tzinfo=ZONA)


def test_ke_wib():
    naive = datetime(2026, 10, 15, 19, 30)
    assert waktu.ke_wib(naive) == datetime(2026, 10, 15, 19, 30, tzinfo=ZONA)
    utc = datetime(2026, 10, 15, 12, 30, tzinfo=timezone.utc)
    hasil = waktu.ke_wib(utc)
    assert hasil.tzinfo is ZONA and (hasil.hour, hasil.minute) == (19, 30) and hasil == utc
    wib = datetime(2026, 10, 15, 19, 30, tzinfo=ZONA)
    assert waktu.ke_wib(wib) == wib


@pytest.mark.parametrize("bulan", [1, 4, 7, 10, 12])
def test_wib_tanpa_musim_panas(bulan):
    assert datetime(2026, bulan, 15, 12, tzinfo=ZONA).utcoffset() == timedelta(hours=7)

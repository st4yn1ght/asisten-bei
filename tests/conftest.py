"""Fixture bersama uji (SPEC §22.1): waktu virtual, isolasi berkas, tanpa rahasia."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from asisten_bei import waktu

AKAR_PROYEK = Path(__file__).resolve().parents[1]
FIXTURES = AKAR_PROYEK / "tests" / "fixtures"
WAKTU_UJI_DEFAULT = "2026-10-15T20:00:00+07:00"


@pytest.fixture(autouse=True)
def _waktu_dan_env(monkeypatch):
    """Semua uji wajib memakai waktu virtual dan tidak membawa token Telegram sungguhan."""
    monkeypatch.setenv(waktu.ENV_WAKTU, WAKTU_UJI_DEFAULT)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    waktu.reset_virtual()
    yield
    waktu.reset_virtual()


@pytest.fixture
def atur_waktu(monkeypatch):
    """Panggil `atur_waktu("2026-10-16T19:00:00+07:00")` untuk mengganti "sekarang"."""

    def _atur(iso: str) -> None:
        monkeypatch.setenv(waktu.ENV_WAKTU, iso)
        waktu.reset_virtual()

    return _atur


@pytest.fixture
def akar_uji(tmp_path) -> Path:
    """Salinan tests/fixtures di direktori sementara; berkas pengguna tidak pernah disentuh."""
    for nama in ("config_uji.yaml", "config_uji_ujibo.yaml"):
        sumber = FIXTURES / nama
        if sumber.exists():
            shutil.copy2(sumber, tmp_path / nama)
    for nama in ("input_uji", "pasar_sintetis", "manual_uji", "acuan"):
        sumber = FIXTURES / nama
        if sumber.exists():
            shutil.copytree(sumber, tmp_path / nama)
    return tmp_path


@pytest.fixture
def ctx_uji(akar_uji):
    from asisten_bei.config import muat_config

    return muat_config(akar_uji / "config_uji.yaml", akar_uji)


@pytest.fixture(scope="session")
def proyek_harian_dasar(tmp_path_factory) -> Path:
    """Proyek sintetis + backtest edge (gate_status.json), dibangun sekali per sesi (tahap 6)."""
    from bantu_harian import bangun_dasar

    return bangun_dasar(tmp_path_factory.mktemp("harian_dasar") / "proyek")


@pytest.fixture
def proyek(tmp_path, proyek_harian_dasar, monkeypatch):
    """Salinan proyek harian dasar; tanpa jaringan/tidur sungguhan; aliran sys dipulihkan."""
    from bantu_harian import salin
    from test_backtest_cli import _isolasi_mp

    _isolasi_mp(monkeypatch)
    return salin(proyek_harian_dasar, tmp_path / "proyek")

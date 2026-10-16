"""asisten-bei — asisten keputusan saham BEI untuk pemakaian pribadi.

BUKAN SARAN INVESTASI. Program ini tidak terhubung ke broker dan tidak pernah mengirim order.
"""

from __future__ import annotations


def _baca_versi() -> str:
    """Versi program dari metadata paket; cadangan: pyproject.toml (sumber tunggal)."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("asisten-bei")
        except PackageNotFoundError:
            pass
    except ImportError:  # pragma: no cover
        pass
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with open(pyproject, "rb") as f:
        return tomllib.load(f)["project"]["version"]


__version__ = _baca_versi()

# Teks disclaimer SPEC §18.4 (wajib di semua keluaran).
DISCLAIMER = (
    "BUKAN SARAN INVESTASI. Laporan ini dibuat otomatis oleh program pribadi berdasarkan aturan "
    "mekanis dan data publik yang bisa salah, terlambat, atau tidak lengkap. Hasil backtest dan "
    "paper trading tidak menjamin hasil di masa depan. Seluruh keputusan dan risiko transaksi "
    "menjadi tanggung jawab pengguna."
)
DISCLAIMER_SINGKAT = "BUKAN SARAN INVESTASI."
# Baris pertama setiap CSV keluaran (dibaca kembali dengan comment="#").
DISCLAIMER_CSV = "# " + DISCLAIMER
# Baris pertama setiap YAML keluaran.
DISCLAIMER_YAML = "# " + DISCLAIMER

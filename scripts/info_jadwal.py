"""BUKAN SARAN INVESTASI. Pembantu `scripts/daftar_jadwal.ps1` (SPEC §19.3).

Membaca config (default `config.yaml` di folder kerja) lewat `muat_config`, sekaligus memvalidasinya, lalu mencetak
`jadwal.jam_run` dan `operasional.batas_waktu_run_jam`, satu nilai per baris. Config tidak valid, berkas tidak ada,
atau paket tidak bisa diimpor → pesan "ERROR: ..." dan kode keluar 3 (§20.1). Semua keluaran ke stdout:
Windows PowerShell 5.1 mengubah baris stderr program menjadi error PowerShell.
"""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    path = argv[0] if argv else "config.yaml"
    try:
        from asisten_bei.config import muat_config

        cfg = muat_config(path).cfg
    except Exception as x:  # noqa: BLE001 - semua kegagalan dilaporkan sebagai config tidak valid
        print(f"ERROR: {x}")
        return 3
    print(cfg.jadwal.jam_run)
    print(cfg.operasional.batas_waktu_run_jam)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

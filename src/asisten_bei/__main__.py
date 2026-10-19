"""CLI asisten-bei (SPEC §19.2). Langkah pertama: pasang penyaring rahasia (§19.5).

    python -m asisten_bei [--config config.yaml] [--akar DIR] <perintah> [opsi]

Opsi global boleh ditulis sebelum atau sesudah nama perintah. Kode keluar §20.1; kesalahan argumen
diperlakukan sebagai input tidak valid (3), bukan kode 2 argparse (kode 2 berarti data belum tersedia).
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from collections.abc import Callable, Sequence

from . import DISCLAIMER, DISCLAIMER_SINGKAT, __version__, operasional

PERINTAH_BELUM_TERSEDIA = {
    "backtest": "tahap 5",
    "harian": "tahap 6",
    "jurnal": "tahap 6",
    "uji-telegram": "tahap 7",
}


class KesalahanArgumen(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # argparse memanggil sys.exit(2); kode 2 di sini berarti data
        raise KesalahanArgumen(f"{self.prog}: {message}")


def buat_parser() -> argparse.ArgumentParser:
    umum = argparse.ArgumentParser(add_help=False)
    umum.add_argument("--config", default=argparse.SUPPRESS, metavar="PATH",
                      help="berkas config (default: config.yaml di folder kerja; .bat menjalankan dari folder proyek)")
    umum.add_argument("--akar", default=argparse.SUPPRESS, metavar="DIR",
                      help="dasar semua path relatif di config (default: folder berisi config)")

    p = _Parser(prog="asisten_bei", description="Asisten keputusan saham BEI — " + DISCLAIMER_SINGKAT,
                parents=[umum])
    p.add_argument("--versi", action="version", version=f"asisten-bei {__version__} — {DISCLAIMER_SINGKAT}")
    sub = p.add_subparsers(dest="perintah", metavar="perintah", parser_class=_Parser)
    sub.required = True

    def tambah(nama: str, bantuan: str) -> argparse.ArgumentParser:
        return sub.add_parser(nama, parents=[umum], help=bantuan, description=f"{bantuan}. {DISCLAIMER_SINGKAT}")

    cek = tambah("cek", "validasi config + input")
    cek.add_argument("--silang", nargs=2, metavar=("KODE", "TANGGAL"),
                     help="cetak bar cache, F(d), dan nilai nominal untuk dicocokkan dengan sumber resmi")

    unduh = tambah("unduh", "perbarui cache data harga")
    unduh.add_argument("--penuh", action="store_true", help="unduh ulang penuh semua simbol")
    unduh.add_argument("--simbol", metavar="K1,K2", help="hanya simbol ini (pisahkan dengan koma)")

    bt = tambah("backtest", "backtest (§16)")
    bt.add_argument("--strategi")
    bt.add_argument("--mode")
    h = tambah("harian", "run harian (§18)")
    for opsi in ("--jadwal", "--offline", "--tanpa-notifikasi", "--catat", "--catat-ulang"):
        h.add_argument(opsi, action="store_true")
    h.add_argument("--tanggal")
    j = tambah("jurnal", "jurnal kertas (§17)")
    j.add_argument("--ekspor", action="store_true")
    j.add_argument("--rekonsiliasi", action="store_true")
    tg = tambah("uji-telegram", "uji notifikasi Telegram (§18.3)")
    tg.add_argument("--cari-chat-id", action="store_true")
    return p


def _jalankan(args: argparse.Namespace, keluar: Callable[[str], None]) -> int:
    from .config import muat_config

    ctx = muat_config(getattr(args, "config", "config.yaml"), getattr(args, "akar", None))
    log = operasional.siapkan_log(ctx)
    log.info("mulai: %s (asisten-bei %s)", args.perintah, __version__)
    try:
        if args.perintah == "cek":
            from .cek import jalankan_cek

            silang = tuple(args.silang) if args.silang else None
            return jalankan_cek(ctx, silang_kode=silang, keluar=keluar).kode_keluar
        if args.perintah == "unduh":
            from .unduh import jalankan_unduh, parse_daftar_simbol

            try:
                simbol = parse_daftar_simbol(args.simbol) if args.simbol is not None else None
            except ValueError as e:
                raise KesalahanArgumen(f"--simbol: {e}") from None
            return jalankan_unduh(ctx, penuh=args.penuh, simbol=simbol, keluar=keluar).kode_keluar
        tahap = PERINTAH_BELUM_TERSEDIA[args.perintah]
        keluar(f"Perintah {args.perintah} belum tersedia pada versi ini (dikerjakan pada {tahap}, SPEC §23).")
        keluar("")
        keluar(DISCLAIMER)
        return operasional.KODE_ERROR
    finally:
        log.info("selesai: %s", args.perintah)


def main(argv: Sequence[str] | None = None, keluar: Callable[[str], None] | None = None) -> int:
    operasional.pasang_penyaring_rahasia()
    keluar = keluar or (lambda s: print(s, flush=True))
    from .config import ConfigTidakValid
    from .inputs import InputTidakValid

    log = logging.getLogger(operasional.NAMA_LOGGER)
    try:
        args = buat_parser().parse_args(argv)
        keluar(DISCLAIMER_SINGKAT)
        return _jalankan(args, keluar)
    except KesalahanArgumen as e:
        keluar(f"ERROR argumen: {e}")
        keluar("Lihat `python -m asisten_bei --help`.")
        keluar(DISCLAIMER_SINGKAT)
        return operasional.KODE_INPUT
    except ConfigTidakValid as e:
        keluar("ERROR config tidak valid:")
        for m in e.pesan:
            keluar(f"  - {m}")
        keluar(DISCLAIMER)
        return operasional.KODE_INPUT
    except InputTidakValid as e:
        keluar("ERROR input tidak valid (jalankan cek untuk rinciannya):")
        for m in e.masalah:
            keluar(f"  - {m}")
        keluar(DISCLAIMER)
        return operasional.KODE_INPUT
    except operasional.KunciDipegang as e:
        keluar(f"ERROR: {e}")
        keluar(DISCLAIMER_SINGKAT)
        return operasional.KODE_KUNCI
    except KeyboardInterrupt:
        keluar("Dihentikan pengguna.")
        return operasional.KODE_ERROR
    except Exception as e:  # error tak terduga: kode 1, jejak lengkap ke log & stderr (tersensor)
        if log.handlers:  # tanpa handler (config gagal dimuat) logging mencetak jejak ganda ke stderr
            log.exception("error tak terduga")
        keluar(f"ERROR tak terduga: {type(e).__name__}: {e}")
        print(traceback.format_exc(), file=sys.stderr)
        keluar(DISCLAIMER_SINGKAT)
        return operasional.KODE_ERROR
    finally:
        operasional.tutup_log()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

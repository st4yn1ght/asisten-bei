"""Memuat, memvalidasi, dan meresolusi config.yaml (SPEC §7).

- Semua kunci wajib ada; kunci tak dikenal = error (pydantic `extra="forbid"`, tanpa default).
- Validasi tambahan: bobot skor = 100, tabel fraksi/ARA/ARB bersambung, persen dalam rentang wajar.
- Resolusi rujukan `ikuti_akun` / `ikuti_risiko` / `ikuti_maks_posisi` per konteks (live/edge/realistis).
- Path relatif diselesaikan terhadap `--akar` (default: folder berisi config.yaml).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, Union

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)


class ConfigTidakValid(Exception):
    """Config tidak valid. `pesan` berisi daftar baris yang menunjuk kunci (kode keluar 3)."""

    def __init__(self, pesan: list[str]):
        self.pesan = list(pesan)
        super().__init__("Config tidak valid:\n" + "\n".join(f"  - {p}" for p in self.pesan))


# ---------------------------------------------------------------------------
# Tipe dasar
# ---------------------------------------------------------------------------

Pct = Annotated[Decimal, Field(ge=0, le=100)]           # persen 0..100
PctPositif = Annotated[Decimal, Field(gt=0, le=100)]
PctFee = Annotated[Decimal, Field(ge=0, le=10)]          # fee/slippage per sisi
Rupiah = Annotated[Decimal, Field(ge=0)]
RupiahPositif = Annotated[Decimal, Field(gt=0)]
IntPos = Annotated[StrictInt, Field(ge=1)]
IntNonNeg = Annotated[StrictInt, Field(ge=0)]
FloatNonNeg = Annotated[float, Field(ge=0)]
FloatPos = Annotated[float, Field(gt=0)]

_POLA_JAM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _cek_jam(v: str | None) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str) or not _POLA_JAM.match(v):
        raise ValueError('format jam harus "HH:MM" (24 jam)')
    return v


def parse_jam(v: str) -> time:
    jam, menit = v.split(":")
    return time(int(jam), int(menit))


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Bagian config
# ---------------------------------------------------------------------------


class Akun(_M):
    modal_awal_idr: RupiahPositif
    file_akun: StrictStr
    maks_umur_hari_bursa: IntNonNeg


class HatiHati(_M):
    pengali_risiko: Annotated[Decimal, Field(ge=0, le=1)]
    maks_entry_baru_per_hari: IntNonNeg | None


class Risiko(_M):
    risiko_per_transaksi_pct: PctPositif
    maks_posisi: IntPos
    alokasi_maks_pct: Union[Literal["ikuti_maks_posisi"], PctPositif]
    maks_per_sektor: IntPos | None
    maks_entry_baru_per_hari: IntPos | None
    izinkan_satu_lot_melebihi_risiko: StrictBool
    dana_jual_langsung_dipakai: StrictBool
    slot_bebas_dari_jual_terjadwal: StrictBool
    hati_hati: HatiHati


class Biaya(_M):
    fee_beli_pct: PctFee
    fee_jual_pct: PctFee
    fee_minimum_idr: Rupiah
    pembulatan_fee: Literal["rupiah_terdekat", "rupiah_ke_atas"] | None
    slippage_pct: PctFee
    terverifikasi: StrictBool


class Entry(_M):
    gap_maks_atr: FloatNonNeg
    stop_aktif_hari_entry: StrictBool


class Jadwal(_M):
    jam_run: StrictStr
    kirim_pesan_saat_libur: StrictBool

    @field_validator("jam_run")
    @classmethod
    def _v_jam(cls, v):
        return _cek_jam(v)


class JadwalSesi(_M):
    terverifikasi: StrictBool
    sumber: StrictStr
    tanggal_cek: date | None
    jam_penutupan: StrictStr | None
    jam_pra_pembukaan: StrictStr | None

    @field_validator("jam_penutupan", "jam_pra_pembukaan")
    @classmethod
    def _v_jam(cls, v):
        return _cek_jam(v)


class BarisFraksi(_M):
    harga_min: Rupiah
    harga_maks: RupiahPositif | None
    fraksi: RupiahPositif


class BarisBatas(_M):
    harga_min: Rupiah
    harga_maks: RupiahPositif | None
    batas_pct: Annotated[Decimal, Field(gt=0, le=100)]


def _cek_tabel_rentang(baris: list, nama: str) -> None:
    """Tabel harus bersambung: harga_min[i+1] == harga_maks[i], tanpa tumpang tindih atau celah,
    dan hanya baris terakhir yang boleh (dan wajib) harga_maks null. harga_min inklusif,
    harga_maks eksklusif."""
    if not baris:
        return
    for i, b in enumerate(baris):
        if b.harga_maks is not None and b.harga_maks <= b.harga_min:
            raise ValueError(f"{nama}[{i}]: harga_maks harus > harga_min")
        if b.harga_maks is None and i != len(baris) - 1:
            raise ValueError(f"{nama}[{i}]: harga_maks null hanya boleh di baris terakhir")
    if baris[-1].harga_maks is not None:
        raise ValueError(f"{nama}: baris terakhir harus harga_maks null (tabel berlubang di atas)")
    for i in range(len(baris) - 1):
        a, b = baris[i], baris[i + 1]
        if b.harga_min < a.harga_maks:
            raise ValueError(f"{nama}[{i}] dan [{i + 1}] tumpang tindih")
        if b.harga_min > a.harga_maks:
            raise ValueError(f"{nama}[{i}] dan [{i + 1}] berlubang (ada celah harga)")


class FraksiHarga(_M):
    terverifikasi: StrictBool
    sumber: StrictStr
    tanggal_cek: date | None
    berlaku_mulai: date | None
    tabel: list[BarisFraksi]

    @model_validator(mode="after")
    def _cek(self):
        _cek_tabel_rentang(self.tabel, "tabel")
        if self.terverifikasi:
            if not self.tabel:
                raise ValueError("terverifikasi: true tetapi tabel kosong")
            if self.berlaku_mulai is None:
                raise ValueError("terverifikasi: true tetapi berlaku_mulai kosong")
        return self


HARGA_ACUAN_DIDUKUNG = ("close_kemarin",)


class AutoReject(_M):
    terverifikasi: StrictBool
    sumber: StrictStr
    tanggal_cek: date | None
    berlaku_mulai: date | None
    papan_berlaku: StrictStr
    harga_acuan: StrictStr | None
    ara: list[BarisBatas]
    arb: list[BarisBatas]

    @model_validator(mode="after")
    def _cek(self):
        _cek_tabel_rentang(self.ara, "ara")
        _cek_tabel_rentang(self.arb, "arb")
        if self.terverifikasi:
            if not self.ara or not self.arb:
                raise ValueError("terverifikasi: true tetapi tabel ara/arb kosong")
            if self.berlaku_mulai is None:
                raise ValueError("terverifikasi: true tetapi berlaku_mulai kosong")
            if self.harga_acuan not in HARGA_ACUAN_DIDUKUNG:
                raise ValueError(
                    f"harga_acuan {self.harga_acuan!r} belum didukung program "
                    f"(didukung: {', '.join(HARGA_ACUAN_DIDUKUNG)})"
                )
        return self


class Pasar(_M):
    lot_lembar: IntPos
    file_libur: StrictStr
    jadwal_sesi: JadwalSesi
    fraksi_harga: FraksiHarga
    auto_reject: AutoReject


class SimbolIhsg(_M):
    yfinance: StrictStr
    csv_manual: StrictStr


class TungguData(_M):
    jumlah: IntNonNeg
    jeda_menit: FloatNonNeg


class Validasi(_M):
    lonjakan_mencurigakan_pct: Annotated[Decimal, Field(gt=0, le=1000)]
    jendela_kelengkapan_bar: IntPos
    maks_bar_hilang: IntNonNeg
    min_cakupan_universe_pct: Pct
    min_hari_kalender_historis_pct: Pct


NamaProvider = Literal["yfinance", "csv_manual"]


class Data(_M):
    provider_urutan: list[NamaProvider]
    folder_cache: StrictStr
    folder_manual: StrictStr
    mulai_unduh: date
    akhiran_yfinance: StrictStr
    simbol_ihsg: SimbolIhsg
    pengali_volume: Annotated[Decimal, Field(gt=0)]
    pengali_volume_terverifikasi: StrictBool
    jeda_data_final_menit: IntNonNeg
    jeda_antar_permintaan_detik: FloatNonNeg
    jeda_batas_permintaan_detik: FloatNonNeg
    maks_simbol_kena_batas_berturut: IntPos
    coba_lagi_tidak_ada_data_hari: IntNonNeg
    tunggu_data_hari_ini: TungguData
    bar_cek_ulang: IntPos
    toleransi_revisi_pct: Pct
    toleransi_revisi_volume_pct: Annotated[Decimal, Field(ge=0, le=1000)]
    min_bar_tumpang_tindih_sambungan: IntPos
    file_anomali_ok: StrictStr
    validasi: Validasi

    @field_validator("provider_urutan")
    @classmethod
    def _unik(cls, v):
        if not v:
            raise ValueError("minimal satu provider")
        if len(set(v)) != len(v):
            raise ValueError("provider tidak boleh berulang")
        return v


class Likuiditas(_M):
    periode_hari: IntPos
    nilai_transaksi_min_idr: Rupiah
    maks_hari_volume_nol: IntNonNeg
    maks_pct_order_dari_nilai_harian: PctPositif


class Universe(_M):
    file: StrictStr
    file_riwayat: StrictStr
    file_exclude: StrictStr
    file_portfolio: StrictStr
    min_bar_riwayat: IntPos


class Indikator(_M):
    atr_periode: IntPos


class KondisiPasar(_M):
    sma_pendek: IntPos
    sma_panjang: IntPos
    breadth_sma: IntPos
    breadth_boleh_min_pct: Pct
    breadth_jangan_maks_pct: Pct
    min_cakupan_breadth_pct: Pct
    min_saham_breadth: IntPos
    file_breadth_universe: StrictStr
    jangan_beli_paksa_jual: StrictBool


class StrategiPB(_M):
    aktif: StrictBool
    sma_cepat: IntPos
    sma_tren: IntPos
    sma_panjang: IntPos
    lookback_slope_sma_tren: IntPos
    lookback_sentuh_sma_cepat: IntPos
    maks_jarak_atas_sma_cepat_atr: float
    lookback_swing_low: IntPos
    penyangga_stop_atr: FloatNonNeg
    maks_jarak_stop_atr: FloatPos
    target_r: FloatPos
    stop_waktu_hari: IntPos


class StrategiBO(_M):
    aktif: StrictBool
    lookback_tertinggi: IntPos
    lookback_volume: IntPos
    min_rasio_volume: FloatPos
    min_posisi_close: Annotated[float, Field(ge=0, le=1)]
    maks_lewat_level_atr: FloatNonNeg
    sma_tren: IntPos
    sma_panjang: IntPos
    stop_atr: FloatPos
    trailing_atr: FloatPos
    stop_waktu_hari: IntPos | None


class StrategiMR(_M):
    aktif: StrictBool
    rsi_periode: IntPos
    rsi_maks: Annotated[float, Field(gt=0, le=100)]
    sma_tren: IntPos
    maks_turun_harian_atr: FloatNonNeg
    stop_atr: FloatPos
    sma_keluar: IntPos
    min_hari_tahan: IntPos
    stop_waktu_hari: IntPos


class StrategiAcak(_M):
    peluang_sinyal_pct: Annotated[float, Field(ge=0, le=100)]
    seed: StrictInt
    stop_atr: FloatPos
    target_r: FloatPos
    stop_waktu_hari: IntPos


class Strategi(_M):
    pb: StrategiPB
    bo: StrategiBO
    mr: StrategiMR
    acak: StrategiAcak


class BobotSkor(_M):
    sinyal: FloatNonNeg
    kekuatan_relatif: FloatNonNeg
    likuiditas: FloatNonNeg
    jarak_stop: FloatNonNeg

    @model_validator(mode="after")
    def _jumlah_100(self):
        total = Decimal(str(self.sinyal)) + Decimal(str(self.kekuatan_relatif)) + Decimal(
            str(self.likuiditas)
        ) + Decimal(str(self.jarak_stop))
        if total != Decimal(100):
            raise ValueError(f"jumlah bobot harus 100 (sekarang {total})")
        return self


class Skor(_M):
    bobot: BobotSkor
    rs_lookback: IntPos
    jarak_stop_ideal_pct: Annotated[float, Field(ge=0, le=100)]
    jarak_stop_buruk_pct: Annotated[float, Field(ge=0, le=100)]

    @model_validator(mode="after")
    def _urutan(self):
        if not self.jarak_stop_ideal_pct < self.jarak_stop_buruk_pct:
            raise ValueError("jarak_stop_ideal_pct harus < jarak_stop_buruk_pct")
        return self


class ModeBacktest(_M):
    modal_idr: Union[Literal["ikuti_akun"], RupiahPositif]
    maks_posisi: Union[Literal["ikuti_risiko"], IntPos]
    maks_per_sektor: Union[Literal["ikuti_risiko"], IntPos, None]
    alokasi_maks_pct: Union[Literal["ikuti_risiko", "ikuti_maks_posisi"], PctPositif]


class ModeBacktestSemua(_M):
    edge: ModeBacktest
    realistis: ModeBacktest


class Backtest(_M):
    mulai: date
    selesai: date | None
    holdout_mulai: date
    pakai_exclude: StrictBool
    maks_hari_tanpa_bar: IntPos
    potongan_data_berakhir_pct: Pct
    maks_pf_r_acak: FloatPos
    mode: ModeBacktestSemua

    @model_validator(mode="after")
    def _urutan(self):
        if not self.mulai < self.holdout_mulai:
            raise ValueError("backtest.mulai harus < holdout_mulai")
        if self.selesai is not None and not self.holdout_mulai < self.selesai:
            raise ValueError("backtest.selesai harus > holdout_mulai")
        return self


class Gerbang(_M):
    min_transaksi_holdout: IntNonNeg
    min_pf_r_holdout: FloatNonNeg
    min_ekspektansi_r_holdout: float
    min_pf_r_insample: FloatNonNeg
    min_transaksi_per_jendela: IntNonNeg
    min_pct_jendela_pf_di_atas_1: Annotated[float, Field(ge=0, le=100)]
    maks_drawdown_r: FloatPos
    kedaluwarsa_hari: IntNonNeg


FormatLaporan = Literal["terminal", "markdown", "html", "csv", "xlsx"]


class Laporan(_M):
    folder: StrictStr
    format: list[FormatLaporan]
    maks_kandidat_ditampilkan: IntNonNeg
    maks_baris_dikeluarkan: IntNonNeg
    bar_grafik: IntPos


class Telegram(_M):
    aktif: StrictBool
    lampirkan_html: StrictBool
    maks_lampiran_mb: FloatPos


class Notifikasi(_M):
    telegram: Telegram


class Jurnal(_M):
    file: StrictStr
    folder_ekspor: StrictStr
    tanggal_mulai_kertas: date | None
    maks_hari_tunggu_data: IntNonNeg
    min_transaksi_peringatan: IntPos
    persentil_peringatan: Annotated[float, Field(gt=0, lt=100)]
    bootstrap_iterasi: IntPos
    bootstrap_seed: StrictInt


class Operasional(_M):
    simpan_log_hari: IntPos
    batas_waktu_run_jam: FloatPos


class Config(_M):
    versi_config: StrictInt
    akun: Akun
    risiko: Risiko
    biaya: Biaya
    entry: Entry
    jadwal: Jadwal
    pasar: Pasar
    data: Data
    likuiditas: Likuiditas
    universe: Universe
    indikator: Indikator
    kondisi_pasar: KondisiPasar
    strategi: Strategi
    skor: Skor
    backtest: Backtest
    gerbang: Gerbang
    laporan: Laporan
    notifikasi: Notifikasi
    jurnal: Jurnal
    operasional: Operasional

    @field_validator("versi_config")
    @classmethod
    def _versi(cls, v):
        if v != 1:
            raise ValueError("config dari versi program lain (versi_config harus 1)")
        return v

    @model_validator(mode="after")
    def _silang(self):
        kp = self.kondisi_pasar
        if not kp.breadth_jangan_maks_pct <= kp.breadth_boleh_min_pct:
            raise ValueError(
                "kondisi_pasar.breadth_jangan_maks_pct harus <= breadth_boleh_min_pct"
            )
        return self


# ---------------------------------------------------------------------------
# Resolusi rujukan (SPEC §7 "Resolusi rujukan")
# ---------------------------------------------------------------------------

NamaKonteks = Literal["live", "edge", "realistis"]


@dataclass(frozen=True)
class KonteksRisiko:
    """Parameter portofolio yang sudah diresolusi untuk satu konteks."""

    nama: NamaKonteks
    modal_idr: Decimal
    maks_posisi: int
    maks_per_sektor: int | None
    alokasi_maks_pct: Decimal | None  # None = ikuti_maks_posisi (ekuitas ÷ maks_posisi)

    def alokasi_maks(self, ekuitas: Decimal) -> Decimal:
        """Alokasi maks per posisi. `ikuti_maks_posisi` = pembagian langsung (3.000.000 ÷ 3 = 1.000.000 tepat)."""
        ekuitas = Decimal(ekuitas)
        if self.alokasi_maks_pct is None:
            return ekuitas / Decimal(self.maks_posisi)
        return ekuitas * self.alokasi_maks_pct / Decimal(100)

    def uraian(self) -> str:
        alok = (
            "ekuitas ÷ maks_posisi"
            if self.alokasi_maks_pct is None
            else f"{self.alokasi_maks_pct}% ekuitas"
        )
        sektor = "tanpa batas" if self.maks_per_sektor is None else str(self.maks_per_sektor)
        modal = f"{self.modal_idr:,.0f}".replace(",", ".")
        return (
            f"{self.nama}: modal Rp {modal}, maks_posisi {self.maks_posisi}, "
            f"maks_per_sektor {sektor}, alokasi_maks {alok}"
        )


def resolusi_konteks(cfg: Config, nama: NamaKonteks) -> KonteksRisiko:
    r = cfg.risiko

    def alok_dari_risiko() -> Decimal | None:
        return None if r.alokasi_maks_pct == "ikuti_maks_posisi" else Decimal(r.alokasi_maks_pct)

    if nama == "live":
        return KonteksRisiko(
            nama="live",
            modal_idr=Decimal(cfg.akun.modal_awal_idr),
            maks_posisi=r.maks_posisi,
            maks_per_sektor=r.maks_per_sektor,
            alokasi_maks_pct=alok_dari_risiko(),
        )
    mode = getattr(cfg.backtest.mode, nama)
    modal = Decimal(cfg.akun.modal_awal_idr) if mode.modal_idr == "ikuti_akun" else Decimal(mode.modal_idr)
    maks_posisi = r.maks_posisi if mode.maks_posisi == "ikuti_risiko" else mode.maks_posisi
    maks_sektor = r.maks_per_sektor if mode.maks_per_sektor == "ikuti_risiko" else mode.maks_per_sektor
    if mode.alokasi_maks_pct == "ikuti_risiko":
        # rujukan berantai: nilai di `risiko` bisa berupa ikuti_maks_posisi, yang diresolusi
        # dalam konteks mode ini (maks_posisi milik mode).
        alok = alok_dari_risiko()
    elif mode.alokasi_maks_pct == "ikuti_maks_posisi":
        alok = None
    else:
        alok = Decimal(mode.alokasi_maks_pct)
    return KonteksRisiko(
        nama=nama, modal_idr=modal, maks_posisi=maks_posisi, maks_per_sektor=maks_sektor,
        alokasi_maks_pct=alok,
    )


# ---------------------------------------------------------------------------
# Pemuatan
# ---------------------------------------------------------------------------

_PESAN_JENIS = {
    "missing": "kunci wajib tidak ada",
    "extra_forbidden": "kunci tidak dikenal",
    "less_than_equal": "harus <= {le}",
    "less_than": "harus < {lt}",
    "greater_than_equal": "harus >= {ge}",
    "greater_than": "harus > {gt}",
    "int_type": "harus bilangan bulat",
    "int_parsing": "harus bilangan bulat",
    "int_from_float": "harus bilangan bulat",
    "bool_type": "harus true atau false",
    "string_type": "harus teks",
    "decimal_parsing": "harus angka",
    "decimal_type": "harus angka",
    "float_parsing": "harus angka",
    "float_type": "harus angka",
    "date_from_datetime_parsing": "harus tanggal YYYY-MM-DD",
    "date_parsing": "harus tanggal YYYY-MM-DD",
    "date_type": "harus tanggal YYYY-MM-DD",
    "list_type": "harus daftar (list)",
    "model_type": "harus pemetaan (kunci: nilai)",
    "dict_type": "harus pemetaan (kunci: nilai)",
}


def _terjemah(err: dict) -> str:
    jenis = err["type"]
    if jenis == "literal_error":
        harapan = (err.get("ctx") or {}).get("expected", "")
        return f"nilai harus salah satu dari {harapan}"
    pola = _PESAN_JENIS.get(jenis)
    if pola is None:
        return err["msg"]
    try:
        return pola.format(**(err.get("ctx") or {}))
    except (KeyError, IndexError):
        return pola


def _format_error(e: ValidationError) -> list[str]:
    hasil = []
    for err in e.errors():
        # Buang label union/tipe internal dari lokasi (mis. 'literal[...]', 'decimal').
        loc = [str(x) for x in err["loc"] if not re.match(r"^(literal\[|function-|constrained-|decimal$|int$|float$|str$|none$|is-instance)", str(x))]
        kunci = ".".join(loc) if loc else "(akar)"
        pesan = _terjemah(err)
        if pesan.startswith("Value error, "):
            pesan = pesan[len("Value error, "):]
        masukan = err.get("input")
        if err["type"] not in ("missing",) and not isinstance(masukan, dict) and masukan is not None:
            pesan = f"{pesan} (nilai: {masukan!r})"
        hasil.append(f"{kunci}: {pesan}")
    # Hapus duplikat dengan urutan tetap (error union bisa berulang per kunci).
    terlihat, unik = set(), []
    for h in hasil:
        if h not in terlihat:
            terlihat.add(h)
            unik.append(h)
    return unik


def validasi_dict(data: Any) -> Config:
    if not isinstance(data, dict):
        raise ConfigTidakValid(["berkas config kosong atau bukan pemetaan YAML"])
    try:
        return Config.model_validate(data)
    except ValidationError as e:
        raise ConfigTidakValid(_format_error(e)) from None


@dataclass(frozen=True)
class KonteksApp:
    """Config tervalidasi + akar proyek. Semua path config diselesaikan lewat `path()`."""

    cfg: Config
    akar: Path
    path_config: Path

    def path(self, relatif: str | Path) -> Path:
        p = Path(relatif)
        return p if p.is_absolute() else (self.akar / p)

    def konteks(self, nama: NamaKonteks) -> KonteksRisiko:
        return resolusi_konteks(self.cfg, nama)


def baca_yaml(path: Path) -> Any:
    try:
        with open(path, encoding="utf-8-sig") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigTidakValid([f"berkas config tidak ditemukan: {path}"]) from None
    except yaml.YAMLError as e:
        raise ConfigTidakValid([f"YAML tidak valid di {path}: {e}"]) from None


def muat_config(path_config: str | Path, akar: str | Path | None = None) -> KonteksApp:
    path_config = Path(path_config).resolve()
    data = baca_yaml(path_config)
    cfg = validasi_dict(data)
    akar_p = Path(akar).resolve() if akar is not None else path_config.parent
    return KonteksApp(cfg=cfg, akar=akar_p, path_config=path_config)


def batas_bar_final_jam(cfg: Config) -> time:
    """Jam batas bar final (SPEC §8.6), tanpa tanggal."""
    js = cfg.pasar.jadwal_sesi
    if js.terverifikasi and js.jam_penutupan:
        dasar = datetime.combine(date(2000, 1, 3), parse_jam(js.jam_penutupan))
        return (dasar + timedelta(minutes=cfg.data.jeda_data_final_menit)).time()
    return parse_jam(cfg.jadwal.jam_run)

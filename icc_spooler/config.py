"""Configuration for the ICC spooler prototype."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path

from .i18n import available_languages, tr

INTENT_NAMES = ("感知", "相对比色", "饱和度", "绝对比色")
INTENT_VALUES = {"感知": 0, "相对比色": 1, "饱和度": 2, "绝对比色": 3}

# RGB->CMYK color-management module. "lcms" = littleCMS (Pillow/ImageCms),
# "wcs" = Windows imaging CMM (mscms.dll, Windows-native numbers).
CMM_NAMES = ("lcms", "wcs")

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def default_config_path() -> Path:
    return PROJECT_ROOT / "config.json"


@dataclass
class ColorSettings:
    enabled: bool = True

    k_reduction: float = 0.0
    k_curve_enabled: bool = False
    k_curve_min: float = 0.0
    k_curve_max: float = 100.0
    k_curve_power: float = 1.0
    preserve_black: bool = True
    black_threshold: float = 95.0

    cmy_scale: list = field(default_factory=lambda: [1.0, 1.0, 1.0])
    c_gamma: float = 1.0
    m_gamma: float = 1.0
    y_gamma: float = 1.0
    k_gamma: float = 1.0

    gcr: float = 0.0
    ucr: float = 0.0
    rich_black: float = 0.0
    rich_black_threshold: float = 0.9

    tac_limit: float = 0.0
    tac_mode: str = "scale_cmy"

    rgb_to_cmyk: bool = False
    gray_to_k: bool = False
    # Near-neutral RGB (max-min channel spread <= neutral_threshold, 0..1) is
    # routed to K-only instead of the ICC profile, so grays stay neutral on any
    # printer rather than picking up the profile's cyan-dominant CMY cast.
    neutral_to_k: bool = False
    neutral_threshold: float = 0.04
    # Lower bound (0..1) for neutral_to_k. Colors darker than this keep the ICC
    # result: a tinted near-black (e.g. a very dark blue) must not be flattened
    # to a neutral K gray. A small value only excludes near-black cells.
    neutral_min: float = 0.05
    # When True, neutral_to_k only rewrites vector content; image pixels keep the
    # ICC conversion (photos generally should not be forced to a flat K-only).
    neutral_vector_only: bool = True
    # Vector text protection: text must not be printed as a single-channel K
    # halftone. Below 100% every tone is screened; small glyph strokes then turn
    # into a mesh because the screen cells are large relative to the stroke.
    #   - near-black text (tone >= 1 - text_solid_black_luma) -> solid K=100
    #   - text at or below text_small_size points           -> solid K=100
    #   - other neutral gray text (larger)                   -> CMY gray (no K)
    # Only text-showing operators are affected; graphics and images are not.
    # `text_dpi` converts the tracked text scale into points (px = pt * dpi/72).
    text_solid_black: bool = False
    text_solid_black_luma: float = 0.20
    text_small_size: float = 7.0
    text_dpi: float = 600.0
    process_images: bool = True
    target_icc: str = "JapanColor2001Uncoated.icc"
    input_rgb_icc: str = ""
    intent: str = "相对比色"
    cmm: str = "lcms"


@dataclass
class AppConfig:
    printer: str = "Brother MFC-L3750CDW BR-Script3"
    virtual_printer: str = "Brother ICC Virtual"
    virtual_port: str = r"C:\ICCSpool\in.prn"
    spool_dir: str = r"C:\ICCSpool"
    archive_dir: str = r"C:\ICCSpool\done"
    spool_pattern: str = "*.prn"
    poll_seconds: float = 1.0
    dry_run: bool = False

    auto_start_service: bool = True
    autostart: bool = False
    start_minimized: bool = False
    minimize_to_tray: bool = True
    log_file: str = ""
    # UI language. "en" is built in; other codes are loaded from
    # icc_spooler/lang/<code>.json. Restart the program to apply a change.
    lang: str = "en"

    color: ColorSettings = field(default_factory=ColorSettings)

    def log_path(self) -> Path:
        if self.log_file:
            return Path(self.log_file)
        return Path(self.spool_dir) / "icc_spooler.log"

    @classmethod
    def from_dict(cls, data: dict) -> "AppConfig":
        data = dict(data or {})
        color_data = data.pop("color", {}) or {}
        known = {f.name for f in fields(cls)}
        top = {k: v for k, v in data.items() if k in known}
        cfg = cls(**top)
        color_fields = {f.name for f in fields(ColorSettings)}
        merged = {f.name: getattr(cfg.color, f.name) for f in fields(ColorSettings)}
        merged.update({k: v for k, v in color_data.items() if k in color_fields})
        cfg.color = ColorSettings(**merged)
        return cfg

    @classmethod
    def load(cls, path: str | Path) -> "AppConfig":
        text = Path(path).read_text(encoding="utf-8")
        return cls.from_dict(json.loads(text))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def validate(self) -> list[str]:
        problems: list[str] = []
        c = self.color
        if self.lang not in available_languages():
            problems.append(tr('lang must be one of the available language codes.'))
        if not 0.0 <= float(c.k_reduction) <= 100.0:
            problems.append(tr('color.k_reduction must be within 0..100.'))
        if c.k_curve_enabled and not (float(c.k_curve_max) > float(c.k_curve_min)):
            problems.append(tr('k_curve_max of the dynamic K curve must be greater than k_curve_min.'))
        if len(c.cmy_scale) != 3:
            problems.append(tr('color.cmy_scale must be 3 numbers [C, M, Y].'))
        if c.tac_limit and not (0.0 < float(c.tac_limit) <= 4.0):
            problems.append(tr('color.tac_limit must be empty or within 0.0..4.0 (e.g. 3.0 = 300%).'))
        if c.tac_mode not in ("scale_cmy", "scale_all"):
            problems.append(tr('color.tac_mode must be scale_cmy or scale_all.'))
        if c.intent not in INTENT_NAMES:
            problems.append(tr('color.intent must be one of: Perceptual / Relative Colorimetric / Saturation / Absolute Colorimetric.'))
        if c.cmm not in CMM_NAMES:
            problems.append(tr('color.cmm must be lcms or wcs.'))
        if not 0.0 <= float(c.neutral_threshold) <= 1.0:
            problems.append(tr('color.neutral_threshold must be within 0..1 (e.g. 0.04 ≈ 10/255).'))
        if not 0.0 <= float(c.neutral_min) <= 1.0:
            problems.append(tr('color.neutral_min must be within 0..1 (e.g. 0.05).'))
        if not 0.0 <= float(c.text_solid_black_luma) <= 1.0:
            problems.append(tr('color.text_solid_black_luma must be within 0..1.'))
        if float(c.text_small_size) < 0.0:
            problems.append(tr('color.text_small_size must be >= 0.'))
        if float(c.text_dpi) <= 0.0:
            problems.append(tr('color.text_dpi must be > 0.'))
        if (c.rgb_to_cmyk or c.enabled) and c.target_icc:
            if not Path(c.target_icc).is_file():
                problems.append(tr('Target ICC not found: {icc} (required for RGB→CMYK).').format(
                    icc=c.target_icc))
        return problems

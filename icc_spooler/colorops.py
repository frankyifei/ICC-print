"""Color transform engine: CMYK numeric controls plus optional ICC RGB->CMYK."""
from __future__ import annotations

from pathlib import Path

from .config import ColorSettings, INTENT_VALUES
from .i18n import tr

# littleCMS2 transform flags, matching Ghostscript's gsicc_lcms2.c (gscms_get_link):
# it always sets cmsFLAGS_HIGHRESPRECALC and adds cmsFLAGS_BLACKPOINTCOMPENSATION
# when its BlackPtComp option is on (the GS default).
_FLAG_BLACKPOINTCOMPENSATION = 0x2000
_FLAG_HIGHRESPRECALC = 0x0400


def clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def fmt_num(value: float) -> bytes:
    if value == 0.0 or abs(value) < 1e-9:
        return b"0"
    s = f"{float(value):.6f}".rstrip("0").rstrip(".")
    if s in ("", "-0", "-"):
        s = "0"
    return s.encode("ascii")


def adjust_k_value(value: float, reduction: float, preserve_black: bool = True,
                   black_threshold: float = 95.0, curve_enabled: bool = False,
                   curve_min: float = 0.0, curve_max: float = 100.0,
                   curve_power: float = 1.0) -> float:
    k = max(0.0, min(1.0, float(value)))
    reduction = max(0.0, min(99.9, float(reduction)))
    if preserve_black and k * 100.0 >= float(black_threshold):
        return k
    if not curve_enabled:
        return max(0.0, k * (1.0 - reduction / 100.0))
    lo = max(0.0, min(100.0, float(curve_min)))
    hi = max(0.0, min(100.0, float(curve_max)))
    power = max(0.1, min(5.0, float(curve_power)))
    if hi <= lo:
        return k
    k_pct = k * 100.0
    if k_pct <= lo:
        curve_factor = 1.0
    elif k_pct >= hi:
        curve_factor = 0.0
    else:
        curve_factor = ((hi - k_pct) / (hi - lo)) ** power
    proportional_reduction = min(0.999, reduction / 100.0 * curve_factor)
    return k * (1.0 - proportional_reduction)


class _LcmsBackend:
    """RGB->CMYK through littleCMS (Pillow/ImageCms), 8 bits per channel."""

    name = "lcms"

    def __init__(self, settings: ColorSettings):
        from PIL import ImageCms
        if not settings.target_icc or not Path(settings.target_icc).is_file():
            raise RuntimeError(tr('RGB→CMYK requires a valid target ICC file.'))
        dst = ImageCms.getOpenProfile(str(Path(settings.target_icc).resolve()))
        if settings.input_rgb_icc and Path(settings.input_rgb_icc).is_file():
            src = ImageCms.getOpenProfile(str(Path(settings.input_rgb_icc).resolve()))
        else:
            src = ImageCms.createProfile("sRGB")
        intent = INTENT_VALUES.get(settings.intent, 1)
        flags = _FLAG_HIGHRESPRECALC | _FLAG_BLACKPOINTCOMPENSATION
        self._t = ImageCms.buildTransform(src, dst, "RGB", "CMYK", intent, flags)

    def pixel(self, r: float, g: float, b: float):
        from PIL import Image, ImageCms
        out = ImageCms.applyTransform(
            Image.new("RGB", (1, 1), (round(r * 255), round(g * 255), round(b * 255))), self._t)
        c, m, y, k = out.getpixel((0, 0))
        return (c / 255.0, m / 255.0, y / 255.0, k / 255.0)

    def buffer(self, rgb: bytes, width: int, height: int) -> bytes:
        from PIL import Image, ImageCms
        return ImageCms.applyTransform(
            Image.frombytes("RGB", (int(width), int(height)), rgb), self._t).tobytes()

    def close(self) -> None:
        self._t = None


class _WcsBackend:
    """RGB->CMYK through the Windows imaging CMM (mscms.dll)."""

    name = "wcs"

    def __init__(self, settings: ColorSettings):
        from .wcscmm import WcsRgbToCmyk
        self._t = WcsRgbToCmyk(settings.target_icc,
                               INTENT_VALUES.get(settings.intent, 1),
                               settings.input_rgb_icc)

    def pixel(self, r: float, g: float, b: float):
        return self._t.convert_pixel(r, g, b)

    def buffer(self, rgb: bytes, width: int, height: int) -> bytes:
        return self._t.convert_buffer(rgb, int(width), int(height))

    def close(self) -> None:
        self._t.close()


class ColorEngine:
    def __init__(self, settings: ColorSettings):
        self.s = settings
        self._rgb_backend = None
        self._rgb_failed = False
        self._rgb_cache: dict = {}
        self._stats = {"cmyk": 0, "rgb": 0, "gray": 0, "images": 0, "images_skipped": 0}

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    def has_active_color(self) -> bool:
        s = self.s
        if not s.enabled:
            return False
        if s.rgb_to_cmyk or s.gray_to_k or s.neutral_to_k:
            return True
        if (s.k_reduction > 0.0 or s.k_curve_enabled or s.gcr > 0.0 or s.ucr > 0.0
                or s.rich_black > 0.0 or s.tac_limit > 0.0):
            return True
        if any(abs(float(x) - 1.0) > 1e-9 for x in (list(s.cmy_scale) + [1.0, 1.0, 1.0])[:3]):
            return True
        if any(abs(float(g) - 1.0) > 1e-9 for g in (s.c_gamma, s.m_gamma, s.y_gamma, s.k_gamma)):
            return True
        return False

    def has_cmyk_adjust(self) -> bool:
        """True when any CMYK-side numeric stage would change pixel values.

        Lets the image path skip the per-pixel pipeline when only RGB->CMYK is
        active (the common case), which keeps pure-Python conversion fast.
        """
        s = self.s
        if (s.k_reduction > 0.0 or s.k_curve_enabled or s.gcr > 0.0 or s.ucr > 0.0
                or s.rich_black > 0.0 or s.tac_limit > 0.0):
            return True
        if any(abs(float(x) - 1.0) > 1e-9 for x in (list(s.cmy_scale) + [1.0, 1.0, 1.0])[:3]):
            return True
        if any(abs(float(g) - 1.0) > 1e-9 for g in (s.c_gamma, s.m_gamma, s.y_gamma, s.k_gamma)):
            return True
        return False

    def wants_rgb(self) -> bool:
        """Convert RGB content when explicitly requested or when any CMYK
        adjustment is active (auto mode, so K/CMY settings always take effect)."""
        return bool(self.s.enabled and (self.s.rgb_to_cmyk or self.s.neutral_to_k
                                        or self.has_cmyk_adjust()))

    def wants_neutral(self, for_image: bool = False) -> bool:
        """Route near-neutral RGB to K-only (neutral_to_k).

        `for_image` is True when the caller handles image pixels; when
        `neutral_vector_only` is set those are left to the ICC profile.
        """
        if not (self.s.enabled and self.s.neutral_to_k):
            return False
        if for_image and self.s.neutral_vector_only:
            return False
        return True

    def is_neutral(self, r: float, g: float, b: float) -> bool:
        """Near-neutral AND not near-black.

        The channel spread must be within `neutral_threshold`, and the lightness
        must be at least `neutral_min`. Near-black cells are left to the ICC
        profile (which keeps their tint) instead of being flattened to a neutral
        K gray; the gray ramp itself stays well above `neutral_min`.
        """
        if (max(r, g, b) - min(r, g, b)) > float(self.s.neutral_threshold) + 1e-9:
            return False
        return (r + g + b) / 3.0 >= float(self.s.neutral_min) - 1e-9

    def neutral_to_cmyk(self, r: float, g: float, b: float):
        gray = (clamp01(r) + clamp01(g) + clamp01(b)) / 3.0
        return (0.0, 0.0, 0.0, 1.0 - gray)

    def wants_gray(self) -> bool:
        return bool(self.s.enabled and (self.s.gray_to_k or self.has_cmyk_adjust()))

    def wants_images(self) -> bool:
        return bool(self.s.enabled and (self.s.process_images or self.has_cmyk_adjust()))

    def rgb_backend(self):
        """Return the active RGB->CMYK backend, or None when nothing usable.

        Honors `color.cmm`: "wcs" uses the Windows CMM, falling back to
        littleCMS if that engine is unavailable or fails to build.
        """
        if self._rgb_backend is not None:
            return self._rgb_backend
        if self._rgb_failed:
            return None
        try:
            if self.s.cmm == "wcs":
                try:
                    self._rgb_backend = _WcsBackend(self.s)
                except Exception:
                    self._rgb_backend = _LcmsBackend(self.s)
            else:
                self._rgb_backend = _LcmsBackend(self.s)
        except Exception:
            self._rgb_failed = True
            return None
        return self._rgb_backend

    def rgb_to_cmyk(self, r: float, g: float, b: float):
        key = (round(clamp01(r) * 255), round(clamp01(g) * 255), round(clamp01(b) * 255))
        cached = self._rgb_cache.get(key)
        if cached is not None:
            return cached
        if self.wants_neutral():
            rf, gf, bf = key[0] / 255.0, key[1] / 255.0, key[2] / 255.0
            if self.is_neutral(rf, gf, bf):
                result = self.neutral_to_cmyk(rf, gf, bf)
                self._rgb_cache[key] = result
                return result
        backend = self.rgb_backend()
        if backend is None:
            # No usable ICC: fall back to the same naive C=1-R, M=1-G, Y=1-B, K=0
            # used by the image path, so vector documents still get adjusted.
            result = (1.0 - key[0] / 255.0, 1.0 - key[1] / 255.0,
                      1.0 - key[2] / 255.0, 0.0)
        else:
            result = tuple(clamp01(v) for v in
                           backend.pixel(key[0] / 255.0, key[1] / 255.0, key[2] / 255.0))
        self._rgb_cache[key] = result
        return result

    def gray_to_cmyk(self, gray: float):
        g = clamp01(gray)
        return (0.0, 0.0, 0.0, 1.0 - g)

    def transform_cmyk(self, c: float, m: float, y: float, k: float):
        s = self.s
        c, m, y, k = clamp01(c), clamp01(m), clamp01(y), clamp01(k)

        if s.gcr > 0.0:
            gray = min(c, m, y)
            amount = clamp01(s.gcr) * gray
            c -= amount
            m -= amount
            y -= amount
            k = clamp01(k + amount)

        if s.ucr > 0.0:
            gray = min(c, m, y)
            amount = clamp01(s.ucr) * gray
            c -= amount
            m -= amount
            y -= amount
            k = clamp01(k + amount)

        c, m, y = clamp01(c), clamp01(m), clamp01(y)

        if s.k_reduction > 0.0 or s.k_curve_enabled:
            k = adjust_k_value(k, s.k_reduction, s.preserve_black, s.black_threshold,
                               s.k_curve_enabled, s.k_curve_min, s.k_curve_max, s.k_curve_power)

        if s.rich_black > 0.0 and s.rich_black_threshold < 1.0 and k >= s.rich_black_threshold:
            span = (k - s.rich_black_threshold) / max(1e-9, 1.0 - s.rich_black_threshold)
            add = clamp01(s.rich_black) * span
            c += add * 0.6
            m += add * 0.4
            y += add * 0.4

        sc, sm, sy = (list(s.cmy_scale) + [1.0, 1.0, 1.0])[:3]
        c = clamp01(c * float(sc))
        m = clamp01(m * float(sm))
        y = clamp01(y * float(sy))

        if s.c_gamma and s.c_gamma != 1.0:
            c = clamp01(c ** (1.0 / max(1e-6, float(s.c_gamma))))
        if s.m_gamma and s.m_gamma != 1.0:
            m = clamp01(m ** (1.0 / max(1e-6, float(s.m_gamma))))
        if s.y_gamma and s.y_gamma != 1.0:
            y = clamp01(y ** (1.0 / max(1e-6, float(s.y_gamma))))
        if s.k_gamma and s.k_gamma != 1.0:
            k = clamp01(k ** (1.0 / max(1e-6, float(s.k_gamma))))

        if s.tac_limit > 0.0:
            limit = float(s.tac_limit)
            total = c + m + y + k
            if total > limit:
                if s.tac_mode == "scale_all":
                    f = limit / total
                    c *= f
                    m *= f
                    y *= f
                    k *= f
                else:
                    cmy = c + m + y
                    allowed = max(0.0, limit - k)
                    if cmy > allowed and cmy > 0.0:
                        f = allowed / cmy
                        c *= f
                        m *= f
                        y *= f
                    if c + m + y + k > limit:
                        k = max(0.0, limit - (c + m + y))

        return clamp01(c), clamp01(m), clamp01(y), clamp01(k)

"""Generate a small PostScript test page with CMYK/RGB/gray swatches."""
from __future__ import annotations

SWATCHES = [
    ("CMYK mid", "0.1 0.6 0.8 0.2 setcmykcolor"),
    ("CMYK light", "0.05 0.15 0.35 0.05 setcmykcolor"),
    ("Pure black", "0 0 0 1 setcmykcolor"),
    ("Near black", "0 0 0 0.96 setcmykcolor"),
    ("Gray 50%", "0.5 setgray"),
    ("RGB red", "0.9 0.2 0.1 setrgbcolor"),
    ("RGB blue", "0.1 0.3 0.85 setrgbcolor"),
    ("DeviceCMYK setcolor", "[/DeviceCMYK] setcolorspace 0.4 0.1 0.2 0.5 setcolor"),
]


def _swatch(x: float, y: float, w: float, h: float, color_op: str) -> list[str]:
    return [
        f"{x} {y} moveto {w} 0 rlineto 0 {h} rlineto {-w} 0 rlineto closepath",
        color_op,
        "fill",
    ]


def sample_ps() -> bytes:
    lines = [
        "%!PS-Adobe-3.0",
        "%%Creator: icc_spooler sample",
        "%%Pages: 1",
        "%%EndComments",
        "%%Page: 1 1",
        "<< /PageSize [595 842] >> setpagedevice",
    ]
    y = 780.0
    for label, op in SWATCHES:
        lines.append(f"% label: {label}")
        lines.extend(_swatch(60, y, 300, 40, op))
        y -= 60
    lines.append("showpage")
    lines.append("%%EOF")
    return ("\n".join(lines) + "\n").encode("latin-1")


if __name__ == "__main__":
    import sys
    from pathlib import Path

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("sample.ps")
    target.write_bytes(sample_ps())
    print(f"wrote {target}")

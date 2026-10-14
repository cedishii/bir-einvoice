"""Render assets/micronet_logo.pdf (the company's original artwork) to
assets/logo.png, trimmed to the artwork, for the app and the printed invoice.

Usage (one-off, needs PyMuPDF which is not an app dependency):
    .venv\\Scripts\\pip install pymupdf
    .venv\\Scripts\\python tools\\prepare_logo.py
"""
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "assets" / "micronet_logo.pdf"
TARGET = ROOT / "assets" / "logo.png"
WIDTH_PX = 900  # ~3 inches at 300 dpi, plenty for the printed header
MARGIN_PT = 6   # white kept around the artwork


def artwork_rect(page: pymupdf.Page) -> pymupdf.Rect:
    """Page-space rectangle around everything that isn't white."""
    preview = page.get_pixmap(dpi=36, alpha=False)
    w, h, n, s = preview.width, preview.height, preview.n, preview.stride
    data = preview.samples
    xs, ys = [], []
    for y in range(h):
        for x in range(w):
            i = y * s + x * n
            if min(data[i:i + 3]) < 240:
                xs.append(x)
                ys.append(y)
    scale = page.rect.width / w
    return pymupdf.Rect(
        min(xs) * scale - MARGIN_PT, min(ys) * scale - MARGIN_PT,
        (max(xs) + 1) * scale + MARGIN_PT, (max(ys) + 1) * scale + MARGIN_PT,
    ) & page.rect


def main() -> None:
    page = pymupdf.open(SOURCE)[0]
    clip = artwork_rect(page)
    zoom = WIDTH_PX / clip.width
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)
    pix.save(TARGET)
    print(f"wrote {TARGET} ({pix.width}x{pix.height})")


if __name__ == "__main__":
    main()

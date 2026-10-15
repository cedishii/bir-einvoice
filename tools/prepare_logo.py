"""Make assets/logo.png and assets/app.ico from the company logo image
(assets/micronet_logo.jpg).

The logo is trimmed to the artwork (white margins and any thin frame around
the image edge are cut off), keeps a little white around it, and is saved
at a width that prints sharply on the invoice. The app icon is the logo on a
white square.

Usage:
    .venv\\Scripts\\python tools\\prepare_logo.py
"""
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "assets" / "micronet_logo.jpg"
LOGO = ROOT / "assets" / "logo.png"
ICON = ROOT / "assets" / "app.ico"
WIDTH_PX = 900      # ~3 inches at 300 dpi, plenty for the printed header
EDGE_PX = 4         # a frame line this close to the image edge is not artwork
MARGIN_PX = 12      # white kept around the artwork
ICON_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def artwork_box(img: Image.Image) -> tuple[int, int, int, int]:
    """Box around everything that isn't (near) white, ignoring the outer edge."""
    gray = img.convert("L")
    w, h = gray.size
    inner = gray.crop((EDGE_PX, EDGE_PX, w - EDGE_PX, h - EDGE_PX))
    mask = inner.point(lambda v: 255 if v < 235 else 0)
    left, top, right, bottom = mask.getbbox()
    return (max(0, left + EDGE_PX - MARGIN_PX), max(0, top + EDGE_PX - MARGIN_PX),
            min(w, right + EDGE_PX + MARGIN_PX), min(h, bottom + EDGE_PX + MARGIN_PX))


def make_logo() -> Image.Image:
    img = Image.open(SOURCE).convert("RGB")
    logo = img.crop(artwork_box(img))
    logo = logo.resize((WIDTH_PX, round(logo.height * WIDTH_PX / logo.width)), Image.LANCZOS)
    logo.save(LOGO, optimize=True)
    return logo


def make_icon(logo: Image.Image) -> None:
    side = max(logo.size)
    square = Image.new("RGB", (side, side), (255, 255, 255))
    square.paste(logo, ((side - logo.width) // 2, (side - logo.height) // 2))
    square.save(ICON, sizes=ICON_SIZES)


def main() -> None:
    logo = make_logo()
    make_icon(logo)
    print(f"wrote {LOGO} ({logo.width}x{logo.height}) and {ICON}")


if __name__ == "__main__":
    main()

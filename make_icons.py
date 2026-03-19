"""
Genererer enkle PNG-ikoner for PWA (Oslo Screener hjemskjerm-ikon).
Krever Pillow. Kjøres én gang under deploy.
"""
from PIL import Image, ImageDraw, ImageFont
import pathlib

SIZES = [192, 512]
BG = (26, 35, 50)       # --surface
TEXT_COLOR = (34, 197, 94)  # --buy (grønn)


def make_icon(size: int, out_path: pathlib.Path) -> None:
    img = Image.new("RGB", (size, size), color=BG)
    draw = ImageDraw.Draw(img)

    # Tegn en enkel "OS"-tekst sentrert
    label = "OS"
    font_size = size // 3

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except Exception:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), label, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - w) // 2 - bbox[0]
    y = (size - h) // 2 - bbox[1]
    draw.text((x, y), label, fill=TEXT_COLOR, font=font)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    print(f"Laget {out_path} ({size}x{size})")


if __name__ == "__main__":
    for s in SIZES:
        make_icon(s, pathlib.Path(f"site/icon-{s}.png"))

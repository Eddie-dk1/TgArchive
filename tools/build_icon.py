"""Render the app's existing archive-box mark into a macOS multi-resolution icon."""
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent


def build_icon():
    scale = 2
    image = Image.new('RGBA', (1024*scale,1024*scale))
    draw = ImageDraw.Draw(image)
    box = lambda points: tuple(int(x*scale) for x in points)
    draw.rounded_rectangle(box((38,38,986,986)), radius=210*scale, fill='#17212B')
    draw.rounded_rectangle(box((100,100,924,924)), radius=160*scale, fill='#223B52')
    draw.rounded_rectangle(box((270,430,754,740)), radius=14*scale, outline='#80B5E4', width=34*scale)
    draw.rounded_rectangle(box((230,290,794,438)), radius=14*scale, fill='#223B52', outline='#80B5E4', width=34*scale)
    draw.rounded_rectangle(box((434,536,590,574)), radius=8*scale, fill='#80B5E4')
    image = image.resize((1024,1024), Image.Resampling.LANCZOS)
    assets = ROOT / 'assets'
    assets.mkdir(exist_ok=True)
    image.save(assets/'archive.icns', format='ICNS')


if __name__ == '__main__':
    build_icon()

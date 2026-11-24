"""Genera assets/tracciato.ico (pianta stilizzata con punti della nuvola, colori AD). Usato da build.bat."""
from pathlib import Path
from PIL import Image, ImageDraw

out = Path(__file__).resolve().parents[1] / "assets" / "tracciato.ico"
out.parent.mkdir(exist_ok=True)
S, w, m = 256, 10, 36
im = Image.new("RGBA", (S, S), (17, 17, 17, 255))
d = ImageDraw.Draw(im)
d.rectangle([m, m, S - m, S - m], outline="white", width=w)          # perimetro
d.line([(S // 2, m), (S // 2, S // 2 - 18)], fill="white", width=w)  # tramezzo con porta
d.line([(S // 2, S // 2 + 30), (S // 2, S - m)], fill="white", width=w)
d.line([(m, S // 2 + 30), (S // 2 - 40, S // 2 + 30)], fill="white", width=w)
for x, y in [(80, 80), (100, 70), (170, 90), (190, 180), (150, 200), (70, 170)]:
    d.ellipse([x - 5, y - 5, x + 5, y + 5], fill=(154, 152, 147, 255))  # punti della nuvola
im.save(out, sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
print(f"Icona: {out}")

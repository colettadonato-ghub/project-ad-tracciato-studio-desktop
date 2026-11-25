"""Tavole PNG nello stile di AD Studio Tecnico: inchiostro su carta, linee sottili, cartiglio.

Ogni elaborato (sagoma, pianta di livello, prospetto, sezione) è un'immagine raster in metri
(numpy, valori 0 = nero … 1 = carta) più le annotazioni vettoriali disegnate da matplotlib.
La tavola è in scala 1:S su formato A3…A0 scelto in automatico, come il PDF della pianta.
"""
import logging
import math
from datetime import datetime

import numpy as np

log = logging.getLogger("tracciato.render")

INK = "#111111"
GRAFITE = "#2E2E2E"
MATITA = "#5C5C5C"
TRACCIA = "#9A9893"
LINEA = "#E4E2DD"
CARTA = "#FFFFFF"
INFO = "#1F4FA3"
PAPERS = [("A3", 420, 297), ("A2", 594, 420), ("A1", 841, 594), ("A0", 1189, 841)]
DPI = 300            # risoluzione di stampa dei PNG
PAPER_CUSTOM = "Fuori formato"   # foglio grande quanto il disegno (es. scala 1:1)
MAX_PNG_PIXELS = 70_000_000   # tetto: oltre, il dpi scende per non esaurire la memoria (A1/A0)


def png_dpi(w_mm, h_mm, dpi=DPI, max_px=MAX_PNG_PIXELS):
    """DPI del PNG: `dpi`, ridotto solo se la tavola supererebbe `max_px` pixel."""
    px = (w_mm / 25.4 * dpi) * (h_mm / 25.4 * dpi)
    return round(dpi * math.sqrt(max_px / px), 2) if px > max_px else dpi   # senza arrotondare: metadati dpi esatti


def _paper(dw, dh):
    """(nome, larghezza, altezza, fattore scritte). Se il disegno non entra in A0 il foglio è fuori formato,
    grande quanto il disegno (a 1:1 = dimensioni reali) e scritte/linee crescono in proporzione."""
    for name, pw, ph in PAPERS:
        if dw <= pw - 24 and dh <= ph - 70:
            return name, pw, ph, 1.0
    ann = max(dw / (PAPERS[-1][1] - 24), dh / (PAPERS[-1][2] - 70), 1.0)
    return PAPER_CUSTOM, dw + 24 * ann, dh + 70 * ann, ann


def _grow(ax, k):
    if k <= 1:
        return
    for t in ax.texts:
        t.set_fontsize(t.get_fontsize() * k)
        ap = getattr(t, "arrow_patch", None)
        if ap is not None:
            ap.set_linewidth(ap.get_linewidth() * k)
    for ln in ax.lines:
        ln.set_linewidth(ln.get_linewidth() * k)
    for pt in ax.patches:
        pt.set_linewidth(pt.get_linewidth() * k)


def nice_step(length_m):
    for s in (1, 2, 5, 10, 20, 50, 100):
        if length_m / s <= 6:
            return s
    return 200


def sheet(path_png, img, extent, scale, title, subtitle, info, draw=None, margin=1.6, pdf_path=None):
    """Compone e salva la tavola.

    img      array (H, W) o (H, W, 3) 0..1, riga 0 = coordinata minima (origin='lower')
    extent   (x0, x1, y0, y1) in metri
    draw     funzione draw(ax) per le annotazioni in coordinate metriche
    Ritorna (formato carta, dpi).
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import cv2

    x0, x1, y0, y1 = extent
    S = scale
    dw, dh = (x1 - x0 + 2 * margin) * 1000 / S, (y1 - y0 + 2 * margin) * 1000 / S
    paper, W_mm, H_mm, ann = _paper(dw, dh)
    dpi = png_dpi(W_mm, H_mm)
    if paper == PAPER_CUSTOM:
        log.info(f"Tavola fuori formato a 1:{S}: {W_mm / 1000:.2f} × {H_mm / 1000:.2f} m, PNG a {dpi:g} dpi "
                 f"(misure reali: i dpi nei metadati restituiscono le dimensioni in scala)")

    # il raster può essere più fitto dei pixel della tavola: tengo i punti scuri (min) prima di ridurre
    out_px_per_m = dw / (x1 - x0 + 2 * margin) * dpi / 25.4
    px_per_m = img.shape[1] / max(x1 - x0, 1e-6)
    f = px_per_m / out_px_per_m
    if f > 1.25:
        k = int(math.ceil(f))
        img = cv2.erode(img.astype(np.float32), np.ones((k, k), np.uint8))

    fig = plt.figure(figsize=(W_mm / 25.4, H_mm / 25.4), facecolor=CARTA)
    left = (W_mm - dw) / 2 / W_mm
    bottom = (H_mm - dh + 38 * ann) / 2 / H_mm
    ax = fig.add_axes([left, bottom, dw / W_mm, dh / H_mm])
    ax.set_xlim(x0 - margin, x1 + margin)
    ax.set_ylim(y0 - margin, y1 + margin)
    ax.set_aspect("equal")
    ax.axis("off")
    cmap = "gray" if img.ndim == 2 else None
    ax.imshow(img, extent=(x0, x1, y0, y1), origin="lower", cmap=cmap, vmin=0, vmax=1,
              interpolation="antialiased", interpolation_stage="rgba", zorder=1)
    if draw:
        draw(ax)
    _grow(ax, ann)

    # cornice sottile della tavola
    fr = fig.add_axes([6 * ann / W_mm, 6 * ann / H_mm, 1 - 12 * ann / W_mm, 1 - 12 * ann / H_mm])
    fr.set_xticks([]); fr.set_yticks([])
    fr.patch.set_alpha(0)
    for s in fr.spines.values():
        s.set_linewidth(0.6 * ann); s.set_color(INK)
    fr.set_zorder(-1)

    # cartiglio
    tb = fig.add_axes([(W_mm - 196 * ann) / W_mm, 10 * ann / H_mm, 184 * ann / W_mm, 28 * ann / H_mm])
    tb.set_xticks([]); tb.set_yticks([])
    for s in tb.spines.values():
        s.set_linewidth(0.6 * ann); s.set_color(INK)
    tb.axvline(0.27, color=INK, lw=0.6 * ann)
    tb.text(0.03, 0.70, "AD", fontsize=15 * ann, weight="light", family="DejaVu Sans", transform=tb.transAxes)
    tb.text(0.03, 0.44, "STUDIO TECNICO", fontsize=6 * ann, family="DejaVu Sans Mono", transform=tb.transAxes)
    tb.text(0.03, 0.12, datetime.now().strftime("%d.%m.%Y"), fontsize=6 * ann, family="DejaVu Sans Mono",
            color=MATITA, transform=tb.transAxes)
    tb.text(0.30, 0.72, title, fontsize=10 * ann, weight="bold", transform=tb.transAxes)
    tb.text(0.30, 0.44, f"{subtitle}  ·  scala 1:{S}  ·  {paper}", fontsize=7 * ann, transform=tb.transAxes)
    tb.text(0.30, 0.14, info, fontsize=6 * ann, color=GRAFITE, family="DejaVu Sans Mono", transform=tb.transAxes)

    fig.savefig(path_png, dpi=dpi, facecolor=CARTA)
    if pdf_path:
        fig.savefig(pdf_path, facecolor=CARTA)
    plt.close(fig)
    return paper, dpi


# ------------------------------------------------------------------ annotazioni ricorrenti
def scale_bar(ax, x, y, length_hint):
    step = nice_step(length_hint)
    n = 5
    seg = step / n
    import matplotlib.pyplot as plt
    for i in range(n):
        ax.add_patch(plt.Rectangle((x + i * seg, y), seg, step * 0.012 + 0.04,
                                   fc=INK if i % 2 == 0 else CARTA, ec=INK, lw=0.4, zorder=5))
    for i in (0, n):
        ax.text(x + i * seg, y - 0.25 - step * 0.02, f"{i * seg:g}" + (" m" if i == n else ""),
                fontsize=5.5, ha="center", va="top", family="DejaVu Sans Mono", zorder=5)


def dim_h(ax, xa, xb, y, text=None, above=False):
    ax.annotate("", (xa, y), (xb, y), arrowprops=dict(arrowstyle="<->", lw=0.5, color=INK,
                                                       shrinkA=0, shrinkB=0), zorder=6)
    for xx in (xa, xb):
        ax.plot([xx, xx], [y - 0.12, y + 0.12], color=INK, lw=0.5, zorder=6)
    ax.text((xa + xb) / 2, y + (0.12 if above else -0.14), text or f"{abs(xb - xa):.2f}", fontsize=6,
            ha="center", va="bottom" if above else "top", family="DejaVu Sans Mono", zorder=6)


def dim_v(ax, x, ya, yb, text=None):
    ax.annotate("", (x, ya), (x, yb), arrowprops=dict(arrowstyle="<->", lw=0.5, color=INK,
                                                       shrinkA=0, shrinkB=0), zorder=6)
    for yy in (ya, yb):
        ax.plot([x - 0.12, x + 0.12], [yy, yy], color=INK, lw=0.5, zorder=6)
    ax.text(x - 0.16, (ya + yb) / 2, text or f"{abs(yb - ya):.2f}", fontsize=6, ha="right", va="center",
            rotation=90, family="DejaVu Sans Mono", zorder=6)


def level_line(ax, xa, xb, z, label):
    """Linea di livello tratteggiata con quota e nome (sezioni e prospetti)."""
    ax.plot([xa, xb], [z, z], color=MATITA, lw=0.5, ls=(0, (6, 3)), zorder=4)
    # triangolo di quota
    ax.plot([xb + 0.15, xb + 0.35, xb + 0.55, xb + 0.15], [z + 0.25, z, z + 0.25, z + 0.25], color=INK,
            lw=0.5, zorder=6)
    ax.text(xb + 0.7, z + 0.04, f"{'+' if z > 0.004 else ('−' if z < -0.004 else '±')}{abs(z):.2f}",
            fontsize=6.5, ha="left", va="bottom", family="DejaVu Sans Mono", zorder=6)
    if label:
        ax.text(xb + 0.7, z - 0.06, label, fontsize=5.5, ha="left", va="top", color=MATITA, zorder=6)


def section_mark(ax, p1, p2, n, label, extend=0.8):
    """Traccia di sezione/prospetto sulla pianta: linea tratto-punto, frecce verso la vista, lettere."""
    p1, p2, n = np.asarray(p1, float), np.asarray(p2, float), np.asarray(n, float)
    u = (p2 - p1) / max(np.linalg.norm(p2 - p1), 1e-9)
    a, b = p1 - u * extend, p2 + u * extend
    ax.plot([a[0], b[0]], [a[1], b[1]], color=INFO, lw=0.6, ls=(0, (8, 2, 1, 2)), zorder=7)
    for q in (a, b):
        ax.annotate("", q + n * 0.7, q, arrowprops=dict(arrowstyle="-|>", lw=0.8, color=INFO,
                                                        shrinkA=0, shrinkB=0), zorder=7)
        ax.text(*(q - n * 0.35 - u * 0.0), label, fontsize=8, weight="bold", color=INFO, ha="center",
                va="center", zorder=7)


def north(ax, x, y):
    ax.annotate("", (x, y + 0.9), (x, y), arrowprops=dict(arrowstyle="-|>", lw=0.8, color=INK), zorder=6)
    ax.text(x, y + 1.05, "N*", fontsize=6.5, ha="center", va="bottom", zorder=6)

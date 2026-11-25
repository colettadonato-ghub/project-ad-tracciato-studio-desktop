"""Elaborati per tipo di rilievo: Esterno edificio, Interno multi-piano, sezioni verticali.

Il tipo "Interno appartamento/ufficio" resta il motore attuale (pipeline.run_job → DXF/PDF/PNG);
qui si aggiungono le eventuali sezioni verticali.

Una sola passata a blocchi sul file: ogni blocco viene proiettato direttamente sulle griglie degli
elaborati (conteggi, quota massima, profondità minima). La memoria dipende dalla dimensione delle
tavole, non dal numero di punti.

Convenzioni
  - quote relative al piano 0 (z0, quota assoluta scelta dall'utente);
  - linea di sezione/prospetto p1 → p2 in coordinate del file; la vista guarda verso la normale
    sinistra n = (−uy, ux), quindi chi guarda ha p1 a sinistra e p2 a destra;
  - sezione: taglio |d| ≤ spessore/2, "visto oltre" fino a `beyond` metri dietro al taglio;
  - prospetto: tutti i punti con 0 ≤ d ≤ profondità, più scuri se più vicini.
"""
import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import cv2
from scipy import ndimage

from .. import __app_name__, __version__
from . import engine, render
from .logs import JobLog

log = logging.getLogger("tracciato.scene")

MODES = {
    "interno": "Interno appartamento/ufficio",
    "esterno": "Esterno edificio",
    "multipiano": "Interno multi-piano",
}
DETAIL_RES = {"alta": 0.01, "media": 0.02, "bozza": 0.03}
MAX_PIXELS = 36_000_000


def stages_for(mode, n_sections=0, n_elevations=0, has_bbox=True):
    st = []
    if not has_bbox:
        st.append(("bounds", "Ingombro della nuvola"))
    st.append(("read", "Lettura e proiezione dei punti"))
    if mode == "esterno":
        st.append(("footprint", "Sagoma planimetrica e perimetro"))
        if n_elevations:
            st.append(("elevations", f"Prospetti ({n_elevations})"))
    elif mode == "multipiano":
        st.append(("plans", "Piante dei livelli"))
    if n_sections:
        st.append(("sections", f"Sezioni verticali ({n_sections})"))
    return st


# ------------------------------------------------------------------ parametri
@dataclass
class Line:
    label: str
    kind: str              # "sezione" | "prospetto"
    p1: tuple
    p2: tuple
    t: float               # sezione: spessore del taglio · prospetto: profondità
    beyond: float = 2.0    # sezione: profondità del "visto oltre" (0 = solo taglio)

    @classmethod
    def from_dict(cls, d):
        ln = cls(label=str(d.get("label") or "A"), kind=d.get("kind", "sezione"),
                 p1=tuple(float(v) for v in d["p1"][:2]), p2=tuple(float(v) for v in d["p2"][:2]),
                 t=float(d.get("t", 0.10)), beyond=float(d.get("beyond", 2.0)))
        if ln.kind not in ("sezione", "prospetto"):
            raise ValueError(f"Tipo di linea non valido: {ln.kind}")
        if math.dist(ln.p1, ln.p2) < 0.3:
            raise ValueError(f"La linea {ln.label} è troppo corta (min 30 cm)")
        lim = (0.01, 2.0) if ln.kind == "sezione" else (0.2, 60.0)
        if not lim[0] <= ln.t <= lim[1]:
            what = "Lo spessore della sezione" if ln.kind == "sezione" else "La profondità del prospetto"
            raise ValueError(f"{what} {ln.label} deve essere tra {lim[0]:g} e {lim[1]:g} m")
        ln.beyond = max(0.0, min(ln.beyond, 30.0))
        return ln

    def frame(self):
        p1, p2 = np.array(self.p1), np.array(self.p2)
        L = float(np.linalg.norm(p2 - p1))
        u = (p2 - p1) / L
        n = np.array([-u[1], u[0]])
        return p1, u, n, L


@dataclass
class Level:
    name: str
    z: float               # quota del pavimento rispetto al piano 0
    cut: float = 1.10      # altezza del piano di sezione dal pavimento del livello
    t: float = 0.10        # spessore della fascia di sezione


@dataclass
class SceneParams:
    mode: str = "esterno"
    title: str = "RILIEVO"
    detail: str = "media"
    scale: int = 100
    z0: float | None = None             # piano 0, quota assoluta
    bbox: list | None = None            # [xmin, ymin, zmin, xmax, ymax, zmax] dall'anteprima
    angle: float = 0.0                  # rotazione dominante dei muri (gradi)
    align: bool = True
    fill_walls: bool = True             # campitura degli elementi tagliati
    cut_h: float = 1.00                 # esterno: quota della sezione orizzontale per il perimetro
    cut_t: float = 0.30
    levels: list = field(default_factory=list)
    lines: list = field(default_factory=list)
    level_marks: list = field(default_factory=list)   # quote da segnare in sezione (interno)

    @classmethod
    def from_dict(cls, d):
        d = dict(d or {})
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        p = cls(**known)
        if p.mode not in MODES:
            raise ValueError(f"Tipo di rilievo non valido: {p.mode}")
        p.scale = int(p.scale)
        if p.scale not in (20, 50, 100, 200, 500):
            raise ValueError("Scala non valida")
        if p.detail not in DETAIL_RES:
            p.detail = "media"
        p.z0 = None if p.z0 in ("", None) else float(p.z0)
        p.angle = float(p.angle or 0)
        p.cut_h, p.cut_t = float(p.cut_h), float(p.cut_t)
        if p.mode == "esterno" and not (0.2 <= p.cut_h <= 30 and 0.02 <= p.cut_t <= 2):
            raise ValueError("Sezione del perimetro: quota tra 0,20 e 30 m, spessore tra 0,02 e 2 m")
        p.levels = [lv if isinstance(lv, Level) else Level(name=str(lv.get("name") or f"Livello {i}"),
                                                             z=float(lv.get("z", 0)),
                                                             cut=float(lv.get("cut", 1.10)),
                                                             t=float(lv.get("t", 0.10)))
                    for i, lv in enumerate(p.levels)]
        for lv in p.levels:
            if not (0.1 <= lv.cut <= 4.0 and 0.01 <= lv.t <= 1.0):
                raise ValueError(f"{lv.name}: altezza di sezione 0,10–4,00 m, spessore 0,01–1,00 m")
        p.levels.sort(key=lambda lv: lv.z)
        if p.mode == "multipiano" and not p.levels:
            raise ValueError("Indica almeno un livello")
        p.lines = [ln if isinstance(ln, Line) else Line.from_dict(ln) for ln in p.lines]
        return p

    @property
    def res(self):
        return DETAIL_RES[self.detail]


# ------------------------------------------------------------------ griglie
class Raster:
    def __init__(self, x0, y0, x1, y1, res):
        self.res = res
        area = (x1 - x0) * (y1 - y0) / res ** 2
        if area > MAX_PIXELS:
            self.res = res * math.sqrt(area / MAX_PIXELS)
            log.warning(f"Elaborato molto esteso: risoluzione portata a {self.res * 100:.1f} cm/pixel")
        self.x0, self.y0 = x0, y0
        self.w = max(int(math.ceil((x1 - x0) / self.res)), 1)
        self.h = max(int(math.ceil((y1 - y0) / self.res)), 1)

    def index(self, a, b):
        ix = np.floor((a - self.x0) / self.res).astype(np.int64)
        iy = np.floor((b - self.y0) / self.res).astype(np.int64)
        m = (ix >= 0) & (ix < self.w) & (iy >= 0) & (iy < self.h)
        return iy[m] * self.w + ix[m], m

    def zeros(self, dtype=np.int32, fill=0):
        return np.full((self.h, self.w), fill, dtype)

    def extent(self):
        return (self.x0, self.x0 + self.w * self.res, self.y0, self.y0 + self.h * self.res)

    def px(self, x, y):
        return (x - self.x0) / self.res, (y - self.y0) / self.res

    def world(self, col, row):
        return self.x0 + (col + 0.5) * self.res, self.y0 + (row + 0.5) * self.res


def add_count(arr, idx):
    if len(idx):
        u, c = np.unique(idx, return_counts=True)
        arr.reshape(-1)[u] += c.astype(arr.dtype)


def add_min(arr, idx, val):
    """arr[idx] = min(arr[idx], val) senza np.minimum.at (lento)."""
    if not len(idx):
        return
    o = np.lexsort((val, idx))
    i, v = idx[o], val[o]
    first = np.r_[True, i[1:] != i[:-1]]
    flat = arr.reshape(-1)
    flat[i[first]] = np.minimum(flat[i[first]], v[first].astype(arr.dtype))


def clean(mask, min_px=6):
    """Toglie i pixel isolati (punti spuri)."""
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    keep = np.zeros(n, bool)
    keep[1:] = stats[1:, cv2.CC_STAT_AREA] >= min_px
    return keep[lab].astype(np.uint8)


def fill_thickness(img, max_wall, res):
    """Come nel motore della pianta: lo scanner vede le due facce di un muro (o di un solaio);
    la chiusura con elementi lineari riempie lo spessore senza chiudere le aperture."""
    L = int(max_wall / res) | 1
    out = img.copy()
    for ker in (np.ones((1, L), np.uint8), np.ones((L, 1), np.uint8)):
        out = cv2.bitwise_or(out, cv2.morphologyEx(img, cv2.MORPH_CLOSE, ker))
    return out


def crop_box(masks, margin_px):
    m = np.zeros_like(masks[0], dtype=bool)
    for k in masks:
        m |= k > 0
    rows, cols = np.where(m)
    if not len(rows):
        return None
    h, w = m.shape
    return (max(cols.min() - margin_px, 0), min(cols.max() + margin_px + 1, w),
            max(rows.min() - margin_px, 0), min(rows.max() + margin_px + 1, h))


# ------------------------------------------------------------------ elaborazione
class Job:
    def __init__(self, path, out_dir, p: SceneParams, hooks, files_base):
        self.path, self.out, self.p, self.hooks, self.base = path, Path(out_dir), p, hooks, files_base
        self.outputs = []
        b = p.bbox
        self.zmin, self.zmax = b[2], b[5]
        self.c = np.array([(b[0] + b[3]) / 2, (b[1] + b[4]) / 2])
        a = -math.radians(p.angle) if p.align else 0.0
        self.R = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
        corners = np.array([[b[0], b[1]], [b[3], b[1]], [b[3], b[4]], [b[0], b[4]]])
        pc = self.to_plan(corners)
        self.plan_box = (*(pc.min(0) - 0.5), *(pc.max(0) + 0.5))

    def to_plan(self, xy):
        return (np.asarray(xy, float) - self.c) @ self.R.T

    # -------------------------------------------------------------- impostazione griglie
    def setup(self):
        p, res = self.p, self.p.res
        x0, y0, x1, y1 = self.plan_box
        self.plan = None
        if p.mode == "esterno":
            self.plan = Raster(x0, y0, x1, y1, res)
            self.fp_max = self.plan.zeros(np.float32, np.inf)        # −(quota massima): copertura/sporti
            self.fp_cut = self.plan.zeros()                           # fascia del perimetro
            self.fp_high = self.plan.zeros()                          # stessa pianta, 1,0–1,6 m più su
        elif p.mode == "multipiano":
            self.plan = Raster(x0, y0, x1, y1, res)
            self.lv_cut = [self.plan.zeros() for _ in p.levels]
            self.lv_under = [self.plan.zeros() for _ in p.levels]
        self.secs = []
        zlo, zhi = self.zmin - self.p.z0 - 0.3, self.zmax - self.p.z0 + 0.3
        for ln in p.lines:
            p1, u, n, L = ln.frame()
            r = Raster(-1.0, zlo, L + 1.0, zhi, res)
            sec = {"line": ln, "p1": p1, "u": u, "n": n, "L": L, "r": r,
                   "cut": r.zeros(), "depth": r.zeros(np.float32, np.inf)}
            self.secs.append(sec)

    # -------------------------------------------------------------- passata sul file
    def accumulate(self, P):
        p = self.p
        zr = P[:, 2] - p.z0
        if self.plan is not None:
            xy = self.to_plan(P[:, :2])
            idx_all, m_all = self.plan.index(xy[:, 0], xy[:, 1])
            z_in = zr[m_all]
            if p.mode == "esterno":
                sel = z_in > 0.25
                add_min(self.fp_max, idx_all[sel], -z_in[sel])          # min di −z = max di z
                sel = np.abs(z_in - p.cut_h) <= p.cut_t / 2
                add_count(self.fp_cut, idx_all[sel])
                sel = (z_in > p.cut_h + 1.0) & (z_in < p.cut_h + 1.6)
                add_count(self.fp_high, idx_all[sel])
            else:
                for i, lv in enumerate(p.levels):
                    zc = lv.z + lv.cut
                    sel = np.abs(z_in - zc) <= lv.t / 2
                    add_count(self.lv_cut[i], idx_all[sel])
                    sel = (z_in > lv.z + 0.05) & (z_in < zc - lv.t / 2)
                    add_count(self.lv_under[i], idx_all[sel])
        for s in self.secs:
            ln = s["line"]
            q = P[:, :2] - s["p1"]
            d = q @ s["n"]
            if ln.kind == "sezione":
                near = (d >= -ln.t / 2) & (d <= ln.t / 2 + ln.beyond)
            else:
                near = (d >= 0) & (d <= ln.t)
            if not near.any():
                continue
            sv = q[near] @ s["u"]
            dv = d[near]
            idx, m = s["r"].index(sv, zr[near])
            dv = dv[m]
            if ln.kind == "sezione":
                cut = np.abs(dv) <= ln.t / 2
                add_count(s["cut"], idx[cut])
                add_min(s["depth"], idx[~cut], dv[~cut])
            else:
                add_count(s["cut"], idx)
                add_min(s["depth"], idx, dv)

    # -------------------------------------------------------------- elaborati
    def _file(self, suffix):
        return str(self.base) + suffix

    def footprint(self):
        p, r = self.p, self.plan
        res = r.res
        has = np.isfinite(self.fp_max) & (clean(np.isfinite(self.fp_max).astype(np.uint8), 8) > 0)
        top = np.where(has, -self.fp_max, 0.0)
        cut = clean((self.fp_cut > 0).astype(np.uint8))
        high = cv2.dilate((self.fp_high > 0).astype(np.uint8), np.ones((5, 5), np.uint8))
        facade = cut & high                                # solo ciò che sale in verticale (niente terreno)
        if facade.sum() < 0.2 * max(cut.sum(), 1):
            log.warning("Poche facciate verticali alla quota del perimetro: uso tutta la fascia di sezione")
            facade = cut
        k = lambda m_: cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (int(m_ / res) | 1,) * 2)
        poly, area, solid = None, 0.0, None
        for close_m in (0.6, 1.5, 3.0, 6.0):              # chiudo porte, finestre e lati poco rilevati
            # chiusura con segmenti orizzontali/verticali (la pianta è già ruotata sui muri): unisce i
            # tratti allineati di una facciata interrotta senza arrotondare gli spigoli
            ring = fill_thickness(facade, close_m, res)
            ring = cv2.morphologyEx(ring, cv2.MORPH_CLOSE, k(0.3))
            ring = cv2.dilate(ring, np.ones((3, 3), np.uint8))   # linee di 1 px: niente fughe in diagonale
            filled = ndimage.binary_fill_holes(ring).astype(np.uint8)
            n, lab, stats, _ = cv2.connectedComponentsWithStats(filled, connectivity=8)
            if n < 2:
                continue
            j = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
            comp = (lab == j).astype(np.uint8)
            ring_area = (ring & comp).sum()
            if comp.sum() > 2.5 * ring_area:               # il contorno si è chiuso
                solid = comp
                break
        if solid is None:
            log.warning("Il perimetro non è chiuso (lati non rilevati?): uso l'involucro convesso delle facciate")
            pts = np.argwhere(facade > 0)[:, ::-1].astype(np.int32)
            if len(pts) >= 3:
                solid = np.zeros_like(facade)
                cv2.fillPoly(solid, [cv2.convexHull(pts)], 1)
        if solid is not None:
            cnts, _ = cv2.findContours(solid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
            c = max(cnts, key=cv2.contourArea)
            ap = cv2.approxPolyDP(c, 0.06 / res, True)[:, 0, :].astype(float)
            P = np.c_[r.x0 + (ap[:, 0] + 0.5) * res, r.y0 + (ap[:, 1] + 0.5) * res]
            if p.align:
                P = engine.snap_poly(P)
            from shapely.geometry import Polygon
            pg = Polygon(P)
            if pg.is_valid and pg.area > 1:
                poly, area = P, float(pg.area)
        # altezza dell'edificio: quota massima dentro la sagoma (99,5° percentile)
        H = None
        if solid is not None:
            inside = cv2.dilate(solid, k(0.6)).astype(bool) & has
            if inside.any():
                H = float(np.percentile(top[inside], 99.5))
        self.building_h = H
        self.footprint_poly = poly

        # immagine: copertura in grigi (più scuro = più alto), facciate tagliate in nero
        hmax = max(H or float(np.percentile(top[has], 99)) if has.any() else 1.0, 0.5)
        img = np.ones((r.h, r.w), np.float32)
        img[has] = 0.93 - 0.38 * np.clip(top[has] / hmax, 0, 1)
        img[cut > 0] = 0.35
        img[facade > 0] = 0.0
        box = crop_box([facade if facade.any() else cut, has.astype(np.uint8)], int(2.0 / res))
        if solid is not None:
            bb = crop_box([solid], int(6.0 / res))
            box = bb or box
        c0, c1, r0, r1 = box
        img = img[r0:r1, c0:c1]
        ext = (r.x0 + c0 * res, r.x0 + c1 * res, r.y0 + r0 * res, r.y0 + r1 * res)
        info = {}
        if poly is not None:
            per = float(np.sum(np.linalg.norm(np.roll(poly, -1, 0) - poly, axis=1)))
            info = {"area": round(area, 2), "perimeter": round(per, 2), "vertices": int(len(poly)),
                    "width": round(float(np.ptp(poly[:, 0])), 2), "depth": round(float(np.ptp(poly[:, 1])), 2)}
        if H is not None:
            info["height"] = round(H, 2)

        def draw(ax):
            if poly is not None:
                q = np.vstack([poly, poly[:1]])
                ax.plot(q[:, 0], q[:, 1], color=render.INK, lw=1.1, zorder=5)
                cx, cy = poly.mean(0)
                for a_, b_ in zip(poly, np.roll(poly, -1, 0)):
                    L = math.dist(a_, b_)
                    if L < 0.8:
                        continue
                    mpt = (a_ + b_) / 2
                    e = b_ - a_
                    nrm = np.array([e[1], -e[0]]) / L
                    if np.dot(nrm, mpt - [cx, cy]) < 0:
                        nrm = -nrm
                    ang = math.degrees(math.atan2(e[1], e[0]))
                    if ang > 90 or ang < -90:
                        ang += 180
                    ax.text(*(mpt + nrm * 0.35), f"{L:.2f}", fontsize=6, ha="center", va="center",
                            rotation=ang, family="DejaVu Sans Mono", zorder=6)
                ax.text(cx, cy + 0.2, "SAGOMA", fontsize=7, weight="bold", ha="center", zorder=6)
                ax.text(cx, cy - 0.3, f"{area:.2f} m²", fontsize=6.5, ha="center", zorder=6)
                if H is not None:
                    ax.text(cx, cy - 0.75, f"H max {H:.2f} m", fontsize=5.5, ha="center", color=render.MATITA,
                            zorder=6)
            self._marks(ax)
            render.scale_bar(ax, ext[0], ext[2] - 0.9, ext[1] - ext[0])
            render.north(ax, ext[1] + 0.6, ext[3] - 1.4)

        png = self._file("_sagoma.png")
        sub = f"Sagoma planimetrica e perimetro a h {p.cut_h:.2f} m dal piano 0"
        inf = (f"Superficie coperta {info.get('area', 0):.2f} m² · perimetro {info.get('perimeter', 0):.2f} m"
               if poly is not None else "Perimetro non ricostruito: vedi log") + \
              (f" · H max {H:.2f} m" if H is not None else "") + f" · rot. {p.angle if p.align else 0:.1f}°"
        paper, _ = render.sheet(png, img, ext, p.scale, f"{p.title} — SAGOMA", sub, inf, draw)
        out = {"kind": "sagoma", "label": "Sagoma planimetrica", "png": png, "paper": paper, "info": info}
        if poly is not None:
            out["dxf"] = self._file("_perimetro.dxf")
            self._write_perimeter_dxf(out["dxf"], poly, area)
        self.outputs.append(out)
        log.info(f"Sagoma: {info.get('area', '—')} m², perimetro {info.get('perimeter', '—')} m, "
                 f"H max {info.get('height', '—')} m → {Path(png).name}")

    def _write_perimeter_dxf(self, path, poly, area):
        import ezdxf
        from ezdxf.enums import TextEntityAlignment
        doc = ezdxf.new("R2018", setup=True)
        doc.units = ezdxf.units.M
        doc.layers.add("PERIMETRO", color=7)
        doc.layers.add("TESTI", color=7)
        msp = doc.modelspace()
        o = poly.min(0)
        q = poly - o
        msp.add_lwpolyline(q.tolist(), close=True, dxfattribs={"layer": "PERIMETRO", "lineweight": 50})
        c = q.mean(0)
        msp.add_text(f"SAGOMA {area:.2f} m²", height=0.25, dxfattribs={"layer": "TESTI"}).set_placement(
            tuple(c), align=TextEntityAlignment.MIDDLE_CENTER)
        doc.header["$INSUNITS"] = 6
        doc.saveas(path)

    def plans(self):
        p, r = self.p, self.plan
        res = r.res
        cuts = []
        for i, lv in enumerate(p.levels):
            c = clean((self.lv_cut[i] > 0).astype(np.uint8))
            if p.fill_walls:
                c = fill_thickness(cv2.morphologyEx(c, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8)), 0.55, res)
            cuts.append(c)
        box = crop_box(cuts, int(1.5 / res))
        if box is None:
            raise RuntimeError("Nessun punto alle quote di sezione dei livelli: controlla piano 0 e quote")
        c0, c1, r0, r1 = box
        ext = (r.x0 + c0 * res, r.x0 + c1 * res, r.y0 + r0 * res, r.y0 + r1 * res)
        for i, lv in enumerate(p.levels):
            self.hooks.check()
            under = clean((self.lv_under[i] > 0).astype(np.uint8), 4)
            img = np.ones((r.h, r.w), np.float32)
            img[under > 0] = 0.78
            img[cuts[i] > 0] = 0.0
            img = img[r0:r1, c0:c1]
            n_cut = int(cuts[i].sum())
            if n_cut < 50:
                log.warning(f"{lv.name}: quasi nessun punto alla quota di sezione (+{lv.z + lv.cut:.2f})")
            cut_area = cuts[i][r0:r1, c0:c1]
            rows, cols = np.where(cut_area > 0)

            def draw(ax, rows=rows, cols=cols):
                if len(rows):
                    xa, xb = ext[0] + cols.min() * res, ext[0] + (cols.max() + 1) * res
                    ya, yb = ext[2] + rows.min() * res, ext[2] + (rows.max() + 1) * res
                    render.dim_h(ax, xa, xb, ya - 0.9)
                    render.dim_v(ax, xa - 0.9, ya, yb)
                self._marks(ax)
                render.scale_bar(ax, ext[0], ext[2] - 1.6, ext[1] - ext[0])
                render.north(ax, ext[1] + 0.6, ext[3] - 1.4)

            slug = f"pianta-{i}"
            png = self._file(f"_{slug}.png")
            zc = lv.z + lv.cut
            sub = f"{lv.name} — pianta a h {lv.cut:.2f} m dal pavimento ({'+' if zc >= 0 else '−'}{abs(zc):.2f} dal piano 0)"
            inf = (f"Pav. {'+' if lv.z >= 0 else '−'}{abs(lv.z):.2f} · fascia {lv.t * 100:.0f} cm · "
                   f"grigio = sotto il taglio · rot. {p.angle if p.align else 0:.1f}° · N* di griglia")
            paper, _ = render.sheet(png, img, ext, p.scale, f"{p.title} — {lv.name.upper()}", sub, inf, draw)
            self.outputs.append({"kind": "pianta", "label": f"Pianta {lv.name}", "png": png, "paper": paper,
                                 "info": {"z": lv.z, "cut": lv.cut, "t": lv.t}})
            log.info(f"Pianta {lv.name}: sezione a +{zc:.2f} → {Path(png).name}")

    def sections(self, kinds):
        p = self.p
        for s in self.secs:
            ln = s["line"]
            if ln.kind not in kinds:
                continue
            self.hooks.check()
            r = s["r"]
            res = r.res
            depth = s["depth"]
            has_d = np.isfinite(depth) & (clean(np.isfinite(depth).astype(np.uint8), 5) > 0)
            img = np.ones((r.h, r.w), np.float32)
            if ln.kind == "sezione":
                cut = clean((s["cut"] > 0).astype(np.uint8))
                if p.fill_walls:
                    cut = fill_thickness(cv2.morphologyEx(cut, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8)), 0.55, res)
                if ln.beyond > 0 and has_d.any():
                    dd = np.clip((depth - ln.t / 2) / max(ln.beyond, 1e-3), 0, 1)
                    v = np.where(has_d, 0.55 + 0.38 * dd, 1.0).astype(np.float32)
                    v = cv2.erode(v, np.ones((3, 3), np.uint8))              # chiude i buchi tra i punti
                    img = np.minimum(img, v)
                img[cut > 0] = 0.0
                main = cut
            else:
                v = np.where(has_d, 0.10 + 0.72 * np.clip(depth / ln.t, 0, 1) ** 0.8, 1.0).astype(np.float32)
                v = cv2.erode(v, np.ones((3, 3), np.uint8))
                img = np.minimum(img, v)
                main = (s["cut"] > 0).astype(np.uint8)
            box = crop_box([main, has_d.astype(np.uint8)] if ln.kind == "prospetto" else [main], int(0.8 / res))
            if box is None:
                log.warning(f"{ln.kind.capitalize()} {ln.label}: nessun punto lungo la linea, elaborato saltato")
                continue
            c0, c1, r0, r1 = box
            img = img[r0:r1, c0:c1]
            ext = (r.x0 + c0 * res, r.x0 + c1 * res, r.y0 + r0 * res, r.y0 + r1 * res)
            marks = self._level_marks()

            def draw(ax, ext=ext, marks=marks):
                for z, name in marks:
                    if ext[2] - 0.5 <= z <= ext[3] + 0.5:
                        render.level_line(ax, ext[0] - 0.3, ext[1] + 0.3, z, name)
                render.dim_h(ax, max(ext[0], 0), min(ext[1], s["L"]), ext[2] - 0.7)
                render.scale_bar(ax, ext[0], ext[2] - 1.5, ext[1] - ext[0])

            kind = "sezione" if ln.kind == "sezione" else "prospetto"
            png = self._file(f"_{kind}-{_slug(ln.label)}.png")
            if kind == "sezione":
                title = f"{p.title} — SEZIONE {ln.label}-{ln.label}'"
                sub = f"Sezione {ln.label}-{ln.label}' · spessore del taglio {ln.t * 100:.0f} cm"
                inf = (f"Lunghezza {s['L']:.2f} m · visto oltre {ln.beyond:.1f} m (in grigio) · quote dal piano 0"
                       if ln.beyond > 0 else f"Lunghezza {s['L']:.2f} m · solo taglio · quote dal piano 0")
            else:
                title = f"{p.title} — PROSPETTO {ln.label}"
                sub = f"Prospetto {ln.label} · profondità {ln.t:.1f} m dalla linea di vista"
                inf = f"Larghezza {s['L']:.2f} m · più scuro = più vicino · quote dal piano 0"
            paper, _ = render.sheet(png, img, ext, p.scale, title, sub, inf, draw)
            self.outputs.append({"kind": kind, "label": f"{kind.capitalize()} {ln.label}", "png": png,
                                 "paper": paper, "info": {"length": round(s["L"], 2), "t": ln.t}})
            log.info(f"{kind.capitalize()} {ln.label}: {s['L']:.2f} m → {Path(png).name}")

    def _level_marks(self):
        p = self.p
        marks = [(0.0, "Piano 0")]
        if p.mode == "multipiano":
            marks = [(lv.z, lv.name) for lv in p.levels]
            if not any(abs(z) < 0.01 for z, _ in marks):
                marks.insert(0, (0.0, "Piano 0"))
        for m in p.level_marks:
            marks.append((float(m["z"]), m.get("name", "")))
        if p.mode == "esterno" and getattr(self, "building_h", None):
            marks.append((self.building_h, "H max"))
        return marks

    def _marks(self, ax):
        """Tracce delle linee di sezione/prospetto sulle piante."""
        for s in self.secs:
            ln = s["line"]
            a = self.to_plan([ln.p1])[0]
            b = self.to_plan([ln.p2])[0]
            n = s["n"] @ self.R.T
            render.section_mark(ax, a, b, n, ln.label)


def _slug(s):
    return "".join(ch if ch.isalnum() else "-" for ch in str(s)).strip("-") or "x"


def scan_bounds(path, hooks, e57_raw):
    mn, mx = np.full(3, np.inf), np.full(3, -np.inf)
    for c, frac in engine.iter_chunks(path, e57_use_raw=e57_raw):
        hooks.check()
        c = c[np.isfinite(c).all(axis=1)]
        if len(c):
            mn, mx = np.minimum(mn, c.min(0)), np.maximum(mx, c.max(0))
        hooks.progress("bounds", frac)
    return [float(v) for v in (*mn, *mx)]


# ------------------------------------------------------------------ orchestrazione
def run_scene(input_path, out_dir, p: SceneParams, on_event=None, cancelled=None, base=None,
              joblog=True) -> dict:
    emit = on_event or (lambda e: None)
    src = Path(input_path)
    if not src.exists():
        raise FileNotFoundError(f"File non trovato: {src}")
    now = datetime.now()
    # con `base` (interno: la cartella è già stata creata dalla pianta) si resta nella stessa sottocartella
    out = Path(out_dir) if base else Path(out_dir) / now.strftime("%Y-%m-%d_%H-%M")
    out.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    base = Path(base) if base else out / f"{src.stem}_{p.mode}_{stamp}"
    files = {"log": str(base) + ".log", "json": str(base) + ".json"} if joblog else {}

    times, current = {}, {"name": None, "t0": None}
    n_sec = sum(ln.kind == "sezione" for ln in p.lines)
    labels = dict(stages_for(p.mode, n_sec, len(p.lines) - n_sec, False))

    def stage(name):
        now = time.perf_counter()
        if current["name"]:
            times[current["name"]] = now - current["t0"]
            emit({"type": "stage", "stage": current["name"], "status": "done",
                  "seconds": round(times[current["name"]], 2)})
        current.update(name=name, t0=now)
        if name:
            log.info(f"▶ {labels.get(name, name)}")
            emit({"type": "stage", "stage": name, "status": "running"})

    def progress(stage_name, frac):
        emit({"type": "progress", "stage": stage_name, "value": round(float(frac), 3)})

    hooks = engine.Hooks(progress=progress, stage=stage, cancelled=cancelled)
    summary = {"app": __app_name__, "version": __version__, "mode": p.mode, "mode_label": MODES[p.mode],
               "input": str(src), "output_dir": str(out), "params": _params_json(p),
               "started": datetime.now().isoformat(timespec="seconds"), "status": "running"}
    t_start = time.perf_counter()

    def ui_log(level, line):
        emit({"type": "log", "level": level, "line": line})

    ctx = JobLog(Path(files["log"]), sink=ui_log) if joblog else _Null()
    job = None
    with ctx:
        try:
            log.info(f"{MODES[p.mode]}: {src} ({__app_name__} {__version__})")
            log.info("Parametri: " + json.dumps(summary["params"], ensure_ascii=False))
            e57_raw = src.suffix.lower() == ".e57"
            if not p.bbox:
                stage("bounds")
                p.bbox = scan_bounds(str(src), hooks, e57_raw)
            if p.z0 is None:
                p.z0 = p.bbox[2]
                log.warning(f"Piano 0 non indicato: uso il punto più basso della nuvola (z={p.z0:.3f})")
            job = Job(str(src), out, p, hooks, base)
            job.setup()
            stage("read")
            n = 0
            for c, frac in engine.iter_chunks(str(src), e57_use_raw=e57_raw):
                hooks.check()
                c = c[np.isfinite(c).all(axis=1)]
                n += len(c)
                job.accumulate(c)
                hooks.progress("read", frac)
            log.info(f"Punti letti: {n:,}")
            if p.mode == "esterno":
                hooks.check(); stage("footprint"); job.footprint()
                if any(ln.kind == "prospetto" for ln in p.lines):
                    hooks.check(); stage("elevations"); job.sections({"prospetto"})
            elif p.mode == "multipiano":
                hooks.check(); stage("plans"); job.plans()
            if any(ln.kind == "sezione" for ln in p.lines):
                hooks.check(); stage("sections"); job.sections({"sezione"})
            stage(None)
            total = time.perf_counter() - t_start
            summary.update(status="ok", seconds=round(total, 1), points_file=n, outputs=job.outputs,
                           stage_seconds={k: round(v, 2) for k, v in times.items()})
            fp = next((o for o in job.outputs if o["kind"] == "sagoma"), None)
            if fp:
                summary["footprint"] = fp["info"]
            log.info(f"✔ Completato in {total:.1f} s — {len(job.outputs)} elaborati")
        except engine.Cancelled:
            summary.update(status="cancelled", seconds=round(time.perf_counter() - t_start, 1))
            log.warning("Elaborazione annullata dall'utente")
            if current["name"]:
                emit({"type": "stage", "stage": current["name"], "status": "cancelled"})
        except Exception as ex:
            summary.update(status="error", error=str(ex), seconds=round(time.perf_counter() - t_start, 1))
            log.error(f"Errore: {ex}")
            logging.getLogger("tracciato").exception("Traceback elaborazione")
            if current["name"]:
                emit({"type": "stage", "stage": current["name"], "status": "error"})
    summary.setdefault("outputs", job.outputs if job else [])
    summary["finished"] = datetime.now().isoformat(timespec="seconds")
    summary["files"] = _files_of(summary["outputs"], files)
    if joblog:
        Path(files["json"]).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _files_of(outputs, extra):
    f = {k: v for k, v in extra.items() if k == "json" or os.path.exists(v)}
    for o in outputs:
        if os.path.exists(o["png"]):
            f.setdefault("png", o["png"])
    return f


def _params_json(p: SceneParams):
    d = asdict(p)
    d["levels"] = [asdict(lv) if not isinstance(lv, dict) else lv for lv in p.levels]
    d["lines"] = [asdict(ln) if not isinstance(ln, dict) else ln for ln in p.lines]
    return d


def run(input_path, out_dir, mode, params, on_event=None, cancelled=None) -> dict:
    """Punto d'ingresso unico per l'interfaccia e la CLI.

    params = {"scene": {...SceneParams...}, "plan": {...pipeline.Params...}}  ("plan" solo per interno)
    """
    emit = on_event or (lambda e: None)
    sp_dict = dict(params.get("scene") or {})
    sp_dict["mode"] = mode
    if mode != "interno":
        return run_scene(input_path, out_dir, SceneParams.from_dict(sp_dict), emit, cancelled)

    # Interno appartamento/ufficio: motore attuale + eventuali sezioni verticali
    from . import pipeline
    pp = pipeline.Params.from_dict(params.get("plan") or {})
    sp = SceneParams.from_dict(sp_dict)
    s = pipeline.run_job(input_path, out_dir, pp, on_event=emit, cancelled=cancelled)
    s["mode"], s["mode_label"] = "interno", MODES["interno"]
    f = s.get("files") or {}
    outs = []
    if s.get("status") == "ok" and f.get("png"):
        outs.append({"kind": "pianta", "label": "Pianta", "png": f.get("preview") or f["png"],
                     "sheet": f["png"], "pdf": f.get("pdf"), "dxf": f.get("dxf"), "paper": s.get("paper"),
                     "info": {"area": s.get("area_total"), "rooms": len(s.get("rooms") or [])}})
    if s.get("status") == "ok" and sp.lines:
        sp.z0 = s.get("floor_z", sp.z0)
        if s.get("height"):
            sp.level_marks = [{"z": float(s["height"]), "name": "Soffitto"}]
        base = str(Path(f["dxf"]).with_suffix("")) if f.get("dxf") else None
        s2 = run_scene(input_path, s["output_dir"], sp, emit, cancelled, base=base, joblog=False)
        outs += s2.get("outputs", [])
        if s2["status"] != "ok":
            s["status"], s["error"] = s2["status"], s2.get("error")
        s["seconds"] = round((s.get("seconds") or 0) + (s2.get("seconds") or 0), 1)
    s["outputs"] = outs
    if f.get("json") and os.path.exists(os.path.dirname(f["json"])):
        try:
            Path(f["json"]).write_text(json.dumps(s, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        except Exception:
            log.debug("Riepilogo non aggiornato", exc_info=True)
    return s

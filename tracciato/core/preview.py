"""Anteprima della nuvola: campione leggero per il viewer 3D e prime misure della scena.

Una sola passata a blocchi sul file (memoria costante) produce:
  - un campione casuale di ~400 000 punti, quantizzato a 16 bit per asse (≈ 2,4 MB);
  - il riquadro d'ingombro esatto (min/max su tutti i punti);
  - le quote delle superfici orizzontali (picchi dell'istogramma Z): pavimenti, solai, terreno, soffitti;
  - la proposta di piano 0, dei livelli e del tipo di rilievo (interno / esterno / multi-piano);
  - l'angolo dominante dei muri, usato per raddrizzare piante e prospetti.

Il risultato è un dict serializzabile in JSON. Viene salvato in cache (cartella dati) così riaprire
la stessa nuvola è immediato.
"""
import base64
import hashlib
import json
import logging
import os
import time
from pathlib import Path

import numpy as np
from scipy import ndimage

from . import engine

log = logging.getLogger("tracciato.preview")

PREVIEW_VERSION = 2          # 2: colore RGB dei punti (LAS/LAZ)
DEFAULT_TARGET = 400_000


def _iter_with_rgb(path, e57_raw):
    """Come engine.iter_chunks, ma con il colore RGB (uint8, N×3) quando il file lo contiene.

    Colore disponibile per LAS/LAZ con formato punto 2, 3, 5, 7, 8, 10 (campi red/green/blue).
    Per E57 e per i LAS senza colore ritorna rgb=None."""
    ext = Path(path).suffix.lower()
    if ext in (".las", ".laz"):
        import laspy
        with laspy.open(path) as f:
            has = all(d in f.header.point_format.dimension_names for d in ("red", "green", "blue"))
            total = max(int(f.header.point_count), 1)
            done = 0
            for pts in f.chunk_iterator(2_000_000):
                done += len(pts)
                xyz = np.c_[pts.x, pts.y, pts.z]
                rgb = None
                if has:
                    c = np.c_[pts.red, pts.green, pts.blue].astype(np.uint32)
                    # i LAS usano 16 bit; molti software scrivono comunque valori a 8 bit
                    rgb = c if c.max() > 255 else c * 257
                    rgb = (rgb >> 8).astype(np.uint8)
                yield xyz, done / total, rgb
        return
    for c, frac in engine.iter_chunks(path, e57_use_raw=e57_raw):
        yield c, frac, None


# ------------------------------------------------------------------ cache
def _cache_dir() -> Path:
    from .logs import data_dir
    d = data_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_key(path: str, target: int) -> str:
    st = os.stat(path)
    raw = f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}|{target}|{PREVIEW_VERSION}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def load_cached(path: str, target: int = DEFAULT_TARGET):
    try:
        f = _cache_dir() / f"{_cache_key(path, target)}.json"
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            data["from_cache"] = True
            return data
    except Exception:
        log.debug("Cache anteprima non leggibile", exc_info=True)
    return None


def save_cached(path: str, data: dict, target: int = DEFAULT_TARGET, keep: int = 12):
    try:
        d = _cache_dir()
        (d / f"{_cache_key(path, target)}.json").write_text(json.dumps(data), encoding="utf-8")
        files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            old.unlink(missing_ok=True)
    except Exception:
        log.debug("Cache anteprima non scritta", exc_info=True)


# ------------------------------------------------------------------ analisi delle quote
def horizontal_peaks(z: np.ndarray, bin_=0.02):
    """Quote delle superfici orizzontali: massimi locali dell'istogramma Z.
    Ritorna [{"z": quota assoluta, "w": peso relativo 0..1}] dal basso verso l'alto."""
    if len(z) < 100:
        return []
    lo, hi = np.percentile(z, [0.2, 99.8])
    if hi - lo < 0.2:
        return [{"z": float(np.median(z)), "w": 1.0}]
    lo, hi = lo - 0.3, hi + 0.3                      # margine: pavimento e soffitto stanno sugli estremi
    hist, edges = np.histogram(z, bins=np.arange(lo, hi + bin_, bin_))
    h = ndimage.gaussian_filter1d(hist.astype(float), 1.0)
    thr = max(h.mean() + 1.5 * h.std(), 0.12 * h.max())
    centers = (edges[:-1] + edges[1:]) / 2
    cand = [i for i in range(1, len(h) - 1) if h[i] >= thr and h[i] >= h[i - 1] and h[i] >= h[i + 1]]
    peaks = []
    for i in cand:                                   # fondo i massimi a meno di 15 cm
        if peaks and centers[i] - peaks[-1][0] < 0.15:
            if h[i] > peaks[-1][1]:
                peaks[-1] = (centers[i], h[i])
            continue
        peaks.append((centers[i], h[i]))
    if not peaks:
        return []
    top = max(p[1] for p in peaks)
    out = []
    for c, w in peaks:
        near = z[np.abs(z - c) < 0.04]
        out.append({"z": float(np.median(near)) if len(near) else float(c), "w": round(float(w / top), 3)})
    return out


def _coverage(P, zc, cell=0.5):
    """Celle da 50 cm occupate dai punti di una superficie orizzontale (quota zc ± 4 cm)."""
    s = P[np.abs(P[:, 2] - zc) < 0.04, :2]
    if not len(s):
        return 0
    return len(np.unique(np.floor(s / cell).astype(np.int64), axis=0))


def suggest(P: np.ndarray, peaks: list):
    """Piano 0, livelli e tipo di rilievo proposti. Sono solo un punto di partenza: l'utente li corregge."""
    z = P[:, 2]
    p1, p99 = np.percentile(z, [1, 99])
    zr = float(p99 - p1)
    strong = [p for p in peaks if p["w"] >= 0.2]
    z0 = strong[0]["z"] if strong else float(p1)
    # soffitto/copertura: superficie orizzontale forte vicino al punto più alto
    # e che copre almeno metà della superficie del piano 0 (un cornicione visto da fuori non basta)
    base = max(_coverage(P, z0), 1)
    has_top = any(p["w"] >= 0.25 and p["z"] > z0 + 1.8 and p["z"] > p99 - 0.6
                  and _coverage(P, p["z"]) >= 0.5 * base for p in peaks)

    # livelli: coppie soffitto+pavimento a meno di 60 cm (lo spessore del solaio) → il pavimento è il superiore
    levels = [0.0]
    above = [p for p in peaks if p["z"] > z0 + 1.8 and p["w"] >= 0.15]
    i = 0
    while i < len(above):
        cur = above[i]
        if i + 1 < len(above) and above[i + 1]["z"] - cur["z"] < 0.6:
            floor = above[i + 1]["z"]
            i += 2
        else:
            floor = cur["z"]
            i += 1
        rel = floor - z0
        is_top = floor > p99 - 0.4                     # il soffitto dell'ultimo livello non è un livello
        if not is_top and rel - levels[-1] >= 1.8:
            levels.append(round(float(rel), 2))

    if len(levels) >= 2 or (zr > 4.2 and has_top):
        mode = "multipiano"
    elif not has_top:
        mode = "esterno"
    else:
        mode = "interno"
    return {"z0": round(float(z0), 3), "levels": levels, "mode": mode, "z_range": round(zr, 2),
            "has_top": bool(has_top)}


# ------------------------------------------------------------------ anteprima
def build_preview(path: str, total_hint=None, target: int = DEFAULT_TARGET, hooks=None, e57_raw=None) -> dict:
    hooks = hooks or engine.Hooks()
    t0 = time.perf_counter()
    ext = Path(path).suffix.lower()
    if e57_raw is None:
        e57_raw = ext == ".e57"            # come pipeline.run_job: parser integrato, niente crash nativi
    p = 1.0 if not total_hint else min(1.0, 1.6 * target / max(total_hint, 1))
    rng = np.random.default_rng(7)
    keep, keep_rgb, n_read = [], [], 0
    mn = np.full(3, np.inf)
    mx = np.full(3, -np.inf)
    hooks.stage("preview")
    for c, frac, rgb in _iter_with_rgb(path, e57_raw):
        hooks.check()
        ok = np.isfinite(c).all(axis=1)
        c = c[ok]
        if rgb is not None:
            rgb = rgb[ok]
        if not len(c):
            continue
        n_read += len(c)
        mn = np.minimum(mn, c.min(0))
        mx = np.maximum(mx, c.max(0))
        if p < 1.0:
            sel = rng.random(len(c)) < p
            c = c[sel]
            if rgb is not None:
                rgb = rgb[sel]
        keep.append(c.astype(np.float64))
        keep_rgb.append(rgb)
        # senza conteggio noto: se il campione cresce troppo lo dimezzo
        if total_hint is None and sum(len(k) for k in keep) > 4 * target:
            keep = [k[::2] for k in keep]
            keep_rgb = [None if k is None else k[::2] for k in keep_rgb]
        hooks.progress("preview", frac)
    if n_read == 0:
        raise RuntimeError("Il file non contiene punti leggibili")
    P = np.vstack(keep)
    del keep
    C = None
    if keep_rgb and all(k is not None for k in keep_rgb):
        C = np.vstack(keep_rgb)
        if not C.any():            # tutto nero: il file ha i campi colore ma non li usa
            C = None
    del keep_rgb
    if len(P) > target:
        idx = rng.choice(len(P), target, replace=False)
        P = P[idx]
        if C is not None:
            C = C[idx]

    z = P[:, 2]
    peaks = horizontal_peaks(z)
    sug = suggest(P, peaks)

    # angolo dominante dei muri: fascia 0,8–2,0 m sopra il piano 0
    angle = 0.0
    band = (z > sug["z0"] + 0.8) & (z < sug["z0"] + 2.0)
    try:
        if band.sum() > 2000:
            angle = float(engine.dominant_angle(P[band, :2], res=0.05))
    except Exception:
        log.debug("Angolo dominante non calcolato", exc_info=True)

    try:
        guides = guide_lines(P, sug["z0"], angle)
    except Exception:
        log.debug("Linee guida non calcolate", exc_info=True)
        guides = {}

    # quantizzazione 16 bit sul riquadro d'ingombro
    span = np.maximum(mx - mn, 1e-6)
    q = np.clip(np.round((P - mn) / span * 65535), 0, 65535).astype("<u2")
    hist, edges = np.histogram(z, bins=120, range=(mn[2], mx[2]))
    out = {
        "version": PREVIEW_VERSION,
        "path": str(path), "points_file": int(n_read), "points_preview": int(len(P)),
        "bbox": [float(v) for v in (*mn, *mx)],
        "data": base64.b64encode(q.tobytes()).decode("ascii"),
        "rgb": base64.b64encode(np.ascontiguousarray(C).tobytes()).decode("ascii") if C is not None else None,
        "peaks": peaks,
        "z_hist": {"counts": hist.astype(int).tolist(), "z_min": float(edges[0]), "z_max": float(edges[-1])},
        "angle": round(angle, 3),
        "suggest": sug,
        "guides": guides,
        "seconds": round(time.perf_counter() - t0, 1),
    }
    hooks.stage(None)
    log.info(f"Anteprima: {n_read:,} punti letti → {len(P):,} nel campione in {out['seconds']} s · "
             f"piano 0 z={sug['z0']:.2f} · livelli {sug['levels']} · tipo proposto: {sug['mode']} · "
             f"rotazione muri {angle:.1f}°")
    return out


def guide_lines(P, z0, angle_deg, cell=0.25):
    """Linee proposte: i 4 fronti dell'edificio (prospetti) e una sezione sul lato lungo.

    Ingombro = componente più grande dei punti tra 0,8 e 2,5 m sopra il piano 0 (esclude terreno e
    gran parte della vegetazione), nel riferimento ruotato sui muri."""
    import cv2
    z = P[:, 2] - z0
    sel = P[(z > 0.8) & (z < 2.5), :2]
    if len(sel) < 500:
        return {}
    a = -np.radians(angle_deg)
    R = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]])
    c = sel.mean(0)
    q = (sel - c) @ R.T
    k = np.floor((q - q.min(0)) / cell).astype(int)
    img = np.zeros(k.max(0)[::-1] + 1, np.uint8)
    img[k[:, 1], k[:, 0]] = 1
    img = cv2.morphologyEx(img, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(img, connectivity=8)
    if n < 2:
        return {}
    j = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    inside = lab[k[:, 1], k[:, 0]] == j
    qq = q[inside]
    (x0, y0), (x1, y1) = np.percentile(qq, 1, axis=0), np.percentile(qq, 99, axis=0)
    back = lambda pts: (np.asarray(pts, float) @ R) + c              # dal riferimento ruotato al file
    off, ext = 1.5, 1.0
    W, D = x1 - x0, y1 - y0
    fronts = []
    # vista verso l'edificio: la normale sinistra di p1→p2 deve puntare dentro
    specs = [("1", (x0 - ext, y0 - off), (x1 + ext, y0 - off), D / 2 + off),    # lato y min, guarda +y
             ("2", (x1 + off, y0 - ext), (x1 + off, y1 + ext), W / 2 + off),    # lato x max, guarda −x
             ("3", (x1 + ext, y1 + off), (x0 - ext, y1 + off), D / 2 + off),    # lato y max, guarda −y
             ("4", (x0 - off, y1 + ext), (x0 - off, y0 - ext), W / 2 + off)]    # lato x min, guarda +x
    for label, p1, p2, depth in specs:
        a_, b_ = back([p1, p2])
        fronts.append({"label": label, "kind": "prospetto", "p1": a_.round(3).tolist(),
                       "p2": b_.round(3).tolist(), "t": round(float(depth), 2)})
    cy = (y0 + y1) / 2
    cx = (x0 + x1) / 2
    if W >= D:
        s1, s2 = back([(x0 - 0.5, cy), (x1 + 0.5, cy)])
    else:
        s1, s2 = back([(cx, y1 + 0.5), (cx, y0 - 0.5)])
    section = {"label": "A", "kind": "sezione", "p1": s1.round(3).tolist(), "p2": s2.round(3).tolist(),
               "t": 0.10, "beyond": 2.0}
    box = back([(x0, y0), (x1, y0), (x1, y1), (x0, y1)]).round(3).tolist()
    return {"fronts": fronts, "section": section, "box": box,
            "size": [round(float(W), 2), round(float(D), 2)]}


def decode_sample(prev: dict) -> np.ndarray:
    """Dal dict di anteprima ai punti (N,3) in coordinate assolute (per test e CLI)."""
    q = np.frombuffer(base64.b64decode(prev["data"]), dtype="<u2").reshape(-1, 3).astype(np.float64)
    b = np.array(prev["bbox"])
    return b[:3] + q / 65535 * np.maximum(b[3:] - b[:3], 1e-6)

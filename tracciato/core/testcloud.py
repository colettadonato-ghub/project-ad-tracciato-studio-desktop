"""Genera una nuvola di punti sintetica di un appartamento (per testare la pipeline).
Muri esterni 30 cm, tramezzi 10 cm, porte, finestre, arredi, rumore, rotazione arbitraria.
Usata anche da "AD-Tracciato-Studio-Pro --selftest".
"""
import numpy as np
import laspy


H = 2.70          # altezza interpiano netta
DENS = 4000       # punti per m² di superficie (un laser scanner reale ne ha molti di più)
rng = None
NOISE = 0.004     # rumore scanner (m)

def sample_box(x0, y0, z0, x1, y1, z1, faces="all"):
    """Punti sulla superficie di un parallelepipedo."""
    pts = []
    def face(axis, val, a0, a1, b0, b1):
        area = (a1 - a0) * (b1 - b0)
        n = max(int(area * DENS), 1)
        a = rng.uniform(a0, a1, n); b = rng.uniform(b0, b1, n); c = np.full(n, val)
        if axis == "x": return np.c_[c, a, b]
        if axis == "y": return np.c_[a, c, b]
        return np.c_[a, b, c]
    pts.append(face("x", x0, y0, y1, z0, z1)); pts.append(face("x", x1, y0, y1, z0, z1))
    pts.append(face("y", y0, x0, x1, z0, z1)); pts.append(face("y", y1, x0, x1, z0, z1))
    if faces == "all":
        pts.append(face("z", z1, x0, x1, y0, y1))
    return np.vstack(pts)

def wall(x0, y0, x1, y1, openings=()):
    """Muro rettangolare con aperture: (pos_inizio, larghezza, z_bottom, z_top) lungo l'asse del muro."""
    horiz = (x1 - x0) > (y1 - y0)
    L = (x1 - x0) if horiz else (y1 - y0)
    # spezzo il muro in pezzi pieni + architravi/davanzali
    pieces = []
    cuts = sorted(openings)
    s = 0.0
    for (p, w, zb, zt) in cuts:
        pieces.append((s, p, 0, H))
        if zb > 0: pieces.append((p, p + w, 0, zb))        # davanzale
        pieces.append((p, p + w, zt, H))                    # architrave
        s = p + w
    pieces.append((s, L, 0, H))
    pts = []
    for a, b, zb, zt in pieces:
        if b - a < 1e-6 or zt - zb < 1e-6: continue
        if horiz: pts.append(sample_box(x0 + a, y0, zb, x0 + b, y1, zt, faces="side"))
        else:     pts.append(sample_box(x0, y0 + a, zb, x1, y0 + b, zt, faces="side"))
    return np.vstack(pts)

def make_cloud(out, seed=42):
    global rng
    rng = np.random.default_rng(seed)
    W, D, T, t = 11.0, 8.0, 0.30, 0.10   # esterno 11x8 m
    parts = []
    # --- muri esterni (con finestre: davanzale 0.9, sopraluce 2.2) e porta d'ingresso
    parts.append(wall(0, 0, W, T, openings=[(1.5, 1.2, 0.9, 2.2), (6.5, 1.4, 0.9, 2.2)]))            # sud
    parts.append(wall(0, D - T, W, D, openings=[(2.0, 1.2, 0.9, 2.2), (7.8, 1.0, 0.0, 2.2)]))        # nord (porta-finestra)
    parts.append(wall(0, 0, T, D, openings=[(3.5, 1.0, 0.0, 2.1)]))                                    # ovest: ingresso
    parts.append(wall(W - T, 0, W, D, openings=[(2.0, 1.2, 0.9, 2.2), (5.2, 0.8, 1.2, 2.2)]))       # est (bagno: finestra alta)
    # --- tramezzi interni
    parts.append(wall(5.0, T, 5.0 + t, D - T, openings=[(1.2, 0.8, 0, 2.1), (5.0, 0.9, 0, 2.1)]))   # verticale centrale
    parts.append(wall(5.0 + t, 4.2, W - T, 4.2 + t, openings=[(0.6, 0.8, 0, 2.1)]))                  # orizzontale dx
    parts.append(wall(8.4, 4.2 + t, 8.4 + t, D - T, openings=[(1.0, 0.7, 0, 2.1)]))                  # bagno
    # --- pavimento e soffitto
    n = int(W * D * DENS * 0.5)
    parts.append(np.c_[rng.uniform(0, W, n), rng.uniform(0, D, n), np.zeros(n)])
    parts.append(np.c_[rng.uniform(0, W, n), rng.uniform(0, D, n), np.full(n, H)])
    # --- arredi (devono sparire dalla pianta)
    parts.append(sample_box(1.0, 5.0, 0, 2.6, 5.9, 0.80))          # divano
    parts.append(sample_box(2.2, 2.0, 0.72, 3.6, 3.0, 0.76))       # piano tavolo
    parts.append(sample_box(6.0, 0.6, 0, 7.8, 1.2, 2.10))          # armadio alto (non tocca il soffitto)
    parts.append(sample_box(9.0, 6.6, 0, 9.8, 7.3, 0.45))          # vasca/piatto doccia

    P = np.vstack(parts)
    P += rng.normal(0, NOISE, P.shape)
    # punti spuri (outlier)
    P = np.vstack([P, np.c_[rng.uniform(-1, W + 1, 3000), rng.uniform(-1, D + 1, 3000), rng.uniform(0, H, 3000)]])
    # rototraslazione arbitraria (la nuvola reale non è mai allineata agli assi)
    a = np.deg2rad(23.0)
    R = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]])
    P = P @ R.T + np.array([512340.0, 4523110.0, 112.35])

    hdr = laspy.LasHeader(point_format=3, version="1.2")
    hdr.offsets = P.min(0); hdr.scales = [0.001, 0.001, 0.001]
    las = laspy.LasData(hdr); las.x, las.y, las.z = P[:, 0], P[:, 1], P[:, 2]
    las.write(out)
    return len(P)


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "test_appartamento.las"
    print(f"Scritti {make_cloud(out):,} punti in {out}")

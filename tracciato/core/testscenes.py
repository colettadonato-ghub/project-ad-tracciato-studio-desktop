"""Nuvole sintetiche per verificare i nuovi tipi di rilievo (usate da --selftest).

  make_exterior(out)    edificio 12 × 9 m, H 6,40 m, rilevato da fuori: facciate con finestre e porta,
                        terreno, un albero, punti spuri. Rototraslato come un rilievo reale.
  make_multilevel(out)  interno su due livelli (solaio a +3,00 m), tramezzo, vano scala con rampa,
                        soppalco nel livello superiore.
"""
import numpy as np
import laspy

DENS = 2500
NOISE = 0.004


def _rect(rng, origin, u, v, lu, lv, holes=()):
    """Punti su un rettangolo origin + a*u + b*v, a∈[0,lu], b∈[0,lv]; holes = [(a0,a1,b0,b1)]."""
    n = max(int(lu * lv * DENS), 1)
    a = rng.uniform(0, lu, n)
    b = rng.uniform(0, lv, n)
    m = np.ones(n, bool)
    for a0, a1, b0, b1 in holes:
        m &= ~((a > a0) & (a < a1) & (b > b0) & (b < b1))
    a, b = a[m], b[m]
    return np.asarray(origin) + np.outer(a, u) + np.outer(b, v)


def _write(out, P, angle_deg, offset, rng):
    P = P + rng.normal(0, NOISE, P.shape)
    a = np.deg2rad(angle_deg)
    R = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]])
    P = P @ R.T + np.asarray(offset)
    hdr = laspy.LasHeader(point_format=3, version="1.2")
    hdr.offsets = P.min(0)
    hdr.scales = [0.001, 0.001, 0.001]
    las = laspy.LasData(hdr)
    las.x, las.y, las.z = P[:, 0], P[:, 1], P[:, 2]
    las.write(out)
    return len(P)


def make_exterior(out, seed=3):
    rng = np.random.default_rng(seed)
    W, D, H = 12.0, 9.0, 6.4
    X, Y, Z = np.eye(3)
    parts = []
    win = lambda a: (a, a + 1.2, 1.0, 2.4)
    win2 = lambda a: (a, a + 1.2, 4.0, 5.4)
    # facciate (solo la faccia esterna, come la vede lo scanner da fuori)
    parts.append(_rect(rng, (0, 0, 0), X, Z, W, H, [win(1.5), win(5.0), win(8.5), win2(1.5), win2(5.0), win2(8.5),
                                                   (10.2, 11.2, 0.0, 2.2)]))            # sud, con porta
    parts.append(_rect(rng, (0, D, 0), X, Z, W, H, [win(2.0), win(8.0), win2(2.0), win2(8.0)]))  # nord
    parts.append(_rect(rng, (0, 0, 0), Y, Z, D, H, [win(3.5), win2(3.5)]))                       # ovest
    parts.append(_rect(rng, (W, 0, 0), Y, Z, D, H, [win(3.5), win2(3.5)]))                       # est
    # cornicione: piccola sporgenza orizzontale in sommità
    parts.append(_rect(rng, (-0.3, -0.3, H), X, Y, W + 0.6, 0.3))
    parts.append(_rect(rng, (-0.3, D, H), X, Y, W + 0.6, 0.3))
    # terreno 30 × 26 m con leggera pendenza, meno denso lontano
    n = 220_000
    gx, gy = rng.uniform(-9, W + 9, n), rng.uniform(-8.5, D + 8.5, n)
    inside = (gx > 0) & (gx < W) & (gy > 0) & (gy < D)
    gx, gy = gx[~inside], gy[~inside]
    parts.append(np.c_[gx, gy, 0.01 * gx - 0.06])
    # albero
    t = rng.normal(0, 1, (6000, 3)) * [0.9, 0.9, 0.8] + [W + 4, D + 3, 3.5]
    parts.append(t)
    parts.append(np.c_[rng.normal(W + 4, 0.08, 800), rng.normal(D + 3, 0.08, 800), rng.uniform(0, 2.7, 800)])
    P = np.vstack(parts)
    P = np.vstack([P, np.c_[rng.uniform(-9, W + 9, 1500), rng.uniform(-9, D + 9, 1500), rng.uniform(0, 8, 1500)]])
    return _write(out, P, 17.0, (612340.0, 4823110.0, 54.20), rng)


def make_multilevel(out, seed=5):
    rng = np.random.default_rng(seed)
    W, D = 10.0, 7.0
    h1, slab, h2 = 2.75, 0.25, 2.75            # piano terra 2,75 · solaio 25 cm · primo piano 2,75
    z1 = h1 + slab                              # +3,00 pavimento primo piano
    X, Y, Z = np.eye(3)
    parts = []
    stair = (6.2, 9.6, 0.3, 1.5)                # foro scala nel solaio: x 6,2–9,6 · y 0,3–1,5
    for zb, hh in ((0.0, h1), (z1, h2)):
        # muri perimetrali (facce interne) con una finestra per lato
        parts.append(_rect(rng, (0, 0, zb), X, Z, W, hh, [(2, 3.2, 0.9, 2.2)]))
        parts.append(_rect(rng, (0, D, zb), X, Z, W, hh, [(6, 7.2, 0.9, 2.2)]))
        parts.append(_rect(rng, (0, 0, zb), Y, Z, D, hh, [(3, 4.0, 0.0, 2.1)] if zb == 0 else [(3, 4.2, 0.9, 2.2)]))
        parts.append(_rect(rng, (W, 0, zb), Y, Z, D, hh))
        # tramezzo a x = 4 con porta
        parts.append(_rect(rng, (4.0, 0, zb), Y, Z, D, hh, [(4.5, 5.4, 0, 2.1)]))
        parts.append(_rect(rng, (4.1, 0, zb), Y, Z, D, hh, [(4.5, 5.4, 0, 2.1)]))
    # pavimento piano terra, soffitto piano terra (con foro scala), pavimento primo piano (con foro), soffitto
    parts.append(_rect(rng, (0, 0, 0), X, Y, W, D))
    hole = [(stair[0], stair[1], stair[2], stair[3])]
    parts.append(_rect(rng, (0, 0, h1), X, Y, W, D, hole))
    parts.append(_rect(rng, (0, 0, z1), X, Y, W, D, hole))
    parts.append(_rect(rng, (0, 0, z1 + h2), X, Y, W, D))
    # rampa: 17 gradini da x 9,6 verso x 6,2 (alzata ~17,6 cm, pedata ~20 cm), larghezza y 0,3–1,5
    n_st = 17
    rise, run = z1 / n_st, (stair[1] - stair[0]) / n_st
    for i in range(n_st):
        x0 = stair[1] - (i + 1) * run
        parts.append(_rect(rng, (x0, stair[2], (i + 1) * rise), X, Y, run, stair[3] - stair[2]))
        parts.append(_rect(rng, (x0 + run, stair[2], i * rise), Y, Z, stair[3] - stair[2], rise))
    # soppalco nel primo piano: piano a +1,20 dal pavimento su x 0–3
    parts.append(_rect(rng, (0.1, 0.1, z1 + 1.2), X, Y, 3.0, D - 0.2))
    # arredi
    parts.append(_rect(rng, (1.0, 5.0, 0.75), X, Y, 1.6, 0.9))
    P = np.vstack(parts)
    P = np.vstack([P, np.c_[rng.uniform(0, W, 2000), rng.uniform(0, D, 2000), rng.uniform(0, 5.75, 2000)]])
    return _write(out, P, -32.0, (512900.0, 4523000.0, 8.40), rng)


if __name__ == "__main__":
    import sys
    print(make_exterior(sys.argv[1] if len(sys.argv) > 1 else "test_esterno.las"))
    print(make_multilevel(sys.argv[2] if len(sys.argv) > 2 else "test_multipiano.las"))

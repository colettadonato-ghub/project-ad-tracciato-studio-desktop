#!/usr/bin/env python3
"""
pointcloud2plan — dalla nuvola di punti (.las/.laz/.e57) alla pianta dell'immobile.

Output:
  <nome>.dxf  layer: MURI (contorni), MURI_CAMPITURA, FINESTRE, PORTE, VANI (poligoni),
              TESTI (nome vano + m²), QUOTE, NUVOLA_SEZIONE (punti della sezione, per verifica/ricalco)
  <nome>.pdf  tavola A3 in scala (default 1:100) con cartiglio
  <nome>.png  anteprima

Pipeline:
  1. lettura + sottocampionamento voxel + rimozione outlier
  2. livelli pavimento/soffitto (istogramma Z)
  3. allineamento "Manhattan" (rotazione attorno a Z sulle direzioni dominanti dei muri)
  4. rasterizzazione di 3 fasce:  sezione (h=1.10 m),  bassa (0.25-0.60 m),  alta (sotto soffitto)
  5. muri  = sezione ∩ fascia alta   (gli arredi non arrivano al soffitto → spariscono)
     ingombro muri = fascia alta     (contiene anche architravi sopra porte/finestre)
     aperture = ingombro − muri;  con davanzale nella fascia bassa → FINESTRA, altrimenti PORTA
  6. vani = spazio libero chiuso dall'ingombro muri → poligoni + superficie
  7. vettorializzazione (contorni, semplificazione, snap a 0/90°) → DXF + PDF

Uso:
  python pointcloud2plan.py rilievo.e57 --out pianta --scale 100 --cut 1.10
  python pointcloud2plan.py rilievo.las --floor-z 0.0 --ceil-z 2.70   (forza i livelli)
"""
import argparse, logging, math, os, sys
import numpy as np
import cv2
from scipy import ndimage

log = logging.getLogger("tracciato.engine")

class Cancelled(Exception):
    """Elaborazione annullata dall'utente."""

class Hooks:
    """Callback opzionali usati dall'interfaccia: avanzamento, fasi, annullamento."""
    def __init__(self, progress=None, stage=None, cancelled=None):
        self.progress = progress or (lambda stage, frac: None)
        self.stage = stage or (lambda name: None)
        self._cancelled = cancelled or (lambda: False)
    def check(self):
        if self._cancelled(): raise Cancelled()

# ----------------------------------------------------------------------------- I/O
def load_cloud(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".las", ".laz"):
        import laspy
        las = laspy.read(path)
        return np.c_[las.x, las.y, las.z].astype(np.float64)
    if ext == ".e57":
        import pye57
        e57 = pye57.E57(path)
        chunks = []
        for i in range(e57.scan_count):
            d = e57.read_scan(i, ignore_missing_fields=True, transform=True)  # applica la registrazione
            chunks.append(np.c_[d["cartesianX"], d["cartesianY"], d["cartesianZ"]])
        return np.vstack(chunks).astype(np.float64)
    raise ValueError(f"Formato non supportato: {ext}")

def iter_chunks(path, chunk=2_000_000, e57_use_raw=False):
    """Legge la nuvola a blocchi (LAS/LAZ ed E57 a blocchi di `chunk` punti: memoria costante)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".las", ".laz"):
        import laspy
        with laspy.open(path) as f:
            total = max(int(f.header.point_count), 1); done = 0
            for pts in f.chunk_iterator(chunk):
                done += len(pts)
                yield np.c_[pts.x, pts.y, pts.z], done / total
    elif ext == ".e57":
        yield from iter_e57(path, chunk=min(chunk, 2_000_000), force_raw=e57_use_raw)
    else:
        raise ValueError(f"Formato non supportato: {ext}")

def _check_ram(needed_gb: float, label: str):
    """Controlla che ci sia abbastanza RAM prima di allocare. Lancia RuntimeError se non basta."""
    try:
        from tracciato.core.logs import memory_info
        mem = memory_info()
        if mem:
            avail_gb = mem[1] / 1024 ** 3
            if avail_gb < needed_gb:
                raise RuntimeError(
                    f"{label}: RAM insufficiente — disponibile {avail_gb:.1f} GB, "
                    f"necessari almeno {needed_gb:.1f} GB. "
                    "Chiudi altri programmi e riprova, oppure usa il dettaglio 'Medio' o 'Basso'."
                )
            log.debug(f"{label}: RAM disponibile {avail_gb:.1f} GB (OK, serve ~{needed_gb:.1f} GB)")
    except RuntimeError:
        raise
    except Exception:
        pass   # se non riesce a leggere la RAM, continua comunque


def _iter_e57_raw(path, chunk=2_000_000):
    """Parser E57 puro-Python senza pye57/libe57.

    Gestisce entrambi i formati di scansione:
    1. CompressedVector con DataPacket (formato standard ASTM E57 — es. Matterport EOS)
    2. ByteStream diretto (formato non-standard usato da alcuni exporter)

    Struttura file E57:
    - Pagine da 1024 byte: 4 byte CRC32C + 1020 byte payload
    - File header a offset fisico 4 (payload pagina 0)
    - xmlPhysOffset = offset fisico dell'XML nell'header
    - CompressedVector fileOffset = offset fisico della CVSectionHeader

    CompressedVectorSectionHeader (32 byte):
      sectionId(1) | reserved(7) | sectionLogicalLength(8)
      | dataPhysicalOffset(8) | indexPhysicalOffset(8)

    DataPacket header (6 byte):
      packetType(1) | reserved(1) | packetLogicalLengthMinus1(2 LE)
      | bytestreamCount(2 LE)
      + bytestreamBufferLength[0..bsc-1] (uint16 LE each)

    I bytestream per Float precision="single" sono float32 raw (LE).
    Il campo cartesianInvalidState (Integer min=0 max=2) è bitpacked 2 bit/record LSB-first.
    """
    import struct, re

    PAGE = 1024
    PAYLOAD = PAGE - 4  # 1020 byte utili per pagina

    # ---- navigazione offset fisici (saltando CRC di ogni pagina) ----

    def _phys_read(f, phys_start, n):
        """Legge n byte dall'offset fisico phys_start, saltando i 4 byte CRC di ogni pagina."""
        buf = bytearray()
        pos = int(phys_start)
        while len(buf) < n:
            page  = pos // PAGE
            off_p = pos % PAGE
            if off_p < 4:                       # siamo dentro i 4 byte CRC: salta al payload
                pos = page * PAGE + 4
                off_p = 4
            end_of_page = (page + 1) * PAGE
            can = min(end_of_page - pos, n - len(buf))
            f.seek(pos)
            chunk_data = f.read(can)
            if not chunk_data:
                break
            buf += chunk_data
            pos += len(chunk_data)
        return bytes(buf)

    def _phys_advance(phys, n_payload):
        """Ritorna l'offset fisico dopo n_payload byte di payload dall'offset fisico phys."""
        pos = int(phys)
        rem = int(n_payload)
        while rem > 0:
            page  = pos // PAGE
            off_p = pos % PAGE
            if off_p < 4:
                pos = page * PAGE + 4
                off_p = 4
            end_of_page = (page + 1) * PAGE
            step = min(end_of_page - pos, rem)
            pos += step
            rem -= step
        return pos

    # ---- lettura XML dall'header del file ----

    with open(path, "rb") as f:
        # Il file header E57 (44 byte) è nel payload della pagina 0 (a offset fisico 4)
        # Layout: signature[8] | major[2] | minor[2] | filePhysLen[8]
        #         | xmlPhysOffset[8] | xmlPhysLen[8] | pageSize[8]
        hdr = _phys_read(f, 4, 44)
        sig = hdr[:8]
        # Accetta sia "ASTM-E57" (file senza page-wrap) che "-E57\x01..." (payload pg0)
        if b"ASTM-E57" not in hdr[:8] and sig != b"-E57\x01\x00\x00\x00":
            raise RuntimeError(f"Non è un file E57 valido: {path}")
        xml_phys_off = struct.unpack_from("<Q", hdr, 20)[0]
        xml_phys_len = struct.unpack_from("<Q", hdr, 28)[0]
        xml_bytes = _phys_read(f, xml_phys_off, xml_phys_len)

    xml = xml_bytes.decode("utf-8", errors="replace")

    # ---- parse XML: estrai scansioni ----
    # Il re.findall flat cattura anche i <vectorChild> ANNIDATI dentro ogni scansione
    # (es. i sotto-elementi di pose/data), restituendo decine di blocchi invece dei
    # 9 scan di primo livello. Usiamo un parser depth-aware che considera solo i
    # <vectorChild> figli diretti di <data3D>.

    def _extract_top_level_vectorchild(xml_str):
        """Ritorna solo i <vectorChild> di primo livello dentro <data3D>."""
        # Trova il contenuto di <data3D>
        m_d3d = re.search(r'<data3D\b[^>]*>(.*?)</data3D>', xml_str, re.DOTALL)
        d3d = m_d3d.group(1) if m_d3d else xml_str
        blocks = []
        depth = 0
        i = 0
        start = 0
        TAG_OPEN  = '<vectorChild'
        TAG_CLOSE = '</vectorChild>'
        len_open  = len(TAG_OPEN)
        len_close = len(TAG_CLOSE)
        while i < len(d3d):
            if d3d[i:i+len_open] == TAG_OPEN:
                depth += 1
                if depth == 1:
                    start = i
                i += len_open
            elif d3d[i:i+len_close] == TAG_CLOSE:
                if depth == 1:
                    # estrai il contenuto (senza i tag esterni)
                    full = d3d[start:i+len_close]
                    # rimuovi il tag di apertura (fino al primo >) e il tag di chiusura
                    inner_start = full.index('>') + 1
                    inner_end   = full.rfind('<')
                    blocks.append(full[inner_start:inner_end])
                depth -= 1
                i += len_close
            else:
                i += 1
        return blocks

    scan_blocks = _extract_top_level_vectorchild(xml)
    if not scan_blocks:
        scan_blocks = [xml]

    def _find_pose(block):
        """Estrae quaternione + traslazione dalla posa della scansione.
        Ancoriamo qx/qy/qz alla sezione <rotation> per evitare di pescare
        i valori <x>/<y> della <translation> (che possono avere |val|>1).
        """
        rot_m = re.search(r'<rotation\b[^>]*>(.*?)</rotation>', block, re.DOTALL)
        rot_block = rot_m.group(1) if rot_m else block
        qw = re.search(r'<w\b[^>]*>\s*([-\d.eE+]+)\s*</w>', rot_block)
        qx = re.search(r'<x\b[^>]*>\s*([-\d.eE+]+)\s*</x>', rot_block)
        qy = re.search(r'<y\b[^>]*>\s*([-\d.eE+]+)\s*</y>', rot_block)
        qz = re.search(r'<z\b[^>]*>\s*([-\d.eE+]+)\s*</z>', rot_block)
        tx = re.search(r'<translation[^>]*>.*?<x[^>]*>\s*([-\d.eE+]+)\s*</x>', block, re.DOTALL)
        ty = re.search(r'<translation[^>]*>.*?<y[^>]*>\s*([-\d.eE+]+)\s*</y>', block, re.DOTALL)
        tz = re.search(r'<translation[^>]*>.*?<z[^>]*>\s*([-\d.eE+]+)\s*</z>', block, re.DOTALL)
        if qw and qx and qy and qz:
            w, x, y, z = float(qw.group(1)), float(qx.group(1)), float(qy.group(1)), float(qz.group(1))
            # Valida norma: quaternione deve avere norma ≈ 1 (XML corrotto → valori assurdi)
            norm = (w*w + x*x + y*y + z*z) ** 0.5
            if norm < 0.5 or norm > 2.0:
                return None, None
            w, x, y, z = w/norm, x/norm, y/norm, z/norm
            # quaternione → matrice di rotazione 3×3
            R = np.array([
                [1-2*(y*y+z*z),   2*(x*y-z*w),   2*(x*z+y*w)],
                [  2*(x*y+z*w), 1-2*(x*x+z*z),   2*(y*z-x*w)],
                [  2*(x*z-y*w),   2*(y*z+x*w), 1-2*(x*x+y*y)],
            ], dtype=np.float64)
            T = np.array([
                float(tx.group(1)) if tx else 0.0,
                float(ty.group(1)) if ty else 0.0,
                float(tz.group(1)) if tz else 0.0,
            ], dtype=np.float64)
            return R, T
        return None, None

    scans = []
    for block in scan_blocks:
        # recordCount o pointCount
        m = re.search(r'recordCount\s*=\s*"(\d+)"', block)
        if not m:
            m = re.search(r"<pointCount[^>]*>\s*(\d+)\s*</pointCount>", block)
        if not m:
            continue
        record_count = int(m.group(1))

        # fileOffset della CompressedVectorSection
        m = re.search(r'<points[^>]+fileOffset\s*=\s*"(\d+)"', block)
        if not m:
            continue
        cv_phys = int(m.group(1))

        # Rileva se sono coordinate cartesiane o sferiche
        has_cart = bool(re.search(r'<cartesianX\b', block))
        has_sph  = bool(re.search(r'<sphericalRange\b', block))
        if not has_cart and not has_sph:
            continue

        # Rileva se ha cartesianInvalidState (bitpacked, 2 bit per record)
        has_invalid = bool(re.search(r'<cartesianInvalidState\b', block))

        # Prototype: estrai min/max per X/Y/Z → ScaledInteger se presenti
        def _proto_range(xml_block, field):
            """Ritorna (min_val, max_val) dal prototipo, o (None, None) se assente."""
            fm = re.search(rf'<{field}\b([^>]*/?>)', xml_block)
            if not fm:
                return None, None
            tag = fm.group(1)
            mn = re.search(r'minimum\s*=\s*"([^"]+)"', tag)
            mx = re.search(r'maximum\s*=\s*"([^"]+)"', tag)
            mn_val = None
            mx_val = None
            if mn:
                try: mn_val = float(mn.group(1))
                except ValueError: pass
            if mx:
                try: mx_val = float(mx.group(1))
                except ValueError: pass
            if mn_val is not None and mx_val is not None:
                return mn_val, mx_val
            # Fallback: range simmetrico (Matterport usa sempre [-R, +R])
            if mn_val is not None:
                return mn_val, -mn_val   # es. min=-246 → max=+246
            return None, None

        proto_m = re.search(r'<points\b[^>]*>(.*?)</points>', block, re.DOTALL)
        proto_xml = proto_m.group(1) if proto_m else block
        x_min, x_max = _proto_range(proto_xml, "cartesianX")
        y_min, y_max = _proto_range(proto_xml, "cartesianY")
        z_min, z_max = _proto_range(proto_xml, "cartesianZ")
        # ScaledInteger: min e max entrambi presenti → int32 → float con formula di scala
        xyz_scaled = (x_min is not None and x_max is not None and
                      y_min is not None and y_max is not None and
                      z_min is not None and z_max is not None)

        R, T = _find_pose(block)
        scans.append({
            "count":      record_count,
            "cv_phys":    cv_phys,
            "sph":        has_sph and not has_cart,
            "invalid":    has_invalid,
            "R": R, "T": T,
            "xyz_scaled": xyz_scaled,
            "x_range":    (x_min, x_max),
            "y_range":    (y_min, y_max),
            "z_range":    (z_min, z_max),
        })

    # Fallback: formato byteStream diretto (vecchio stile non-standard)
    if not scans:
        log.warning("E57 raw: nessun CompressedVector trovato, provo formato byteStream diretto")

        def find_stream(xml_block, field_name):
            pat = (r'<' + field_name + r'\b[^>]*>.*?'
                   r'<byteStream\s[^>]*fileOffset\s*=\s*"(\d+)"[^>]*length\s*=\s*"(\d+)"')
            m2 = re.search(pat, xml_block, re.DOTALL)
            if m2:
                return int(m2.group(1)), int(m2.group(2))
            return None

        for block in scan_blocks:
            m = re.search(r"<pointCount[^>]*>\s*(\d+)\s*</pointCount>", block)
            if not m:
                continue
            point_count = int(m.group(1))
            cx = find_stream(block, "cartesianX")
            cy = find_stream(block, "cartesianY")
            cz = find_stream(block, "cartesianZ")
            if cx and cy and cz:
                scans.append({"count": point_count, "cv_phys": None,
                               "stream_xyz": (cx, cy, cz), "sph": False,
                               "invalid": False, "R": None, "T": None})

    if not scans:
        raise RuntimeError(
            "E57 raw: nessuna scansione leggibile trovata nel file.\n"
            "Il file usa una struttura dati non standard o una compressione non supportata dal parser integrato.\n"
            "Prova a riesportarlo da Leica Cyclone, Faro Scene o ReCap come 'E57 standard'."
        )

    total = max(sum(s["count"] for s in scans), 1)
    log.info(f"E57 raw: {len(scans)} scansioni, {total:,} punti totali (parser integrato)")

    with open(path, "rb") as f:
        done = 0
        for si, scan in enumerate(scans):
            record_count = scan["count"]
            R_pose, T_pose = scan["R"], scan["T"]
            log.info(f"→ Scansione {si+1}/{len(scans)}: {record_count:,} punti"
                     f"{' con posa' if R_pose is not None else ''}")

            # ---- formato byteStream diretto (fallback legacy) ----
            if scan.get("stream_xyz"):
                def read_floats_legacy(offset, length):
                    data = _phys_read(f, offset, length)
                    n = len(data) // 4
                    return np.frombuffer(data[:n*4], dtype="<f4").astype(np.float64)
                xs = read_floats_legacy(*scan["stream_xyz"][0])
                ys = read_floats_legacy(*scan["stream_xyz"][1])
                zs = read_floats_legacy(*scan["stream_xyz"][2])
                n_pts = min(len(xs), len(ys), len(zs), record_count)
                xyz_all = np.column_stack((xs[:n_pts], ys[:n_pts], zs[:n_pts]))
                valid = np.isfinite(xyz_all).all(axis=1)
                xyz_all = xyz_all[valid]
                if R_pose is not None:
                    xyz_all = xyz_all @ R_pose.T + T_pose
                for start in range(0, len(xyz_all), chunk):
                    blk = xyz_all[start:start + chunk]
                    done += len(blk)
                    yield blk, done / total
                continue

            # ---- formato CompressedVector (standard ASTM E57) ----
            cv_phys = scan["cv_phys"]

            # Leggi CompressedVectorSectionHeader (32 byte)
            cv_hdr = _phys_read(f, cv_phys, 32)
            data_phys = struct.unpack_from("<q", cv_hdr, 16)[0]

            rec_done = 0
            pkt_phys = data_phys
            accum_x, accum_y, accum_z = [], [], []
            accum_n = 0

            while rec_done < record_count:
                # Leggi header pacchetto (6 byte fissi)
                pkt_hdr = _phys_read(f, pkt_phys, 6)
                if len(pkt_hdr) < 6:
                    break
                pkt_type   = pkt_hdr[0]
                pkt_loglen = struct.unpack_from("<H", pkt_hdr, 2)[0] + 1
                bsc        = struct.unpack_from("<H", pkt_hdr, 4)[0]

                if pkt_type == 2:
                    # EmptyPacket → fine sezione
                    break
                if pkt_type not in (0, 1):
                    # tipo sconosciuto → interrompi
                    break

                # pkt_type == 1 con bsc == 0 → IndexPacket: salta
                # pkt_type == 1 con bsc > 0  → DataPacket non-standard (Matterport): elabora
                # pkt_type == 0              → DataPacket standard
                if pkt_type == 1 and bsc == 0:
                    pkt_phys = _phys_advance(pkt_phys, pkt_loglen)
                    continue

                # DataPacket — salta se bsc anomalo (>100 = pkt corrotto)
                if bsc < 3 or bsc > 100:
                    pkt_phys = _phys_advance(pkt_phys, pkt_loglen)
                    continue

                # Leggi bytestreamBufferLengths
                bs_lens_raw = _phys_read(f, _phys_advance(pkt_phys, 6), bsc * 2)
                bs_lens = [struct.unpack_from("<H", bs_lens_raw, i*2)[0] for i in range(bsc)]

                # Posizione del primo bytestream (dopo header 6 + bsc*2 byte)
                hdr_sz = 6 + bsc * 2
                pos = _phys_advance(pkt_phys, hdr_sz)

                # Leggi i bytestream: X=bs[0], Y=bs[1], Z=bs[2], InvalidState=bs[3] (se presente)
                raw_x = _phys_read(f, pos, bs_lens[0])
                pos   = _phys_advance(pos, bs_lens[0])
                raw_y = _phys_read(f, pos, bs_lens[1])
                pos   = _phys_advance(pos, bs_lens[1])
                raw_z = _phys_read(f, pos, bs_lens[2])
                pos   = _phys_advance(pos, bs_lens[2])

                # Tronca i buffer a multiplo di 4 (robustezza contro letture parziali di pagina)
                raw_x = raw_x[: (len(raw_x) // 4) * 4]
                raw_y = raw_y[: (len(raw_y) // 4) * 4]
                raw_z = raw_z[: (len(raw_z) // 4) * 4]
                n_rec = min(len(raw_x), len(raw_y), len(raw_z)) // 4

                if n_rec == 0:
                    rec_done += 0
                    pkt_phys = _phys_advance(pkt_phys, pkt_loglen)
                    continue

                if scan["xyz_scaled"]:
                    # ScaledInteger: i byte sono int32 LE; scala lineare su [min, max]
                    # Formula: val = min + (int_val - INT32_MIN) / UINT32_MAX * (max - min)
                    _INT32_MIN = -(2**31)
                    _UINT32_MAX = float((2**32) - 1)
                    ix = np.frombuffer(raw_x[:n_rec*4], dtype="<i4").astype(np.float64)
                    iy = np.frombuffer(raw_y[:n_rec*4], dtype="<i4").astype(np.float64)
                    iz = np.frombuffer(raw_z[:n_rec*4], dtype="<i4").astype(np.float64)
                    xmn, xmx = scan["x_range"]; ymn, ymx = scan["y_range"]; zmn, zmx = scan["z_range"]
                    xs_pkt = xmn + (ix - _INT32_MIN) / _UINT32_MAX * (xmx - xmn)
                    ys_pkt = ymn + (iy - _INT32_MIN) / _UINT32_MAX * (ymx - ymn)
                    zs_pkt = zmn + (iz - _INT32_MIN) / _UINT32_MAX * (zmx - zmn)
                    n_rec = min(len(xs_pkt), len(ys_pkt), len(zs_pkt))
                    xs_pkt = xs_pkt[:n_rec]
                    ys_pkt = ys_pkt[:n_rec]
                    zs_pkt = zs_pkt[:n_rec]
                else:
                    # Float32 raw LE (formato standard ASTM E57)
                    xs_pkt = np.frombuffer(raw_x[:n_rec*4], dtype="<f4").astype(np.float64)
                    ys_pkt = np.frombuffer(raw_y[:n_rec*4], dtype="<f4").astype(np.float64)
                    zs_pkt = np.frombuffer(raw_z[:n_rec*4], dtype="<f4").astype(np.float64)
                    n_rec = min(len(xs_pkt), len(ys_pkt), len(zs_pkt))
                    xs_pkt = xs_pkt[:n_rec]; ys_pkt = ys_pkt[:n_rec]; zs_pkt = zs_pkt[:n_rec]

                if n_rec == 0:
                    pkt_phys = _phys_advance(pkt_phys, pkt_loglen)
                    continue

                # Maschera validità: bitpacked 2 bit/record LSB-first (cartesianInvalidState)
                if scan["invalid"] and bsc >= 4 and bs_lens[3] > 0:
                    raw_inv = _phys_read(f, pos, bs_lens[3])
                    inv_arr = np.frombuffer(raw_inv, dtype=np.uint8)
                    bits = np.unpackbits(inv_arr, bitorder="little")
                    n_bits = n_rec * 2
                    if len(bits) >= n_bits:
                        invalid_state = bits[:n_bits].reshape(n_rec, 2).dot([1, 2]).astype(np.uint8)
                        valid_mask = (invalid_state == 0)
                    else:
                        # bits insufficienti: prendi quello che c'è
                        n_avail = len(bits) // 2
                        if n_avail > 0:
                            invalid_state = bits[:n_avail*2].reshape(n_avail, 2).dot([1, 2]).astype(np.uint8)
                            valid_mask = np.zeros(n_rec, dtype=bool)
                            valid_mask[:n_avail] = (invalid_state == 0)
                        else:
                            valid_mask = np.isfinite(xs_pkt) & np.isfinite(ys_pkt) & np.isfinite(zs_pkt)
                else:
                    if scan["xyz_scaled"]:
                        # ScaledInteger: tutti i valori sono finiti per costruzione;
                        # filtra solo quelli fuori range (corruzione dati)
                        xmn, xmx = scan["x_range"]; ymn, ymx = scan["y_range"]; zmn, zmx = scan["z_range"]
                        valid_mask = (
                            (xs_pkt >= xmn - 1.0) & (xs_pkt <= xmx + 1.0) &
                            (ys_pkt >= ymn - 1.0) & (ys_pkt <= ymx + 1.0) &
                            (zs_pkt >= zmn - 1.0) & (zs_pkt <= zmx + 1.0)
                        )
                    else:
                        valid_mask = np.isfinite(xs_pkt) & np.isfinite(ys_pkt) & np.isfinite(zs_pkt)

                xs_v = xs_pkt[valid_mask].astype(np.float64)
                ys_v = ys_pkt[valid_mask].astype(np.float64)
                zs_v = zs_pkt[valid_mask].astype(np.float64)

                if len(xs_v) > 0:
                    accum_x.append(xs_v)
                    accum_y.append(ys_v)
                    accum_z.append(zs_v)
                    accum_n += len(xs_v)

                rec_done += n_rec
                pkt_phys = _phys_advance(pkt_phys, pkt_loglen)

                # Flush accumulated points when we have enough for a chunk
                if accum_n >= chunk:
                    xyz_blk = np.column_stack((
                        np.concatenate(accum_x),
                        np.concatenate(accum_y),
                        np.concatenate(accum_z),
                    ))
                    if R_pose is not None:
                        xyz_blk = xyz_blk @ R_pose.T + T_pose
                    fin = np.isfinite(xyz_blk).all(axis=1)
                    xyz_blk = xyz_blk[fin]
                    for start in range(0, len(xyz_blk), chunk):
                        blk = xyz_blk[start:start + chunk]
                        done += len(blk)
                        yield blk, min(done / total, 1.0)
                    accum_x, accum_y, accum_z = [], [], []
                    accum_n = 0

            # Flush remaining
            if accum_n > 0:
                xyz_blk = np.column_stack((
                    np.concatenate(accum_x),
                    np.concatenate(accum_y),
                    np.concatenate(accum_z),
                ))
                if R_pose is not None:
                    xyz_blk = xyz_blk @ R_pose.T + T_pose
                fin = np.isfinite(xyz_blk).all(axis=1)
                xyz_blk = xyz_blk[fin]
                for start in range(0, len(xyz_blk), chunk):
                    blk = xyz_blk[start:start + chunk]
                    done += len(blk)
                    yield blk, min(done / total, 1.0)

            log.info(f"   Scansione {si+1}: {rec_done:,} record processati, {done:,} punti validi accumulati")

        log.info("E57 raw: lettura completata")


def iter_e57(path, chunk=2_000_000, force_raw=False):
    """Legge un E57 a blocchi di dimensione fissa, una scansione alla volta.

    Se `force_raw=True` (impostato da pipeline.py quando l'inspect raw ha trovato i dati),
    usa direttamente il parser E57 integrato (puro Python) senza nemmeno tentare pye57/libe57.
    Questo evita i crash nativi (0xC0000005) su file prodotti da software non conformi.

    Se `force_raw=False`, tenta pye57/libe57 prima e cade sul raw parser solo in caso di
    eccezione Python (non per crash nativi in C++, che uccidono il processo).
    """
    if force_raw:
        log.info("E57: uso parser integrato (puro Python) — pye57/libe57 non verrà caricato")
        yield from _iter_e57_raw(path, chunk=chunk)
        return

    import pye57
    from pye57 import libe57

    # Prova ad aprire il file con pye57: se crasha in C++ (es. 0xC0000005) il processo muore
    # comunque — ma se lancia un'eccezione Python, possiamo usare il fallback.
    pye57_ok = False
    e57 = None
    try:
        e57 = pye57.E57(path)
        pye57_ok = True
    except Exception as ex:
        log.warning(
            f"pye57.E57() non ha aperto il file ({ex}); "
            "uso il parser E57 integrato (puro Python) come fallback."
        )

    if not pye57_ok:
        yield from _iter_e57_raw(path, chunk=chunk)
        return

    headers = [e57.get_header(i) for i in range(e57.scan_count)]
    total = max(sum(h.point_count for h in headers), 1)
    log.info(f"E57: {e57.scan_count} scansioni, {total:,} punti totali")

    # Stima memoria: 3 float64 × chunk punti × 2 (array + copia numpy) = ~48 MB per chunk da 1 M punti
    chunk_gb = (chunk * 3 * 8 * 2) / 1024 ** 3
    _check_ram(chunk_gb + 0.5, "iter_e57 pre-lettura")

    done = 0
    for i, h in enumerate(headers):
        fields = set(h.point_fields)
        if {"cartesianX", "cartesianY", "cartesianZ"} <= fields:
            names, inv, sph = ["cartesianX", "cartesianY", "cartesianZ"], "cartesianInvalidState", False
        elif {"sphericalRange", "sphericalAzimuth", "sphericalElevation"} <= fields:
            names, inv, sph = ["sphericalRange", "sphericalAzimuth", "sphericalElevation"], "sphericalInvalidState", True
        else:
            log.warning(f"Scansione {i + 1}/{len(headers)}: nessuna coordinata leggibile, saltata")
            continue
        if inv in fields:
            names = names + [inv]
        cap = int(min(chunk, max(h.point_count, 1)))
        arrays, buffers = {}, libe57.VectorSourceDestBuffer()
        for f in names:
            arr, buf = e57.make_buffer(f, cap)
            arrays[f] = arr
            buffers.append(buf)
        R = np.asarray(h.rotation_matrix) if h.has_pose() else None
        T = np.asarray(h.translation) if h.has_pose() else None
        log.info(f"→ Scansione {i + 1}/{len(headers)}: {h.point_count:,} punti"
                 f"{' (sferiche)' if sph else ''}{' con posa' if R is not None else ''}"
                 f" — avvio lettura chunk da {cap:,} punti")
        reader = h.points.reader(buffers)
        chunk_num = 0
        try:
            while True:
                chunk_num += 1
                log.debug(f"   Scansione {i + 1}, chunk {chunk_num} — reader.read() ...")
                n = reader.read()
                log.debug(f"   Scansione {i + 1}, chunk {chunk_num} — letti {n} punti")
                if n <= 0:
                    break
                a = [arrays[f][:n] for f in names[:3]]
                if sph:
                    r, az, el = a
                    ce = np.cos(el)
                    xyz = np.column_stack((r * ce * np.cos(az), r * ce * np.sin(az), r * np.sin(el)))
                else:
                    xyz = np.column_stack(a)
                if inv in arrays:
                    xyz = xyz[arrays[inv][:n] == 0]
                if R is not None:
                    xyz = xyz @ R.T + T
                done += n
                yield xyz, done / total
        finally:
            reader.close()
        log.info(f"   Scansione {i + 1} completata ({chunk_num} chunk)")


def load_bands_streaming(path, a, hooks=None):
    """Due passate sul file, memoria proporzionale solo ai punti utili:
       1) campione di Z (1 punto ogni 20) → pavimento e soffitto
       2) tiene solo le fasce di quota usate dalla pianta, sottocampionate a voxel blocco per blocco."""
    hooks = hooks or Hooks()
    e57_raw = getattr(a, "e57_use_raw", False)   # True → usa parser integrato, evita pye57/libe57
    if a.floor_z is None or a.ceil_z is None:
        hooks.stage("levels")
        zs = []
        for c, frac in iter_chunks(path, e57_use_raw=e57_raw):
            hooks.check(); zs.append(c[::20, 2].copy()); hooks.progress("levels", frac)
        zf, zc = detect_levels(np.concatenate(zs)); del zs
    else:
        zf, zc = a.floor_z, a.ceil_z
    H = zc - zf
    # Margine attorno ai livelli rilevati per escludere scan con posa anomala.
    # Usa ±max(2m, H) per coprire edifici alti ma tagliare scan fuori posto di centinaia di metri.
    _margin = max(2.0, H)
    z_global_lo = zf - _margin
    z_global_hi = zc + _margin
    bands = [(a.cut - 0.06, a.cut + 0.06), (0.25, 0.60), (H - a.high_band - 0.01, H - 0.07)]
    hooks.stage("read")
    kept, n_read = [], 0
    for c, frac in iter_chunks(path, e57_use_raw=e57_raw):
        hooks.check(); hooks.progress("read", frac)
        n_read += len(c)
        # Scarta punti con Z fuori range plausibile (scan con posa errata)
        z_abs = c[:, 2]
        c = c[(z_abs >= z_global_lo) & (z_abs <= z_global_hi)]
        if len(c) == 0:
            del c; continue
        z = c[:, 2] - zf
        m = np.zeros(len(c), bool)
        for lo, hi in bands: m |= (z > lo) & (z < hi)
        kept.append(voxel_downsample(c[m], a.voxel))
        del c, z, m
    kept = [k for k in kept if len(k) > 0]
    if not kept:
        raise ValueError(f"Nessun punto nelle fasce Z [{zf:.2f},{zc:.2f}] — controlla floor_z/ceil_z o il file E57.")
    P = voxel_downsample(np.vstack(kept), a.voxel); del kept

    # Filtro XY: se il bounding box supera 50 m in qualsiasi direzione,
    # i punti sono in coordinate mondo (Matterport) con pose diverse.
    # Trova il cluster XY più denso usando voxel 5 m e taglia i punti lontani.
    xy_range = P[:, :2].max(0) - P[:, :2].min(0)
    if xy_range.max() > 50.0:
        _vox = 5.0
        kxy = np.floor(P[:, :2] / _vox).astype(np.int64)
        kxy -= kxy.min(0)
        key = kxy[:, 0] * 100000 + kxy[:, 1]
        _, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
        # Voxel più denso = centro dell'edificio
        best_key_idx = np.argmax(cnt)
        best_mask = (inv == best_key_idx)
        cx, cy = P[best_mask, 0].mean(), P[best_mask, 1].mean()
        # Tieni punti entro 30 m dal centro del voxel più denso
        dist = np.sqrt((P[:, 0] - cx)**2 + (P[:, 1] - cy)**2)
        P = P[dist <= 30.0]
        log.info(f"   Filtro XY: cluster centrale ({cx:.1f},{cy:.1f}), raggio 30 m → {len(P):,} punti")

    return P, zf, zc, n_read

def voxel_downsample(P, v):
    if len(P) == 0: return P
    k = np.floor(P / v).astype(np.int64); k -= k.min(0)
    key = (k[:, 0] << 42) | (k[:, 1] << 21) | k[:, 2]        # 21 bit per asse: fino a ~20 km a 1 cm
    _, idx = np.unique(key, return_index=True)
    return P[idx]

def remove_isolated(P, v=0.05, min_pts=3):
    """Elimina punti in voxel quasi vuoti (outlier sparsi)."""
    k = np.floor(P / v).astype(np.int64); k -= k.min(0)
    key = (k[:, 0] << 42) | (k[:, 1] << 21) | k[:, 2]
    _, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
    return P[cnt[inv.ravel()] >= min_pts]

# ----------------------------------------------------------------------------- livelli
def detect_levels(z, bin_=0.05):
    """Pavimento = picco più basso, soffitto = picco più alto dell'istogramma Z.
    Robusto per file Matterport in coordinate mondo assolute:
    - Raggruppa i picchi in cluster (gap > 20 m = cluster separato)
    - Prende il cluster con più punti totali
    - Dentro il cluster cerca il picco più basso (pavimento) e più alto (soffitto)
    """
    hist, edges = np.histogram(z, bins=np.arange(z.min(), z.max() + bin_, bin_))
    thr = hist.mean() + 2 * hist.std()
    peak_idx = np.where(hist > thr)[0]
    if len(peak_idx) < 2:
        raise RuntimeError("Impossibile individuare pavimento/soffitto: usa floor_z/ceil_z manualmente")
    centers = (edges[:-1] + edges[1:]) / 2

    # Raggruppa picchi in cluster (gap > 20 m)
    clusters = []
    cur = [peak_idx[0]]
    for prev, nxt in zip(peak_idx, peak_idx[1:]):
        if centers[nxt] - centers[prev] > 20.0:
            clusters.append(cur); cur = []
        cur.append(nxt)
    clusters.append(cur)

    # Cluster con più punti totali
    best_cluster = max(clusters, key=lambda c: sum(hist[i] for i in c))

    # Pavimento = picco più basso del cluster, soffitto = più alto
    zf_idx = best_cluster[0]
    zc_idx = best_cluster[-1]

    refine = lambda c: float(np.median(z[np.abs(z - c) < 0.10]))
    zf = refine(centers[zf_idx])
    zc = refine(centers[zc_idx])

    if zc - zf < 1.5:
        raise RuntimeError(f"Altezza rilevata {zc-zf:.2f} m troppo bassa: usa floor_z/ceil_z manualmente")
    return zf, zc

# ----------------------------------------------------------------------------- raster
class Grid:
    def __init__(self, xy, res, pad=0.5):
        self.res = res
        self.x0, self.y0 = xy.min(0) - pad
        x1, y1 = xy.max(0) + pad
        self.w = int(math.ceil((x1 - self.x0) / res)); self.h = int(math.ceil((y1 - self.y0) / res))
    def raster(self, xy, min_pts=1):
        ix = ((xy[:, 0] - self.x0) / self.res).astype(int)
        iy = ((xy[:, 1] - self.y0) / self.res).astype(int)
        # clip: dopo rotazione o sezione a quota diversa i punti possono uscire di 1-2 px dalla bbox
        np.clip(ix, 0, self.w - 1, out=ix)
        np.clip(iy, 0, self.h - 1, out=iy)
        img = np.zeros((self.h, self.w), np.int32)
        np.add.at(img, (iy, ix), 1)
        return (img >= min_pts).astype(np.uint8)
    def to_world(self, px):   # px: (N,2) colonna,riga -> x,y metri
        px = np.asarray(px, float)
        return np.c_[self.x0 + (px[:, 0] + 0.5) * self.res, self.y0 + (px[:, 1] + 0.5) * self.res]

def dominant_angle(xy, res=0.03):
    """Angolo dominante dei muri (mod 90°) da Hough sulla sezione."""
    g = Grid(xy, res); img = g.raster(xy) * 255
    lines = cv2.HoughLinesP(img.astype(np.uint8), 1, np.pi / 720, threshold=40,
                            minLineLength=int(0.8 / res), maxLineGap=int(0.1 / res))
    if lines is None: return 0.0
    l = np.asarray(lines).reshape(-1, 4).astype(float)
    ang = np.degrees(np.arctan2(l[:, 3] - l[:, 1], l[:, 2] - l[:, 0])) % 90
    length = np.hypot(l[:, 2] - l[:, 0], l[:, 3] - l[:, 1])
    hist, edges = np.histogram(ang, bins=180, range=(0, 90), weights=length)
    hist = ndimage.gaussian_filter1d(hist, 1.5, mode="wrap")
    k = np.argmax(hist)
    sel = np.abs(((ang - (edges[k] + 0.25)) + 45) % 90 - 45) < 1.0
    return float(np.average(ang[sel], weights=length[sel])) if sel.any() else edges[k]

# ----------------------------------------------------------------------------- vettorializzazione
def snap_poly(poly, tol_deg=8.0):
    """Raddrizza i lati quasi orizzontali/verticali (muri ortogonali)."""
    p = poly.copy(); n = len(p)
    for _ in range(2):
        for i in range(n):
            a, b = p[i], p[(i + 1) % n]
            d = b - a; ang = abs(math.degrees(math.atan2(d[1], d[0]))) % 180
            if min(ang, 180 - ang) < tol_deg:           # quasi orizzontale
                m = (a[1] + b[1]) / 2; p[i][1] = m; p[(i + 1) % n][1] = m
            elif abs(ang - 90) < tol_deg:               # quasi verticale
                m = (a[0] + b[0]) / 2; p[i][0] = m; p[(i + 1) % n][0] = m
    # elimina vertici collineari/duplicati
    out = []
    for i in range(n):
        a, b, c = p[i - 1], p[i], p[(i + 1) % n]
        if np.linalg.norm(b - a) < 1e-4: continue
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(cross) > 1e-6: out.append(b)
    return np.array(out) if len(out) >= 3 else p

def mask_to_polys(mask, grid, eps_m, min_area_m2, snap=True):
    """Contorni (esterni + fori) di una maschera binaria → poligoni in metri."""
    cnts, hier = cv2.findContours(mask.astype(np.uint8), cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    polys = []
    if hier is None: return polys
    for c, hh in zip(cnts, hier[0]):
        area = cv2.contourArea(c) * grid.res ** 2
        if area < min_area_m2: continue
        ap = cv2.approxPolyDP(c, eps_m / grid.res, True)[:, 0, :]
        if len(ap) < 3: continue
        P = grid.to_world(ap)
        # i contorni passano per i CENTRI dei pixel di bordo: riporto il bordo sul filo reale (½ pixel)
        from shapely.geometry import Polygon
        poly = Polygon(P).buffer((-1 if hh[3] != -1 else 1) * grid.res / 2, join_style=2)
        if poly.is_empty or poly.geom_type != "Polygon": continue
        P = np.array(poly.exterior.coords)[:-1]
        if snap: P = snap_poly(P)
        polys.append({"pts": P, "hole": hh[3] != -1, "area": area})
    return polys

# ----------------------------------------------------------------------------- pipeline
def build_plan(P, a, hooks=None):
    hooks = hooks or Hooks()
    info = log.info
    hooks.stage("align")
    P = remove_isolated(P)
    info(f"punti utili (fasce di quota, voxel {a.voxel*100:.0f} cm, senza outlier): {len(P):,}")
    zf, zc = a.floor_z, a.ceil_z
    info(f"pavimento z={zf:.3f}  soffitto z={zc:.3f}  (h netta {zc - zf:.2f} m)")
    z = P[:, 2] - zf; H = zc - zf

    # allineamento: rotazione attorno a Z perché i muri siano paralleli agli assi
    band = (z > a.cut - 0.05) & (z < a.cut + 0.05)
    ang = dominant_angle(P[band, :2]) if not a.no_align else 0.0
    r = -math.radians(ang)
    R = np.array([[math.cos(r), -math.sin(r)], [math.sin(r), math.cos(r)]])
    origin = P[:, :2].mean(0)
    xy = (P[:, :2] - origin) @ R.T
    info(f"rotazione di allineamento: {ang:.2f}°")

    hooks.check(); hooks.stage("walls")
    res = a.res
    g = Grid(xy[(z > 0.1) & (z < H - 0.1)], res)
    s_cut  = (z > a.cut - 0.05) & (z < a.cut + 0.05)
    s_high = (z > H - a.high_band) & (z < H - 0.08)
    s_low  = (z > 0.25) & (z < 0.60)
    cut, high, low = g.raster(xy[s_cut]), g.raster(xy[s_high]), g.raster(xy[s_low])

    k = lambda m: cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (int(m / res) | 1,) * 2)
    close = lambda img, m: cv2.morphologyEx(img, cv2.MORPH_CLOSE, k(m))
    cut, high, low = close(cut, 0.06), close(high, 0.06), close(low, 0.06)

    # Lo scanner vede solo le FACCE dei muri: un muro da 30 cm è una coppia di linee con il vuoto in mezzo.
    # Chiusura con elementi lineari orizzontale/verticale lunghi quanto lo spessore massimo dei muri
    # → riempie lo spessore senza chiudere porte/finestre (più larghe di --max-wall).
    def fill_thickness(img):
        L = int(a.max_wall / res) | 1
        out = img.copy()
        for ker in (np.ones((1, L), np.uint8), np.ones((L, 1), np.uint8)):
            out = cv2.bitwise_or(out, cv2.morphologyEx(img, cv2.MORPH_CLOSE, ker))
        return out
    _high_pixels = int(high.sum())
    _cut_pixels  = int(cut.sum())
    survey_type = getattr(a, "survey_type", "interno")
    log.info(f"   raster px — cut:{_cut_pixels}  high:{_high_pixels}  (H={H:.2f} m, res={res} m/px, tipo={survey_type})")

    if survey_type == "esterno":
        # ESTERNO: usa direttamente fill_thickness(cut) — perimetro edificio senza filtro soffitto
        # I muri perimetrali sono visibili alla sezione orizzontale, non serve il filtro architrave
        log.info("   Modalità ESTERNO: estrazione perimetro da fascia di taglio")
        footprint = fill_thickness(cut)
        footprint = cv2.morphologyEx(close(footprint, 0.20), cv2.MORPH_OPEN, k(0.08))
        walls = footprint.copy()
        walls = cv2.morphologyEx(close(walls, 0.10), cv2.MORPH_OPEN, k(0.05))
    else:
        # INTERNO / MULTIPIANO: metodo standard con filtro soffitto
        # ingombro muri: fascia sotto il soffitto (contiene anche gli architravi → porte e finestre "chiuse")
        footprint = fill_thickness(high)
        footprint = cv2.morphologyEx(footprint, cv2.MORPH_OPEN, k(0.05))
        # Fallback: se la fascia alta è vuota/scarsa usa la fascia di taglio come footprint
        if _high_pixels < max(100, _cut_pixels // 20):
            log.warning(
                f"   Fascia alta scarsa ({_high_pixels} px vs cut {_cut_pixels} px) — "
                f"uso fascia di taglio come footprint (soffitto non scansionato o H={H:.2f} m anomala)"
            )
            footprint = fill_thickness(cut)
            footprint = cv2.morphologyEx(footprint, cv2.MORPH_OPEN, k(0.05))
        # muri tagliati dal piano di sezione: pieni alla quota di taglio E presenti fino al soffitto
        # (armadi, mobili, tavoli non arrivano al soffitto → spariscono)
        walls = cv2.bitwise_and(fill_thickness(cut), cv2.dilate(footprint, k(0.04)))
        walls = cv2.morphologyEx(close(walls, 0.10), cv2.MORPH_OPEN, k(0.05))   # chiude micro-fessure, toglie briciole
    footprint = close(footprint, 0.10)
    low_f = fill_thickness(low)
    # compensazione: il rumore del sensore + la discretizzazione "ingrassano" ogni faccia di ~1 pixel
    shrink = np.ones((3, 3), np.uint8)
    walls, footprint = cv2.erode(walls, shrink), cv2.erode(footprint, shrink)

    if getattr(a,"debug",False):
        import cv2 as _c
        for nm,im in [("cut",cut),("high",high),("low",low),("walls",walls),("footprint",footprint)]: _c.imwrite(f"dbg_{nm}.png", (255-im*255)[::-1])
    # aperture
    hooks.check(); hooks.stage("openings")
    openings = cv2.bitwise_and(footprint, 1 - cv2.dilate(walls, k(0.04)))
    openings = cv2.morphologyEx(openings, cv2.MORPH_OPEN, k(0.05))
    lab, n = ndimage.label(openings)
    ops = []
    for i in range(1, n + 1):
        m = (lab == i).astype(np.uint8)
        cnt, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        (cx, cy), (w, h), rot = cv2.minAreaRect(cnt[0])
        width, depth = max(w, h) * res, min(w, h) * res
        if width < 0.5 or width > 3.5: continue           # rumore o vetrate fuori scala
        # misura luce netta: dal centro scorro lungo l'asse del muro fino alle spallette
        ccol, crow = int(round(cx)), int(round(cy))
        ux_, uy_ = (1, 0) if (w >= h) == (abs(rot) <= 45) else (0, 1)
        half = int(0.04 / res)  # banda trasversale
        def run(sign):
            for st in range(1, int(4.0 / res)):
                c_, r_ = ccol + sign * st * ux_, crow + sign * st * uy_
                if not (0 <= r_ < walls.shape[0] and 0 <= c_ < walls.shape[1]): return None
                band_ = walls[max(r_ - half * uy_ ** 0 * (1 - uy_) , 0): r_ + 1 + half * (1 - uy_), max(c_ - half * (1 - ux_), 0): c_ + 1 + half * (1 - ux_)]
                if band_.any(): return st
            return None
        l_, r2 = run(-1), run(+1)
        if l_ and r2: width = (l_ + r2 - 1) * res
        sill = (cv2.bitwise_and(m, cv2.dilate(low_f, k(0.04))).sum() / max(m.sum(), 1)) > 0.4
        horiz = w >= h
        if abs(rot) > 45: horiz = not horiz
        c = g.to_world([[cx, cy]])[0]
        ops.append({"type": "FINESTRA" if sill else "PORTA", "c": c, "width": width,
                    "depth": depth, "horiz": horiz})
    info(f"aperture: {sum(o['type']=='PORTA' for o in ops)} porte, {sum(o['type']=='FINESTRA' for o in ops)} finestre")

    # vani: spazio libero chiuso, che non tocca il bordo
    hooks.check(); hooks.stage("rooms")
    free = (1 - footprint).astype(np.uint8)
    lab, n = ndimage.label(free)
    border = set(np.unique(np.r_[lab[0], lab[-1], lab[:, 0], lab[:, -1]]))
    rooms = []
    for i in range(1, n + 1):
        if i in border: continue
        m = (lab == i).astype(np.uint8)
        area = m.sum() * res ** 2
        if area < a.min_room: continue
        polys = [p for p in mask_to_polys(m, g, a.eps, a.min_room) if not p["hole"]]
        if polys:
            rooms.append({"pts": polys[0]["pts"], "area": area})
    rooms.sort(key=lambda r: (-r["pts"][:, 1].mean(), r["pts"][:, 0].mean()))
    info(f"vani: {len(rooms)}  superficie utile totale: {sum(r['area'] for r in rooms):.2f} m²")

    wall_polys = mask_to_polys(walls, g, a.eps, 0.02)
    log.info(f"   wall_polys={len(wall_polys)}  walls_px={int(walls.sum())}")
    if not wall_polys:
        # Fallback finale: usa fill_thickness(cut) diretto senza filtro footprint
        log.warning("   wall_polys vuota — fallback: cut grezzo senza filtro soffitto")
        walls_fallback = fill_thickness(cut)
        walls_fallback = cv2.morphologyEx(close(walls_fallback, 0.10), cv2.MORPH_OPEN, k(0.05))
        walls_fallback = cv2.erode(walls_fallback, shrink)
        wall_polys = mask_to_polys(walls_fallback, g, a.eps, 0.02)
        log.info(f"   fallback wall_polys={len(wall_polys)}  walls_px={int(walls_fallback.sum())}")
        if wall_polys:
            walls = walls_fallback
        else:
            raise ValueError("Nessun muro rilevato nella nuvola di punti — il piano di taglio potrebbe non intersecare strutture.")
    # trasla tutto con origine nell'angolo in basso a sinistra dei muri
    allp = np.vstack([p["pts"] for p in wall_polys])
    o = allp.min(0)
    for p in wall_polys: p["pts"] = p["pts"] - o
    for r_ in rooms: r_["pts"] = r_["pts"] - o
    for op in ops: op["c"] = op["c"] - o
    sec = xy[s_cut] - o
    meta = {"rotation_deg": ang, "floor_z": zf, "ceil_z": zc, "cut": a.cut,
            "origin_world": (origin + (o @ R)).tolist()}  # punto (0,0) della pianta in coordinate originali (approx.)
    return wall_polys, ops, rooms, sec, meta

# ----------------------------------------------------------------------------- DXF
def write_dxf(path, walls, ops, rooms, sec, meta, a):
    import ezdxf
    from ezdxf.enums import TextEntityAlignment
    doc = ezdxf.new("R2018", setup=True); doc.units = ezdxf.units.M
    for name, col in [("MURI", 7), ("MURI_CAMPITURA", 8), ("FINESTRE", 4), ("PORTE", 3),
                      ("VANI", 6), ("TESTI", 7), ("QUOTE", 1), ("NUVOLA_SEZIONE", 9)]:
        doc.layers.add(name, color=col)
    doc.layers.get("NUVOLA_SEZIONE").off()        # accendila per verificare/ricalcare
    msp = doc.modelspace()

    # muri: contorni + campitura (esterni + fori dei muri)
    outer = [p for p in walls if not p["hole"]]; holes = [p for p in walls if p["hole"]]
    for p in walls:
        msp.add_lwpolyline(p["pts"].tolist(), close=True, dxfattribs={"layer": "MURI", "lineweight": 50})
    hatch = msp.add_hatch(color=8, dxfattribs={"layer": "MURI_CAMPITURA"})
    hatch.set_pattern_fill("ANSI31", scale=0.02)
    for p in outer: hatch.paths.add_polyline_path(p["pts"].tolist(), is_closed=True, flags=1)
    for p in holes: hatch.paths.add_polyline_path(p["pts"].tolist(), is_closed=True, flags=0)

    # aperture
    for op in ops:
        cx, cy = op["c"]; w, d = op["width"], max(op["depth"], 0.1)
        ux, uy = (1, 0) if op["horiz"] else (0, 1)   # direzione del muro
        vx, vy = -uy, ux                              # normale
        def P(s, t): return (cx + ux * s + vx * t, cy + uy * s + vy * t)
        if op["type"] == "FINESTRA":
            for t in (-d / 2, -d / 6, d / 6, d / 2):
                msp.add_line(P(-w / 2, t), P(w / 2, t), dxfattribs={"layer": "FINESTRE"})
            for s in (-w / 2, w / 2):
                msp.add_line(P(s, -d / 2), P(s, d / 2), dxfattribs={"layer": "FINESTRE"})
        else:
            for s in (-w / 2, w / 2):
                msp.add_line(P(s, -d / 2), P(s, d / 2), dxfattribs={"layer": "PORTE"})
            # anta a 90° + arco di apertura (verso da verificare sul rilievo)
            hinge = P(-w / 2, d / 2); tip = P(-w / 2, d / 2 + w)
            msp.add_line(hinge, tip, dxfattribs={"layer": "PORTE"})
            base = math.degrees(math.atan2(vy, vx))
            msp.add_arc(hinge, w, base - 90 if True else base, base, dxfattribs={"layer": "PORTE"})
        msp.add_text(f"{'F' if op['type']=='FINESTRA' else 'P'} {w*100:.0f}", height=0.12,
                     dxfattribs={"layer": "TESTI"}).set_placement(P(0, -d / 2 - 0.25), align=TextEntityAlignment.MIDDLE_CENTER)

    # vani
    for i, r in enumerate(rooms, 1):
        msp.add_lwpolyline(r["pts"].tolist(), close=True, dxfattribs={"layer": "VANI"})
        c = r["pts"].mean(0)
        msp.add_text(f"VANO {i}", height=0.20, dxfattribs={"layer": "TESTI"}).set_placement(
            (c[0], c[1] + 0.15), align=TextEntityAlignment.MIDDLE_CENTER)
        msp.add_text(f"{r['area']:.2f} m²", height=0.16, dxfattribs={"layer": "TESTI"}).set_placement(
            (c[0], c[1] - 0.15), align=TextEntityAlignment.MIDDLE_CENTER)

    # quote d'ingombro esterne
    allp = np.vstack([p["pts"] for p in outer]); x0, y0 = allp.min(0); x1, y1 = allp.max(0)
    st = "EZDXF"
    big = {"dimtxt": 0.18, "dimasz": 0.12, "dimdec": 2, "dimlfac": 1.0}
    small = {"dimtxt": 0.12, "dimasz": 0.08, "dimdec": 2, "dimlfac": 1.0, "dimexe": 0.05, "dimexo": 0.0}
    msp.add_linear_dim(base=(0, y0 - 0.8), p1=(x0, y0), p2=(x1, y0), dimstyle=st,
                       override=big, dxfattribs={"layer": "QUOTE"}).render()
    msp.add_linear_dim(base=(x0 - 0.8, 0), p1=(x0, y0), p2=(x0, y1), angle=90, dimstyle=st,
                       override=big, dxfattribs={"layer": "QUOTE"}).render()
    # quote dei vani (larghezza x profondità del rettangolo d'ingombro)
    for r in rooms:   # luci nette interne del vano (rettangolo d'ingombro)
        rx0, ry0 = r["pts"].min(0); rx1, ry1 = r["pts"].max(0)
        yb, xb = ry0 + 0.45, rx0 + 0.45
        msp.add_linear_dim(base=(0, yb), p1=(rx0, yb), p2=(rx1, yb), dimstyle=st, override=small,
                           dxfattribs={"layer": "QUOTE"}).render()
        msp.add_linear_dim(base=(xb, 0), p1=(xb, ry0), p2=(xb, ry1), angle=90, dimstyle=st, override=small,
                           dxfattribs={"layer": "QUOTE"}).render()

    # punti della sezione (layer spento) — come il DXF che usi oggi per ricalcare
    step = max(1, len(sec) // 60000)
    for x, y in (sec[::step] if getattr(a, "section_points", True) else []):
        msp.add_point((x, y), dxfattribs={"layer": "NUVOLA_SEZIONE"})

    doc.header["$INSUNITS"] = 6
    doc.saveas(path)

# ----------------------------------------------------------------------------- PDF / PNG
def write_pdf(path_pdf, path_png, walls, ops, rooms, meta, a, path_preview=None):
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import PathPatch, Arc
    from matplotlib.path import Path as MPath
    allp = np.vstack([p["pts"] for p in walls]); x0, y0 = allp.min(0); x1, y1 = allp.max(0)
    S = a.scale
    m = 1.5                                                      # margine disegno (m)
    dw, dh = (x1 - x0 + 2 * m) * 1000 / S, (y1 - y0 + 2 * m) * 1000 / S
    # formato carta: il più piccolo (orizzontale) in cui il disegno entra in scala
    papers = [("A3", 420, 297), ("A2", 594, 420), ("A1", 841, 594), ("A0", 1189, 841)]
    paper, W_mm, H_mm = papers[-1]
    for name, pw, ph in papers:
        if dw <= pw - 20 and dh <= ph - 60:
            paper, W_mm, H_mm = name, pw, ph
            break
    else:
        log.warning(f"Il disegno non entra in A0 a 1:{S}: scegli una scala più piccola")
    meta["paper"] = paper
    fig = plt.figure(figsize=(W_mm / 25.4, H_mm / 25.4))
    left = (W_mm - dw) / 2 / W_mm; bottom = (H_mm - dh + 30) / 2 / H_mm
    ax = fig.add_axes([left, bottom, dw / W_mm, dh / H_mm])
    ax.set_xlim(x0 - m, x1 + m); ax.set_ylim(y0 - m, y1 + m); ax.set_aspect("equal"); ax.axis("off")

    verts, codes = [], []
    for p in walls:
        pts = p["pts"]; verts += pts.tolist() + [pts[0].tolist()]
        codes += [MPath.MOVETO] + [MPath.LINETO] * (len(pts) - 1) + [MPath.CLOSEPOLY]
    ax.add_patch(PathPatch(MPath(verts, codes), facecolor="#2E2E2E", edgecolor="#111111", lw=0.8))

    for op in ops:
        cx, cy = op["c"]; w, d = op["width"], max(op["depth"], 0.1)
        ux, uy = (1, 0) if op["horiz"] else (0, 1); vx, vy = -uy, ux
        P = lambda s, t: (cx + ux * s + vx * t, cy + uy * s + vy * t)
        if op["type"] == "FINESTRA":
            for t in (-d / 2, -d / 6, d / 6, d / 2):
                (xa, ya), (xb, yb) = P(-w / 2, t), P(w / 2, t); ax.plot([xa, xb], [ya, yb], c="#1F4FA3", lw=0.6)
        else:
            hinge = P(-w / 2, d / 2); tip = P(-w / 2, d / 2 + w)
            ax.plot([hinge[0], tip[0]], [hinge[1], tip[1]], c="#2E2E2E", lw=0.7)
            base = math.degrees(math.atan2(vy, vx))
            ax.add_patch(Arc(hinge, 2 * w, 2 * w, theta1=base - 90, theta2=base, color="#5C5C5C", lw=0.5, ls="--"))
        lp = P(0, -d / 2 - 0.25)
        ax.text(*lp, f"{'F' if op['type']=='FINESTRA' else 'P'} {w*100:.0f}", fontsize=5, ha="center", va="center", color="#555")

    for i, r in enumerate(rooms, 1):
        c = r["pts"].mean(0)
        ax.text(c[0], c[1] + 0.12, f"VANO {i}", fontsize=7, ha="center", weight="bold")
        ax.text(c[0], c[1] - 0.18, f"{r['area']:.2f} m²", fontsize=6.5, ha="center")
    # quote esterne
    for (pa, pb, off, rot) in [((x0, y0 - 0.8), (x1, y0 - 0.8), (0, -0.25), 0), ((x0 - 0.8, y0), (x0 - 0.8, y1), (-0.25, 0), 90)]:
        ax.annotate("", pa, pb, arrowprops=dict(arrowstyle="<->", lw=0.5))
        mid = ((pa[0] + pb[0]) / 2 + off[0], (pa[1] + pb[1]) / 2 + off[1])
        L = math.dist(pa, pb); ax.text(*mid, f"{L:.2f}", fontsize=6.5, ha="center", va="center", rotation=rot)
    # barra di scala 0-5 m
    bx, by = x0, y0 - 1.3
    for i in range(5):
        ax.add_patch(plt.Rectangle((bx + i, by), 1, 0.08, fc="black" if i % 2 == 0 else "white", ec="black", lw=0.4))
    for i in range(6):
        ax.text(bx + i, by - 0.2, f"{i}" + (" m" if i == 5 else ""), fontsize=5, ha="center")
    ax.annotate("N*", (x1 + 0.8, y1 + 0.2), (x1 + 0.8, y1 - 0.6), ha="center", fontsize=7,
                arrowprops=dict(arrowstyle="-|>", lw=0.8))

    # cartiglio
    tb = fig.add_axes([(W_mm - 190) / W_mm, 8 / H_mm, 180 / W_mm, 24 / H_mm]); tb.set_xticks([]); tb.set_yticks([])
    tb.text(0.02, 0.72, a.title, fontsize=10, weight="bold", transform=tb.transAxes)
    tb.text(0.02, 0.40, f"Pianta da nuvola di punti — piano di sezione h = {meta['cut']:.2f} m  —  scala 1:{S}  —  {paper}", fontsize=7, transform=tb.transAxes)
    tb.text(0.02, 0.12, f"Superficie utile vani: {sum(r['area'] for r in rooms):.2f} m²   ·   h netta {meta['ceil_z']-meta['floor_z']:.2f} m   ·   "
            f"rot. {meta['rotation_deg']:.2f}°   ·   *N di griglia", fontsize=6, transform=tb.transAxes)
    fig.savefig(path_pdf); fig.savefig(path_png, dpi=200)
    if path_preview:
        bb = ax.get_tightbbox(fig.canvas.get_renderer()).transformed(fig.dpi_scale_trans.inverted())
        fig.savefig(path_preview, dpi=220, bbox_inches=bb.expanded(1.03, 1.03))
    plt.close(fig)
    return meta

# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input"); ap.add_argument("--out", default=None)
    ap.add_argument("--cut", type=float, default=1.10, help="quota del piano di sezione dal pavimento (m)")
    ap.add_argument("--res", type=float, default=0.02, help="risoluzione raster (m)")
    ap.add_argument("--voxel", type=float, default=0.01, help="sottocampionamento (m)")
    ap.add_argument("--high-band", type=float, default=0.35, help="fascia sotto soffitto usata per muri/architravi (m)")
    ap.add_argument("--max-wall", type=float, default=0.55, help="spessore massimo dei muri da riempire (m)")
    ap.add_argument("--eps", type=float, default=0.02, help="tolleranza semplificazione linee (m)")
    ap.add_argument("--min-room", type=float, default=1.0, help="superficie minima di un vano (m²)")
    ap.add_argument("--floor-z", type=float); ap.add_argument("--ceil-z", type=float)
    ap.add_argument("--debug", action="store_true", help="salva le maschere intermedie dbg_*.png")
    ap.add_argument("--no-align", action="store_true", help="non ruotare la nuvola")
    ap.add_argument("--scale", type=int, default=100); ap.add_argument("--title", default="RILIEVO — PIANTA")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="  · %(message)s")
    out = a.out or os.path.splitext(a.input)[0] + "_pianta"
    print(f"▶ {a.input}")
    P, a.floor_z, a.ceil_z, n = load_bands_streaming(a.input, a)
    print(f"  · punti nel file: {n:,}  →  tenuti in memoria: {len(P):,} ({100*len(P)/max(n,1):.1f}%)")
    walls, ops, rooms, sec, meta = build_plan(P, a)
    write_dxf(out + ".dxf", walls, ops, rooms, sec, meta, a)
    write_pdf(out + ".pdf", out + ".png", walls, ops, rooms, meta, a)
    print(f"✔ {out}.dxf  {out}.pdf  {out}.png")

if __name__ == "__main__":
    main()

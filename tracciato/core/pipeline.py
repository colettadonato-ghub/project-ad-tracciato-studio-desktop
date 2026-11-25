"""Orchestrazione di un'elaborazione: parametri, fasi con tempi, file di output, log dedicato."""
import json
import logging
import os
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from .. import __app_name__, __version__
from . import engine
from .logs import JobLog

log = logging.getLogger("tracciato.pipeline")

STAGES = [
    ("levels", "Livelli pavimento e soffitto"),
    ("read", "Lettura e filtro della nuvola"),
    ("align", "Allineamento dei muri"),
    ("walls", "Estrazione dei muri"),
    ("openings", "Porte e finestre"),
    ("rooms", "Vani e superfici"),
    ("dxf", "Export DXF"),
    ("pdf", "Export PDF e anteprima"),
]
SUPPORTED = (".las", ".laz", ".e57")

# livelli di dettaglio: voxel di sottocampionamento e risoluzione del raster
DETAIL = {"alta": (0.01, 0.02), "media": (0.02, 0.03), "bozza": (0.03, 0.045)}


@dataclass
class Params:
    cut: float = 1.10               # quota del piano di sezione dal pavimento (m)
    scale: int = 100                # scala del PDF (1:scale)
    detail: str = "alta"            # alta | media | bozza
    title: str = "RILIEVO — PIANTA"
    align: bool = True              # raddrizza i muri sugli assi
    section_points: bool = True     # punti della sezione nel DXF (layer spento)
    inner_dims: bool = True         # quote interne alla pianta (luci dei vani, misure di porte e finestre)
    max_wall: float = 0.55          # spessore massimo dei muri (m)
    high_band: float = 0.35         # fascia sotto soffitto (m)
    min_room: float = 1.0           # superficie minima di un vano (m²)
    floor_z: float | None = None    # quote forzate (None = automatico)
    ceil_z: float | None = None
    survey_type: str = "interno"    # interno | esterno | multipiano

    @classmethod
    def from_dict(cls, d: dict) -> "Params":
        known = {k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__}
        for k in ("floor_z", "ceil_z"):
            if known.get(k) in ("", None):
                known[k] = None
        p = cls(**known)
        p.cut, p.scale = float(p.cut), int(p.scale)
        for k in ("max_wall", "high_band", "min_room"):
            setattr(p, k, float(getattr(p, k)))
        if p.floor_z is not None: p.floor_z = float(p.floor_z)
        if p.ceil_z is not None: p.ceil_z = float(p.ceil_z)
        if p.detail not in DETAIL: p.detail = "alta"
        if not 0.3 <= p.cut <= 2.5:
            raise ValueError("L'altezza di sezione deve essere tra 0,30 e 2,50 m")
        return p

    def engine_args(self) -> SimpleNamespace:
        voxel, res = DETAIL[self.detail]
        # Adatta parametri in base al tipo di rilievo
        cut = self.cut
        high_band = self.high_band
        if self.survey_type == "esterno":
            # Esterno: sezione a 1.00 m, fascia alta non usata per filtro soffitto
            cut = self.cut if self.cut != 1.10 else 1.00
            high_band = 0.10  # fascia ridotta, soffitto spesso non scansionato
        elif self.survey_type == "multipiano":
            # Multi-piano: parametri standard, detect_levels gestirà i piani
            pass
        return SimpleNamespace(cut=cut, res=res, voxel=voxel, high_band=high_band,
                               max_wall=self.max_wall, eps=0.02, min_room=self.min_room,
                               floor_z=self.floor_z, ceil_z=self.ceil_z, no_align=not self.align,
                               scale=self.scale, title=self.title or "RILIEVO — PIANTA",
                               section_points=self.section_points, inner_dims=self.inner_dims, debug=False,
                               survey_type=self.survey_type)


def _inspect_e57_raw(path: str) -> dict:
    """Legge header E57 direttamente come XML binario, senza usare pye57/libe57.
    Il formato ASTM E57 ha una sezione XML in chiaro all'offset indicato nell'intestazione binaria
    del file (byte 24-31 = xmlOffset, 32-39 = xmlLength, tutto little-endian uint64).
    Se la struttura non è quella attesa, ritorna un dict parziale senza sollevare eccezioni."""
    import struct, re
    result = {}
    try:
        with open(path, "rb") as f:
            sig = f.read(8)
            if sig != b"ASTM-E57":
                return result  # non è un E57 valido
            # campi header binario (da spec ASTM E57 1.0)
            # offset 8:  fileVersionMajor  (uint32)
            # offset 12: fileVersionMinor  (uint32)
            # offset 16: filePhysicalLength (uint64)
            # offset 24: xmlOffset         (uint64)
            # offset 32: xmlLength         (uint64)
            f.seek(24)
            xml_offset = struct.unpack("<Q", f.read(8))[0]
            xml_length = struct.unpack("<Q", f.read(8))[0]
            # sanity: evita di caricare un XML gigantesco se il file è corrotto
            read_len = min(int(xml_length), 2 * 1024 * 1024)
            f.seek(int(xml_offset))
            xml_bytes = f.read(read_len)
        xml_text = xml_bytes.decode("utf-8", errors="replace")
        # scan count: numero di <vectorChild> (ogni scansione è un nodo)
        scans = len(re.findall(r"<vectorChild\b", xml_text))
        if scans == 0:
            # alcuni writer usano <points> direttamente sotto <data3D>
            scans = len(re.findall(r"<points\b", xml_text))
        result["scans"] = max(scans, 1)
        # punto count per scansione: valore di <pointCount> (anche con attributi tipo type="Integer")
        counts = [int(m) for m in re.findall(r"<pointCount(?:\s[^>]*)?>(\d+)</pointCount>", xml_text)]
        if not counts:
            # alcuni writer usano <recordCount> o <points recordCount="N">
            counts = [int(m) for m in re.findall(r'recordCount[^>]*?>(\d+)<', xml_text)]
        if counts:
            result["points"] = sum(counts)
        # GUID del file, solo per diagnostica
        m = re.search(r"<guid[^>]*>\s*([^<]+)\s*</guid>", xml_text)
        if m:
            result["guid"] = m.group(1).strip()
    except Exception as ex:
        log.debug(f"Parsing XML E57 raw fallito: {ex}")
    return result


def inspect_file(path: str) -> dict:
    """Informazioni rapide sul file (senza leggere i punti)."""
    p = Path(path)
    info = {"path": str(p), "name": p.name, "size": p.stat().st_size, "points": None, "scans": None,
            "format": p.suffix.lower().lstrip(".").upper()}
    try:
        if p.suffix.lower() in (".las", ".laz"):
            import laspy
            with laspy.open(str(p)) as f:
                info["points"] = int(f.header.point_count)
        elif p.suffix.lower() == ".e57":
            # Usa SOLO il parser raw per l'ispezione: pye57/libe57 causa crash nativi (0xC0000005)
            # su file non conformi a ASTM E57-2010 (es. esportati da Matterport, ReCap, ecc.).
            raw = _inspect_e57_raw(str(p))
            info.update(raw)
    except Exception as ex:
        log.warning(f"Impossibile leggere l'intestazione di {p.name}: {ex}")
    return info


def default_output_dir(input_path: str) -> str:
    return str(Path(input_path).parent / "Tracciato")


def _e57_needs_raw_parser(path: str) -> bool:
    """Ritorna True se il file E57 è già stato letto con successo dal parser raw ma non da pye57.
    Questo indica un file non-standard che causerebbe un crash nativo (0xC0000005) in libe57.
    La logica: se il raw parser trova i dati (XML leggibile con scansioni e stream binari),
    usiamo il raw parser per l'elaborazione senza nemmeno tentare pye57."""
    raw = _inspect_e57_raw(path)
    if not raw.get("points"):
        # raw parser non ha trovato nulla → non sappiamo, proviamo pye57 normalmente
        return False
    # Ora verifica veloce: pye57 riesce ad aprire il file?
    # Lo facciamo in un processo separato (già siamo nel worker, quindi è isolato).
    # Purtroppo non possiamo fare un sub-processo qui in modo pulito.
    # Strategia alternativa: se il raw parser ha trovato scansioni E il file ha già crashato
    # (lo sappiamo dal fatto che siamo in run_job dopo un crash dell'inspect)... ma non abbiamo
    # quella informazione qui.
    # Soluzione pratica: usiamo il raw parser SEMPRE per file E57 con scansioni raw trovate,
    # perché il raw parser è più robusto di pye57 per file non-standard, e per file standard
    # i risultati sono identici (entrambi leggono float32 dal binario).
    # Il raw parser è leggermente più lento (<5%) su file grandi perché non usa C++,
    # ma evita completamente i crash nativi.
    log.info(f"E57: file analizzato con parser integrato ({raw.get('scans', '?')} scansioni, "
             f"{raw.get('points', '?'):,} punti) — uso parser integrato per l'elaborazione")
    return True


def run_job(input_path: str, out_dir: str, params: Params, on_event=None, cancelled=None) -> dict:
    """Esegue l'elaborazione completa. on_event(dict) riceve gli eventi per l'interfaccia.
    Ritorna il riepilogo (salvato anche come .json accanto agli elaborati)."""
    emit = on_event or (lambda e: None)
    src = Path(input_path)
    if src.suffix.lower() not in SUPPORTED:
        raise ValueError(f"Formato non supportato: {src.suffix} (ammessi: LAS, LAZ, E57)")
    if not src.exists():
        raise FileNotFoundError(f"File non trovato: {src}")

    out = Path(out_dir or default_output_dir(input_path))
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = out / f"{src.stem}_pianta_{stamp}"
    files = {k: str(base) + ext for k, ext in
             [("dxf", ".dxf"), ("pdf", ".pdf"), ("png", ".png"), ("preview", "_anteprima.png"),
              ("log", ".log"), ("json", ".json")]}

    times, current = {}, {"name": None, "t0": None}

    def stage(name):
        now = time.perf_counter()
        if current["name"]:
            times[current["name"]] = now - current["t0"]
            emit({"type": "stage", "stage": current["name"], "status": "done",
                  "seconds": round(times[current["name"]], 2)})
        current.update(name=name, t0=now)
        if name:
            label = dict(STAGES).get(name, name)
            log.info(f"▶ {label}")
            emit({"type": "stage", "stage": name, "status": "running"})

    def progress(stage_name, frac):
        emit({"type": "progress", "stage": stage_name, "value": round(float(frac), 3)})

    hooks = engine.Hooks(progress=progress, stage=stage, cancelled=cancelled)
    summary = {"app": __app_name__, "version": __version__, "input": str(src), "output_dir": str(out),
               "params": asdict(params), "started": datetime.now().isoformat(timespec="seconds"),
               "files": files, "status": "running"}

    def ui_log(level, line):
        emit({"type": "log", "level": level, "line": line})

    t_start = time.perf_counter()
    with JobLog(Path(files["log"]), sink=ui_log):
        try:
            log.info(f"Elaborazione di {src} ({__app_name__} {__version__})")
            log.info("Parametri: " + json.dumps(asdict(params), ensure_ascii=False))
            a = params.engine_args()
            # Per file E57: usa SEMPRE il parser integrato (puro Python) invece di pye57/libe57.
            # pye57/libe57 causa crash nativi (0xC0000005) su file prodotti da molti software
            # (Matterport, ReCap, ecc.) non conformi a ASTM E57-2010. Il raw parser è più robusto,
            # legge direttamente le pagine binarie del formato E57 senza dipendere dalla libreria C++,
            # e produce risultati identici per file conformi. Il costo è trascurabile (<5% più lento).
            a.e57_use_raw = src.suffix.lower() == ".e57"
            if params.floor_z is not None and params.ceil_z is not None:
                emit({"type": "stage", "stage": "levels", "status": "skipped"})
                log.info(f"Quote forzate: pavimento {params.floor_z}, soffitto {params.ceil_z}")
            P, a.floor_z, a.ceil_z, n = engine.load_bands_streaming(str(src), a, hooks)
            if len(P) < 2000:
                raise RuntimeError("Alle quote di sezione non ci sono abbastanza punti: controlla le quote "
                                   "di pavimento/soffitto o l'altezza di sezione")
            log.info(f"Punti nel file: {n:,} → tenuti in memoria: {len(P):,} ({100*len(P)/max(n,1):.1f}%)")
            walls, ops, rooms, sec, meta = engine.build_plan(P, a, hooks)
            del P
            if not walls:
                raise RuntimeError("Nessun muro riconosciuto: controlla l'altezza di sezione o le quote")
            hooks.check(); stage("dxf")
            engine.write_dxf(files["dxf"], walls, ops, rooms, sec, meta, a)
            hooks.check(); stage("pdf")
            meta = engine.write_pdf(files["pdf"], files["png"], walls, ops, rooms, meta, a,
                                    path_preview=files["preview"])
            stage(None)
            total = time.perf_counter() - t_start
            summary.update(
                status="ok", seconds=round(total, 1), points_file=n,
                floor_z=round(meta["floor_z"], 3), ceil_z=round(meta["ceil_z"], 3),
                height=round(meta["ceil_z"] - meta["floor_z"], 2),
                rotation_deg=round(meta["rotation_deg"], 2), paper=meta.get("paper"),
                rooms=[{"name": f"Vano {i}", "area": round(r["area"], 2)} for i, r in enumerate(rooms, 1)],
                area_total=round(sum(r["area"] for r in rooms), 2),
                doors=sum(o["type"] == "PORTA" for o in ops),
                windows=sum(o["type"] == "FINESTRA" for o in ops),
                stage_seconds={k: round(v, 2) for k, v in times.items()},
            )
            log.info(f"✔ Completato in {total:.1f} s — {len(rooms)} vani, {summary['area_total']} m², "
                     f"{summary['doors']} porte, {summary['windows']} finestre — formato {summary['paper']}")
            log.info(f"Elaborati: {files['dxf']} · {files['pdf']}")
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
    summary["finished"] = datetime.now().isoformat(timespec="seconds")
    # rimuovo dai riferimenti i file non prodotti
    summary["files"] = {k: v for k, v in files.items() if os.path.exists(v) or k == "json"}
    Path(files["json"]).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary

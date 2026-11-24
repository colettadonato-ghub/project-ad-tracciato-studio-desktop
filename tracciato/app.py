"""Finestra dell'app: interfaccia HTML (cartella ui/) servita da pywebview, logica in Python.

Su Windows pywebview usa il motore Edge WebView2, già presente in Windows 10/11.
Tutte le chiamate dall'interfaccia passano da `Api`; gli eventi delle elaborazioni arrivano
all'interfaccia tramite window.tracciato.onEvent(...).

Flusso (dalla 0.2):
  1. scelta della nuvola → anteprima (campione per il viewer 3D, quote, tipo proposto)
  2. tipo di rilievo: interno appartamento/ufficio · esterno edificio · interno multi-piano
  3. parametri del tipo (piano 0, quote di sezione, linee di sezione/prospetto con spessore)
  4. elaborazione in un processo separato → tavole PNG (+ DXF/PDF per la pianta d'interno)
"""
import base64
import json
import logging
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from . import __app_name__, __version__
from .core import pipeline, preview as preview_mod, scene, store, worker
from .core.logs import (begin_session, data_dir, end_session, gb, log_dir, memory_info, setup_logging,
                        system_summary)

log = logging.getLogger("tracciato.app")


def resource_dir() -> Path:
    """Cartella delle risorse, sia da sorgente sia dentro l'eseguibile PyInstaller."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "tracciato"
    return Path(__file__).parent


def open_in_os(path: str):
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - apertura con l'applicazione predefinita (AutoCAD, lettore PDF…)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


class Api:
    def __init__(self):
        self._window = None
        self._job = None
        self._cancel_fn = None
        self._lock = threading.Lock()
        self._previous_crash = None
        self._preview = None

    # ------------------------------------------------------------------ stato iniziale
    def get_state(self):
        settings = store.load_settings()
        defaults = pipeline.Params().__dict__ | settings.get("params", {})
        return {
            "app": __app_name__, "version": __version__,
            "params": defaults,
            "scene_params": settings.get("scene_params", {}),
            "last_mode": settings.get("last_mode", ""),
            "output_dir": settings.get("output_dir", ""),
            "last_input": settings.get("last_input", ""),
            "history": store.load_history()[:30],
            "log_dir": str(log_dir()), "data_dir": str(data_dir()),
            "stages": pipeline.STAGES,
            "modes": scene.MODES,
            "busy": self._busy(),
            "previous_crash": self._previous_crash,
        }

    def _busy(self):
        return self._job is not None and self._job.is_alive()

    # ------------------------------------------------------------------ file
    def pick_input(self):
        import webview
        start = os.path.dirname(store.load_settings().get("last_input", "")) or str(Path.home())
        res = self._window.create_file_dialog(
            webview.FileDialog.OPEN, directory=start, allow_multiple=False,
            file_types=("Nuvole di punti (*.las;*.laz;*.e57)", "Tutti i file (*.*)"))
        if not res:
            return None
        path = res[0] if isinstance(res, (list, tuple)) else res
        log.info(f"File scelto: {path}")
        return {"path": path}

    def inspect(self, path):
        if not path or not os.path.isfile(path):
            return {"error": "File non trovato"}
        if Path(path).suffix.lower() not in pipeline.SUPPORTED:
            return {"error": "Formato non supportato: usa LAS, LAZ o E57"}
        size = os.path.getsize(path)
        log.info(f"Lettura intestazione: {path} ({size / 1e6:.1f} MB)")
        if Path(path).suffix.lower() == ".e57":
            info = worker.inspect_isolated(path)      # libreria nativa: processo separato
        else:
            info = pipeline.inspect_file(path)
        if info.get("error"):
            log.error(info["error"])
            return info
        info["default_output"] = pipeline.default_output_dir(path)
        log.info(f"Nuvola selezionata: {path} ({info['size']/1e6:.1f} MB, punti: {info['points']})")
        return info

    def pick_output_dir(self):
        import webview
        res = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return res[0] if res else None

    # ------------------------------------------------------------------ anteprima
    def load_preview(self, path, points=None, force=False):
        """Avvia l'anteprima in background. Eventi: preview_progress, preview_done.
        Il campione (qualche MB) si ritira poi con get_preview()."""
        with self._lock:
            if self._busy():
                return {"ok": False, "error": "C'è già un'elaborazione in corso"}
            target = int(points or preview_mod.DEFAULT_TARGET)
            self._preview = None
            store.save_settings({"last_input": path})
            self._cancel_fn = None
            self._job = threading.Thread(target=self._run_preview, args=(path, target, force),
                                         name="anteprima", daemon=True)
            self._job.start()
            return {"ok": True}

    def _run_preview(self, path, target, force):
        self._emit({"type": "preview_start"})
        res = None if force else preview_mod.load_cached(path, target)
        if res:
            log.info(f"Anteprima dalla cache: {path}")
        else:
            info = self.inspect(path)
            if info.get("error"):
                res = info
            else:
                mem = memory_info()
                log.info(f"Anteprima di {path}" + (f" · RAM libera {gb(mem[1])}" if mem else ""))
                res = worker.preview_isolated(path, info.get("points"), target, self._emit,
                                              register_cancel=self._register_cancel)
                if not res.get("error"):
                    res["info"] = info
        if res.get("error"):
            log.error(res["error"])
            self._emit({"type": "preview_done", "ok": False, "error": res["error"],
                        "cancelled": bool(res.get("cancelled"))})
            return
        if "info" not in res:
            p = Path(path)
            res["info"] = {"path": str(p), "name": p.name, "size": p.stat().st_size,
                           "points": res.get("points_file"), "format": p.suffix.lstrip(".").upper(),
                           "default_output": pipeline.default_output_dir(path)}
        self._preview = res
        self._emit({"type": "preview_done", "ok": True, "seconds": res.get("seconds"),
                    "from_cache": bool(res.get("from_cache"))})

    def get_preview(self):
        return self._preview

    # ------------------------------------------------------------------ elaborazione
    def start(self, input_path, output_dir, params):
        """Compatibilità 0.1: solo la pianta d'interno."""
        return self.start_scene(input_path, output_dir, "interno", {"plan": params, "scene": {}})

    def start_scene(self, input_path, output_dir, mode, params):
        with self._lock:
            if self._busy():
                return {"ok": False, "error": "C'è già un'elaborazione in corso"}
            if mode not in scene.MODES:
                return {"ok": False, "error": f"Tipo di rilievo non valido: {mode}"}
            params = params or {}
            try:   # validazione immediata: gli errori dei parametri arrivano subito all'interfaccia
                sp = scene.SceneParams.from_dict(dict(params.get("scene") or {}, mode=mode))
                if mode == "interno":
                    pipeline.Params.from_dict(params.get("plan") or {})
            except Exception as ex:
                return {"ok": False, "error": str(ex)}
            n_sec = sum(ln.kind == "sezione" for ln in sp.lines)
            if mode == "interno":
                stages = list(pipeline.STAGES) + ([("sections", f"Sezioni verticali ({n_sec})")] if n_sec else [])
            else:
                stages = scene.stages_for(mode, n_sec, len(sp.lines) - n_sec, bool(sp.bbox))
            out = output_dir or pipeline.default_output_dir(input_path)
            settings = store.load_settings()
            sp_saved = settings.get("scene_params", {})
            sp_saved[mode] = {k: v for k, v in (params.get("scene") or {}).items() if k not in ("bbox", "lines")}
            new = {"last_input": input_path, "output_dir": output_dir or "", "last_mode": mode,
                   "scene_params": sp_saved}
            if mode == "interno" and params.get("plan"):
                new["params"] = params["plan"]
            store.save_settings(new)
            self._cancel_fn = None
            self._job = threading.Thread(target=self._run_scene, args=(input_path, out, mode, params),
                                         name="elaborazione", daemon=True)
            self._job.start()
            return {"ok": True, "output_dir": out, "stages": stages}

    def cancel(self):
        if self._busy():
            log.info("Richiesta di annullamento")
            if self._cancel_fn:
                self._cancel_fn()
        return True

    def _emit(self, event):
        if self._window is None:
            return
        try:
            self._window.evaluate_js(f"window.tracciato && window.tracciato.onEvent({json.dumps(event)})")
        except Exception:
            log.debug("Evento non consegnato all'interfaccia", exc_info=True)

    def _register_cancel(self, fn):
        self._cancel_fn = fn

    def _run_scene(self, input_path, out_dir, mode, params):
        mem = memory_info()
        size = os.path.getsize(input_path) if os.path.exists(input_path) else 0
        log.info(f"Avvio elaborazione [{scene.MODES[mode]}]: {input_path} ({size / 1e6:.1f} MB) → {out_dir}"
                 + (f" · RAM libera {gb(mem[1])} su {gb(mem[0])}" if mem else ""))
        if mem and mem[1] < 1.5 * 1024**3:
            log.warning("Poca RAM libera: chiudi altri programmi se l'elaborazione si interrompe")
        self._emit({"type": "start"})
        try:
            summary = worker.run_scene_isolated(input_path, out_dir, mode, params, self._emit,
                                                register_cancel=self._register_cancel)
        except Exception as ex:   # errori nell'avvio del processo
            log.exception("Elaborazione non avviata")
            summary = {"status": "error", "error": str(ex), "input": input_path, "files": {}, "mode": mode,
                       "outputs": []}
        entry = {k: summary.get(k) for k in ("status", "input", "output_dir", "started", "seconds",
                                             "area_total", "doors", "windows", "paper", "error",
                                             "files", "params", "rooms", "height", "rotation_deg",
                                             "points_file", "mode", "mode_label", "outputs", "footprint")}
        entry["mode"] = entry["mode"] or mode
        entry["started"] = entry["started"] or datetime.now().isoformat(timespec="seconds")
        entry["crash"] = summary.get("crash")
        store.add_history({k: v for k, v in entry.items() if k != "crash"})
        log.info(f"Fine elaborazione: stato={summary.get('status')} tempo={summary.get('seconds')} s")
        self._emit({"type": "done", "summary": entry})

    # ------------------------------------------------------------------ risultati
    def preview(self, path):
        try:
            data = Path(path).read_bytes()
            return "data:image/png;base64," + base64.b64encode(data).decode()
        except Exception:
            return None

    def open_path(self, path):
        if not path or not os.path.exists(path):
            return {"ok": False, "error": "Il file non esiste più (spostato o cancellato?)"}
        log.info(f"Apertura: {path}")
        open_in_os(path)
        return {"ok": True}

    def open_logs(self):
        return self.open_path(str(log_dir()))

    def read_app_log(self, lines=200):
        try:
            text = (log_dir() / "tracciato.log").read_text(encoding="utf-8", errors="replace")
            return text.splitlines()[-int(lines):]
        except FileNotFoundError:
            return []


def main(debug=False):
    import webview
    path = setup_logging()
    log.info(f"Avvio {__app_name__} {__version__} · {system_summary()}")
    log.info(f"Log: {path}")
    api = Api()
    previous = begin_session()
    if previous:
        log.warning(f"La sessione precedente ({previous}) non si è chiusa correttamente")
        api._previous_crash = previous
    index = resource_dir() / "ui" / "index.html"
    window = webview.create_window(
        f"{__app_name__} — AD Studio Tecnico", url=str(index), js_api=api,
        width=1440, height=920, min_size=(1180, 740), background_color="#F6F5F2", text_select=True)
    api._window = window
    def _closed():
        log.info("Chiusura applicazione")
        end_session()
    window.events.closed += _closed
    try:
        webview.start(debug=debug, private_mode=False, storage_path=str(data_dir() / "webview"))
    except Exception:
        log.exception("Errore del motore dell'interfaccia (WebView2)")
        raise

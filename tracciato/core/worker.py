"""Esecuzione isolata in un processo separato.

Se la lettura di una nuvola fa crollare una libreria nativa (memoria esaurita, file danneggiato…)
Windows chiude solo il processo di elaborazione: l'interfaccia resta aperta, registra il codice
d'uscita e la parte di crash-elaborazione.log scritta da faulthandler, e mostra l'errore.

Processi:
  _inspect_entry   lettura dell'intestazione
  _preview_entry   anteprima della nuvola (campione per il viewer 3D, livelli, tipo proposto)
  _job_entry       pianta (motore attuale, "Interno appartamento/ufficio" senza sezioni)
  _scene_entry     elaborati per tipo di rilievo (interno + sezioni, esterno, multi-piano)
"""
import logging
import multiprocessing as mp
import queue as queue_mod
import time
from pathlib import Path

from .logs import log_dir, tail

log = logging.getLogger("tracciato.worker")

# codici d'uscita Windows più comuni (NTSTATUS)
WIN_CODES = {
    0xC0000005: "accesso alla memoria non valido (crash di una libreria nativa)",
    0xC0000017: "memoria insufficiente",
    0xC00000FD: "stack overflow",
    0xC0000409: "interruzione forzata della libreria nativa (spesso memoria insufficiente)",
    0xC000001D: "istruzione non supportata dal processore",
    0xE06D7363: "eccezione C++ non gestita (spesso memoria insufficiente o file danneggiato)",
}


def describe_exit(code) -> str:
    if code is None:
        return "processo terminato"
    c = code & 0xFFFFFFFF if code < 0 or code > 0x7FFFFFFF else code
    desc = WIN_CODES.get(c)
    return f"codice {code} (0x{c:08X}){' — ' + desc if desc else ''}"


# ------------------------------------------------------------------ funzioni eseguite nel figlio
def _job_entry(q, cancel_ev, input_path, out_dir, params_dict):
    from .logs import setup_worker_logging
    setup_worker_logging(q, f"elaborazione {input_path}")
    from . import pipeline
    try:
        p = pipeline.Params.from_dict(params_dict)
        summary = pipeline.run_job(input_path, out_dir, p,
                                   on_event=lambda e: q.put(("event", e)),
                                   cancelled=cancel_ev.is_set)
    except Exception as ex:
        logging.getLogger("tracciato.worker").exception("Elaborazione fallita prima dell'avvio")
        summary = {"status": "error", "error": str(ex), "input": input_path, "files": {}}
    q.put(("done", summary))


def _scene_entry(q, cancel_ev, input_path, out_dir, mode, params):
    from .logs import setup_worker_logging
    setup_worker_logging(q, f"elaborazione {mode} {input_path}")
    from . import scene
    try:
        summary = scene.run(input_path, out_dir, mode, params,
                            on_event=lambda e: q.put(("event", e)), cancelled=cancel_ev.is_set)
    except Exception as ex:
        logging.getLogger("tracciato.worker").exception("Elaborazione fallita prima dell'avvio")
        summary = {"status": "error", "error": str(ex), "input": input_path, "files": {}, "mode": mode,
                   "outputs": []}
    q.put(("done", summary))


def _preview_entry(q, cancel_ev, path, total_hint, target):
    from .logs import setup_worker_logging
    setup_worker_logging(q, f"anteprima {path}")
    from . import engine, preview
    try:
        hooks = engine.Hooks(progress=lambda s, f: q.put(("event", {"type": "preview_progress", "value": round(f, 3)})),
                             cancelled=cancel_ev.is_set)
        res = preview.build_preview(path, total_hint=total_hint, target=target, hooks=hooks)
        preview.save_cached(path, res, target)
    except engine.Cancelled:
        res = {"error": "Anteprima annullata", "cancelled": True}
    except Exception as ex:
        logging.getLogger("tracciato.worker").exception("Anteprima non riuscita")
        res = {"error": f"Anteprima non riuscita: {ex}"}
    q.put(("done", res))


def _inspect_entry(q, path):
    from .logs import setup_worker_logging
    setup_worker_logging(q, f"lettura intestazione {path}")
    from . import pipeline
    q.put(("done", pipeline.inspect_file(path)))


_WITH_CANCEL = (_job_entry, _scene_entry, _preview_entry)


# ------------------------------------------------------------------ lato interfaccia
class Isolated:
    """Avvia una funzione in un processo separato e ne raccoglie log, eventi e risultato."""

    def __init__(self, target, args, on_event=None, name="elaborazione"):
        self.ctx = mp.get_context("spawn")
        self.q = self.ctx.Queue()
        self.cancel_ev = self.ctx.Event()
        self.on_event = on_event or (lambda e: None)
        self.crash_file = log_dir() / "crash-elaborazione.log"
        self._crash_offset = self.crash_file.stat().st_size if self.crash_file.exists() else 0
        a = (self.q, self.cancel_ev, *args) if target in _WITH_CANCEL else (self.q, *args)
        self.proc = self.ctx.Process(target=target, args=a, name=name, daemon=True)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "log":
            _, level, name, text = msg
            logging.getLogger(name).log(level, text)
        elif kind == "event":
            self.on_event(msg[1])
        elif kind == "done":
            return msg[1]
        return None

    def run(self, timeout=None, cancel_grace=8.0):
        t0 = time.monotonic()
        self.proc.start()
        log.info(f"Processo '{self.proc.name}' avviato (pid {self.proc.pid})")
        result, cancel_at = None, None
        while True:
            try:
                r = self._handle(self.q.get(timeout=0.3))
                if r is not None:
                    result = r
                    break
            except queue_mod.Empty:
                pass
            if self.cancel_ev.is_set() and cancel_at is None:
                cancel_at = time.monotonic()
            if cancel_at and time.monotonic() - cancel_at > cancel_grace and self.proc.is_alive():
                log.warning("Il processo non si ferma: lo termino")
                self.proc.terminate()
            if timeout and time.monotonic() - t0 > timeout and self.proc.is_alive():
                log.error(f"Tempo massimo superato ({timeout} s): termino il processo")
                self.proc.terminate()
            if not self.proc.is_alive():
                # svuoto i messaggi rimasti in coda
                try:
                    while True:
                        r = self._handle(self.q.get(timeout=0.5))
                        if r is not None:
                            result = r
                except queue_mod.Empty:
                    pass
                break
        self.proc.join(timeout=5)
        code = self.proc.exitcode
        if result is None:
            crash = self._crash_text()
            if self.cancel_ev.is_set():
                log.warning("Processo terminato su richiesta dell'utente")
                return None, "cancelled", ""
            log.error(f"Il processo '{self.proc.name}' si è chiuso in modo anomalo: {describe_exit(code)}")
            if crash:
                log.error("Ultime righe di crash-elaborazione.log:\n" + crash)
            return None, describe_exit(code), crash
        return result, "ok", ""

    def cancel(self):
        self.cancel_ev.set()

    def _crash_text(self) -> str:
        try:
            if not self.crash_file.exists():
                return ""
            with open(self.crash_file, "rb") as f:
                f.seek(self._crash_offset)
                return f.read().decode("utf-8", errors="replace").strip()[-4000:]
        except Exception:
            return tail(self.crash_file)


HINT = ("Controlla la RAM libera e chiudi altri programmi; se si ripete, prova con dettaglio "
        "“Medio” o invia crash-elaborazione.log.")


def run_job_isolated(input_path, out_dir, params_dict, on_event, register_cancel=None):
    iso = Isolated(_job_entry, (input_path, out_dir, params_dict), on_event)
    if register_cancel:
        register_cancel(iso.cancel)
    result, how, crash = iso.run()
    if result is not None:
        return result
    if how == "cancelled":
        return {"status": "cancelled", "input": input_path, "output_dir": out_dir, "files": {},
                "params": params_dict}
    return {"status": "error", "input": input_path, "output_dir": out_dir, "files": {}, "params": params_dict,
            "error": f"L'elaborazione si è interrotta in modo anomalo ({how}). {HINT}", "crash": crash[-1500:]}


def run_scene_isolated(input_path, out_dir, mode, params, on_event, register_cancel=None):
    iso = Isolated(_scene_entry, (input_path, out_dir, mode, params), on_event, name=f"elaborazione-{mode}")
    if register_cancel:
        register_cancel(iso.cancel)
    result, how, crash = iso.run()
    if result is not None:
        return result
    base = {"input": input_path, "output_dir": out_dir, "files": {}, "params": params, "mode": mode, "outputs": []}
    if how == "cancelled":
        return base | {"status": "cancelled"}
    return base | {"status": "error", "crash": crash[-1500:],
                   "error": f"L'elaborazione si è interrotta in modo anomalo ({how}). {HINT}"}


def preview_isolated(path, total_hint, target, on_event, register_cancel=None):
    iso = Isolated(_preview_entry, (path, total_hint, target), on_event, name="anteprima")
    if register_cancel:
        register_cancel(iso.cancel)
    result, how, _ = iso.run()
    if result is not None:
        return result
    if how == "cancelled":
        return {"error": "Anteprima annullata", "cancelled": True}
    return {"error": f"Anteprima interrotta in modo anomalo ({how}). Il file potrebbe essere danneggiato, "
                     f"non standard o troppo grande per la RAM libera."}


def inspect_isolated(path, timeout=120):
    iso = Isolated(_inspect_entry, (path,), name="lettura-intestazione")
    result, how, _ = iso.run(timeout=timeout)
    if result is None:
        return {"error": f"Impossibile leggere il file: il processo di lettura si è interrotto ({how}). "
                         f"Il file potrebbe essere danneggiato o non standard."}
    return result

"""Storico delle elaborazioni e impostazioni dell'utente (file JSON nella cartella dati)."""
import json
import logging
import threading
from pathlib import Path

from .logs import data_dir

log = logging.getLogger("tracciato.store")
_lock = threading.Lock()
MAX_HISTORY = 100


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception:
        log.exception(f"File {path.name} illeggibile: riparto da zero (copia in {path.name}.bak)")
        try:
            path.replace(path.with_suffix(path.suffix + ".bak"))
        except Exception:
            pass
        return default


def _write(path: Path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)          # scrittura atomica


def history_path() -> Path:
    return data_dir() / "storico.json"


def settings_path() -> Path:
    return data_dir() / "impostazioni.json"


def load_history():
    with _lock:
        return _read(history_path(), [])


def add_history(entry: dict):
    with _lock:
        h = _read(history_path(), [])
        h.insert(0, entry)
        _write(history_path(), h[:MAX_HISTORY])


def load_settings():
    with _lock:
        return _read(settings_path(), {})


def save_settings(s: dict):
    with _lock:
        cur = _read(settings_path(), {})
        cur.update(s)
        _write(settings_path(), cur)

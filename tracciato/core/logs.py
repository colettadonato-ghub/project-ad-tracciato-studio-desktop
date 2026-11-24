"""Log dell'applicazione.

File nella cartella  %LOCALAPPDATA%\\Tracciato\\logs\\  (o TRACCIATO_HOME\\logs):

  tracciato.log              log applicativo a rotazione (5 × 2 MB): avvio, sistema (RAM, disco),
                             file aperti, elaborazioni, errori con traceback, utente@PC su ogni riga
  crash.log                  crash "nativi" dell'interfaccia (faulthandler): anche quando il processo
                             viene terminato da Windows e il logging normale non fa in tempo a scrivere
  crash-elaborazione.log     come sopra, ma del processo separato che esegue l'elaborazione
  output.log                 tutto ciò che le librerie scrivono su stdout/stderr (l'exe non ha console)

Più un file .log accanto a ogni pianta, con i passaggi di quella singola elaborazione.
"""
import ctypes
import faulthandler
import getpass
import logging
import os
import platform
import shutil
import sys
import tempfile
import threading
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

FMT = "%(asctime)s | %(levelname)-7s | %(user)s@%(host)s | pid %(process)d | %(name)s | %(message)s"
JOB_FMT = "%(asctime)s | %(levelname)-7s | %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"
_DATA_DIR = None
_keep_open = []          # file aperti per faulthandler: non devono essere chiusi


def _writable(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".scrittura"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def data_dir() -> Path:
    """Cartella dati scrivibile: TRACCIATO_HOME → %LOCALAPPDATA%\\Tracciato → accanto all'exe → TEMP."""
    global _DATA_DIR
    if _DATA_DIR:
        return _DATA_DIR
    candidates = []
    if os.environ.get("TRACCIATO_HOME"):
        candidates.append(Path(os.environ["TRACCIATO_HOME"]))
    if sys.platform == "win32":
        candidates.append(Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Tracciato")
    else:
        candidates.append(Path.home() / ".tracciato")
    candidates.append(Path(sys.executable).parent / "dati")
    candidates.append(Path(tempfile.gettempdir()) / "Tracciato")
    for c in candidates:
        if _writable(c):
            _DATA_DIR = c
            return c
    raise RuntimeError("Nessuna cartella scrivibile per i dati di Tracciato")


def log_dir() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


class _ContextFilter(logging.Filter):
    user = getpass.getuser()
    host = platform.node()

    def filter(self, record):
        record.user = self.user
        record.host = self.host
        return True


def memory_info():
    """(totale, disponibile) in byte; None se non rilevabile."""
    try:
        if sys.platform == "win32":
            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.ullTotalPhys, m.ullAvailPhys
        pages, size = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
        avail = os.sysconf("SC_AVPHYS_PAGES") * size
        return pages * size, avail
    except Exception:
        return None


def gb(b):
    return f"{b / 1024**3:.1f} GB"


def system_summary() -> str:
    mem = memory_info()
    parts = [f"Python {sys.version.split()[0]}", f"{platform.system()} {platform.release()} ({platform.version()})",
             f"CPU {os.cpu_count()}", "exe" if getattr(sys, "frozen", False) else "sorgente"]
    if mem:
        parts.append(f"RAM {gb(mem[0])}, libera {gb(mem[1])}")
    try:
        parts.append(f"disco libero {gb(shutil.disk_usage(data_dir()).free)}")
    except Exception:
        pass
    return " · ".join(parts)


def _enable_faulthandler(filename: str, header: str):
    f = open(log_dir() / filename, "a", encoding="utf-8", buffering=1)
    f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} · pid {os.getpid()} · {header}\n")
    f.flush()
    faulthandler.enable(file=f, all_threads=True)
    _keep_open.append(f)


def _redirect_std():
    """L'exe senza console ha stdout/stderr = None: li mando su output.log."""
    if sys.stdout is None or sys.stderr is None:
        f = open(log_dir() / "output.log", "a", encoding="utf-8", buffering=1)
        _keep_open.append(f)
        if sys.stdout is None:
            sys.stdout = f
        if sys.stderr is None:
            sys.stderr = f


def _install_excepthooks(root):
    def _excepthook(exc_type, exc, tb):
        root.critical("Eccezione non gestita", exc_info=(exc_type, exc, tb))

    def _thread_excepthook(args):
        root.critical(f"Eccezione non gestita nel thread {args.thread.name}",
                      exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook


def setup_logging(level=logging.INFO, console=False) -> Path:
    """Logging del processo principale. Ritorna il percorso del log applicativo."""
    root = logging.getLogger("tracciato")
    path = log_dir() / "tracciato.log"
    if getattr(root, "_configured", False):
        return path
    _redirect_std()
    root.setLevel(level)
    root.propagate = False
    fh = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=5, encoding="utf-8", delay=False)
    fh.setFormatter(logging.Formatter(FMT, DATEFMT))
    fh.addFilter(_ContextFilter())
    root.addHandler(fh)
    if console:
        # Riconfigura stdout per accettare Unicode (es. ▶ → ✔) senza crash su console cp1252.
        # `reconfigure` è disponibile su TextIOWrapper (stdout in console Windows); se manca
        # (stdout = None ridiretta su file), l'errore viene silenziato via errors='replace'.
        try:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(logging.Formatter("  · %(message)s"))
        # Ulteriore fallback: se il formattatore produce un testo non codificabile,
        # lo StreamHandler non deve propagare l'eccezione al chiamante.
        ch.handleError = lambda record: None
        root.addHandler(ch)
    _enable_faulthandler("crash.log", "interfaccia")
    _install_excepthooks(root)
    root._configured = True
    return path


# ------------------------------------------------------------------ processo di elaborazione
class QueueForwarder(logging.Handler):
    """Nel processo di elaborazione: inoltra i messaggi al processo principale, che li scrive
    nel log applicativo (un solo processo scrive su tracciato.log, niente conflitti di rotazione)."""

    def __init__(self, q):
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record):
        try:
            msg = self.format(record)   # include l'eventuale traceback
            self.q.put(("log", record.levelno, record.name, msg))
        except Exception:
            pass


def setup_worker_logging(q, header: str):
    root = logging.getLogger("tracciato")
    for h in list(root.handlers):
        root.removeHandler(h)
    _redirect_std()
    root.setLevel(logging.INFO)
    root.propagate = False
    root.addHandler(QueueForwarder(q))
    _enable_faulthandler("crash-elaborazione.log", header)
    _install_excepthooks(root)


# ------------------------------------------------------------------ chiusura anomala
def _marker() -> Path:
    return data_dir() / "sessione-in-corso.txt"


def begin_session() -> str | None:
    """Segna l'avvio. Se il segnaposto della sessione precedente esiste ancora, l'app non si era
    chiusa correttamente: ritorna la descrizione per avvisare l'utente."""
    m = _marker()
    previous = None
    if m.exists():
        try:
            previous = m.read_text(encoding="utf-8").strip()
        except Exception:
            previous = "sessione precedente"
    m.write_text(f"avviata {datetime.now():%Y-%m-%d %H:%M:%S} · pid {os.getpid()}", encoding="utf-8")
    return previous


def end_session():
    try:
        _marker().unlink()
    except FileNotFoundError:
        pass


def tail(path: Path, max_bytes=4000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


class JobLog:
    """Context manager: durante l'elaborazione copia i messaggi anche in un file dedicato
    e (opzionale) li inoltra all'interfaccia."""

    def __init__(self, path: Path, sink=None):
        self.path = Path(path)
        self.sink = sink
        self._handlers = []

    def __enter__(self):
        root = logging.getLogger("tracciato")
        fh = logging.FileHandler(self.path, encoding="utf-8")
        fh.setFormatter(logging.Formatter(JOB_FMT, DATEFMT))
        root.addHandler(fh)
        self._handlers.append(fh)
        if self.sink:
            sink = self.sink

            class _UIHandler(logging.Handler):
                def emit(self, record):
                    try:
                        sink(record.levelname, self.format(record))
                    except Exception:
                        pass

            uh = _UIHandler()
            uh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
            root.addHandler(uh)
            self._handlers.append(uh)
        return self

    def __exit__(self, *exc):
        root = logging.getLogger("tracciato")
        for h in self._handlers:
            root.removeHandler(h)
            h.close()
        return False

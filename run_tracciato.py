"""Avvio di Tracciato (usato da PyInstaller e da `python run_tracciato.py`)."""
import multiprocessing

from tracciato.__main__ import run

if __name__ == "__main__":
    multiprocessing.freeze_support()
    run()

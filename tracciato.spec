# -*- mode: python ; coding: utf-8 -*-
# PyInstaller: due eseguibili nella stessa cartella dist\Tracciato\
#   Tracciato.exe     → app con interfaccia (senza console)
#   TracciatoCLI.exe  → riga di comando: --cli, --selftest
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = [("tracciato/ui", "tracciato/ui")]
datas += collect_data_files("ezdxf")
datas += collect_data_files("webview")
hidden = collect_submodules("webview", filter=lambda m: "android" not in m and "cocoa" not in m)
hidden += ["laspy", "lazrs", "pye57", "shapely", "clr",
           # matplotlib carica i backend solo al momento del salvataggio: vanno dichiarati
           "matplotlib.backends.backend_pdf", "matplotlib.backends.backend_agg"]

a = Analysis(["run_tracciato.py"], pathex=["."], datas=datas, hiddenimports=hidden,
             excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "IPython", "pytest"],
             noarchive=False)
pyz = PYZ(a.pure)

gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Tracciato", console=False,
          icon="assets/tracciato.ico", version=None)
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="TracciatoCLI", console=True,
          icon="assets/tracciato.ico")
coll = COLLECT(gui, cli, a.binaries, a.datas, strip=False, upx=False, name="Tracciato")

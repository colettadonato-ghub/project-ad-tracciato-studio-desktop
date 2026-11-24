"""Genera una nuvola di punti sintetica (appartamento 11×8 m) per provare Tracciato.
Uso: python tools/make_test_cloud.py prova.las
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tracciato.core.testcloud import make_cloud

if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "test_appartamento.las"
    print(f"Scritti {make_cloud(out):,} punti in {out}")

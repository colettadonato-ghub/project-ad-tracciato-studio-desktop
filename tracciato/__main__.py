"""Punto d'ingresso.

  AD-Tracciato-Studio-Pro.exe                          apre l'interfaccia
  AD-Tracciato-Studio-Pro.exe --cli nuvola.e57 [...]   pianta d'interno da riga di comando (come la 0.1)
  AD-Tracciato-Studio-Pro.exe --scene esterno|multipiano|interno nuvola.las [--params parametri.json] [--out cartella]
                                         elaborati per tipo di rilievo; senza --params usa l'anteprima
                                         per proporre piano 0, livelli e linee
  AD-Tracciato-Studio-Pro.exe --selftest               genera nuvole di prova e le elabora (verifica installazione)
  AD-Tracciato-Studio-Pro.exe --debug                  interfaccia con strumenti sviluppatore
"""
import argparse
import json
import sys


def _cli(argv):
    from .core import pipeline
    from .core.logs import setup_logging
    ap = argparse.ArgumentParser(prog="AD-Tracciato-Studio-Pro --cli")
    ap.add_argument("input")
    ap.add_argument("--out", default="")
    ap.add_argument("--cut", type=float, default=1.10)
    ap.add_argument("--scale", type=int, default=1, help="scala 1:N delle tavole (1 = dimensioni reali)")
    ap.add_argument("--detail", choices=["alta", "media", "bozza"], default="alta")
    ap.add_argument("--title", default="RILIEVO — PIANTA")
    ap.add_argument("--floor-z", type=float)
    ap.add_argument("--ceil-z", type=float)
    a = ap.parse_args(argv)
    path = setup_logging(console=True)
    from .core.logs import system_summary
    print(f"Sistema: {system_summary()}")
    p = pipeline.Params(cut=a.cut, scale=a.scale, detail=a.detail, title=a.title,
                        floor_z=a.floor_z, ceil_z=a.ceil_z)
    s = pipeline.run_job(a.input, a.out, p)
    print(json.dumps({k: s.get(k) for k in ("status", "seconds", "area_total", "doors", "windows",
                                            "paper", "error", "files")}, ensure_ascii=False, indent=2))
    print(f"Log applicativo: {path}")
    return 0 if s["status"] == "ok" else 1


def auto_scene_params(prev: dict, mode: str) -> dict:
    """Parametri proposti dall'anteprima (stessa logica dell'interfaccia)."""
    sug, g = prev["suggest"], prev.get("guides") or {}
    sp = {"z0": sug["z0"], "bbox": prev["bbox"], "angle": prev["angle"], "title": "RILIEVO"}
    if mode == "esterno":
        sp["lines"] = list(g.get("fronts", []))
    elif mode == "multipiano":
        names = ["Piano terra"] + [f"Livello {i}" for i in range(1, 10)]
        sp["levels"] = [{"name": names[i], "z": z} for i, z in enumerate(sug["levels"])]
        sp["lines"] = [g["section"]] if g.get("section") else []
    return sp


def _scene_cli(argv):
    from .core import engine, pipeline, preview, scene
    from .core.logs import setup_logging, system_summary
    ap = argparse.ArgumentParser(prog="AD-Tracciato-Studio-Pro --scene")
    ap.add_argument("mode", choices=list(scene.MODES))
    ap.add_argument("input")
    ap.add_argument("--out", default="")
    ap.add_argument("--params", help="JSON {scene: {...}, plan: {...}} (come lo salva l'interfaccia)")
    a = ap.parse_args(argv)
    path = setup_logging(console=True)
    print(f"Sistema: {system_summary()}")
    if a.params:
        with open(a.params, encoding="utf-8") as f:
            params = json.load(f)
    else:
        info = pipeline.inspect_file(a.input)
        prev = preview.build_preview(a.input, total_hint=info.get("points"))
        params = {"scene": auto_scene_params(prev, a.mode), "plan": {}}
        print(f"Proposta dall'anteprima: piano 0 z={prev['suggest']['z0']:.3f}, livelli {prev['suggest']['levels']}, "
              f"tipo {prev['suggest']['mode']}")
    s = scene.run(a.input, a.out or pipeline.default_output_dir(a.input), a.mode, params)
    print(json.dumps({k: s.get(k) for k in ("status", "seconds", "error", "footprint")}, ensure_ascii=False, indent=2))
    for o in s.get("outputs", []):
        print(f"  {o['label']}: {o['png']}")
    print(f"Log applicativo: {path}")
    return 0 if s["status"] == "ok" else 1


def _selftest():
    import tempfile
    from pathlib import Path
    from .core import pipeline, preview, worker
    from .core.logs import setup_logging, system_summary
    from .core.testcloud import make_cloud
    from .core.testscenes import make_exterior, make_multilevel
    path = setup_logging(console=True)
    print(f"Sistema: {system_summary()}\nLog: {path}")
    d = Path(tempfile.mkdtemp(prefix="tracciato_test_"))
    results = []

    # 1 · interno appartamento (motore della 0.1, stessi valori attesi)
    las = d / "appartamento_prova.las"
    make_cloud(str(las))
    s = worker.run_job_isolated(str(las), str(d / "out"), pipeline.Params(title="SELFTEST").__dict__,
                                on_event=lambda e: None)
    ok = s["status"] == "ok" and s["doors"] == 6 and s["windows"] == 4 and abs(s["area_total"] - 75.3) < 1.0
    results.append(("Interno appartamento", ok, s.get("error") or f"{s.get('area_total')} m²"))

    # 2 · esterno edificio 12 × 9 m
    ext = d / "esterno_prova.las"
    make_exterior(str(ext))
    prev = preview.build_preview(str(ext), total_hint=pipeline.inspect_file(str(ext))["points"])
    s = worker.run_scene_isolated(str(ext), str(d / "out"), "esterno",
                                  {"scene": auto_scene_params(prev, "esterno") | {"title": "SELFTEST"}},
                                  on_event=lambda e: None)
    fp = s.get("footprint") or {}
    ok = (prev["suggest"]["mode"] == "esterno" and s["status"] == "ok" and abs(fp.get("area", 0) - 108) < 4
          and len(s.get("outputs", [])) == 5)
    results.append(("Esterno edificio", ok, s.get("error") or f"sagoma {fp.get('area')} m², {len(s.get('outputs', []))} tavole"))

    # 3 · interno multi-piano (solaio a +3,00)
    ml = d / "multipiano_prova.las"
    make_multilevel(str(ml))
    prev = preview.build_preview(str(ml), total_hint=pipeline.inspect_file(str(ml))["points"])
    s = worker.run_scene_isolated(str(ml), str(d / "out"), "multipiano",
                                  {"scene": auto_scene_params(prev, "multipiano") | {"title": "SELFTEST"}},
                                  on_event=lambda e: None)
    lv = prev["suggest"]["levels"]
    ok = (prev["suggest"]["mode"] == "multipiano" and len(lv) == 2 and abs(lv[1] - 3.0) < 0.05
          and s["status"] == "ok" and len(s.get("outputs", [])) == 3)
    results.append(("Interno multi-piano", ok, s.get("error") or f"livelli {lv}, {len(s.get('outputs', []))} tavole"))

    for name, ok, msg in results:
        print(f"  {'OK     ' if ok else 'FALLITO'}  {name}: {msg}")
    all_ok = all(r[1] for r in results)
    print(("SELFTEST OK" if all_ok else "SELFTEST FALLITO") + f" — cartella: {d}")
    return 0 if all_ok else 1


def run():
    argv = sys.argv[1:]
    if argv[:1] == ["--cli"]:
        sys.exit(_cli(argv[1:]))
    if argv[:1] == ["--scene"]:
        sys.exit(_scene_cli(argv[1:]))
    if argv[:1] == ["--selftest"]:
        sys.exit(_selftest())
    from .app import main
    main(debug="--debug" in argv)


if __name__ == "__main__":
    run()

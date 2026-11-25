# AD - Tracciato Studio Pro — Contesto tecnico sviluppo

> Ultimo aggiornamento: 2026-11-25  
> Versione app: 0.3.0  
> File principale: `desktop-lite-app/`

---

## 1. Architettura generale

```
desktop-lite-app/
├── tracciato/
│   ├── app.py            # entry-point Eel (server Python + UI WebView)
│   ├── core/
│   │   ├── engine.py     # 🔑 algoritmo point cloud → pianta (voxel, raster, CV)
│   │   ├── pipeline.py   # orchestrazione: Params, run_job, export DXF/PDF
│   │   ├── worker.py     # processo separato (multiprocessing) per l'elaborazione
│   │   ├── store.py      # persistenza sessione (JSON in AppData)
│   │   └── logs.py       # logging strutturato su file
│   └── ui/
│       ├── index.html    # UI principale (parametri, drop zone)
│       ├── app.js        # logica UI: readParams, applyParams, eventi
│       └── styles.css
├── build.bat             # PyInstaller → dist/AD-Tracciato-Studio-Pro/AD-Tracciato-Studio-Pro.exe
├── tracciato.spec        # spec PyInstaller
└── requirements.txt
```

**Stack**: Python 3.10+, Eel (WebView), OpenCV, NumPy, laspy/pylas, ezdxf, reportlab.  
**Distribuzione**: `.exe` standalone via PyInstaller — nessun Python richiesto sul PC cliente.

---

## 2. Pipeline di elaborazione

```
LAS/E57 → load_bands_streaming → allineamento → build_plan → export DXF + PDF
```

### 2.1 `Params` (pipeline.py)

| Campo | Default | Note |
|---|---|---|
| `cut` | 1.10 m | altezza piano di sezione |
| `scale` | 100 | scala di stampa |
| `detail` | `"alta"` | risoluzione raster |
| `align` | `True` | auto-allineamento muri a 0/90° |
| `max_wall` | 0.55 m | spessore massimo muro |
| `min_room` | 1.0 m² | area minima vano |
| `high_band` | 0.35 m | fascia sotto soffitto per footprint |
| `floor_z` | `None` | quota pavimento forzata |
| `ceil_z` | `None` | quota soffitto forzata |
| `survey_type` | `"interno"` | tipo rilievo: `interno` / `esterno` / `multipiano` |

### 2.2 `build_plan` (engine.py) — fasce Z

Per un file con `floor_z = F`, `ceil_z = C`, `H = C - F`:

| Fascia | Range Z | Uso |
|---|---|---|
| `cut` | F + cut ± 0.05 m | sezione principale (muri) |
| `low` | F + 0.25 … F + 0.60 m | davanzali finestre |
| `high` | C - high_band … C | footprint struttura (soffitto) |

### 2.3 Logica wall detection

```
survey_type == "esterno":
    footprint = fill_thickness(cut)   # perimetro diretto dalla sezione
    walls     = footprint (con kernel 0.20 m invece di 0.05 m)

survey_type in ("interno", "multipiano"):
    footprint = fill_thickness(high)  # dal soffitto
    # Fallback 1: se high_pixels < max(100, cut_pixels // 20)
    #   → footprint = fill_thickness(cut)   [soffitto non scansionato]
    walls = bitwise_and(fill_thickness(cut), dilate(footprint, 0.04))
    # Fallback 2: se wall_polys vuota dopo bitwise_and
    #   → walls = fill_thickness(cut) diretto [cut grezzo]
```

**Bug fix applicato (2026-11-23)**: `Grid.raster()` ora fa `np.clip(ix, 0, w-1)` / `np.clip(iy, 0, h-1)` prima di `np.add.at` per evitare `IndexError` quando dopo la rotazione di allineamento i punti escono di 1-2 px dalla bounding box.

---

## 3. Tipi di rilievo (`survey_type`)

| Tipo | Caso d'uso | Comportamento pipeline |
|---|---|---|
| `interno` | appartamento, ufficio, negozio | standard: cut @ 1.10 m, footprint da soffitto |
| `esterno` | facciata, perimetro edificio | cut @ 1.00 m, footprint diretto da cut, kernel morfo 0.20 m |
| `multipiano` | edificio a più livelli | come interno, pronto per slice per piano (TODO: floor selector) |

Implementato su:
- `pipeline.py` → `Params.survey_type`, `engine_args()` adatta `cut` e `high_band`
- `engine.py` → `build_plan` branchia su `survey_type`
- `index.html` → selettore `<select id="pSurveyType">` + campo piano nascosto `fieldFloorN`
- `app.js` → `readParams()` / `applyParams()`, `updateSurveyUI()`, listener change

---

## 4. UI — campi parametri

| ID elemento | Tipo | Descrizione |
|---|---|---|
| `pSurveyType` | select | tipo rilievo (vedi §3) |
| `fieldFloorN` | div nascosto | piano da estrarre (solo multipiano) |
| `pCut` | range slider | altezza sezione (0.5–3.0 m) |
| `pCutNum` | number input | stesso valore di pCut, editabile direttamente |
| `vCut` | span | valore leggibile (es. "1,10 m") |
| `pDetail` | select | risoluzione raster |
| `pAlign` | checkbox | auto-allineamento |
| `pSection` | checkbox | punti sezione in DXF |
| `pWall` | number | spessore max muro |
| `pRoom` | number | area min vano |
| `pBand` | number | fascia alta (high_band) |
| `pFloor` / `pCeil` | number | quote forzate pavimento/soffitto |

`pCut` e `pCutNum` sono **sincronizzati bidirezionalmente** via `syncCut(val)`.  
Quando `pSurveyType` cambia, `updateSurveyUI()` aggiorna hint, visibilità `fieldFloorN` e aggiusta `pCut` di default.

---

## 5. File di sessione / AppData

```
C:\Users\<user>\AppData\Local\Tracciato\
├── tracciato.log          # log rotante
└── session.json           # ultimi parametri usati (auto-restore)
```

Sicuro da cancellare per reset pulito (non contiene dati di output).

---

## 6. Rebuild exe

```bat
cd C:\personale\piattaforma-studio-tecnico\desktop-lite-app
rmdir /s /q dist build
rmdir /s /q tracciato\__pycache__ tracciato\core\__pycache__ tracciato\ui\__pycache__
build.bat
```

> ⚠️ Cancellare sempre `__pycache__` prima del rebuild: PyInstaller può includere `.pyc` stale e non vedere le modifiche ai sorgenti.

Output: `dist\AD-Tracciato-Studio-Pro\AD-Tracciato-Studio-Pro.exe` (standalone, copiabile su qualsiasi PC Windows).

---

## 7. Test di regressione

```bat
dist\AD-Tracciato-Studio-Pro\AD-Tracciato-Studio-Pro-CLI.exe --selftest
```

Output atteso (appartamento_prova.las interno):
```
✔ selftest OK — 4 vani, 75.28 m², 4.8 s
```

---

## 8. Problemi noti / TODO

| # | Stato | Descrizione |
|---|---|---|
| 1 | ✅ risolto | `Nessun muro rilevato` per H > 8 m (soffitto fuori fascia) |
| 2 | ✅ risolto | `IndexError: index out of bounds` a cut < 0.80 m dopo rotazione |
| 3 | ✅ implementato | Selettore tipo rilievo (interno/esterno/multipiano) in UI |
| 4 | 🔄 in progress | Output `esterno`: 0 vani attesi, ma perimetro da verificare qualitativamente |
| 5 | 📋 TODO | `multipiano`: floor selector, slice automatica per livello Z |
| 6 | 📋 TODO | Aggiungere `survey_type` nei metadati JSON di output |
| 7 | 📋 TODO | Viewer anteprima pianta integrato nell'UI |

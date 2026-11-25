# AD - Tracciato Studio Pro: dalla nuvola di punti agli elaborati

Micro-app desktop di **AD Studio Tecnico**. Apre un rilievo laser (`.las`, `.laz`, `.e57`) e ne mostra un'**anteprima 3D**. Poi chiede cosa rappresenta la nuvola e, in base alla risposta, i parametri necessari. Infine genera le tavole in **PNG**.

È la versione "lite", per un singolo PC, della funzione *Rilievo* della piattaforma (vedi `design/webapp/Rilievo.html`). Usa lo stesso motore e lo stesso sistema visivo, così il codice passa alla web app senza riscritture. Si distribuisce come cartella con `AD-Tracciato-Studio-Pro.exe`: sul PC dello studio non serve installare nulla.

## Uso

| Passaggio | Cosa fai | Cosa succede |
|---|---|---|
| **1 · Nuvola e anteprima** | **Scegli nuvola…** | Il file viene letto a blocchi. Se ne ricava un campione di 400 000 punti, visibile nel viewer (3D, pianta, laterale). Il viewer mostra anche ingombro, dislivello, rotazione dei muri e quote delle superfici orizzontali. L'anteprima resta in cache: riaprire lo stesso file è immediato. |
| **2 · Tipo di rilievo** | Scegli uno dei tre tipi | L'app propone il tipo più probabile (etichetta *Proposto*). |
| **3 · Parametri** | Controlli quote e linee | Nella nuvola vedi in tempo reale cosa verrà tagliato: blu = sezione orizzontale, ocra = sezione verticale. La vista *Laterale* mostra le quote. |
| **4 · Elaborati PNG** | **Genera elaborati** | Pipeline e log in diretta, poi l'elenco delle tavole. Clic su una tavola per vederla, doppio clic per aprirla. |

### I tre tipi di rilievo

**Interno appartamento/ufficio**
- È il caso già esistente e funziona bene.
- Parametri: altezza di sezione dal pavimento, ortogonalità dei muri e le opzioni avanzate di sempre.
- Elaborati: pianta in DXF a layer, PDF in scala e PNG.
- Puoi aggiungere sezioni verticali facoltative.

**Esterno edificio** (perimetro, facciata, sagoma planimetrica)
- **Piano 0:** è la quota del terreno. Lo scegli dalle pastiglie delle superfici orizzontali trovate o lo scrivi a mano.
- **Sezione del perimetro:** quota e spessore della fascia in cui leggere il filo delle facciate. Il default è 1,00 m ± 15 cm.
- **Prospetti:** sono proposti i 4 fronti. Per ognuno indichi la linea di vista e la profondità. Puoi anche disegnarne altri.
- **Sezioni:** linea e spessore, facoltative.
- **Elaborati:**
  - *sagoma planimetrica* con perimetro quotato, superficie coperta e H max, più il perimetro in DXF;
  - un PNG per ogni prospetto: più il punto è vicino alla linea di vista, più è scuro;
  - le sezioni.

**Interno multi-piano** (scale, soppalchi, H > 4 m con più livelli)
- **Piano 0.**
- **Livelli:** nome, quota del pavimento dal piano 0, altezza del taglio e spessore della fascia. Sono proposti dalle coppie solaio/pavimento trovate nella nuvola. Per un soppalco usa un taglio più basso, per esempio 0,60 m.
- **Sezioni verticali:** linea e spessore. Lo *spessore* è la fascia tagliata, campita in nero. *Oltre* è quanto si vede dietro al taglio, in grigio.
- **Elaborati:**
  - una pianta per livello, tutte con la stessa inquadratura, quindi sovrapponibili;
  - in nero ciò che è tagliato, in grigio ciò che sta sotto il taglio;
  - le sezioni, con le linee di livello quotate.

### Linee di sezione e prospetto

- Premi **Disegna** e fai due clic sulla nuvola, in vista 3D o Pianta.
- **Maiusc** disegna la linea parallela o perpendicolare ai muri. **Esc** annulla.
- La freccia indica il verso di vista. **⇄** lo inverte.
- Ogni tavola di pianta riporta le tracce delle sezioni e dei prospetti.

### Tavole

- Le tavole sono in scala **1:1 (predefinita, dimensioni reali)**, 1:20, 1:50, 1:100, 1:200 o 1:500.
- Se il disegno entra in A3…A0 alla scala scelta, il formato è scelto in automatico. Se non entra (sempre a 1:1) la tavola PNG è *fuori formato*: grande quanto il rilievo, con scritte e linee ingrandite in proporzione e i dpi nei metadati calibrati sulle misure reali (importando il PNG in CAD a 1:1 le dimensioni sono quelle vere). Per tenere i file gestibili i dpi scendono, fino a circa 1 pixel per millimetro.
- Il PDF della pianta d'interno, quando la scala scelta non entra in A0, usa la scala standard più grande che entra (indicata nel cartiglio). Il DXF è sempre in metri, 1:1.
- Ogni tavola ha cornice, cartiglio di studio, barra di scala e quote. Le quote sono riferite al piano 0.

### File prodotti

Gli elaborati vengono salvati accanto alla nuvola, nella sottocartella `Tracciato\`, dentro una cartella per ogni elaborazione con data e ora (`AAAA-MM-GG_HH-MM`, es. `2026-11-25_20-44`). Puoi cambiare la cartella base in *Cartella di destinazione*.

| File | Contenuto |
|---|---|
| `<nuvola>_esterno_<data-ora>_sagoma.png` · `_perimetro.dxf` | sagoma planimetrica; perimetro su layer PERIMETRO |
| `…_prospetto-1.png` … | un PNG per ogni prospetto |
| `…_multipiano_<data-ora>_pianta-0.png` … | una pianta per livello |
| `…_sezione-A.png` … | una sezione per linea |
| `<nuvola>_pianta_<data-ora>.dxf/.pdf/.png` | pianta d'interno (come nella 0.1) |
| `….log`, `….json` | log e riepilogo della singola elaborazione: parametri, linee, quote, tempi |

Lo **Storico** elenca le ultime 100 elaborazioni, con il tipo di rilievo. **Riusa** riapre la nuvola dalla cache con lo stesso tipo, le stesse linee e gli stessi livelli.

## Log e diagnostica

Il pulsante **Apri cartella log** apre `%LOCALAPPDATA%\Tracciato\logs\`:

| File | Cosa |
|---|---|
| `tracciato.log` | log applicativo a rotazione (5 × 2 MB): avvio con RAM e disco, file scelti, anteprime, elaborazioni con tipo e parametri, errori con traceback. Ogni riga riporta **utente@PC** |
| `crash.log` / `crash-elaborazione.log` | crash nativi (faulthandler) dell'interfaccia o del processo di elaborazione/anteprima |
| `output.log` | stdout/stderr delle librerie |
| `%LOCALAPPDATA%\Tracciato\cache\` | anteprime delle ultime 12 nuvole aperte (si possono cancellare) |

Anteprima ed elaborazione girano in **processi separati**. Se una libreria nativa va in crash (memoria esaurita, file danneggiato), l'interfaccia resta aperta e mostra il codice d'uscita. Gli E57 vengono letti con il parser integrato, come dalla 0.1.2.

## Creare l'eseguibile (Windows)

Requisiti: **Python 3.12 64 bit** da python.org. WebView2 (con WebGL per il viewer) è già incluso in Windows 10/11.

```bat
build.bat
```

Lo script:
1. crea `.venv`;
2. installa le dipendenze;
3. esegue il **selftest**: tre nuvole sintetiche, cioè l'appartamento, un edificio 12 × 9 m visto da fuori e un interno su due livelli con scala e soppalco;
4. crea `dist\AD-Tracciato-Studio-Pro\`.

Per distribuirla copia l'intera cartella, anche sul NAS. Non servono dipendenze sul PC di destinazione.

| Eseguibile | Uso |
|---|---|
| `AD-Tracciato-Studio-Pro.exe` | app con interfaccia |
| `AD-Tracciato-Studio-Pro-CLI.exe --selftest` | verifica l'installazione su un PC nuovo (i 3 tipi) |
| `AD-Tracciato-Studio-Pro-CLI.exe --cli rilievo.e57 --scale 50 --cut 1.2` | pianta d'interno senza interfaccia |
| `AD-Tracciato-Studio-Pro-CLI.exe --scene esterno rilievo.las [--params p.json] [--out D:\Tavole]` | elaborati per tipo. Senza `--params` usa piano 0, livelli e linee proposti dall'anteprima |

Per lo sviluppo: `avvia_sviluppo.bat` apre l'app da sorgente con gli strumenti sviluppatore.

## Struttura

```
desktop-lite-app/
├─ run_tracciato.py
├─ tracciato/
│  ├─ __main__.py            interfaccia, --cli, --scene, --selftest
│  ├─ app.py                 finestra pywebview + API: anteprima, elaborazione per tipo, storico
│  ├─ core/
│  │  ├─ engine.py           motore della pianta d'interno (invariato)
│  │  ├─ pipeline.py         orchestrazione pianta d'interno (invariata)
│  │  ├─ preview.py          anteprima: campione 16 bit, quote orizzontali, piano 0, livelli, tipo, fronti, cache
│  │  ├─ scene.py            esterno, multi-piano, sezioni/prospetti da linea + spessore (una passata a blocchi)
│  │  ├─ render.py           tavole PNG: cartiglio, scala, quote, tracce di sezione
│  │  ├─ worker.py           processi separati: intestazione, anteprima, elaborazioni
│  │  ├─ logs.py · store.py  log e storico/impostazioni
│  │  └─ testcloud.py · testscenes.py   nuvole sintetiche per il selftest
│  └─ ui/                    index.html, styles.css, fonts.css, viewer.js (WebGL), app.js
```

**Scelte tecniche**
- **Viewer 3D:** WebGL 1 scritto a mano in `viewer.js`, senza librerie né CDN, quindi funziona offline. Le coordinate sono traslate all'origine, così i float32 restano precisi anche con coordinate UTM. Le evidenziazioni (fasce di taglio, linee) sono calcolate nello shader, per cui il feedback è immediato mentre muovi gli slider.
- **Una passata, memoria costante:** ogni blocco di punti viene proiettato direttamente sulle griglie degli elaborati (conteggi, quota massima, profondità minima). La RAM dipende dalla dimensione delle tavole, non dai punti. Oltre 36 Mpixel per elaborato la risoluzione si adatta.
- **Perimetro esterno:** si usano solo i punti che salgono in verticale (facciate) alla quota scelta, così il terreno in pendenza non entra. Le facciate interrotte vengono chiuse con segmenti allineati ai muri, poi il perimetro viene vettorializzato e raddrizzato.

## Versioni

- **0.2.0**
  - Anteprima 3D della nuvola (3D / pianta / laterale), con cache.
  - Scelta del tipo di rilievo con proposta automatica.
  - Parametri per tipo, con evidenziazione nella nuvola.
  - Linee di sezione e prospetto disegnabili, con spessore, profondità e verso di vista.
  - Nuovi elaborati PNG: sagoma e perimetro (anche DXF), prospetti, piante per livello, sezioni.
  - CLI `--scene`; selftest esteso ai 3 tipi.
- **0.1.5 … 0.1.0**: pianta d'interno (vedi storico precedente). Lettura E57 a blocchi con parser integrato, processi separati, log di crash.

## Limiti noti

- **Anteprima di E57 molto grandi:** il file viene letto per intero (parser integrato, puro Python). Su file da diversi GB può richiedere alcuni minuti la prima volta; poi c'è la cache.
- **Proposte automatiche** (piano 0, livelli, tipo, fronti): sono un punto di partenza. Su terreni in forte pendenza o con molta vegetazione vanno verificate.
- **Prospetti:** sono ortofoto della nuvola in scala di grigi per profondità, non disegni vettoriali.
- **Sezioni da facciata:** se lo scanner ha visto l'edificio solo da fuori, in sezione compaiono solo le facce esterne.
- **Pianta d'interno:** valgono i limiti della 0.1 (verso delle porte, muri obliqui, controsoffitti).
- **Precisione:** sagoma ±5 cm sul test sintetico (12 × 9 m → 109,05 m² contro 108). Va verificata sui rilievi reali dello studio.

/* Tracciato — logica dell'interfaccia. Parla con Python tramite window.pywebview.api
 *
 * Passaggi: 1 nuvola e anteprima → 2 tipo di rilievo → 3 parametri del tipo → 4 elaborati PNG
 */
(() => {
  const $ = (id) => document.getElementById(id);
  const fmtNum = (n, d = 2) => Number(n).toLocaleString("it-IT", { minimumFractionDigits: d, maximumFractionDigits: d });
  const fmtSize = (b) => b > 1e9 ? `${fmtNum(b / 1e9, 1)} GB` : `${fmtNum(b / 1e6, 1)} MB`;
  const fmtPts = (p) => p == null ? "n.d." : p >= 1e6 ? `${fmtNum(p / 1e6, 1)} M` : p.toLocaleString("it-IT");
  const fmtSec = (s) => s == null ? "" : s < 60 ? `${fmtNum(s, 1)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
  const fmtQ = (z) => `${z > 0.004 ? "+" : z < -0.004 ? "−" : "±"}${fmtNum(Math.abs(z))}`;
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const base = (p) => String(p || "").split(/[\\/]/).pop();
  const dirOf = (p) => String(p || "").replace(/[\\/][^\\/]*$/, "");
  const num = (v, d) => { const x = parseFloat(String(v).replace(",", ".")); return Number.isFinite(x) ? x : d; };

  const ICON_OK = '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="#1E7A46" stroke-width="2" aria-hidden="true"><path d="M5 12l5 5 9-10"/></svg>';
  const ICON_ERR = '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="#B42318" stroke-width="2" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';
  const ICON_SKIP = '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="#9A9893" stroke-width="2" aria-hidden="true"><path d="M5 12h14"/></svg>';
  const MODE_SHORT = { interno: "Interno", esterno: "Esterno", multipiano: "Multi-piano" };

  const S = {
    api: null, file: null, prev: null, mode: null, step: 1, maxStep: 1,
    lines: [], levels: [], z0: 0, scale: 100, outDir: "", busy: false, previewing: false,
    last: null, modes: {}, sceneSaved: {}, seq: 1, viewer: null,
  };

  // ------------------------------------------------------------------ navigazione
  document.querySelectorAll(".nav button").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));
  function show(view) {
    document.querySelectorAll(".nav button").forEach((b) => {
      if (b.dataset.view === view) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    });
    ["elab", "history", "logs"].forEach((v) => ($("view-" + v).hidden = v !== view));
    if (view === "history") loadHistory();
    if (view === "logs") loadAppLog();
    if (view === "elab" && S.viewer) S.viewer.resize();
  }

  // ------------------------------------------------------------------ toast
  let toastTimer;
  function toast(msg, err = false) {
    $("toastMsg").textContent = msg;
    $("toast").className = "toast" + (err ? " err" : "");
    $("toast").hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => ($("toast").hidden = true), err ? 9000 : 4000);
  }
  $("toastClose").onclick = () => ($("toast").hidden = true);
  function status(kind, text) { $("status").className = "pill " + kind; $("status").textContent = text; }

  // ------------------------------------------------------------------ passaggi
  function goStep(n) {
    if (n > S.maxStep) return;
    S.step = n;
    document.querySelectorAll(".step-pane").forEach((p) => (p.hidden = +p.dataset.pane !== n));
    document.querySelectorAll("#stepper button").forEach((b) => {
      const k = +b.dataset.step;
      b.disabled = k > S.maxStep || (S.busy && k !== n);
      b.setAttribute("aria-current", k === n ? "step" : "false");
      b.parentElement.dataset.state = k < n ? "done" : k === n ? "current" : "todo";
    });
    if (n === 3) renderParams();
    if (n !== 4) tab("cloud");
    updateViewer();
    $("btnRun").disabled = !(S.file && S.mode && S.maxStep >= 3) || S.busy;
  }
  document.querySelectorAll("#stepper button").forEach((b) => b.addEventListener("click", () => goStep(+b.dataset.step)));
  $("toStep2").onclick = () => { S.maxStep = Math.max(S.maxStep, 2); goStep(2); };
  $("toStep3").onclick = () => { S.maxStep = Math.max(S.maxStep, 3); goStep(3); };
  $("btnChangeMode").onclick = () => goStep(2);
  $("btnBackParams").onclick = () => goStep(3);

  // ------------------------------------------------------------------ viewer
  function initViewer() {
    try {
      S.viewer = new window.TracciatoViewer($("gl"), $("ov"));
      if (S.viewer.failed) throw new Error("WebGL non disponibile");
      // linee già disegnate: trascinate nella vista in pianta (estremi liberi, tratto in ortogonale)
      S.viewer.onEdit = (l, final) => {
        const row = document.querySelector(`.line-row[data-id="${l.id}"] .lmeta`);
        if (row) row.textContent = `${l.kind === "sezione" ? "Sezione" : "Prospetto"} · ${fmtNum(lineLen(l))} m`;
        if (final) { renderLines(); updateViewer(); }
      };
    } catch (e) {
      S.viewer = null;
      $("vHint").textContent = "Anteprima 3D non disponibile su questo PC (WebGL): puoi comunque scegliere tipo e parametri.";
    }
  }
  $("vMode").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b || !S.viewer) return;
    setViewMode(b.dataset.v);
  });
  function setViewMode(m) {
    $("vMode").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x.dataset.v === m));
    S.viewer && S.viewer.setMode(m);
    hintDefault();
  }
  function hintDefault() {
    const m = S.viewer ? S.viewer.mode : "3d";
    $("vHint").textContent = m === "top" ? "Pianta · trascina per spostare · Alt+trascina per ruotare · rotella per lo zoom · trascina una linea per spostarla, un estremo per modificarla"
      : m === "side" ? "Laterale · trascina per girare attorno · tasto destro per spostare · le linee sono le quote"
      : "Trascina per ruotare · tasto destro o Maiusc per spostare · rotella per lo zoom";
  }
  $("vColor").onchange = () => S.viewer && S.viewer.setColor(+$("vColor").value);
  $("vSize").oninput = () => S.viewer && S.viewer.setSize(+$("vSize").value);
  $("vFit").onclick = () => S.viewer && S.viewer.fit();

  function tab(which) {
    document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === which));
    $("viewerBox").hidden = which !== "cloud"; $("sheetsBox").hidden = which !== "sheets";
    $("viewTools").style.visibility = which === "cloud" ? "visible" : "hidden";
    if (which === "cloud" && S.viewer) S.viewer.resize();
  }
  document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => tab(b.dataset.tab)));

  // evidenziazioni nella nuvola: cosa verrà tagliato con i parametri attuali
  function updateViewer() {
    const v = S.viewer; if (!v || !S.prev) return;
    v.setPlaneZ(S.z0);
    const showParams = S.step >= 3 && S.mode;
    const bands = [], levels = [{ z: S.z0, label: "±0,00 piano 0", color: "#111" }];
    if (showParams) {
      if (S.mode === "interno") {
        const c = num($("pCut").value, 1.1);
        bands.push({ z0: S.z0 + c - 0.05, z1: S.z0 + c + 0.05 });
        levels.push({ z: S.z0 + c, label: `${fmtQ(c)} taglio pianta (pav. stimato)`, color: "#1F4FA3", dash: true });
      } else if (S.mode === "esterno") {
        const h = num($("pCutH").value, 1), t = num($("pCutT").value, 0.3);
        bands.push({ z0: S.z0 + h - t / 2, z1: S.z0 + h + t / 2 });
        levels.push({ z: S.z0 + h, label: `${fmtQ(h)} sezione perimetro`, color: "#1F4FA3", dash: true });
      } else {
        S.levels.forEach((lv) => {
          bands.push({ z0: S.z0 + lv.z + lv.cut - lv.t / 2, z1: S.z0 + lv.z + lv.cut + lv.t / 2 });
          levels.push({ z: S.z0 + lv.z, label: `${fmtQ(lv.z)} ${lv.name}`, color: "#111" });
          levels.push({ z: S.z0 + lv.z + lv.cut, label: `${fmtQ(lv.z + lv.cut)} taglio ${lv.name}`, color: "#1F4FA3", dash: true });
        });
      }
    }
    v.setBands(bands);
    v.setLevels(levels);
    v.setLines(showParams ? visibleLines() : []);
  }
  function visibleLines() {
    return S.lines.filter((l) => l.kind === "sezione" || S.mode === "esterno");
  }

  // ------------------------------------------------------------------ file e anteprima
  async function pick() {
    if (S.busy || S.previewing) return;
    const r = await S.api.pick_input();
    if (!r || !r.path) return;
    await loadPreview(r.path);
  }
  async function loadPreview(path, force = false) {
    S.file = { path, name: base(path) };
    S.prev = null; S.mode = null; S.maxStep = 1; S.last = null;
    $("title").textContent = base(path);
    $("fileMeta").textContent = "Lettura del file…";
    $("fName").textContent = base(path); $("fInfo").textContent = dirOf(path);
    $("prevFacts").hidden = true; $("toStep2").disabled = true; $("btnRepreview").hidden = true;
    $("empty").hidden = true;
    S.viewer && S.viewer.clear();
    resetPipeline();
    goStep(1);
    const r = await S.api.load_preview(path, null, force);
    if (!r.ok) { toast(r.error, true); return; }
  }
  function onPreviewStart() {
    S.previewing = true;
    $("prevProgress").hidden = false; $("prevBar").style.width = "0%"; $("prevPct").textContent = "0%";
    $("btnPick").disabled = $("btnPick2").disabled = true;
    $("running").hidden = false; $("runningLabel").textContent = "ANTEPRIMA DELLA NUVOLA";
    $("btnCancel").hidden = false; $("btnRun").hidden = true; $("btnCancel").disabled = false;
    status("run", "Anteprima");
  }
  function onPreviewProgress(v) {
    $("prevBar").style.width = `${Math.round(v * 100)}%`; $("prevPct").textContent = `${Math.round(v * 100)}%`;
  }
  async function onPreviewDone(e) {
    S.previewing = false;
    $("prevProgress").hidden = true; $("running").hidden = true;
    $("btnPick").disabled = $("btnPick2").disabled = false;
    $("btnCancel").hidden = true; $("btnRun").hidden = false;
    if (!e.ok) {
      status(e.cancelled ? "wait" : "err", e.cancelled ? "Annullato" : "Errore");
      $("fileMeta").textContent = e.error; $("empty").hidden = false;
      if (!e.cancelled) toast(e.error, true);
      return;
    }
    const prev = await S.api.get_preview();
    if (!prev) { toast("Anteprima non disponibile", true); return; }
    setPreview(prev);
    status("ok", e.from_cache ? "Anteprima (cache)" : `Anteprima in ${fmtSec(e.seconds)}`);
    if (S.pendingReuse) { const r = S.pendingReuse; S.pendingReuse = null; applyReuse(r); }
  }
  function setPreview(prev) {
    S.prev = prev;
    const info = prev.info || {};
    S.file = { ...info, path: prev.path, name: base(prev.path) };
    const bin = atob(prev.data), u8 = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
    const q = new Uint16Array(u8.buffer);
    if (S.viewer) { S.viewer.setCloud(q, prev.bbox, prev.angle || 0); setViewMode("3d"); }
    const b = prev.bbox, sug = prev.suggest;
    $("title").textContent = S.file.name;
    $("fileMeta").textContent = `${info.format || ""} · ${info.size ? fmtSize(info.size) : ""} · ${fmtPts(prev.points_file)} punti${info.scans ? ` · ${info.scans} scansioni` : ""}`;
    $("fName").textContent = S.file.name; $("fInfo").textContent = dirOf(prev.path);
    $("fPts").textContent = fmtPts(prev.points_file); $("fPrev").textContent = fmtPts(prev.points_preview);
    const g = prev.guides || {};
    $("fSize").textContent = g.size ? `${fmtNum(g.size[0])} × ${fmtNum(g.size[1])} m` : `${fmtNum(b[3] - b[0])} × ${fmtNum(b[4] - b[1])} m`;
    $("fH").textContent = `${fmtNum(b[5] - b[2])} m`;
    $("fRot").textContent = `${fmtNum(prev.angle || 0, 1)}°`;
    $("fPeaks").textContent = (prev.peaks || []).filter((p) => p.w >= 0.15).map((p) => fmtQ(p.z - sug.z0)).join(" · ") || "—";
    $("prevFacts").hidden = false; $("btnRepreview").hidden = false;
    $("toStep2").disabled = false;
    if (!S.outDir) $("outDir").textContent = info.default_output || dirOf(prev.path) + "\\Tracciato";

    // proposte
    S.z0 = sug.z0;
    S.levels = (sug.levels || [0]).map((z, i) => ({ name: i === 0 ? "Piano terra" : `Livello ${i}`, z, cut: 1.1, t: 0.1 }));
    S.lines = [];
    (g.fronts || []).forEach((f) => S.lines.push(mkLine(f, true)));
    if (g.section) S.lines.push(mkLine(g.section, true));
    document.querySelectorAll(".choice").forEach((c) => {
      c.querySelector(".tag").hidden = c.dataset.mode !== sug.mode;
      c.setAttribute("aria-checked", "false");
    });
    selectMode(sug.mode, false);
    S.maxStep = 2;
    goStep(1);
  }
  function mkLine(d, active) {
    return { id: S.seq++, label: d.label, kind: d.kind, p1: d.p1.slice(0, 2), p2: d.p2.slice(0, 2),
             t: d.t ?? (d.kind === "sezione" ? 0.1 : 5), beyond: d.beyond ?? (d.kind === "sezione" ? 2 : 0), active };
  }
  $("btnPick").onclick = pick; $("btnPick2").onclick = pick; $("btnPick3").onclick = pick;
  $("btnRepreview").onclick = () => S.file && loadPreview(S.file.path, true);
  $("btnOut").onclick = async () => { const d = await S.api.pick_output_dir(); if (d) { S.outDir = d; $("outDir").textContent = d; } };
  $("btnOutReset").onclick = () => { S.outDir = ""; $("outDir").textContent = (S.file && S.file.default_output) || "accanto alla nuvola, in “Tracciato”"; };

  // ------------------------------------------------------------------ tipo di rilievo
  $("modeChoices").addEventListener("click", (e) => {
    const c = e.target.closest(".choice"); if (!c) return;
    selectMode(c.dataset.mode, true);
  });
  function selectMode(m, user) {
    S.mode = m;
    document.querySelectorAll(".choice").forEach((c) => c.setAttribute("aria-checked", c.dataset.mode === m));
    $("toStep3").disabled = !m;
    const saved = S.sceneSaved[m] || {};
    if (saved.title) $("pTitle").value = saved.title;
    else $("pTitle").value = m === "interno" ? "RILIEVO — PIANTA" : m === "esterno" ? "RILIEVO ESTERNO" : "RILIEVO";
    if (saved.detail) $("pDetail").value = saved.detail; else $("pDetail").value = m === "interno" ? "alta" : "media";
    if (saved.scale) setScale(saved.scale);
    if (saved.cut_h != null) { $("pCutH").value = saved.cut_h; }
    if (saved.cut_t != null) { $("pCutT").value = saved.cut_t; }
    if (saved.fill_walls != null) $("pFill").checked = saved.fill_walls;
    $("paramsTitle").textContent = `3 · Parametri · ${MODE_SHORT[m]}`;
    if (user) { S.maxStep = Math.max(S.maxStep, 3); }
    $("btnRun").disabled = !(S.file && S.mode && S.maxStep >= 3) || S.busy;
  }

  // ------------------------------------------------------------------ parametri
  function setScale(v) {
    S.scale = +v;
    $("pScale").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", +x.dataset.v === S.scale));
  }
  $("pScale").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setScale(b.dataset.v); });
  $("pCut").addEventListener("input", () => { $("vCut").textContent = `${fmtNum($("pCut").value)} m`; updateViewer(); });
  $("pCutH").addEventListener("input", () => { $("vCutH").textContent = `${fmtNum($("pCutH").value)} m`; updateViewer(); });
  $("pCutT").addEventListener("input", updateViewer);
  $("pZ0").addEventListener("input", () => { S.z0 = num($("pZ0").value, S.z0); z0Rel(); updateViewer(); });
  function z0Rel() {
    const b = S.prev && S.prev.bbox;
    $("vZ0rel").textContent = b ? `${fmtNum(S.z0 - b[2])} m sopra il punto più basso` : "";
    $("z0Chips").querySelectorAll("button").forEach((c) => c.setAttribute("aria-pressed", Math.abs(+c.dataset.z - S.z0) < 0.005));
  }

  function renderParams() {
    const m = S.mode;
    document.querySelectorAll("[data-for]").forEach((el) => (el.hidden = !el.dataset.for.split(" ").includes(m)));
    $("pZ0").value = S.z0.toFixed(3);
    const peaks = (S.prev && S.prev.peaks || []).filter((p) => p.w >= 0.12);
    $("z0Chips").innerHTML = peaks.map((p) =>
      `<button type="button" class="chip" data-z="${p.z}" title="peso ${fmtNum(p.w * 100, 0)}%">${fmtNum(p.z, 2)}</button>`).join("");
    z0Rel();
    $("vCutH").textContent = `${fmtNum($("pCutH").value)} m`;
    renderLevels(); renderLines();
  }
  $("z0Chips").addEventListener("click", (e) => {
    const c = e.target.closest(".chip"); if (!c) return;
    S.z0 = +c.dataset.z; $("pZ0").value = S.z0.toFixed(3); z0Rel(); updateViewer();
  });

  // livelli (multi-piano)
  function renderLevels() {
    $("levels").innerHTML = `<div class="lv-head"><span>Nome</span><span>Pav. (m)</span><span>Taglio</span><span>Sp.</span><span></span></div>` +
      S.levels.map((lv, i) => `<div class="lv-row" data-i="${i}">
        <input class="input" data-k="name" value="${esc(lv.name)}" aria-label="Nome livello">
        <input class="input mono" data-k="z" type="number" step="0.01" value="${lv.z}" aria-label="Quota pavimento dal piano 0">
        <input class="input mono" data-k="cut" type="number" step="0.05" min="0.1" max="4" value="${lv.cut}" aria-label="Altezza di sezione">
        <input class="input mono" data-k="t" type="number" step="0.01" min="0.01" max="1" value="${lv.t}" aria-label="Spessore fascia">
        <button type="button" class="icon-btn" data-del="${i}" aria-label="Elimina livello" title="Elimina">×</button></div>`).join("");
  }
  $("levels").addEventListener("input", (e) => {
    const row = e.target.closest(".lv-row"); if (!row) return;
    const lv = S.levels[+row.dataset.i], k = e.target.dataset.k;
    lv[k] = k === "name" ? e.target.value : num(e.target.value, lv[k]);
    updateViewer();
  });
  $("levels").addEventListener("click", (e) => {
    const b = e.target.closest("[data-del]"); if (!b) return;
    S.levels.splice(+b.dataset.del, 1); renderLevels(); updateViewer();
  });
  $("btnLevelAdd").onclick = () => {
    const last = S.levels[S.levels.length - 1];
    S.levels.push({ name: `Livello ${S.levels.length}`, z: last ? +(last.z + 3).toFixed(2) : 0, cut: 1.1, t: 0.1 });
    renderLevels(); updateViewer();
  };
  $("btnLevelsAuto").onclick = () => {
    const sug = S.prev.suggest;
    S.levels = sug.levels.map((z, i) => ({ name: i === 0 ? "Piano terra" : `Livello ${i}`, z, cut: 1.1, t: 0.1 }));
    renderLevels(); updateViewer();
  };

  // linee di sezione / prospetto
  function lineLen(l) { return Math.hypot(l.p2[0] - l.p1[0], l.p2[1] - l.p1[1]); }
  function renderLines() {
    for (const kind of ["prospetto", "sezione"]) {
      const box = $(kind === "sezione" ? "linesSezione" : "linesProspetto");
      const ls = S.lines.filter((l) => l.kind === kind);
      box.innerHTML = ls.length ? ls.map((l) => `<div class="line-row${l.active ? "" : " off"}" data-id="${l.id}">
          <input type="checkbox" data-k="active" ${l.active ? "checked" : ""} aria-label="Includi ${esc(l.label)}">
          <input class="input badge-in ${kind}" data-k="label" value="${esc(l.label)}" maxlength="4" aria-label="Nome">
          <span class="lmeta">${kind === "sezione" ? "Sezione" : "Prospetto"} · ${fmtNum(lineLen(l))} m</span>
          <button type="button" class="icon-btn" data-act="flip" title="Inverti il verso di vista" aria-label="Inverti verso">⇄</button>
          <button type="button" class="icon-btn" data-act="del" title="Elimina" aria-label="Elimina">×</button>
          <div class="lparams">
            <span class="mini-f">sposta
              <button type="button" class="icon-btn" data-act="nudge-" title="Sposta di 10 cm all'indietro (ortogonale alla linea, contro la freccia di vista)" aria-label="Sposta indietro">↓</button>
              <button type="button" class="icon-btn" data-act="nudge+" title="Sposta di 10 cm in avanti (ortogonale alla linea, verso la freccia di vista)" aria-label="Sposta avanti">↑</button></span>
            <label class="mini-f">${kind === "sezione" ? "spessore m" : "profondità m"}
              <input class="input mono" data-k="t" type="number" step="${kind === "sezione" ? 0.01 : 0.5}" value="${l.t}"></label>
            ${kind === "sezione" ? `<label class="mini-f">oltre m
              <input class="input mono" data-k="beyond" type="number" step="0.5" min="0" value="${l.beyond}"></label>` : ""}
          </div>
        </div>`).join("") : `<div class="muted small-note">${kind === "sezione" ? "Nessuna sezione: premi “Disegna” e fai due clic sulla nuvola." : "Nessun prospetto."}</div>`;
    }
  }
  function lineOf(e) { const r = e.target.closest(".line-row"); return r && S.lines.find((l) => l.id === +r.dataset.id); }
  for (const id of ["linesSezione", "linesProspetto"]) {
    const box = $(id);
    box.addEventListener("input", (e) => {
      const l = lineOf(e); if (!l) return;
      const k = e.target.dataset.k;
      if (k === "active") { l.active = e.target.checked; e.target.closest(".line-row").classList.toggle("off", !l.active); }
      else if (k === "label") l.label = e.target.value.trim() || l.label;
      else l[k] = Math.max(0, num(e.target.value, l[k]));
      updateViewer();
    });
    box.addEventListener("click", (e) => {
      const b = e.target.closest("[data-act]"); if (!b) return;
      const l = lineOf(e); if (!l) return;
      if (b.dataset.act === "flip") { const t = l.p1; l.p1 = l.p2; l.p2 = t; }
      else if (b.dataset.act.startsWith("nudge")) {
        // 10 cm in ortogonale alla linea: + = verso la freccia di vista (normale sinistra p1→p2)
        const L = lineLen(l) || 1, k = (b.dataset.act === "nudge+" ? 0.1 : -0.1) / L;
        const nx = -(l.p2[1] - l.p1[1]) * k, ny = (l.p2[0] - l.p1[0]) * k;
        l.p1 = [l.p1[0] + nx, l.p1[1] + ny]; l.p2 = [l.p2[0] + nx, l.p2[1] + ny];
      }
      else S.lines = S.lines.filter((x) => x !== l);
      renderLines(); updateViewer();
    });
    box.addEventListener("mouseover", (e) => { const l = lineOf(e); S.lines.forEach((x) => (x.focus = x === l)); S.viewer && S.viewer.render(); });
    box.addEventListener("mouseleave", () => { S.lines.forEach((x) => (x.focus = false)); S.viewer && S.viewer.render(); });
  }
  $("btnFrontsAuto").onclick = () => {
    S.lines = S.lines.filter((l) => l.kind !== "prospetto");
    ((S.prev.guides || {}).fronts || []).forEach((f) => S.lines.push(mkLine(f, true)));
    renderLines(); updateViewer();
  };
  function nextLabel(kind) {
    const used = new Set(S.lines.filter((l) => l.kind === kind).map((l) => l.label));
    if (kind === "sezione") { for (let i = 0; i < 26; i++) { const c = String.fromCharCode(65 + i); if (!used.has(c)) return c; } }
    for (let i = 1; i < 99; i++) if (!used.has(String(i))) return String(i);
    return "X";
  }
  document.querySelectorAll("[data-draw]").forEach((b) => b.addEventListener("click", () => {
    if (!S.viewer || !S.prev) return;
    const kind = b.dataset.draw;
    tab("cloud");
    if (S.viewer.mode === "side") setViewMode("top");
    $("vHint").textContent = `${kind === "sezione" ? "Sezione" : "Prospetto"}: clic sul primo punto, poi sul secondo · Maiusc = parallela ai muri · Esc annulla`;
    $("vHint").classList.add("active");
    S.viewer.startDraw(S.z0, (p1, p2) => {
      $("vHint").classList.remove("active"); hintDefault();
      if (!p1) return;
      const l = mkLine({ label: nextLabel(kind), kind, p1, p2 }, true);
      if (kind === "prospetto") l.t = 5;
      S.lines.push(l); renderLines(); updateViewer();
    });
  }));

  function readPlanParams() {
    const n = (id) => ($(id).value === "" ? null : +$(id).value);
    return {
      cut: +$("pCut").value, scale: Math.min(S.scale, 200), detail: $("pDetail").value, title: $("pTitle").value.trim(),
      align: $("pAlign").checked, section_points: $("pSection").checked, inner_dims: $("pInnerDims").checked,
      max_wall: +$("pWall").value || 0.55, min_room: +$("pRoom").value || 1, high_band: +$("pBand").value || 0.35,
      floor_z: n("pFloor"), ceil_z: n("pCeil"),
    };
  }
  function applyPlanParams(p) {
    if (!p) return;
    $("pCut").value = p.cut ?? 1.1; $("vCut").textContent = `${fmtNum($("pCut").value)} m`;
    $("pAlign").checked = p.align ?? true; $("pSection").checked = p.section_points ?? true; $("pInnerDims").checked = p.inner_dims ?? true;
    $("pWall").value = p.max_wall ?? 0.55; $("pRoom").value = p.min_room ?? 1; $("pBand").value = p.high_band ?? 0.35;
    $("pFloor").value = p.floor_z ?? ""; $("pCeil").value = p.ceil_z ?? "";
  }
  function buildParams() {
    const m = S.mode;
    const lines = S.lines.filter((l) => l.active && (l.kind === "sezione" || m === "esterno"))
      .map((l) => ({ label: l.label, kind: l.kind, p1: l.p1, p2: l.p2, t: l.t, beyond: l.beyond }));
    const scene = {
      title: $("pTitle").value.trim() || "RILIEVO", detail: $("pDetail").value, scale: S.scale,
      z0: S.z0, bbox: S.prev.bbox, angle: S.prev.angle || 0, align: m === "interno" ? $("pAlign").checked : true,
      fill_walls: $("pFill").checked, cut_h: num($("pCutH").value, 1), cut_t: num($("pCutT").value, 0.3),
      levels: m === "multipiano" ? S.levels.map((l) => ({ ...l })) : [], lines,
    };
    return { scene, plan: m === "interno" ? readPlanParams() : null };
  }

  // ------------------------------------------------------------------ elaborazione
  function resetPipeline() {
    $("steps").innerHTML = `<li class="muted" style="font-size:13px">Le fasi compaiono all'avvio dell'elaborazione.</li>`;
    $("log").textContent = ""; $("total").textContent = "";
  }
  function renderSteps(stages) {
    $("steps").innerHTML = stages.map(([k, label]) =>
      `<li data-k="${k}" data-s="pending"><div class="row"><span class="name"><span class="ico pending"></span>${esc(label)}</span><span class="t"></span></div>` +
      (["read", "levels", "bounds"].includes(k) ? `<div class="bar" hidden><i></i></div>` : "") + `</li>`).join("");
  }
  function setStage(k, st, seconds) {
    const li = $("steps").querySelector(`li[data-k="${k}"]`); if (!li) return;
    li.dataset.s = st;
    const ico = li.querySelector(".ico");
    const tmp = document.createElement("span");
    tmp.innerHTML = st === "done" ? ICON_OK : st === "error" ? ICON_ERR : st === "skipped" || st === "cancelled" ? ICON_SKIP : `<span class="ico ${st}"></span>`;
    ico.replaceWith(tmp.firstElementChild);
    if (seconds != null) li.querySelector(".t").textContent = fmtSec(seconds);
    if (st === "skipped") li.querySelector(".t").textContent = "manuale";
    const bar = li.querySelector(".bar"); if (bar) bar.hidden = st !== "running";
    if (st === "running") $("runningLabel").textContent = li.querySelector(".name").textContent.toUpperCase();
  }
  function setProgress(k, v) {
    const el = $("steps").querySelector(`li[data-k="${k}"] .bar i`); if (el) el.style.width = `${Math.round(v * 100)}%`;
  }
  function logLine(level, line) {
    const box = $("log");
    const el = document.createElement("span"); el.className = level; el.textContent = line + "\n";
    box.appendChild(el);
    while (box.childNodes.length > 400) box.removeChild(box.firstChild);
    box.scrollTop = box.scrollHeight;
  }

  async function run() {
    if (!S.file || !S.prev || !S.mode || S.busy) return;
    if (S.mode === "multipiano" && !S.levels.length) { toast("Aggiungi almeno un livello", true); return; }
    resetPipeline();
    const params = buildParams();
    const r = await S.api.start_scene(S.file.path, S.outDir, S.mode, params);
    if (!r.ok) { toast(r.error, true); return; }
    S.sceneSaved[S.mode] = params.scene;
    renderSteps(r.stages);
    setBusy(true);
  }
  function setBusy(b) {
    S.busy = b;
    $("btnRun").hidden = b; $("btnCancel").hidden = !b; $("btnCancel").disabled = false;
    $("btnPick").disabled = b; $("btnPick2").disabled = b;
    document.querySelectorAll(".side-left input, .side-left select, .side-left button").forEach((x) => (x.disabled = b));
    $("running").hidden = !b;
    if (b) { status("run", "In elaborazione"); tab("cloud"); }
    goStep(S.step);
  }
  $("btnRun").onclick = run;
  $("btnCancel").onclick = async () => { $("btnCancel").disabled = true; status("wait", "Annullamento…"); await S.api.cancel(); };

  async function showResult(sum) {
    S.last = sum;
    $("total").textContent = sum.seconds != null ? `TOT ${fmtSec(sum.seconds)}` : "";
    const outs = sum.outputs || [];
    S.maxStep = 4;
    if (sum.status === "ok" || outs.length) {
      status(sum.status === "ok" ? "ok" : "err", sum.status === "ok" ? `Completato in ${fmtSec(sum.seconds)}` : "Completato con errori");
      $("resStatus").className = "pill " + (sum.status === "ok" ? "ok" : "err");
      $("resStatus").textContent = `${outs.length} tavol${outs.length === 1 ? "a" : "e"}`;
      renderStats(sum);
      $("outputs").innerHTML = outs.map((o, i) => `<li data-i="${i}">
          <button type="button" class="out-main" data-show="${i}"><span class="o-kind">${esc(o.kind.toUpperCase())}</span>
            <span class="o-label">${esc(o.label)}</span><span class="o-meta">${esc(o.paper || "")} · ${esc(base(o.png))}</span></button>
          <div class="o-acts">
            <button type="button" class="btn small" data-open="${i}" data-f="png">PNG</button>
            ${o.pdf ? `<button type="button" class="btn small" data-open="${i}" data-f="pdf">PDF</button>` : ""}
            ${o.dxf ? `<button type="button" class="btn small" data-open="${i}" data-f="dxf">DXF</button>` : ""}
          </div></li>`).join("");
      document.querySelector('.tabs button[data-tab="sheets"]').disabled = !outs.length;
      goStep(4);
      if (outs.length) await showSheet(0);
      if (sum.status !== "ok") toast(sum.error || "Errore in una parte dell'elaborazione: vedi il log", true);
    } else if (sum.status === "cancelled") {
      status("wait", "Annullato"); S.maxStep = 3; goStep(3);
    } else {
      status("err", "Errore"); S.maxStep = 3; goStep(3);
      if (sum.crash) logLine("ERROR", "— dettagli tecnici (crash-elaborazione.log) —\n" + sum.crash);
      toast(sum.error || "Errore durante l'elaborazione: vedi il log", true);
    }
  }
  function renderStats(sum) {
    let items = [];
    if (sum.mode === "interno" && sum.status === "ok") {
      items = [[(sum.rooms || []).length, "Vani"], [fmtNum(sum.area_total ?? 0), "m² utili"], [sum.doors, "Porte"],
               [sum.windows, "Finestre"], [fmtNum(sum.height ?? 0), "H netta"], [sum.paper || "—", "Formato"]];
    } else if (sum.footprint) {
      const f = sum.footprint;
      items = [[fmtNum(f.area ?? 0), "m² coperti"], [fmtNum(f.perimeter ?? 0), "Perimetro m"],
               [f.height != null ? fmtNum(f.height) : "—", "H max m"], [`${fmtNum(f.width ?? 0)} × ${fmtNum(f.depth ?? 0)}`, "Ingombro m"]];
    }
    $("resStats").innerHTML = items.map(([v, l]) => `<div class="stat"><b>${esc(v)}</b><span>${esc(l)}</span></div>`).join("");
    $("resStats").hidden = !items.length;
  }
  async function showSheet(i) {
    const o = (S.last.outputs || [])[i]; if (!o) return;
    $("outputs").querySelectorAll("li").forEach((li) => li.classList.toggle("sel", +li.dataset.i === i));
    tab("sheets");
    $("sheetImg").removeAttribute("src");
    const img = await S.api.preview(o.png);
    if (img) $("sheetImg").src = img; else toast("Tavola non trovata: " + o.png, true);
  }
  $("outputs").addEventListener("click", async (e) => {
    const s = e.target.closest("[data-show]"); if (s) return showSheet(+s.dataset.show);
    const b = e.target.closest("[data-open]"); if (!b) return;
    const o = S.last.outputs[+b.dataset.open];
    const p = b.dataset.f === "png" ? (o.sheet || o.png) : o[b.dataset.f];
    const r = await S.api.open_path(p); if (!r.ok) toast(r.error, true);
  });
  $("sheetImg").addEventListener("dblclick", async () => {
    const sel = $("outputs").querySelector("li.sel"); if (!sel) return;
    const o = S.last.outputs[+sel.dataset.i]; const r = await S.api.open_path(o.sheet || o.png); if (!r.ok) toast(r.error, true);
  });
  $("btnOpenDir").onclick = () => openOut(S.last, "dir");
  $("btnOpenLog").onclick = () => openOut(S.last, "log");
  async function openOut(sum, what) {
    if (!sum) return;
    const f = sum.files || {};
    const path = what === "dir" ? (sum.output_dir || dirOf(f.png)) : f[what];
    const r = await S.api.open_path(path);
    if (!r.ok) toast(r.error, true);
  }

  // eventi da Python
  window.tracciato = {
    onEvent(e) {
      if (e.type === "stage") setStage(e.stage, e.status, e.seconds);
      else if (e.type === "progress") setProgress(e.stage, e.value);
      else if (e.type === "log") logLine(e.level, e.line);
      else if (e.type === "preview_start") onPreviewStart();
      else if (e.type === "preview_progress") onPreviewProgress(e.value);
      else if (e.type === "preview_done") onPreviewDone(e);
      else if (e.type === "done") { setBusy(false); showResult(e.summary); }
    },
  };

  // ------------------------------------------------------------------ storico
  const STATUS = { ok: ["ok", "Completato"], error: ["err", "Errore"], cancelled: ["wait", "Annullato"] };
  async function loadHistory() {
    const st = await S.api.get_state();
    const rows = st.history || [];
    $("historyBody").innerHTML = rows.length ? rows.map((h, i) => {
      const [cls, label] = STATUS[h.status] || ["idle", h.status];
      const d = h.started ? new Date(h.started) : null;
      const when = d ? d.toLocaleString("it-IT", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";
      const mode = h.mode || "interno";
      const outs = h.outputs || [];
      let res;
      if (h.status !== "ok") res = `<span class="sub">${esc(h.error || "—")}</span>`;
      else if (mode === "interno" && h.area_total != null) res = `${(h.rooms || []).length} vani · ${fmtNum(h.area_total)} m²<div class="sub">${h.doors} P · ${h.windows} F · ${fmtSec(h.seconds)}</div>`;
      else res = `${outs.length} tavole${h.footprint ? ` · sagoma ${fmtNum(h.footprint.area || 0)} m²` : ""}<div class="sub">${fmtSec(h.seconds)}</div>`;
      const ok = h.status === "ok";
      return `<tr><td class="sub">${when}</td><td><div class="file">${esc(base(h.input))}</div><div class="sub">${esc(dirOf(h.input))}</div></td>
        <td><span class="sub">${esc(MODE_SHORT[mode] || mode)}</span></td>
        <td><span class="pill ${cls}">${label}</span></td><td>${res}</td>
        <td><div class="acts">
          ${ok && outs.length ? `<button class="btn small" data-h="${i}" data-a="png">PNG</button>` : ""}
          ${ok && h.files && h.files.pdf ? `<button class="btn small" data-h="${i}" data-a="pdf">PDF</button>` : ""}
          <button class="btn small" data-h="${i}" data-a="dir">Cartella</button>
          <button class="btn small link" data-h="${i}" data-a="reuse">Riusa</button></div></td></tr>`;
    }).join("") : `<tr><td colspan="6" class="muted" style="padding:32px 16px">Nessuna elaborazione ancora.</td></tr>`;
    $("historyBody").onclick = async (e) => {
      const b = e.target.closest("button[data-h]"); if (!b) return;
      const h = rows[+b.dataset.h];
      if (b.dataset.a === "reuse") {
        show("elab");
        S.pendingReuse = h;
        await loadPreview(h.input);
      } else if (b.dataset.a === "png") {
        const o = h.outputs[0]; const r = await S.api.open_path(o.sheet || o.png); if (!r.ok) toast(r.error, true);
      } else openOut(h, b.dataset.a);
    };
  }
  function applyReuse(h) {
    const mode = h.mode || "interno";
    const p = h.params || {};
    selectMode(mode, true);
    const sp = p.scene || (mode === "interno" ? null : p);
    if (mode === "interno") applyPlanParams(p.plan || p);
    if (sp) {
      if (sp.z0 != null) S.z0 = sp.z0;
      if (sp.title) $("pTitle").value = sp.title;
      if (sp.detail) $("pDetail").value = sp.detail;
      if (sp.scale) setScale(sp.scale);
      if (sp.cut_h != null) $("pCutH").value = sp.cut_h;
      if (sp.cut_t != null) $("pCutT").value = sp.cut_t;
      if (sp.levels && sp.levels.length) S.levels = sp.levels.map((l) => ({ ...l }));
      if (sp.lines) S.lines = sp.lines.map((l) => mkLine(l, true));
    }
    S.maxStep = 3; goStep(3);
  }

  // ------------------------------------------------------------------ log applicativo
  async function loadAppLog() {
    const lines = await S.api.read_app_log(400);
    $("appLog").innerHTML = lines.map((l) => {
      const lvl = (l.split("|")[1] || "").trim();
      return `<span class="${esc(lvl)}">${esc(l)}</span>`;
    }).join("\n");
    $("appLog").scrollTop = $("appLog").scrollHeight;
  }
  $("btnLogRefresh").onclick = loadAppLog;
  $("btnLogOpen").onclick = () => S.api.open_logs();
  $("btnLogOpen2").onclick = () => S.api.open_logs();

  // ------------------------------------------------------------------ avvio
  async function init() {
    S.api = window.pywebview.api;
    initViewer();
    const st = await S.api.get_state();
    S.modes = st.modes || {}; S.sceneSaved = st.scene_params || {};
    $("ver").textContent = "v" + st.version;
    $("dataDir").textContent = st.data_dir;
    $("logPath").textContent = st.log_dir;
    applyPlanParams(st.params);
    setScale(100);
    if (st.output_dir) { S.outDir = st.output_dir; $("outDir").textContent = st.output_dir; }
    goStep(1);
    if (st.previous_crash) {
      toast("L'ultima sessione di Tracciato si è chiusa in modo anomalo. I dettagli sono in tracciato.log e crash.log (pulsante “Apri cartella log”).", true);
    }
  }
  if (window.pywebview && window.pywebview.api) init();
  else window.addEventListener("pywebviewready", init);
})();

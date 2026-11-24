/* Tracciato — viewer della nuvola di punti (WebGL 1, nessuna libreria esterna).
 *
 * Coordinate "locali" = coordinate del file − origine (centro XY del riquadro, Z minima): i float32
 * della GPU restano precisi anche con coordinate UTM/Gauss-Boaga a 6–7 cifre.
 *
 *   viewer.setCloud(quantizedUint16, bbox)      carica il campione dell'anteprima
 *   viewer.setMode("3d" | "top" | "side")        vista prospettica, pianta, laterale (ortogonali)
 *   viewer.setBands([{z0, z1}])                  fasce orizzontali evidenziate (quote assolute)
 *   viewer.setLines([{p1, p2, kind, t, beyond, label, active}])   linee di sezione/prospetto (assolute)
 *   viewer.setLevels([{z, label}])               linee di quota (vista laterale)
 *   viewer.startDraw(cb)                         due clic → cb(p1, p2) in coordinate assolute
 */
(() => {
  const VS = `
    attribute vec3 aPos;
    uniform mat4 uMVP;
    uniform float uSize, uZmin, uZmax;
    uniform int uColor;
    uniform vec2 uBands[6];
    uniform int uNBands;
    uniform vec4 uLA[8];   // p1.xy, u.xy
    uniform vec4 uLB[8];   // L, dmin, dmax, attiva
    uniform int uNLines;
    varying vec3 vCol;
    vec3 ramp(float t) {
      vec3 a = vec3(0.80, 0.78, 0.74), b = vec3(0.07, 0.07, 0.07);
      return mix(a, b, pow(t, 0.85));
    }
    vec3 spectrum(float t) {
      vec3 c0 = vec3(0.19, 0.27, 0.67), c1 = vec3(0.16, 0.62, 0.56), c2 = vec3(0.93, 0.80, 0.24), c3 = vec3(0.78, 0.20, 0.16);
      if (t < 0.33) return mix(c0, c1, t / 0.33);
      if (t < 0.66) return mix(c1, c2, (t - 0.33) / 0.33);
      return mix(c2, c3, (t - 0.66) / 0.34);
    }
    void main() {
      gl_Position = uMVP * vec4(aPos, 1.0);
      gl_PointSize = uSize;
      float t = clamp((aPos.z - uZmin) / max(uZmax - uZmin, 0.001), 0.0, 1.0);
      vec3 c = uColor == 0 ? ramp(t) : (uColor == 1 ? spectrum(t) : vec3(0.18));
      bool hl = false, hv = false, any = false;
      for (int i = 0; i < 6; i++) {
        if (i >= uNBands) break;
        any = true;
        if (aPos.z >= uBands[i].x && aPos.z <= uBands[i].y) hl = true;
      }
      for (int i = 0; i < 8; i++) {
        if (i >= uNLines) break;
        if (uLB[i].w < 0.5) continue;
        any = true;
        vec2 q = aPos.xy - uLA[i].xy;
        float s = dot(q, uLA[i].zw);
        float d = dot(q, vec2(-uLA[i].w, uLA[i].z));
        if (s >= 0.0 && s <= uLB[i].x && d >= uLB[i].y && d <= uLB[i].z) hv = true;
      }
      if (hl) c = vec3(0.12, 0.31, 0.64);
      else if (hv) c = vec3(0.60, 0.32, 0.0);
      else if (any) c = mix(c, vec3(1.0), 0.55);
      vCol = c;
    }`;
  const FS = `
    precision mediump float;
    varying vec3 vCol;
    void main() {
      vec2 p = gl_PointCoord - 0.5;
      if (dot(p, p) > 0.25) discard;
      gl_FragColor = vec4(vCol, 1.0);
    }`;

  // ------------------------------------------------------------------ matrici 4×4 (colonne)
  const M = {
    mul(a, b) {
      const o = new Float64Array(16);
      for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) {
        let s = 0; for (let k = 0; k < 4; k++) s += a[k * 4 + r] * b[c * 4 + k]; o[c * 4 + r] = s;
      }
      return o;
    },
    persp(fovy, asp, n, f) {
      const t = 1 / Math.tan(fovy / 2), o = new Float64Array(16);
      o[0] = t / asp; o[5] = t; o[10] = (f + n) / (n - f); o[11] = -1; o[14] = 2 * f * n / (n - f); return o;
    },
    ortho(l, r, b, t, n, f) {
      const o = new Float64Array(16);
      o[0] = 2 / (r - l); o[5] = 2 / (t - b); o[10] = -2 / (f - n);
      o[12] = -(r + l) / (r - l); o[13] = -(t + b) / (t - b); o[14] = -(f + n) / (f - n); o[15] = 1; return o;
    },
    lookAt(e, c, up) {
      const z = norm(sub(e, c)), x = norm(cross(up, z)), y = cross(z, x), o = new Float64Array(16);
      o[0] = x[0]; o[4] = x[1]; o[8] = x[2]; o[1] = y[0]; o[5] = y[1]; o[9] = y[2];
      o[2] = z[0]; o[6] = z[1]; o[10] = z[2];
      o[12] = -dot(x, e); o[13] = -dot(y, e); o[14] = -dot(z, e); o[15] = 1; return o;
    },
    inv(m) {
      const a = m, o = new Float64Array(16);
      const b00 = a[0] * a[5] - a[1] * a[4], b01 = a[0] * a[6] - a[2] * a[4], b02 = a[0] * a[7] - a[3] * a[4],
        b03 = a[1] * a[6] - a[2] * a[5], b04 = a[1] * a[7] - a[3] * a[5], b05 = a[2] * a[7] - a[3] * a[6],
        b06 = a[8] * a[13] - a[9] * a[12], b07 = a[8] * a[14] - a[10] * a[12], b08 = a[8] * a[15] - a[11] * a[12],
        b09 = a[9] * a[14] - a[10] * a[13], b10 = a[9] * a[15] - a[11] * a[13], b11 = a[10] * a[15] - a[11] * a[14];
      let d = b00 * b11 - b01 * b10 + b02 * b09 + b03 * b08 - b04 * b07 + b05 * b06;
      if (!d) return null; d = 1 / d;
      o[0] = (a[5] * b11 - a[6] * b10 + a[7] * b09) * d; o[1] = (a[2] * b10 - a[1] * b11 - a[3] * b09) * d;
      o[2] = (a[13] * b05 - a[14] * b04 + a[15] * b03) * d; o[3] = (a[10] * b04 - a[9] * b05 - a[11] * b03) * d;
      o[4] = (a[6] * b08 - a[4] * b11 - a[7] * b07) * d; o[5] = (a[0] * b11 - a[2] * b08 + a[3] * b07) * d;
      o[6] = (a[14] * b02 - a[12] * b05 - a[15] * b01) * d; o[7] = (a[8] * b05 - a[10] * b02 + a[11] * b01) * d;
      o[8] = (a[4] * b10 - a[5] * b08 + a[7] * b06) * d; o[9] = (a[1] * b08 - a[0] * b10 - a[3] * b06) * d;
      o[10] = (a[12] * b04 - a[13] * b02 + a[15] * b00) * d; o[11] = (a[9] * b02 - a[8] * b04 - a[11] * b00) * d;
      o[12] = (a[5] * b07 - a[4] * b09 - a[6] * b06) * d; o[13] = (a[0] * b09 - a[1] * b07 + a[2] * b06) * d;
      o[14] = (a[13] * b01 - a[12] * b03 - a[14] * b00) * d; o[15] = (a[8] * b03 - a[9] * b01 + a[10] * b00) * d;
      return o;
    },
    xf(m, p) {
      const x = p[0], y = p[1], z = p[2], w = m[3] * x + m[7] * y + m[11] * z + m[15];
      return [(m[0] * x + m[4] * y + m[8] * z + m[12]) / w, (m[1] * x + m[5] * y + m[9] * z + m[13]) / w,
              (m[2] * x + m[6] * y + m[10] * z + m[14]) / w, w];
    },
  };
  const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  const norm = (a) => { const l = Math.hypot(...a) || 1; return [a[0] / l, a[1] / l, a[2] / l]; };
  const fmt = (v, d = 2) => Number(v).toLocaleString("it-IT", { minimumFractionDigits: d, maximumFractionDigits: d });

  class Viewer {
    constructor(canvas, overlay) {
      this.cv = canvas; this.ov = overlay; this.ctx = overlay.getContext("2d");
      const gl = canvas.getContext("webgl", { antialias: true, alpha: true, preserveDrawingBuffer: true });
      this.gl = gl;
      if (!gl) { this.failed = true; return; }
      const sh = (type, src) => {
        const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
        if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
        return s;
      };
      const p = gl.createProgram();
      gl.attachShader(p, sh(gl.VERTEX_SHADER, VS)); gl.attachShader(p, sh(gl.FRAGMENT_SHADER, FS));
      gl.linkProgram(p);
      if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
      this.prog = p;
      this.loc = {};
      ["uMVP", "uSize", "uZmin", "uZmax", "uColor", "uNBands", "uNLines"].forEach((n) => (this.loc[n] = gl.getUniformLocation(p, n)));
      this.loc.uBands = gl.getUniformLocation(p, "uBands[0]");
      this.loc.uLA = gl.getUniformLocation(p, "uLA[0]");
      this.loc.uLB = gl.getUniformLocation(p, "uLB[0]");
      this.aPos = gl.getAttribLocation(p, "aPos");
      this.buf = gl.createBuffer();
      this.n = 0;
      this.mode = "3d"; this.colorMode = 0; this.size = 2;
      this.cam = { t: [0, 0, 0], yaw: -2.2, pitch: 0.55, dist: 30, half: 10 };
      this.bands = []; this.lines = []; this.levels = []; this.draw = null; this.hover = null;
      this.angle = 0;
      this._bind();
      new ResizeObserver(() => this.resize()).observe(canvas.parentElement);
      this.resize();
    }

    // -------------------------------------------------------------- dati
    setCloud(q, bbox, angleDeg = 0) {
      const gl = this.gl;
      const [x0, y0, z0, x1, y1, z1] = bbox;
      this.bbox = bbox;
      this.origin = [(x0 + x1) / 2, (y0 + y1) / 2, z0];
      this.angle = angleDeg * Math.PI / 180;
      const sx = (x1 - x0) / 65535, sy = (y1 - y0) / 65535, sz = (z1 - z0) / 65535;
      const n = q.length / 3, pos = new Float32Array(n * 3);
      const ox = x0 - this.origin[0], oy = y0 - this.origin[1];
      for (let i = 0, j = 0; i < n; i++, j += 3) {
        pos[j] = ox + q[j] * sx; pos[j + 1] = oy + q[j + 1] * sy; pos[j + 2] = q[j + 2] * sz;
      }
      this.n = n;
      this.ext = [x1 - x0, y1 - y0, z1 - z0];
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buf);
      gl.bufferData(gl.ARRAY_BUFFER, pos, gl.STATIC_DRAW);
      this.fit();
    }
    clear() { this.n = 0; this.bbox = null; this.render(); }
    toLocal(p) { return [p[0] - this.origin[0], p[1] - this.origin[1], (p[2] ?? this.origin[2]) - this.origin[2]]; }
    toAbs(p) { return [p[0] + this.origin[0], p[1] + this.origin[1], p[2] + this.origin[2]]; }

    fit() {
      if (!this.bbox) return;
      const [w, d, h] = this.ext, r = Math.hypot(w, d, h) / 2;
      this.cam.t = [0, 0, h / 2];
      this.cam.dist = r / Math.tan(0.4) * 1.05;
      const asp = this.cv.clientWidth / Math.max(this.cv.clientHeight, 1);
      if (this.mode === "top") {
        this.cam.t = [0, 0, h / 2];
        const c = Math.cos(this.angle), s = Math.sin(this.angle);
        const aw = Math.abs(w * c) + Math.abs(d * s), ad = Math.abs(w * s) + Math.abs(d * c);
        this.cam.half = Math.max(ad / 2, aw / 2 / asp) * 1.08;
      } else if (this.mode === "side") {
        this.cam.half = Math.max(h / 2, Math.hypot(w, d) / 2 / asp) * 1.12;
      } else {
        this.cam.half = r;
      }
      this.render();
    }

    setMode(m) {
      this.mode = m;
      if (m === "top") { this.cam.pitch = Math.PI / 2 - 1e-4; this.cam.yaw = this.angle + Math.PI / 2; }
      else if (m === "side") { this.cam.pitch = 0; this.cam.yaw = this.angle + Math.PI / 2; }
      else { this.cam.pitch = 0.55; this.cam.yaw = this.angle - 2.2 + Math.PI / 2; }
      this.fit();
    }
    setColor(m) { this.colorMode = m; this.render(); }
    setSize(s) { this.size = s; this.render(); }
    setBands(b) { this.bands = b || []; this.render(); }
    setLines(l) { this.lines = l || []; this.render(); }
    setLevels(l) { this.levels = l || []; this.render(); }
    setPlaneZ(zAbs) { if (this.origin) { this.levelZ = zAbs - this.origin[2]; this.render(); } }

    // -------------------------------------------------------------- camera
    _matrices() {
      const W = this.cv.clientWidth || 1, H = this.cv.clientHeight || 1, asp = W / H, c = this.cam;
      const f = [Math.cos(c.yaw), Math.sin(c.yaw), 0];
      const cp = Math.cos(c.pitch), sp = Math.sin(c.pitch);
      const back = [-f[0] * cp, -f[1] * cp, sp];
      const up = [f[0] * sp, f[1] * sp, cp];
      const R = Math.hypot(...this.ext || [10, 10, 10]) * 4 + c.dist;
      const eye = [c.t[0] + back[0] * (this.mode === "3d" ? c.dist : R / 2), c.t[1] + back[1] * (this.mode === "3d" ? c.dist : R / 2),
                   c.t[2] + back[2] * (this.mode === "3d" ? c.dist : R / 2)];
      const V = M.lookAt(eye, c.t, up);
      const P = this.mode === "3d" ? M.persp(0.8, asp, Math.max(c.dist / 500, 0.01), c.dist * 20 + R)
                                   : M.ortho(-c.half * asp, c.half * asp, -c.half, c.half, 0.01, R * 2);
      return { VP: M.mul(P, V), W, H, f, eye };
    }
    project(pl) {   // locale → pixel
      const { VP, W, H } = this._mx || this._matrices();
      const n = M.xf(VP, pl);
      if (n[3] <= 0) return null;
      return [(n[0] + 1) / 2 * W, (1 - n[1]) / 2 * H];
    }
    unprojectToZ(px, py, z) {   // pixel → punto locale sul piano orizzontale di quota z
      const { VP, W, H } = this._matrices();
      const inv = M.inv(VP); if (!inv) return null;
      const nx = px / W * 2 - 1, ny = 1 - py / H * 2;
      const a = M.xf(inv, [nx, ny, -1]), b = M.xf(inv, [nx, ny, 1]);
      const dz = b[2] - a[2];
      if (Math.abs(dz) < 1e-9) return null;
      const t = (z - a[2]) / dz;
      return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, z];
    }

    // -------------------------------------------------------------- interazione
    _bind() {
      const cv = this.ov;
      let drag = null;
      cv.addEventListener("contextmenu", (e) => e.preventDefault());
      cv.addEventListener("pointerdown", (e) => {
        if (this.draw && e.button === 0) return;   // in disegno il clic sinistro piazza i punti
        cv.setPointerCapture(e.pointerId);
        drag = { x: e.clientX, y: e.clientY, b: e.button, shift: e.shiftKey };
      });
      cv.addEventListener("pointermove", (e) => {
        const r = cv.getBoundingClientRect();
        if (this.draw) { this._drawMove(e.clientX - r.left, e.clientY - r.top, e.shiftKey); }
        if (!drag) return;
        const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
        drag.x = e.clientX; drag.y = e.clientY;
        const pan = drag.b === 2 || drag.b === 1 || drag.shift || (this.mode === "top" && !e.altKey);
        if (pan) this._pan(dx, dy);
        else {
          this.cam.yaw -= dx * 0.006;
          if (this.mode === "3d") this.cam.pitch = Math.min(1.55, Math.max(-0.2, this.cam.pitch + dy * 0.005));
        }
        this.render();
      });
      const end = () => (drag = null);
      cv.addEventListener("pointerup", end); cv.addEventListener("pointercancel", end);
      cv.addEventListener("wheel", (e) => {
        e.preventDefault();
        const k = Math.exp(e.deltaY * 0.0012);
        if (this.mode === "3d") this.cam.dist = Math.max(0.5, this.cam.dist * k);
        else {
          // zoom verso il cursore
          const r = cv.getBoundingClientRect(), px = e.clientX - r.left, py = e.clientY - r.top;
          const before = this._screenToPlane(px, py);
          this.cam.half = Math.max(0.3, this.cam.half * k);
          const after = this._screenToPlane(px, py);
          if (before && after) { for (let i = 0; i < 3; i++) this.cam.t[i] += before[i] - after[i]; }
        }
        this.render();
      }, { passive: false });
      cv.addEventListener("click", (e) => {
        if (!this.draw) return;
        const r = cv.getBoundingClientRect();
        this._drawClick(e.clientX - r.left, e.clientY - r.top, e.shiftKey);
      });
      window.addEventListener("keydown", (e) => { if (e.key === "Escape" && this.draw) this.cancelDraw(); });
    }
    _screenToPlane(px, py) {
      if (this.mode === "side") {
        const { VP, W, H } = this._matrices(); const inv = M.inv(VP);
        return M.xf(inv, [px / W * 2 - 1, 1 - py / H * 2, 0]).slice(0, 3);
      }
      return this.unprojectToZ(px, py, this.cam.t[2]);
    }
    _pan(dx, dy) {
      const H = this.cv.clientHeight || 1;
      const s = this.mode === "3d" ? this.cam.dist * 0.8 / H * 1.0 : this.cam.half * 2 / H;
      const c = this.cam, f = [Math.cos(c.yaw), Math.sin(c.yaw), 0];
      const right = [f[1], -f[0], 0];
      const cp = Math.cos(c.pitch), sp = Math.sin(c.pitch);
      const up = [f[0] * sp, f[1] * sp, cp];
      for (let i = 0; i < 3; i++) c.t[i] += (-dx * right[i] + dy * up[i]) * s;
    }

    // -------------------------------------------------------------- disegno linea
    startDraw(zPlaneAbs, cb) {
      if (this.mode === "side") this.setMode("top");
      this.draw = { z: zPlaneAbs - this.origin[2], p1: null, cur: null, cb };
      this.ov.classList.add("drawing");
      this.render();
    }
    cancelDraw() {
      const d = this.draw; this.draw = null; this.ov.classList.remove("drawing"); this.render();
      if (d && d.cb) d.cb(null);
    }
    _snap(p1, p, shift) {
      if (!shift || !p1) return p;
      const dx = p[0] - p1[0], dy = p[1] - p1[1], L = Math.hypot(dx, dy);
      let a = Math.atan2(dy, dx) - this.angle;
      a = Math.round(a / (Math.PI / 2)) * (Math.PI / 2) + this.angle;
      return [p1[0] + Math.cos(a) * L, p1[1] + Math.sin(a) * L, p[2]];
    }
    _drawMove(px, py, shift) {
      const p = this.unprojectToZ(px, py, this.draw.z);
      this.draw.cur = p ? this._snap(this.draw.p1, p, shift) : null;
      this.render();
    }
    _drawClick(px, py, shift) {
      const p = this.unprojectToZ(px, py, this.draw.z);
      if (!p) return;
      if (!this.draw.p1) { this.draw.p1 = p; this.render(); return; }
      const p2 = this._snap(this.draw.p1, p, shift);
      if (Math.hypot(p2[0] - this.draw.p1[0], p2[1] - this.draw.p1[1]) < 0.3) return;
      const a = this.toAbs(this.draw.p1), b = this.toAbs(p2), cb = this.draw.cb;
      this.draw = null; this.ov.classList.remove("drawing");
      cb && cb([a[0], a[1]], [b[0], b[1]]);
      this.render();
    }

    // -------------------------------------------------------------- disegno
    resize() {
      const dpr = window.devicePixelRatio || 1, el = this.cv.parentElement;
      const w = el.clientWidth, h = el.clientHeight;
      for (const c of [this.cv, this.ov]) {
        c.width = Math.max(1, Math.round(w * dpr)); c.height = Math.max(1, Math.round(h * dpr));
        c.style.width = w + "px"; c.style.height = h + "px";
      }
      this.dpr = dpr;
      this.render();
    }
    render() {
      if (this._raf) return;
      this._raf = requestAnimationFrame(() => { this._raf = null; this._render(); });
    }
    _render() {
      const gl = this.gl;
      if (!gl) return;
      gl.viewport(0, 0, this.cv.width, this.cv.height);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      this._mx = this._matrices();
      if (this.n) {
        gl.enable(gl.DEPTH_TEST);
        gl.useProgram(this.prog);
        gl.uniformMatrix4fv(this.loc.uMVP, false, new Float32Array(this._mx.VP));
        gl.uniform1f(this.loc.uSize, this.size * this.dpr);
        gl.uniform1f(this.loc.uZmin, 0); gl.uniform1f(this.loc.uZmax, this.ext[2]);
        gl.uniform1i(this.loc.uColor, this.colorMode);
        const b = new Float32Array(12);
        const bands = this.bands.slice(0, 6);
        bands.forEach((x, i) => { b[i * 2] = x.z0 - this.origin[2]; b[i * 2 + 1] = x.z1 - this.origin[2]; });
        gl.uniform2fv(this.loc.uBands, b); gl.uniform1i(this.loc.uNBands, bands.length);
        const la = new Float32Array(32), lb = new Float32Array(32);
        const lines = this.lines.slice(0, 8);
        lines.forEach((l, i) => {
          const f = this._frame(l);
          la.set([f.p1[0], f.p1[1], f.u[0], f.u[1]], i * 4);
          // nei punti evidenzio solo la fascia tagliata (sezione) o il prospetto sotto il mouse
          const cut = l.kind === "sezione";
          lb.set([f.L, cut ? -l.t / 2 : f.dmin, cut ? l.t / 2 : f.dmax,
                  l.active !== false && (cut || l.focus) ? 1 : 0], i * 4);
        });
        gl.uniform4fv(this.loc.uLA, la); gl.uniform4fv(this.loc.uLB, lb); gl.uniform1i(this.loc.uNLines, lines.length);
        gl.bindBuffer(gl.ARRAY_BUFFER, this.buf);
        gl.enableVertexAttribArray(this.aPos);
        gl.vertexAttribPointer(this.aPos, 3, gl.FLOAT, false, 0, 0);
        gl.drawArrays(gl.POINTS, 0, this.n);
      }
      this._overlay();
    }
    _frame(l) {
      const p1 = this.toLocal([l.p1[0], l.p1[1], 0]), p2 = this.toLocal([l.p2[0], l.p2[1], 0]);
      const L = Math.hypot(p2[0] - p1[0], p2[1] - p1[1]) || 1e-6;
      const u = [(p2[0] - p1[0]) / L, (p2[1] - p1[1]) / L], n = [-u[1], u[0]];
      const dmin = l.kind === "sezione" ? -l.t / 2 : 0;
      const dmax = l.kind === "sezione" ? l.t / 2 + (l.beyond || 0) : l.t;
      return { p1, p2, u, n, L, dmin, dmax };
    }

    _overlay() {
      const g = this.ctx, dpr = this.dpr || 1;
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, this.ov.width, this.ov.height);
      if (!this.bbox) return;
      const W = this.cv.clientWidth;
      g.font = "11px 'JetBrains Mono', Consolas, monospace";
      // linee di quota nella vista laterale
      if (this.mode === "side") {
        for (const lv of this.levels) {
          const p = this.project([this.cam.t[0], this.cam.t[1], lv.z - this.origin[2]]);
          if (!p) continue;
          g.strokeStyle = lv.color || "#5C5C5C"; g.lineWidth = 1; g.setLineDash(lv.dash ? [6, 4] : []);
          g.beginPath(); g.moveTo(0, p[1]); g.lineTo(W, p[1]); g.stroke(); g.setLineDash([]);
          const txt = lv.label;
          g.fillStyle = "rgba(255,255,255,.85)"; const tw = g.measureText(txt).width;
          g.fillRect(W - tw - 18, p[1] - 17, tw + 10, 15);
          g.fillStyle = lv.color || "#111"; g.fillText(txt, W - tw - 13, p[1] - 5);
        }
      }
      // linee di sezione / prospetto
      const zl = this.draw ? this.draw.z : (this.levelZ ?? 0);
      for (const l of this.lines) {
        if (this.mode === "side") continue;
        const f = this._frame(l), z = zl;
        const P = (s, d) => this.project([f.p1[0] + f.u[0] * s + f.n[0] * d, f.p1[1] + f.u[1] * s + f.n[1] * d, z]);
        const col = l.kind === "sezione" ? "#1F4FA3" : "#9A5200";
        const on = l.active !== false;
        const quad = [P(0, f.dmin), P(f.L, f.dmin), P(f.L, f.dmax), P(0, f.dmax)];
        if (quad.every(Boolean) && on) {
          g.fillStyle = l.kind === "sezione" ? "rgba(31,79,163,.10)" : "rgba(154,82,0,.08)";
          g.beginPath(); quad.forEach((q, i) => (i ? g.lineTo(...q) : g.moveTo(...q))); g.closePath(); g.fill();
        }
        const a = P(0, 0), b = P(f.L, 0);
        if (!a || !b) continue;
        g.strokeStyle = col; g.globalAlpha = on ? 1 : 0.35; g.lineWidth = l.focus ? 2.5 : 1.5;
        g.setLineDash([10, 3, 2, 3]); g.beginPath(); g.moveTo(...a); g.lineTo(...b); g.stroke(); g.setLineDash([]);
        // frecce verso la vista
        const arrow = (q, s) => {
          const t = P(s, Math.max(0.6, Math.min(2, f.L * 0.08)));
          if (!t) return;
          g.beginPath(); g.moveTo(...q); g.lineTo(...t); g.stroke();
          const ang = Math.atan2(t[1] - q[1], t[0] - q[0]);
          g.beginPath(); g.moveTo(...t);
          g.lineTo(t[0] - 9 * Math.cos(ang - 0.4), t[1] - 9 * Math.sin(ang - 0.4));
          g.lineTo(t[0] - 9 * Math.cos(ang + 0.4), t[1] - 9 * Math.sin(ang + 0.4)); g.closePath(); g.fillStyle = col; g.fill();
        };
        arrow(a, 0); arrow(b, f.L);
        g.font = "600 13px Inter, 'Segoe UI', sans-serif";
        const lab = (q, s) => { const t = P(s, -0.7); if (t) { g.fillStyle = col; g.fillText(l.label, t[0] - 4, t[1] + 4); } };
        lab(a, 0); lab(b, f.L);
        g.globalAlpha = 1;
      }
      // linea in costruzione
      if (this.draw) {
        g.fillStyle = "#111"; g.strokeStyle = "#111"; g.lineWidth = 1.5;
        const dot = (pl) => { const p = this.project(pl); if (p) { g.beginPath(); g.arc(p[0], p[1], 4, 0, 7); g.fill(); } return p; };
        const a = this.draw.p1 && dot(this.draw.p1);
        const c = this.draw.cur && dot(this.draw.cur);
        if (a && c) {
          g.setLineDash([6, 4]); g.beginPath(); g.moveTo(...a); g.lineTo(...c); g.stroke(); g.setLineDash([]);
          const L = Math.hypot(this.draw.cur[0] - this.draw.p1[0], this.draw.cur[1] - this.draw.p1[1]);
          g.font = "12px 'JetBrains Mono', Consolas, monospace";
          const txt = `${fmt(L)} m`, tw = g.measureText(txt).width;
          g.fillStyle = "#111"; g.fillRect(c[0] + 10, c[1] - 22, tw + 12, 18);
          g.fillStyle = "#fff"; g.fillText(txt, c[0] + 16, c[1] - 9);
        }
      }
      // assi e scala (pianta)
      if (this.mode === "top") {
        const H = this.cv.clientHeight, mpp = this.cam.half * 2 / H;
        let step = 1; for (const s of [0.5, 1, 2, 5, 10, 20, 50, 100]) { if (s / mpp > 60) { step = s; break; } step = s; }
        const px = step / mpp, x = 16, y = H - 22;
        g.fillStyle = "#111"; g.fillRect(x, y, px, 3);
        g.font = "11px 'JetBrains Mono', Consolas, monospace"; g.fillText(`${step} m`, x, y - 6);
      }
    }
  }
  window.TracciatoViewer = Viewer;
})();

// Canvas viewers for the Inspect page:
//   ScanView  - overview micrograph with the scan mesh and every scan position; hover/click picks a position.
//   FrameView - the diffraction pattern at the picked position (or a sampled mean), with scale/colormap.
//
// Geometry follows azohp_to_py4d: coords[:, 0] is the row (y) voltage, coords[:, 1] the column (x)
// voltage; real-space positions are x = nm_per_V * col_V and y = -nm_per_V * row_V, so the scan
// starts at the top-left. '_overview_extent' is [top row V, left col V, row span V, col span V] and
// meshParams 'extent' is [y0, x0, height, width], both in the same voltage frame.

import { api, fetchImage } from "./api.js";
import { fmt, h, segmented } from "./dom.js";
import { storage } from "./state.js";

const STOPS = {
  inferno: ["#000004", "#1b0c41", "#4a0c6b", "#781c6d", "#a52c60", "#cf4446", "#ed6925", "#fb9b06", "#fcffa4"],
  viridis: ["#440154", "#472d7b", "#3b528b", "#2c728e", "#21918c", "#28ae80", "#5ec962", "#addc30", "#fde725"],
};
const LUTS = {};
function lut(name) {
  if (LUTS[name]) return LUTS[name];
  const stops = STOPS[name].map((hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)));
  const table = new Uint8ClampedArray(256 * 3);
  for (let v = 0; v < 256; v++) {
    const t = (v / 255) * (stops.length - 1);
    const i = Math.min(Math.floor(t), stops.length - 2);
    const f = t - i;
    for (let c = 0; c < 3; c++) table[v * 3 + c] = stops[i][c] + (stops[i + 1][c] - stops[i][c]) * f;
  }
  return (LUTS[name] = table);
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function niceStep(raw) {
  const exp = Math.pow(10, Math.floor(Math.log10(raw)));
  const m = raw / exp;
  return (m < 1.5 ? 1 : m < 3.5 ? 2 : m < 7.5 ? 5 : 10) * exp;
}

function sizeCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.round(canvas.clientWidth * dpr));
  const hgt = Math.max(1, Math.round(canvas.clientHeight * dpr));
  if (canvas.width !== w || canvas.height !== hgt) {
    canvas.width = w;
    canvas.height = hgt;
    return true;
  }
  return false;
}

// ---------------------------------------------------------------------------------------------

export class ScanView {
  constructor({ path, geometry, onPick }) {
    this.path = path;
    this.g = geometry;
    this.onPick = onPick;
    this.coords = null;
    this.overview = null;
    this.selected = 0;
    this.hover = null;
    this.mode = storage.get("scanMode", "region");
    this.showPoints = storage.get("scanPoints", true);
    this.base = document.createElement("canvas");

    this.canvas = h("canvas", { "aria-label": "Scan positions over the overview image. Click to pick a position." });
    this.status = h("div", { class: "state" }, "Loading scan coordinates…");
    this.chipPos = h("div", { class: "overlay-chip bl", hidden: true });
    this.chipIdx = h("div", { class: "overlay-chip tr", hidden: true });
    this.well = h("div", { class: "well square" }, this.canvas, this.status, this.chipPos, this.chipIdx);
    this.toolbar = h("div", { class: "viewer-toolbar" });
    this.el = h("div", null, this.toolbar, this.well,
      h("div", { class: "viewer-foot" },
        h("div", { class: "legend" },
          h("span", null, h("i", { class: "swatch", style: { background: "var(--accent)" } }), "Scan positions"),
          h("span", null, h("i", { class: "swatch", style: { background: "var(--beam)" } }), "First 10 (scan start)"),
          h("span", null, h("i", { class: "swatch", style: { border: "1.5px dashed var(--accent)" } }), "Mesh region"))));
    this.renderToolbar();

    this.canvas.addEventListener("mousemove", (e) => this.onMove(e));
    this.canvas.addEventListener("mouseleave", () => { this.hover = null; this.draw(); });
    this.canvas.addEventListener("click", (e) => {
      const hit = this.hitTest(e);
      if (hit != null) this.onPick(hit);
    });
    this.resizeObserver = new ResizeObserver(() => { if (sizeCanvas(this.canvas)) this.redrawBase(); });
    this.resizeObserver.observe(this.canvas);
    this.load();
  }

  destroy() { this.resizeObserver.disconnect(); }

  async load() {
    try {
      const [coords, overview] = await Promise.all([
        api.coords(this.path),
        this.g.overview_shape ? fetchImage(api.overviewUrl(this.path, Date.now())).then((r) => r.bitmap).catch(() => null) : null,
      ]);
      this.coords = coords;
      this.overview = overview;
      this.status.hidden = true;
      if (!this.g.overview_extent) this.mode = "region";
      this.renderToolbar();
      sizeCanvas(this.canvas);
      this.redrawBase();
    } catch (err) {
      this.status.textContent = err.message;
    }
  }

  renderToolbar() {
    const canFull = Boolean(this.g.overview_extent && this.g.overview_shape);
    this.toolbar.replaceChildren(
      segmented([
        { value: "region", label: "Scan region" },
        { value: "full", label: "Full overview", disabled: !canFull },
      ], this.mode, (mode) => { this.mode = mode; storage.set("scanMode", mode); this.renderToolbar(); this.redrawBase(); },
      { label: "Field of view" }),
      h("label", { class: "check-row", style: { gridTemplateColumns: "16px auto", alignItems: "center" } },
        h("input", { type: "checkbox", class: "checkbox", checked: this.showPoints,
          onChange: (e) => { this.showPoints = e.target.checked; storage.set("scanPoints", this.showPoints); this.redrawBase(); } }),
        h("span", null, "Positions")),
      h("span", { class: "faint", style: { marginLeft: "auto", fontSize: "12px" } },
        this.g.n_coords ? `${fmt.int(this.g.n_coords)} positions` : ""));
  }

  window() {
    const g = this.g;
    let top, left, height, width;
    if (this.mode === "full" && g.overview_extent) {
      [top, left, height, width] = g.overview_extent;
    } else if (g.mesh_extent) {
      [top, left, height, width] = g.mesh_extent;
    } else if (this.coords) {
      let r0 = Infinity, r1 = -Infinity, c0 = Infinity, c1 = -Infinity;
      for (let i = 0; i < this.coords.length; i += 2) {
        r0 = Math.min(r0, this.coords[i]); r1 = Math.max(r1, this.coords[i]);
        c0 = Math.min(c0, this.coords[i + 1]); c1 = Math.max(c1, this.coords[i + 1]);
      }
      [top, left, height, width] = [r0, c0, r1 - r0 || 1, c1 - c0 || 1];
    } else {
      [top, left, height, width] = [-8, -8, 16, 16];
    }
    if (height < 0) { top += height; height = -height; }
    if (width < 0) { left += width; width = -width; }
    let size = Math.max(height, width);
    if (this.mode !== "full") {
      const pad = size * 0.08;
      top -= pad; left -= pad; height += 2 * pad; width += 2 * pad; size += 2 * pad;
    }
    return { top: top - (size - height) / 2, left: left - (size - width) / 2, size };
  }

  toPx(rowV, colV) {
    const w = this.win, W = this.canvas.width;
    return [((colV - w.left) / w.size) * W, ((rowV - w.top) / w.size) * W];
  }

  redrawBase() {
    if (!this.coords) return;
    const W = this.canvas.width, H = this.canvas.height;
    const dpr = window.devicePixelRatio || 1;
    this.base.width = W;
    this.base.height = H;
    this.win = this.window();
    const ctx = this.base.getContext("2d");
    const g = this.g;
    const accent = cssVar("--accent") || "#4db3ff";
    const beam = cssVar("--beam") || "#ff7847";

    ctx.fillStyle = "#060709";
    ctx.fillRect(0, 0, W, H);

    if (this.overview && g.overview_extent) {
      const [t, l, rs, cs] = g.overview_extent;
      const [x0, y0] = this.toPx(t, l);
      const [x1, y1] = this.toPx(t + rs, l + cs);
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = "high";
      ctx.drawImage(this.overview, x0, y0, x1 - x0, y1 - y0);
    }

    if (g.mesh_extent) {
      const [y0v, x0v, hv, wv] = g.mesh_extent;
      const [mx0, my0] = this.toPx(y0v, x0v);
      const [mx1, my1] = this.toPx(y0v + hv, x0v + wv);
      if (this.mode === "full") {
        ctx.fillStyle = "rgba(0,0,0,0.42)";
        ctx.beginPath();
        ctx.rect(0, 0, W, H);
        ctx.rect(mx0, my0, mx1 - mx0, my1 - my0);
        ctx.fill("evenodd");
      }
      ctx.setLineDash([6 * dpr, 4 * dpr]);
      ctx.lineWidth = 1.5 * dpr;
      ctx.strokeStyle = accent;
      ctx.strokeRect(mx0, my0, mx1 - mx0, my1 - my0);
      ctx.setLineDash([]);
    }

    const c = this.coords;
    const n = c.length / 2;
    const cols = g.grid_shape ? g.grid_shape[1] : Math.round(Math.sqrt(n));
    const [ax] = this.toPx(0, 0);
    const [bx] = this.toPx(0, g.mesh_extent ? g.mesh_extent[3] / Math.max(1, cols - 1) : 0.1);
    const spacing = Math.abs(bx - ax);
    this.hoverRadiusV = g.mesh_extent ? Math.abs(g.mesh_extent[3]) / Math.max(1, cols - 1) * 1.5 : Infinity;

    if (this.showPoints) {
      const r = Math.min(3 * dpr, Math.max(0.6 * dpr, spacing * 0.22));
      ctx.fillStyle = accent;
      ctx.globalAlpha = this.mode === "full" ? 0.55 : 0.75;
      if (r < 1.3) {
        for (let i = 0; i < n; i++) {
          const [x, y] = this.toPx(c[2 * i], c[2 * i + 1]);
          ctx.fillRect(x - r, y - r, 2 * r, 2 * r);
        }
      } else {
        ctx.beginPath();
        for (let i = 0; i < n; i++) {
          const [x, y] = this.toPx(c[2 * i], c[2 * i + 1]);
          ctx.moveTo(x + r, y);
          ctx.arc(x, y, r, 0, Math.PI * 2);
        }
        ctx.fill();
      }
      ctx.globalAlpha = 1;
    }

    // Scan start: the first 10 positions, as in the converter's coordinate check figure.
    ctx.strokeStyle = beam;
    ctx.lineWidth = 1.5 * dpr;
    ctx.beginPath();
    for (let i = 0; i < Math.min(10, n); i++) {
      const [x, y] = this.toPx(c[2 * i], c[2 * i + 1]);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    ctx.stroke();
    const m = 3 * dpr;
    ctx.beginPath();
    for (let i = 0; i < Math.min(10, n); i++) {
      const [x, y] = this.toPx(c[2 * i], c[2 * i + 1]);
      ctx.moveTo(x - m, y - m); ctx.lineTo(x + m, y + m);
      ctx.moveTo(x + m, y - m); ctx.lineTo(x - m, y + m);
    }
    ctx.stroke();

    this.drawAxes(ctx, W, H, dpr);
    this.draw();
  }

  drawAxes(ctx, W, H, dpr) {
    const nmPerV = this.g.nm_per_V;
    if (!nmPerV) return;
    const w = this.win;
    const spanNm = w.size * nmPerV;
    const step = niceStep(spanNm / 5);
    const decimals = Math.max(0, -Math.floor(Math.log10(step)));
    ctx.font = `${10 * dpr}px ui-monospace, Menlo, monospace`;
    ctx.fillStyle = "rgba(230,236,243,0.62)";
    ctx.strokeStyle = "rgba(230,236,243,0.35)";
    ctx.lineWidth = dpr;

    // x (nm) along the bottom edge
    const x0 = w.left * nmPerV, x1 = (w.left + w.size) * nmPerV;
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    for (let v = Math.ceil(x0 / step) * step; v <= x1; v += step) {
      const px = ((v / nmPerV - w.left) / w.size) * W;
      if (px < 24 * dpr || px > W - 24 * dpr) continue;
      ctx.beginPath(); ctx.moveTo(px, H); ctx.lineTo(px, H - 5 * dpr); ctx.stroke();
      ctx.fillText(v.toFixed(decimals), px, H - 7 * dpr);
    }
    // y (nm) = -nm_per_V * row_V along the left edge
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    const yTop = -w.top * nmPerV, yBottom = -(w.top + w.size) * nmPerV;
    for (let v = Math.ceil(yBottom / step) * step; v <= yTop; v += step) {
      const py = ((-v / nmPerV - w.top) / w.size) * H;
      if (py < 16 * dpr || py > H - 30 * dpr) continue;
      ctx.beginPath(); ctx.moveTo(0, py); ctx.lineTo(5 * dpr, py); ctx.stroke();
      ctx.fillText(v.toFixed(decimals), 7 * dpr, py);
    }
    ctx.textAlign = "right";
    ctx.textBaseline = "bottom";
    ctx.fillText("nm", W - 6 * dpr, H - 7 * dpr);
  }

  nearest(rowV, colV) {
    const c = this.coords;
    let best = -1, bestD = Infinity;
    for (let i = 0, n = c.length / 2; i < n; i++) {
      const dr = c[2 * i] - rowV, dc = c[2 * i + 1] - colV;
      const d = dr * dr + dc * dc;
      if (d < bestD) { bestD = d; best = i; }
    }
    return { index: best, dist: Math.sqrt(bestD) };
  }

  eventToV(e) {
    const rect = this.canvas.getBoundingClientRect();
    const w = this.win;
    return [w.top + ((e.clientY - rect.top) / rect.height) * w.size,
            w.left + ((e.clientX - rect.left) / rect.width) * w.size];
  }

  hitTest(e) {
    if (!this.coords || !this.win) return null;
    const [rowV, colV] = this.eventToV(e);
    const { index, dist } = this.nearest(rowV, colV);
    return dist <= this.hoverRadiusV ? index : null;
  }

  onMove(e) {
    if (!this.coords || !this.win) return;
    const [rowV, colV] = this.eventToV(e);
    const hit = this.hitTest(e);
    this.hover = { rowV, colV, index: hit };
    if (!this.pending) {
      this.pending = requestAnimationFrame(() => { this.pending = null; this.draw(); });
    }
  }

  setSelected(index) {
    this.selected = index;
    this.draw();
  }

  describe(index) {
    const cols = this.g.grid_shape ? this.g.grid_shape[1] : null;
    return cols ? `row ${Math.floor(index / cols)} · col ${index % cols} · #${index}` : `#${index}`;
  }

  draw() {
    const canvas = this.canvas;
    if (!this.coords || !this.win) return;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    ctx.drawImage(this.base, 0, 0);
    const c = this.coords;

    const ring = (index, color, radius) => {
      const [x, y] = this.toPx(c[2 * index], c[2 * index + 1]);
      ctx.beginPath();
      ctx.arc(x, y, radius * dpr, 0, Math.PI * 2);
      ctx.lineWidth = 2 * dpr;
      ctx.strokeStyle = "rgba(0,0,0,0.6)";
      ctx.stroke();
      ctx.lineWidth = 1.5 * dpr;
      ctx.strokeStyle = color;
      ctx.stroke();
    };

    if (this.hover) {
      const [x, y] = this.toPx(this.hover.rowV, this.hover.colV);
      ctx.strokeStyle = "rgba(255,255,255,0.22)";
      ctx.lineWidth = dpr;
      ctx.beginPath();
      ctx.moveTo(x, 0); ctx.lineTo(x, canvas.height);
      ctx.moveTo(0, y); ctx.lineTo(canvas.width, y);
      ctx.stroke();
      if (this.hover.index != null) ring(this.hover.index, "rgba(255,255,255,0.9)", 5);
    }
    if (this.selected != null && this.selected < c.length / 2) ring(this.selected, "#ffd24a", 7);

    const nmPerV = this.g.nm_per_V;
    const shown = this.hover && this.hover.index != null ? this.hover.index : this.selected;
    this.chipIdx.hidden = shown == null;
    if (shown != null) this.chipIdx.textContent = this.describe(shown);
    this.chipPos.hidden = !(this.hover && nmPerV);
    if (this.hover && nmPerV) {
      this.chipPos.textContent = `x ${(nmPerV * this.hover.colV).toFixed(3)} nm · y ${(-nmPerV * this.hover.rowV).toFixed(3)} nm`;
    }
  }
}

// ---------------------------------------------------------------------------------------------

export class FrameView {
  constructor({ path, geometry, onStep, getRadius }) {
    this.path = path;
    this.g = geometry;
    this.onStep = onStep;
    this.getRadius = getRadius;
    this.index = 0;
    this.source = "position";
    this.scale = storage.get("frameScale", "log");
    this.cmap = storage.get("frameCmap", "inferno");
    this.image = null;
    this.stats = null;
    this.controller = null;

    this.canvas = h("canvas", { "aria-label": "Diffraction pattern" });
    this.status = h("div", { class: "state" }, "Loading…");
    this.chipInfo = h("div", { class: "overlay-chip tl" });
    this.chipStats = h("div", { class: "overlay-chip bl", hidden: true });
    this.well = h("div", {
      class: "well square", tabindex: "0",
      title: "Arrow keys step through scan positions",
      onKeydown: (e) => this.onKey(e),
    }, this.canvas, this.status, this.chipInfo, this.chipStats);
    this.toolbar = h("div", { class: "viewer-toolbar" });
    this.foot = h("div", { class: "viewer-foot" });
    this.el = h("div", null, this.toolbar, this.well, this.foot);

    this.resizeObserver = new ResizeObserver(() => { sizeCanvas(this.canvas); this.draw(); });
    this.resizeObserver.observe(this.canvas);
    this.renderControls();
    this.load();
  }

  destroy() {
    this.resizeObserver.disconnect();
    if (this.controller) this.controller.abort();
  }

  get grid() { return this.g.grid_shape || [1, this.g.dp_shape ? this.g.dp_shape[0] : 1]; }
  get frameCount() { return this.g.dp_shape ? this.g.dp_shape[0] : 0; }

  setIndex(index) {
    index = Math.max(0, Math.min(this.frameCount - 1, index));
    if (index === this.index && this.source === "position" && this.image) return;
    this.index = index;
    this.source = "position";
    this.renderControls();
    this.load();
  }

  onKey(e) {
    const [rows, cols] = this.grid;
    const r = Math.floor(this.index / cols), c = this.index % cols;
    const moves = { ArrowLeft: [0, -1], ArrowRight: [0, 1], ArrowUp: [-1, 0], ArrowDown: [1, 0] };
    const move = moves[e.key];
    if (!move) return;
    e.preventDefault();
    const nr = Math.max(0, Math.min(rows - 1, r + move[0]));
    const nc = Math.max(0, Math.min(cols - 1, c + move[1]));
    this.onStep(nr * cols + nc);
  }

  renderControls() {
    const [rows, cols] = this.grid;
    const row = Math.floor(this.index / cols), col = this.index % cols;
    const numberInput = (value, max, label, apply) => h("input", {
      class: "input mono", type: "number", min: "0", max: String(max), value: String(value), "aria-label": label,
      disabled: this.source !== "position",
      onChange: (e) => { const v = parseInt(e.target.value, 10); if (Number.isFinite(v)) apply(v); },
    });

    this.toolbar.replaceChildren(
      segmented([
        { value: "position", label: "At position" },
        { value: "mean", label: "Sampled mean", title: "Mean of up to 256 evenly spaced patterns" },
      ], this.source, (source) => { this.source = source; this.renderControls(); this.load(); }, { label: "Pattern source" }),
      segmented([
        { value: "log", label: "Log" },
        { value: "linear", label: "Linear" },
      ], this.scale, (scale) => { this.scale = scale; storage.set("frameScale", scale); this.renderControls(); this.load(); },
      { label: "Intensity scale" }),
      h("select", {
        class: "select", "aria-label": "Colormap", style: { marginLeft: "auto" },
        onChange: (e) => { this.cmap = e.target.value; storage.set("frameCmap", this.cmap); this.recolor(); },
      }, ["inferno", "viridis", "gray"].map((name) =>
        h("option", { value: name, selected: name === this.cmap ? "selected" : null }, name[0].toUpperCase() + name.slice(1)))));

    this.foot.replaceChildren(
      h("span", { class: "stepper-input" }, "Row", numberInput(row, rows - 1, "Scan row", (v) => this.onStep(Math.max(0, Math.min(rows - 1, v)) * cols + col))),
      h("span", { class: "stepper-input" }, "Col", numberInput(col, cols - 1, "Scan column", (v) => this.onStep(row * cols + Math.max(0, Math.min(cols - 1, v))))),
      h("span", { class: "faint", style: { marginLeft: "auto" } },
        this.g.dp_shape ? `${this.g.dp_shape[1]} × ${this.g.dp_shape[2]} px · arrow keys step` : ""));
  }

  async load() {
    if (!this.frameCount) {
      this.status.textContent = "No diffraction stack in this file";
      return;
    }
    if (this.controller) this.controller.abort();
    const controller = (this.controller = new AbortController());
    this.status.hidden = Boolean(this.image);
    this.well.style.opacity = this.image ? "0.85" : "1";
    const url = this.source === "mean" ? api.meanUrl(this.path, this.scale) : api.frameUrl(this.path, this.index, this.scale);
    try {
      const { bitmap, stats } = await fetchImage(url, controller.signal);
      if (controller.signal.aborted) return;
      this.bitmap = bitmap;
      this.stats = stats;
      this.status.hidden = true;
      this.recolor();
    } catch (err) {
      if (err.name === "AbortError") return;
      this.status.hidden = false;
      this.status.textContent = err.message;
    } finally {
      if (this.controller === controller) this.well.style.opacity = "1";
    }
  }

  recolor() {
    if (!this.bitmap) return;
    const { width, height } = this.bitmap;
    const img = document.createElement("canvas");
    img.width = width;
    img.height = height;
    const ctx = img.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(this.bitmap, 0, 0);
    if (this.cmap !== "gray") {
      const data = ctx.getImageData(0, 0, width, height);
      const table = lut(this.cmap);
      const px = data.data;
      for (let p = 0; p < px.length; p += 4) {
        const v = px[p] * 3;
        px[p] = table[v]; px[p + 1] = table[v + 1]; px[p + 2] = table[v + 2];
      }
      ctx.putImageData(data, 0, 0);
    }
    this.image = img;
    this.draw();
  }

  draw() {
    if (!this.image) return;
    sizeCanvas(this.canvas);
    const ctx = this.canvas.getContext("2d");
    const W = this.canvas.width, H = this.canvas.height;
    const dpr = window.devicePixelRatio || 1;
    ctx.fillStyle = "#060709";
    ctx.fillRect(0, 0, W, H);
    const scale = Math.min(W / this.image.width, H / this.image.height);
    const dw = this.image.width * scale, dh = this.image.height * scale;
    const ox = (W - dw) / 2, oy = (H - dh) / 2;
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(this.image, ox, oy, dw, dh);

    // Detector centre (where azohp_to_py4d places its virtual detectors) and, if set, the BF disk.
    const cx = ox + (this.image.width / 2 + 0.5) * scale;
    const cy = oy + (this.image.height / 2 + 0.5) * scale;
    ctx.strokeStyle = "rgba(255,255,255,0.35)";
    ctx.lineWidth = dpr;
    ctx.beginPath();
    ctx.moveTo(cx - 8 * dpr, cy); ctx.lineTo(cx + 8 * dpr, cy);
    ctx.moveTo(cx, cy - 8 * dpr); ctx.lineTo(cx, cy + 8 * dpr);
    ctx.stroke();
    const radius = this.getRadius();
    if (radius) {
      ctx.setLineDash([5 * dpr, 4 * dpr]);
      ctx.strokeStyle = "rgba(120, 220, 255, 0.9)";
      ctx.lineWidth = 1.5 * dpr;
      ctx.beginPath();
      ctx.arc(cx, cy, radius * scale, 0, Math.PI * 2);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    const [, cols] = this.grid;
    this.chipInfo.textContent = this.source === "mean"
      ? `Mean of ${this.stats ? this.stats.frames : "…"} patterns`
      : `row ${Math.floor(this.index / cols)} · col ${this.index % cols} · #${this.index}`;
    this.chipStats.hidden = !this.stats;
    if (this.stats) {
      this.chipStats.textContent = `min ${fmt.sig(this.stats.min, 3)} · max ${fmt.sig(this.stats.max, 3)} · mean ${fmt.sig(this.stats.mean, 3)}`;
    }
  }
}

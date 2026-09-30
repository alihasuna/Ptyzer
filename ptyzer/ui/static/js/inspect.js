// Inspect view: pre-flight checks, scan geometry, diffraction patterns, acquisition summary,
// earlier outputs, decoded metadata and HDF5 layout for the focused .hp file.

import { api } from "./api.js";
import { generateSample, navigate } from "./browser.js";
import { callout, card, clear, copyText, fmt, h, icon, segmented, toast } from "./dom.js";
import { openLightbox } from "./lightbox.js";
import { state, subscribe, update } from "./state.js";
import { FrameView, ScanView } from "./viewers.js";

const root = document.getElementById("inspect");
let loadToken = 0;
let loadedPath = null;
let error = null;
let scanView = null;
let frameView = null;
let metaSource = "diffraction/meta";
let metaFilter = "";

const FIGURE_LABELS = {
  coord_checks: "Scan-coordinate checks",
  overview: "Overview & scan mesh",
  virtual_diff: "Virtual images & diffraction",
  parallax_recon: "Parallax reconstruction",
};

function destroyViewers() {
  scanView && scanView.destroy();
  frameView && frameView.destroy();
  scanView = frameView = null;
}

async function load(path) {
  const token = ++loadToken;
  loadedPath = path;
  error = null;
  destroyViewers();
  update({ inspection: null });
  render();
  try {
    const data = await api.inspect(path);
    if (token !== loadToken) return;
    update({ inspection: data });
  } catch (err) {
    if (token !== loadToken) return;
    error = err.message;
  }
  render();
}

function emptyMark() {
  const dots = [24, 36, 48, 60].map((y) => [24, 36, 48, 60].map((x) =>
    `<circle cx="${x}" cy="${y}" r="2.2" fill="var(--accent)" opacity="${x === 48 && y === 36 ? 1 : 0.35}"/>`).join("")).join("");
  const wrap = document.createElement("div");
  wrap.innerHTML = `<svg class="empty-mark" viewBox="0 0 88 88" aria-hidden="true">
    <rect x="8" y="8" width="72" height="72" rx="18" fill="var(--accent-soft)"/>${dots}
    <circle cx="48" cy="36" r="9" fill="none" stroke="var(--accent)" stroke-width="2"/>
    <path d="M24 24 L60 24" stroke="var(--beam)" stroke-width="2" fill="none" stroke-linecap="round"/></svg>`;
  return wrap.firstElementChild;
}

function emptyState() {
  return h("div", { class: "empty" },
    h("div", { class: "empty-inner" },
      emptyMark(),
      h("h2", null, "Inspect an Azorus dataset"),
      h("p", null, "Pick a ", h("span", { class: "code-inline" }, ".hp"),
        " file in the browser to check its metadata, scan geometry and diffraction patterns, then convert it to a py4DSTEM ",
        h("span", { class: "code-inline" }, ".h5"), " with QC figures and a parallax aberration fit."),
      h("div", { class: "empty-actions" },
        h("button", { class: "btn primary", onClick: generateSample }, icon("sparkle"), "Try a sample dataset"),
        state.env && h("button", { class: "btn", onClick: () => navigate(state.env.start_dir) }, icon("folder"), "Open launch folder"))));
}

function skeleton() {
  return h("div", { class: "page" },
    h("div", { class: "skeleton", style: { height: "16px", width: "240px" } }),
    h("div", { class: "skeleton", style: { height: "28px", width: "380px" } }),
    h("div", { class: "skeleton", style: { height: "26px", width: "520px" } }),
    h("div", { class: "grid-2 viewers" },
      h("div", { class: "skeleton", style: { aspectRatio: "1 / 1.1" } }),
      h("div", { class: "skeleton", style: { aspectRatio: "1 / 1.1" } })));
}

function header(data) {
  const g = data.geometry || {};
  const chip = (label, value) => value && h("span", { class: "chip" }, label, h("strong", null, value));
  return h("div", { class: "page-head" },
    h("div", { class: "eyebrow" }, icon("folder", "sm"),
      h("span", { class: "path mono", title: fmt.dirname(data.path) }, `‎${fmt.dirname(data.path)}`)),
    h("div", { class: "page-title-row" },
      h("h1", { class: "page-title" }, data.name),
      h("div", { class: "card-actions" },
        h("button", { class: "btn sm", onClick: () => copyText(data.path, "Path copied") }, icon("copy", "sm"), "Copy path"),
        h("button", { class: "btn sm", onClick: () => api.reveal(data.path).catch((e) => toast(e.message, { kind: "fail" })) },
          icon("reveal", "sm"), "Reveal"),
        h("button", { class: "icon-btn sm", title: "Reload", "aria-label": "Reload", onClick: () => load(data.path) }, icon("refresh", "sm")))),
    h("div", { class: "chips" },
      chip("Scan", g.grid_shape && `${g.grid_shape[0]} × ${g.grid_shape[1]}`),
      chip("Frames", g.dp_shape && fmt.int(g.dp_shape[0])),
      chip("Detector", g.dp_shape && `${g.dp_shape[1]} × ${g.dp_shape[2]} px`),
      chip("Step", g.R_pixel_size_nm && `${(g.R_pixel_size_nm * 1000).toFixed(2)} pm`),
      chip("dtype", g.dp_dtype),
      chip("On disk", fmt.bytes(data.size)),
      chip("Modified", fmt.datetime(data.mtime))));
}

function checks(data) {
  const fails = data.checks.filter((c) => c.status === "fail");
  const warns = data.checks.filter((c) => c.status === "warn");
  const oks = data.checks.filter((c) => c.status === "ok");
  return h("div", { style: { display: "grid", gap: "8px" } },
    h("div", { class: "checks" },
      h("span", { class: `check ${fails.length ? "fail" : warns.length ? "warn" : "ok"}` },
        icon(fails.length ? "x" : warns.length ? "alert" : "check", "sm"),
        fails.length ? "Conversion will fail" : warns.length ? "Ready, with warnings" : "Ready to convert"),
      oks.map((c) => h("span", { class: "check", title: c.detail }, icon("check", "sm"), c.title, c.detail && h("span", { class: "check-detail" }, `· ${c.detail}`)))),
    [...fails, ...warns].map((c) => callout(c.status, c.title, c.detail)));
}

function viewers(data) {
  const g = data.geometry;
  if (!g || !g.dp_shape) return null;
  scanView = new ScanView({ path: data.path, geometry: g, onPick: (i) => pick(i) });
  frameView = new FrameView({
    path: data.path, geometry: g, onStep: (i) => pick(i),
    getRadius: () => (state.settings && state.settings.bf_disk_radius) || null,
  });
  const center = g.grid_shape ? Math.floor(g.grid_shape[0] / 2) * g.grid_shape[1] + Math.floor(g.grid_shape[1] / 2) : 0;
  pick(center);
  return h("div", { class: "grid-2 viewers" },
    card({ title: "Scan geometry", iconName: "grid", sub: g.nm_per_V ? `${g.nm_per_V.toFixed(4)} nm/V` : null, body: scanView.el }),
    card({ title: "Diffraction pattern", iconName: "crosshair", body: frameView.el }));
}

function pick(index) {
  scanView && scanView.setSelected(index);
  frameView && frameView.setIndex(index);
}

function acquisition(data) {
  if (!data.summary.length) return null;
  const groups = {};
  for (const row of data.summary) (groups[row.group] ||= []).push(row);
  return card({
    title: "Acquisition", iconName: "pulse", sub: "as reported by print_diffraction_acquisition_summary",
    body: h("div", { class: "kv" }, Object.entries(groups).map(([name, rows]) =>
      h("dl", { class: "kv-group", style: { margin: 0 } },
        h("h4", null, name),
        rows.map((r) => h("div", { class: "kv-row" },
          h("dt", null, r.label), h("dd", null, r.value, r.unit && h("span", { class: "unit" }, r.unit)))))))
  });
}

function outputs(data) {
  if (!data.outputs.length) return null;
  return card({
    title: "Earlier conversions", iconName: "layers", sub: `${data.outputs.length} found next to this file`,
    body: h("div", { style: { display: "grid", gap: "16px" } }, data.outputs.slice(0, 4).map((out) => {
      const items = out.figures.map((f) => ({ src: api.outputUrl(f.path, out.mtime), label: FIGURE_LABELS[f.kind], sub: fmt.basename(out.folder) }));
      return h("div", { style: { display: "grid", gap: "8px" } },
        h("div", { class: "path-row" },
          icon("folder", "sm"),
          h("span", { class: "mono", title: out.folder }, `‎${out.folder}`),
          h("span", { class: "faint", style: { whiteSpace: "nowrap", fontSize: "12px" } }, fmt.datetime(out.mtime)),
          (out.output_file || out.h5) && h("span", { class: "badge ok" }, out.output_format || ".h5"),
          h("button", { class: "icon-btn sm", title: "Reveal", "aria-label": "Reveal folder", onClick: () => api.reveal(out.output_file || out.h5 || out.folder) }, icon("reveal", "sm"))),
        items.length > 0 && h("div", { class: "thumbs", style: { gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))" } },
          items.map((item, i) => h("button", { class: "thumb", onClick: () => openLightbox(items, i) },
            h("div", { class: "thumb-img" }, h("img", { src: item.src, alt: item.label, loading: "lazy" })),
            h("div", { class: "thumb-label" }, item.label)))));
    })),
  });
}

function metadata(data) {
  const sources = Object.keys(data.metadata);
  if (!sources.length) return null;
  if (!sources.includes(metaSource)) metaSource = sources[0];
  const tbody = h("tbody");
  const fill = () => {
    const needle = metaFilter.trim().toLowerCase();
    const rows = data.metadata[metaSource].filter((r) => !needle || r.key.toLowerCase().includes(needle) || r.value.toLowerCase().includes(needle));
    clear(tbody, rows.length ? rows.map((r) => {
      const cut = r.key.lastIndexOf(".") + 1;
      return h("tr", null,
        h("td", { class: "mono" }, h("span", { class: "key-prefix" }, r.key.slice(0, cut)), h("span", { class: "key-leaf" }, r.key.slice(cut))),
        h("td", { class: "type-cell" }, h("span", { class: "type-tag" }, r.type)),
        h("td", { class: "value-cell" }, r.value));
    }) : h("tr", null, h("td", { colspan: "3", class: "faint", style: { textAlign: "center", padding: "20px" } }, "No matching fields")));
  };
  fill();
  const seg = () => segmented(sources.map((s) => ({ value: s, label: `${s} · ${data.metadata[s].length}` })), metaSource, (s) => {
    metaSource = s;
    segHost.replaceChildren(seg());
    fill();
  }, { label: "Metadata source" });
  const segHost = h("span", null, seg());
  return card({
    title: "Metadata", iconName: "code", sub: "decoded with unpack_diffraction_meta_all",
    actions: [segHost,
      h("div", { class: "input-wrap", style: { width: "200px" } }, icon("search", "sm"),
        h("input", { class: "input", type: "search", placeholder: "Search fields", value: metaFilter, "aria-label": "Search metadata",
          style: { height: "28px" }, onInput: (e) => { metaFilter = e.target.value; fill(); } }))],
    bodyClass: "card-body flush",
    body: h("div", { class: "table-wrap" },
      h("table", { class: "data" },
        h("thead", null, h("tr", null, h("th", null, "Field"), h("th", null, "Type"), h("th", null, "Value"))),
        tbody)),
  });
}

function datasets(data) {
  return card({
    title: "HDF5 datasets", iconName: "layers", sub: `${data.datasets.length} in file`,
    bodyClass: "card-body flush",
    body: h("div", { class: "table-wrap" },
      h("table", { class: "data" },
        h("thead", null, h("tr", null, ["Path", "Shape", "dtype", "Size", "Chunks"].map((t) => h("th", null, t)))),
        h("tbody", null, data.datasets.map((d) => h("tr", null,
          h("td", { class: "mono" }, d.path),
          h("td", { class: "mono" }, d.shape.length ? d.shape.join(" × ") : "scalar"),
          h("td", { class: "mono" }, d.dtype),
          h("td", { class: "mono" }, d.nbytes == null ? "—" : fmt.bytes(d.nbytes)),
          h("td", { class: "mono faint" }, d.chunks ? d.chunks.join(" × ") : "—", d.compression ? ` · ${d.compression}` : "")))))),
  });
}

function render() {
  if (!state.focused) {
    destroyViewers();
    return clear(root, emptyState());
  }
  if (error) {
    return clear(root, h("div", { class: "page" },
      header({ path: state.focused, name: fmt.basename(state.focused), geometry: {}, size: null, mtime: null }),
      callout("fail", "Could not inspect this file", error),
      h("div", { style: { display: "flex", gap: "8px" } },
        h("button", { class: "btn", onClick: () => load(state.focused) }, icon("retry"), "Try again"),
        h("button", { class: "btn ghost", onClick: () => update({ focused: null }) }, "Close"))));
  }
  const data = state.inspection;
  if (!data || data.path !== state.focused) return clear(root, skeleton());

  const envIssue = state.env && !state.env.converter_ok && !state.env.backends?.quantem?.available;
  clear(root, h("div", { class: "page" },
    envIssue && callout("fail", "The converter can't be imported in this Python environment", state.env.converter_error),
    header(data),
    checks(data),
    viewers(data),
    acquisition(data),
    outputs(data),
    metadata(data),
    datasets(data)));
}

export function initInspect() {
  subscribe((keys) => {
    if (keys.includes("focused") && state.focused !== loadedPath) {
      if (state.focused) load(state.focused);
      else { loadedPath = null; render(); }
    } else if (keys.includes("env") && !state.focused) {
      render();
    } else if (keys.includes("settings") && frameView) {
      frameView.draw();
    }
  });
  if (state.focused) load(state.focused);
  else render();
}

export function reloadInspection() {
  if (state.focused) load(state.focused);
}

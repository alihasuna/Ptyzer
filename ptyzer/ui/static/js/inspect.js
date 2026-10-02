// Inspect view: pre-flight checks, scan geometry, diffraction patterns, acquisition summary,
// earlier outputs, decoded metadata and HDF5 layout for the focused .hp file.

import { api } from "./api.js";
import { generateSample, navigate } from "./browser.js";
import { button, callout, card, clear, copyText, fmt, h, pathText, scrollRegion, segmented, status, toast } from "./dom.js";
import { figureGrid } from "./lightbox.js";
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
const CHECK_STATUS = { ok: ["ok", "OK"], warn: ["warn", "Check"], fail: ["error", "Fail"] };

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

function label(text) {
  return h("p", { class: "es-label" }, h("span", { class: "es-plus", "aria-hidden": "true" }, "+"), text);
}

function emptyState() {
  return h("div", { class: "pz-page pz-empty" },
    label("Inspect"),
    h("h1", { class: "es-h1" }, "Inspect an Azorus dataset."),
    h("p", { class: "es-lede" }, "Pick a .hp file under Files to check its metadata, scan geometry and diffraction patterns, then convert it with py4DSTEM or Quantem, with QC figures and a parallax aberration fit."),
    h("div", { class: "pz-actions pz-actions--lg" },
      button("Try a sample dataset", generateSample, { variant: "dark" }),
      state.env && button("Open launch folder", () => navigate(state.env.start_dir))));
}

function skeleton() {
  return h("div", { class: "pz-page", "aria-busy": "true" },
    h("p", { class: "es-sr-only", role: "status" }, "Loading file…"),
    h("div", { class: "pz-skeleton", style: { height: "24px", width: "120px" } }),
    h("div", { class: "pz-skeleton", style: { height: "40px", width: "min(420px, 100%)" } }),
    h("div", { class: "pz-grid-2" },
      h("div", { class: "pz-skeleton", style: { aspectRatio: "1 / 1.1" } }),
      h("div", { class: "pz-skeleton", style: { aspectRatio: "1 / 1.1" } })));
}

function header(data) {
  const g = data.geometry || {};
  const facts = [
    ["Scan", g.grid_shape && `${g.grid_shape[0]} × ${g.grid_shape[1]}`],
    ["Frames", g.dp_shape && fmt.int(g.dp_shape[0])],
    ["Detector", g.dp_shape && `${g.dp_shape[1]} × ${g.dp_shape[2]} px`],
    ["Step", g.R_pixel_size_nm && `${(g.R_pixel_size_nm * 1000).toFixed(2)} pm`],
    ["dtype", g.dp_dtype],
    ["On disk", data.size != null && fmt.bytes(data.size)],
    ["Modified", data.mtime && fmt.datetime(data.mtime)],
  ].filter(([, v]) => v);
  const lede = g.grid_shape && g.dp_shape
    ? `${g.grid_shape[0]} × ${g.grid_shape[1]} scan, ${g.dp_shape[1]} × ${g.dp_shape[2]} detector.`
    : null;
  return h("header", { class: "pz-page-head" },
    label("Inspect"),
    h("div", { class: "pz-title-row" },
      h("h1", { class: "es-h1 pz-title" }, `${data.name}.`),
      h("div", { class: "pz-actions" },
        button("Copy path", () => copyText(data.path, "Path copied"), { size: "sm" }),
        button("Reveal", () => api.reveal(data.path).catch((e) => toast(e.message, { kind: "fail" })), { size: "sm", ariaLabel: "Reveal file in the file manager" }),
        button("Reload", () => load(data.path), { size: "sm", variant: "quiet" }))),
    lede && h("p", { class: "es-lede" }, lede),
    h("p", { class: "pz-path-line" }, pathText(fmt.dirname(data.path))),
    facts.length > 0 && h("dl", { class: "pz-facts" }, facts.map(([k, v]) =>
      h("div", null, h("dt", null, k), h("dd", { class: "es-num" }, v)))));
}

function checks(data) {
  const order = { fail: 0, warn: 1, ok: 2 };
  const rows = [...data.checks].sort((a, b) => order[a.status] - order[b.status]);
  const fails = rows.filter((c) => c.status === "fail").length;
  const warns = rows.filter((c) => c.status === "warn").length;
  const overall = fails ? status("error", "Will fail") : warns ? status("warn", "Warnings") : status("ok", "Passed");
  return card({
    title: "Pre-flight checks", actions: overall,
    body: scrollRegion("Pre-flight checks", h("table", { class: "es-table" },
      h("thead", null, h("tr", null, h("th", { scope: "col" }, "Check"), h("th", { scope: "col" }, "Result"), h("th", { scope: "col" }, "Detail"))),
      h("tbody", null, rows.map((c) => {
        const [kind, word] = CHECK_STATUS[c.status] || ["idle", c.status];
        return h("tr", { class: c.status !== "ok" ? "pz-flag" : null },
          h("td", null, c.title), h("td", null, status(kind, word)), h("td", null, c.detail || "—"));
      })))),
  });
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
  return h("div", { class: "pz-grid-2" },
    card({ title: "Scan geometry", sub: g.nm_per_V ? `${g.nm_per_V.toFixed(4)} nm/V` : null, body: scanView.el }),
    card({ title: "Diffraction pattern", body: frameView.el }));
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
    title: "Acquisition", sub: "as print_diffraction_acquisition_summary reports it", quiet: true,
    body: h("div", { class: "pz-spec-groups" }, Object.entries(groups).map(([name, rows]) =>
      h("div", null,
        h("h3", { class: "pz-subhead" }, name),
        h("ul", { class: "es-spec" }, rows.map((r) => h("li", null,
          h("b", null, r.label), h("span", { class: "es-num" }, r.value, r.unit ? ` ${r.unit}` : ""))))))),
  });
}

function outputs(data) {
  if (!data.outputs.length) return null;
  return card({
    title: "Earlier conversions", sub: `${data.outputs.length} next to this file`,
    body: h("div", { class: "pz-stack" }, data.outputs.slice(0, 4).map((out) => {
      const items = out.figures.map((f) => ({ src: api.outputUrl(f.path, out.mtime), label: FIGURE_LABELS[f.kind], sub: fmt.basename(out.folder) }));
      return h("div", { class: "pz-output" },
        h("div", { class: "pz-path-row" },
          pathText(out.folder),
          h("span", { class: "es-caption es-num" }, fmt.datetime(out.mtime)),
          (out.output_file || out.h5) && h("span", { class: "pz-tag" }, out.output_format || ".h5"),
          button("Reveal", () => api.reveal(out.output_file || out.h5 || out.folder), { size: "sm", variant: "quiet", ariaLabel: "Reveal output folder" })),
        items.length > 0 && figureGrid(items));
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
        h("td", { class: "es-num pz-key" }, h("span", { class: "pz-key-prefix" }, r.key.slice(0, cut)), r.key.slice(cut)),
        h("td", { class: "es-num pz-type" }, r.type),
        h("td", { class: "pz-value" }, r.value));
    }) : h("tr", null, h("td", { colspan: "3", class: "es-caption" }, "No matching fields")));
  };
  fill();
  const seg = () => segmented(sources.map((s) => ({ value: s, label: `${s} · ${data.metadata[s].length}` })), metaSource, (s) => {
    metaSource = s;
    fill();
  }, { label: "Metadata source", name: "meta-source" });
  return card({
    title: "Metadata", sub: "decoded with unpack_diffraction_meta_all",
    body: [
      h("div", { class: "pz-toolbar" }, seg(),
        h("div", { class: "es-field pz-search" },
          h("label", { for: "metaSearch" }, "Search fields"),
          h("input", { id: "metaSearch", class: "es-input", type: "search", value: metaFilter, autocomplete: "off",
            onInput: (e) => { metaFilter = e.target.value; fill(); } }))),
      scrollRegion("Metadata fields", h("table", { class: "es-table" },
        h("thead", null, h("tr", null, h("th", { scope: "col" }, "Field"), h("th", { scope: "col" }, "Type"), h("th", { scope: "col" }, "Value"))),
        tbody), "pz-table-wrap pz-table-wrap--tall"),
    ],
  });
}

function datasets(data) {
  return card({
    title: "HDF5 datasets", sub: `${data.datasets.length} in file`, quiet: true,
    body: scrollRegion("HDF5 datasets", h("table", { class: "es-table" },
      h("thead", null, h("tr", null, ["Path", "Shape", "dtype", "Size", "Chunks"].map((t) => h("th", { scope: "col" }, t)))),
      h("tbody", null, data.datasets.map((d) => h("tr", null,
        h("td", { class: "es-num" }, d.path),
        h("td", { class: "es-num" }, d.shape.length ? d.shape.join(" × ") : "scalar"),
        h("td", { class: "es-num" }, d.dtype),
        h("td", { class: "es-num" }, d.nbytes == null ? "—" : fmt.bytes(d.nbytes)),
        h("td", { class: "es-num" }, d.chunks ? d.chunks.join(" × ") : "—", d.compression ? ` · ${d.compression}` : "")))))),
  });
}

function render() {
  if (!state.focused) {
    destroyViewers();
    return clear(root, emptyState());
  }
  if (error) {
    return clear(root, h("div", { class: "pz-page" },
      header({ path: state.focused, name: fmt.basename(state.focused), geometry: {}, size: null, mtime: null }),
      callout("fail", "Could not inspect this file", error),
      h("div", { class: "pz-actions" },
        button("Try again", () => load(state.focused)),
        button("Close", () => update({ focused: null }), { variant: "quiet" }))));
  }
  const data = state.inspection;
  if (!data || data.path !== state.focused) return clear(root, skeleton());

  const envIssue = state.env && !state.env.converter_ok && !state.env.backends?.quantem?.available;
  clear(root, h("div", { class: "pz-page" },
    envIssue && callout("fail", "The converter can't be imported in this Python environment", state.env.converter_error),
    header(data),
    h("div", { class: "pz-grid-2 pz-grid-2--wide" }, checks(data), acquisition(data)),
    viewers(data),
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

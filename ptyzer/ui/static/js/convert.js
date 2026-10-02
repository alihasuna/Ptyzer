// Conversion settings panel: maps one-to-one onto azohp_to_py4d's arguments, queues runs for the
// focused file or the batch selection, and can export the equivalent Python call.

import { api } from "./api.js";
import { button, callout, checkRow, clear, copyText, fmt, h, segmented, toast } from "./dom.js";
import { refreshJobs, showRuns } from "./jobstore.js";
import { state, storage, subscribe, update } from "./state.js";

const root = document.getElementById("convert");
let settings = null;
let manualRadius = storage.get("manualRadius", 20);
let busy = false;
// Where each target file would write, as the server resolves it (read-only source folders fall
// back to the output root): { key, byFile: { path: { dir, writable, fallback } } }.
let targetInfo = { key: null, byFile: {} };

const PRESETS = {
  quick: { do_save: false, parallax: false, aberrations: false, do_recentering: false,
           plot_coord_checks: true, plot_overview: true, plot_virtual_diff: true, plot_parallax_recon: false },
  full: { do_save: true, parallax: true, aberrations: true, do_recentering: false,
          plot_coord_checks: true, plot_overview: true, plot_virtual_diff: true, plot_parallax_recon: true },
};

const FIGURES = [
  ["plot_coord_checks", "Scan-coordinate checks", "Raw vs calibrated coordinates and the reshaped x/y grids"],
  ["plot_overview", "Overview & scan mesh", "Overview micrograph with the scan region outlined"],
  ["plot_virtual_diff", "Virtual images & diffraction", "Bright/dark field images, mean and max patterns"],
  ["plot_parallax_recon", "Parallax summary", "Aligned bright field, shifts and fitted aberrations"],
];

function presetOf(s) {
  for (const [name, preset] of Object.entries(PRESETS)) {
    if (Object.entries(preset).every(([k, v]) => s[k] === v)) return name;
  }
  return "custom";
}

function set(patch) {
  settings = { ...settings, ...patch };
  if (patch.aberrations === true) settings.parallax = true;
  if (patch.parallax === false) settings.aberrations = false;
  storage.set("settings", settings);
  update({ settings });
}

export function targets() {
  if (state.selected.size) return [...state.selected];
  return state.focused ? [state.focused] : [];
}

const py = (b) => (b ? "True" : "False");
const lit = (s) => JSON.stringify(s);

export function pythonSnippet(files, p, outputDir) {
  if (p.backend === "quantem") {
    const dest = outputDir || p.output_dir || null;
    return `import json\nfrom ptyzer.ui.quantem_backend import run_quantem\n\n` +
      `params = json.loads(${lit(JSON.stringify(p))})\n` +
      `files = json.loads(${lit(JSON.stringify(files))})\n` +
      `for file in files:\n    result = run_quantem(file, ${dest ? lit(dest) : "None"}, params)\n`;
  }
  const savepath = outputDir ? lit(outputDir) : p.output_dir ? lit(p.output_dir) : "None";
  const kwargs = [
    `savepathname=${savepath},`,
    `beam_kV=${p.beam_kV},`,
    `recon_pix_size=${p.recon_pix_size_pm}e-12,`,
    `do_recentering=${py(p.do_recentering)},`,
    `centre_method=${lit(p.centre_method)},`,
    `do_save=${py(p.do_save)},`,
    `get_parallax_plots=${py(p.parallax || p.aberrations)},`,
    `get_parallax_aberrations=${py(p.aberrations)},`,
    `bf_disk_radius=${p.bf_disk_radius == null ? "None" : p.bf_disk_radius},`,
    `plot_coord_checks=${py(p.plot_coord_checks)},`,
    `plot_overview=${py(p.plot_overview)},`,
    `plot_virtual_diff=${py(p.plot_virtual_diff)},`,
    `plot_parallax_recon=${py(p.plot_parallax_recon)},`,
    `force_transpose=${py(p.force_transpose)},`,
  ];
  const head = "from ptyzer.io.azohp_to_py4d import azohp_to_py4d\n\n";
  if (files.length === 1) {
    return `${head}datacube = azohp_to_py4d(\n    ${lit(files[0])},\n    ${kwargs.join("\n    ")}\n)\n`;
  }
  return `${head}files = [\n${files.map((f) => `    ${lit(f)},`).join("\n")}\n]\n\nfor loadupname in files:\n` +
    `    datacube = azohp_to_py4d(\n        loadupname,\n        ${kwargs.join("\n        ")}\n    )\n`;
}

function refreshTargets(files) {
  const key = JSON.stringify([files, settings.backend, settings.output_dir]);
  if (key === targetInfo.key) return;
  targetInfo = { key, byFile: {} };
  Promise.all(files.slice(0, 200).map((f) => api.outputTarget(f, settings.backend, settings.output_dir)
    .then((t) => [f, t]).catch(() => [f, null])))
    .then((pairs) => {
      if (targetInfo.key !== key) return;
      targetInfo = { key, byFile: Object.fromEntries(pairs.filter(([, t]) => t)) };
      render();
    });
}

function outputPreview(file) {
  const sep = (state.env && state.env.sep) || "/";
  const known = targetInfo.byFile[file];
  const base = known ? known.dir : settings.output_dir || `${fmt.dirname(file)}${sep}${settings.backend === "quantem" ? "ReformattedForQuantem" : "ReformattedForPy4DSTEM"}`;
  const stem = fmt.basename(file).replace(/\.hp$/i, "");
  return settings.per_run_folder ? `${base}${sep}${stem}_<date-time>` : base;
}

function compatIssues(area) {
  if (settings?.backend === "quantem") return [];
  return ((state.env && state.env.checks) || []).filter((c) => c.affects.includes(area));
}

async function submit() {
  const files = targets();
  if (!files.length || busy) return;
  busy = true;
  render();
  try {
    const { jobs } = await api.submit(files, settings);
    await refreshJobs();
    showRuns(jobs[0].id);
    toast(`Queued ${jobs.length} conversion${jobs.length === 1 ? "" : "s"}`, { kind: "ok" });
  } catch (err) {
    toast(err.message, { kind: "fail", timeout: 6000 });
  } finally {
    busy = false;
    render();
  }
}

function toggleRow(id, label, checked, onChange, { hint, disabled, issues = [] } = {}) {
  return h("div", { class: "pz-field-group" },
    checkRow(id, label, checked && !disabled, onChange, { hint, disabled }),
    checked && issues.map((c) => callout(c.status, c.title, c.detail)));
}

function unitInput(id, value, unit, onChange, { min = 0, step = "any", ariaLabel, describedBy } = {}) {
  return h("div", { class: "pz-unit-input" },
    h("input", {
      id, class: "es-input es-num", type: "number", min: String(min), step, value: String(value), inputmode: "decimal",
      "aria-label": ariaLabel, "aria-describedby": describedBy, onChange,
    }),
    h("span", { class: "pz-unit", "aria-hidden": "true" }, unit));
}

function numberField(id, label, value, unit, onCommit, { hint, extra } = {}) {
  const hintId = hint ? `${id}-help` : null;
  return h("div", { class: "es-field" },
    h("label", { for: id }, label, h("span", { class: "es-sr-only" }, ` (${unit})`)),
    unitInput(id, value, unit, (e) => {
      const v = parseFloat(e.target.value);
      if (Number.isFinite(v) && v > 0) onCommit(v);
      else { e.target.value = String(value); toast(`${label} must be a positive number`, { kind: "fail" }); }
    }, { describedBy: hintId }),
    extra,
    hint && h("span", { class: "es-help", id: hintId }, hint));
}

function section(title, ...children) {
  const id = `conv-${title.toLowerCase().replace(/[^a-z]+/g, "-")}`;
  return h("section", { class: "pz-convert-section", "aria-labelledby": id },
    h("h3", { class: "pz-subhead", id }, title), children);
}

function render() {
  if (!settings) {
    return clear(root, h("h2", { class: "es-sr-only", id: "convertTitle" }, "Convert"),
      h("div", { class: "pz-convert-body", "aria-busy": "true" },
        [120, 80, 180, 140].map((height) => h("div", { class: "pz-skeleton", style: { height: `${height}px` } }))));
  }
  const focusId = document.activeElement && root.contains(document.activeElement) ? document.activeElement.id : null;
  const scrollTop = root.querySelector(".pz-convert-body")?.scrollTop || 0;

  const s = settings;
  const parallaxOn = s.parallax || s.aberrations;
  const suggestedKV = state.inspection && state.inspection.path === state.focused && state.inspection.suggested.beam_kV;
  const files = targets();
  const env = state.env;
  const quantem = s.backend === "quantem";
  const engine = env?.backends?.[s.backend || "py4dstem"];
  const blocked = engine ? !engine.available : env && !env.converter_ok;

  const head = h("div", { class: "pz-convert-head" },
    h("div", { class: "es-section-head" },
      h("h2", { class: "es-h2", id: "convertTitle" }, "Convert"),
      h("span", { class: "es-label" }, quantem ? "Quantem · experimental" : "py4DSTEM")),
    segmented([
      { value: "quick", label: "Quick look", title: "QC figures only, nothing saved" },
      { value: "full", label: "Full pipeline", title: "Figures, parallax, aberration fit and native dataset" },
      { value: "custom", label: "Custom", disabled: true },
    ], presetOf(s), (name) => PRESETS[name] && set(PRESETS[name]), { block: true, label: "Preset", name: "preset" }));

  const body = h("div", { class: "pz-convert-body" },
    blocked && callout("fail", "Converter unavailable", engine?.error || env.converter_error),

    section("Processing engine",
      h("div", { class: "es-field" },
        h("label", { for: "opt-backend" }, "Engine"),
        h("select", { id: "opt-backend", class: "es-select", "aria-describedby": quantem ? "opt-backend-help" : null,
          onChange: (e) => set({ backend: e.target.value }) },
          h("option", { value: "py4dstem", selected: !quantem }, "py4DSTEM"),
          h("option", { value: "quantem", selected: quantem }, "Quantem (experimental)")),
        quantem && h("span", { class: "es-help", id: "opt-backend-help" },
          "Native Zarr export. Different fitting method: compare results on experimental reference data before replacing py4DSTEM."))),

    section("Output",
      toggleRow("opt-save", quantem ? "Save Quantem .zarr.zip" : "Save py4DSTEM .h5", s.do_save, (v) => set({ do_save: v }),
        { hint: "Data cube with embedded Azorus metadata, scan coordinates and parallax results" }),
      h("div", { class: "es-field" },
        h("label", { for: "opt-outdir" }, "Output folder"),
        h("input", {
          id: "opt-outdir", class: "es-input es-num", value: s.output_dir, "aria-describedby": "opt-outdir-help",
          onChange: (e) => set({ output_dir: e.target.value.trim() }),
        }),
        h("span", { class: "es-help", id: "opt-outdir-help" },
          `Leave empty to write next to each file, in ${quantem ? "ReformattedForQuantem" : "ReformattedForPy4DSTEM"}.`)),
      toggleRow("opt-perrun", "New subfolder for each run", s.per_run_folder, (v) => set({ per_run_folder: v }),
        { hint: "Keeps earlier results instead of overwriting files with the same names" })),

    section("Calibration",
      numberField("opt-kv", "Beam energy", s.beam_kV, "kV", (v) => set({ beam_kV: v }), {
        extra: suggestedKV && suggestedKV !== s.beam_kV && button(`Use ${suggestedKV} kV from file`,
          () => set({ beam_kV: suggestedKV }), { size: "sm", variant: "quiet", title: "Gun high voltage recorded in this file" }),
      }),
      numberField("opt-pix", "Reconstruction pixel size", s.recon_pix_size_pm, "pm", (v) => set({ recon_pix_size_pm: v }),
        { hint: "Sets the reciprocal-space calibration (λ / pixel size across the detector)" }),
      h("div", { class: "es-field" },
        h("span", { class: "es-field-label", id: "opt-radius-label" }, "Bright-field disk radius"),
        segmented([{ value: "auto", label: "Estimate" }, { value: "manual", label: "Set manually" }],
          s.bf_disk_radius == null ? "auto" : "manual",
          (mode) => set({ bf_disk_radius: mode === "auto" ? null : manualRadius }), { block: true, label: "Bright-field disk radius", name: "radius-mode" }),
        s.bf_disk_radius != null && unitInput("opt-radius", s.bf_disk_radius, "px", (e) => {
          const v = parseFloat(e.target.value);
          if (Number.isFinite(v) && v > 0) { manualRadius = v; storage.set("manualRadius", v); set({ bf_disk_radius: v }); }
        }, { min: 1, step: "0.5", ariaLabel: "Bright-field disk radius in pixels" }),
        h("span", { class: "es-help" }, s.bf_disk_radius == null
          ? "Estimated from the mean pattern (×1.2 for the virtual detectors)"
          : "Drawn on the diffraction pattern viewer; used for the virtual detectors"))),

    section("Preprocessing",
      toggleRow("opt-recenter", "Recenter diffraction patterns", s.do_recentering, (v) => set({ do_recentering: v }),
        { hint: quantem ? "Bilinear shifts from centre-of-mass estimates; differs from py4DSTEM disk fitting" : "Shift every pattern so the central beam sits at the detector centre" }),
      s.do_recentering && h("div", { class: "es-field" },
        h("span", { class: "es-field-label" }, "Centre estimate"),
        segmented([
          { value: "fit", label: "Plane fit per position" },
          { value: "simple", label: "Single global centre" },
        ], s.centre_method, (v) => set({ centre_method: v }), { block: true, label: "Centre estimate", name: "centre-method" }))),

    section("Parallax",
      toggleRow("opt-parallax", "Parallax reconstruction", parallaxOn, (v) => set({ parallax: v }),
        { hint: quantem ? "Cross-correlation alignment and direct parallax reconstruction" : "Align bright-field images across the detector (py4DSTEM Parallax)", issues: compatIssues("parallax") }),
      toggleRow("opt-aberr", "Fit aberrations", s.aberrations, (v) => set({ aberrations: v }),
        { hint: quantem ? "Rotation, defocus C10 and astigmatism C12 from the cross-correlation shifts" : "Defocus, astigmatism, Cs and the scan/detector rotation from the measured shifts", issues: compatIssues("aberrations") }),
      toggleRow("opt-transpose", "Mirrored scan (transpose)", s.force_transpose, (v) => set({ force_transpose: v }),
        { disabled: quantem ? !parallaxOn : !s.aberrations, hint: "Scan and detector have opposite handedness. One dataset can't reveal this (a mirrored defocus fits as astigmatism), so set it once per instrument from a defocus series" }),
      quantem && toggleRow("opt-ls", "Fourier-phase refinement (unvalidated)", s.quantem_least_squares, (v) => set({ quantem_least_squares: v }),
        { disabled: !s.aberrations, hint: "Quantem least squares adds Cs and higher orders. Not yet validated: on the synthetic sample it drives defocus to ~0" })),

    section("QC figures",
      FIGURES.map(([key, label, hint]) => {
        const disabled = key === "plot_parallax_recon" && !parallaxOn;
        return checkRow(`fig-${key}`, label, s[key] && !disabled, (v) => set({ [key]: v }),
          { disabled, hint: disabled ? "Needs parallax reconstruction" : hint });
      }),
      s.plot_parallax_recon && parallaxOn && compatIssues("parallax_figure").map((c) => callout(c.status, c.title, c.detail))));

  refreshTargets(files);
  const known = files.map((f) => targetInfo.byFile[f]).filter(Boolean);
  const unwritable = known.filter((t) => !t.writable);
  const fallbacks = known.filter((t) => t.fallback && t.writable);
  const multi = files.length > 1;
  const foot = h("div", { class: "pz-convert-foot" },
    unwritable.length > 0 && callout("fail", "Output folder isn't writable",
      `${unwritable[0].dir}${unwritable.length > 1 ? ` and ${unwritable.length - 1} more` : ""}. Set an output folder you can write to.`),
    fallbacks.length > 0 && callout("warn", "Source folder is read-only",
      fallbacks.length === 1 ? `Results will go to ${fallbacks[0].dir}.`
        : `${fallbacks.length} files are in read-only folders; their results go under ${state.env?.output_root || "the output root"}.`),
    h("p", { class: "pz-target", title: files.length === 1 ? outputPreview(files[0]) : "" },
      h("span", { class: "es-field-label" }, "Writes to"),
      h("span", { class: files.length === 1 ? "es-num pz-path" : "" },
        files.length === 0 ? "Select a .hp file to convert"
          : multi ? `${files.length} files from the batch selection`
          : `‎${outputPreview(files[0])}`)),
    h("div", { class: "pz-actions" },
      button(busy ? "Queuing…" : files.length > 1 ? `Convert ${files.length} files` : "Convert", submit,
        { variant: "dark", disabled: !files.length || busy || blocked || unwritable.length > 0 }),
      button("Copy as Python", () => copyText(pythonSnippet(files, s), "Python call copied"),
        { disabled: !files.length, title: "Copy the equivalent Python call" })));

  clear(root, head, body, foot);
  root.querySelector(".pz-convert-body").scrollTop = scrollTop;
  if (focusId) document.getElementById(focusId)?.focus();
}

// Load a run's parameters into the panel (e.g. to change the output folder of a failed run before
// running it again) and put the cursor in the output folder field.
export function loadSettings(params) {
  const defaults = (state.env && state.env.defaults) || {};
  settings = { ...defaults };
  for (const key of Object.keys(defaults)) if (key in params) settings[key] = params[key];
  storage.set("settings", settings);
  update({ settings });
  requestAnimationFrame(() => {
    const field = document.getElementById("opt-outdir");
    if (field) { field.scrollIntoView({ block: "center" }); field.focus(); }
  });
}

export function initConvert() {
  subscribe((keys) => {
    if (keys.includes("env") && state.env && !settings) {
      const defaults = state.env.defaults || {};
      const saved = storage.get("settings", {});
      settings = { ...defaults };
      for (const key of Object.keys(defaults)) if (key in saved) settings[key] = saved[key];
      update({ settings });
    }
    if (["env", "settings", "focused", "selected", "inspection"].some((k) => keys.includes(k))) render();
  });
  render();
}

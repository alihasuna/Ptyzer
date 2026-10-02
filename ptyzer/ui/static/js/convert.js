// Conversion settings panel: maps one-to-one onto azohp_to_py4d's arguments, queues runs for the
// focused file or the batch selection, and can export the equivalent Python call.

import { api } from "./api.js";
import { callout, clear, copyText, fmt, h, icon, segmented, switchControl, toast } from "./dom.js";
import { refreshJobs, showRuns } from "./jobstore.js";
import { state, storage, subscribe, update } from "./state.js";

const root = document.getElementById("convert");
let settings = null;
let manualRadius = storage.get("manualRadius", 20);
let busy = false;

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

function outputPreview(file) {
  const sep = (state.env && state.env.sep) || "/";
  const base = settings.output_dir || `${fmt.dirname(file)}${sep}${settings.backend === "quantem" ? "ReformattedForQuantem" : "ReformattedForPy4DSTEM"}`;
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
  return h("div", { class: "field" },
    h("div", { class: "toggle-row" },
      h("label", { for: id }, label),
      switchControl(checked, onChange, { id, disabled }),
      hint && h("div", { class: "hint" }, hint)),
    checked && issues.map((c) => callout(c.status, c.title, c.detail)));
}

function numberField(id, label, value, unit, onCommit, { hint, min = 0, step = "any", extra } = {}) {
  return h("div", { class: "field" },
    h("label", { class: "field-label", for: id }, label, extra),
    h("div", { class: "input-wrap has-unit" },
      h("input", {
        id, class: "input mono", type: "number", min: String(min), step, value: String(value), style: { paddingLeft: "10px" },
        onChange: (e) => {
          const v = parseFloat(e.target.value);
          if (Number.isFinite(v) && v > 0) onCommit(v);
          else { e.target.value = String(value); toast(`${label} must be a positive number`, { kind: "fail" }); }
        },
      }),
      h("span", { class: "input-unit" }, unit)),
    hint && h("div", { class: "hint" }, hint));
}

function render() {
  if (!settings) {
    return clear(root, h("div", { class: "convert-body", style: { paddingTop: "16px", display: "grid", gap: "12px" } },
      [120, 80, 180, 140].map((height) => h("div", { class: "skeleton", style: { height: `${height}px` } }))));
  }
  const focusId = document.activeElement && root.contains(document.activeElement) ? document.activeElement.id : null;
  const scrollTop = root.querySelector(".convert-body")?.scrollTop || 0;

  const s = settings;
  const parallaxOn = s.parallax || s.aberrations;
  const suggestedKV = state.inspection && state.inspection.path === state.focused && state.inspection.suggested.beam_kV;
  const files = targets();
  const env = state.env;
  const quantem = s.backend === "quantem";
  const engine = env?.backends?.[s.backend || "py4dstem"];
  const blocked = engine ? !engine.available : env && !env.converter_ok;

  const head = h("div", { class: "convert-head" },
    h("div", { class: "convert-title" }, h("h2", null, "Convert"), h("span", { class: "mono faint" }, quantem ? "Quantem · experimental" : "py4DSTEM")),
    segmented([
      { value: "quick", label: "Quick look", title: "QC figures only, nothing saved" },
      { value: "full", label: "Full pipeline", title: "Figures, parallax, aberration fit and native dataset" },
      { value: "custom", label: "Custom", disabled: true },
    ], presetOf(s), (name) => PRESETS[name] && set(PRESETS[name]), { block: true, label: "Preset" }));

  const body = h("div", { class: "convert-body" },
    blocked && h("div", { style: { paddingTop: "14px" } }, callout("fail", "Converter unavailable", engine?.error || env.converter_error)),

    h("section", { class: "section" },
      h("h3", null, "Processing engine"),
      h("label", { class: "field-label", for: "opt-backend" }, "Engine"),
      h("select", { id: "opt-backend", class: "input", onChange: (e) => set({ backend: e.target.value }) },
        h("option", { value: "py4dstem", selected: !quantem }, "py4DSTEM"),
        h("option", { value: "quantem", selected: quantem }, "Quantem (experimental)")),
      quantem && h("div", { class: "hint" }, "Native Zarr export. Different fitting method: compare results on experimental reference data before replacing py4DSTEM.")),
    h("section", { class: "section" },
      h("h3", null, "Output"),
      toggleRow("opt-save", quantem ? "Save Quantem .zarr.zip" : "Save py4DSTEM .h5", s.do_save, (v) => set({ do_save: v }),
        { hint: "Data cube with embedded Azorus metadata, scan coordinates and parallax results" }),
      h("div", { class: "field" },
        h("label", { class: "field-label", for: "opt-outdir" }, "Output folder"),
        h("input", {
          id: "opt-outdir", class: "input mono", placeholder: `Next to each file / ${quantem ? "ReformattedForQuantem" : "ReformattedForPy4DSTEM"}`, value: s.output_dir,
          onChange: (e) => set({ output_dir: e.target.value.trim() }),
        })),
      toggleRow("opt-perrun", "New subfolder for each run", s.per_run_folder, (v) => set({ per_run_folder: v }),
        { hint: "Keeps earlier results instead of overwriting files with the same names" })),

    h("section", { class: "section" },
      h("h3", null, "Calibration"),
      numberField("opt-kv", "Beam energy", s.beam_kV, "kV", (v) => set({ beam_kV: v }), {
        extra: suggestedKV && suggestedKV !== s.beam_kV && h("button", {
          class: "suggest hint-inline", type: "button", onClick: () => set({ beam_kV: suggestedKV }),
          title: "Gun high voltage recorded in this file",
        }, `Use ${suggestedKV} kV from file`),
      }),
      numberField("opt-pix", "Reconstruction pixel size", s.recon_pix_size_pm, "pm", (v) => set({ recon_pix_size_pm: v }),
        { hint: "Sets the reciprocal-space calibration (λ / pixel size across the detector)" }),
      h("div", { class: "field" },
        h("div", { class: "field-label" }, "Bright-field disk radius"),
        segmented([{ value: "auto", label: "Estimate" }, { value: "manual", label: "Set manually" }],
          s.bf_disk_radius == null ? "auto" : "manual",
          (mode) => set({ bf_disk_radius: mode === "auto" ? null : manualRadius }), { block: true, label: "Radius mode" }),
        s.bf_disk_radius != null && h("div", { class: "input-wrap has-unit" },
          h("input", {
            id: "opt-radius", class: "input mono", type: "number", min: "1", step: "0.5", value: String(s.bf_disk_radius),
            "aria-label": "Bright-field disk radius in pixels",
            onChange: (e) => {
              const v = parseFloat(e.target.value);
              if (Number.isFinite(v) && v > 0) { manualRadius = v; storage.set("manualRadius", v); set({ bf_disk_radius: v }); }
            },
          }),
          h("span", { class: "input-unit" }, "px")),
        h("div", { class: "hint" }, s.bf_disk_radius == null
          ? "Estimated from the mean pattern (×1.2 for the virtual detectors)"
          : "Drawn on the diffraction pattern viewer; used for the virtual detectors"))),

    h("section", { class: "section" },
      h("h3", null, "Preprocessing"),
      toggleRow("opt-recenter", "Recenter diffraction patterns", s.do_recentering, (v) => set({ do_recentering: v }),
        { hint: quantem ? "Bilinear shifts from centre-of-mass estimates; differs from py4DSTEM disk fitting" : "Shift every pattern so the central beam sits at the detector centre" }),
      s.do_recentering && h("div", { class: "field" },
        h("div", { class: "field-label" }, "Centre estimate"),
        segmented([
          { value: "fit", label: "Plane fit per position" },
          { value: "simple", label: "Single global centre" },
        ], s.centre_method, (v) => set({ centre_method: v }), { block: true, label: "Centre method" }))),

    h("section", { class: "section" },
      h("h3", null, "Parallax"),
      toggleRow("opt-parallax", "Parallax reconstruction", parallaxOn, (v) => set({ parallax: v }),
        { hint: quantem ? "Cross-correlation alignment and direct parallax reconstruction" : "Align bright-field images across the detector (py4DSTEM Parallax)", issues: compatIssues("parallax") }),
      toggleRow("opt-aberr", "Fit aberrations", s.aberrations, (v) => set({ aberrations: v }),
        { hint: quantem ? "Rotation, defocus C10 and astigmatism C12 from the cross-correlation shifts" : "Defocus, astigmatism, Cs and the scan/detector rotation from the measured shifts", issues: compatIssues("aberrations") }),
      toggleRow("opt-transpose", "Mirrored scan (transpose)", s.force_transpose, (v) => set({ force_transpose: v }),
        { disabled: quantem ? !parallaxOn : !s.aberrations, hint: "Scan and detector have opposite handedness. One dataset can't reveal this (a mirrored defocus fits as astigmatism), so set it once per instrument from a defocus series" }),
      quantem && toggleRow("opt-ls", "Fourier-phase refinement (unvalidated)", s.quantem_least_squares, (v) => set({ quantem_least_squares: v }),
        { disabled: !s.aberrations, hint: "Quantem least squares adds Cs and higher orders. Not yet validated: on the synthetic sample it drives defocus to ~0" })),

    h("section", { class: "section" },
      h("h3", null, "QC figures"),
      FIGURES.map(([key, label, hint]) => {
        const disabled = key === "plot_parallax_recon" && !parallaxOn;
        return h("label", { class: `check-row${disabled ? " disabled" : ""}` },
          h("input", { type: "checkbox", class: "checkbox", checked: s[key] && !disabled, disabled, onChange: (e) => set({ [key]: e.target.checked }) }),
          h("span", null, label),
          h("span", { class: "hint" }, disabled ? "Needs parallax reconstruction" : hint));
      }),
      s.plot_parallax_recon && parallaxOn && compatIssues("parallax_figure").map((c) => callout(c.status, c.title, c.detail))));

  const multi = files.length > 1;
  const foot = h("div", { class: "convert-foot" },
    h("div", { class: "target-line", title: files.length === 1 ? outputPreview(files[0]) : "" },
      icon(multi ? "layers" : "folder", "sm"),
      h("span", { class: files.length === 1 ? "mono" : "" },
        files.length === 0 ? "Select a .hp file to convert"
          : multi ? `${files.length} files from the batch selection`
          : `→ ${outputPreview(files[0])}`)),
    h("div", { class: "foot-actions" },
      h("button", { class: "btn primary lg", disabled: !files.length || busy || blocked, onClick: submit },
        busy ? h("span", { class: "spinner", style: { borderColor: "rgba(255,255,255,.3)", borderTopColor: "currentColor" } }) : icon("play"),
        files.length > 1 ? `Convert ${files.length} files` : "Convert"),
      h("button", {
        class: "btn lg", title: "Copy the equivalent Python call", "aria-label": "Copy as Python",
        disabled: !files.length, onClick: () => copyText(pythonSnippet(files, s), "Python call copied"),
      }, icon("code"))));

  clear(root, head, body, foot);
  root.querySelector(".convert-body").scrollTop = scrollTop;
  if (focusId) document.getElementById(focusId)?.focus();
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

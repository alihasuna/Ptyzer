// Runs view: the job queue on the left; pipeline progress, results, figures and the live log of
// the selected job on the right.

import { api } from "./api.js";
import { callout, card, clear, copyText, fmt, h, icon, toast } from "./dom.js";
import { focusFile } from "./browser.js";
import { pythonSnippet } from "./convert.js";
import { isActive, refreshJobs } from "./jobstore.js";
import { openLightbox } from "./lightbox.js";
import { state, subscribe, update } from "./state.js";

const root = document.getElementById("viewRuns");
const FIGURE_KINDS = [
  ["plot_coord_checks", "coord_checks", "Scan-coordinate checks"],
  ["plot_overview", "overview", "Overview & scan mesh"],
  ["plot_virtual_diff", "virtual_diff", "Virtual images & diffraction"],
  ["plot_parallax_recon", "parallax_recon", "Parallax reconstruction"],
];
const PARAM_LABELS = {
  backend: ["Engine"],
  beam_kV: ["Beam energy", "kV"], recon_pix_size_pm: ["Reconstruction pixel size", "pm"],
  bf_disk_radius: ["BF disk radius", "px"], do_recentering: ["Recentering"], centre_method: ["Centre method"],
  do_save: ["Save dataset"], parallax: ["Parallax"], aberrations: ["Aberration fit"],
  plot_coord_checks: ["Coordinate figure"], plot_overview: ["Overview figure"],
  plot_virtual_diff: ["Virtual images figure"], plot_parallax_recon: ["Parallax figure"],
};

// Live stream for the selected job's log.
let stream = null;
let streamJobId = null;
let logLines = [];
let progressText = null;
let consoleEl = null;
let progressEl = null;
let pendingFlush = false;
let refreshSoon = null;
let ticker = null;

const now = () => Date.now() / 1000;

function statusIcon(status) {
  if (status === "running") return h("span", { class: "status-icon" }, h("span", { class: "spinner" }));
  const name = { done: "check", failed: "alert", cancelled: "stop", queued: "clock" }[status] || "clock";
  return h("span", { class: `status-icon ${status}` }, icon(name, "sm"));
}

function stageStatus(job, key) {
  const s = job.stages[key];
  if (s) {
    if (s.end == null) return "running";
    if (job.stage === key && job.status === "failed") return "failed";
    if (job.stage === key && job.status === "cancelled") return "cancelled";
    return "done";
  }
  if (job.status === "done") return "done";
  if (job.status === "failed" || job.status === "cancelled") return "skipped";
  return "pending";
}

function progressFraction(job) {
  if (job.status === "done") return 1;
  const done = job.plan.filter((st) => stageStatus(job, st.key) === "done").length;
  return job.plan.length ? done / job.plan.length : 0;
}

function subtitle(job) {
  if (job.status === "queued") return "Waiting in queue";
  if (job.status === "running") {
    const stage = job.plan.find((st) => st.key === job.stage);
    return stage ? stage.label : "Starting…";
  }
  if (job.status === "done") return `Finished in ${fmt.duration(job.finished - job.started)}`;
  if (job.status === "cancelled") return "Cancelled";
  return job.error ? `${job.error.type_name || "Error"}: ${job.error.message}` : "Failed";
}

// -- list --------------------------------------------------------------------------------------

function renderList() {
  const jobs = state.jobs;
  const finished = jobs.filter((j) => !isActive(j));
  return h("div", { class: "runs-list" },
    h("div", { class: "runs-list-head" },
      h("span", { class: "sidebar-title" }, "Runs", jobs.length > 0 && h("span", { class: "faint", style: { fontWeight: 400, marginLeft: "6px" } }, jobs.length)),
      finished.length > 0 && h("button", {
        class: "btn sm ghost", onClick: async () => {
          await Promise.all(finished.map((j) => api.jobAction(j.id, "remove").catch(() => null)));
          refreshJobs();
        },
      }, "Clear finished")),
    h("div", { class: "runs-items" },
      jobs.length === 0
        ? h("div", { class: "list-note" }, "No runs yet. Queue a conversion from the Inspect view.")
        : jobs.map((job) => h("button", {
          class: "run-item", "aria-current": String(job.id === state.activeJobId),
          onClick: () => update({ activeJobId: job.id }),
        },
          statusIcon(job.status),
          h("span", { class: "run-name", title: job.file }, job.name),
          h("span", { class: "run-time" }, fmt.time(job.created)),
          h("span", { class: "run-sub", title: subtitle(job) }, `${job.params.backend === "quantem" ? "Quantem" : "py4DSTEM"} · ${subtitle(job)}`),
          isActive(job) && h("span", { class: "run-bar" }, h("i", { style: { width: `${Math.round(progressFraction(job) * 100)}%` } }))))));
}

// -- detail ------------------------------------------------------------------------------------

function pipeline(job) {
  return card({
    title: "Pipeline", iconName: "pulse",
    sub: `${job.plan.filter((st) => stageStatus(job, st.key) === "done").length} of ${job.plan.length} steps`,
    bodyClass: "card-body flush",
    body: h("ol", { class: "pipeline" }, job.plan.map((st) => {
      const status = stageStatus(job, st.key);
      const timing = job.stages[st.key];
      const dot = status === "running" ? h("span", { class: "spinner", style: { width: "12px", height: "12px" } })
        : status === "done" ? icon("check") : status === "failed" ? icon("x") : status === "cancelled" ? icon("stop")
        : h("span", { class: "step-pending-dot" });
      return h("li", { class: `step ${status}` },
        h("span", { class: "step-dot" }, dot),
        h("span", { class: "step-label" }, st.label),
        h("span", { class: "step-time", "data-stage": st.key },
          timing ? fmt.duration((timing.end || now()) - timing.start) : ""));
    })),
  });
}

function metric(label, value, unit, hero) {
  return h("div", { class: `metric${hero ? " hero" : ""}` },
    h("div", { class: "metric-label", title: label }, label),
    h("div", { class: "metric-value", title: `${value}${unit ? ` ${unit}` : ""}` }, value, unit && h("span", { class: "unit" }, unit)));
}

function results(job) {
  const r = job.result;
  if (!r) {
    if (job.status === "failed" || job.status === "cancelled") return null;
    return card({
      title: "Results", iconName: "sparkle",
      body: h("div", { class: "results-stack" },
        h("div", { class: "metrics" }, [0, 1, 2, 3].map(() => h("div", { class: "metric" },
          h("div", { class: "skeleton", style: { height: "11px", width: "60%", marginBottom: "8px" } }),
          h("div", { class: "skeleton", style: { height: "20px", width: "80%" } })))),
        h("div", { class: "hint" }, job.status === "queued" ? "Results appear when the run finishes." : "Running…")),
    });
  }
  const a = r.aberrations;
  const output = r.output_file || r.output_h5;
  const rUnits = r.R_pixel_units === "A" ? "Å" : r.R_pixel_units;
  const qUnits = (r.Q_pixel_units || "").replace("A^-1", "Å⁻¹");
  return card({
    title: "Results", iconName: "sparkle", sub: `in ${fmt.duration(r.elapsed_s)}`,
    body: h("div", { class: "results-stack" },
      a && h("div", { class: "results-label" }, "Aberration fit"),
      a && h("div", { class: "metrics" },
        metric("Defocus", fmt.sig(a.defocus_nm, 5), "nm", true),
        a.astigmatism_nm != null && metric("Astigmatism C12", fmt.sig(a.astigmatism_nm, 4), "nm"),
        metric("Spherical aberration Cs", a.cs_mm == null ? "not fitted" : fmt.sig(a.cs_mm, 4), a.cs_mm == null ? "" : "mm"),
        metric("Rotation Q → R", fmt.fixed(a.rotation_deg, 2), "°"),
        a.transpose != null && metric("Transpose", a.transpose ? "Yes" : "No"),
        a.beam_half_angle_mrad != null && metric("Beam half-angle", fmt.fixed(a.beam_half_angle_mrad, 2), "mrad")),
      r.method_note && callout("info", "Quantem method", r.method_note),
      r.aberrations_error && callout("warn", "Couldn't read the aberration fit", r.aberrations_error),
      r.aberrations_warning && callout("warn", "Inconsistent defocus fit", r.aberrations_warning),
      h("div", { class: "results-label" }, "Data cube"),
      h("div", { class: "metrics" },
        metric("Shape", r.datacube_shape.join(" × ")),
        metric("Real-space pixel", fmt.sig(r.R_pixel_size * (rUnits === "nm" ? 1000 : 1), 4), rUnits === "nm" ? "pm" : rUnits),
        metric("Reciprocal pixel", fmt.sig(r.Q_pixel_size, 4), qUnits),
        r.diffraction_angular_FOV_mrad != null && metric("Angular FOV", fmt.fixed(r.diffraction_angular_FOV_mrad, 1), "mrad")),
      output && h("div", { class: "path-row" },
        h("span", { class: "badge ok" }, r.output_format || ".h5"),
        h("span", { class: "mono", title: output }, `‎${output}`),
        h("button", { class: "icon-btn sm", title: "Copy path", "aria-label": "Copy path", onClick: () => copyText(output, "Path copied") }, icon("copy", "sm")),
        h("button", { class: "icon-btn sm", title: "Reveal", "aria-label": "Reveal file", onClick: () => api.reveal(output).catch((e) => toast(e.message, { kind: "fail" })) }, icon("reveal", "sm")))),
  });
}

function figures(job) {
  const parallaxOn = job.params.parallax || job.params.aberrations;
  const expected = FIGURE_KINDS.filter(([param, kind]) => job.params[param] && (kind !== "parallax_recon" || parallaxOn));
  if (!expected.length && !job.figures.length) return null;
  const items = job.figures.map((f) => ({ src: `${api.figureUrl(job.id, f.index)}?t=${job.finished || ""}`, label: f.label, sub: job.name }));
  const tiles = expected.map(([, kind, label]) => {
    const i = job.figures.findIndex((f) => f.kind === kind);
    if (i === -1) {
      const waiting = isActive(job);
      return h("button", { class: "thumb", disabled: true },
        h("div", { class: "thumb-img pending" }, waiting ? h("span", { class: "spinner" }) : icon("image"), waiting ? "Pending" : "Not produced"),
        h("div", { class: "thumb-label faint" }, label));
    }
    return h("button", { class: "thumb", onClick: () => openLightbox(items, i) },
      h("div", { class: "thumb-img" }, h("img", { src: items[i].src, alt: label })),
      h("div", { class: "thumb-label" }, icon("image", "sm"), label));
  });
  return card({
    title: "QC figures", iconName: "image", sub: `${job.figures.length} of ${expected.length}`,
    actions: job.figures.length > 0 && h("button", { class: "btn sm", onClick: () => api.reveal(job.output_dir) }, icon("reveal", "sm"), "Open folder"),
    body: h("div", { class: "thumbs" }, tiles),
  });
}

function logCard(job) {
  consoleEl = h("pre", { class: "console", tabindex: "0", "aria-label": "Conversion log" });
  progressEl = h("div", { class: "progress-line", hidden: true });
  const el = card({
    title: "Log", iconName: "terminal",
    actions: [
      h("button", { class: "btn sm", onClick: () => copyText(logLines.join("\n"), "Log copied") }, icon("copy", "sm"), "Copy"),
    ],
    bodyClass: "", body: [progressEl, consoleEl],
  });
  flushConsole(true);
  return el;
}

function lineClass(text) {
  if (/^-{4,}.*-{4,}$/.test(text.trim())) return "l-sec";
  if (/Traceback|Error:|Exception|^\s*File ".*", line \d+/.test(text)) return "l-err";
  if (/^\s*$/.test(text)) return null;
  if (/^[\s-]+$|^\s*\[.*\]\s*$/.test(text)) return "l-dim";
  return null;
}

function flushConsole(full = false) {
  pendingFlush = false;
  if (!consoleEl) return;
  const nearBottom = consoleEl.scrollHeight - consoleEl.scrollTop - consoleEl.clientHeight < 40;
  const start = full ? Math.max(0, logLines.length - 4000) : Number(consoleEl.dataset.count || 0);
  if (full) consoleEl.replaceChildren();
  const frag = document.createDocumentFragment();
  for (let i = start; i < logLines.length; i++) {
    const cls = lineClass(logLines[i]);
    frag.append(cls ? h("span", { class: cls }, logLines[i]) : logLines[i], "\n");
  }
  consoleEl.append(frag);
  consoleEl.dataset.count = String(logLines.length);
  if (!logLines.length) consoleEl.textContent = "Waiting for output…";
  if (full || nearBottom) consoleEl.scrollTop = consoleEl.scrollHeight;

  progressEl.hidden = !progressText;
  if (progressText) {
    const match = progressText.match(/(\d+)%\|/);
    const pct = match ? Number(match[1]) : null;
    const label = progressText.split("|")[0].replace(/\s*\d+%\s*$/, "").trim() || "Working";
    const counts = (progressText.match(/\|\s*([^[]+)\[/) || [])[1];
    clear(progressEl,
      h("span", { class: "mono" }, label, counts && h("span", { class: "faint" }, `  ${counts.trim()}`)),
      h("span", { class: "mono faint" }, pct != null ? `${pct}%` : ""),
      h("div", { class: `progress-track${pct == null ? " indeterminate" : ""}` }, h("i", { style: { width: `${pct || 0}%` } })));
  }
}

function scheduleFlush() {
  if (!pendingFlush) {
    pendingFlush = true;
    requestAnimationFrame(() => flushConsole());
  }
}

function openStream(job) {
  if (streamJobId === job.id) return;
  if (stream) stream.close();
  streamJobId = job.id;
  logLines = [];
  progressText = null;
  stream = api.stream(job.id, (item) => {
    if (item.type === "log") { logLines.push(item.text); progressText = null; scheduleFlush(); }
    else if (item.type === "progress") { progressText = item.text; scheduleFlush(); }
    else if (["stage", "figure", "result", "error", "status"].includes(item.type)) {
      if (item.type === "status" && item.status !== "running") { progressText = null; scheduleFlush(); }
      clearTimeout(refreshSoon);
      refreshSoon = setTimeout(refreshJobs, 80);
    }
  }, () => { progressText = null; scheduleFlush(); refreshJobs(); });
}

function errorCard(job) {
  const e = job.error;
  if (!e) return null;
  return card({
    title: "Conversion failed", iconName: "alert", cls: "error-card",
    body: h("div", { style: { display: "grid", gap: "10px" } },
      h("div", { class: "error-title" }, `${e.type_name || "Error"}: ${e.message}`),
      e.hint && callout("info", "What to try", e.hint),
      e.traceback && h("details", { class: "trace" }, h("summary", null, "Traceback"), h("pre", null, e.traceback))),
  });
}

function paramsCard(job) {
  const p = job.params;
  const value = (key) => {
    const v = p[key];
    if (key === "bf_disk_radius" && v == null) return "estimated";
    if (key === "centre_method") return p.do_recentering ? (v === "fit" ? "plane fit" : "global") : "—";
    if (typeof v === "boolean") return v ? "on" : "off";
    return String(v);
  };
  return card({
    title: "Parameters", iconName: "sliders",
    actions: h("button", { class: "btn sm", onClick: () => copyText(pythonSnippet([job.file], p, job.output_dir), "Python call copied") },
      icon("code", "sm"), "Copy as Python"),
    body: h("div", { class: "kv" }, h("dl", { class: "kv-group", style: { margin: 0, gridColumn: "1 / -1" } },
      h("div", { style: { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(230px, 1fr))", columnGap: "24px" } },
        Object.entries(PARAM_LABELS).map(([key, [label, unit]]) => h("div", { class: "kv-row" },
          h("dt", null, label), h("dd", null, value(key), unit && p[key] != null && h("span", { class: "unit" }, unit))))))),
  });
}

function renderDetail(job) {
  if (!job) {
    return h("div", { class: "run-detail" }, h("div", { class: "empty" }, h("div", { class: "empty-inner" },
      icon("layers", "lg"),
      h("h2", null, state.jobs.length ? "Select a run" : "No runs yet"),
      h("p", null, "Conversions you queue from the Inspect view show up here with live progress, QC figures and results."))));
  }
  openStream(job);
  const actions = [
    isActive(job) && h("button", { class: "btn sm danger", onClick: () => act(job, "cancel") }, icon("stop", "sm"), job.status === "queued" ? "Remove from queue" : "Cancel"),
    !isActive(job) && h("button", { class: "btn sm", onClick: () => act(job, "retry") }, icon("retry", "sm"), "Run again"),
    h("button", { class: "btn sm", onClick: () => focusFile(job.file) }, icon("search", "sm"), "Inspect file"),
    !isActive(job) && h("button", { class: "icon-btn sm", title: "Remove from list", "aria-label": "Remove from list", onClick: () => act(job, "remove") }, icon("trash", "sm")),
  ];
  const badgeKind = { done: "ok", failed: "fail", running: "accent", cancelled: "", queued: "" }[job.status];
  return h("div", { class: "run-detail" }, h("div", { class: "page" },
    h("div", { class: "page-head" },
      h("div", { class: "eyebrow" },
        h("span", { class: `badge ${badgeKind}` }, job.status),
        `Queued ${fmt.datetime(job.created)}`,
        job.info && h("span", { class: "faint" }, `· ${job.info.engine || "py4DSTEM"} ${job.info.version || job.info.py4DSTEM}`)),
      h("div", { class: "page-title-row" }, h("h1", { class: "page-title" }, job.name), h("div", { class: "card-actions" }, actions)),
      h("div", { class: "path-row" },
        icon("folder", "sm"),
        h("span", { class: "mono", title: job.output_dir }, `‎${job.output_dir}`),
        h("button", { class: "icon-btn sm", title: "Copy output folder", "aria-label": "Copy output folder", onClick: () => copyText(job.output_dir, "Path copied") }, icon("copy", "sm")),
        h("button", { class: "icon-btn sm", title: "Reveal output folder", "aria-label": "Reveal output folder",
          onClick: () => api.reveal(job.output_dir).catch((e) => toast(e.message, { kind: "fail" })) }, icon("reveal", "sm")))),
    errorCard(job),
    h("div", { class: "grid-2" }, pipeline(job), results(job) || paramsCard(job)),
    figures(job),
    logCard(job),
    job.result && paramsCard(job)));
}

async function act(job, action) {
  try {
    const result = await api.jobAction(job.id, action);
    if (action === "retry") update({ activeJobId: result.id });
    if (action === "remove") update({ activeJobId: null });
    await refreshJobs();
  } catch (err) {
    toast(err.message, { kind: "fail" });
  }
}

// Re-rendering the whole view on every poll would reset scroll positions and the console, so the
// detail is only rebuilt when something visible changed.
let lastSignature = null;
function signature(job) {
  if (!job) return `none:${state.jobs.length}`;
  return JSON.stringify([job.id, job.status, job.stage, Object.keys(job.stages).length, job.figures.length,
    Boolean(job.result), Boolean(job.error), job.plan.length, Boolean(job.info)]);
}

function render(force = false) {
  if (state.view !== "runs") return;
  let job = state.jobs.find((j) => j.id === state.activeJobId);
  if (!job && state.jobs.length) {
    job = state.jobs[0];
    state.activeJobId = job.id;
  }
  const list = renderList();
  const existingList = root.querySelector(".runs-list");
  const scroll = existingList?.querySelector(".runs-items")?.scrollTop || 0;
  const sig = signature(job);
  const detailHost = root.querySelector(".run-detail");
  if (!force && detailHost && sig === lastSignature) {
    existingList.replaceWith(list);
    list.querySelector(".runs-items").scrollTop = scroll;
    return;
  }
  const detailScroll = detailHost ? detailHost.scrollTop : 0;
  const sameJob = lastSignature && job && lastSignature.startsWith(`["${job.id}"`);
  lastSignature = sig;
  clear(root, list, renderDetail(job));
  list.querySelector(".runs-items").scrollTop = scroll;
  if (consoleEl) consoleEl.scrollTop = consoleEl.scrollHeight;
  if (sameJob) root.querySelector(".run-detail").scrollTop = detailScroll;
}

function tick() {
  // Keep running stage timers moving without a full re-render.
  const job = state.jobs.find((j) => j.id === state.activeJobId);
  if (!job || job.status !== "running" || state.view !== "runs") return;
  for (const el of root.querySelectorAll(".step-time[data-stage]")) {
    const timing = job.stages[el.dataset.stage];
    if (timing && timing.end == null) el.textContent = fmt.duration(now() - timing.start);
  }
}

export function initRuns() {
  subscribe((keys) => {
    if (keys.includes("jobs")) {
      const count = state.jobs.filter(isActive).length;
      const badge = document.getElementById("runsCount");
      badge.hidden = count === 0;
      badge.textContent = String(count);
      render();
    }
    if (keys.includes("activeJobId")) render(true);
    if (keys.includes("view") && state.view === "runs") render(true);
  });
  ticker = setInterval(tick, 500);
}

// Runs view: the job queue on the left; pipeline progress, results, figures and the live log of
// the selected job on the right.

import { api } from "./api.js";
import { button, callout, card, clear, copyText, fmt, h, pathText, status, toast } from "./dom.js";
import { focusFile } from "./browser.js";
import { loadSettings, pythonSnippet } from "./convert.js";
import { isActive, refreshJobs } from "./jobstore.js";
import { figureGrid } from "./lightbox.js";
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
  beam_kV: ["Beam energy", "kV"], recon_pix_size_pm: ["Recon. pixel size", "pm"],
  bf_disk_radius: ["BF disk radius", "px"], do_recentering: ["Recentering"], centre_method: ["Centre method"],
  do_save: ["Save dataset"], parallax: ["Parallax"], aberrations: ["Aberration fit"],
  force_transpose: ["Mirrored scan"], quantem_least_squares: ["LS refinement"],
  plot_coord_checks: ["Coordinate figure"], plot_overview: ["Overview figure"],
  plot_virtual_diff: ["Virtual images figure"], plot_parallax_recon: ["Parallax figure"],
};
// Job status as a dot plus a word (never colour alone).
const JOB_STATUS = {
  queued: ["idle", "Queued"], running: ["run", "Running"], done: ["ok", "Done"],
  failed: ["error", "Failed"], cancelled: ["idle", "Cancelled"],
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
let liveEl = null;
let lastAnnounced = null;

const now = () => Date.now() / 1000;

function jobStatus(job) {
  const [kind, word] = JOB_STATUS[job.status] || ["idle", job.status];
  return status(kind, word);
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
    return stage ? stage.label : "Starting";
  }
  if (job.status === "done") return `Finished in ${fmt.duration(job.finished - job.started)}`;
  if (job.status === "cancelled") return "Cancelled";
  return job.error ? `${job.error.type_name || "Error"}: ${job.error.message}` : "Failed";
}

function progressBar(job, label) {
  const pct = Math.round(progressFraction(job) * 100);
  return h("div", { class: "es-progress", role: "progressbar", "aria-label": label,
    "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(pct) }, h("span", { style: { width: `${pct}%` } }));
}

// -- list --------------------------------------------------------------------------------------

function renderList() {
  const jobs = state.jobs;
  const finished = jobs.filter((j) => !isActive(j));
  return h("section", { class: "pz-runs-list", "aria-labelledby": "runsListTitle" },
    h("div", { class: "es-section-head pz-sidebar-title" },
      h("h2", { class: "es-h3", id: "runsListTitle" }, "Runs", jobs.length > 0 && h("span", { class: "es-caption es-num" }, ` ${jobs.length}`)),
      finished.length > 0 && button("Clear finished", async () => {
        await Promise.all(finished.map((j) => api.jobAction(j.id, "remove").catch(() => null)));
        refreshJobs();
      }, { size: "sm", variant: "quiet" })),
    jobs.length === 0
      ? h("p", { class: "es-caption pz-list-note" }, "No runs yet. Queue a conversion from the Inspect view.")
      : h("ul", { class: "pz-run-items" }, jobs.map((job) => h("li", null,
        h("button", {
          type: "button", class: "pz-run", "aria-current": job.id === state.activeJobId ? "true" : null,
          dataset: { key: job.id }, onClick: () => update({ activeJobId: job.id }),
        },
          h("span", { class: "pz-run-top" }, jobStatus(job), h("span", { class: "es-caption es-num" }, fmt.time(job.created))),
          h("span", { class: "pz-run-name", title: job.file }, job.name),
          h("span", { class: "pz-run-sub es-caption", title: subtitle(job) },
            `${job.params.backend === "quantem" ? "Quantem" : "py4DSTEM"} · ${subtitle(job)}`),
          isActive(job) && progressBar(job, `${job.name} progress`))))));
}

// -- detail ------------------------------------------------------------------------------------

const STEP_WORD = { failed: "Failed", cancelled: "Cancelled", skipped: "Skipped", running: "In progress" };

function pipeline(job) {
  const done = job.plan.filter((st) => stageStatus(job, st.key) === "done").length;
  return card({
    title: "Pipeline", sub: `${done} of ${job.plan.length} steps`,
    body: [
      h("ol", { class: "es-steps pz-steps" }, job.plan.map((st) => {
        const s = stageStatus(job, st.key);
        const timing = job.stages[st.key];
        const cls = s === "done" ? "is-done" : s === "pending" || s === "skipped" ? "is-pending" : s === "running" ? "" : `is-${s}`;
        return h("li", { class: cls },
          h("span", { class: "pz-step-label" }, st.label,
            STEP_WORD[s] && h("span", { class: "pz-step-word" }, ` · ${STEP_WORD[s]}`),
            s === "done" && h("span", { class: "es-sr-only" }, " · Done"),
            s === "pending" && h("span", { class: "es-sr-only" }, " · Pending")),
          h("span", { class: "pz-step-time es-num", "data-stage": st.key },
            timing ? fmt.duration((timing.end || now()) - timing.start) : ""));
      })),
      progressBar(job, "Pipeline progress"),
    ],
  });
}

function metric(label, value, unit, hero) {
  return h("div", { class: `pz-metric${hero ? " is-hero" : ""}` },
    h("dt", null, label),
    h("dd", { class: "es-num" }, value, unit && h("span", { class: "pz-unit" }, ` ${unit}`)));
}

function results(job) {
  const r = job.result;
  if (!r) {
    if (job.status === "failed" || job.status === "cancelled") return null;
    return card({
      title: "Results",
      body: h("p", { class: "es-caption" }, job.status === "queued" ? "Results appear when the run finishes." : "Results appear when the run finishes. It is running now."),
    });
  }
  const a = r.aberrations;
  const output = r.output_file || r.output_h5;
  const rUnits = r.R_pixel_units === "A" ? "Å" : r.R_pixel_units;
  const qUnits = (r.Q_pixel_units || "").replace("A^-1", "Å⁻¹");
  return card({
    title: "Results", sub: `in ${fmt.duration(r.elapsed_s)}`,
    body: h("div", { class: "pz-stack" },
      a && h("h3", { class: "pz-subhead" }, "Aberration fit"),
      a && h("dl", { class: "pz-metrics" },
        metric("Defocus", fmt.sig(a.defocus_nm, 5), "nm", true),
        a.astigmatism_nm != null && metric("Astigmatism C12", fmt.sig(a.astigmatism_nm, 4), "nm"),
        metric("Spherical aberration Cs", a.cs_mm == null ? "not fitted" : fmt.sig(a.cs_mm, 4), a.cs_mm == null ? "" : "mm"),
        metric("Rotation Q → R", fmt.fixed(a.rotation_deg, 2), "°"),
        a.transpose != null && metric("Transpose", a.transpose ? "Yes" : "No"),
        a.beam_half_angle_mrad != null && metric("Beam half-angle", fmt.fixed(a.beam_half_angle_mrad, 2), "mrad")),
      r.method_note && callout("info", "Quantem method", r.method_note),
      r.aberrations_error && callout("warn", "Couldn't read the aberration fit", r.aberrations_error),
      r.aberrations_warning && callout("warn", "Inconsistent defocus fit", r.aberrations_warning),
      (r.preflight_warnings || []).map((w) => callout("warn", "Pre-flight warning", w)),
      h("h3", { class: "pz-subhead" }, "Data cube"),
      h("dl", { class: "pz-metrics" },
        metric("Shape", r.datacube_shape.join(" × ")),
        metric("Real-space pixel", fmt.sig(r.R_pixel_size * (rUnits === "nm" ? 1000 : 1), 4), rUnits === "nm" ? "pm" : rUnits),
        metric("Reciprocal pixel", fmt.sig(r.Q_pixel_size, 4), qUnits),
        r.diffraction_angular_FOV_mrad != null && metric("Angular FOV", fmt.fixed(r.diffraction_angular_FOV_mrad, 1), "mrad")),
      output && h("div", { class: "pz-path-row" },
        h("span", { class: "pz-tag" }, r.output_format || ".h5"),
        pathText(output),
        button("Copy", () => copyText(output, "Path copied"), { size: "sm", variant: "quiet", ariaLabel: "Copy output path" }),
        button("Reveal", () => api.reveal(output).catch((e) => toast(e.message, { kind: "fail" })), { size: "sm", variant: "quiet", ariaLabel: "Reveal output file" })),
      r.record_file && h("div", { class: "pz-path-row" },
        h("span", { class: "pz-tag", title: "Run record: engine, input fingerprint, parameters, results and conventions" }, "Record"),
        pathText(r.record_file),
        button("Copy", () => copyText(r.record_file, "Path copied"), { size: "sm", variant: "quiet", ariaLabel: "Copy run record path" }))),
  });
}

function figures(job) {
  const parallaxOn = job.params.parallax || job.params.aberrations;
  const expected = FIGURE_KINDS.filter(([param, kind]) => job.params[param] && (kind !== "parallax_recon" || parallaxOn));
  if (!expected.length && !job.figures.length) return null;
  const items = job.figures.map((f) => ({ src: `${api.figureUrl(job.id, f.index)}?t=${job.finished || ""}`, label: f.label, sub: job.name }));
  const missing = expected.filter(([, kind]) => !job.figures.some((f) => f.kind === kind))
    .map(([, , label]) => ({ label, state: isActive(job) ? "Pending" : "Not produced" }));
  return card({
    title: "QC figures", sub: `${job.figures.length} of ${expected.length}`,
    actions: job.figures.length > 0 && button("Open folder", () => api.reveal(job.output_dir), { size: "sm", variant: "quiet" }),
    body: figureGrid(items, missing),
  });
}

function logCard(job) {
  consoleEl = h("pre", { class: "es-log pz-log", tabindex: "0", "aria-label": "Conversion log" });
  progressEl = h("div", { class: "pz-progress-line", hidden: true });
  const el = card({
    title: "Log",
    actions: button("Copy", () => copyText(logLines.join("\n"), "Log copied"), { size: "sm", variant: "quiet", ariaLabel: "Copy log" }),
    body: [progressEl, consoleEl],
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

  // tqdm progress: shown, but not announced (it changes several times a second).
  progressEl.hidden = !progressText;
  if (progressText) {
    const match = progressText.match(/(\d+)%\|/);
    const pct = match ? Number(match[1]) : null;
    const label = progressText.split("|")[0].replace(/\s*\d+%\s*$/, "").trim() || "Working";
    const counts = (progressText.match(/\|\s*([^[]+)\[/) || [])[1];
    clear(progressEl,
      h("div", { class: "pz-progress-text es-num" },
        h("span", null, label, counts && h("span", { class: "es-caption" }, `  ${counts.trim()}`)),
        h("span", null, pct != null ? `${pct}%` : "")),
      h("div", { class: "es-progress", "aria-hidden": "true" }, h("span", { style: { width: `${pct ?? 0}%` } })));
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
    title: "Conversion failed", cls: "pz-panel--error",
    body: h("div", { class: "pz-stack" },
      callout("fail", `${e.type_name || "Error"}: ${e.message}`),
      e.hint && callout("info", "What to try", e.hint),
      e.traceback && h("details", { class: "pz-details" }, h("summary", null, "Traceback"), h("pre", { class: "es-log", tabindex: "0" }, e.traceback))),
  });
}

function paramsCard(job) {
  const p = job.params;
  const value = (key) => {
    const v = p[key];
    if (key === "bf_disk_radius" && v == null) return "estimated";
    if (key === "centre_method") return p.do_recentering ? (v === "fit" ? "plane fit" : "global") : "—";
    if (key === "backend") return v === "quantem" ? "Quantem" : "py4DSTEM";
    if (typeof v === "boolean") return v ? "on" : "off";
    return v == null ? "—" : String(v);
  };
  return card({
    title: "Parameters", quiet: true,
    actions: button("Copy as Python", () => copyText(pythonSnippet([job.file], p, job.output_dir), "Python call copied"), { size: "sm", variant: "quiet" }),
    body: h("ul", { class: "es-spec pz-spec-cols" },
      Object.entries(PARAM_LABELS).filter(([key]) => key in p).map(([key, [label, unit]]) => h("li", null,
        h("b", null, label), h("span", { class: "es-num" }, value(key), unit && p[key] != null ? ` ${unit}` : "")))),
  });
}

function label(text) {
  return h("p", { class: "es-label" }, h("span", { class: "es-plus", "aria-hidden": "true" }, "+"), text);
}

function renderDetail(job) {
  if (!job) {
    return h("div", { class: "pz-run-detail" }, h("div", { class: "pz-page pz-empty" },
      label("Runs"),
      h("h1", { class: "es-h1" }, state.jobs.length ? "Select a run." : "No runs yet."),
      h("p", { class: "es-lede" }, "Conversions you queue from the Inspect view show up here with live progress, QC figures and results.")));
  }
  openStream(job);
  const actions = [
    isActive(job) && button(job.status === "queued" ? "Remove from queue" : "Cancel", () => act(job, "cancel"), { size: "sm" }),
    job.status === "failed"
      ? button("Edit and run again", () => editAndRerun(job), { size: "sm", variant: "dark" })
      : !isActive(job) && button("Run again", () => act(job, "retry"), { size: "sm" }),
    button("Inspect file", () => focusFile(job.file), { size: "sm" }),
    !isActive(job) && button("Remove", () => act(job, "remove"), { size: "sm", variant: "quiet", ariaLabel: "Remove from list" }),
  ];
  const engine = job.info && `${job.info.engine || "py4DSTEM"} ${job.info.version || job.info.py4DSTEM || ""}`.trim();
  return h("div", { class: "pz-run-detail" }, h("div", { class: "pz-page" },
    h("header", { class: "pz-page-head" },
      h("div", { class: "pz-label-row" }, label("Run"), jobStatus(job)),
      h("div", { class: "pz-title-row" }, h("h1", { class: "es-h1 pz-title" }, `${job.name}.`), h("div", { class: "pz-actions" }, actions)),
      h("p", { class: "es-lede" }, [`Queued ${fmt.datetime(job.created)}`, engine, subtitle(job)].filter(Boolean).join(" · ")),
      h("div", { class: "pz-path-row" },
        pathText(job.output_dir),
        button("Copy", () => copyText(job.output_dir, "Path copied"), { size: "sm", variant: "quiet", ariaLabel: "Copy output folder" }),
        button("Reveal", () => api.reveal(job.output_dir).catch((e) => toast(e.message, { kind: "fail" })), { size: "sm", variant: "quiet", ariaLabel: "Reveal output folder" }))),
    errorCard(job),
    h("div", { class: "pz-grid-2" }, pipeline(job), results(job) || paramsCard(job)),
    figures(job),
    logCard(job),
    job.result && paramsCard(job)));
}

// A failed run usually needs a change (often the output folder): open its settings instead of
// repeating them.
function editAndRerun(job) {
  focusFile(job.file);
  loadSettings(job.params);
  toast("This run's settings are loaded in Convert. Change the output folder or other settings, then convert.",
    { timeout: 7000 });
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

// Announce stage and status changes of the selected run (polite, once per change).
function announce(job) {
  if (!liveEl || !job) return;
  const text = `${job.name}: ${JOB_STATUS[job.status]?.[1] || job.status}. ${subtitle(job)}`;
  if (text !== lastAnnounced) { lastAnnounced = text; liveEl.textContent = text; }
}

// Re-rendering the whole view on every poll would reset scroll positions and the console, so the
// detail is only rebuilt when something visible changed.
let lastSignature = null;
let awaitedJobId = null;
function signature(job) {
  if (!job) return `none:${state.jobs.length}`;
  return JSON.stringify([job.id, job.status, job.stage, Object.keys(job.stages).length, job.figures.length,
    Boolean(job.result), Boolean(job.error), job.plan.length, Boolean(job.info)]);
}

function render(force = false) {
  if (state.view !== "runs") return;
  let job = state.jobs.find((j) => j.id === state.activeJobId);
  if (!job && state.jobs.length) {
    // Show the newest run meanwhile, but keep a requested id (e.g. from #runs/<id>) that the job
    // list hasn't caught up with yet; the next refresh selects it.
    job = state.jobs[0];
    if (state.activeJobId && state.activeJobId !== awaitedJobId) {
      awaitedJobId = state.activeJobId;
      refreshJobs();
    } else {
      state.activeJobId = job.id;  // unknown even after a refresh (e.g. a stale link): show the newest
    }
  }
  if (!liveEl) {
    liveEl = h("div", { class: "es-sr-only", "aria-live": "polite", role: "status" });
    root.before(liveEl);
  }
  announce(job);
  const focusKey = root.contains(document.activeElement) ? document.activeElement.dataset.key : null;
  const list = renderList();
  const existingList = root.querySelector(".pz-runs-list");
  const scroll = existingList?.scrollTop || 0;
  const sig = signature(job);
  const detailHost = root.querySelector(".pz-run-detail");
  if (!force && detailHost && sig === lastSignature) {
    existingList.replaceWith(list);
    list.scrollTop = scroll;
    if (focusKey) list.querySelector(`[data-key="${CSS.escape(focusKey)}"]`)?.focus();
    return;
  }
  const detailScroll = detailHost ? detailHost.scrollTop : 0;
  const sameJob = lastSignature && job && lastSignature.startsWith(`["${job.id}"`);
  lastSignature = sig;
  clear(root, list, renderDetail(job));
  list.scrollTop = scroll;
  if (consoleEl) consoleEl.scrollTop = consoleEl.scrollHeight;
  if (sameJob) root.querySelector(".pz-run-detail").scrollTop = detailScroll;
  if (focusKey) root.querySelector(`[data-key="${CSS.escape(focusKey)}"]`)?.focus();
}

function tick() {
  // Keep running stage timers moving without a full re-render.
  const job = state.jobs.find((j) => j.id === state.activeJobId);
  if (!job || job.status !== "running" || state.view !== "runs") return;
  for (const el of root.querySelectorAll(".pz-step-time[data-stage]")) {
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
      badge.textContent = count ? ` · ${count} active` : "";
      render();
    }
    if (keys.includes("activeJobId")) render(true);
    if (keys.includes("view") && state.view === "runs") render(true);
  });
  ticker = setInterval(tick, 500);
}


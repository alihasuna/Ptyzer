// Boot: theme, view routing, environment status, and module wiring.

import { api } from "./api.js";
import { initBrowser } from "./browser.js";
import { initConvert } from "./convert.js";
import { button, callout, clear, copyText, h, pathText, status } from "./dom.js";
import { initInspect } from "./inspect.js";
import { refreshJobs } from "./jobstore.js";
import { initRuns } from "./runs.js";
import { state, storage, subscribe, update } from "./state.js";

const app = document.getElementById("app");
const THEME_KEY = "es-theme"; // shared with the ElectroSim website and JupyterHub

// -- theme -------------------------------------------------------------------------------------

function effectiveTheme() {
  return document.documentElement.dataset.theme
    || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}

function initTheme() {
  const button = document.getElementById("themeToggle");
  const paint = () => {
    const next = effectiveTheme() === "dark" ? "light" : "dark";
    button.querySelector(".pz-theme-text").textContent = next === "dark" ? "Dark" : "Light";
    button.setAttribute("aria-label", `Switch to ${next} theme`);
  };
  button.addEventListener("click", () => {
    const next = effectiveTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem(THEME_KEY, next); } catch { /* storage unavailable */ }
    paint();
    // Canvas viewers read CSS variables when drawing.
    window.dispatchEvent(new Event("resize"));
    update({ settings: state.settings });
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", paint);
  paint();
}

// -- views -------------------------------------------------------------------------------------

function applyView() {
  document.getElementById("viewInspect").hidden = state.view !== "inspect";
  document.getElementById("viewRuns").hidden = state.view !== "runs";
  for (const link of document.querySelectorAll(".es-nav [data-view]")) {
    if (link.dataset.view === state.view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  document.title = `${state.view === "runs" ? "Runs" : "Inspect"} · Ptyzer · ElectroSim`;
  const hash = state.view === "runs" ? (state.activeJobId ? `#runs/${state.activeJobId}` : "#runs") : "#inspect";
  if (location.hash !== hash) history.replaceState(null, "", hash);
}

function routeFromHash() {
  const match = location.hash.match(/^#runs(?:\/([0-9a-f]+))?/);
  if (match) update({ view: "runs", activeJobId: match[1] || state.activeJobId });
  else update({ view: "inspect" });
}

function initNav() {
  subscribe((keys) => {
    if (keys.includes("view") || keys.includes("activeJobId")) applyView();
  });
  window.addEventListener("hashchange", routeFromHash);
}

function setDrawer(open) {
  app.classList.toggle("drawer-open", open);
  document.getElementById("drawerToggle").setAttribute("aria-expanded", String(open));
  if (open) document.getElementById("sidebar").querySelector("button, input")?.focus();
}

function initDrawer() {
  const toggle = document.getElementById("drawerToggle");
  toggle.addEventListener("click", () => setDrawer(!app.classList.contains("drawer-open")));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && app.classList.contains("drawer-open")) { setDrawer(false); toggle.focus(); }
  });
  subscribe((keys) => { if (keys.includes("focused")) setDrawer(false); });
}

// -- environment -------------------------------------------------------------------------------

function renderEnv(err) {
  const statusEl = document.getElementById("envStatus");
  const textEl = document.getElementById("envText");
  const pop = document.getElementById("envPopover");
  const env = state.env;
  // The status follows the engine selected in Convert; each engine has its own Python environment.
  const backend = state.settings?.backend || "py4dstem";
  let kind = "idle", word = "Checking", text = "";
  if (err) { kind = "error"; word = "Offline"; text = "Server unreachable"; }
  else if (env && backend === "quantem") {
    const q = env.backends?.quantem;
    if (q?.available) { kind = "ok"; word = "Ready"; text = `Quantem ${q.version}`; }
    else { kind = "error"; word = "Unavailable"; text = "Quantem"; }
  } else if (env) {
    const fails = env.checks.filter((c) => c.status === "fail").length;
    const warns = env.checks.filter((c) => c.status === "warn").length;
    const versions = `py4DSTEM ${env.packages.py4DSTEM || "missing"}`;
    if (!env.converter_ok) {
      kind = "error"; word = "Unavailable"; text = "py4DSTEM";
    } else if (fails || warns) {
      kind = fails ? "error" : "warn"; word = `${fails + warns} issue${fails + warns === 1 ? "" : "s"}`; text = versions;
    } else { kind = "ok"; word = "Ready"; text = versions; }
  }
  statusEl.className = `es-status es-status--${kind}`;
  statusEl.textContent = word;
  textEl.textContent = text;

  if (!env) {
    return clear(pop, err ? callout("fail", "Can't reach the Ptyzer server", err.message) : h("p", { class: "es-caption" }, "Loading…"));
  }
  const q = env.backends?.quantem;
  const p4 = env.backends?.py4dstem;
  const engineRow = (key, name, info, detail) => h("li", null,
    h("b", null, name, key === backend ? " · selected" : ""),
    h("span", null, status(info?.available ? "ok" : "error", info?.available ? "Available" : "Unavailable"),
      h("span", { class: "es-num" }, " ", detail)));
  clear(pop,
    h("div", { class: "es-section-head" }, h("h2", { class: "es-h3" }, "Engines"),
      button("Close", () => setPopover(false), { size: "sm", variant: "quiet" })),
    h("ul", { class: "es-spec pz-spec" },
      engineRow("py4dstem", "py4DSTEM", p4, p4?.available ? p4.version : (p4?.error || "not installed")),
      engineRow("quantem", "Quantem", q, q?.available ? `${q.version} · numpy ${q.numpy} · experimental` : (q?.error || "not installed"))),
    h("h3", { class: "pz-subhead" }, "Server Python (runs py4DSTEM)"),
    h("div", { class: "pz-path-row" },
      h("span", { class: "es-label" }, `Python ${env.python}`),
      pathText(env.executable),
      button("Copy", () => copyText(env.executable), { size: "sm", variant: "quiet", ariaLabel: "Copy interpreter path" })),
    h("ul", { class: "es-spec pz-spec" }, Object.entries(env.packages).map(([name, version]) =>
      h("li", null, h("b", null, name), h("span", { class: "es-num" }, version || "not installed")))),
    !env.converter_ok && callout("fail", "ptyzer.io.azohp_to_py4d could not be imported", env.converter_error),
    env.checks.map((c) => callout(c.status, c.title, c.detail)),
    env.converter_ok && !env.checks.length && callout("info", "All compatibility checks passed",
      "The py4DSTEM code paths Ptyzer relies on (Parallax, aberration fitting, figure hooks) match what it expects."),
    h("p", { class: "es-help" }, "Each engine runs in a separate worker using its configured Python environment; the status button follows the engine selected in Convert."));
}

function setPopover(open) {
  const pill = document.getElementById("envPill");
  const pop = document.getElementById("envPopover");
  pop.hidden = !open;
  pill.setAttribute("aria-expanded", String(open));
  if (open) pop.querySelector("button")?.focus();
  else if (pop.contains(document.activeElement)) pill.focus();
}

function initEnvStatus() {
  subscribe((keys) => { if (keys.includes("settings")) renderEnv(); });
}

function initEnvPopover() {
  const pill = document.getElementById("envPill");
  const pop = document.getElementById("envPopover");
  pill.addEventListener("click", (e) => { e.stopPropagation(); setPopover(pop.hidden); });
  document.addEventListener("click", (e) => { if (!pop.hidden && !pop.contains(e.target)) setPopover(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !pop.hidden) setPopover(false); });
}

async function loadEnv() {
  renderEnv();
  try {
    update({ env: await api.env() });
    renderEnv();
  } catch (err) {
    renderEnv(err);
    setTimeout(loadEnv, 3000);
  }
}

// -- boot --------------------------------------------------------------------------------------

initTheme();
initNav();
initDrawer();
initEnvPopover();
initEnvStatus();
initConvert();
initRuns();

const focused = storage.get("focused", null);
if (focused) state.focused = focused;
initInspect();
initBrowser(storage.get("dir", null));
routeFromHash();
applyView();
loadEnv();
refreshJobs();


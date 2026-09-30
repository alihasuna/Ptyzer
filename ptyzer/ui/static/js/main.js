// Boot: theme, view routing, environment status, and module wiring.

import { api } from "./api.js";
import { initBrowser } from "./browser.js";
import { initConvert } from "./convert.js";
import { callout, clear, copyText, h, icon } from "./dom.js";
import { initInspect } from "./inspect.js";
import { refreshJobs } from "./jobstore.js";
import { initRuns } from "./runs.js";
import { state, storage, subscribe, update } from "./state.js";

const app = document.getElementById("app");

// -- theme -------------------------------------------------------------------------------------

function effectiveTheme() {
  return document.documentElement.dataset.theme
    || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}

function initTheme() {
  const button = document.getElementById("themeToggle");
  const paint = () => clear(button, icon(effectiveTheme() === "dark" ? "sun" : "moon"));
  button.addEventListener("click", () => {
    const next = effectiveTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    storage.set("theme", next);
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
  for (const tab of document.querySelectorAll(".tab")) {
    tab.setAttribute("aria-selected", String(tab.dataset.view === state.view));
  }
  const hash = state.view === "runs" ? (state.activeJobId ? `#runs/${state.activeJobId}` : "#runs") : "#inspect";
  if (location.hash !== hash) history.replaceState(null, "", hash);
}

function routeFromHash() {
  const match = location.hash.match(/^#runs(?:\/([0-9a-f]+))?/);
  if (match) update({ view: "runs", activeJobId: match[1] || state.activeJobId });
  else update({ view: "inspect" });
}

function initTabs() {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => update({ view: tab.dataset.view }));
  }
  subscribe((keys) => {
    if (keys.includes("view") || keys.includes("activeJobId")) applyView();
  });
  window.addEventListener("hashchange", routeFromHash);
}

function initDrawer() {
  const toggle = document.getElementById("drawerToggle");
  toggle.append(icon("menu"));
  toggle.addEventListener("click", () => app.classList.toggle("drawer-open"));
  document.getElementById("drawerScrim").addEventListener("click", () => app.classList.remove("drawer-open"));
}

// -- environment -------------------------------------------------------------------------------

function renderEnv(err) {
  const pill = document.getElementById("envPill");
  const pop = document.getElementById("envPopover");
  const env = state.env;
  let dot = "pulse", text = "Checking environment…";
  if (err) { dot = "fail"; text = "Server unreachable"; }
  else if (env) {
    const fails = env.checks.filter((c) => c.status === "fail").length;
    const warns = env.checks.filter((c) => c.status === "warn").length;
    const versions = `py4DSTEM ${env.packages.py4DSTEM || "missing"} · numpy ${env.packages.numpy || "?"}`;
    if (!env.converter_ok) { dot = env.backends?.quantem?.available ? "ok" : "fail"; text = env.backends?.quantem?.available ? "Quantem available · py4DSTEM unavailable" : "Converter unavailable"; }
    else if (fails || warns) { dot = fails ? "fail" : "warn"; text = `${versions} · ${fails + warns} issue${fails + warns === 1 ? "" : "s"}`; }
    else { dot = "ok"; text = versions; }
  }
  clear(pill, h("span", { class: `dot ${dot}` }), h("span", { class: "env-text" }, text));

  if (!env) return clear(pop, err ? callout("fail", "Can't reach the Ptyzer server", err.message) : h("div", { class: "faint" }, "Loading…"));
  clear(pop,
    h("h3", null, "Python environment"),
    h("div", { class: "path-row" },
      h("span", { class: "badge" }, `Python ${env.python}`),
      h("span", { class: "mono", title: env.executable }, `‎${env.executable}`),
      h("button", { class: "icon-btn sm", "aria-label": "Copy interpreter path", onClick: () => copyText(env.executable) }, icon("copy", "sm"))),
    h("dl", { class: "kv-group", style: { margin: 0 } }, Object.entries(env.packages).map(([name, version]) =>
      h("div", { class: "kv-row" }, h("dt", null, name), h("dd", null, version || h("span", { style: { color: "var(--fail)" } }, "not installed"))))),
    !env.converter_ok && callout("fail", "ptyzer.io.azohp_to_py4d could not be imported", env.converter_error),
    env.checks.map((c) => callout(c.status, c.title, c.detail)),
    env.converter_ok && !env.checks.length && callout("info", "All compatibility checks passed",
      "The py4DSTEM code paths Ptyzer relies on (Parallax, aberration fitting, figure hooks) match what it expects."),
    env.backends?.quantem && h("div", { class: "hint" }, env.backends.quantem.available
      ? `Quantem ${env.backends.quantem.version} · numpy ${env.backends.quantem.numpy} · experimental`
      : `Quantem unavailable: ${env.backends.quantem.error || "not installed"}`),
    h("div", { class: "hint" }, "Each engine runs in a separate worker using its configured Python environment."));
}

function initEnvPopover() {
  const pill = document.getElementById("envPill");
  const pop = document.getElementById("envPopover");
  const setOpen = (open) => { pop.hidden = !open; pill.setAttribute("aria-expanded", String(open)); };
  pill.addEventListener("click", (e) => { e.stopPropagation(); setOpen(pop.hidden); });
  document.addEventListener("click", (e) => { if (!pop.hidden && !pop.contains(e.target)) setOpen(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") setOpen(false); });
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
initTabs();
initDrawer();
initEnvPopover();
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

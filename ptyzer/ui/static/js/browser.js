// Sidebar file browser: navigate folders, focus a .hp file for inspection, tick files for batch runs.

import { api } from "./api.js";
import { button, clear, fmt, h, toast } from "./dom.js";
import { state, storage, subscribe, update } from "./state.js";

const root = document.getElementById("sidebar");
let listing = null;
let filter = "";
let loading = false;
let error = null;

export async function navigate(path) {
  loading = true;
  render();
  try {
    listing = await api.browse(path);
    error = null;
    filter = "";
    storage.set("dir", listing.path);
  } catch (err) {
    error = err.message;
  } finally {
    loading = false;
    render();
  }
}

export function focusFile(path) {
  storage.set("focused", path);
  update({ focused: path, view: "inspect" });
}

export async function generateSample() {
  toast("Writing a synthetic 48 × 48 scan dataset…");
  try {
    const { path, dir } = await api.sample();
    await navigate(dir);
    focusFile(path);
    toast("Sample dataset ready", { kind: "ok" });
  } catch (err) {
    toast(`Could not create the sample: ${err.message}`, { kind: "fail" });
  }
}

function toggleSelected(path) {
  const selected = new Set(state.selected);
  selected.has(path) ? selected.delete(path) : selected.add(path);
  update({ selected });
}

function fileRow(entry) {
  if (entry.kind === "dir") {
    return h("li", { class: "pz-file pz-file--dir" },
      h("button", { type: "button", class: "pz-file-open", title: entry.path, onClick: () => navigate(entry.path) },
        h("span", { class: "pz-file-name" }, `${entry.name}/`),
        h("span", { class: "es-sr-only" }, "folder")));
  }
  const current = state.focused === entry.path;
  return h("li", { class: `pz-file${current ? " is-current" : ""}` },
    h("input", {
      type: "checkbox", class: "pz-file-check", checked: state.selected.has(entry.path), dataset: { key: `check:${entry.path}` },
      "aria-label": `Select ${entry.name} for batch conversion`, onChange: () => toggleSelected(entry.path),
    }),
    h("button", {
      type: "button", class: "pz-file-open", title: entry.path, "aria-current": current ? "true" : null, dataset: { key: `open:${entry.path}` },
      onClick: () => focusFile(entry.path),
    },
      h("span", { class: "pz-file-name" }, entry.name),
      h("span", { class: "pz-file-meta es-num" },
        entry.converted && h("span", { class: "pz-tag" }, "Converted"),
        fmt.bytes(entry.size))));
}

function render() {
  // Re-rendering replaces the rows; keep keyboard focus on the same control.
  const focusKey = root.contains(document.activeElement) ? document.activeElement.dataset.key : null;
  const head = h("div", { class: "pz-sidebar-head" },
    h("div", { class: "es-section-head pz-sidebar-title" },
      h("h2", { class: "es-h3" }, "Files"),
      h("div", { class: "pz-actions" },
        button("Up", () => listing && navigate(listing.parent), {
          size: "sm", variant: "quiet", disabled: !listing || !listing.parent, ariaLabel: "Up to parent folder" }),
        button("Refresh", () => navigate(listing ? listing.path : null), { size: "sm", variant: "quiet" }))),
    listing && h("nav", { class: "pz-crumbs", "aria-label": "Current folder" },
      h("ol", null, listing.crumbs.map((c, i) => h("li", null,
        h("button", { type: "button", class: "pz-crumb", title: c.path, "aria-current": i === listing.crumbs.length - 1 ? "location" : null,
          onClick: () => navigate(c.path) }, c.name))))),
    h("div", { class: "es-field" },
      h("label", { for: "fileFilter" }, "Filter or go to folder"),
      h("input", {
        id: "fileFilter", class: "es-input", type: "search", placeholder: "Name, or a path and Enter", value: filter,
        autocomplete: "off",
        onInput: (e) => { filter = e.target.value; renderList(); },
        onKeydown: (e) => {
          const value = e.target.value.trim();
          if (e.key === "Enter" && /^([\\/~]|[A-Za-z]:)/.test(value)) navigate(value);
        },
      })),
    listing && h("ul", { class: "pz-places", "aria-label": "Places" },
      listing.places.map((p) => h("li", null,
        button(p.label, () => navigate(p.path), { size: "sm", variant: "quiet", title: p.path })))));

  const list = h("ul", { class: "pz-file-list", id: "fileList", "aria-label": "Folder contents", "aria-busy": loading ? "true" : null });
  const selectedCount = state.selected.size;
  const foot = h("div", { class: "pz-sidebar-foot" },
    selectedCount > 0 && h("div", { class: "pz-selection", role: "status" },
      h("span", { class: "es-num" }, `${selectedCount} selected for batch`),
      button("Clear", () => update({ selected: new Set() }), { size: "sm", variant: "quiet" })),
    button("Generate sample dataset", generateSample, { class: "es-btn pz-block" }));

  clear(root, head, list, foot);
  const crumbs = root.querySelector(".pz-crumbs");
  if (crumbs) crumbs.scrollLeft = crumbs.scrollWidth;
  renderList();
  if (focusKey) root.querySelector(`[data-key="${CSS.escape(focusKey)}"]`)?.focus();
}

function note(text) {
  return h("li", { class: "pz-list-note es-caption" }, text);
}

function renderList() {
  const list = root.querySelector("#fileList");
  if (!list) return;
  if (loading && !listing) return clear(list, note("Loading folder…"));
  if (error) return clear(list, h("li", { class: "pz-list-note" }, h("span", { class: "es-status es-status--error" }, "Error"), " ", error));
  if (!listing) return clear(list);

  const needle = filter.trim().toLowerCase();
  const isPath = /^([\\/~]|[A-Za-z]:)/.test(needle);
  const entries = listing.entries.filter((e) => isPath || !needle || e.name.toLowerCase().includes(needle));
  const files = entries.filter((e) => e.kind === "hp");
  clear(list, entries.map(fileRow));
  if (isPath) list.prepend(note("Press Enter to open this folder"));
  else if (!files.length) {
    list.append(note(needle ? "No matching files" : listing.entries.length ? "No .hp files in this folder" : "This folder is empty"));
  }
}

export function initBrowser(startDir) {
  subscribe((keys) => {
    if (keys.includes("focused") || keys.includes("selected")) render();
  });
  render();
  // A remembered folder may have gone away (unplugged drive, deleted run); fall back to the launch folder.
  navigate(startDir).then(() => { if (error && startDir) navigate(null); });
}

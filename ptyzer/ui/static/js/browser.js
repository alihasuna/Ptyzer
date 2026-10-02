// Sidebar file browser: navigate folders, focus a .hp file for inspection, tick files for batch runs.

import { api } from "./api.js";
import { clear, fmt, h, icon, toast } from "./dom.js";
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
  document.getElementById("app").classList.remove("drawer-open");
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

function placeIcon(place) {
  if (place.label === "Home") return "home";
  if (place.label === "Sample data") return "sparkle";
  if (/^[A-Z]:\\$/.test(place.label) || place.label === "/" || place.label === "Volumes") return "drive";
  return "folder";
}

function fileRow(entry) {
  if (entry.kind === "dir") {
    return h("button", { class: "file-row dir", title: entry.name, onClick: () => navigate(entry.path) },
      icon("folder"), h("span", { class: "file-name" }, entry.name), icon("chevronRight", "sm"));
  }
  const selected = state.selected.has(entry.path);
  const row = h("div", {
    class: "file-row", role: "button", tabindex: "0", title: entry.path,
    "aria-current": String(state.focused === entry.path),
    onClick: () => focusFile(entry.path),
    onKeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); focusFile(entry.path); } },
  },
    h("label", { class: "file-check", onClick: (e) => e.stopPropagation() },
      h("input", {
        type: "checkbox", class: "checkbox", checked: selected, "aria-label": `Select ${entry.name} for batch conversion`,
        onChange: () => toggleSelected(entry.path),
      })),
    icon("dataset"),
    h("span", { class: "file-name" }, entry.name),
    h("span", { class: "file-meta" },
      entry.converted && h("span", { class: "badge ok", title: "A saved conversion already exists for this file" }, "converted"),
      fmt.bytes(entry.size)));
  return row;
}

function render() {
  const head = h("div", { class: "sidebar-head" },
    h("div", { class: "sidebar-row" },
      h("span", { class: "sidebar-title" }, "Files"),
      h("button", { class: "icon-btn sm", title: "Parent folder", "aria-label": "Parent folder",
        disabled: !listing || !listing.parent, onClick: () => listing && navigate(listing.parent) }, icon("up")),
      h("button", { class: "icon-btn sm", title: "Refresh", "aria-label": "Refresh",
        onClick: () => navigate(listing ? listing.path : null) }, icon("refresh"))),
    listing && h("nav", { class: "crumbs", "aria-label": "Current folder" },
      listing.crumbs.map((c, i) => [
        i > 0 && !/[\\/]$/.test(listing.crumbs[i - 1].name) && h("span", { class: "crumb-sep" }, icon("chevronRight", "sm")),
        h("button", { class: "crumb", title: c.path, onClick: () => navigate(c.path) }, c.name),
      ])),
    h("div", { class: "input-wrap" },
      icon("search", "sm"),
      h("input", {
        class: "input", type: "search", placeholder: "Filter, or paste a folder path…", value: filter,
        "aria-label": "Filter files or go to a folder",
        onInput: (e) => { filter = e.target.value; renderList(); },
        onKeydown: (e) => {
          const value = e.target.value.trim();
          if (e.key === "Enter" && /^([\\/~]|[A-Za-z]:)/.test(value)) navigate(value);
        },
      })),
    listing && h("div", { class: "places" },
      listing.places.map((p) => h("button", { class: "place", title: p.path, onClick: () => navigate(p.path) },
        icon(placeIcon(p), "sm"), p.label))));

  const list = h("div", { class: "file-list", id: "fileList" });
  const selectedCount = state.selected.size;
  const foot = h("div", { class: "sidebar-foot" },
    selectedCount > 0 && h("div", { class: "selection-bar" },
      icon("layers", "sm"), `${selectedCount} selected for batch`,
      h("button", { class: "btn sm ghost", onClick: () => update({ selected: new Set() }) }, "Clear")),
    h("button", { class: "btn block", onClick: generateSample }, icon("sparkle"), "Generate sample dataset"));

  clear(root, head, list, foot);
  const crumbs = root.querySelector(".crumbs");
  if (crumbs) crumbs.scrollLeft = crumbs.scrollWidth;
  renderList();
}

function renderList() {
  const list = root.querySelector("#fileList");
  if (!list) return;
  if (loading && !listing) return clear(list, ...[0, 1, 2, 3, 4].map(() =>
    h("div", { class: "skeleton", style: { height: "28px", margin: "6px 8px" } })));
  if (error) return clear(list, h("div", { class: "list-note" }, icon("alert"), h("div", null, error)));
  if (!listing) return clear(list);

  const needle = filter.trim().toLowerCase();
  const isPath = /^([\\/~]|[A-Za-z]:)/.test(needle);
  const entries = listing.entries.filter((e) => isPath || !needle || e.name.toLowerCase().includes(needle));
  const files = entries.filter((e) => e.kind === "hp");
  clear(list, entries.map(fileRow));
  if (isPath) list.prepend(h("div", { class: "list-note" }, "Press Enter to open this folder"));
  else if (!files.length) {
    list.append(h("div", { class: "list-note" },
      needle ? "No matching files" : listing.entries.length ? "No .hp files in this folder" : "This folder is empty"));
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

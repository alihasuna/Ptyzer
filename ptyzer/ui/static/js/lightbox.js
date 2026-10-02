// Figure grid and full-screen figure viewer (keyboard navigation, fit / actual-size toggle).
// QC figures are scientific images: shown on a white mat with a hairline, never restyled.

import { button, clear, h } from "./dom.js";

const root = document.getElementById("lightbox");
let items = [];
let index = 0;
let actual = false;
let returnFocus = null;

// items: [{ src, label, sub }]; emptyTiles: [{ label, state }] for figures not produced yet.
export function figureGrid(list, emptyTiles = []) {
  return h("ul", { class: "pz-figures" },
    list.map((item, i) => h("li", null,
      h("button", { type: "button", class: "pz-figure", onClick: () => openLightbox(list, i) },
        h("span", { class: "pz-figure-mat" }, h("img", { src: item.src, alt: "", loading: "lazy" })),
        h("span", { class: "pz-figure-caption" }, item.label, h("span", { class: "es-sr-only" }, ", open full size"))))),
    emptyTiles.map((t) => h("li", null,
      h("div", { class: "pz-figure is-empty" },
        h("span", { class: "pz-figure-mat es-caption" }, t.state),
        h("span", { class: "pz-figure-caption" }, t.label)))));
}

export function openLightbox(list, start = 0) {
  if (!list.length) return;
  items = list;
  index = start;
  actual = false;
  returnFocus = document.activeElement;
  root.hidden = false;
  document.addEventListener("keydown", onKey);
  render();
  root.querySelector("[data-close]").focus();
}

function close() {
  root.hidden = true;
  clear(root);
  document.removeEventListener("keydown", onKey);
  if (returnFocus && returnFocus.focus) returnFocus.focus();
}

function go(delta) {
  index = (index + delta + items.length) % items.length;
  actual = false;
  render();
}

function onKey(e) {
  if (e.key === "Escape") close();
  else if (e.key === "ArrowLeft" && items.length > 1) go(-1);
  else if (e.key === "ArrowRight" && items.length > 1) go(1);
  else if (e.key === "Tab") {
    // Keep focus inside the dialog while it is open.
    const focusable = [...root.querySelectorAll("button, a[href]")];
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
}

function render() {
  const item = items[index];
  const keepFocus = root.contains(document.activeElement) ? document.activeElement.dataset.role : null;
  clear(root,
    h("div", { class: "pz-lightbox-bar" },
      h("h2", { class: "es-h3", id: "lightboxTitle" }, item.label),
      h("span", { class: "es-caption es-num" }, [items.length > 1 && `${index + 1} of ${items.length}`, item.sub].filter(Boolean).join(" · ")),
      h("div", { class: "pz-actions" },
        items.length > 1 && button("Previous", () => go(-1), { size: "sm", "data-role": "prev" }),
        items.length > 1 && button("Next", () => go(1), { size: "sm", "data-role": "next" }),
        button(actual ? "Fit to screen" : "Actual size", () => { actual = !actual; render(); }, { size: "sm", "data-role": "size" }),
        h("a", { class: "es-btn es-btn--sm", href: item.src, target: "_blank", rel: "noopener", "data-role": "open" }, "Open in new tab"),
        button("Close", close, { size: "sm", variant: "dark", "data-close": true, "data-role": "close" }))),
    h("div", { class: `pz-lightbox-stage${actual ? " is-actual" : ""}`, onClick: (e) => { if (e.target === e.currentTarget) close(); } },
      h("figure", { class: "es-figure" },
        h("img", { src: item.src, alt: item.label }),
        h("figcaption", null, item.sub ? `${item.label}. ${item.sub}` : item.label))));
  root.setAttribute("aria-labelledby", "lightboxTitle");
  if (keepFocus) root.querySelector(`[data-role="${keepFocus}"]`)?.focus();
}

// Full-screen figure viewer with keyboard navigation and fit / actual-size toggle.

import { clear, h, icon } from "./dom.js";

const root = document.getElementById("lightbox");
let items = [];
let index = 0;
let actual = false;
let returnFocus = null;

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
}

function render() {
  const item = items[index];
  const img = h("img", { src: item.src, alt: item.label, onClick: () => { actual = !actual; render(); } });
  clear(root,
    h("div", { class: "lightbox-bar" },
      h("span", { class: "lightbox-title" }, item.label),
      items.length > 1 && h("span", { class: "lightbox-count" }, `${index + 1} / ${items.length}`),
      item.sub && h("span", { class: "lightbox-count" }, item.sub),
      h("span", { style: { marginLeft: "auto" } }),
      h("a", { class: "icon-btn", href: item.src, target: "_blank", rel: "noopener", title: "Open full size in a new tab", "aria-label": "Open full size in a new tab" }, icon("external")),
      h("button", { class: "icon-btn", "data-close": true, "aria-label": "Close", onClick: close }, icon("x"))),
    h("div", {
      class: `lightbox-stage${actual ? " actual" : ""}`,
      onClick: (e) => { if (e.target === e.currentTarget) close(); },
    }, img),
    items.length > 1 && h("button", { class: "lightbox-nav prev", "aria-label": "Previous figure", onClick: () => go(-1) }, icon("chevronLeft")),
    items.length > 1 && h("button", { class: "lightbox-nav next", "aria-label": "Next figure", onClick: () => go(1) }, icon("chevronRight")));
}

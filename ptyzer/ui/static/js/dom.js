// Small DOM, formatting and feedback helpers shared by every view. Components are the ElectroSim
// UI kit's (electrosim-ui.css, es- classes); app layout lives in styles.css (pz- classes).

export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "value" || key === "checked" || key === "disabled" || key === "hidden") el[key] = value;
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, value);
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function clear(el, ...children) {
  el.replaceChildren();
  return append(el, children);
}

let uid = 0;
export const nextId = (prefix = "pz") => `${prefix}-${++uid}`;

// -- buttons -----------------------------------------------------------------------------------

// variant: "dark" (primary action), "line" (default), "quiet", "accent" (rare); size: "sm".
export function button(label, onClick, { variant = "line", size, title, disabled, ariaLabel, type = "button", ...rest } = {}) {
  const cls = ["es-btn", variant !== "line" && `es-btn--${variant}`, size && `es-btn--${size}`].filter(Boolean).join(" ");
  return h("button", { type, class: cls, title, disabled, "aria-label": ariaLabel, onClick, ...rest }, label);
}

// -- status ------------------------------------------------------------------------------------

// A dot plus a word; never colour alone. kind: ok | warn | error | run | idle.
export function status(kind, word, props = {}) {
  return h("span", { ...props, class: `es-status es-status--${kind}${props.class ? ` ${props.class}` : ""}` }, word);
}

const ALERT_WORD = { fail: "Error", error: "Error", warn: "Warning", info: "Note", ok: "Done" };

export function callout(kind, title, detail) {
  return h("div", { class: `es-alert pz-alert${kind === "fail" || kind === "error" ? " es-alert--error" : ""}` },
    h("span", { class: "pz-alert-kind" }, ALERT_WORD[kind] || "Note"),
    title && h("strong", { class: "pz-alert-title" }, title),
    detail && h("div", { class: "pz-alert-detail" }, detail));
}

// -- surfaces ----------------------------------------------------------------------------------

// Grey panel with the dark top edge; the panel title is an h2 styled as the kit's panel title.
export function card({ title, sub, actions, body, cls = "", quiet = false, level = 2, id }) {
  const titleId = id || nextId("panel");
  return h("section", { class: `es-panel pz-panel${quiet ? " es-panel--quiet" : ""} ${cls}`.trim(), "aria-labelledby": titleId },
    h("div", { class: "es-section-head pz-panel-head" },
      h(`h${level}`, { class: "es-h3", id: titleId }, title),
      (sub || actions) && h("div", { class: "pz-panel-meta" },
        sub && h("span", { class: "es-caption pz-panel-sub" }, sub),
        actions && h("div", { class: "pz-actions" }, actions))),
    body);
}

// A scrollable data region must be reachable by keyboard.
export function scrollRegion(label, child, cls = "pz-table-wrap") {
  return h("div", { class: cls, tabindex: "0", role: "region", "aria-label": label }, child);
}

// -- form controls -----------------------------------------------------------------------------

// Segmented choice built from native radios (arrow keys, labels and focus come for free).
export function segmented(options, value, onChange, { label, block = false, name } = {}) {
  const group = name || nextId("seg");
  return h("div", { class: `pz-seg${block ? " pz-seg--block" : ""}`, role: "radiogroup", "aria-label": label },
    options.map((opt) => h("label", { class: "pz-seg-opt", title: opt.title },
      h("input", {
        type: "radio", name: group, id: name ? `${group}-${opt.value}` : null, value: opt.value, checked: opt.value === value, disabled: opt.disabled,
        class: "es-sr-only", onChange: () => onChange(opt.value),
      }),
      h("span", null, opt.label))));
}

// Checkbox with its label and optional help text (replaces the old switch).
export function checkRow(id, label, checked, onChange, { hint, disabled } = {}) {
  const hintId = hint ? `${id}-help` : null;
  return h("div", { class: `pz-check-row${disabled ? " is-disabled" : ""}` },
    h("label", { class: "es-check", for: id },
      h("input", { type: "checkbox", id, checked: Boolean(checked), disabled, "aria-describedby": hintId,
        onChange: (e) => onChange(e.target.checked) }),
      h("span", null, label)),
    hint && h("div", { class: "es-help", id: hintId }, hint));
}

// -- formatting --------------------------------------------------------------------------------

export const fmt = {
  bytes(n) {
    if (n == null) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${i && n < 10 ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
  },
  int(n) { return n == null ? "—" : Number(n).toLocaleString(); },
  sig(v, digits = 4) {
    if (v == null || !Number.isFinite(v)) return "—";
    if (v === 0) return "0";
    const abs = Math.abs(v);
    if (abs >= 1e5 || abs < 1e-3) return v.toExponential(digits - 1).replace("e+", "e").replace(/-/g, "−");
    return String(Number(v.toPrecision(digits))).replace("-", "−");
  },
  fixed(v, d = 2) { return v == null || !Number.isFinite(v) ? "—" : v.toFixed(d).replace("-", "−"); },
  duration(s) {
    if (s == null || !Number.isFinite(s)) return "—";
    if (s < 1) return `${Math.max(1, Math.round(s * 1000))} ms`;
    if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} s`;
    const m = Math.floor(s / 60);
    return `${m}m ${String(Math.round(s - m * 60)).padStart(2, "0")}s`;
  },
  time(ts) {
    if (!ts) return "—";
    return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  },
  datetime(ts) {
    if (!ts) return "—";
    return new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  },
  basename(p) { return (p || "").split(/[\\/]/).filter(Boolean).pop() || p; },
  dirname(p) {
    const parts = (p || "").split(/([\\/])/);
    return parts.slice(0, -2).join("") || parts[0];
  },
};

// -- feedback ----------------------------------------------------------------------------------

// Toasts go into the #toasts live region; errors also get role="alert".
export function toast(message, { kind = "info", action, timeout = 4000 } = {}) {
  const host = document.getElementById("toasts");
  const el = h("div", { class: `es-alert pz-toast${kind === "fail" ? " es-alert--error" : ""}`, role: kind === "fail" ? "alert" : null },
    h("span", { class: "pz-alert-kind" }, ALERT_WORD[kind] || "Note"),
    h("span", null, message),
    action && button(action.label, () => { action.run(); el.remove(); }, { size: "sm" }));
  host.append(el);
  setTimeout(() => el.remove(), timeout);
}

export async function copyText(text, label = "Copied to clipboard") {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = h("textarea", { style: { position: "fixed", opacity: "0" }, "aria-hidden": "true" }, text);
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  toast(label, { kind: "ok", timeout: 1800 });
}

// A file path that may be long: shown in mono, truncated from the left, full text in the title.
export function pathText(path, cls = "") {
  return h("span", { class: `pz-path es-num ${cls}`.trim(), title: path }, `‎${path}`);
}

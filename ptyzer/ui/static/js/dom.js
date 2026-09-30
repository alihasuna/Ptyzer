// Small DOM, formatting and feedback helpers shared by every view.

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

const ICONS = {
  folder: '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2h7.5A2.5 2.5 0 0 1 21 9.5v7a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 16.5z"/>',
  dataset: '<rect x="4" y="4" width="16" height="16" rx="3.5"/><circle cx="9" cy="9" r=".9"/><circle cx="15" cy="9" r=".9"/><circle cx="9" cy="15" r=".9"/><circle cx="15" cy="15" r=".9"/><circle cx="12" cy="12" r="2.4"/>',
  up: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  chevronRight: '<path d="m9 6 6 6-6 6"/>',
  chevronLeft: '<path d="m15 6-6 6 6 6"/>',
  refresh: '<path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20 4v5h-5"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m20 20-4-4"/>',
  play: '<path d="M8 5.5v13l10.5-6.5z"/>',
  stop: '<rect x="6.5" y="6.5" width="11" height="11" rx="2"/>',
  retry: '<path d="M4 12a8 8 0 1 0 2.4-5.7"/><path d="M4 4v5h5"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12.5a1.5 1.5 0 0 0 1.5 1.5h7a1.5 1.5 0 0 0 1.5-1.5L18 7M9 7V4.5h6V7"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2.5"/><path d="M5 15V6.5A2.5 2.5 0 0 1 7.5 4H15"/>',
  reveal: '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  alert: '<path d="M10.3 4.3 2.6 17.6A2 2 0 0 0 4.3 20.5h15.4a2 2 0 0 0 1.7-2.9L13.7 4.3a2 2 0 0 0-3.4 0z"/><path d="M12 9.5v4M12 17h.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.5h.01"/>',
  sparkle: '<path d="M12 3.5 13.8 9a2 2 0 0 0 1.2 1.2l5.5 1.8-5.5 1.8a2 2 0 0 0-1.2 1.2L12 20.5 10.2 15A2 2 0 0 0 9 13.8L3.5 12 9 10.2A2 2 0 0 0 10.2 9z"/>',
  image: '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><circle cx="9" cy="10" r="1.8"/><path d="m20.5 15.5-4.5-4.5-9 8.5"/>',
  terminal: '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="m7.5 9.5 3 2.5-3 2.5M13 15h3.5"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6 6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4"/>',
  moon: '<path d="M20 14.2A8 8 0 1 1 9.8 4a6.4 6.4 0 0 0 10.2 10.2z"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  code: '<path d="m8.5 8-4 4 4 4M15.5 8l4 4-4 4"/>',
  layers: '<path d="m12 4 8.5 4.5L12 13 3.5 8.5z"/><path d="m3.5 12.5 8.5 4.5 8.5-4.5"/><path d="m3.5 16.5 8.5 4.5 8.5-4.5" opacity=".5"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  crosshair: '<circle cx="12" cy="12" r="7.5"/><path d="M12 2.5v5M12 16.5v5M2.5 12h5M16.5 12h5"/>',
  home: '<path d="M4 11 12 4.5l8 6.5"/><path d="M6 9.5V19a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V9.5"/>',
  drive: '<rect x="3.5" y="13" width="17" height="7" rx="2"/><path d="M5.5 13 7.5 5h9l2 8M16.5 16.5h.01"/>',
  cpu: '<rect x="6" y="6" width="12" height="12" rx="2.5"/><rect x="9.5" y="9.5" width="5" height="5" rx="1"/><path d="M9.5 3v3M14.5 3v3M9.5 18v3M14.5 18v3M3 9.5h3M3 14.5h3M18 9.5h3M18 14.5h3"/>',
  sliders: '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
  expand: '<path d="M14.5 4H20v5.5M9.5 20H4v-5.5M20 4l-6.5 6.5M4 20l6.5-6.5"/>',
  external: '<path d="M14 4h6v6M20 4l-8.5 8.5"/><path d="M18 13.5v5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6h5"/>',
  grid: '<rect x="4" y="4" width="16" height="16" rx="2.5"/><path d="M4 12h16M12 4v16"/>',
  pulse: '<path d="M3 12h4l2.5-6 5 12 2.5-6h4"/>',
};

export function icon(name, cls = "") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", `icon ${cls}`.trim());
  svg.innerHTML = ICONS[name] || "";
  return svg;
}

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
  fixed(v, d = 2) { return v == null || !Number.isFinite(v) ? "—" : v.toFixed(d); },
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

export function toast(message, { kind = "info", action, timeout = 3600 } = {}) {
  const host = document.getElementById("toasts");
  const iconName = kind === "fail" ? "alert" : kind === "ok" ? "check" : "info";
  const el = h("div", { class: `toast ${kind}`, role: kind === "fail" ? "alert" : "status" },
    icon(iconName), h("span", null, message),
    action && h("button", { class: "btn sm", onClick: () => { action.run(); el.remove(); } }, action.label));
  host.append(el);
  setTimeout(() => el.remove(), timeout);
}

export async function copyText(text, label = "Copied to clipboard") {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = h("textarea", { style: { position: "fixed", opacity: "0" } }, text);
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  toast(label, { kind: "ok", timeout: 1800 });
}

export function segmented(options, value, onChange, { block = false, label } = {}) {
  const group = h("div", { class: `seg${block ? " block" : ""}`, role: "radiogroup", "aria-label": label });
  for (const opt of options) {
    group.append(h("button", {
      type: "button", role: "radio", "aria-checked": String(opt.value === value), disabled: opt.disabled,
      title: opt.title, onClick: () => onChange(opt.value),
    }, opt.label));
  }
  return group;
}

export function switchControl(checked, onChange, { id, disabled, label } = {}) {
  return h("button", {
    type: "button", class: "switch", role: "switch", id, disabled, "aria-label": label,
    "aria-checked": String(Boolean(checked)), onClick: () => onChange(!checked),
  });
}

export function callout(kind, title, detail) {
  return h("div", { class: `callout ${kind}` },
    icon(kind === "info" ? "info" : "alert"),
    h("div", null, title && h("strong", null, title), detail));
}

export function card({ title, iconName, sub, actions, body, bodyClass = "card-body", cls = "" }) {
  return h("section", { class: `card ${cls}`.trim() },
    h("header", { class: "card-head" },
      h("div", { class: "card-title" }, iconName && icon(iconName), title),
      sub && h("span", { class: "card-sub" }, sub),
      actions && h("div", { class: "card-actions" }, actions)),
    h("div", { class: bodyClass }, body));
}

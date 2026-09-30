// Shared app state with a minimal change notification, plus namespaced localStorage.

const listeners = new Set();

export const state = {
  env: null,          // /api/env response
  focused: null,      // path of the .hp file shown in Inspect
  inspection: null,   // /api/hp/inspect response for `focused`
  selected: new Set(),// .hp paths ticked in the browser for batch conversion
  view: "inspect",    // "inspect" | "runs"
  jobs: [],           // /api/jobs list
  activeJobId: null,
  settings: null,     // conversion params (see convert.js)
};

export function update(patch) {
  Object.assign(state, patch);
  const keys = Object.keys(patch);
  for (const fn of listeners) fn(keys);
}

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export const storage = {
  get(key, fallback) {
    try {
      const raw = localStorage.getItem(`ptyzer.${key}`);
      return raw == null ? fallback : JSON.parse(raw);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try { localStorage.setItem(`ptyzer.${key}`, JSON.stringify(value)); } catch { /* storage unavailable */ }
  },
};

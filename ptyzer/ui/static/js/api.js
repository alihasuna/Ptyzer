// Thin client for the local Ptyzer server API.

async function request(method, url, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const res = await fetch(url, options);
  const isJson = (res.headers.get("Content-Type") || "").includes("application/json");
  const data = isJson ? await res.json() : null;
  if (!res.ok) throw new Error((data && data.error) || `${res.status} ${res.statusText}`);
  return data;
}

const q = encodeURIComponent;

export const api = {
  env: () => request("GET", "api/env"),
  browse: (path) => request("GET", `api/browse${path ? `?path=${q(path)}` : ""}`),
  inspect: (path) => request("GET", `api/hp/inspect?path=${q(path)}`),
  async coords(path) {
    const res = await fetch(`api/hp/coords?path=${q(path)}`);
    if (!res.ok) throw new Error(`Could not read coordinates (${res.status})`);
    return new Float32Array(await res.arrayBuffer());
  },
  overviewUrl: (path, v) => `api/hp/overview.png?path=${q(path)}&v=${v}`,
  frameUrl: (path, index, scale) => `api/hp/frame.png?path=${q(path)}&index=${index}&scale=${scale}`,
  meanUrl: (path, scale) => `api/hp/mean.png?path=${q(path)}&scale=${scale}`,
  outputTarget: (path, backend, outputDir) =>
    request("GET", `api/output_target?path=${q(path)}&backend=${q(backend || "py4dstem")}&output_dir=${q(outputDir || "")}`),
  outputUrl: (path, v) => `api/output.png?path=${q(path)}&v=${v || ""}`,
  figureUrl: (jobId, index) => `api/jobs/${jobId}/figures/${index}`,
  jobs: () => request("GET", "api/jobs"),
  submit: (files, params) => request("POST", "api/jobs", { files, params }),
  jobAction: (id, action) => request("POST", `api/jobs/${id}/${action}`, {}),
  sample: () => request("POST", "api/sample", {}),
  reveal: (path) => request("POST", "api/reveal", { path }),

  stream(jobId, onItem, onEnd) {
    const source = new EventSource(`api/jobs/${jobId}/stream`);
    source.onmessage = (event) => onItem(JSON.parse(event.data));
    source.addEventListener("end", () => { source.close(); onEnd && onEnd(); });
    return source;
  },
};

// Fetch an image endpoint as an ImageBitmap plus the stats the server reports in a header.
export async function fetchImage(url, signal) {
  const res = await fetch(url, { signal });
  if (!res.ok) {
    let message = res.statusText;
    try { message = (await res.json()).error || message; } catch { /* not JSON */ }
    throw new Error(message);
  }
  const stats = res.headers.get("X-Ptyzer-Stats");
  const bitmap = await createImageBitmap(await res.blob());
  return { bitmap, stats: stats ? JSON.parse(stats) : null };
}

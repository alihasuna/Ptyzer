// Keeps state.jobs fresh: polls quickly while anything is queued or running, slowly otherwise.

import { api } from "./api.js";
import { state, update } from "./state.js";

let timer = null;
let inflight = null;

export const isActive = (job) => job.status === "queued" || job.status === "running";

export function refreshJobs() {
  if (inflight) return inflight;
  inflight = api.jobs()
    .then(({ jobs }) => update({ jobs }))
    .catch(() => { /* server restarting; try again on the next tick */ })
    .finally(() => {
      inflight = null;
      clearTimeout(timer);
      timer = setTimeout(refreshJobs, state.jobs.some(isActive) ? 1000 : 5000);
    });
  return inflight;
}

export function showRuns(jobId) {
  update({ view: "runs", activeJobId: jobId || state.activeJobId });
}

"""
Conversion job queue for the UI.

Jobs run one at a time (the converter loads the whole diffraction stack into memory and Parallax
is CPU heavy), each in its own `python -m ptyzer.ui.worker` subprocess so a crash or cancel never
takes the UI down. Worker output is split into log lines, tqdm-style progress updates (carriage
return terminated) and structured events, and appended to the job's feed, which the server
streams to the browser.
"""
import codecs
import datetime
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import worker
from .hpfile import OUTPUT_DIRNAME

REPO_ROOT = Path(__file__).resolve().parents[2]
TERMINAL = ("done", "failed", "cancelled")
MAX_FEED = 50_000

DEFAULT_PARAMS = {
    "backend": "py4dstem",
    "beam_kV": 200.0,
    "recon_pix_size_pm": 25.0,
    "bf_disk_radius": None,
    "do_recentering": False,
    "centre_method": "simple",
    "do_save": True,
    "parallax": True,
    "aberrations": True,
    "plot_coord_checks": True,
    "plot_overview": True,
    "plot_virtual_diff": True,
    "plot_parallax_recon": True,
    "output_dir": "",
    "per_run_folder": True,
}


def normalize_params(raw):
    """Validate and coerce UI params; raises ValueError with a readable message."""
    p = dict(DEFAULT_PARAMS)
    raw = raw or {}
    unknown = set(raw) - set(p)
    if unknown:
        raise ValueError(f"Unknown parameters: {', '.join(sorted(unknown))}")
    p.update(raw)

    if p["backend"] not in ("py4dstem", "quantem"):
        raise ValueError("Engine must be py4dstem or quantem")

    def positive(name, label):
        try:
            value = float(p[name])
        except (TypeError, ValueError):
            raise ValueError(f"{label} must be a number")
        if not math.isfinite(value) or not value > 0:
            raise ValueError(f"{label} must be greater than zero")
        p[name] = value

    positive("beam_kV", "Beam energy")
    positive("recon_pix_size_pm", "Reconstruction pixel size")
    if p["bf_disk_radius"] in ("", None):
        p["bf_disk_radius"] = None
    else:
        positive("bf_disk_radius", "Bright-field disk radius")
    for name in ("do_recentering", "do_save", "parallax", "aberrations", "plot_coord_checks",
                 "plot_overview", "plot_virtual_diff", "plot_parallax_recon", "per_run_folder"):
        p[name] = bool(p[name])
    if p["centre_method"] not in ("fit", "simple"):
        raise ValueError("Centre method must be 'fit' or 'simple'")
    p["output_dir"] = str(p["output_dir"] or "").strip()
    return p


class Job:
    def __init__(self, file, params, output_dir):
        self.id = uuid.uuid4().hex[:10]
        self.file = file
        self.name = os.path.basename(file)
        self.params = params
        self.output_dir = output_dir
        self.status = "queued"
        self.created = time.time()
        self.started = None
        self.finished = None
        self.plan = worker.build_plan(params)
        self.stage = None
        self.stages = {}
        self.figures = []
        self.result = None
        self.error = None
        self.info = None
        self.progress = None
        self.feed = []
        self.cond = threading.Condition()
        self.proc = None
        self.cancel_requested = False
        self._last_progress = 0.0

    @property
    def terminal(self):
        return self.status in TERMINAL

    def push(self, item):
        with self.cond:
            now = time.time()
            item["t"] = now
            kind = item["type"]
            if kind == "status":
                self.status = item["status"]
                if self.status == "running":
                    self.started = now
                if self.status in TERMINAL:
                    self.finished = now
                    if self.stage and self.stages[self.stage]["end"] is None:
                        self.stages[self.stage]["end"] = now
                    self.progress = None
            elif kind == "stage":
                if self.stage and self.stages[self.stage]["end"] is None:
                    self.stages[self.stage]["end"] = now
                self.stage = item["key"]
                self.stages[item["key"]] = {"start": now, "end": None}
                self.progress = None
            elif kind == "plan":
                self.plan = item["stages"]
            elif kind == "figure":
                item["index"] = len(self.figures)
                self.figures.append({k: item[k] for k in ("index", "kind", "label", "path")})
            elif kind == "result":
                self.result = {k: v for k, v in item.items() if k not in ("type", "t")}
            elif kind == "error":
                self.error = {k: v for k, v in item.items() if k not in ("type", "t")}
            elif kind == "info":
                self.info = {k: v for k, v in item.items() if k not in ("type", "t")}
            elif kind == "progress":
                self.progress = item["text"]
            elif kind == "log":
                self.progress = None
            if len(self.feed) < MAX_FEED or kind != "log":
                item["seq"] = len(self.feed)
                self.feed.append(item)
            self.cond.notify_all()

    def to_json(self):
        with self.cond:
            return {
                "id": self.id, "file": self.file, "name": self.name, "status": self.status,
                "created": self.created, "started": self.started, "finished": self.finished,
                "output_dir": self.output_dir, "params": self.params, "plan": self.plan,
                "stage": self.stage, "stages": self.stages, "progress": self.progress,
                "figures": [{k: f[k] for k in ("index", "kind", "label")} for f in self.figures],
                "result": self.result, "error": self.error, "info": self.info,
                "feed_length": len(self.feed),
            }


class JobManager:
    def __init__(self, python=None, quantem_python=None):
        self.python = python or sys.executable
        self.quantem_python = quantem_python or self.python
        self.quantem_available = False
        self.jobs = {}
        self._lock = threading.Lock()
        self._queue = queue.Queue()
        self._reserved_dirs = set()
        threading.Thread(target=self._run_loop, name="ptyzer-jobs", daemon=True).start()

    # -- public API -------------------------------------------------------------------------

    def list(self):
        with self._lock:
            jobs = list(self.jobs.values())
        return [job.to_json() for job in sorted(jobs, key=lambda j: j.created, reverse=True)]

    def get(self, job_id):
        with self._lock:
            return self.jobs.get(job_id)

    def submit(self, files, raw_params):
        params = normalize_params(raw_params)
        if params["backend"] == "quantem" and not self.quantem_available:
            raise ValueError("Quantem is unavailable. Configure --quantem-python with an environment containing Quantem 0.1.9.")
        if not files:
            raise ValueError("No files given")
        for file in files:
            if not os.path.isfile(file):
                raise ValueError(f"Not a file: {file}")
        stamp = datetime.datetime.now()
        created = []
        with self._lock:
            for file in files:
                job = Job(os.path.abspath(file), params, self._output_dir(file, params, stamp))
                self.jobs[job.id] = job
                created.append(job)
        for job in created:
            self._queue.put(job.id)
        return created

    def retry(self, job_id):
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return self.submit([job.file], job.params)[0]

    def cancel(self, job_id):
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        with job.cond:
            if job.terminal:
                return job
            job.cancel_requested = True
            proc = job.proc
            queued = job.status == "queued"
            if queued:
                job.push({"type": "status", "status": "cancelled"})
        if proc is not None and proc.poll() is None:
            proc.terminate()
            threading.Timer(5.0, lambda: proc.poll() is None and proc.kill()).start()
        return job

    def remove(self, job_id):
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if not job.terminal:
            raise ValueError("Cancel the job before removing it")
        with self._lock:
            self.jobs.pop(job_id, None)

    # -- internals --------------------------------------------------------------------------

    def _output_dir(self, file, params, stamp):
        root = "ReformattedForQuantem" if params["backend"] == "quantem" else OUTPUT_DIRNAME
        base = params["output_dir"] or os.path.join(os.path.dirname(os.path.abspath(file)), root)
        base = os.path.abspath(os.path.expanduser(base))
        if not params["per_run_folder"]:
            return base
        stem = os.path.splitext(os.path.basename(file))[0]
        candidate = os.path.join(base, f"{stem}_{stamp:%Y%m%d-%H%M%S}")
        n = 2
        unique = candidate
        while unique in self._reserved_dirs or os.path.exists(unique):
            unique = f"{candidate}-{n}"
            n += 1
        self._reserved_dirs.add(unique)
        return unique

    def _run_loop(self):
        while True:
            job = self.get(self._queue.get())
            if job is None or job.terminal or job.cancel_requested:
                continue
            try:
                self._run(job)
            except Exception as exc:  # never let one job kill the queue
                job.push({"type": "error", "type_name": type(exc).__name__,
                          "message": f"Could not run the worker: {exc}", "hint": None})
                job.push({"type": "status", "status": "failed"})

    def _run(self, job):
        os.makedirs(job.output_dir, exist_ok=True)
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")]))
        env.update(MPLBACKEND="Agg", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        python = self.quantem_python if job.params["backend"] == "quantem" else self.python
        # Hold the condition through process publication and input delivery so cancellation
        # cannot mistake a starting process for a queued job with no process to terminate.
        with job.cond:
            if job.cancel_requested or job.terminal:
                return
            proc = subprocess.Popen(
                [python, "-u", "-m", "ptyzer.ui.worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=0, env=env, cwd=job.output_dir,
            )
            job.proc = proc
            job.push({"type": "status", "status": "running"})
            proc.stdin.write(json.dumps({"file": job.file, "output_dir": job.output_dir,
                                         "params": job.params}).encode("utf-8"))
            proc.stdin.close()

        self._pump(job, proc)
        code = proc.wait()
        if job.cancel_requested:
            job.push({"type": "log", "text": "Cancelled."})
            job.push({"type": "status", "status": "cancelled"})
        elif code == 0 and job.result is not None:
            job.push({"type": "status", "status": "done"})
        else:
            if job.error is None:
                hint = ("The worker was killed, possibly because the system ran out of memory."
                        if code < 0 or code == 137 else None)
                job.push({"type": "error", "type_name": "WorkerExit",
                          "message": f"The conversion process exited with code {code}.", "hint": hint})
            job.push({"type": "status", "status": "failed"})

    def _pump(self, job, proc):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buffer = ""
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            buffer = self._consume(job, buffer + decoder.decode(chunk), final=False)
        self._consume(job, buffer + decoder.decode(b"", final=True), final=True)

    def _consume(self, job, buffer, final):
        start = 0
        length = len(buffer)
        while start < length:
            cr = buffer.find("\r", start)
            lf = buffer.find("\n", start)
            ends = [i for i in (cr, lf) if i != -1]
            if not ends:
                break
            end = min(ends)
            if buffer[end] == "\r":
                if end + 1 >= length and not final:
                    break  # need the next character to tell \r from \r\n
                if buffer.startswith("\n", end + 1):
                    self._segment(job, buffer[start:end], "log")
                    start = end + 2
                else:
                    self._segment(job, buffer[start:end], "progress")
                    start = end + 1
            else:
                self._segment(job, buffer[start:end], "log")
                start = end + 1
        rest = buffer[start:]
        if final and rest:
            self._segment(job, rest, "log")
            rest = ""
        return rest

    def _segment(self, job, text, kind):
        marker = text.find(worker.EVENT_PREFIX)
        if marker != -1:
            if text[:marker].strip():
                self._segment(job, text[:marker], kind)
            try:
                event = json.loads(text[marker + len(worker.EVENT_PREFIX):])
            except json.JSONDecodeError:
                job.push({"type": "log", "text": text})
                return
            name = event.pop("event", None)
            if name == "error":
                event["type_name"] = event.pop("type", None)
            event["type"] = name
            if name in ("plan", "stage", "figure", "result", "error", "info"):
                job.push(event)
            return
        if kind == "progress":
            if not text.strip():
                return
            now = time.time()
            if now - job._last_progress < 0.2 and "100%" not in text:
                with job.cond:
                    job.progress = text
                return
            job._last_progress = now
            job.push({"type": "progress", "text": text})
        else:
            job.push({"type": "log", "text": text.rstrip()})

"""
Local HTTP server for the Ptyzer UI, built on the standard library only.

Serves the single-page app from ./static and a small JSON API:

  GET  /api/env                          interpreter/package versions and compatibility checks
  GET  /api/browse?path=                 directories and .hp files in a folder
  GET  /api/hp/inspect?path=             pre-flight checks, summary, metadata, geometry
  GET  /api/hp/coords?path=              scan coordinates (float32 pairs, volts)
  GET  /api/hp/overview.png?path=        overview micrograph
  GET  /api/hp/frame.png?path=&index=    one diffraction pattern (&scale=log|linear)
  GET  /api/hp/mean.png?path=            mean of a sample of diffraction patterns
  GET  /api/output.png?path=             a QC figure written by an earlier conversion
  GET  /api/jobs                         all jobs
  GET  /api/jobs/<id>/stream             job feed as server-sent events (log, stages, figures, result)
  GET  /api/jobs/<id>/figures/<n>        a figure produced by a job
  POST /api/jobs                         {"files": [...], "params": {...}} queue conversions
  POST /api/jobs/<id>/cancel | retry | remove
  POST /api/sample                       write a synthetic .hp dataset
  POST /api/reveal                       {"path": ...} show a file/folder in the OS file manager

The server is meant to run on the loopback interface. Requests with a foreign Host header are
rejected (DNS-rebinding protection) and state-changing requests must be JSON from the same origin.
"""
import json
import os
import re
import string
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import hpfile
from .jobs import DEFAULT_PARAMS, JobManager

STATIC_DIR = Path(__file__).resolve().parent / "static"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
    ".json": "application/json", ".ico": "image/x-icon",
}
LOOPBACK_NAMES = {"127.0.0.1", "localhost", "[::1]", "::1"}


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _clean(value):
    """Replace non-finite floats (which JSON can't carry) with None, recursively."""
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _natural_key(name):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


class App:
    def __init__(self, start_dir, loopback_only=True, verbose=False, quantem_python=None):
        self.start_dir = os.path.abspath(os.path.expanduser(start_dir))
        self.loopback_only = loopback_only
        self.verbose = verbose
        from .backends import quantem_python as resolve_python
        self.jobs = JobManager(quantem_python=resolve_python(quantem_python))
        self.sample_dir = os.path.join(tempfile.gettempdir(), "ptyzer-sample")
        self._env = None
        self._env_lock = threading.Lock()
        # Import py4DSTEM in the background so the first inspection is quick.
        threading.Thread(target=self.environment, daemon=True).start()

    def environment(self):
        with self._env_lock:
            if self._env is None:
                self._env = hpfile.environment()
                from .backends import probe_quantem
                quantem_info = probe_quantem(self.jobs.quantem_python)
                self.jobs.quantem_available = quantem_info["available"]
                self._env["backends"] = {
                    "py4dstem": {"available": self._env["converter_ok"], "version": self._env["packages"].get("py4DSTEM"),
                                  "error": self._env.get("converter_error"), "output_format": ".h5"},
                    "quantem": quantem_info,
                }
                self._env.update(start_dir=self.start_dir, sample_dir=self.sample_dir,
                                 home=os.path.expanduser("~"), defaults=DEFAULT_PARAMS,
                                 os=os.name, sep=os.sep)
            return self._env

    # -- browsing -------------------------------------------------------------------------------

    def places(self):
        places = [{"label": "Home", "path": os.path.expanduser("~")},
                  {"label": "Launch folder", "path": self.start_dir}]
        if os.path.isdir(self.sample_dir):
            places.append({"label": "Sample data", "path": self.sample_dir})
        if os.name == "nt":
            for letter in string.ascii_uppercase:
                drive = f"{letter}:\\"
                if os.path.exists(drive):
                    places.append({"label": drive, "path": drive})
        else:
            places.append({"label": "/", "path": "/"})
            if sys.platform == "darwin" and os.path.isdir("/Volumes"):
                places.append({"label": "Volumes", "path": "/Volumes"})
        seen, unique = set(), []
        for place in places:
            if place["path"] not in seen:
                seen.add(place["path"])
                unique.append(place)
        return unique

    def browse(self, path):
        path = os.path.abspath(os.path.expanduser(path or self.start_dir))
        if os.path.isfile(path):
            path = os.path.dirname(path)
        if not os.path.isdir(path):
            raise HttpError(HTTPStatus.NOT_FOUND, f"Folder not found: {path}")
        dirs, files = [], []
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    try:
                        if entry.is_dir():
                            dirs.append({"name": entry.name, "path": entry.path, "kind": "dir"})
                        elif entry.name.lower().endswith(".hp") and entry.is_file():
                            st = entry.stat()
                            files.append({"name": entry.name, "path": entry.path, "kind": "hp",
                                          "size": st.st_size, "mtime": st.st_mtime})
                    except OSError:
                        continue
        except PermissionError:
            raise HttpError(HTTPStatus.FORBIDDEN, f"Permission denied: {path}")

        converted = set()
        out_root = os.path.join(path, hpfile.OUTPUT_DIRNAME)
        if files and os.path.isdir(out_root):
            try:
                names = os.listdir(out_root)
                for f in files:
                    stem = os.path.splitext(f["name"])[0]
                    if stem + "_py4.h5" in names or any(n.startswith(stem + "_") and
                                                        os.path.isfile(os.path.join(out_root, n, stem + "_py4.h5"))
                                                        for n in names):
                        converted.add(f["path"])
            except OSError:
                pass
        quantem_root = os.path.join(path, "ReformattedForQuantem")
        if files and os.path.isdir(quantem_root):
            try:
                names = os.listdir(quantem_root)
                for f in files:
                    stem = os.path.splitext(f["name"])[0]
                    suffix = stem + "_quantem.zarr.zip"
                    if suffix in names or any(n.startswith(stem + "_") and
                                              os.path.isfile(os.path.join(quantem_root, n, suffix)) for n in names):
                        converted.add(f["path"])
            except OSError:
                pass

        for f in files:
            f["converted"] = f["path"] in converted

        crumbs = []
        head = path
        while True:
            parent, name = os.path.split(head)
            crumbs.append({"name": name or head, "path": head})
            if not name or parent == head:
                break
            head = parent
        crumbs.reverse()
        parent = os.path.dirname(path)
        return {
            "path": path, "parent": parent if parent != path else None, "crumbs": crumbs,
            "entries": sorted(dirs, key=lambda e: _natural_key(e["name"]))
                       + sorted(files, key=lambda e: _natural_key(e["name"])),
            "places": self.places(),
        }

    def reveal(self, path):
        path = os.path.abspath(path)
        if not os.path.exists(path):
            raise HttpError(HTTPStatus.NOT_FOUND, f"Not found: {path}")
        if sys.platform == "darwin":
            cmd = ["open", "-R", path] if os.path.isfile(path) else ["open", path]
        elif os.name == "nt":
            cmd = ["explorer", f"/select,{path}"] if os.path.isfile(path) else ["explorer", path]
        else:
            cmd = ["xdg-open", path if os.path.isdir(path) else os.path.dirname(path)]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PtyzerUI/1.0"

        def log_message(self, fmt, *args):
            if app.verbose:
                sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        # -- plumbing ---------------------------------------------------------------------------

        def _send(self, status, body, content_type, headers=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, data, status=HTTPStatus.OK):
            self._send(status, json.dumps(_clean(data), allow_nan=False, default=str).encode("utf-8"),
                       "application/json")

        def _query(self):
            parsed = urlparse(self.path)
            return parsed.path, {k: v[-1] for k, v in parse_qs(parsed.query).items()}

        def _file_param(self, query):
            path = query.get("path")
            if not path:
                raise HttpError(HTTPStatus.BAD_REQUEST, "Missing 'path'")
            path = os.path.abspath(os.path.expanduser(path))
            if not os.path.isfile(path):
                raise HttpError(HTTPStatus.NOT_FOUND, f"File not found: {path}")
            return path

        def _host_ok(self):
            if not app.loopback_only:
                return True
            host = self.headers.get("Host", "")
            name = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
            return name in LOOPBACK_NAMES

        def _same_origin(self):
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            return urlparse(origin).netloc == self.headers.get("Host")

        def _dispatch(self, routes):
            if not self._host_ok():
                return self._json({"error": "Host not allowed"}, HTTPStatus.FORBIDDEN)
            path, query = self._query()
            try:
                for pattern, handler in routes:
                    match = re.fullmatch(pattern, path)
                    if match:
                        return handler(query, *match.groups())
                raise HttpError(HTTPStatus.NOT_FOUND, "Not found")
            except HttpError as exc:
                self._json({"error": exc.message}, exc.status)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                traceback.print_exc()
                try:
                    self._json({"error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                except Exception:
                    pass

        def do_GET(self):
            self._dispatch([
                (r"/", self.index),
                (r"/static/(.+)", self.static),
                (r"/api/env", lambda q: self._json(app.environment())),
                (r"/api/browse", lambda q: self._json(app.browse(q.get("path")))),
                (r"/api/hp/inspect", self.inspect),
                (r"/api/hp/coords", self.coords),
                (r"/api/hp/overview\.png", self.overview),
                (r"/api/hp/frame\.png", self.frame),
                (r"/api/hp/mean\.png", self.mean),
                (r"/api/output\.png", self.output_png),
                (r"/api/jobs", lambda q: self._json({"jobs": app.jobs.list()})),
                (r"/api/jobs/([0-9a-f]+)", lambda q, jid: self._json(self._job(jid).to_json())),
                (r"/api/jobs/([0-9a-f]+)/stream", self.stream),
                (r"/api/jobs/([0-9a-f]+)/figures/(\d+)", self.job_figure),
            ])

        def do_POST(self):
            if not self._same_origin():
                return self._json({"error": "Cross-origin request refused"}, HTTPStatus.FORBIDDEN)
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._json({"error": "Expected application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                self.body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "Invalid JSON"}, HTTPStatus.BAD_REQUEST)
            self._dispatch([
                (r"/api/jobs", self.submit),
                (r"/api/jobs/([0-9a-f]+)/(cancel|retry|remove)", self.job_action),
                (r"/api/sample", self.sample),
                (r"/api/reveal", self.reveal),
            ])

        # -- static -----------------------------------------------------------------------------

        def index(self, query):
            self._send(HTTPStatus.OK, (STATIC_DIR / "index.html").read_bytes(), CONTENT_TYPES[".html"])

        def static(self, query, rel):
            target = (STATIC_DIR / rel).resolve()
            if STATIC_DIR not in target.parents or not target.is_file():
                raise HttpError(HTTPStatus.NOT_FOUND, "Not found")
            self._send(HTTPStatus.OK, target.read_bytes(),
                       CONTENT_TYPES.get(target.suffix, "application/octet-stream"))

        # -- .hp inspection ---------------------------------------------------------------------

        def inspect(self, query):
            self._json(hpfile.inspect_hp(self._file_param(query)))

        def coords(self, query):
            self._send(HTTPStatus.OK, hpfile.coords_bytes(self._file_param(query)), "application/octet-stream")

        def overview(self, query):
            self._send(HTTPStatus.OK, hpfile.overview_png(self._file_param(query)), "image/png")

        def frame(self, query):
            try:
                index = int(query.get("index", 0))
            except ValueError:
                raise HttpError(HTTPStatus.BAD_REQUEST, "'index' must be an integer")
            png, stats = hpfile.frame_png(self._file_param(query), index, query.get("scale", "log"))
            self._send(HTTPStatus.OK, png, "image/png", {"X-Ptyzer-Stats": json.dumps(stats)})

        def mean(self, query):
            png, stats = hpfile.mean_frame_png(self._file_param(query), scale=query.get("scale", "log"))
            self._send(HTTPStatus.OK, png, "image/png", {"X-Ptyzer-Stats": json.dumps(stats)})

        def output_png(self, query):
            path = self._file_param(query)
            if not hpfile.is_output_figure(path):
                raise HttpError(HTTPStatus.FORBIDDEN, "Only Ptyzer output figures can be served")
            self._send(HTTPStatus.OK, Path(path).read_bytes(), "image/png")

        # -- jobs -------------------------------------------------------------------------------

        def _job(self, job_id):
            job = app.jobs.get(job_id)
            if job is None:
                raise HttpError(HTTPStatus.NOT_FOUND, "Job not found")
            return job

        def submit(self, query):
            files = self.body.get("files") or []
            if not isinstance(files, list):
                raise HttpError(HTTPStatus.BAD_REQUEST, "'files' must be a list")
            try:
                jobs = app.jobs.submit([os.path.abspath(os.path.expanduser(str(f))) for f in files],
                                       self.body.get("params"))
            except ValueError as exc:
                raise HttpError(HTTPStatus.BAD_REQUEST, str(exc))
            self._json({"jobs": [job.to_json() for job in jobs]}, HTTPStatus.CREATED)

        def job_action(self, query, job_id, action):
            self._job(job_id)
            try:
                if action == "cancel":
                    self._json(app.jobs.cancel(job_id).to_json())
                elif action == "retry":
                    self._json(app.jobs.retry(job_id).to_json(), HTTPStatus.CREATED)
                else:
                    app.jobs.remove(job_id)
                    self._json({"removed": job_id})
            except ValueError as exc:
                raise HttpError(HTTPStatus.CONFLICT, str(exc))

        def job_figure(self, query, job_id, index):
            job = self._job(job_id)
            index = int(index)
            if index >= len(job.figures) or not os.path.isfile(job.figures[index]["path"]):
                raise HttpError(HTTPStatus.NOT_FOUND, "Figure not found")
            self._send(HTTPStatus.OK, Path(job.figures[index]["path"]).read_bytes(), "image/png")

        def stream(self, query, job_id):
            job = self._job(job_id)
            try:
                cursor = int(query.get("from") or 0)
                last_id = self.headers.get("Last-Event-ID")
                if last_id is not None:
                    cursor = int(last_id) + 1
            except ValueError:
                cursor = 0
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            last_write = time.time()
            try:
                while True:
                    with job.cond:
                        if cursor >= len(job.feed) and not job.terminal:
                            job.cond.wait(timeout=10)
                        items = job.feed[cursor:cursor + 500]
                        finished = job.terminal and cursor + len(items) >= len(job.feed)
                    if items:
                        payload = "".join(f"id: {item['seq']}\ndata: {json.dumps(item, default=str)}\n\n"
                                          for item in items)
                        self.wfile.write(payload.encode("utf-8"))
                        cursor += len(items)
                        last_write = time.time()
                    if finished:
                        self.wfile.write(b"event: end\ndata: {}\n\n")
                        self.wfile.flush()
                        return
                    if time.time() - last_write > 10:
                        self.wfile.write(b": ping\n\n")
                        last_write = time.time()
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

        # -- misc -------------------------------------------------------------------------------

        def sample(self, query):
            from .sample_data import write_sample_hp
            path = os.path.join(app.sample_dir, "Synthetic Lattice Dataset.hp")
            write_sample_hp(path)
            self._json({"path": path, "dir": app.sample_dir}, HTTPStatus.CREATED)

        def reveal(self, query):
            path = self.body.get("path")
            if not path:
                raise HttpError(HTTPStatus.BAD_REQUEST, "Missing 'path'")
            app.reveal(path)
            self._json({"ok": True})

    return Handler


def serve(host="127.0.0.1", port=8765, start_dir=None, verbose=False, quantem_python=None):
    """Create the server (not yet serving). Port 0 picks a free port."""
    loopback_only = host in LOOPBACK_NAMES
    app = App(start_dir or os.getcwd(), loopback_only=loopback_only, verbose=verbose, quantem_python=quantem_python)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    return httpd, app

"""
Engine-neutral pieces of a conversion run, shared by the py4DSTEM and Quantem workers:

  preflight(file, backend)        the Inspect pre-flight checks as a gate before any engine runs
  fingerprint(file)               a hash identifying the acquisition in a .hp file
  write_record(folder, stem, ...) <stem>_ptyzer.json: what ran, on what, with which conventions
  append_azorus_raw(h5, file)     the raw Azorus metadata and scan voltages, added to a py4DSTEM .h5

Only the standard library, numpy and h5py, so it imports in either engine's environment.
"""
import datetime
import hashlib
import json
import math
import os
import platform
import sys

import h5py
import numpy as np

RECORD_SCHEMA = "ptyzer-run/1"
RAW_GROUP = "ptyzer_azorus_raw"

# Stated once, written into every record: what the reported numbers mean.
CONVENTIONS = {
    "defocus_nm": ("Defocus C1 (py4DSTEM) or C10 (Quantem), in nm. Both engines report the same sign: positive "
                   "when bright-field pixels see the specimen displaced along their probe angle "
                   "(tests/test_ground_truth.py)."),
    "astigmatism_nm": "Two-fold astigmatism magnitude |C12|, in nm.",
    "cs_mm": "Spherical aberration C3 / C30, in mm; null when the method does not fit it.",
    "rotation_deg": ("Scan-to-detector (Q to R) rotation, degrees counter-clockwise. Quantem reads +1.9 to +2.3 deg "
                     "higher than py4DSTEM on the synthetic sample."),
    "transpose": ("True when the scan and detector have opposite handedness (set by the user; one dataset cannot "
                  "reveal it)."),
}


class PreflightError(Exception):
    def __init__(self, failures):
        self.failures = failures
        super().__init__("; ".join(f"{c['title']}: {c['detail']}" if c.get("detail") else c["title"] for c in failures))


def preflight(file, backend):
    """
    Run the Inspect view's pre-flight checks (hpfile.inspect_hp) and stop on problems either engine
    would trip over. Returns (checks, warnings); raises PreflightError on a failure.

    Hard for both engines: any failed check, non-square patterns (the calibration assumes square),
    non-finite scan coordinates or calibration. Raster orientation is a warning for py4DSTEM, whose
    coordinate check figure exists to diagnose it, and a failure for Quantem, which only supports
    the expected raster.
    """
    from .hpfile import inspect_hp
    info = inspect_hp(file)
    checks = [dict(c) for c in info["checks"]]
    with h5py.File(file, "r") as f:
        if "coords" in f:
            coords = np.asarray(f["coords"], dtype=float)
            if coords.size and not np.isfinite(coords).all():
                checks.append({"id": "coords_finite", "status": "fail", "title": "Scan coordinates aren't finite",
                               "detail": f"{int((~np.isfinite(coords)).sum())} coordinate values are NaN or infinite."})
    nm_per_V = (info.get("geometry") or {}).get("nm_per_V")
    if nm_per_V is not None and not math.isfinite(nm_per_V):
        checks.append({"id": "scan_calibration", "status": "fail", "title": "Scan calibration isn't finite", "detail": ""})
    for c in checks:
        if backend == "quantem" and c["id"] == "orientation" and c["status"] == "warn":
            c["status"] = "fail"
            c["detail"] = (c.get("detail") or "") + " The Quantem engine only supports the expected raster."
    failures = [c for c in checks if c["status"] == "fail"]
    if failures:
        raise PreflightError(failures)
    return checks, [c for c in checks if c["status"] == "warn"]


def fingerprint(file):
    """
    sha256 over what identifies the acquisition: the raw metadata JSON ('attrs', 'diffraction/meta'),
    the scan coordinates and the diffraction stack's shape and dtype. Cheap on any file size; it does
    not hash the intensities (use a full-file checksum for archival integrity).
    """
    h = hashlib.sha256()
    with h5py.File(file, "r") as f:
        for key in ("attrs", "diffraction/meta"):
            if key in f:
                raw = f[key][()]
                h.update(key.encode() + b"\0" + (raw if isinstance(raw, bytes) else str(raw).encode()))
        if "coords" in f:
            coords = np.ascontiguousarray(f["coords"][()])
            h.update(b"coords\0" + coords.dtype.str.encode() + repr(coords.shape).encode() + coords.tobytes())
        if "diffraction/micrograph" in f:
            d = f["diffraction/micrograph"]
            h.update(b"stack\0" + repr(tuple(d.shape)).encode() + str(d.dtype).encode())
    return {"sha256": h.hexdigest(),
            "covers": "attrs and diffraction/meta JSON, scan coordinates, diffraction stack shape and dtype"}


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def write_record(folder, stem, *, file, params, engine, result, checks, warnings, figures, started):
    """Write <stem>_ptyzer.json next to the run's outputs and return its path."""
    st = os.stat(file)
    numpy_version = np.__version__
    record = {
        "schema": RECORD_SCHEMA,
        "created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "engine": dict(engine, python=sys.version.split()[0], numpy=numpy_version, platform=platform.platform()),
        "input": {"path": os.path.abspath(file), "size": st.st_size,
                  "mtime": datetime.datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds"),
                  "fingerprint": fingerprint(file)},
        "parameters": params,
        "preflight": checks,
        "calibration": {k: result.get(k) for k in ("datacube_shape", "R_pixel_size", "R_pixel_units", "Q_pixel_size",
                                                  "Q_pixel_units", "diffraction_angular_FOV_mrad") if k in result},
        "aberrations": result.get("aberrations"),
        "conventions": CONVENTIONS,
        "warnings": [w.get("title") + (f": {w['detail']}" if w.get("detail") else "") for w in warnings]
                    + [result[k] for k in ("aberrations_warning", "aberrations_error") if result.get(k)],
        "outputs": {"dataset": result.get("output_file"), "format": result.get("output_format"), "figures": figures},
        "elapsed_s": round(datetime.datetime.now().timestamp() - started, 3),
    }
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{stem}_ptyzer.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(_jsonable(record), fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return path


def append_azorus_raw(h5_path, file):
    """
    Add the Azorus file's raw metadata JSON (byte for byte) and raw scan voltages to a py4DSTEM .h5,
    in a root-level '/ptyzer_azorus_raw' group that py4DSTEM.read ignores. The converter itself stores
    decoded metadata, which cannot hold every typed field losslessly.
    """
    with h5py.File(file, "r") as src, h5py.File(h5_path, "a") as dst:
        if RAW_GROUP in dst:
            del dst[RAW_GROUP]
        g = dst.create_group(RAW_GROUP)
        g.attrs["source_file"] = os.path.abspath(file)
        g.attrs["description"] = "Raw Azorus .hp metadata JSON and scan coordinates (volts), copied unchanged by Ptyzer."
        for key, name in (("attrs", "attrs_json"), ("diffraction/meta", "diffraction_meta_json")):
            if key in src:
                raw = src[key][()]
                g.create_dataset(name, data=raw.decode("utf-8") if isinstance(raw, bytes) else raw,
                                 dtype=h5py.string_dtype("utf-8"))
        if "coords" in src:
            ds = g.create_dataset("coords_V", data=src["coords"][()])
            ds.attrs["columns"] = "row (y) voltage, column (x) voltage"
    return h5_path

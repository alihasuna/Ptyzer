"""
Read-only inspection of Azorus .hp files for the UI: pre-flight checks, acquisition summary,
decoded metadata, scan coordinates, and quick-look images (overview micrograph and individual
diffraction patterns).

Metadata is decoded with the converter's own helpers (unpack_diffraction_meta_all etc. from
ptyzer.io.azohp_to_py4d), so what the UI shows is exactly what azohp_to_py4d will read.
Nothing here loads the full diffraction stack; only single frames or small samples are read.
"""
import importlib
import importlib.metadata
import inspect
import os
import platform
import re
import struct
import sys
import threading
import uuid
import zlib

import h5py
import numpy as np

REQUIRED_DATASETS = {
    "diffraction/meta": "acquisition metadata (JSON)",
    "attrs": "scan metadata (JSON)",
    "coords": "scan coordinates",
    "diffraction/micrograph": "diffraction stack",
    "overview/micrograph": "overview micrograph",
}

OUTPUT_DIRNAME = "ReformattedForPy4DSTEM"
FIGURE_KINDS = ("coord_checks", "overview", "virtual_diff", "parallax_recon")

_converter = None
_converter_error = None
_converter_lock = threading.Lock()


def converter():
    """Import (once) and return the ptyzer.io.azohp_to_py4d module."""
    global _converter, _converter_error
    with _converter_lock:
        if _converter is None and _converter_error is None:
            os.environ.setdefault("MPLBACKEND", "Agg")
            try:
                _converter = importlib.import_module("ptyzer.io.azohp_to_py4d")
            except Exception as exc:  # reported through environment()
                _converter_error = f"{type(exc).__name__}: {exc}"
        if _converter is None:
            raise RuntimeError(f"Could not import the Ptyzer converter: {_converter_error}")
        return _converter


# --------------------------------------------------------------------------------------------
# Environment / compatibility
# --------------------------------------------------------------------------------------------

def _pkg_version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def environment():
    """Interpreter, package versions, and feature checks for the py4DSTEM code paths Ptyzer uses."""
    info = {
        "python": platform.python_version(),
        "executable": sys.executable,
        "platform": platform.platform(),
        "packages": {name: _pkg_version(name) for name in
                     ("py4DSTEM", "numpy", "h5py", "matplotlib", "emdfile")},
        "converter_ok": False,
        "converter_error": None,
        "checks": [],
    }
    try:
        converter()
        info["converter_ok"] = True
    except RuntimeError as exc:
        info["converter_error"] = str(exc)
        return info

    checks = info["checks"]
    try:
        from py4DSTEM.process.phase import Parallax
    except Exception as exc:
        checks.append({"id": "parallax", "status": "fail", "affects": ["parallax", "aberrations"],
                       "title": "Parallax is unavailable",
                       "detail": f"py4DSTEM.process.phase.Parallax could not be imported ({exc})."})
        return info

    def source(fn):
        try:
            return inspect.getsource(fn)
        except (OSError, TypeError):
            return None

    recon_src = source(Parallax.reconstruct)
    numpy_major = int((info["packages"]["numpy"] or "0").split(".")[0])
    if recon_src is not None and numpy_major >= 2 and "float(self._recon_error)" in recon_src:
        checks.append({
            "id": "numpy2", "status": "warn", "affects": ["parallax", "aberrations"],
            "title": "Parallax reconstruction may fail under numpy 2",
            "detail": ("py4DSTEM's Parallax.reconstruct calls float() on a 1-element array, which "
                       "numpy 2 rejects (see the note in azohp_to_py4d.py). Use numpy<2."),
        })
    if not hasattr(Parallax, "_visualize_figax"):
        checks.append({
            "id": "visualize_figax", "status": "fail", "affects": ["parallax_figure"],
            "title": "Parallax summary figure will fail",
            "detail": "This py4DSTEM's Parallax has no _visualize_figax, which plot_parallax_summary uses.",
        })
    return info


def total_memory_bytes():
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        pass
    if os.name == "nt":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullTotalPhys)
    return None


# --------------------------------------------------------------------------------------------
# Inspection
# --------------------------------------------------------------------------------------------

def _json_scalar(value):
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not np.isfinite(value):
        return str(value)
    return value


def _describe(value):
    """(type label, display string) for a decoded metadata leaf."""
    if isinstance(value, np.ndarray):
        label = f"ndarray {value.dtype} {'×'.join(map(str, value.shape)) or 'scalar'}"
        flat = value.ravel()
        shown = np.array2string(flat[:12], precision=6, separator=", ", max_line_width=10**6)
        if flat.size > 12:
            shown = shown[:-1] + ", …]"
        return label, shown
    if isinstance(value, uuid.UUID):
        return "uuid", str(value)
    if isinstance(value, (bool, np.bool_)):
        return "bool", str(bool(value))
    if isinstance(value, (int, np.integer)):
        return "int", str(int(value))
    if isinstance(value, (float, np.floating)):
        return "float", f"{float(value):.8g}"
    if isinstance(value, str):
        return "str", value
    if value is None:
        return "null", "null"
    if isinstance(value, (list, tuple)):
        text = repr(list(value))
        return f"list[{len(value)}]", text if len(text) <= 240 else text[:239] + "…"
    if isinstance(value, dict):
        return "dict", "{}" if not value else repr(value)[:240]
    return type(value).__name__, repr(value)[:240]


def _flat_items(flat):
    items = []
    for key, value in flat.items():
        kind, text = _describe(value)
        items.append({"key": key, "type": kind, "value": text})
    return items


def _get(d, *keys):
    for k in keys:
        try:
            d = d[k]
        except (KeyError, IndexError, TypeError):
            return None
    return d


def _check(checks, cid, status, title, detail=""):
    checks.append({"id": cid, "status": status, "title": title, "detail": detail})


def raster_check(x, y):
    """
    Check that scan positions, reshaped in C order to the mesh as azohp_to_py4d does, form the
    raster its coordinate check figure expects: x increasing along columns and y decreasing down
    rows. Returns (status, title, detail) or None if the mesh is too small to tell.
    """
    rows, cols = x.shape
    if rows < 2 or cols < 2:
        return None
    dxc, dyc = np.diff(x, axis=1), np.diff(y, axis=1)   # one column step
    dxr, dyr = np.diff(x, axis=0), np.diff(y, axis=0)   # one row step
    col_step = np.array([np.median(dxc), np.median(dyc)])
    row_step = np.array([np.median(dxr), np.median(dyr)])
    col_len, row_len = np.hypot(*col_step), np.hypot(*row_step)
    figure_hint = "Compare with the scan-coordinate check figure before trusting the conversion."
    if col_len == 0 or row_len == 0:
        return ("warn", "Scan positions don't advance along the mesh",
                "Neighbouring positions share coordinates after reshaping to the mesh. " + figure_hint)

    # Share of individual steps pointing the same way as the typical step: a saw-tooth or
    # scrambled order (e.g. coordinates listed in a different order than the reshape assumes) fails this.
    agree_c = np.mean((dxc * col_step[0] + dyc * col_step[1]) > 0.5 * col_len ** 2)
    agree_r = np.mean((dxr * row_step[0] + dyr * row_step[1]) > 0.5 * row_len ** 2)
    if min(agree_c, agree_r) < 0.9:
        return ("warn", "Scan order doesn't form a clean raster",
                f"Only {100 * min(agree_c, agree_r):.0f}% of steps follow the mesh direction after reshaping, "
                "which gives the saw-tooth pattern the converter warns about: the stored order likely differs "
                "from the row-by-row order the reshape assumes. " + figure_hint)

    col_angle = np.degrees(np.arctan2(col_step[1], col_step[0]))
    handedness = col_step[0] * row_step[1] - col_step[1] * row_step[0]   # negative for the expected layout
    if abs(col_angle) > 45:
        return ("warn", "Rows and columns look swapped",
                f"Moving along a column changes position at {col_angle:.0f}° instead of along +x, so the "
                "data may be transposed or rotated by a quarter turn. " + figure_hint)
    if handedness > 0:
        return ("warn", "One scan axis runs the opposite way",
                "x and y don't both follow the expected directions (x increasing along columns, y decreasing "
                "down rows); the scan-voltage-to-beam-position convention may differ from the system the "
                "converter was tested on. " + figure_hint)
    if abs(col_angle) > 5:
        return ("warn", f"Scan grid is rotated by about {col_angle:.0f}°",
                "Consistent with a rotation applied in Azorus before saving. The average step size is estimated "
                "along the unrotated axes.")
    return ("ok", "Scan raster orientation as expected", "x increases along columns, y decreases down rows")


def existing_outputs(hp_path):
    """Outputs of earlier conversions in the default output folder (and its per-run subfolders)."""
    stem = os.path.splitext(os.path.basename(hp_path))[0]
    found = []
    for root, suffix, engine in ((OUTPUT_DIRNAME, "_py4.h5", "py4dstem"),
                                 ("ReformattedForQuantem", "_quantem.zarr.zip", "quantem")):
        out_root = os.path.join(os.path.dirname(hp_path), root)
        if not os.path.isdir(out_root):
            continue
        candidates = [out_root]
        try:
            candidates += sorted((e.path for e in os.scandir(out_root)
                                  if e.is_dir() and e.name.startswith(stem + "_")), reverse=True)
        except OSError:
            pass
        for folder in candidates:
            output = os.path.join(folder, stem + suffix)
            figures = [{"kind": kind, "path": os.path.join(folder, f"{stem}_{kind}.png")}
                       for kind in FIGURE_KINDS if os.path.isfile(os.path.join(folder, f"{stem}_{kind}.png"))]
            exists = os.path.isfile(output)
            if exists or figures:
                entry = {"folder": folder, "backend": engine,
                         "h5": output if exists and engine == "py4dstem" else None,
                         "output_file": output if exists else None,
                         "output_format": ".h5" if engine == "py4dstem" else ".zarr.zip", "figures": figures}
                entry["mtime"] = os.path.getmtime(output if exists else figures[0]["path"])
                found.append(entry)
    found.sort(key=lambda e: e["mtime"], reverse=True)
    return found


def is_output_figure(path):
    """Whether `path` looks like a figure azohp_to_py4d writes (the only files served by path)."""
    name = os.path.basename(path)
    return (os.path.isfile(path) and name.endswith(".png")
            and re.search(r"_(%s)\.png$" % "|".join(FIGURE_KINDS), name) is not None)


def inspect_hp(path):
    from . import azorus as conv
    st = os.stat(path)
    checks = []
    result = {
        "path": path, "name": os.path.basename(path), "size": st.st_size, "mtime": st.st_mtime,
        "checks": checks, "datasets": [], "summary": [], "metadata": {}, "geometry": None,
        "suggested": {}, "outputs": existing_outputs(path),
    }

    with h5py.File(path, "r") as f:
        def visit(name, obj):
            if isinstance(obj, h5py.Dataset):
                result["datasets"].append({
                    "path": name, "shape": list(obj.shape), "dtype": str(obj.dtype),
                    "nbytes": int(obj.size * obj.dtype.itemsize) if obj.dtype.kind != "O" else None,
                    "chunks": list(obj.chunks) if obj.chunks else None, "compression": obj.compression,
                })
        f.visititems(visit)

        missing = [name for name in REQUIRED_DATASETS if name not in f]
        if missing:
            _check(checks, "datasets", "fail", "Missing required datasets",
                   ", ".join(f"'{m}' ({REQUIRED_DATASETS[m]})" for m in missing))
        else:
            _check(checks, "datasets", "ok", "All required datasets present")

        dp_meta = general = None
        for field, label in (("diffraction/meta", "Acquisition metadata"), ("attrs", "Scan metadata")):
            if field not in f:
                continue
            try:
                decoded, flat = conv.unpack_diffraction_meta_all(f, field)
                result["metadata"][field] = _flat_items(flat)
                if field == "attrs":
                    general = decoded
                else:
                    dp_meta = decoded
            except Exception as exc:
                _check(checks, f"decode_{field}", "fail", f"{label} could not be decoded",
                       f"'{field}': {type(exc).__name__}: {exc}")

        nm_per_V = None
        grid_shape = None
        if general is not None:
            cal = _get(general, "scanCalibration")
            if isinstance(cal, (int, float)) and cal > 0:
                nm_per_V = cal / 1e-9
            else:
                _check(checks, "scan_calibration", "fail", "No scan calibration",
                       "attrs['scanCalibration'] is missing or not a positive number.")
            shape = _get(general, "meshParams", "shape")
            if isinstance(shape, (list, tuple)) and len(shape) == 2 and all(isinstance(s, int) for s in shape):
                grid_shape = [int(shape[0]), int(shape[1])]
            else:
                _check(checks, "mesh_shape", "fail", "No scan mesh shape",
                       "attrs['meshParams']['shape'] is missing, so the data cannot be reshaped to 4D.")
            if _get(general, "_overview_extent") is None or _get(general, "meshParams", "extent") is None:
                _check(checks, "extents", "warn", "Overview figure will fail",
                       "attrs['_overview_extent'] or attrs['meshParams']['extent'] is missing.")

        coords = np.asarray(f["coords"]) if "coords" in f else None
        dp = f["diffraction/micrograph"] if "diffraction/micrograph" in f else None
        overview = f["overview/micrograph"] if "overview/micrograph" in f else None
        n_expected = grid_shape[0] * grid_shape[1] if grid_shape else None

        if coords is not None:
            if coords.ndim != 2 or coords.shape[1] != 2:
                _check(checks, "coords", "fail", "Unexpected coordinate layout",
                       f"'coords' has shape {coords.shape}; expected (N, 2).")
            elif n_expected is not None and coords.shape[0] != n_expected:
                _check(checks, "coords", "fail", "Coordinate count doesn't match the mesh",
                       f"{coords.shape[0]} positions vs a {grid_shape[0]}×{grid_shape[1]} mesh ({n_expected}).")
        if dp is not None:
            if dp.ndim != 3:
                _check(checks, "frames", "fail", "Unexpected diffraction stack layout",
                       f"'diffraction/micrograph' has shape {dp.shape}; expected (N, Qy, Qx).")
            else:
                if n_expected is not None and dp.shape[0] != n_expected:
                    _check(checks, "frames", "fail", "Frame count doesn't match the mesh",
                           f"{dp.shape[0]} frames vs a {grid_shape[0]}×{grid_shape[1]} mesh ({n_expected}).")
                elif n_expected is not None:
                    _check(checks, "frames", "ok", "Frames match the scan mesh",
                           f"{dp.shape[0]:,} frames = {grid_shape[0]}×{grid_shape[1]}")
                if dp.shape[1] != dp.shape[2]:
                    _check(checks, "square", "warn", "Diffraction patterns aren't square",
                           f"{dp.shape[1]}×{dp.shape[2]}; the calibration assumes square patterns.")
                nbytes = int(np.prod(dp.shape)) * dp.dtype.itemsize
                ram = total_memory_bytes()
                if ram and nbytes > 0.25 * ram:
                    _check(checks, "memory", "warn", "Large dataset for this machine",
                           f"The converter loads the full stack ({nbytes / 2**30:.1f} GiB) into memory, and "
                           f"recentering, virtual images and parallax need working memory on top of that; "
                           f"this machine has {ram / 2**30:.0f} GiB.")

        geometry = {
            "grid_shape": grid_shape, "nm_per_V": nm_per_V,
            "n_coords": int(coords.shape[0]) if coords is not None and coords.ndim == 2 else None,
            "dp_shape": list(dp.shape) if dp is not None else None,
            "dp_dtype": str(dp.dtype) if dp is not None else None,
            "dp_nbytes": int(np.prod(dp.shape)) * dp.dtype.itemsize if dp is not None else None,
            "overview_shape": list(overview.shape) if overview is not None else None,
            "overview_extent": _get(general, "_overview_extent"),
            "mesh_extent": _get(general, "meshParams", "extent"),
            "R_pixel_size_nm": None,
        }
        # Same average step estimate azohp_to_py4d uses for the py4DSTEM real-space calibration.
        if (coords is not None and nm_per_V and grid_shape and coords.ndim == 2
                and coords.shape[0] == n_expected and min(grid_shape) > 1):
            x = (nm_per_V * coords[:, 1]).reshape(grid_shape)
            y = (-nm_per_V * coords[:, 0]).reshape(grid_shape)
            x_pix = (np.mean(x[:, -1]) - np.mean(x[:, 0])) / (grid_shape[1] - 1)
            y_pix = (np.mean(y[0, :]) - np.mean(y[-1, :])) / (grid_shape[0] - 1)
            geometry["R_pixel_size_nm"] = float((x_pix + y_pix) / 2)
            orientation = raster_check(x, y)
            if orientation:
                _check(checks, "orientation", *orientation)
        geometry = {k: _json_scalar(v) if not isinstance(v, (list, dict)) else v for k, v in geometry.items()}
        result["geometry"] = geometry

    # Acquisition summary: the same fields print_diffraction_acquisition_summary reports.
    summary = result["summary"]

    def add(group, label, value, unit="", digits=None):
        if value is None:
            return
        if digits is not None and isinstance(value, (int, float)):
            value = f"{value:.{digits}f}"
        summary.append({"group": group, "label": label, "value": str(value), "unit": unit})

    mag = _get(dp_meta, "microscope", "Mag")
    add("Microscope", "Magnification", f"{mag:,.0f}×" if isinstance(mag, (int, float)) else mag)
    add("Microscope", "C1 lens current", _get(dp_meta, "microscope", "C1", 0), "A", 5)
    add("Microscope", "C2 lens current", _get(dp_meta, "microscope", "C2", 0), "A", 5)
    add("Microscope", "Objective lens current", _get(dp_meta, "microscope", "Obj", 0), "A", 5)
    hv = _get(dp_meta, "microscope", "Gun.HighVoltage")
    add("Microscope", "Gun high voltage", hv, "kV", 1)
    add("Scan", "Mesh style", _get(general, "style", "name"))
    if grid_shape:
        add("Scan", "Mesh", f"{grid_shape[0]} × {grid_shape[1]}")
    if nm_per_V:
        add("Scan", "Scan calibration", nm_per_V, "nm/V", 4)
        ov = _get(general, "_overview_extent")
        if ov is not None and len(ov) == 4:
            add("Scan", "Scan extent", f"{nm_per_V * ov[3]:.2f} × {nm_per_V * ov[2]:.2f}", "nm")
        mesh = _get(general, "meshParams", "extent")
        if mesh is not None and len(mesh) == 4:
            add("Scan", "Mesh region", f"{nm_per_V * mesh[3]:.3f} × {nm_per_V * mesh[2]:.3f}", "nm")
    if geometry["R_pixel_size_nm"]:
        add("Scan", "Average step (R pixel size)", geometry["R_pixel_size_nm"] * 1000, "pm", 2)
    if geometry["dp_shape"]:
        add("Detector", "Diffraction pattern", f"{geometry['dp_shape'][1]} × {geometry['dp_shape'][2]} px")
        add("Detector", "Frames", f"{geometry['dp_shape'][0]:,}")
        add("Detector", "Data type", geometry["dp_dtype"])
        add("Detector", "Stack size in memory", f"{geometry['dp_nbytes'] / 2**20:,.1f}", "MiB")

    if isinstance(hv, (int, float)) and hv > 0:
        result["suggested"]["beam_kV"] = float(hv)
    return result


# --------------------------------------------------------------------------------------------
# Coordinates and images
# --------------------------------------------------------------------------------------------

def coords_bytes(path):
    """Raw scan coordinates (volts) as little-endian float32 pairs [col0 (row/y), col1 (col/x)]."""
    with h5py.File(path, "r") as f:
        coords = np.asarray(f["coords"], dtype="<f4")
    return np.ascontiguousarray(coords.reshape(-1, 2)).tobytes()


def encode_png(gray):
    """Encode a 2D uint8 array as a grayscale PNG."""
    gray = np.ascontiguousarray(gray, dtype=np.uint8)
    height, width = gray.shape
    raw = np.hstack([np.zeros((height, 1), dtype=np.uint8), gray]).tobytes()

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def _to_uint8(img, scale="linear", lo_pct=0.5, hi_pct=99.5):
    img = np.asarray(img, dtype=np.float64)
    finite = np.isfinite(img)
    if not finite.any():
        return np.zeros(img.shape, dtype=np.uint8)
    if scale == "log":
        img = np.log1p(np.clip(img - np.min(img[finite]), 0, None))
        lo, hi = np.min(img[finite]), np.percentile(img[finite], 99.95)
    else:
        lo, hi = np.percentile(img[finite], [lo_pct, hi_pct])
    if hi <= lo:
        hi = lo + 1
    out = np.clip((img - lo) / (hi - lo), 0, 1)
    out[~finite] = 0
    return (out * 255 + 0.5).astype(np.uint8)


def overview_png(path, max_side=1024):
    with h5py.File(path, "r") as f:
        ds = f["overview/micrograph"]
        step = max(1, int(np.ceil(max(ds.shape[:2]) / max_side)))
        img = np.asarray(ds[::step, ::step])
    if img.ndim == 3:
        img = img.mean(axis=-1)
    return encode_png(_to_uint8(img))


def frame_png(path, index, scale="log"):
    """PNG of one diffraction pattern (by flat scan index) plus intensity stats."""
    with h5py.File(path, "r") as f:
        ds = f["diffraction/micrograph"]
        index = int(np.clip(index, 0, ds.shape[0] - 1))
        frame = np.asarray(ds[index], dtype=np.float64)
    stats = {"index": index, "min": float(frame.min()), "max": float(frame.max()),
             "mean": float(frame.mean()), "sum": float(frame.sum())}
    return encode_png(_to_uint8(frame, scale)), stats


def mean_frame_png(path, samples=256, scale="log"):
    """PNG of the mean of up to `samples` evenly spaced diffraction patterns."""
    with h5py.File(path, "r") as f:
        ds = f["diffraction/micrograph"]
        n = ds.shape[0]
        idx = np.unique(np.linspace(0, n - 1, min(samples, n)).astype(int))
        acc = np.zeros(ds.shape[1:], dtype=np.float64)
        for i in idx:
            acc += ds[int(i)]
        acc /= len(idx)
    stats = {"frames": int(len(idx)), "min": float(acc.min()), "max": float(acc.max()),
             "mean": float(acc.mean())}
    return encode_png(_to_uint8(acc, scale)), stats

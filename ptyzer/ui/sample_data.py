"""
Writes a small synthetic Azorus-style .hp file, so the UI (and azohp_to_py4d) can be exercised
without real microscope data.

The file follows the layout that azohp_to_py4d reads:
  - 'diffraction/meta'        JSON blob (microscope lens values, with '_py_'-tagged ndarray/uuid
                              leaves like the real Azorus encoder produces)
  - 'attrs'                   JSON blob (scanCalibration, meshParams, _overview_extent, style)
  - 'coords'                  (N, 2) scan positions in volts, column 0 = row (y), column 1 = col (x)
  - 'diffraction/micrograph'  (N, Qy, Qx) diffraction stack
  - 'overview/micrograph'     2D overview image

The specimen is a rotated square lattice of atoms. Each bright-field detector pixel k sees the
specimen shifted in proportion to k (the parallax a defocused probe produces), so the Parallax
reconstruction has a genuine signal to align and fit.

Usage:
    python -m ptyzer.ui.sample_data [output.hp] [--scan 32] [--detector 128]
"""
import argparse
import base64
import json
import os
import uuid

import h5py
import numpy as np


def _ndarray_json(arr):
    arr = np.ascontiguousarray(arr)
    return {"_py_": "ndarray", "shape": list(arr.shape), "dtype": arr.dtype.str,
            "data": base64.b64encode(arr.tobytes()).decode("ascii")}


def _lattice(x_nm, y_nm, spacing_nm=0.39, angle_deg=12.0, sharpness=2):
    """Atom-like peaks (values 0..1) on a rotated square lattice, evaluated at x/y in nm."""
    a = np.deg2rad(angle_deg)
    u = np.cos(a) * x_nm + np.sin(a) * y_nm
    v = -np.sin(a) * x_nm + np.cos(a) * y_nm
    return (np.cos(np.pi * u / spacing_nm) * np.cos(np.pi * v / spacing_nm)) ** (2 * sharpness)


def write_sample_hp(path, scan=32, detector=128, seed=0):
    """
    Write a synthetic .hp file to `path` and return the path.

    scan : number of scan positions along each side of the (square) scan mesh.
    detector : diffraction pattern width in pixels (square).
    """
    rng = np.random.default_rng(seed)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    nm_per_V = 0.4                        # scan calibration
    overview_extent = [-8.0, -8.0, 16.0, 16.0]   # [top row V, left col V, row span V, col span V]
    mesh_extent = [-2.0, -2.0, 4.0, 4.0]         # [y0, x0, height, width] in volts

    # Scan coordinates (volts): C-order raster, row voltage increasing down the rows, with a
    # small jitter like Azorus' jittered mesh styles.
    y0, x0, h, w = mesh_extent
    row_V = y0 + np.arange(scan) * h / (scan - 1)
    col_V = x0 + np.arange(scan) * w / (scan - 1)
    RR, CC = np.meshgrid(row_V, col_V, indexing="ij")
    step_V = h / (scan - 1)
    jitter = rng.normal(0, 0.04 * step_V, size=(scan * scan, 2))
    coords = np.stack([RR.ravel(), CC.ravel()], axis=1) + jitter

    # Real-space positions (nm) as azohp_to_py4d will calibrate them.
    x_nm = nm_per_V * coords[:, 1]
    y_nm = -nm_per_V * coords[:, 0]

    # Diffraction stack: a bright-field disk whose pixels each see a parallax-shifted specimen.
    q = np.arange(detector) - detector / 2
    QY, QX = np.meshgrid(q, q, indexing="ij")
    radius = detector * 0.22
    in_disk = (QX ** 2 + QY ** 2) <= radius ** 2
    shift_nm_per_px = 0.12 / radius        # disk edge sees the specimen shifted by 0.12 nm

    kx = QX[in_disk]
    ky = QY[in_disk]
    bf = 1.0 - 0.35 * _lattice(x_nm[:, None] + shift_nm_per_px * kx[None, :],
                               y_nm[:, None] + shift_nm_per_px * ky[None, :])
    edge = np.clip(radius + 0.5 - np.sqrt(QX ** 2 + QY ** 2), 0, 1)
    halo = 0.02 * np.exp(-np.sqrt(QX ** 2 + QY ** 2) / (detector * 0.12))

    # Atoms also scatter to high angles, so the dark-field region carries the specimen too.
    df = 0.4 + _lattice(x_nm, y_nm)
    stack = (halo[None, :, :] * 900 * df[:, None, None]).astype(np.float32)
    stack[:, in_disk] += 900 * bf * edge[in_disk]
    stack = rng.poisson(np.clip(stack, 0, None)).astype(np.uint16)

    # Overview micrograph: HAADF-like view of the same lattice over the overview field of view.
    ov = 256
    top, left, rspan, cspan = overview_extent
    ov_row_V = top + (np.arange(ov) + 0.5) * rspan / ov
    ov_col_V = left + (np.arange(ov) + 0.5) * cspan / ov
    OR, OC = np.meshgrid(ov_row_V, ov_col_V, indexing="ij")
    overview = _lattice(nm_per_V * OC, -nm_per_V * OR)
    overview = overview * (0.8 + 0.2 * np.sin(OC / 5.0)) + rng.normal(0, 0.05, overview.shape)
    overview = (np.clip(overview, 0, None) * 20000).astype(np.uint16)

    dp_meta = {
        "microscope": {
            "Mag": 4_000_000,
            "C1": [1.84213, 0.0],
            "C2": [2.51907, 0.0],
            "Obj": [6.03381, 0.0],
            "Gun.HighVoltage": 200.0,
            "PA": _ndarray_json(np.array([0.0132, -0.0047])),
            "StageXYZ": _ndarray_json(np.array([12.5e-6, -3.1e-6, 0.4e-6])),
        },
        "datetime": [2026, 9, 16, 14, 32, 7],
        "n_frames": scan * scan,
        "uuid1": {"_py_": "uuid", "hex": uuid.UUID(int=int(rng.integers(0, 2 ** 62))).hex},
        "transforms": [
            {"_py_": "framestream.micrograph.Transform", "name": "descan", "scale": 1.0},
            {"_py_": "framestream.micrograph.Transform", "name": "rotation", "angle": 0.0},
        ],
        "synthetic": True,
    }
    attrs = {
        "scanCalibration": nm_per_V * 1e-9,
        "meshParams": {"shape": [scan, scan], "extent": mesh_extent},
        "_overview_extent": overview_extent,
        "style": {"name": "Jitter Raster (synthetic)"},
        "dwellTime": 0.002,
    }

    with h5py.File(path, "w") as f:
        f.create_dataset("diffraction/meta", data=json.dumps(dp_meta))
        f.create_dataset("attrs", data=json.dumps(attrs))
        f.create_dataset("coords", data=coords)
        f.create_dataset("diffraction/micrograph", data=stack, chunks=(1, detector, detector))
        f.create_dataset("overview/micrograph", data=overview)
    return path


def main():
    parser = argparse.ArgumentParser(description="Write a synthetic Azorus-style .hp file.")
    parser.add_argument("output", nargs="?", default="Synthetic Lattice Dataset.hp")
    parser.add_argument("--scan", type=int, default=32, help="scan positions per side")
    parser.add_argument("--detector", type=int, default=128, help="diffraction pattern width (px)")
    args = parser.parse_args()
    print(write_sample_hp(args.output, scan=args.scan, detector=args.detector))


if __name__ == "__main__":
    main()

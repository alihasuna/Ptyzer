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

The specimen is a rotated square lattice of atoms. Each bright-field detector pixel sees the
specimen shifted in proportion to its probe angle (the parallax a defocused probe produces), so
the parallax reconstructions have a genuine signal to align and fit, with a known answer.

Ground truth (stored in the file as diffraction/meta['synthetic_truth']). Both the scan and the
detector are described in the same image frame, x = column index, y = -row index (y up). A
bright-field pixel at probe angle theta = (theta_x, theta_y) sees the specimen displaced by

    shift = R(rotation_deg) @ (shift_per_angle_nm * theta)

where R rotates counter-clockwise and theta is read off the detector with its row and column axes
swapped when transpose=True (a mirrored scan/detector handedness, which py4DSTEM's
force_transpose undoes). shift_per_angle_nm is the parallax displacement per radian of probe angle
(nm/rad, i.e. the defocus magnitude in nm); both engines report it as C1/C10 with the same sign
(see tests/test_ground_truth.py). Angles are calibrated the way azohp_to_py4d calibrates them, from
beam_kV and recon_pix_size_pm.

Usage:
    python -m ptyzer.ui.sample_data [output.hp] [--scan 48] [--detector 96]
        [--shift-per-angle-nm 5] [--rotation-deg 0] [--transpose]
"""
import argparse
import base64
import json
import os
import math
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


def probe_angle_per_pixel(beam_kV, recon_pix_size_pm, detector):
    """Detector angular sampling (rad/pixel), as get_diffraction_calibration derives it."""
    wavelength_m = 4.135667662e-18 * 2.99792458e8 / math.sqrt(beam_kV * (2 * 510.9989461 + beam_kV))
    return wavelength_m / (recon_pix_size_pm * 1e-12) / detector


def write_sample_hp(path, scan=48, detector=96, seed=0, shift_per_angle_nm=5.0, rotation_deg=0.0,
                    transpose=False, beam_kV=200.0, recon_pix_size_pm=25.0):
    """
    Write a synthetic .hp file to `path` and return the path.

    scan : number of scan positions along each side of the (square) scan mesh.
    detector : diffraction pattern width in pixels (square).
    shift_per_angle_nm, rotation_deg, transpose : the parallax ground truth (see module docstring).
    beam_kV, recon_pix_size_pm : the calibration the angles are defined with; convert with the
        same values to compare fitted results against the truth.
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
    QY, QX = np.meshgrid(q, q, indexing="ij")     # QY = row index, QX = column index
    radius = detector * 0.22
    in_disk = (QX ** 2 + QY ** 2) <= radius ** 2

    dtheta = probe_angle_per_pixel(beam_kV, recon_pix_size_pm, detector)
    theta_x = QX[in_disk] * dtheta                # same image frame as the scan: x = col, y = -row
    theta_y = -QY[in_disk] * dtheta
    if transpose:                                 # swap the detector's row and column axes
        theta_x, theta_y = -theta_y, -theta_x
    c, s = math.cos(math.radians(rotation_deg)), math.sin(math.radians(rotation_deg))
    shift_x = shift_per_angle_nm * (c * theta_x - s * theta_y)
    shift_y = shift_per_angle_nm * (s * theta_x + c * theta_y)
    bf = 1.0 - 0.35 * _lattice(x_nm[:, None] + shift_x[None, :],
                               y_nm[:, None] + shift_y[None, :])
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
        "synthetic_truth": {
            "shift_per_angle_nm": float(shift_per_angle_nm), "rotation_deg": float(rotation_deg),
            "transpose": bool(transpose), "cs_mm": 0.0, "beam_kV": float(beam_kV),
            "recon_pix_size_pm": float(recon_pix_size_pm),
            "frame": "x = column index, y = -row index; shift = R(rotation) (shift_per_angle * theta), "
                     "theta read with detector rows/columns swapped if transpose",
        },
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
    parser.add_argument("--scan", type=int, default=48, help="scan positions per side")
    parser.add_argument("--detector", type=int, default=96, help="diffraction pattern width (px)")
    parser.add_argument("--shift-per-angle-nm", type=float, default=5.0,
                        help="parallax displacement per radian of probe angle (defocus magnitude, nm)")
    parser.add_argument("--rotation-deg", type=float, default=0.0, help="scan-to-detector rotation")
    parser.add_argument("--transpose", action="store_true", help="swap the detector row/column axes (mirrored handedness)")
    args = parser.parse_args()
    print(write_sample_hp(args.output, scan=args.scan, detector=args.detector,
                          shift_per_angle_nm=args.shift_per_angle_nm, rotation_deg=args.rotation_deg,
                          transpose=args.transpose))


if __name__ == "__main__":
    main()

"""
Engine-neutral run plumbing (ptyzer.ui.records): the pre-flight gate, the run record and the raw
Azorus metadata added to py4DSTEM files; plus the Quantem engine's QC figures. Engine-specific
tests skip when that engine isn't installed.
"""
import contextlib
import io
import json
import os
import shutil

import h5py
import numpy as np
import pytest

from ptyzer.ui import records


def _copy(samples, tmp_path, name="broken.hp"):
    dst = tmp_path / name
    shutil.copy(samples["base"][0], dst)
    return str(dst)


def test_preflight_passes_good_file(samples):
    checks, warnings = records.preflight(samples["base"][0], "py4dstem")
    assert any(c["id"] == "frames" and c["status"] == "ok" for c in checks)
    assert warnings == []


@pytest.mark.parametrize("backend", ["py4dstem", "quantem"])
def test_preflight_stops_on_frame_mismatch(samples, tmp_path, backend):
    path = _copy(samples, tmp_path)
    with h5py.File(path, "a") as f:
        stack = f["diffraction/micrograph"][:-1]
        del f["diffraction/micrograph"]
        f.create_dataset("diffraction/micrograph", data=stack)
    with pytest.raises(records.PreflightError, match="Frame count"):
        records.preflight(path, backend)


@pytest.mark.parametrize("backend", ["py4dstem", "quantem"])
def test_preflight_stops_on_non_finite_coordinates(samples, tmp_path, backend):
    path = _copy(samples, tmp_path)
    with h5py.File(path, "a") as f:
        coords = f["coords"][()]
        coords[3, 0] = np.nan
        f["coords"][...] = coords
    with pytest.raises(records.PreflightError, match="aren't finite"):
        records.preflight(path, backend)


def test_preflight_orientation_is_a_warning_for_py4dstem_and_a_stop_for_quantem(samples, tmp_path):
    path = _copy(samples, tmp_path)
    with h5py.File(path, "a") as f:
        coords = f["coords"][()]
        f["coords"][...] = coords[:, ::-1]          # swap row/column voltages: rows and columns look swapped
    _, warnings = records.preflight(path, "py4dstem")
    assert any(w["id"] == "orientation" for w in warnings)
    with pytest.raises(records.PreflightError):
        records.preflight(path, "quantem")


def test_environment_without_py4dstem_reports_converter_unavailable():
    # azohp_to_py4d imports without py4DSTEM (deferred imports); the UI must still report py4DSTEM missing.
    try:
        import py4DSTEM  # noqa: F401
        pytest.skip("py4DSTEM is installed here")
    except ImportError:
        pass
    from ptyzer.ui import hpfile
    env = hpfile.environment()
    assert env["converter_ok"] is False
    assert "py4DSTEM" in env["converter_error"]


def test_fingerprint_tracks_metadata_not_path(samples, tmp_path):
    original = samples["base"][0]
    copy = _copy(samples, tmp_path, "copy.hp")
    assert records.fingerprint(original) == records.fingerprint(copy)
    with h5py.File(copy, "a") as f:
        coords = f["coords"][()]
        coords[0, 0] += 1e-6
        f["coords"][...] = coords
    assert records.fingerprint(original)["sha256"] != records.fingerprint(copy)["sha256"]


def test_write_record(samples, tmp_path):
    import time
    result = {"aberrations": {"defocus_nm": 4.9, "rotation_deg": 0.1}, "output_file": None, "output_format": ".h5",
              "R_pixel_size": 0.034, "aberrations_warning": "check me"}
    path = records.write_record(str(tmp_path), "base", file=samples["base"][0], params={"backend": "py4dstem"},
                                engine={"name": "py4DSTEM", "version": "x"}, result=result, checks=[],
                                warnings=[{"title": "W", "detail": "d"}], figures=[], started=time.time())
    record = json.loads(open(path).read())
    assert record["schema"] == records.RECORD_SCHEMA
    assert record["input"]["fingerprint"]["sha256"] == records.fingerprint(samples["base"][0])["sha256"]
    assert record["aberrations"]["defocus_nm"] == 4.9
    assert set(records.CONVENTIONS) <= set(record["conventions"])
    assert record["warnings"] == ["W: d", "check me"]


def test_raw_metadata_in_py4dstem_file(samples, tmp_path):
    py4DSTEM = pytest.importorskip("py4DSTEM")
    from ptyzer.io import azohp_to_py4d as conv
    with contextlib.redirect_stdout(io.StringIO()):
        datacube = conv.azohp_to_py4d(samples["base"][0], savepathname=str(tmp_path), do_save=True)
    h5 = str(tmp_path / "base_py4.h5")
    records.append_azorus_raw(h5, samples["base"][0])
    back = py4DSTEM.read(h5)                       # still a normal py4DSTEM file
    assert np.array_equal(np.asarray(back.data), np.asarray(datacube.data))
    with h5py.File(h5, "r") as out, h5py.File(samples["base"][0], "r") as src:
        g = out[records.RAW_GROUP]
        assert g["attrs_json"][()].decode() == src["attrs"][()].decode()
        assert g["diffraction_meta_json"][()].decode() == src["diffraction/meta"][()].decode()
        assert np.array_equal(g["coords_V"][()], src["coords"][()])


def test_quantem_figures(samples, tmp_path):
    pytest.importorskip("quantem")
    from ptyzer.ui.quantem_backend import run_quantem
    params = dict(backend="quantem", do_save=False)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        run_quantem(samples["rot30"][0], str(tmp_path), params)
    for kind in ("coord_checks", "overview", "virtual_diff", "parallax_recon"):
        assert os.path.getsize(tmp_path / f"rot30_{kind}.png") > 10_000, kind


def test_quantem_rotated_shifts_are_radial(samples):
    # For pure defocus, rotating the detector positions by the fitted rotation makes every
    # measured shift point radially; this is what the summary's rotated-shifts panel relies on.
    pytest.importorskip("quantem")
    from ptyzer.ui import quantem_figures as qfig
    from ptyzer.ui.azorus import calibration, unpack_diffraction_meta_all
    from quantem.core.datastructures import Dataset4dstem
    from quantem.diffractive_imaging import DirectPtychography
    with h5py.File(samples["rot30"][0]) as f:
        attrs, _ = unpack_diffraction_meta_all(f, "attrs")
        coords, stack = f["coords"][()], f["diffraction/micrograph"][()]
    shape, _, _, rstep, qstep, fov = calibration(attrs, coords, stack.shape[-1], 200, 25)
    arr = stack.reshape(*shape, *stack.shape[-2:]).astype(np.float32)
    ds = Dataset4dstem.from_array(arr, sampling=(rstep * 10, rstep * 10, qstep, qstep), units=("A", "A", "A^-1", "A^-1"))
    mean = arr.mean((0, 1))
    semi = float(np.sqrt((mean > mean.max() * .5).sum() / np.pi)) / mean.shape[-1] * fov
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), qfig.capture_shift_fit() as calls:
        d = DirectPtychography.from_dataset4d(ds, energy=200e3, semiangle_cutoff=semi, rotation_angle=0.,
                                              force_fitted_origin=tuple(v / 2 for v in arr.shape[-2:]), device="cpu", rng=0)
        d.fit_hyperparameters_cross_correlation(bin_factors=(4, 2, 1), dft_upsample_factor=4, regularize_shifts=False,
                                                deconvolution_kernel="parallax", parallax_flip_phase=False)
    k, s, _ = qfig.shift_geometry(calls[-1])
    t = np.deg2rad(float(d.hyperparameter_state.current_rotation_angle()))
    kr = k @ np.array([[np.cos(t), -np.sin(t)], [np.sin(t), np.cos(t)]]).T
    cross = (kr[:, 0] * s[:, 1] - kr[:, 1] * s[:, 0]) / (np.hypot(*kr.T) * np.hypot(*s.T) + 1e-12)
    assert np.nanmean(np.abs(cross)) < 0.05

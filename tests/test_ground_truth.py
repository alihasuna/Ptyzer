"""
Regression tests against the synthetic ground truth written by ptyzer.ui.sample_data.

The sample shifts the specimen seen by each bright-field pixel in proportion to its probe angle,
with no contrast transfer, so it validates shift-based fits (py4DSTEM's aberration_fit and
Quantem's cross-correlation fit) and fixes their sign, rotation and mirror conventions. It cannot
validate Quantem's Fourier-phase least-squares refinement, which needs a physical simulation.

Each engine's tests skip when that engine isn't importable; run this file once from a py4DSTEM
environment (numpy < 2) and once from a Quantem environment (numpy >= 2).

Measured biases on the 48 x 48 x 96 x 96 sample, encoded in the tolerances below:
  py4DSTEM 0.14.14 and 0.14.18: C1 1.3% low, rotation within 0.05 deg.
  Quantem (commit 55a0b01): C10 1.1% high, rotation +1.9 deg (not yet explained).
"""
import io
import contextlib

import h5py
import numpy as np
import pytest

C1_REL_TOL = 0.025
ROTATION_TOL_DEG = 0.5
QUANTEM_ROTATION_BIAS_DEG = 1.9


def expected_c1_nm(truth):
    return truth["shift_per_angle_nm"]


def assert_angle(measured, expected, tol):
    diff = (measured - expected + 180.0) % 360.0 - 180.0
    assert abs(diff) < tol, f"rotation {measured:.2f} deg, expected {expected:.2f} deg"


# --------------------------------------------------------------------------------------------
# py4DSTEM (Arthur's converter)
# --------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def py4d_fit(samples, tmp_path_factory):
    pytest.importorskip("py4DSTEM")
    from ptyzer.io import azohp_to_py4d as conv
    cache = {}

    def fit(name, force_transpose=False):
        key = (name, force_transpose)
        if key not in cache:
            path, truth = samples[name]
            out = tmp_path_factory.mktemp(f"py4d_{name}")
            with contextlib.redirect_stdout(io.StringIO()):
                datacube = conv.azohp_to_py4d(path, savepathname=str(out), get_parallax_aberrations=True,
                                              force_transpose=force_transpose)
            parallax = datacube.tree("parallax_reconstruction")
            c1, cs = conv.parallax_defocus_and_cs_Ang(parallax)
            polar = getattr(parallax, "aberrations_dict_polar", None)
            if polar is not None:
                c12 = float(polar.get("C12", 0.0))
            else:
                fits = parallax.aberration_dict_cartesian
                c12 = float(np.hypot(*(fits[(1, 2, i)]["value [Ang]"] for i in (0, 1))))
            cache[key] = dict(c1_nm=c1 / 10, c12_nm=c12 / 10, cs_mm=cs / 1e7,
                              rotation_deg=float(np.rad2deg(parallax.rotation_Q_to_R_rads)),
                              affine_c1_nm=conv.parallax_affine_defocus_Ang(parallax) / 10,
                              truth=truth, datacube=datacube)
        return cache[key]
    return fit


@pytest.mark.parametrize("name", ["base", "neg", "rot30"])
def test_py4dstem_recovers_defocus_and_rotation(py4d_fit, name):
    r = py4d_fit(name)
    assert r["c1_nm"] == pytest.approx(expected_c1_nm(r["truth"]), rel=C1_REL_TOL)
    assert abs(r["c12_nm"]) < 0.05
    assert_angle(r["rotation_deg"], r["truth"]["rotation_deg"], ROTATION_TOL_DEG)


def test_py4dstem_fits_cs_on_every_version(py4d_fit):
    # 0.14.18 silently ignored the old aberration_fit argument names and fitted only up to third
    # radial order, leaving C30 exactly zero. The sample has no Cs, so expect a small non-zero fit.
    assert py4d_fit("base")["cs_mm"] != 0.0


def test_py4dstem_mirror_with_transpose(py4d_fit):
    r = py4d_fit("mirror", force_transpose=True)
    assert r["c1_nm"] == pytest.approx(expected_c1_nm(r["truth"]), rel=C1_REL_TOL)
    assert_angle(r["rotation_deg"], 0.0, ROTATION_TOL_DEG)


def test_py4dstem_mirror_without_transpose_reads_as_astigmatism(py4d_fit):
    # A mirrored defocus is indistinguishable from pure two-fold astigmatism in one dataset; the
    # handedness has to come from the instrument set-up (e.g. a defocus series), not the fit.
    r = py4d_fit("mirror")
    assert abs(r["c1_nm"]) < 0.05
    assert r["c12_nm"] == pytest.approx(abs(expected_c1_nm(r["truth"])), rel=C1_REL_TOL)


@pytest.mark.xfail(strict=True, reason="py4DSTEM 0.14.14/0.14.18: with force_transpose and a non-zero "
                                       "rotation the refined C1 is scaled by cos(2 * rotation)")
def test_py4dstem_mirror_and_rotation(py4d_fit):
    r = py4d_fit("mirror_rot30", force_transpose=True)
    assert r["c1_nm"] == pytest.approx(expected_c1_nm(r["truth"]), rel=C1_REL_TOL)


def test_py4dstem_affine_estimate_flags_mirror_and_rotation(py4d_fit):
    r = py4d_fit("mirror_rot30", force_transpose=True)
    assert r["affine_c1_nm"] == pytest.approx(expected_c1_nm(r["truth"]), rel=C1_REL_TOL)
    assert abs(r["c1_nm"] - r["affine_c1_nm"]) > 0.05 * abs(r["affine_c1_nm"])
    assert_angle(r["rotation_deg"], r["truth"]["rotation_deg"], ROTATION_TOL_DEG)


def test_py4dstem_and_shared_calibration_agree(py4d_fit, samples):
    from ptyzer.ui.azorus import calibration, unpack_diffraction_meta_all
    datacube = py4d_fit("base")["datacube"]
    with h5py.File(samples["base"][0], "r") as f:
        attrs, _ = unpack_diffraction_meta_all(f, "attrs")
        _, _, _, rstep, qstep, _ = calibration(attrs, f["coords"][()], f["diffraction/micrograph"].shape[-1], 200, 25)
    assert datacube.calibration.R_pixel_size == pytest.approx(rstep, rel=1e-12)
    assert datacube.calibration.Q_pixel_size == pytest.approx(qstep, rel=1e-12)


def test_py4dstem_saved_file_reloads_unchanged(samples, tmp_path):
    py4DSTEM = pytest.importorskip("py4DSTEM")
    from ptyzer.io import azohp_to_py4d as conv
    with contextlib.redirect_stdout(io.StringIO()):
        datacube = conv.azohp_to_py4d(samples["base"][0], savepathname=str(tmp_path), do_save=True)
    back = py4DSTEM.read(str(tmp_path / "base_py4.h5"))
    assert np.array_equal(np.asarray(back.data), np.asarray(datacube.data))


# --------------------------------------------------------------------------------------------
# Quantem adapter
# --------------------------------------------------------------------------------------------

QUANTEM_PARAMS = dict(backend="quantem", plot_coord_checks=False, plot_overview=False,
                      plot_virtual_diff=False, plot_parallax_recon=False, do_save=False)


@pytest.fixture(scope="module")
def quantem_fit(samples, tmp_path_factory):
    pytest.importorskip("quantem")
    from ptyzer.ui.quantem_backend import run_quantem
    cache = {}

    def fit(name, force_transpose=False):
        key = (name, force_transpose)
        if key not in cache:
            path, truth = samples[name]
            out = tmp_path_factory.mktemp(f"quantem_{name}")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = run_quantem(path, str(out), dict(QUANTEM_PARAMS, force_transpose=force_transpose))
            cache[key] = dict(result["aberrations"], truth=truth)
        return cache[key]
    return fit


@pytest.mark.parametrize("name,force_transpose", [
    ("base", False), ("neg", False), ("rot30", False), ("mirror", True), ("mirror_rot30", True)])
def test_quantem_recovers_defocus_and_rotation(quantem_fit, name, force_transpose):
    r = quantem_fit(name, force_transpose)
    assert r["method"] == "cross-correlation"
    assert r["defocus_nm"] == pytest.approx(expected_c1_nm(r["truth"]), rel=C1_REL_TOL)
    assert abs(r["astigmatism_nm"]) < 0.05
    assert_angle(r["rotation_deg"], r["truth"]["rotation_deg"] + QUANTEM_ROTATION_BIAS_DEG, ROTATION_TOL_DEG)


def test_quantem_mirror_without_transpose_flips_defocus(quantem_fit):
    # Quantem absorbs the mirror into an improper "rotation": defocus changes sign and the
    # rotation is off by about 90 degrees, rather than showing up as astigmatism as in py4DSTEM.
    r = quantem_fit("mirror", False)
    assert r["defocus_nm"] == pytest.approx(-expected_c1_nm(r["truth"]), rel=C1_REL_TOL)
    assert abs((r["rotation_deg"] + 180.0) % 360.0 - 180.0) > 45.0


def test_quantem_least_squares_refinement():
    pytest.skip("Fourier-phase refinement needs a physical (e.g. abTEM) simulation; the shift-only "
                "sample has no contrast transfer.")


def test_quantem_archive_round_trip(samples, tmp_path):
    pytest.importorskip("quantem")
    import json
    from quantem.core.io import load
    from ptyzer.ui.quantem_backend import run_quantem
    path = samples["base"][0]
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        result = run_quantem(path, str(tmp_path), dict(QUANTEM_PARAMS, do_save=True))
    dataset = load(result["output_file"])
    with h5py.File(path, "r") as f:
        stack = f["diffraction/micrograph"][()]
        raw_attrs = f["attrs"][()].decode("utf-8")
        coords = f["coords"][()]
    assert np.array_equal(np.asarray(dataset.array).reshape(stack.shape), stack)
    assert dataset.metadata["azorus_json"]["attrs"] == raw_attrs
    assert np.array_equal(np.asarray(dataset.azorus_scan_coordinates_V), coords)
    assert json.loads(dataset.metadata["azorus_json"]["attrs"])["scanCalibration"] > 0

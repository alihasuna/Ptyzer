"""
Subprocess entry point that runs one azohp_to_py4d conversion for the UI.

Reads a JSON job description on stdin:
    {"file": "...", "output_dir": "...", "params": {...normalized UI params...}}

and runs ptyzer.io.azohp_to_py4d.azohp_to_py4d unchanged. Progress is reported by wrapping (from
the outside) the module-level functions and Parallax methods the converter calls, each wrapper
emitting a structured event line on stdout:

    @@PTYZER@@{"event": "stage", "key": "parallax_reconstruct"}

Everything else the converter and py4DSTEM print (acquisition summary, tqdm progress bars,
aberration tables) passes through as ordinary log output.
"""
import functools
import json
import os
import sys
import time
import traceback
import warnings

EVENT_PREFIX = "@@PTYZER@@"

FIGURE_LABELS = {
    "coord_checks": "Scan-coordinate checks",
    "overview": "Overview & scan mesh",
    "virtual_diff": "Virtual images & diffraction",
    "parallax_recon": "Parallax reconstruction",
}


def build_plan(p):
    """The ordered pipeline stages azohp_to_py4d will go through for these params."""
    if p.get("backend") == "quantem":
        stages = [("preflight", "Check input"), ("read_meta", "Read file & metadata"), ("load_data", "Load & calibrate Quantem dataset")]
        for param, key, label in (
            ("plot_coord_checks", "coord_checks", "Scan-coordinate check figure"),
            ("do_recentering", "recenter", "Recenter diffraction patterns"),
            ("plot_overview", "overview_fig", "Overview figure"),
            ("plot_virtual_diff", "virtual_fig", "Virtual images & diffraction figure"),
        ):
            if p[param]: stages.append((key, label))
        if p["parallax"] or p["aberrations"]:
            stages.extend([("parallax_preprocess", "Quantem · build bright-field stack"),
                           ("parallax_reconstruct", "Quantem · fit shifts, rotation, C10/C12")])
            if p["aberrations"] and p["quantem_least_squares"]:
                stages.append(("aberration_fit", "Quantem · Fourier-phase refinement (unvalidated)"))
            stages.append(("parallax_subpixel", "Quantem · reconstruct at 4× sampling"))
            if p["plot_parallax_recon"]: stages.append(("parallax_fig", "Parallax summary figure"))
        if p["do_save"]: stages.append(("save", "Write Quantem .zarr.zip"))
        stages.append(("record", "Write run record"))
        return [{"key": key, "label": label} for key, label in stages]
    stages = [("preflight", "Check input"), ("read_meta", "Read file & metadata")]
    if p["plot_coord_checks"]:
        stages.append(("coord_checks", "Scan-coordinate check figure"))
    stages.append(("load_data", "Load & calibrate diffraction stack"))
    if p["do_recentering"]:
        stages.append(("recenter", "Recenter diffraction patterns"))
    if p["plot_overview"]:
        stages.append(("overview_fig", "Overview figure"))
    if p["plot_virtual_diff"]:
        stages.append(("virtual_fig", "Virtual images & diffraction figure"))
    if p["parallax"] or p["aberrations"]:
        stages.append(("parallax_preprocess", "Parallax · preprocess"))
        stages.append(("parallax_reconstruct", "Parallax · align bright-field images"))
        stages.append(("parallax_subpixel", "Parallax · sub-pixel alignment"))
        if p["aberrations"]:
            stages.append(("aberration_fit", "Fit aberrations"))
        if p["plot_parallax_recon"]:
            stages.append(("parallax_fig", "Parallax summary figure"))
    if p["do_save"]:
        stages.append(("save", "Write py4DSTEM .h5 (+ raw Azorus metadata)"))
    stages.append(("record", "Write run record"))
    return [{"key": key, "label": label} for key, label in stages]


def converter_kwargs(file, output_dir, p):
    """Map normalized UI params onto azohp_to_py4d's keyword arguments."""
    return dict(
        loadupname=file,
        savepathname=output_dir,
        beam_kV=p["beam_kV"],
        recon_pix_size=p["recon_pix_size_pm"] * 1e-12,
        do_recentering=p["do_recentering"],
        centre_method=p["centre_method"],
        do_save=p["do_save"],
        get_parallax_plots=p["parallax"] or p["aberrations"],
        get_parallax_aberrations=p["aberrations"],
        bf_disk_radius=p["bf_disk_radius"],
        plot_coord_checks=p["plot_coord_checks"],
        plot_overview=p["plot_overview"],
        plot_virtual_diff=p["plot_virtual_diff"],
        plot_parallax_recon=p["plot_parallax_recon"],
        force_transpose=p["force_transpose"],
    )


def _json_default(value):
    try:
        import numpy as np
        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, np.ndarray):
            return value.tolist()
    except ImportError:
        pass
    return str(value)


_figures = []  # figure events of this run, for the run record


def emit(event, **data):
    if event == "figure":
        _figures.append({"kind": data.get("kind"), "path": data.get("path")})
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    line = (EVENT_PREFIX + json.dumps({"event": event, **data}, default=_json_default) + "\n").encode("utf-8")
    while line:
        written = os.write(1, line)
        line = line[written:]


def explain(exc, text):
    """A short, actionable hint for failures we know how to recognise."""
    if "only 0-dimensional arrays can be converted" in text:
        return "py4DSTEM's Parallax code is incompatible with numpy 2. Install numpy<2."
    if type(exc).__name__ == "PreflightError":
        return "The file failed the pre-flight checks the Inspect view shows; fix the file or its metadata first."
    if isinstance(exc, MemoryError):
        return "Ran out of memory: the converter loads the full diffraction stack into RAM."
    if "cannot reshape array" in text:
        return "The diffraction stack or coordinates don't match attrs['meshParams']['shape']."
    if isinstance(exc, KeyError):
        return "A field the converter expects is missing from this .hp file. Check the Inspect view."
    return None


def install_hooks(conv, py4DSTEM, p, captured):
    import matplotlib.pyplot as plt

    def begin(key):
        emit("stage", key=key)

    def after(fn, then):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            result = fn(*args, **kwargs)
            then()
            return result
        return wrapper

    # Metadata has been read once the acquisition summary is printed; the diffraction stack is
    # loaded next (after the optional coordinate figure).
    conv.print_diffraction_acquisition_summary = after(
        conv.print_diffraction_acquisition_summary,
        lambda: None if p["plot_coord_checks"] else begin("load_data"))

    # The calibration is computed right after the stack is read; recentering follows it.
    conv.get_diffraction_calibration = after(
        conv.get_diffraction_calibration,
        lambda: begin("recenter") if p["do_recentering"] else None)

    def figure(name, key, kind, then=None):
        original = getattr(conv, name)

        @functools.wraps(original)
        def wrapper(*args, **kwargs):
            begin(key)
            if kind == "parallax_recon":
                captured["aberration_params"] = kwargs.get("aberration_params")
            try:
                result = original(*args, **kwargs)
            finally:
                plt.close("all")
            path = kwargs.get("save_path")
            if path and os.path.isfile(path):
                emit("figure", kind=kind, label=FIGURE_LABELS[kind], path=path)
            if then:
                begin(then)
            return result
        setattr(conv, name, wrapper)

    figure("plot_scan_coordinate_check_figure", "coord_checks", "coord_checks", then="load_data")
    figure("plot_overview_image", "overview_fig", "overview")
    figure("plot_diffraction_and_virtual_images", "virtual_fig", "virtual_diff")
    figure("plot_parallax_summary", "parallax_fig", "parallax_recon")

    Parallax = py4DSTEM.process.phase.Parallax
    for method, key in (("preprocess", "parallax_preprocess"), ("reconstruct", "parallax_reconstruct"),
                        ("subpixel_alignment", "parallax_subpixel"), ("aberration_fit", "aberration_fit")):
        original = getattr(Parallax, method)

        def make(original, key):
            @functools.wraps(original)
            def wrapper(self, *args, **kwargs):
                begin(key)
                return original(self, *args, **kwargs)
            return wrapper
        setattr(Parallax, method, make(original, key))

    original_save = py4DSTEM.save

    @functools.wraps(original_save)
    def save(filepath, *args, **kwargs):
        begin("save")
        result = original_save(filepath, *args, **kwargs)
        captured["output_h5"] = os.path.abspath(filepath)
        return result
    py4DSTEM.save = save


def summarize(datacube, conv, p, captured):
    import numpy as np

    result = {"backend": "py4dstem", "output_file": captured.get("output_h5"), "output_format": ".h5", "datacube_shape": list(datacube.shape), "output_h5": captured.get("output_h5")}
    cal = datacube.calibration
    for key in ("R_pixel_size", "R_pixel_units", "Q_pixel_size", "Q_pixel_units"):
        try:
            result[key] = getattr(cal, key)
        except Exception:
            result[key] = None
    try:
        _, _, angular_fov = conv.get_diffraction_calibration(
            beam_kV=p["beam_kV"], recon_pix_size=p["recon_pix_size_pm"] * 1e-12, dpsize=datacube.shape[-1])
        result["diffraction_angular_FOV_mrad"] = angular_fov * 1000
    except Exception:
        pass

    parallax = None
    for key in datacube.treekeys:
        node = datacube.tree(key)
        if type(node).__name__ == "Parallax":
            parallax = node
            break
    if parallax is not None and p["aberrations"] and hasattr(parallax, "rotation_Q_to_R_rads"):
        try:
            defocus, cs = conv.parallax_defocus_and_cs_Ang(parallax)
            aberrations = {
                "defocus_nm": defocus / 10,
                "cs_mm": cs / 1e7,
                "rotation_deg": float(np.rad2deg(parallax.rotation_Q_to_R_rads)),
                "transpose": bool(parallax.transpose),
            }
            polar = getattr(parallax, "aberrations_dict_polar", None)  # py4DSTEM >= 0.14.18
            if polar is not None and "C12" in polar:
                aberrations["astigmatism_nm"] = float(polar["C12"]) / 10
            elif hasattr(parallax, "aberration_dict_cartesian"):
                fits = parallax.aberration_dict_cartesian
                a, b = (float(fits[(1, 2, i)]["value [Ang]"]) for i in (0, 1))
                aberrations["astigmatism_nm"] = float(np.hypot(a, b)) / 10
            extra = captured.get("aberration_params") or {}
            affine = conv.parallax_affine_defocus_Ang(parallax)
            if affine is not None:
                aberrations["defocus_affine_estimate_nm"] = affine / 10
                if abs(defocus - affine) > 0.05 * max(abs(affine), 10.0):
                    result["aberrations_warning"] = (
                        f"Refined defocus ({defocus / 10:.3f} nm) disagrees with py4DSTEM's initial affine "
                        f"estimate ({affine / 10:.3f} nm). Check the mirror setting and compare with the Quantem "
                        "engine before using this value.")
            if "approximate_beam_half_angle_mrad" in extra:
                aberrations["beam_half_angle_mrad"] = float(extra["approximate_beam_half_angle_mrad"])
            result["aberrations"] = aberrations
        except Exception as exc:
            result["aberrations_error"] = f"{type(exc).__name__}: {exc}"
    return result


def main():
    job = json.load(sys.stdin)
    p = job["params"]
    started = time.time()
    emit("plan", stages=build_plan(p))
    emit("stage", key="preflight")

    os.environ["MPLBACKEND"] = "Agg"
    warnings.filterwarnings("ignore", message=".*non-interactive.*cannot be shown")
    try:
        from . import records
        file, backend = job["file"], p.get("backend") or "py4dstem"
        checks, warns = records.preflight(file, backend)
        for w in warns:
            print(f"Pre-flight warning: {w['title']}. {w.get('detail') or ''}".strip(), flush=True)
        emit("stage", key="read_meta")

        import matplotlib
        matplotlib.use("Agg")
        if backend == "quantem":
            from .quantem_backend import run_quantem
            import quantem
            engine = {"name": "Quantem", "version": quantem.__version__}
            result = run_quantem(file, job["output_dir"], p, emit_event=emit)
        else:
            import py4DSTEM
            from ptyzer.io import azohp_to_py4d as conv

            captured = {}
            install_hooks(conv, py4DSTEM, p, captured)
            engine = {"name": "py4DSTEM", "version": getattr(py4DSTEM, "__version__", None)}
            emit("info", engine="py4DSTEM", version=engine["version"], py4DSTEM=engine["version"],
                 python=sys.version.split()[0], executable=sys.executable)

            datacube = conv.azohp_to_py4d(**converter_kwargs(file, job["output_dir"], p))
            result = summarize(datacube, conv, p, captured)
            if result.get("output_file"):
                records.append_azorus_raw(result["output_file"], file)

        emit("stage", key="record")
        result["preflight_warnings"] = [w["title"] for w in warns]
        result["record_file"] = records.write_record(
            job["output_dir"], os.path.splitext(os.path.basename(file))[0], file=file, params=p, engine=engine,
            result=result, checks=checks, warnings=warns, figures=list(_figures), started=started)
        result["elapsed_s"] = time.time() - started
        emit("result", **result)
    except Exception as exc:
        text = traceback.format_exc()
        sys.stderr.write(text)
        emit("error", type=type(exc).__name__, message=str(exc), traceback=text, hint=explain(exc, text))
        sys.exit(1)


if __name__ == "__main__":
    main()

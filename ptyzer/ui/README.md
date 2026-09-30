# Ptyzer UI

A local web interface for Azorus `.hp` processing with py4DSTEM or an optional experimental Quantem backend. It lets you browse to Azorus `.hp` files, check them before converting, run conversions (singly or as a batch) with live progress, and review the QC figures and fitted aberrations afterwards.

The UI wraps the converter without changing it: every py4DSTEM run calls `ptyzer.io.azohp_to_py4d.azohp_to_py4d` with the arguments shown in the settings panel, in a separate Python process.

## Running it

From the repository root, in the Python environment you already use for py4DSTEM:

```bash
python -m ptyzer.ui
```

This opens `http://127.0.0.1:8765/` in your browser. Useful options:

| Option | Default | |
| --- | --- | --- |
| `--dir PATH` | current folder | Folder the file browser starts in |
| `--port N` | `8765` | `0` picks a free port |
| `--no-browser` | off | Don't open a browser tab |
| `--host ADDR` | `127.0.0.1` | Binding to anything else exposes your files to the network |

No packages beyond what the converter itself needs (py4DSTEM, h5py, numpy, matplotlib) are required. The server uses only the Python standard library, and the frontend is plain HTML/CSS/JS with no build step or CDN, so it also works on offline instrument PCs.

### py4DSTEM version

`azohp_to_py4d.py` was developed against **py4DSTEM 0.14.14** with **numpy < 2**. Later py4DSTEM releases (0.14.18 onwards) store the aberration fit as `aberrations_dict_cartesian` with a different layout, so `get_parallax_aberrations=True` fails with an `AttributeError`. The UI detects this at start-up and shows a warning next to the affected options. A matching environment:

```bash
pip install "py4DSTEM==0.14.14" "numpy<2"
```

## Optional Quantem engine

py4DSTEM stays the default. Quantem 0.1.9 can run in a separate Python environment:

```bash
python3.12 -m venv .venv-quantem
.venv-quantem/bin/python -m pip install "quantem @ git+https://github.com/electronmicroscopy/quantem.git@55a0b01706de9747e91b9323a283e09bd82c97ab"
# Launch using your existing py4DSTEM Python:
python -m ptyzer.ui --quantem-python "$PWD/.venv-quantem/bin/python"
```

`PTYZER_QUANTEM_PYTHON` is the equivalent environment variable. Without a configured
path, the server checks its own interpreter for Quantem. Interpreter paths are
server configuration; browsers cannot supply an executable. Quantem requires
NumPy 2, while the original converter requires NumPy <2, so separate environments
are recommended. The environment popover reports each engine's availability.

Select **Quantem (experimental)** in the Convert panel. It reads the `.hp` stack,
preserves the original metadata JSON and scan coordinates, applies the same
scan/Q calibration, and writes `*_quantem.zarr.zip` under `ReformattedForQuantem/`.
Archives also contain the reconstructed BF image, fit coefficients, fitting
parameters, engine version and calibrated coordinates. Load with:

```python
from quantem.core.io import load
from ptyzer.ui.azorus import decode
import json

dataset = load("path/to/sample_quantem.zarr.zip")
attrs = decode(json.loads(dataset.metadata["azorus_json"]["attrs"]))
image = dataset.parallax_reconstruction  # only when reconstruction was enabled
fit = dataset.metadata.get("quantem_fit")
```

The engine currently supports CPU processing, square diffraction patterns and
the raster orientation accepted by Ptyzer. Recentering uses centre-of-mass
estimates and bilinear shifts. The native DirectPtychography preprocessing shifts
the detector origin to a corner internally. A manual BF radius applies only to
virtual BF/ADF images, matching the existing converter; reconstruction aperture
is estimated from the mean intensity above half maximum. The overview QC uses the same Azorus overview and mesh extent convention
as the original converter, with axes in nm.

Quantem aligns BF shifts and rotation using cross-correlation, optionally fits a
low-order basis including C10/C30 using Fourier-phase least squares, and uses the
parallax kernel with phase flipping off and 4× reconstruction sampling. These
are different algorithms from py4DSTEM's alignment, KDE upsampling and
high-order shift fitting. Its detector rotation is displayed in Quantem's own
convention, without pretending a py4DSTEM transpose flag exists. The adapter
preserves the cross-correlation rotation through the 0.1.9 least-squares call.

A synthetic run establishes integration and archive fidelity, not scientific
accuracy. Fitted defocus and rotation differed substantially between engines on
the bundled lattice example. Compare reconstructions and coefficients on a real
reference dataset with your supervisor before making Quantem the default.

## What it does

**Files (sidebar).** Browse folders, paste a path, and tick several `.hp` files for a batch run. Files that already have a `*_py4.h5` in `ReformattedForPy4DSTEM/` are marked *converted*. **Generate sample dataset** writes a small synthetic file (a 32 × 32 scan of a rotated lattice, with real parallax shifts) to the temp folder, so you can try everything without microscope data.

**Inspect.** For the selected file:
- Pre-flight checks that mirror what the converter needs: required datasets, decodable metadata, scan calibration, mesh shape vs. coordinate and frame counts, square patterns, memory (the converter loads the whole stack into RAM), and whether the coordinates form the raster the reshape assumes (flags swapped, mirrored, rotated, or saw-tooth scan orders, the cases the converter's coordinate check figure describes).
- Scan geometry: the overview micrograph with the mesh region, every scan position and the first ten (scan start), with axes in nm. Click a position to see its diffraction pattern.
- Diffraction pattern at that position, or a sampled mean, with log/linear scaling and colormaps. Arrow keys step through the scan. A manually set bright-field disk radius is drawn on it.
- Acquisition summary (the same fields `print_diffraction_acquisition_summary` prints), earlier conversions with their figures, every decoded metadata field (searchable), and the HDF5 layout.

Only single frames or small samples are read, never the whole stack.

**Convert (right panel).** Each control maps onto one `azohp_to_py4d` argument. *Quick look* produces QC figures only; *Full pipeline* matches the example in `azohp_to_py4d.py`'s `__main__` block. **New subfolder for each run** (on by default) writes each run to `ReformattedForPy4DSTEM/<name>_<date-time>/`, so re-running with different settings doesn't overwrite earlier results. The `</>` button copies the equivalent Python call.

**Runs.** Conversions run one at a time. Each shows its pipeline stages with timings, the QC figures as they're written, the fitted defocus / Cs / rotation and data-cube calibration, the native dataset path, and the full log, including py4DSTEM's progress bars. Runs can be cancelled, re-run, or opened in Finder/Explorer. Failures show the exception, a hint for known problems, and the traceback.

## How it works

```
browser ──HTTP/SSE──▶ server.py ──▶ hpfile.py        (read-only inspection, uses backend-independent Azorus metadata decoding)
                          │
                          └──────▶ jobs.py ──subprocess──▶ worker.py ──▶ azohp_to_py4d(...)
```

- `worker.py` runs one conversion. It reports progress by wrapping, from the outside, the plot functions and `Parallax` methods the converter calls, emitting `@@PTYZER@@{json}` event lines. Everything else the converter prints passes through as log output.
- `jobs.py` queues jobs, streams worker output (splitting tqdm's carriage-return updates from log lines) and handles cancellation.
- `server.py` serves the app and a small JSON API (documented at the top of the file). It accepts only loopback `Host` headers and same-origin JSON POSTs.
- `sample_data.py` writes the synthetic dataset (`python -m ptyzer.ui.sample_data out.hp --scan 32 --detector 128`).

Job history is kept in memory, so it clears when the server restarts. Output files stay on disk.

# Quantem integration check — 2026-09-30

Reference source: Quantem 0.1.9, commit
`55a0b01706de9747e91b9323a283e09bd82c97ab`. py4DSTEM 0.14.14 ran in a
separate environment with NumPy 1.26.4; Quantem used NumPy 2.5.3.

Both engines completed the full pipeline on the bundled Synthetic Lattice
Dataset from `sample_data.py`, using 200 kV, 25 pm reciprocal-calibration
input, estimated BF radius, no recentering, all figures, reconstruction,
aberration fitting and native export.

| Result | py4DSTEM | Quantem |
| --- | --- | --- |
| Data shape | 32 × 32 × 128 × 128 | 32 × 32 × 128 × 128 |
| Scan sampling | 51.6204486021 pm | 51.6204486021 pm |
| Reciprocal sampling | 0.03125 Å⁻¹ | 0.03125 Å⁻¹ |
| Angular field of view | 100.317361275 mrad | 100.317361275 mrad |
| QC figures | 4 PNGs | 4 PNGs |
| Native export | EMD `.h5` | Zarr `.zarr.zip` |
| Displayed fitted defocus | 0.0081478 nm | −8.0403557 nm |
| Displayed fitted Cs | −2.666e−10 mm | −6.0659082e−11 mm |
| Displayed fitted rotation | −0.07° Q → R | −47.04745° detector rotation |

The fitting methods, rotation conventions and upsampling algorithms differ.
These fitted values are not established as equivalent or accurate. The lattice
sample is a UI fixture, not a physical reference simulation. Keep py4DSTEM as
the default until real microscope data and a reference result are reviewed.

Manual checks completed:

- Conversion, progress, results and all four figures from both engines through
  the browser, with no browser error/warning logs during the checked runs.
- Reloaded the py4DSTEM export; every saved intensity equalled its input.
- Reloaded the Quantem archive with `quantem.core.io.load`; every saved raw
  intensity, voltage-coordinate pair and original JSON metadata blob equalled
  the input. Scan sampling converted from nm to Å correctly. The archive
  included a finite 128 × 128 reconstructed image and fitted parameters.
- Both global and plane-fit Quantem recentering paths completed QC/export and
  reloaded as finite float32 intensity arrays. Reconstruction also completed
  with optional aberration fitting and file saving disabled.
- Python and changed JavaScript files passed syntax checks. Invalid engine
  names and non-finite/non-positive calibration inputs were rejected.
- Cancelling a job between dequeue and process creation prevented worker
  creation in a controlled check. The process-start lock also covers publishing
  the process and its input, allowing cancellation to find a starting worker.

Setup, native output loading and method details are in [README.md](README.md).

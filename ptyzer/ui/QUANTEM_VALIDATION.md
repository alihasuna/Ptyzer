# Engine validation against synthetic ground truth — 2026-10-01

Supersedes the 2026-09-30 check, whose sample had an unintended scan/detector mirror: its
pure defocus read as astigmatism in py4DSTEM (C1 ≈ 0, C12 ≈ 5.4 nm) and was not comparable
between engines.

Environments: py4DSTEM 0.14.14 and 0.14.18 (each with NumPy 1.26.4); Quantem at commit
`55a0b01706de9747e91b9323a283e09bd82c97ab` (reports 0.1.9, but is 376 commits after the v0.1.9
tag) with NumPy 2.5.3 and PyTorch 2.14.1. Reproduce with `pytest tests` in each environment.

## Sample

`sample_data.py`, 48 × 48 scan, 96 × 96 detector, 200 kV and 25 pm calibration. Each
bright-field pixel sees the specimen displaced by 5 nm per radian of probe angle (a 5 nm
defocus), optionally rotated and/or with the detector axes swapped (mirror). The truth is stored
in the file as `diffraction/meta['synthetic_truth']`. The sample has no contrast transfer, so it
tests shift-based fits only.

## Results

Defocus in nm (truth +5.0 unless noted), rotation in degrees.

| Case | py4DSTEM C1 | py4DSTEM rotation | Quantem C10 | Quantem rotation |
| --- | --- | --- | --- | --- |
| No mirror, 0° | +4.936 | +0.02 | +5.039 | +2.08 |
| Defocus −5 nm | −4.936 | +0.03 | −5.057 | +1.91 |
| No mirror, 30° | +4.934 | +30.03 | +5.020 | +32.27 |
| Mirror, transpose on, 0° | +4.934 | +0.02 | +5.055 | +1.89 |
| Mirror, transpose on, 30° | +4.937 (was +2.467 before the workaround) | +30.01 | +5.021 | +32.26 |
| Mirror, transpose off, 0° | C1 ≈ 0, C12 = 4.934 | +89.98 | **−5.055** | +88.11 |

- Both engines report C1/C10 with the same sign as the input and the same rotation sense.
- py4DSTEM's C1 is 1.3 % low; Quantem's C10 is within 1.1 %. Quantem's rotation carries an
  unexplained +1.9° to +2.3° offset and up to 0.05 nm of spurious |C12|.
- Quantem's cross-correlation runs with `regularize_shifts=False`, like the converter's py4DSTEM
  call, so the parallax summary shows the measured shifts (with regularization the shifts equal the
  fitted model and the residual panel is empty). Regularized, C10 was 1.1 % high and the rotation
  offset +1.9°.
- **py4DSTEM, mirror and rotation (fixed in Ptyzer):** in both 0.14.14 and 0.14.18 the refined
  fit builds its aberration basis with the reported rotation, but under `force_transpose` the
  affine stage that reports it transposes the rotation matrix, so the basis needs the opposite
  angle. The refined C1 came out scaled by cos(2 × rotation) while the affine estimate
  (`aberrations_C1`) was right. `fit_parallax_aberrations` now refits with the rotation forced to
  its negative and restores the reported rotation and affine estimates; the converter and UI still
  warn if the refined and affine defocus disagree by more than 5 %. The one-line upstream fix is
  to negate the basis rotation when transposed; not yet reported to py4DSTEM.
- **Mirror detection:** neither engine can tell from one dataset. With the wrong setting,
  py4DSTEM reads the defocus as astigmatism, Quantem as a sign-flipped defocus about 90° out in
  rotation.
- **Quantem least-squares refinement** (now opt-in): drives C10 to about −0.2 nm on this sample.
  Untested until validated on a physical simulation.
- **Scan size:** on the previous 32 × 32 sample (1.6 nm field of view) Quantem's
  reference alignment was unstable (C10 −1.9 nm at −69°); hence the larger default sample.

## py4DSTEM 0.14.18 compatibility

0.14.18 renamed `Parallax.aberration_fit`'s arguments and silently ignored the old names, so the
converter's fit ran to third order only (C30 exactly 0), then crashed reading the renamed results.
`fit_parallax_aberrations` and `parallax_defocus_and_cs_Ang` handle both versions; the two
versions now agree to 0.001 nm on every case above.

## Data fidelity

`tests/test_ground_truth.py` also checks that the py4DSTEM `.h5` reloads with identical
intensities, that the Quantem `.zarr.zip` reloads with identical intensities, the raw `attrs`
JSON and the raw voltage coordinates, and that both engines use identical real- and
reciprocal-space calibration.

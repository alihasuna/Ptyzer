"""
QC figures for the Quantem engine in the layout of azohp_to_py4d's py4DSTEM figures, so runs from
either engine can be checked the same way. The coordinate check and overview figures are
azohp_to_py4d's own functions (they need no py4DSTEM); the two below need Quantem's data:

  plot_virtual_images      azohp_to_py4d.plot_diffraction_and_virtual_images, from a plain array
  plot_parallax_summary    azohp_to_py4d.plot_parallax_summary, from the shifts Quantem measured

Quantem does not keep the per-pixel bright-field shifts it fits, so capture_shift_fit() wraps
quantem's fit_aberrations_from_shifts from the outside (as the worker wraps py4DSTEM) and records
its inputs.
"""
import contextlib
import os

import numpy as np


@contextlib.contextmanager
def capture_shift_fit():
    """Record the inputs of every fit_aberrations_from_shifts call made by DirectPtychography."""
    import quantem.diffractive_imaging.direct_ptychography as dp
    original = dp.fit_aberrations_from_shifts
    calls = []

    def wrapper(shifts_ang, bf_mask, wavelength, gpts, sampling, *args, **kwargs):
        calls.append({
            "shifts_A": shifts_ang.detach().cpu().numpy().astype(float),
            "bf_mask": bf_mask.detach().cpu().numpy().astype(bool),
            "wavelength_A": float(wavelength), "gpts": tuple(int(g) for g in gpts),
            "sampling_A": tuple(float(s) for s in sampling),
        })
        return original(shifts_ang, bf_mask, wavelength, gpts, sampling, *args, **kwargs)

    dp.fit_aberrations_from_shifts = wrapper
    try:
        yield calls
    finally:
        dp.fit_aberrations_from_shifts = original


def shift_geometry(call):
    """BF-pixel spatial frequencies (Å⁻¹), measured shifts (Å) and the affine model fit to them."""
    kx = np.fft.fftfreq(call["gpts"][0], call["sampling_A"][0])[:, None] * np.ones(call["gpts"][1])
    ky = np.fft.fftfreq(call["gpts"][1], call["sampling_A"][1])[None, :] * np.ones((call["gpts"][0], 1))
    mask = call["bf_mask"]
    kvec = np.stack((kx[mask], ky[mask]), axis=1)
    shifts = call["shifts_A"]
    basis = kvec * call["wavelength_A"]
    M = np.linalg.lstsq(basis, shifts, rcond=None)[0]   # same affine fit Quantem decomposes
    return kvec, shifts, basis @ M


def _maps(call, values):
    """Scatter per-BF-pixel values onto the (centred) detector grid, NaN outside the BF disk."""
    grid = np.full(call["gpts"], np.nan)
    grid[call["bf_mask"]] = values
    return np.fft.fftshift(grid)


def _save(fig, save_path):
    fig.savefig(save_path)
    import matplotlib.pyplot as plt
    plt.close(fig)


def plot_virtual_images(array, loadupname, bf_disk_radius, save_path):
    """Dark field, bright field, mean and max patterns with the BF detector drawn, as in the converter."""
    import matplotlib.pyplot as plt
    fig, ((ax_df, ax_bf), (ax_mean, ax_max)) = plt.subplots(2, 2, figsize=(10, 10))
    qy, qx = np.indices(array.shape[-2:])
    cy, cx = array.shape[-2] / 2, array.shape[-1] / 2
    r = np.hypot(qy - cy, qx - cx)
    bf = array[..., r <= bf_disk_radius].sum(axis=-1)
    df = array[..., r > bf_disk_radius].sum(axis=-1)
    mean = array.mean(axis=(0, 1))
    peak = array.max(axis=(0, 1))
    for ax, img, title in ((ax_df, df, "Dark Field"), (ax_bf, bf, "Bright Field")):
        ax.imshow(img, cmap="gray")
        ax.set_title(title)
    ax_mean.imshow(np.log1p(mean), cmap="gray")
    ax_mean.set_title("Mean Diffraction Pattern")
    ax_max.imshow(np.log1p(peak), cmap="gray")
    ax_max.add_patch(plt.Circle((cx, cy), bf_disk_radius, fill=False, color="#ef5b3b", lw=1.5))
    ax_max.set_title("Max Diffraction Pattern")
    fig.suptitle(os.path.basename(loadupname))
    _save(fig, save_path)


def plot_parallax_summary(call, loadupname, aligned_bf, upsampled_bf, fit_params, save_path, rotation_deg=0.0):
    """
    The converter's eight-panel parallax summary, from Quantem's measured shifts: aligned and
    upsampled bright field, measured and rotation-corrected shift vectors, measured vertical and
    horizontal shift maps, the residual of the affine fit, and the fitted values.
    """
    import matplotlib.pyplot as plt
    fig, axs = plt.subplots(2, 4, figsize=(19, 9))
    kvec, shifts, model = shift_geometry(call)

    # col 0, row 0: Quantem keeps no alignment-error history; show how well the affine model fits.
    # Colour scales clip at the 99th percentile so one badly aligned pixel doesn't flatten the rest;
    # it still shows, saturated.
    resid = np.hypot(*(shifts - model).T)
    im = axs[0, 0].imshow(_maps(call, resid), cmap="magma", vmin=0, vmax=max(np.percentile(resid, 99), 1e-6))
    fig.colorbar(im, ax=axs[0, 0], fraction=0.046, pad=0.04, label="Å (clipped at 99th percentile)")
    axs[0, 0].set_title(f"Affine-fit residual (max {resid.max():.2g} Å)")
    axs[0, 0].set_xticks([]); axs[0, 0].set_yticks([])

    for ax, img, title in ((axs[0, 1], aligned_bf, "Aligned Bright Field"), (axs[0, 2], upsampled_bf, "Upsampled Bright Field (4×)")):
        ax.imshow(img, cmap="gray")
        ax.set_title(title)
        ax.set_xticks([]); ax.set_yticks([])

    step = max(1, len(kvec) // 400)  # keep the quiver legible
    sel = slice(None, None, step)
    axs[1, 0].quiver(kvec[sel, 1], kvec[sel, 0], shifts[sel, 1], shifts[sel, 0], color=(1, 0, 0, 1),
                     angles="xy", scale_units="xy", scale=None)
    axs[1, 0].set_title("Measured Bright Field Shifts")
    # Rotate the detector positions (not the shifts) by the fitted rotation: for pure defocus the
    # shifts then point radially, as in the converter's rotated-shifts panel.
    t = np.deg2rad(rotation_deg)
    rot = np.array([[np.cos(t), -np.sin(t)], [np.sin(t), np.cos(t)]])
    kr = kvec @ rot.T
    axs[1, 1].quiver(kr[sel, 1], kr[sel, 0], shifts[sel, 1], shifts[sel, 0], angles="xy", scale_units="xy", scale=None)
    axs[1, 1].set_title("Rotated Bright Field Shifts")
    kmax = 1.2 * np.abs(kvec).max()
    for ax in (axs[1, 0], axs[1, 1]):
        ax.set_xlim(-kmax, kmax); ax.set_ylim(-kmax, kmax)
        ax.set_xlabel(r"$k_y$ [$A^{-1}$]"); ax.set_ylabel(r"$k_x$ [$A^{-1}$]")
        ax.set_aspect("equal")

    smax = max(np.percentile(np.abs(shifts), 99), 1e-6)
    for ax, comp, title in ((axs[0, 3], 0, "Measured Vertical Shifts"), (axs[1, 3], 1, "Measured Horizontal Shifts")):
        ax.imshow(_maps(call, shifts[:, comp]), cmap="PiYG", vmin=-smax, vmax=smax)
        ax.set_title(title)
        ax.set_xticks([]); ax.set_yticks([])

    axs[1, 2].axis("off")
    if fit_params:
        axs[1, 2].text(0.5, 0.5, "\n".join(f"{k}: {v:.5g}" if isinstance(v, float) else f"{k}: {v}" for k, v in fit_params.items()),
                       ha="center", va="center", fontsize=12, transform=axs[1, 2].transAxes)
    fig.suptitle(os.path.basename(loadupname))
    fig.tight_layout()
    _save(fig, save_path)

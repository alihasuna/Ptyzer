"""Experimental Azorus adapter for Quantem 0.1.9 DirectPtychography on CPU.

Native output is a Quantem Dataset4dstem Zarr archive. Fitting and interpolation
are different from py4DSTEM; validate scientific results on experimental data.
"""
import sys
import time
from pathlib import Path

from .azorus import calibration, unpack_diffraction_meta_all


def run_quantem(file, output_dir=None, params=None, emit_event=None):
    from .jobs import normalize_params
    from .worker import FIGURE_LABELS
    import h5py
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import quantem
    from quantem.core.datastructures import Dataset4dstem
    from quantem.diffractive_imaging import DirectPtychography
    from quantem.diffractive_imaging.origin_models import CenterOfMassOriginModel

    p = normalize_params(params)
    emit = emit_event or (lambda *args, **kwargs: None)
    started = time.time()
    folder = Path(output_dir or p['output_dir'] or Path(file).parent / 'ReformattedForQuantem')
    folder.mkdir(parents=True, exist_ok=True)
    stem = Path(file).stem
    emit('info', engine='Quantem', version=quantem.__version__, python=sys.version.split()[0], executable=sys.executable)
    emit('stage', key='read_meta')
    with h5py.File(file, 'r') as source:
        attrs, _ = unpack_diffraction_meta_all(source, 'attrs')
        unpack_diffraction_meta_all(source)  # Validate acquisition metadata before loading the stack.
        # Keep the raw blobs too: unknown typed fields can be recovered without loss.
        raw_meta = {key: source[key][()].decode('utf-8') for key in ('attrs', 'diffraction/meta')}
        coords = source['coords'][()]
        stack = source['diffraction/micrograph']
        if stack.ndim != 3 or stack.shape[-2] != stack.shape[-1]:
            raise ValueError('This adapter currently supports square diffraction patterns')
        shape, x, y, rstep, qstep, fov = calibration(attrs, coords, stack.shape[-1], p['beam_kV'], p['recon_pix_size_pm'])
        if stack.shape[0] != np.prod(shape):
            raise ValueError('Diffraction frame count does not match the scan grid')
        emit('stage', key='load_data')
        array = stack[()].reshape(*shape, *stack.shape[-2:])
        overview = source['overview/micrograph'][()] if p['plot_overview'] else None

    dataset = Dataset4dstem.from_array(array, name=stem, sampling=(rstep*10, rstep*10, qstep, qstep), units=('A', 'A', 'A^-1', 'A^-1'))
    dataset.metadata.update(azorus_json=raw_meta, ptyzer_parameters=dict(p), beam_energy_eV=p['beam_kV']*1000,
                            ptyzer_backend='quantem', quantem_version=quantem.__version__)
    dataset.azorus_scan_coordinates_V = coords
    dataset.scan_coordinates_nm = np.stack((y, x), axis=-1)
    result = dict(backend='quantem', datacube_shape=list(array.shape), R_pixel_size=rstep, R_pixel_units='nm',
                  Q_pixel_size=qstep, Q_pixel_units='A^-1', diffraction_angular_FOV_mrad=fov,
                  output_file=None, output_format='.zarr.zip',
                  method_note='Experimental Quantem backend. Cross-correlation fit validated on the synthetic ground truth only; validate against experimental reference data.')

    def save_figure(kind, fig):
        fig.suptitle(f'{stem} · Quantem {quantem.__version__}')
        fig.tight_layout()
        path = str((folder / f'{stem}_{kind}.png').absolute())
        fig.savefig(path, dpi=120)
        plt.close(fig)
        emit('figure', kind=kind, label=FIGURE_LABELS[kind], path=path)

    def figure(kind, images, titles, cmap='gray'):
        fig, axes = plt.subplots(1, len(images), figsize=(5*len(images), 4), squeeze=False)
        for ax, img, title in zip(axes[0], images, titles):
            im = ax.imshow(img, cmap=cmap)
            ax.set_title(title)
            fig.colorbar(im, ax=ax, shrink=.7)
        save_figure(kind, fig)

    if p['plot_coord_checks']:
        emit('stage', key='coord_checks')
        figure('coord_checks', [coords.reshape(*shape, 2)[..., 0], coords.reshape(*shape, 2)[..., 1], x, y],
               ['Raw row voltage (V)', 'Raw column voltage (V)', 'Scan x (nm)', 'Scan y (nm)'], 'coolwarm')
    if p['do_recentering']:
        emit('stage', key='recenter')
        origin = CenterOfMassOriginModel.from_dataset(dataset, device='cpu')
        origin.calculate_origin(max_batch_size=64).fit_origin_background(fit_method='plane' if p['centre_method']=='fit' else 'constant')
        origin.shift_origin_to(tuple(v/2 for v in array.shape[-2:]), max_batch_size=64, mode='bilinear')
        dataset.array = origin.shifted_tensor.cpu().numpy()
        if not np.isfinite(dataset.array).all():
            raise ValueError('Recentering returned non-finite intensities; check for empty diffraction patterns')
        del origin
    if p['plot_overview']:
        emit('stage', key='overview_fig')
        nm_per_v = float(attrs['scanCalibration']) / 1e-9
        top, left, height, width = attrs['_overview_extent']
        fig, ax = plt.subplots(figsize=(6,5))
        ax.imshow(overview, cmap='gray', extent=(left*nm_per_v, (left+width)*nm_per_v,
                   (top+height)*nm_per_v, top*nm_per_v))
        mesh_y, mesh_x, mesh_h, mesh_w = attrs['meshParams']['extent']
        ax.add_patch(plt.Rectangle((mesh_x*nm_per_v, mesh_y*nm_per_v), mesh_w*nm_per_v,
                     mesh_h*nm_per_v, edgecolor='red', facecolor='none', label='Scan mesh'))
        ax.set(xlabel='x (nm)', ylabel='y (nm)', title='Overview & scan mesh')
        ax.legend()
        save_figure('overview', fig)
    if p['plot_virtual_diff']:
        emit('stage', key='virtual_fig')
        mean = dataset.array.mean(axis=(0,1))
        mask = mean > mean.max()*.5
        radius = p['bf_disk_radius'] or float(np.sqrt(mask.sum()/np.pi)*1.2)
        centre = tuple(v/2 for v in mean.shape)
        bf = dataset.get_virtual_image(mode='circle', geometry=(centre, radius), name='BF', show=False).array
        adf = dataset.get_virtual_image(mode='annular', geometry=(centre, (radius, 2000)), name='ADF', show=False).array
        figure('virtual_diff', [bf, adf, np.log1p(mean), np.log1p(dataset.array.max(axis=(0,1)))],
               ['Virtual BF', 'Virtual ADF', 'Log mean diffraction', 'Log max diffraction'])
    if p['parallax'] or p['aberrations']:
        emit('stage', key='parallax_preprocess')
        # Corner shifting is an internal Quantem representation change. Optional persisted
        # recentering above is separate; never silently fit a scan-dependent origin here.
        mean = dataset.array.mean(axis=(0,1))
        aperture_radius = float(np.sqrt((mean > mean.max()*.5).sum()/np.pi))
        semiangle = aperture_radius / mean.shape[-1] * fov
        result['aperture_estimate_mrad'] = semiangle
        dataset.metadata['semiangle_cutoff_mrad'] = semiangle
        source = dataset
        if p['force_transpose']:
            # Same correction as py4DSTEM's force_transpose: swap the detector's row/column axes.
            source = Dataset4dstem.from_array(dataset.array.swapaxes(-1, -2), name=stem,
                                              sampling=dataset.sampling, units=dataset.units)
        direct = DirectPtychography.from_dataset4d(source, energy=p['beam_kV']*1000, semiangle_cutoff=semiangle, rotation_angle=0.,
                 force_fitted_origin=tuple(v/2 for v in array.shape[-2:]), max_batch_size=64, device='cpu', rng=0)
        emit('stage', key='parallax_reconstruct')
        # Cross-correlation shift fit (rotation, C10, C12). This is the step validated against the
        # ground-truth sample (tests/test_ground_truth.py), and the one comparable to py4DSTEM's
        # shift-based aberration_fit.
        direct.fit_hyperparameters_cross_correlation(bin_factors=(4,2,1), dft_upsample_factor=4,
                 deconvolution_kernel='parallax', parallax_flip_phase=False, max_batch_size=64)
        rotation = float(direct.hyperparameter_state.current_rotation_angle())
        if p['aberrations'] and p['quantem_least_squares']:
            # Fourier-phase least-squares refinement. Not validated: the synthetic sample has no
            # contrast transfer, so it cannot test this step (it drives C10 to ~0 there).
            emit('stage', key='aberration_fit')
            direct.fit_hyperparameters_least_squares(rotation_angle=rotation, cartesian_basis='low_order',
                 deconvolution_kernel='parallax', parallax_flip_phase=False, max_batch_size=64)
            # Quantem 0.1.9 LS clears the optimized rotation; restore the measured value.
            direct.hyperparameter_state.optimized_rotation_angle = rotation
        emit('stage', key='parallax_subpixel')
        direct.reconstruct(upsampling_factor=4, override_rotation_angle=rotation,
                 deconvolution_kernel='parallax', parallax_flip_phase=False, max_batch_size=64)
        coefs = {k: float(v) for k,v in direct.hyperparameter_state.current_aberrations().items()}
        recon = direct.corrected_bf.detach().cpu().numpy()
        if not np.isfinite(recon).all() or not np.isfinite(rotation) or not all(np.isfinite(v) for v in coefs.values()):
            raise ValueError('Quantem returned non-finite reconstruction or fitted coefficients')
        dataset.parallax_reconstruction = recon
        dataset.metadata['quantem_fit'] = dict(aberration_coefficients=coefs,
               coefficient_units={k: 'rad' if k.startswith('phi') else 'A' for k in coefs}, rotation_deg=rotation,
               reconstruction_sampling_A=rstep*10/4, upsampling_factor=4, phase_flip=False,
               transpose=p['force_transpose'], least_squares_refinement=p['quantem_least_squares'])
        if p['aberrations']:
            result['aberrations'] = dict(defocus_nm=coefs.get('C10', 0)/10, astigmatism_nm=coefs.get('C12', 0)/10,
                 cs_mm=coefs['C30']/1e7 if 'C30' in coefs else None, rotation_deg=rotation, transpose=p['force_transpose'],
                 method='least squares (unvalidated)' if p['quantem_least_squares'] else 'cross-correlation')
        if p['plot_parallax_recon']:
            emit('stage', key='parallax_fig')
            figure('parallax_recon', [recon], ['Aligned BF (4× sampling; phase flip off)'])
    if p['do_save']:
        emit('stage', key='save')
        path = str((folder / f'{stem}_quantem.zarr.zip').absolute())
        result['output_file'] = path
        dataset.metadata['ptyzer_results'] = dict(result)
        dataset.save(path, mode='w')
    result['elapsed_s'] = time.time()-started
    return result

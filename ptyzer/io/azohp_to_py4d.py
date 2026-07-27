"""
Converts an Azorus-generated .hp file into a py4DSTEM/EMD DataCube, with optional diffraction
recentering, data quality check plots, parallax reconstruction, and associated probe aberration 
(defocus/spherical aberration) estimation.

Layout of this module:
  - Helper functions (roughly top to bottom):
      * electron_wavelength / get_diffraction_calibration - beam-energy-dependent reciprocal-space
        calibration.
      * unpack_diffraction_meta_all / _decode_meta_value /
        flatten_meta_dict / _is_leaf - parse and decode the JSON + base64-encoded metadata blobs
        stored in the .hp file's 'diffraction/meta' and 'attrs' fields.
      * _sanitize_for_metadata - rework decoded meta trees (which may contain uuid.UUID values or
        lists of dicts) into types emdfile.Metadata.to_h5 can serialize.
      * plot_scan_coords / plot_scan_coordinate_check_figure / plot_scan_coords_grid /
        plot_scan_coords_grid_3d / plot_overview_image / plot_diffraction_and_virtual_images /
        plot_parallax_summary (plus its _plot_bf_shifts_on_axis / _measured_shift_maps helpers) -
        QC/diagnostic figures for scan-coordinate calibration, the overview image, virtual
        images/diffraction patterns, and the parallax reconstruction.
  - azohp_to_py4d(loadupname, ...) - the main entry point: loads the .hp file, calibrates and
    reshapes the scan coordinates and diffraction stack into a py4DSTEM DataCube, optionally
    recenters the diffraction patterns, optionally runs a Parallax reconstruction/aberration fit,
    attaches the Azorus metadata/scan coordinates/Parallax result to the DataCube, and optionally
    saves everything to a py4DSTEM/EMD '<name>_py4.h5' file.
  - __main__ block - example batch usage, converting a hardcoded list of .hp files.

@author: Arthur Blackburn
"""
import py4DSTEM
from py4DSTEM import show
from py4DSTEM.process.phase.utils import AffineTransform
import h5py
import numpy as np
import json
import matplotlib.pyplot as plt
from matplotlib import cm
import base64
import os
import uuid
import textwrap
# %%
# helper functions:
def electron_wavelength(kV):
    """
    Compute the relativistic electron wavelength for a given beam energy.

    kV : beam energy (keV).

    Returns the electron wavelength (metres).
    """
    # Gives electron wavelength in metres
    m0=0.5109989461*10**3 # keV / c**2
    h=4.135667662*10**(-15)*10**(-3) # eV * s
    c=2.99792458*10**8 # m / s
    return h*c/np.sqrt(kV*(2*m0+kV))

def _decode_meta_value(value):
    """
    Recursively decode a value from the parsed diffraction/meta JSON tree.

    value : a value taken from the JSON tree produced by json.loads() on an
        Azorus meta/attrs blob - a dict, list, or plain scalar (str/int/float/
        bool/None). Since this function recurses into dicts/lists, it is also
        called (by itself) on every nested sub-value of the original tree.

    The Azorus JSON encodes non-JSON-native Python objects as dicts tagged
    with a '_py_' key naming the original type:
      - '_py_': 'ndarray' -> {'shape', 'dtype', 'data' (base64)} decoded into
        a numpy array via np.frombuffer + reshape.
      - '_py_': 'uuid'    -> {'hex'} decoded into a uuid.UUID.
      - any other '_py_' tag (e.g. 'framestream.micrograph.Transform') is not
        a value we know how to reconstruct, so it's left as a plain dict
        (with '_py_' intact) rather than guessed at.
    Plain dicts/lists are walked recursively; scalars are returned as-is.

    Returns the decoded value, in the same dict/list shape as the input, with
    any '_py_'-tagged ndarray/uuid leaves replaced by their decoded form.
    """
    if isinstance(value, dict):
        tag = value.get('_py_')
        if tag == 'ndarray':
            arr = np.frombuffer(base64.b64decode(value['data'].encode('ascii')),
                                 dtype=value['dtype'])
            return arr.reshape(tuple(value['shape']))
        if tag == 'uuid':
            return uuid.UUID(value['hex'])
        return {k: _decode_meta_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode_meta_value(v) for v in value]
    return value


def _sanitize_for_metadata(value):
    """
    Recursively convert a decoded meta value (as returned by
    unpack_diffraction_meta_all) into types that emdfile.Metadata.to_h5 (and
    thus py4DSTEM.save) can actually serialize: numbers, bools, strings, None,
    numpy arrays, dicts, and tuples/lists of numbers/arrays/strings.

    value : a value from the decoded meta tree (dict, list, tuple, uuid.UUID,
        or plain scalar). Since this function recurses into dicts/lists/tuples,
        it is also called (by itself) on every nested sub-value.

    Two things that show up in the decoded Azorus meta tree aren't in that
    list and make Metadata.to_h5 raise:
      - uuid.UUID values (e.g. 'uuid1')              -> converted to str.
      - lists/tuples of dicts (e.g. 'transforms')     -> converted to a dict
        keyed by string index, since Metadata does support nested dicts.

    Returns the sanitized value, in the same shape as the input except where
    noted above.
    """
    if isinstance(value, dict):
        return {k: _sanitize_for_metadata(v) for k, v in value.items()}
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (list, tuple)):
        sanitized = [_sanitize_for_metadata(v) for v in value]
        if any(isinstance(v, dict) for v in sanitized):
            return {str(i): v for i, v in enumerate(sanitized)}
        return tuple(sanitized) if isinstance(value, tuple) else sanitized
    return value


def _is_leaf(value):
    """
    Whether flatten_meta_dict should stop descending into a value and store
    it whole, rather than recursing further into it.

    value : a (already-decoded) value from the meta tree - a dict, list, or
        plain scalar.

    Returns True for dicts/lists that flatten_meta_dict should stop recursing
    into: plain scalars, and lists that don't contain any dicts (e.g. a list
    of numbers like 'datetime'). Returns False for dicts, and for lists that
    do contain at least one dict (e.g. 'transforms'), both of which
    flatten_meta_dict should recurse into instead.
    """
    if isinstance(value, dict):
        return False
    if isinstance(value, list) and any(isinstance(item, dict) for item in value):
        return False
    return True


def flatten_meta_dict(decoded_meta, prefix=""):
    """
    Flatten a (already-decoded) nested meta dict into {dotted.path: value}.

    decoded_meta : the (sub-)tree to flatten - a dict, list, or plain scalar,
        as returned by _decode_meta_value. Called recursively, so on
        recursive calls this is a nested value from the original tree rather
        than always the top-level dict.

    prefix : the dotted path accumulated so far (e.g. 'microscope'); used
        internally by the recursion to build up each leaf's full dotted key.
        Leave at the default "" when calling this on a top-level meta dict.

    Dicts are descended into and lists-of-dicts are descended into with an
    integer index in the path (e.g. 'transforms.0.name'); plain scalars and
    lists of scalars (e.g. 'datetime', 'n_frames') are kept whole as leaves.

    Returns a flat dict mapping each dotted path string to its (decoded) value.
    """
    flat = {}
    if isinstance(decoded_meta, dict):
        for k, v in decoded_meta.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            flat.update(flatten_meta_dict(v, key))
    elif isinstance(decoded_meta, list) and not _is_leaf(decoded_meta):
        for i, item in enumerate(decoded_meta):
            key = f"{prefix}.{i}" if prefix else str(i)
            flat.update(flatten_meta_dict(item, key))
    else:
        flat[prefix] = decoded_meta
    return flat


def unpack_diffraction_meta_all(f, fieldname = 'diffraction/meta'):
    """
    Decode every field present in the diffraction/meta JSON blob of an open
    Azorus .hp file, discovering field names rather than hardcoding them.

    f : open h5py.File handle for the Azorus .hp file.

    fieldname : name of the field in f holding the JSON-encoded blob to decode
        (e.g. 'diffraction/meta' for the acquisition-time lens metadata, or
        'attrs' for the scan/acquisition metadata).

    Returns (decoded, flat):
      decoded : the full meta tree with the same nested dict/list shape as
          the source JSON, but with base64-encoded ndarray fields decoded to
          numpy arrays and uuid fields decoded to uuid.UUID (e.g.
          decoded['microscope']['PA'] is a numpy array).
      flat : a dict mapping dotted field paths to their decoded values (e.g.
          flat['microscope.PA'], flat['microscope.Obj'], flat['datetime']),
          for quick discovery/lookup of every field without knowing the tree
          shape up front.
    """
    meta_str = np.bytes_(f[fieldname]).decode('utf-8')
    raw = json.loads(meta_str)
    decoded = _decode_meta_value(raw)
    flat = flatten_meta_dict(decoded)
    return decoded, flat


def plot_scan_coords(coord_data, scan_coords, loadupname):
    """
    Plot the raw (voltage) and calibrated (nm) scan coordinates.

    coord_data : (N, 2) array of raw scan positions in volts, as read directly
        from the Azorus .hp file's 'coords' field.

    scan_coords : dict with keys "x" and "y", the same N scan positions after
        calibration/transformation to real-space nm.

    loadupname : full path to the source .hp file; only its basename is used,
        as the figure's suptitle.
    """

    fig, (ax1, ax2) = plt.subplots(1, 2)
    ax1.plot(coord_data[:,0], coord_data[:,1],
             marker='.', markersize = 1, linestyle='None', color='blue', label='Voltage Coords')

    ax1.plot(coord_data[:10,0], coord_data[:10,1],
             marker='x', linestyle='-', color='red', label='Voltage Coords')

    ax1.set_xlabel('col 0 (V)')
    ax1.set_ylabel('col 1 (V)')
    ax1.set_aspect('equal')
    ax1.set_title('Voltage Coordinates')

    # from this we see that some swapping needs to be done. Red shows first
    # part of data which should be in top left hand side for a raster in usual
    # x, y coordinates.

    # Also note that we are in volts here. If things have been done correctly the
    # scan calibration should have been stored in the hp file. But it might also be
    # incorrect...

    ax2.plot(scan_coords["x"], scan_coords["y"],
             marker='.', markersize = 1, linestyle='None', color='blue', label='Voltage Coords')

    ax2.plot(scan_coords["x"][:10], scan_coords["y"][:10],
             marker='x', linestyle='-', color='red', label='Voltage Coords')

    ax2.set_xlabel('x (nm)')
    ax2.set_ylabel('y (nm)')
    ax2.set_aspect('equal')
    ax2.set_title('Real-Space Coordinates')

    fig.subplots_adjust(bottom=0.22)
    fig.text(0.5, 0.03,
             'The input voltage coordinates in the left-hand plot should have been transformed to '
             'real-space coordinates in the right-hand plot, such that the first scan points (i.e. '
             'where the scan starts) shown in red are at the top left hand corner of the plot.',
             ha='center', va='bottom', wrap=True, fontsize=8)

    fig.suptitle(os.path.basename(loadupname))
    plt.show()


def plot_scan_coordinate_check_figure(coord_data, scan_coords, scan_coords_reshape, grid_shape, loadupname,
                                       save_path=None):
    """
    Combined scan-coordinate sanity-check figure: the raw (voltage) and
    calibrated (nm) coordinate scatter plots (top row, as in plot_scan_coords)
    above the reshaped x/y coordinate grids shown as images (bottom row, as in
    plot_scan_coords_grid). The four plots occupy the left two-thirds of the
    figure (a 2x3 grid, using only the left two columns); the right-hand
    column holds the transform/translation check note (top) and a longer
    note on interpreting the coordinate trends (bottom).

    coord_data : (N, 2) array of raw scan positions in volts, as read directly
        from the Azorus .hp file's 'coords' field.

    scan_coords : dict with keys "x" and "y", the same N scan positions after
        calibration/transformation to real-space nm.

    scan_coords_reshape : dict with keys "x" and "y", the same coordinates as
        scan_coords but reshaped to the 2D scan grid_shape (row, col).

    grid_shape : (num_rows, num_cols) shape of the scan grid.

    loadupname : full path to the source .hp file; only its basename is used,
        as the figure's suptitle.

    save_path : if given, the figure is also saved to this path (e.g. a .png file).
    """
    fig = plt.figure(figsize=(13, 9))
    gs = fig.add_gridspec(2, 3, wspace=0.4, hspace=0.4)

    ax1 = fig.add_subplot(gs[0, 0])
    ax2 = fig.add_subplot(gs[0, 1])
    ax3 = fig.add_subplot(gs[1, 0])
    ax4 = fig.add_subplot(gs[1, 1])

    # --- Top row: raw (voltage) and calibrated (nm) scan coordinates ---
    ax1.plot(coord_data[:,0], coord_data[:,1],
             marker='.', markersize = 1, linestyle='None', color='blue', label='Voltage Coords')

    ax1.plot(coord_data[:10,0], coord_data[:10,1],
             marker='x', linestyle='-', color='red', label='Voltage Coords')

    ax1.set_xlabel('col 0 (V)')
    ax1.set_ylabel('col 1 (V)')
    ax1.set_aspect('equal')
    ax1.set_title('Voltage Coordinates')

    # from this we see that some swapping needs to be done. Red shows first
    # part of data which should be in top left hand side for a raster in usual
    # x, y coordinates.

    # Also note that we are in volts here. If things have been done correctly the
    # scan calibration should have been stored in the hp file. But it might also be
    # incorrect...

    ax2.plot(scan_coords["x"], scan_coords["y"],
             marker='.', markersize = 1, linestyle='None', color='blue', label='Voltage Coords')

    ax2.plot(scan_coords["x"][:10], scan_coords["y"][:10],
             marker='x', linestyle='-', color='red', label='Voltage Coords')

    ax2.set_xlabel('x (nm)')
    ax2.set_ylabel('y (nm)')
    ax2.set_aspect('equal')
    ax2.set_title('Real-Space Coordinates')

    # --- Note, to the right of the top row ---
    note_ax = fig.add_subplot(gs[0, 2])
    note_ax.axis('off')
    note_ax.text(0.5, 0.5,
             textwrap.fill(
                 'The input voltage coordinates in the left-hand plot should have been transformed to '
                 'real-space coordinates in the right-hand plot, such that the first scan points (i.e. '
                 'where the scan starts) shown in red are at the top left hand corner of the plot.',
                 width=38),
             ha='center', va='center', fontsize=8, transform=note_ax.transAxes)

    # --- Bottom row: reshaped x/y coordinate grids, as images ---
    im3 = ax3.imshow(scan_coords_reshape["x"], cmap=cm.coolwarm)
    ax3.set_xlabel('Col Number')
    ax3.set_ylabel('Row Number')
    ax3.set_title('X coordinate')
    fig.colorbar(im3, ax=ax3, fraction=0.046, pad=0.04)

    im4 = ax4.imshow(scan_coords_reshape["y"], cmap=cm.coolwarm)
    ax4.set_xlabel('Col Number')
    ax4.set_ylabel('Row Number')
    ax4.set_title('Y coordinate')
    fig.colorbar(im4, ax=ax4, fraction=0.046, pad=0.04)

    # --- Descriptive note, to the right of the bottom row ---
    desc_ax = fig.add_subplot(gs[1, 2])
    desc_ax.axis('off')
    desc_paragraph_1 = (
        "If coordinate repacking from Azorus has been correctly performed, the x-coordinate "
        "value should increase with column number and there should be no variation in y. "
        "Similarly, the Y-coordinate value should be greater at the top of image, and lower "
        "at the bottom with no variation in x."
    )
    desc_paragraph_2 = (
        "If there is some rotation, this indicates that a coordinate transformation "
        "(rotation) was applied in Azorus before it was saved. If the trends go in the "
        "opposite directions to those described here, then likely your x and y scan voltage "
        "to beam position conversion is different to the system this code was tested on. If "
        "there is a saw-tooth like appearance then something unexpected has happened "
        "with the unpacking, and likely you should dig into the code or your specific data. "
        "Future work will deal better with these cases."
    )
    desc_text = "\n\n".join(textwrap.fill(p, width=38) for p in (desc_paragraph_1, desc_paragraph_2))
    desc_ax.text(0.5, 0.5, desc_text,
             ha='center', va='center', fontsize=8, transform=desc_ax.transAxes)

    fig.suptitle(os.path.basename(loadupname))
    if save_path is not None:
        fig.savefig(save_path)
    plt.show()


def plot_scan_coords_grid(scan_coords_reshape, grid_shape, loadupname):
    """
    Plot the reshaped x and y scan coordinate grids as 2D images (imshow) vs
    row/col index, since the data is defined on a regular meshgrid.
    If all has proceeded correctly the x-coordinate should increase steadily with column
    number, and not increase with the row number, and the y-coordinate should DECREASE
    with increasing row number, as image coordinates in py4dstem and other image programs
    have the zeroth row at the top of the image.

    scan_coords_reshape : dict with keys "x" and "y", each a 2D array (real-space
        nm scan coordinates) shaped like grid_shape.

    grid_shape : (num_rows, num_cols) shape of the scan grid.

    loadupname : full path to the source .hp file; only its basename is used,
        as the figure's suptitle.
    """
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=plt.figaspect(0.5))

    im1 = ax1.imshow(scan_coords_reshape["x"], cmap=cm.coolwarm)
    ax1.set_xlabel('Col Number')
    ax1.set_ylabel('Row Number')
    ax1.set_title('X coordinate')
    fig.colorbar(im1, ax=ax1)

    im2 = ax2.imshow(scan_coords_reshape["y"], cmap=cm.coolwarm)
    ax2.set_xlabel('Col Number')
    ax2.set_ylabel('Row Number')
    ax2.set_title('Y coordinate')
    fig.colorbar(im2, ax=ax2)

    fig.suptitle(os.path.basename(loadupname))
    plt.show()


def plot_scan_coords_grid_3d(scan_coords_reshape, grid_shape, loadupname):
    """
    Plot the reshaped x and y scan coordinate grids as 3D surfaces vs row/col index.
    If all has proceeded correctly the x-coordinate should increase steadily with column
    number, and not increase with the row number, and the y-coordinate should DECREASE
    with increasing row number, as image coordinates in py4dstem and other image programs
    have the zeroth row at the top of the image.

    scan_coords_reshape : dict with keys "x" and "y", each a 2D array (real-space
        nm scan coordinates) shaped like grid_shape.

    grid_shape : (num_rows, num_cols) shape of the scan grid.

    loadupname : full path to the source .hp file; only its basename is used,
        as the figure's suptitle.
    """
    row_n = np.arange(0, grid_shape[0])
    col_n = np.arange(0, grid_shape[1])
    X, Y = np.meshgrid(col_n, row_n)

    # set up a figure twice as wide as it is tall
    fig = plt.figure(figsize=plt.figaspect(0.5))

    # =============
    # First subplot
    # =============
    ax1 = fig.add_subplot(1, 2, 1, projection='3d')

    ax1.plot_surface(X, Y, scan_coords_reshape["x"], rstride=1, cstride=1, cmap=cm.coolwarm,
                           linewidth=0, antialiased=False)

    ax1.set_xlabel('Col Number')
    ax1.set_ylabel('Row Number')
    ax1.yaxis.set_inverted(True)
    ax1.set_zlabel('X coordinate')
    ax1.set_title('X coordinate')

    # ==============
    # Second subplot
    # ==============
    ax2 = fig.add_subplot(1, 2, 2, projection='3d')

    ax2.plot_surface(X, Y, scan_coords_reshape["y"], rstride=1, cstride=1, cmap=cm.coolwarm,
                           linewidth=0, antialiased=False)

    ax2.set_xlabel('Col Number')
    ax2.set_ylabel('Row Number')
    ax2.yaxis.set_inverted(True)
    ax2.set_zlabel('Y coordinate')
    ax2.set_title('Y coordinate')

    fig.suptitle(os.path.basename(loadupname))
    plt.show()


def plot_overview_image(overview_data, general_atts, nm_per_V, loadupname, save_path=None):
    """
    Plot the overview micrograph as a grayscale image (nm axes), with the scan
    mesh region (general_atts['meshParams']['extent']) overlaid as a rectangle.

    overview_data : 2D array, the overview micrograph image (from the Azorus
        .hp file's 'overview/micrograph' field).

    general_atts : the decoded 'attrs' meta dict (as returned by
        unpack_diffraction_meta_all(f, 'attrs')), used here for its
        '_overview_extent' and 'meshParams' entries:

        general_atts['_overview_extent'] gives [TopRowVoltage, LeftColVoltage,
        RowSpanVoltage, ColumnSpanVoltage]: in mesh (cartesian x, y) coordinates,
        the image's top-left corner is (LeftColVoltage, TopRowVoltage) and its
        bottom-right corner is (LeftColVoltage + ColumnSpanVoltage,
        TopRowVoltage + RowSpanVoltage).

        general_atts['meshParams']['extent'] gives [y0, x0, height, width] (mesh
        coordinates) of the scan mesh rectangle's upper-left corner and size.

    nm_per_V : scan calibration (nm per volt); both extents above are given in
        mesh (deflector voltage) coordinates, and are converted to nm via
        nm_per_V so the overlay lines up with the image.

    loadupname : full path to the source .hp file; only its basename is used,
        as the figure's suptitle.

    save_path : if given, the figure is also saved to this path (e.g. a .png file).
    """
    top_row_V, left_col_V, row_span_V, col_span_V = general_atts['_overview_extent']

    # Image extent, in mesh (voltage) coordinates converted to nm:
    #   row 0 (top of image)    -> mesh y = top_row_V
    #   last row (bottom of image) -> mesh y = top_row_V + row_span_V
    img_extent = (
        left_col_V * nm_per_V,
        (left_col_V + col_span_V) * nm_per_V,
        (top_row_V + row_span_V) * nm_per_V,
        top_row_V * nm_per_V,
    )

    fig, ax = plt.subplots()
    ax.imshow(overview_data, cmap='gray', extent=img_extent)

    mesh_y0, mesh_x0, mesh_h, mesh_w = general_atts['meshParams']['extent']
    rect = plt.Rectangle(
        (mesh_x0 * nm_per_V, mesh_y0 * nm_per_V),
        mesh_w * nm_per_V,
        mesh_h * nm_per_V,
        edgecolor='red', facecolor='none', linewidth=1.5, label='Scan Mesh',
    )
    ax.add_patch(rect)

    ax.set_xlabel('x (nm)')
    ax.set_ylabel('y (nm)')
    ax.set_aspect('equal')
    ax.set_title('Overview Image with Scan Mesh')
    ax.legend()

    fig.suptitle(os.path.basename(loadupname))
    if save_path is not None:
        fig.savefig(save_path)
    plt.show()


def plot_diffraction_and_virtual_images(datacube, loadupname, bf_disk_radius, save_path=None):
    """
    Show, in a single figure with four subplots, the virtual dark field image
    (upper left), the virtual bright field image (upper right), the mean
    diffraction pattern (lower left), and the max diffraction pattern with the
    bright field detector position overlaid (lower right).

    datacube : the py4DSTEM DataCube to compute/show the diffraction patterns
        and virtual images from.

    loadupname : full path to the source .hp file; only its basename is used,
        as the figure's suptitle.

    bf_disk_radius : radius (pixels) of the circular bright-field detector used
        both for the bright field virtual image and as the inner radius of the
        annular dark-field detector.

    save_path : if given, the figure is also saved to this path (e.g. a .png file).
    """
    fig, ((ax_df, ax_bf), (ax_mean, ax_max)) = plt.subplots(2, 2, figsize=(10, 10))

    num_Qy, num_Qx = datacube.shape[2:]
    center = (num_Qy / 2, num_Qx / 2)

    # --- lower left: mean diffraction pattern, useful for checking the quality of the data ---
    dp_mean = datacube.get_dp_mean()
    show(dp_mean, scaling='log', figax=(fig, ax_mean), title='Mean Diffraction Pattern')

    # --- lower right: max diffraction pattern, useful for determining the bright field disk
    # radius, with the bright field detector position overlaid ---
    datacube.get_dp_max()

    datacube.position_detector(
        mode = 'circle',
        data = datacube.tree('dp_max'),
        geometry = (
            center,
            bf_disk_radius
        ),
        figax = (fig, ax_max),
    )
    ax_max.set_title('Max Diffraction Pattern')

    # --- upper right: bright field virtual image ---
    datacube.get_virtual_image(
        mode = 'circle',
        geometry = (center,bf_disk_radius),
        name = 'bright_field',       # the output will be stored in `datacube`'s tree with this name
    )

    show(datacube.tree('bright_field'), figax=(fig, ax_bf), title='Bright Field')

    # --- upper left: dark field virtual image, using an annular detector, with the inner radius
    # set to the bright field disk radius and the outer radius set to a large number to include
    # all the rest of the detector ---
    r_inner,r_outer = bf_disk_radius , 2000
    # 2000 is just a large number to include all the rest of the detector
    radii = r_inner,r_outer

    datacube.get_virtual_image(
        mode = 'annulus',
        geometry = (center,radii),
        name = 'annular_dark_field'
    )

    show(datacube.tree('annular_dark_field'), figax=(fig, ax_df), title='Dark Field')

    fig.suptitle(os.path.basename(loadupname))
    if save_path is not None:
        fig.savefig(save_path)
    plt.show()


def _plot_bf_shifts_on_axis(parallax, ax, rotated=False, plot_arrow_freq=2, scale_arrows=1):
    """
    Draw a single BF-pixel-shifts quiver plot (measured, or rotation-corrected
    if rotated=True) onto a given axis.

    parallax : a py4DSTEM Parallax instance that has already had .reconstruct()
        run on it (and, if rotated=True, .aberration_fit() as well, since that
        is what sets the rotation_Q_to_R_rads attribute used below).

    ax : the matplotlib Axes to draw the quiver plot into.

    rotated : if False (default), plot the raw measured BF shifts; if True,
        plot the shifts after the fitted Q-to-R rotation correction is applied
        (requires parallax.rotation_Q_to_R_rads to exist).

    plot_arrow_freq : only plot every Nth arrow (in both scan directions), to
        keep the quiver plot legible.

    scale_arrows : multiplier applied to the plotted shift vectors' length.

    Reimplements the relevant part of Parallax.show_shifts()'s plotting math
    directly against a caller-supplied axis, since the installed py4DSTEM's
    show_shifts() always opens its own new figure and has no way to draw into
    an existing one (no `figax`/`ax` argument).
    """
    xp = parallax._xp
    asnumpy = parallax._asnumpy
    color = (1, 0, 0, 1)

    dp_mask_ind = xp.nonzero(parallax._dp_mask)
    yy, xx = xp.meshgrid(
        xp.arange(parallax._region_of_interest_shape[1]),
        xp.arange(parallax._region_of_interest_shape[0]),
    )
    freq_mask = xp.logical_and(xx % plot_arrow_freq == 0, yy % plot_arrow_freq == 0)
    masked_ind = xp.logical_and(freq_mask, parallax._dp_mask)
    plot_ind = masked_ind[dp_mask_ind]
    kr_max = xp.max(parallax._kr)

    if rotated:
        scaling_factor = (
            xp.array(parallax._reciprocal_sampling)
            / xp.array(parallax._scan_sampling)
            * scale_arrows
        )
        rotated_shifts = parallax._xy_shifts_Ang * scaling_factor
        tf_T = AffineTransform(angle=-parallax.rotation_Q_to_R_rads)
        rotated_kxy = tf_T(parallax._kxy[plot_ind], xp=xp)
        ax.quiver(
            asnumpy(rotated_kxy[:, 1]),
            asnumpy(rotated_kxy[:, 0]),
            asnumpy(rotated_shifts[plot_ind, 1]),
            asnumpy(rotated_shifts[plot_ind, 0]),
            angles="xy", scale_units="xy", scale=1,
        )
        ax.set_title("Rotated Bright Field Shifts")
    else:
        shifts = parallax._xy_shifts * scale_arrows * parallax._reciprocal_sampling[0]
        ax.quiver(
            asnumpy(parallax._kxy[plot_ind, 1]),
            asnumpy(parallax._kxy[plot_ind, 0]),
            asnumpy(shifts[plot_ind, 1]),
            asnumpy(shifts[plot_ind, 0]),
            color=color, angles="xy", scale_units="xy", scale=1,
        )
        ax.set_title("Measured Bright Field Shifts")

    ax.set_xlim([-1.2 * kr_max, 1.2 * kr_max])
    ax.set_ylim([-1.2 * kr_max, 1.2 * kr_max])
    ax.set_ylabel(r"$k_x$ [$A^{-1}$]")
    ax.set_xlabel(r"$k_y$ [$A^{-1}$]")
    ax.set_aspect("equal")


def _measured_shift_maps(parallax):
    """
    Recompute the measured per-scan-position vertical/horizontal BF shift maps
    (in Angstroms) that Parallax.aberration_fit(plot_BF_shifts_comparison=True)
    shows in the upper-left ("Measured Vertical Shifts") and lower-left
    ("Measured Horizontal Shifts") subplots of its own 4-subplot comparison
    figure.

    parallax : a py4DSTEM Parallax instance that has already had
        .aberration_fit() run on it (this is what sets the _xy_shifts_Ang
        attribute used below).

    Reimplemented directly from parallax's private attributes, since
    plot_BF_shifts_comparison=True both computes these arrays AND immediately
    opens its own separate figure with no way to get just the arrays back or
    redirect the plotting into a given axis.

    Returns (measured_shifts_sx, measured_shifts_sy), each shaped like
    parallax._region_of_interest_shape.
    """
    xp = parallax._xp
    asnumpy = parallax._asnumpy

    measured_shifts_sx = xp.zeros(parallax._region_of_interest_shape, dtype=xp.float32)
    measured_shifts_sx[parallax._xy_inds[:, 0], parallax._xy_inds[:, 1]] = (
        parallax._xy_shifts_Ang[:, 0]
    )

    measured_shifts_sy = xp.zeros(parallax._region_of_interest_shape, dtype=xp.float32)
    measured_shifts_sy[parallax._xy_inds[:, 0], parallax._xy_inds[:, 1]] = (
        parallax._xy_shifts_Ang[:, 1]
    )

    return asnumpy(measured_shifts_sx), asnumpy(measured_shifts_sy)


def plot_parallax_summary(parallax, loadupname, aberration_params=None, save_path=None):
    """
    Show, in a single figure with eight subplots, everything the parallax
    reconstruction stage produces:
      - the alignment convergence (error) curve (col 0, row 0)
      - the final aligned bright field image (col 1, row 0)
      - the upsampled bright field reconstruction (col 2, row 0)
      - the measured vertical BF shift map (col 3, row 0) - the upper-left
        subplot of aberration_fit(plot_BF_shifts_comparison=True)'s own figure
      - the measured (and, if fitted, rotation-corrected) BF pixel shift
        vectors (col 0/1, row 1)
      - a text panel listing every entry in `aberration_params`, if given
        (col 2, row 1)
      - the measured horizontal BF shift map (col 3, row 1) - the lower-left
        subplot of aberration_fit(plot_BF_shifts_comparison=True)'s own figure

    This is drawn using Parallax's lower-level plotting hook (`_visualize_figax(fig, ax, ...)`)
    plus the local `_plot_bf_shifts_on_axis`/`_measured_shift_maps` helpers above, rather than
    its higher-level `show_shifts()`/`visualize()`/`reconstruct(plot_aligned_bf=True)`/
    `aberration_fit(plot_BF_shifts_comparison=True)` convenience paths, since those always
    open their own separate figure rather than drawing into a given axis.

    parallax : a py4DSTEM Parallax instance that has already had .reconstruct()
        and .subpixel_alignment() run on it (and, optionally, .aberration_fit()
        - the rotation-corrected shifts, measured shift maps, and text panel
        are only shown if that was also run, detected via
        parallax.rotation_Q_to_R_rads).

    loadupname : full path to the source .hp file; only its basename is used,
        as the figure's suptitle.

    aberration_params : optional dict of fitted aberration/beam values (e.g.
        'defocus', 'cs', 'rotation_degrees') to list as text in the lower-middle
        panel; if None or empty, that panel is left blank.

    save_path : if given, the figure is also saved to this path (e.g. a .png file).
    """
    fig, axs = plt.subplots(2, 4, figsize=(19, 9))

    # --- col 0, row 0: alignment convergence (error) curve ---
    x_range = np.arange(len(parallax.error_iterations))
    axs[0, 0].plot(x_range, parallax.error_iterations)
    axs[0, 0].set_xlabel('Alignment step')
    axs[0, 0].set_ylabel('Error')
    axs[0, 0].set_title('Reconstruction Convergence')

    # --- col 1, row 0: final aligned bright field image ---
    parallax._visualize_figax(fig, axs[0, 1])
    axs[0, 1].set_xlabel('y [A]')
    axs[0, 1].set_ylabel('x [A]')
    axs[0, 1].set_title('Aligned Bright Field')

    # --- col 2, row 0: upsampled bright field reconstruction ---
    if hasattr(parallax, '_recon_BF_subpixel_aligned'):
        parallax._visualize_figax(fig, axs[0, 2], upsampled=True, cmap='grey')
        axs[0, 2].set_xlabel('y [A]')
        axs[0, 2].set_ylabel('x [A]')
        axs[0, 2].set_title('Upsampled Bright Field')
    else:
        axs[0, 2].axis('off')

    # --- col 0/1, row 1: measured (and, if fitted, rotation-corrected) BF pixel shifts ---
    _plot_bf_shifts_on_axis(parallax, axs[1, 0], rotated=False, plot_arrow_freq=2)
    have_aberrations = hasattr(parallax, 'rotation_Q_to_R_rads')
    if have_aberrations:
        _plot_bf_shifts_on_axis(parallax, axs[1, 1], rotated=True, plot_arrow_freq=2)
    else:
        axs[1, 1].axis('off')

    # --- col 3, rows 0/1: measured vertical/horizontal BF shift maps (the upper-left
    # and lower-left subplots of aberration_fit(plot_BF_shifts_comparison=True)) ---
    if have_aberrations:
        measured_shifts_sx, measured_shifts_sy = _measured_shift_maps(parallax)
        max_shift = max(np.abs(measured_shifts_sx).max(), np.abs(measured_shifts_sy).max())

        axs[0, 3].imshow(measured_shifts_sx, cmap='PiYG', vmin=-max_shift, vmax=max_shift)
        axs[0, 3].set_title('Measured Vertical Shifts')
        axs[0, 3].set_xticks([])
        axs[0, 3].set_yticks([])

        axs[1, 3].imshow(measured_shifts_sy, cmap='PiYG', vmin=-max_shift, vmax=max_shift)
        axs[1, 3].set_title('Measured Horizontal Shifts')
        axs[1, 3].set_xticks([])
        axs[1, 3].set_yticks([])
    else:
        axs[0, 3].axis('off')
        axs[1, 3].axis('off')

    # --- col 2, row 1: aberration_params values, as printed to the console ---
    axs[1, 2].axis('off')
    if aberration_params:
        text = "\n".join(f"{key}: {value:.5g}" for key, value in aberration_params.items())
        axs[1, 2].text(0.5, 0.5, text, ha='center', va='center', fontsize=12)

    fig.suptitle(os.path.basename(loadupname))
    fig.tight_layout()
    if save_path is not None:
        fig.savefig(save_path)
    plt.show()


def get_diffraction_calibration(beam_kV = 200, recon_pix_size = 25e-12, dpsize = 256):
    """
    Compute the diffraction calibration (reciprocal-space pixel size) for a
    given beam energy and desired reconstruction pixel size.

    beam_kV : beam energy (keV) used for the electron wavelength calculation.

    recon_pix_size : target real-space reconstruction pixel size (metres); the
        reciprocal-space calibration is derived to be consistent with this,
        not measured directly from the diffraction pattern (see TO DO below).

    dpsize : the diffraction pattern's pixel width (assumed square), used to
        convert the per-image angular field of view into a per-pixel
        reciprocal-space calibration.

    TO DO:
    Eventually, and in the course of achieving a more accurate reconstruction, an accurate calibration
    will be required. This would involve for example looking at the average diffraction pattern from a scanning
    nano-beam running over the sample at the same lens conditions, or fitting to disks etc. Distoptica and
    emicroml (Matthew Fitzpatrick's code) could be used to measure and correct for these distortions.
    Thus, the calibration provided here is just a starting point, and will likely be updated in due course.

    Returns (Q_pixel_size, Q_pixel_units, angular_FOV).
    """
    wvl = electron_wavelength(beam_kV)
    required_angular_FOV = wvl / recon_pix_size
    # See note above:  
    angular_FOV = required_angular_FOV

    experimental_recon_pix_size = wvl / angular_FOV
    exp_recon_pix_size_A = experimental_recon_pix_size * 1e10

    Q_pixel_size = (1 / exp_recon_pix_size_A) / dpsize
    Q_pixel_units = "A^-1"
    return Q_pixel_size, Q_pixel_units, angular_FOV
# %%

def azohp_to_py4d(loadupname, savepathname=None,
                   beam_kV=200, recon_pix_size=25e-12,
                   do_recentering=False, centre_method='fit', do_save=False,
                   get_parallax_plots=False, get_parallax_aberrations=False, bf_disk_radius=None,
                   plot_coord_checks=False, plot_overview=False, plot_virtual_diff=False,
                   plot_parallax_recon=False):
    """
    Convert a single Azorus-generated .hp file into a py4DSTEM DataCube, recenter
    the diffraction stack, and run a Parallax aberration reconstruction on it.

    This is the core conversion routine. besides reshaping the
    raw Azorus microscopy data into a py4DSTEM/emdfile-compatible DataCube, it embeds all of the
    Azorus .hp file's metadata (microscope lens, coil, and stage coordinates used during
    acquisition, along with timing and scan parameters) into the resulting DataCube, so that
    nothing is lost in the conversion. It also performs the basic checks, preprocessing, and
    analysis (scan-coordinate calibration, diffraction recentering, and the Parallax aberration
    reconstruction) that are useful to run prior to forming a ptychographic reconstruction; the
    results of that analysis are likewise embedded into the DataCube (and, if do_save is True,
    the saved output .h5 file) rather than only being returned or plotted.

    loadupname : full path to the .hp file to load.

    savepathname : directory the converted py4DSTEM file (and any of the plot_*
        figures below) would be saved into (defaults to a 'ReformattedForPy4DSTEM'
        subfolder next to loadupname).

    do_recentering : if True, estimate the diffraction pattern center (via
        datacube.get_probe_size(), refined per centre_method) and shift every
        diffraction pattern so that center sits at the detector's geometric
        center.

    centre_method : how to refine the center estimate when do_recentering is
        True. 'fit' additionally measures the origin at every scan position
        (py4DSTEM's get_origin) and fits a plane across the scan
        (fit_origin(..., fitfunction='plane')) to smooth out per-position
        noise/descan. Any other value (e.g. 'simple') skips that refinement
        and uses the single global probe-size-based center estimate directly.

    do_save : if True, save the resulting datacube (with the Azorus meta,
        scan-coordinate PointList, and parallax reconstruction attached) to a
        py4DSTEM/EMD '<name>_py4.h5' file in savepathname.

    get_parallax_plots : if True (along with get_parallax_aberrations), run the
        Parallax reconstruction pipeline (preprocess/reconstruct/subpixel_alignment)
        on the datacube, so plot_parallax_recon has something to show even if
        get_parallax_aberrations is False (i.e. without also fitting aberrations).

    get_parallax_aberrations : if True, additionally run Parallax's
        .aberration_fit() on the reconstruction, extracting/printing the fitted
        defocus and spherical aberration and populating the aberration_params
        text panel in the plot_parallax_recon figure.

    Each plot_* flag below both displays and saves (as a PNG in savepathname,
    named from loadupname the same way as the '_py4.h5' output) one of the four
    figures this function can produce:

    plot_coord_checks : display/save the combined scan-coordinate sanity-check
        figure (raw/calibrated coordinate scatter plots plus the reshaped x/y
        coordinate grids), to allow checks to be made on the scan coordinate
        transformations and translations. Saved as '<name>_coord_checks.png'.

    plot_overview : display/save the overview image with the scan mesh overlaid.
        Saved as '<name>_overview.png'.

    plot_virtual_diff : display/save the diffraction/virtual-image figure (dark
        field, bright field, mean and max diffraction patterns). Saved as
        '<name>_virtual_diff.png'.

    plot_parallax_recon : display/save the parallax reconstruction summary
        figure (only has an effect if get_parallax_plots or
        get_parallax_aberrations is also True, since that's what runs the
        parallax reconstruction in the first place). Saved as
        '<name>_parallax_recon.png'.

    Calibration:
    beam_kV : beam energy (kV) used for wavelength and reciprocal-space calibration.
    recon_pix_size : target reconstruction pixel size (metres).
    bf_disk_radius : radius (pixels) of the bright field disk used for virtual
        imaging and for the parallax reconstruction; if None, estimated from
        datacube.get_probe_size() when plot_virtual_diff is True.

    Returns (datacube, parallax).
    """
    # %%
    if savepathname is None:
        savepathname = os.path.join(os.path.dirname(loadupname), 'ReformattedForPy4DSTEM')
    os.makedirs(savepathname, exist_ok=True)

    # Base name shared by the saved '_py4.h5' file and every plot_* PNG below.
    corename_base = os.path.splitext(os.path.basename(loadupname))[0]

    print("-------- Starting Conversion and Load  ---------\n")
    f = h5py.File(loadupname, 'r')
    print(loadupname)
    print("\n-------- **************** ---------\n")

    
    print(f"Loaded File: {loadupname}")
    dp_meta, _ = unpack_diffraction_meta_all(f)
    
    print('Key data about the diffraction data acquistion:\n')
    
    print('\nMicroscope Params:\n')
    print(f"Scanning Mode Magnification: {dp_meta['microscope']['Mag']:.0f}")
    print(f"C1 Lens Current (A): {dp_meta['microscope']['C1'][0]:.5f}") # C1 Lens Current
    print(f"C2 Lens Current (A): {dp_meta['microscope']['C2'][0]:.5f}") # C2 Lens Current
    print(f"Objective Lens Current (A): {dp_meta['microscope']['Obj'][0]:.5f}") # Objective Lens Current
    print(f"Gun HV (kV): {dp_meta['microscope']['Gun.HighVoltage']:.1f}") 
    
    general_atts, _ = unpack_diffraction_meta_all(f, 'attrs')
    print('\nScan Params:\n')
    print(f"Mesh Style: {general_atts['style']['name']}")
    nm_per_V = general_atts['scanCalibration']/1e-9
    print(f"In-File Scan Calibration (nm/V): {nm_per_V:.4f}")
    # general_atts['_overview_extent'] = [TopRowVoltage, LeftColVoltage, RowSpanVoltage, ColumnSpanVoltage]
    scan_FOV = nm_per_V * np.array(general_atts['_overview_extent'])[[3, 2]]  # (x, y) extent
    print(f"Scan Extent (nm) {scan_FOV[0]:.2f} x {scan_FOV[1]:.2f}")
    
 
    # Determine the scan shape size, etc:
    
    # The (voltage) coordinates *******
    coord_data = np.array(f['coords'])
    # Note the 'transpose' here: 
    #    0-th element is the row,    i.e. y
    #    1-th element is the column, i.e. x
    scan_coords = {"x":nm_per_V*coord_data[:,1],"y":-1*nm_per_V*coord_data[:,0]}

    # Azorus lists the x, y points as simple lists. Also the diffraction data
    # is given as a simple list. py4dstem and others require the data as 4d cube, 
    # so the diffraction data and coordinates must be reshaped into cube in 
    # order to be successfully used in py4dstem and other similar codes.
    
    # Assuming it is rectangular grid get the shape.
    # An azorus image spans +/- 8 V:
    grid_shape = general_atts['meshParams']['shape']
    # Not sure if py4stem can actually use irregular or random coord positions in
    # py4dstem, but let's assign anyway for consistency. Hopefully this comes in handy for
    # ptyRAD, etc
    scan_coords_reshape = {"x":scan_coords["x"].reshape(grid_shape,order='C'),
                           "y":scan_coords["y"].reshape(grid_shape,order='C')}

    # plot 1
    if plot_coord_checks:
        save_path = os.path.join(savepathname, corename_base + '_coord_checks.png')
        plot_scan_coordinate_check_figure(coord_data, scan_coords, scan_coords_reshape, grid_shape,
                                           loadupname, save_path=save_path)

    # With ptychography we might have - in fact likely did - use an irregular scan.
    # Py4dSTEM assumes a uniform pixel size, so what do we do to proceed?
    
    # Here I am taking the mean of the x values on the left hand side of the scan (i.e. column 0), and the
    # mean on the column values on the right hand side to get the x limits.
    xlims = np.array((np.mean(scan_coords_reshape["x"][:,0]) , np.mean(scan_coords_reshape["x"][:,-1])))
    # Similar is done from the y limits:
    ylims = np.array((np.mean(scan_coords_reshape["y"][-1,:]) , np.mean(scan_coords_reshape["y"][0,:])))
            
    # Thus, in the case of Jitter scan with random offsets from a regular grid, the pixel size here
    # is just an approximation. Nonetheless, it useful as it allows to process the image in py4dstem to 
    # get a sense of the image, using it's built in functions and features.
    # Note of course that ptychographic results obtained directly from py4dstem will not be accurate unless
    # the scan coordinates given above are used in the reconstruction.
    # will of course require the real coordinates to be used.

    #  Use these limits to get the average step (pixel) size.
    cal = py4DSTEM.Calibration()
    y_pix_sz = (ylims[1]-ylims[0])/(grid_shape[0]-1)
    x_pix_sz = (xlims[1]-xlims[0])/(grid_shape[1]-1)

    cal.R_pixel_size = (x_pix_sz + y_pix_sz)/2
    cal.R_pixel_units = "nm"

    #%%        
    # Get the diffraction Data
    dp_data = np.array(f['diffraction/micrograph'])
    
    # Calibration of the diffraction data is a bit more involved, and hence is performed in a separation function.
    # For now, we'll just take in a value from the user and use that to calibrate the diffraction data.
    dp_size = dp_data.shape[-1] # this assumes the diffraction data is square, which it should be... but might not be. 

    cal.Q_pixel_size, cal.Q_pixel_units, angular_FOV = \
        get_diffraction_calibration(beam_kV = beam_kV, recon_pix_size = recon_pix_size, dpsize=dp_size)

    # Now that we have the diffraction data we can create a py4DSTEM datacube object. 
    datacube = py4DSTEM.DataCube(np.reshape(dp_data, tuple(grid_shape) + dp_data.shape[1:]),
                                 calibration = cal)
    # NOTE if cal object is changed later it will update the datacube. It is not fixed at creation.

    # Recenter the diffraction stack:
    r_est = None
    if do_recentering:
        # Get the approximate radius of the center beam (in pixels)
        datacube.get_dp_mean() # this is required in order to run the next command:
        r_est, qx0, qy0 = datacube.get_probe_size(thresh_lower=0.05, thresh_upper=0.99)
        if centre_method  == 'fit':
            # This returns the measured (qx0, qy0) positions of the center disks
            qx0_meas, qy0_meas, _ = py4DSTEM.process.calibration.get_origin(datacube, r=r_est, rscale=1.2)
            # Fit the measured origins to a plane, parabola, etc. to smooth out noise
            qx0, qy0, _, _ = py4DSTEM.process.calibration.fit_origin((qx0_meas, qy0_meas), fitfunction='plane')
        # The centre of the diffraction data images:
        num_Qy, num_Qx = datacube.shape[2:]
        center_y, center_x = num_Qy / 2, num_Qx / 2
        py4DSTEM.preprocess.preprocess.datacube_diffraction_shift(datacube,
                            center_x - qx0,
                            center_y - qy0,
                            periodic=True, bilinear=False)
        
    overview_data = np.array(f['overview/micrograph'])

    # plot 2
    if plot_overview:
        save_path = os.path.join(savepathname, corename_base + '_overview.png')
        plot_overview_image(overview_data, general_atts, nm_per_V, loadupname, save_path=save_path)

    # plot 3
    if plot_virtual_diff:
        if bf_disk_radius is None:
            r_est, _ , _ = datacube.get_probe_size(thresh_lower=0.05, thresh_upper=0.99)
            bf_disk_radius = r_est*1.2
        save_path = os.path.join(savepathname, corename_base + '_virtual_diff.png')
        plot_diffraction_and_virtual_images(datacube, loadupname, bf_disk_radius, save_path=save_path)
    #%%    
    parallax = None
    if r_est is None:
        r_est, _ , _ = datacube.get_probe_size(thresh_lower=0.05, thresh_upper=0.99)
    if get_parallax_plots or get_parallax_aberrations:
        # With explicit radius on BF disk
        parallax = py4DSTEM.process.phase.Parallax(
            datacube=datacube,
            energy = beam_kV*1e3,
            device = "cpu",
            object_padding_px=(8,8),
        ).preprocess(
            edge_blend=8,
            plot_average_bf=False,
            r = r_est,
        ).reconstruct(
            alignment_bin_values=np.array([32,32,32,32,32,32,16,16,16,16,8,8]),
            regularize_shifts=False,
            progress_bar=True,
            # Suppressed: by default reconstruct() pops up its own per-bin aligned-BF grid plus a
            # convergence plot, regardless of plot_parallax_recon/get_parallax_plots. The convergence
            # curve is instead redrawn (from parallax.error_iterations) inside plot_parallax_summary below.
            plot_aligned_bf=False,
            plot_convergence=False,
        )

        # throws error : -> 1027 self.error_iterations.append(float(self._recon_error))
        # TypeError: only 0-dimensional arrays can be converted to Python scalars
        # This related to me currently running numpy 2.x in python 3.13, whereas py4DSTEM 
        # was written for numpy 1.x. The fix is to convert the numpy array to a float, as below:
        # self.error_iterations.append(self._recon_error.astype(float))
        # ... \py4DSTEM\process\phase\parallax.py
        # line 1443 (latest dev version) or line 1027 in 14.14 version currently installed. 
        # Applying this fix gets this bit of code back in business, though regions might break later... 
        
        alignment_plots = False # Maybe add this in as a switch later?
        # lets just see what upsampling can do ...
        parallax = parallax.subpixel_alignment(
            kde_upsample_factor=4,
            plot_upsampled_BF_comparison=alignment_plots,
            plot_upsampled_FFT_comparison=alignment_plots,
        )

        defocus, cs = None, None
        main_aberrations_and_params = None
        if get_parallax_aberrations:
            parallax = parallax.aberration_fit(
                fit_BF_shifts=True,
                fit_aberrations_max_radial_order=6,
                fit_aberrations_max_angular_order=4,
                # Suppressed: aberration_fit()'s own BF-shifts comparison plot always opens its own
                # figure. The (rotation-corrected) shifts are instead redrawn inside
                # plot_parallax_summary below.
                plot_BF_shifts_comparison=False,
            )
            # extract key first order aberrations:
                
            main_aberrations_and_params = {'defocus': parallax.aberration_dict_cartesian[(1,0,0)]['value [Ang]'],
                               'cs': parallax.aberration_dict_cartesian[(3,0,0)]['value [Ang]'],
                               'rotation_degrees': np.rad2deg(parallax.rotation_Q_to_R_rads),
                               'approximate_beam_half_angle_mrad':(r_est/dp_size)*angular_FOV*1000,
                               'recon_pixel_size_pm':recon_pix_size*1e12,
                               'diffraction_angular_FOV_mrad':angular_FOV*1000}
        
            defocus = parallax.aberration_dict_cartesian[(1,0,0)]['value [Ang]']
            cs = parallax.aberration_dict_cartesian[(3,0,0)]['value [Ang]']

            print(f"Defocus (nm): {defocus/10:.5f}")
            print(f"Spherical Aberration (mm): {cs/10**7:.5f}")

        # plot 4
        if plot_parallax_recon:
            save_path = os.path.join(savepathname, corename_base + '_parallax_recon.png')
            plot_parallax_summary(parallax, loadupname, aberration_params=main_aberrations_and_params,
                                   save_path=save_path)
# %%
    # The meta fields read in from the Azorus hp file should be embedded in the py4DSTEM file, so that they can be
    # accessed later if needed. `datacube.tree(...)` only accepts other Node instances (DataCube, Array,
    # PointList, etc.), not plain dicts, so these instead need to be attached as `datacube.metadata`
    # (emdfile.Metadata instances) - that's the emdfile mechanism for storing arbitrary side-car data on a
    # node. `_sanitize_for_metadata` reworks the decoded meta trees into types Metadata.to_h5 can actually
    # serialize (it can't write uuid.UUID values or a list of dicts, like 'transforms', directly).
    
    datacube.metadata = py4DSTEM.Metadata(name='diffraction_meta', data=_sanitize_for_metadata(dp_meta))
    # retrieve by
    # datacube.metadata['diffraction_meta']['microscope']['Obj']
    
    datacube.metadata = py4DSTEM.Metadata(name='general_meta', data=_sanitize_for_metadata(general_atts))
    # retrieve by
    # datacube.metadata['general_meta']['scanCalibration']
    
    # Also store the scan coordinates, so that they can be accessed later if needed.
    datacube.metadata = py4DSTEM.Metadata(name='scan_coords', data=scan_coords_reshape)
    # get at data by for example:
    # datacube.metadata['scan_coords']['x']

    # Unlike the dict-shaped meta above, a PointList *is* a Node, so it's added as a
    # child via `datacube.tree(...)` rather than as metadata. It needs a flat numpy
    # structured array, so the (row, col) grids in scan_coords_reshape are raveled
    # back to one point per scan position, in the same order='C' used to reshape them.
    scan_coords_pointdata = np.empty(scan_coords_reshape["x"].size, dtype=[('x', float), ('y', float)])
    scan_coords_pointdata['x'] = scan_coords_reshape["x"].ravel(order='C')
    scan_coords_pointdata['y'] = scan_coords_reshape["y"].ravel(order='C')
    datacube.tree(py4DSTEM.PointList(data=scan_coords_pointdata, name='scan_coords_pointlist'))

    # The Parallax reconstruction (aligned BF image, aberration fits, etc.) is itself an emdfile
    # Node (py4DSTEM's phase-reconstruction classes subclass emdfile.Custom) with its own
    # to_h5/from_h5, so - like the PointList above, and unlike the plain-dict meta - it just needs
    # adding as a child node to be saved and reloaded alongside the datacube. By default it does
    # *not* re-embed its own copy of the datacube (Parallax's `save_datacube` default is False),
    # so this doesn't duplicate the diffraction data in the saved file.
    if parallax is not None:
        datacube.tree(add=parallax)
        
    # load aberrations back by for example: 
    # test = datacube.tree('parallax_reconstruction')
    # test.aberration_dict_cartesian[(1,0,0)]
    

    if do_save:
        corename = corename_base + '_py4.h5'
        save_converted_name = os.path.join(savepathname,corename)
        py4DSTEM.save(
            save_converted_name,
            datacube,
            mode = 'o'    # this says that if a file of this name already exists, we'll overwrite it
            #     tree = None,  # this indicates saving everything *under* datacube, but not not datacube itself
        )

    return datacube


if __name__ == "__main__":
    """
    Example usage of azohp_to_py4d() to convert a set of Azorus-generated .hp files into py4DSTEM DataCubes, 
    recenter the diffraction stacks, and run Parallax aberration reconstructions on them. The resulting 
    DataCubes and any requested figures are saved to the specified output directory.
    """

    # %%
    py4DSTEM.__version__

    base_dir = r"H:\Arthur\2026_transfer" 

    fnames = ["Dataset 7  Defocus x+2.hp",
    "Dataset 8  AT FOCUS.hp",
    "Dataset 9  Defocus x-1.hp",
    "Dataset 11  Defocus x-3.hp"]

    savepathname = r"D:\Data\Code_Dev_Example_Data\2026_05_15\test_output"

    # %%

    # run_inds = [0,2,3,4,5,6,7]
    # run_inds = [0]
    # range(1,4)

    for ii in range(0,len(fnames)):
        datacube, parallax = azohp_to_py4d(
            os.path.join(base_dir, fnames[ii]),
            savepathname=savepathname,
            beam_kV=200,
            recon_pix_size=25e-12,
            do_recentering=True,
            centre_method='simple',
            do_save=True,
            get_parallax_plots=True,
            get_parallax_aberrations=True,
            bf_disk_radius=None,
            plot_coord_checks=True,
            plot_overview=True,
            plot_virtual_diff=True,
            plot_parallax_recon=True,
            )

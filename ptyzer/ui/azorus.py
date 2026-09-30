"""Azorus metadata and calibration without a reconstruction-engine dependency."""
import base64
import json
import math
import uuid
import numpy as np


def decode(value):
    if isinstance(value, dict):
        if value.get('_py_') == 'ndarray':
            return np.frombuffer(base64.b64decode(value['data']), dtype=value['dtype']).reshape(value['shape'])
        if value.get('_py_') == 'uuid':
            return uuid.UUID(value['hex'])
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


def flatten(value, prefix=''):
    if isinstance(value, dict):
        items = value.items()
    elif isinstance(value, list) and any(isinstance(v, dict) for v in value):
        items = enumerate(value)
    else:
        return {prefix: value}
    result = {}
    for k, v in items:
        result.update(flatten(v, f'{prefix}.{k}' if prefix else str(k)))
    return result


def unpack_diffraction_meta_all(f, fieldname='diffraction/meta'):
    raw = f[fieldname][()]
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8')
    value = decode(json.loads(raw))
    return value, flatten(value)


def calibration(attrs, coords, detector_size, beam_kV, pixel_pm):
    shape = tuple(int(v) for v in attrs['meshParams']['shape'])
    if len(shape) != 2 or min(shape) < 2:
        raise ValueError('Reconstruction requires a raster with at least two rows and columns')
    nm_per_v = float(attrs['scanCalibration']) / 1e-9
    if not math.isfinite(nm_per_v) or nm_per_v <= 0:
        raise ValueError('Scan calibration must be finite and positive')
    xy = np.asarray(coords, dtype=float)
    if xy.shape != (math.prod(shape), 2) or not np.isfinite(xy).all():
        raise ValueError('Coordinates must contain one finite pair per scan position')
    x = (nm_per_v * xy[:, 1]).reshape(shape)
    y = (-nm_per_v * xy[:, 0]).reshape(shape)
    xstep = (x[:, -1].mean() - x[:, 0].mean()) / (shape[1] - 1)
    ystep = (y[0].mean() - y[-1].mean()) / (shape[0] - 1)
    step = float((xstep + ystep) / 2)
    if not math.isfinite(step) or step <= 0:
        raise ValueError('Scan coordinates do not follow the supported positive raster orientation')
    wavelength_m = 4.135667662e-18 * 2.99792458e8 / math.sqrt(beam_kV * (2 * 510.9989461 + beam_kV))
    qstep = 1 / (pixel_pm * 1e-12 * 1e10 * detector_size)
    return shape, x, y, step, qstep, wavelength_m / (pixel_pm * 1e-12) * 1000

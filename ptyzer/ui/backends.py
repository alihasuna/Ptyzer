"""Probe trusted, server-configured Python environments for optional engines."""
import json
import os
import subprocess
import sys


def quantem_python(configured=None):
    return os.path.abspath(os.path.expanduser(configured)) if configured else sys.executable


def probe_quantem(python):
    script = '''import json, quantem, numpy
from quantem.core.datastructures import Dataset4dstem
from quantem.diffractive_imaging import DirectPtychography
assert hasattr(DirectPtychography, "fit_hyperparameters_cross_correlation")
assert hasattr(DirectPtychography, "fit_hyperparameters_least_squares")
print("PTYZER_BACKEND=" + json.dumps({"version": quantem.__version__, "numpy": numpy.__version__}))'''
    info = {'available': False, 'python': python, 'output_format': '.zarr.zip'}
    try:
        run = subprocess.run([python, '-c', script], capture_output=True, text=True, timeout=60)
        if run.returncode:
            info['error'] = run.stderr.strip().splitlines()[-1] if run.stderr.strip() else 'Quantem could not be imported'
        else:
            payload = next(line for line in reversed(run.stdout.splitlines()) if line.startswith('PTYZER_BACKEND='))
            info.update(json.loads(payload.split('=', 1)[1]), available=True)
    except (OSError, subprocess.TimeoutExpired, StopIteration, ValueError) as exc:
        info['error'] = str(exc) or 'Quantem environment probe failed'
    return info

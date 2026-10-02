import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MPLBACKEND", "Agg")

from ptyzer.ui.sample_data import write_sample_hp  # noqa: E402

# Ground truth shared by every synthetic case: parallax displacement of 5 nm per radian of probe
# angle (a 5 nm defocus), calibrated at the converter's defaults (200 kV, 25 pm).
SHIFT_PER_ANGLE_NM = 5.0
CASES = {
    "base": dict(),
    "neg": dict(shift_per_angle_nm=-SHIFT_PER_ANGLE_NM),
    "rot30": dict(rotation_deg=30.0),
    "mirror": dict(transpose=True),
    "mirror_rot30": dict(transpose=True, rotation_deg=30.0),
}


@pytest.fixture(scope="session")
def samples(tmp_path_factory):
    """Write each ground-truth case once per session; returns {name: (path, truth)}."""
    root = tmp_path_factory.mktemp("ground_truth")
    out = {}
    for name, kwargs in CASES.items():
        truth = {"shift_per_angle_nm": SHIFT_PER_ANGLE_NM, "rotation_deg": 0.0, "transpose": False, **kwargs}
        out[name] = (write_sample_hp(str(root / f"{name}.hp"), **kwargs), truth)
    return out

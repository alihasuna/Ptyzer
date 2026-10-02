"""
Where conversions write when the data folder is read-only (e.g. a read-only sshfs mount of a
project): ptyzer.ui.jobs.output_target, the queue's refusal of unwritable targets, finding earlier
outputs in the fallback location, and the 'Fir data' file-browser shortcut. Engine-neutral.
"""
import os
import shutil
import stat

import pytest

from ptyzer.ui import hpfile, jobs


@pytest.fixture
def readonly_data(samples, tmp_path):
    """A copy of the sample in a folder this user can't write to (as on a read-only mount)."""
    data = tmp_path / "fir" / "project-x"
    data.mkdir(parents=True)
    hp = data / "scan.hp"
    shutil.copy(samples["base"][0], hp)
    data.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
    if os.access(data, os.W_OK):  # e.g. running as root
        data.chmod(0o755)
        pytest.skip("can't make a read-only folder for this user")
    yield str(hp)
    data.chmod(0o755)


def test_writable_source_writes_next_to_the_file(samples, tmp_path):
    hp = tmp_path / "data" / "scan.hp"
    hp.parent.mkdir()
    shutil.copy(samples["base"][0], hp)
    t = jobs.output_target(str(hp), {}, str(tmp_path / "out-root"))
    assert t == {"dir": str(hp.parent / "ReformattedForPy4DSTEM"), "writable": True, "fallback": False,
                 "source_dir": str(hp.parent)}


@pytest.mark.parametrize("backend, sub", [("py4dstem", "ReformattedForPy4DSTEM"), ("quantem", "ReformattedForQuantem")])
def test_readonly_source_falls_back_to_output_root(readonly_data, tmp_path, backend, sub):
    root = tmp_path / "ptyzer-output"
    t = jobs.output_target(readonly_data, {"backend": backend}, str(root))
    assert t["fallback"] and t["writable"]
    assert t["dir"] == str(root / "project-x" / sub)


def test_explicit_unwritable_output_folder_is_refused_at_queue_time(readonly_data, tmp_path):
    manager = jobs.JobManager(output_root=str(tmp_path / "out"))
    target_dir = os.path.join(os.path.dirname(readonly_data), "results")
    assert not jobs.output_target(readonly_data, {"output_dir": target_dir})["writable"]
    with pytest.raises(ValueError, match="isn't writable"):
        manager.submit([readonly_data], {"output_dir": target_dir})
    assert manager.list() == []


def test_queued_job_uses_the_fallback(readonly_data, tmp_path):
    root = tmp_path / "out"
    manager = jobs.JobManager(output_root=str(root))
    manager._queue = type("NoRun", (), {"put": lambda self, item: None})()  # don't start a worker
    job = manager.submit([readonly_data], {"per_run_folder": False})[0]
    assert job.output_dir == str(root / "project-x" / "ReformattedForPy4DSTEM")


def test_earlier_outputs_found_in_the_fallback(readonly_data, tmp_path):
    root = tmp_path / "out"
    folder = root / "project-x" / "ReformattedForPy4DSTEM"
    folder.mkdir(parents=True)
    (folder / "scan_py4.h5").write_bytes(b"")
    outputs = hpfile.existing_outputs(readonly_data, str(root))
    assert [o["folder"] for o in outputs] == [str(folder)]


def test_fir_shortcut(tmp_path, monkeypatch):
    from ptyzer.ui.server import App
    monkeypatch.setenv("HOME", str(tmp_path))
    labels = lambda: [p["label"] for p in App.places(type("A", (), {"start_dir": str(tmp_path), "sample_dir": "/nonexistent"})())]
    assert "Fir data" not in labels()
    (tmp_path / "fir").mkdir()
    assert "Fir data" in labels()

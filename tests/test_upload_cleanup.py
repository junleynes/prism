"""The system temp folder must not grow without limit (JUN: 1.8 TB and growing)."""
import os
import time
import unittest.mock as mock

with mock.patch('requests.post'), mock.patch('requests.get'):
    import core
    import pipeline


def _age(path, secs):
    t = time.time() - secs
    os.utime(path, (t, t))


def test_folders_left_by_dead_runs_are_removed_and_live_ones_kept(tmp_path):
    dead, live, other = (tmp_path / 'prism_up_dead'), (tmp_path / 'prism_up_live'), (tmp_path / 'unrelated')
    for d in (dead, live, other):
        d.mkdir()
        (d / 'big.bin').write_bytes(b'x' * 1000)
    (dead / '.pid').write_text('999999999')
    (live / '.pid').write_text(str(os.getppid()))
    freed = core.reap_stale_upload_dirs(str(tmp_path))
    assert freed >= 1000 and not dead.exists()
    assert live.exists() and other.exists(), 'a running instance and non-PRISM folders are never touched'


def test_a_folder_with_no_pid_is_only_removed_once_idle(tmp_path):
    fresh, old = tmp_path / 'prism_up_fresh', tmp_path / 'prism_up_old'
    for d in (fresh, old):
        d.mkdir()
    _age(old, 7 * 3600)
    core.reap_stale_upload_dirs(str(tmp_path))
    assert fresh.exists() and not old.exists()


def test_the_sweeper_removes_old_work_folders_not_just_files(tmp_path):
    folder = pipeline.app.config['UPLOAD_FOLDER']
    old, busy = os.path.join(folder, 'work_old'), os.path.join(folder, 'work_busy')
    for d in (old, busy):
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, 'seg.mp4'), 'wb').write(b'y' * 500)
    _age(os.path.join(old, 'seg.mp4'), 7 * 3600); _age(old, 7 * 3600)
    _age(busy, 7 * 3600)                                     # folder looks old, but a file inside is fresh
    pid = os.path.join(folder, '.pid')
    open(pid, 'w').write('1'); _age(pid, 9 * 3600)
    pipeline.sweep_upload_folder()
    assert not os.path.exists(old) and os.path.isdir(busy)
    assert os.path.exists(pid), "the live-run marker is never swept"
    import shutil; shutil.rmtree(busy, ignore_errors=True)


def test_the_size_cap_deletes_oldest_first_and_spares_recent_files(tmp_path, monkeypatch):
    folder = pipeline.app.config['UPLOAD_FOLDER']
    names = {'a_old.bin': 3 * 3600, 'b_mid.bin': 2 * 3600, 'c_new.bin': 60}
    for n, age in names.items():
        p = os.path.join(folder, n)
        open(p, 'wb').write(b'z' * 1024 * 1024)
        _age(p, age)
    monkeypatch.setattr(pipeline, 'UPLOAD_MAX_GB', 2.5 / 1024)   # 2.5 MB allowed, 3 MB present
    pipeline._enforce_upload_cap(folder, time.time())
    assert not os.path.exists(os.path.join(folder, 'a_old.bin'))
    assert os.path.exists(os.path.join(folder, 'b_mid.bin')) and os.path.exists(os.path.join(folder, 'c_new.bin'))
    monkeypatch.setattr(pipeline, 'UPLOAD_MAX_GB', 0.0001 / 1024)  # far over, but c_new is too recent to touch
    pipeline._enforce_upload_cap(folder, time.time())
    assert not os.path.exists(os.path.join(folder, 'b_mid.bin')) and os.path.exists(os.path.join(folder, 'c_new.bin'))
    os.remove(os.path.join(folder, 'c_new.bin'))

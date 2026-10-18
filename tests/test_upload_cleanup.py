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


def test_folders_of_runs_that_stopped_beating_are_removed_and_live_ones_kept(tmp_path):
    dead, live, other = (tmp_path / 'prism_up_dead'), (tmp_path / 'prism_up_live'), (tmp_path / 'unrelated')
    for d in (dead, live, other):
        d.mkdir()
        (d / 'big.bin').write_bytes(b'x' * 1000)
    (dead / '.pid').write_text('1'); _age(dead / '.pid', 3600)      # nobody has touched it for an hour
    (live / '.pid').write_text('1')                                 # touched just now
    freed = core.reap_stale_upload_dirs(str(tmp_path))
    assert freed >= 1000 and not dead.exists()
    assert live.exists() and other.exists(), 'a running instance and non-PRISM folders are never touched'


def test_a_folder_with_no_marker_is_only_removed_once_idle(tmp_path):
    fresh, old = tmp_path / 'prism_up_fresh', tmp_path / 'prism_up_old'
    for d in (fresh, old):
        d.mkdir()
    (old / 'f.bin').write_bytes(b'z'); _age(old / 'f.bin', 7 * 3600); _age(old, 7 * 3600)
    core.reap_stale_upload_dirs(str(tmp_path))
    assert fresh.exists() and not old.exists()


def test_old_style_folders_are_reaped_only_where_the_deployment_gave_the_folder_to_the_app(tmp_path):
    """JUN: D:\\TEMP held 1.8 TB in tmpXXXXXXXX folders from before the prefix existed."""
    legacy, busy, stranger = tmp_path / 'tmpa1b2c3d4', tmp_path / 'tmpzzzzzzzz', tmp_path / 'my_own_folder'
    for d in (legacy, busy, stranger):
        d.mkdir()
        (d / 'clip.mp4').write_bytes(b'v' * 4000)
    _age(legacy / 'clip.mp4', 9 * 3600); _age(legacy, 9 * 3600)
    _age(stranger / 'clip.mp4', 9 * 3600); _age(stranger, 9 * 3600)
    assert core.reap_stale_upload_dirs(str(tmp_path)) == 0 and legacy.exists(), 'not asked to: the system temp folder is not ours'
    found = core.stale_upload_dirs(str(tmp_path), legacy=True)
    assert [os.path.basename(p) for p, _b in found] == ['tmpa1b2c3d4'] and found[0][1] == 4000
    assert core.reap_stale_upload_dirs(str(tmp_path), legacy=True) == 4000
    assert not legacy.exists() and busy.exists() and stranger.exists(), 'recent and differently named folders stay'


def test_a_running_instance_keeps_its_marker_fresh_and_nothing_signals_a_process():
    import inspect
    folder = core.app.config['UPLOAD_FOLDER']
    assert os.path.exists(os.path.join(folder, '.pid'))
    assert 'os.kill' not in inspect.getsource(core), 'on Windows signalling a pid terminates it'


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


def test_old_style_folders_in_a_shared_temp_folder_need_prism_files_in_them(tmp_path):
    """JUN: D:\\TEMP\\tmpq6i9xlcc, from September, holding net_*.mov and thumb_*.jpg, was still there."""
    ours, theirs, empty = tmp_path / 'tmpq6i9xlcc', tmp_path / 'tmpabcd1234', tmp_path / 'tmpffff0000'
    for d in (ours, theirs, empty):
        d.mkdir()
    (ours / 'net_1789025550_WEEK6_DAY4_PLUG1.mov').write_bytes(b'm' * 3000)
    (ours / 'thumb_b1f3ad0613e8ed03.jpg').write_bytes(b'j' * 100)
    (theirs / 'installer.cab').write_bytes(b'o' * 500)
    for d in (ours, theirs, empty):
        for f in d.iterdir():
            _age(f, 40 * 24 * 3600)
        _age(d, 40 * 24 * 3600)
    found = core.stale_upload_dirs(str(tmp_path), legacy='signature')
    assert [os.path.basename(p) for p, _b in found] == ['tmpq6i9xlcc'] and found[0][1] == 3100
    assert core.reap_stale_upload_dirs(str(tmp_path), legacy='signature') == 3100
    assert not ours.exists() and theirs.exists(), "another program's folder is left alone"


def test_both_places_are_searched_for_old_folders():
    assert all(flag for _p, flag in core.upload_parents())

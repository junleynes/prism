import json
import os

import settings_store as ss


def _app_path(name):
    return os.path.join(ss.APP_DIR, name)


def test_saved_settings_survive_loss_of_the_file_and_import_old_file():
    p = _app_path('zz_test_settings.json')
    import database
    conn = database.connect()
    ss._ensure(conn)
    conn.execute("DELETE FROM app_settings WHERE name='zz_test_settings.json'")
    conn.commit()
    conn.close()
    try:
        assert ss.load(p) is None
        with open(p, 'w') as f:                      # a file from before: taken into the database
            json.dump({'a': 1}, f)
        assert ss.load(p) == {'a': 1}
        os.remove(p)                                  # app folder replaced: the settings are still there
        assert ss.load(p) == {'a': 1}
        ss.save(p, {'a': 2})
        os.remove(p)
        assert ss.load(p) == {'a': 2}
    finally:
        if os.path.exists(p):
            os.remove(p)
        conn = database.connect()
        conn.execute("DELETE FROM app_settings WHERE name='zz_test_settings.json'")
        conn.commit()
        conn.close()


def test_file_elsewhere_stays_a_plain_file(tmp_path):
    p = str(tmp_path / 'production_defaults.json')
    assert ss.load(p) is None
    ss.save(p, {'x': 1})
    assert json.load(open(p)) == {'x': 1} and ss.load(p) == {'x': 1}

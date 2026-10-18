"""The dashboard's quick actions: one per creation tab the account may use."""
import unittest.mock as mock

with mock.patch('requests.post'), mock.patch('requests.get'):
    import main


def _dash(role, perms):
    c = main.app.test_client()
    with c.session_transaction() as s:
        s.update(authed=True, user_id=1, username='u', role=role, csrf_token='t')
    with mock.patch('main.user_permissions', return_value=set(perms)):
        html = c.get('/').get_data(as_text=True)
    i = html.index('class="dash-actions"')
    return html[i:html.index('dash-grid', i)]


def test_admin_gets_all_three():
    a = _dash('admin', [])
    for label in ('Generate Highlights', 'Generate Shorts', 'Generate Artwork Animation'):
        assert label in a
    assert 'Generate a promo' not in a


def test_each_button_follows_the_permission():
    a = _dash('user', ['vertical_shorts'])
    assert 'Generate Shorts' in a and 'Generate Highlights' not in a and 'Artwork Animation' not in a
    b = _dash('user', ['promo_generation', 'schedule_plug'])
    assert 'Generate Highlights' in b and 'Generate Artwork Animation' in b and 'Generate Shorts' not in b


def test_the_quick_action_icons_have_one_fixed_size():
    c = main.app.test_client()
    with c.session_transaction() as s:
        s.update(authed=True, user_id=1, username='u', role='admin', csrf_token='t')
    html = c.get('/').get_data(as_text=True)
    assert '.dash-actions .tab-icon svg{width:18px;height:18px' in html

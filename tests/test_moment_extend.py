"""Moments can be made longer, not only shorter (JUN): Set In/Out here reaches past the ends."""
import unittest.mock as mock

with mock.patch('requests.post'), mock.patch('requests.get'):
    import main


def _page():
    c = main.app.test_client()
    with c.session_transaction() as s:
        s.update(authed=True, user_id=1, username='admin', role='admin', csrf_token='t')
    return c.get('/').get_data(as_text=True)


def test_the_preview_can_play_beyond_the_ends_and_set_in_out_account_for_it():
    html = _page()
    assert 'id=ws-beyond' in html and 'show 5 s beyond the ends' in html
    assert "start: c.start - WS.lead, end: c.end + WS.tail" in html, 'the padded range is what is played'
    # Set In/Out and cuts read the playhead on the source clock, not relative to the unpadded clip
    assert html.count('c.start - WS.lead + (v.currentTime || 0)') == 2
    assert 'function shPartBounds' in html, 'never past the part the moment is in'


def test_the_docs_say_what_the_trim_limits_really_are():
    html = _page()
    assert 'outside the lengths set for the cut is refused' not in html
    assert '3 seconds up to 5 minutes' in html

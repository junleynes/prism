"""
Requested by JUN: Episodic Plug is now "Highlights Video" and Schedule Plug
"Artwork Animation"; in their projects, "Episode" is "Program Code" and
"Episode title" is "Title". Internal names (permission keys, URLs, job kinds)
are unchanged, so saved groups, projects and jobs carry on.
"""
import os
import unittest.mock as mock

with mock.patch('requests.post'), mock.patch('requests.get'):
    import main
    import auth

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _page():
    c = main.app.test_client()
    with c.session_transaction() as s:
        s.update(authed=True, user_id=1, username='admin', role='admin', csrf_token='t')
    return c.get('/').get_data(as_text=True)


def test_the_tools_have_their_new_names():
    html = _page()
    for old in ('Episodic Plug', 'Episodic plug', 'Schedule Plug', 'Schedule plug', 'Generate Promo Plug',
                'Saved plugs'):
        assert old not in html, old
    assert 'Highlights Video' in html and 'Artwork Animation' in html
    labels = dict(auth.AVAILABLE_PERMISSIONS)
    assert labels['promo_generation'] == 'Highlights Video' and labels['schedule_plug'] == 'Artwork Animation'


def test_highlights_and_artwork_projects_ask_for_a_program_code_and_a_title():
    js = open(os.path.join(HERE, 'static', 'projects.js'), encoding='utf-8').read()
    assert '>Program Code:</label>' in js and '>Title:</label>' in js
    assert '>Episode:</label>' not in js and '>Episode title:</label>' not in js


def test_no_script_line_is_glued_to_the_call_before_it():
    """A line starting with '(' right after one ending in ')' is read as calling the previous result: that
    broke the Highlights Video tab's saved-videos panel (document.addEventListener(...)(function(){...}))."""
    import re
    src = open(os.path.join(HERE, 'templates', 'index.html'), encoding='utf-8').read().split('\n')
    last = ''
    for n, line in enumerate(src, 1):
        if line.strip():
            assert not (re.match(r'\s*\(', line) and re.search(r'\)\s*$', last)), f'index.html line {n}'
            last = line

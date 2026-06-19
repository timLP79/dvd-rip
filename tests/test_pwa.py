import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_ROOT", str(tmp_path / "output"))
    monkeypatch.setenv("DVD_DEVICE", "/dev/null")
    monkeypatch.setenv("TMDB_API_KEY", "")
    monkeypatch.setenv("LIBRARY_DB", str(tmp_path / "library.db"))
    from importlib import reload
    from app import main as main_module
    reload(main_module)
    with TestClient(main_module.app) as c:
        yield c


def test_service_worker_served_at_root_scope(client):
    res = client.get("/sw.js")
    assert res.status_code == 200
    assert "javascript" in res.headers["content-type"]
    assert "addEventListener('push'" in res.text
    assert "no-cache" in res.headers.get("cache-control", "")


def test_manifest_served(client):
    res = client.get("/static/manifest.webmanifest")
    assert res.status_code == 200
    assert '"display": "standalone"' in res.text


def test_index_links_manifest_and_theme(client):
    html = client.get("/").text
    assert 'rel="manifest"' in html
    assert 'name="theme-color"' in html


def test_notify_button_present(client):
    assert 'id="btn-notify"' in client.get("/").text


def test_app_js_wires_push(client):
    js = client.get("/static/app.js").text
    assert "/api/push/key" in js
    assert "pushManager.subscribe" in js
    assert "/sw.js" in js


def test_mobile_titles_reflow_present(client):
    css = client.get("/static/style.css").text
    assert "max-width: 480px" in css   # phone breakpoint added
    assert "#titles thead" in css      # table-to-cards reflow
    assert "col-duration::before" in css  # CSS-supplied row labels


def test_mobile_library_reflow_present(client):
    # The library table needs the same stacked-card reflow as the rip page,
    # driven by data-label attributes set in library.js.
    assert 'id="library"' in client.get("/library").text
    css = client.get("/static/style.css").text
    assert "#library thead" in css
    assert "content: attr(data-label)" in css
    js = client.get("/static/library.js").text
    assert "dataset.label" in js
    assert "lib-title" in js


def test_library_filter_controls_present(client):
    html = client.get("/library").text
    for marker in (
        'id="lib-search"', 'id="lib-type"', 'id="lib-format"', 'id="lib-status"',
        'id="lib-date-from"', 'id="lib-date-to"', 'id="lib-clear"',
        'id="lib-pager"', 'id="lib-prev"', 'id="lib-next"',
    ):
        assert marker in html, marker
    assert 'value="done" selected' in html      # status defaults to Done
    assert 'id="show-failed"' not in html        # old checkbox removed


def test_library_filter_styles_present(client):
    css = client.get("/static/style.css").text
    assert ".lib-filters" in css
    assert ".lib-pager" in css


def test_library_js_filter_paginate_logic(client):
    js = client.get("/static/library.js").text
    assert "PAGE_SIZE" in js
    assert "include_unsuccessful=true" in js
    assert "function matches" in js
    assert "No rips match your filters" in js

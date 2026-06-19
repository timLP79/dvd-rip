import pytest
from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch, password):
    monkeypatch.setenv("OUTPUT_ROOT", str(tmp_path / "output"))
    monkeypatch.setenv("DVD_DEVICE", "/dev/null")
    monkeypatch.setenv("TMDB_API_KEY", "")
    monkeypatch.setenv("LIBRARY_DB", str(tmp_path / "library.db"))
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "0")  # TestClient uses http
    if password is None:
        monkeypatch.delenv("APP_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("APP_PASSWORD", password)
    from importlib import reload
    from app import main as main_module
    reload(main_module)
    return TestClient(main_module.app, follow_redirects=False)


def test_auth_disabled_when_no_password(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, None) as c:
        assert c.get("/").status_code == 200


def test_protected_routes_require_login(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, "hunter2") as c:
        assert c.get("/").status_code == 303          # redirect to /login
        assert c.get("/api/jobs").status_code == 401   # API: 401, not redirect
        assert c.get("/login").status_code == 200      # login page open
        assert c.get("/static/style.css").status_code == 200  # static exempt
        assert c.get("/sw.js").status_code == 200      # sw exempt


def test_login_flow(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, "hunter2") as c:
        assert c.post("/login", data={"password": "wrong"}).status_code == 401
        ok = c.post("/login", data={"password": "hunter2"})
        assert ok.status_code == 303
        # session cookie now lets us through
        assert c.get("/").status_code == 200
        assert c.get("/api/jobs").status_code == 200
